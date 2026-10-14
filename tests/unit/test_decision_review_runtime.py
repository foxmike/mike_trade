"""Decision review in the SHADOW and PAPER runtimes (real graphs, fake reviewer, no network)."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import timedelta
from decimal import Decimal as D

import pytest
from review_support import FakeReviewer, review_registry
from test_strategy_collections_runtime import SCENARIO, Clock, ctx, one_symbol, orders, start
from tests.live_support import ctx as live_ctx
from tests.live_support import start_paper
from tests.support.fake_alpaca import FakeAlpacaExchange

from mike_trade.app import SymbolStrategySettings
from mike_trade.domain.trading import OrderPurpose
from mike_trade.review.config import parse_decision_review
from mike_trade.review.evidence import UnsupportedEvidenceError
from mike_trade.review.stage import DecisionReviewStage
from mike_trade.review.store import MemoryReviewStore
from test_strategy_collections_runtime import assign  # noqa: E402


def stage_for(reviewer, mode_name: str, **overrides) -> DecisionReviewStage:  # noqa: ANN003
    config = parse_decision_review(
        {"enabled": True, "mode": "enforce", "source": "live", **overrides}
    )
    return DecisionReviewStage(
        config, mode=mode_name, reviewer=reviewer, cache_store=MemoryReviewStore()
    )


BUY = [("ev_buy_a", {"fire_at": 3})]
SELL = [("ev_sell_a", {"fire_at": 5})]


async def started(tmp_path, clock, buys, sells, reviewer, **stage_options):
    runtime, shadow = await start(
        tmp_path, clock, one_symbol(buys, sells), registry=review_registry()
    )
    stage = stage_for(reviewer, "SHADOW", **stage_options)
    runtime.lifecycle.bind_review_stage(stage, ("SPY",))
    return runtime, shadow, stage


# ------------------------------------------------------------------------------------ SHADOW


async def test_shadow_approve_executes_the_entry_in_the_same_pass(tmp_path):
    clock = Clock()
    runtime, shadow, stage = await started(tmp_path, clock, BUY, [], FakeReviewer("APPROVE"))
    try:
        out = await runtime.lifecycle.process({"SPY": ctx("SPY", clock.value, 3)})
        assert any(a.startswith("ENTRY:") for a in out[SCENARIO])
        assert "SPY" in shadow.account.state.positions
        (record,) = stage.records
        assert record.mode == "SHADOW" and record.action == "BUY" and record.effect == "APPROVED"
    finally:
        await runtime.stop()


async def test_shadow_reject_blocks_the_entry_and_holds_no_budget(tmp_path):
    clock = Clock()
    runtime, shadow, stage = await started(tmp_path, clock, BUY, [], FakeReviewer("REJECT"))
    try:
        out = await runtime.lifecycle.process({"SPY": ctx("SPY", clock.value, 3)})
        assert out[SCENARIO] == ["REVIEW_BLOCKED:BUY:SPY:REJECTED_BLOCKED"]
        assert not shadow.account.state.positions and not shadow.account.state.orders
    finally:
        await runtime.stop()


async def test_shadow_sell_reject_keeps_the_position_and_its_protective_stop(tmp_path):
    clock = Clock()
    reviewer = FakeReviewer(lambda r: "REJECT" if r["proposal"]["action"] == "SELL" else "APPROVE")
    runtime, shadow, stage = await started(tmp_path, clock, BUY, SELL, reviewer)
    try:
        life = runtime.lifecycle
        await life.process({"SPY": ctx("SPY", clock.value, 3)})
        position = shadow.account.state.positions["SPY"]
        clock.value += timedelta(minutes=2)
        out = await life.process({"SPY": ctx("SPY", clock.value, 5)})
        assert out[SCENARIO] == ["REVIEW_BLOCKED:SELL:SPY:REJECTED_BLOCKED"]
        assert shadow.account.state.positions["SPY"].quantity == position.quantity
        assert shadow.stops.state(position.cycle_id).status == "ACTIVE"
        assert not orders(shadow, OrderPurpose.STRATEGY_EXIT)
        sell_request = stage.records[-1].request
        assert sell_request["position"]["quantity"] == str(position.quantity)
        assert sell_request["protective_stop"]["current_stop"] is not None
    finally:
        await runtime.stop()


async def test_shadow_advisory_never_waits_and_never_blocks(tmp_path):
    clock = Clock()
    reviewer = FakeReviewer("REJECT", delay=0.2)
    runtime, shadow, stage = await started(
        tmp_path, clock, BUY, [], reviewer, mode="advisory"
    )
    try:
        out = await asyncio.wait_for(
            runtime.lifecycle.process({"SPY": ctx("SPY", clock.value, 3)}), timeout=0.15
        )
        assert any(a.startswith("ENTRY:") for a in out[SCENARIO])  # did not wait for the review
        await runtime.lifecycle.drain()
        (record,) = stage.records
        assert record.effect == "ADVISORY_REJECT_NOT_ENFORCED"
    finally:
        await runtime.stop()


async def test_shadow_reviews_for_different_symbols_run_concurrently_then_execute_in_order(tmp_path):
    clock = Clock()
    cfg = {
        "symbols": ("SPY", "QQQ"),
        "symbol_strategies": tuple(
            SymbolStrategySettings(s, assign(*BUY), ()) for s in ("SPY", "QQQ")
        ),
    }
    runtime, shadow = await start(tmp_path, clock, cfg, registry=review_registry())
    reviewer = FakeReviewer("APPROVE", delay=0.1)
    stage = stage_for(reviewer, "SHADOW")
    runtime.lifecycle.bind_review_stage(stage, ("SPY", "QQQ"))
    try:
        started_at = asyncio.get_running_loop().time()
        out = await runtime.lifecycle.process(
            {"SPY": ctx("SPY", clock.value, 3), "QQQ": ctx("QQQ", clock.value, 3)}
        )
        elapsed = asyncio.get_running_loop().time() - started_at
        assert reviewer.max_in_flight == 2 and elapsed < 0.19  # overlapped, not 2 x 0.1
        assert sum(a.startswith("ENTRY:") for a in out[SCENARIO]) == 2
    finally:
        await runtime.stop()


async def test_shadow_a_changed_state_while_the_review_was_pending_blocks_the_entry(tmp_path):
    clock = Clock()
    runtime = shadow = None
    holder: dict = {}

    def decide(_request):
        holder["life"]._entry_permitted = lambda _direction: False  # the gate closes meanwhile
        return "APPROVE"

    runtime, shadow, stage = await started(tmp_path, clock, BUY, [], FakeReviewer(decide))
    holder["life"] = runtime.lifecycle
    try:
        out = await runtime.lifecycle.process({"SPY": ctx("SPY", clock.value, 3)})
        assert out[SCENARIO] == ["REVIEW_BLOCKED:BUY:SPY:STALE_BLOCKED"]
        assert not shadow.account.state.orders
        assert any(r.source == "disposition" for r in stage.records)
    finally:
        await runtime.stop()


async def test_shadow_reviewer_error_follows_on_error_reject(tmp_path):
    clock = Clock()
    runtime, shadow, stage = await started(
        tmp_path, clock, BUY, [], FakeReviewer(error=RuntimeError("down")), on_error="reject"
    )
    try:
        out = await runtime.lifecycle.process({"SPY": ctx("SPY", clock.value, 3)})
        assert out[SCENARIO] == ["REVIEW_BLOCKED:BUY:SPY:ERROR_REJECTED_BLOCKED"]
        assert not shadow.account.state.orders
    finally:
        await runtime.stop()


async def test_shadow_protective_stop_exit_is_never_reviewed(tmp_path):
    clock = Clock()
    runtime, shadow, stage = await started(
        tmp_path, clock, BUY, [], FakeReviewer("REJECT"), review_buy=False
    )
    try:
        life = runtime.lifecycle
        await life.process({"SPY": ctx("SPY", clock.value, 3)})  # unreviewed entry
        clock.value += timedelta(minutes=2)
        crash = await life.process({"SPY": ctx("SPY", clock.value, 4, bid="90", ask="90.1")})
        assert "STOP_EXIT" in crash[SCENARIO] and not stage.records
        assert "SPY" not in shadow.account.state.positions
    finally:
        await runtime.stop()


async def test_shadow_unsupported_strategy_fails_closed_at_startup(tmp_path):
    clock = Clock()
    runtime, _shadow = await start(
        tmp_path, clock, one_symbol([("plain_buy", {"fire_at": 3})], []),
        registry=review_registry(),
    )
    try:
        with pytest.raises(UnsupportedEvidenceError, match="plain_buy"):
            runtime.lifecycle.bind_review_stage(
                stage_for(FakeReviewer(), "SHADOW"), ("SPY",)
            )
        # excluded from review -> fine
        runtime.lifecycle.bind_review_stage(
            stage_for(FakeReviewer(), "SHADOW", review_buy=False), ("SPY",)
        )
    finally:
        await runtime.stop()


# ------------------------------------------------------------------------------------ PAPER


@pytest.fixture
def exchange() -> FakeAlpacaExchange:
    ex = FakeAlpacaExchange()
    ex.set_price("AAA", "100")
    return ex


def db_rows(tmp_path, sql):
    conn = sqlite3.connect(tmp_path / "paper.sqlite3")
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


async def paper(tmp_path, exchange, buys, sells, reviewer, **options):
    runtime = await start_paper(tmp_path, exchange, buys, sells, registry=review_registry())
    stage = stage_for(reviewer, "PAPER", **options)
    runtime.lifecycle.bind_review_stage(stage, ("AAA",))
    return runtime, stage


async def test_paper_approve_places_the_broker_entry_and_protects_it(tmp_path, exchange):
    runtime, stage = await paper(tmp_path, exchange, BUY, [], FakeReviewer("APPROVE"))
    try:
        out = await runtime.lifecycle.process({"AAA": live_ctx("AAA", exchange.clock.now(), 3)})
        assert "PROTECTED" in out["live"] and exchange.position_qty("AAA") == D("29")
        (record,) = stage.records
        assert record.mode == "PAPER" and record.effect == "APPROVED"
    finally:
        await runtime.stop()


async def test_paper_reject_sends_nothing_to_the_broker(tmp_path, exchange):
    runtime, stage = await paper(tmp_path, exchange, BUY, [], FakeReviewer("REJECT"))
    try:
        out = await runtime.lifecycle.process({"AAA": live_ctx("AAA", exchange.clock.now(), 3)})
        assert out["live"] == ["REVIEW_BLOCKED:BUY:AAA:REJECTED_BLOCKED"]
        assert not exchange.posts() and exchange.position_qty("AAA") == 0
        assert db_rows(tmp_path, "SELECT COUNT(*) FROM position_cycles") == [(0,)]
    finally:
        await runtime.stop()


async def test_paper_sell_review_waits_outside_the_reconciler_lock(tmp_path, exchange):
    holder: dict = {}
    seen: list[bool] = []

    def decide(request):
        seen.append(holder["life"].reconciler.lock.locked())
        return "APPROVE"

    runtime, stage = await paper(tmp_path, exchange, BUY, SELL, FakeReviewer(decide))
    holder["life"] = runtime.lifecycle
    try:
        life = runtime.lifecycle
        await life.process({"AAA": live_ctx("AAA", exchange.clock.now(), 3)})
        exchange.clock.advance(minutes=2)
        exchange.set_price("AAA", "101")
        out = await life.process({"AAA": live_ctx("AAA", exchange.clock.now(), 5, "101")})
        assert any(a.startswith("EXIT:FLAT") for a in out["live"])
        assert seen == [False, False]  # neither review held the lock that other work needs
        assert exchange.position_qty("AAA") == 0
    finally:
        await runtime.stop()


async def test_paper_sell_reject_leaves_the_stop_and_the_position_untouched(tmp_path, exchange):
    reviewer = FakeReviewer(lambda r: "REJECT" if r["proposal"]["action"] == "SELL" else "APPROVE")
    runtime, stage = await paper(tmp_path, exchange, BUY, SELL, reviewer)
    try:
        life = runtime.lifecycle
        await life.process({"AAA": live_ctx("AAA", exchange.clock.now(), 3)})
        (stop,) = exchange.open_stops("AAA")
        exchange.clock.advance(minutes=2)
        out = await life.process({"AAA": live_ctx("AAA", exchange.clock.now(), 5)})
        assert out["live"] == ["REVIEW_BLOCKED:SELL:AAA:REJECTED_BLOCKED"]
        assert exchange.position_qty("AAA") == D("29")
        assert exchange.orders[stop["id"]]["status"] not in {"canceled", "filled"}
        assert not [p for p in exchange.posts() if p["side"] == "sell" and p["type"] == "market"]
    finally:
        await runtime.stop()


async def test_paper_unsupported_strategy_fails_closed(tmp_path, exchange):
    runtime = await start_paper(
        tmp_path, exchange, [("plain_buy", {"fire_at": 3})], [], registry=review_registry()
    )
    try:
        with pytest.raises(UnsupportedEvidenceError, match="plain_buy"):
            runtime.lifecycle.bind_review_stage(stage_for(FakeReviewer(), "PAPER"), ("AAA",))
    finally:
        await runtime.stop()


def test_runtime_composition_ignores_inactive_review_and_requires_a_lifecycle_when_enabled():
    from mike_trade.app import AppSettings, _bind_decision_review
    from mike_trade.domain.errors import ConfigurationError
    from mike_trade.runtime import ExecutionMode

    _bind_decision_review(AppSettings(mode=ExecutionMode.SHADOW), None, object())  # default: no-op
    enabled = AppSettings(
        mode=ExecutionMode.SHADOW,
        decision_review={"enabled": True, "mode": "advisory", "source": "replay",
                         "replay_from": ["/nonexistent"]},
    )
    with pytest.raises(ConfigurationError, match="no strategy lifecycle"):
        _bind_decision_review(enabled, None, object())


# ----------------------------------------------------- real trained models through the research runner

from test_model_workflow import campaign_yaml, workflow  # noqa: E402, F401  (module-scoped fixture)


def test_real_model_signals_carry_their_actual_probabilities_and_features_into_reviews(
    workflow, tmp_path, monkeypatch  # noqa: F811
):
    """End to end on the production research path (tiny synthetic models, fake reviewer)."""
    import dataclasses

    from mike_trade.modeling.campaign import load_campaign, plan_campaign
    from mike_trade.research.commands import _execute_experiment

    configs = "  A_xgb_buy: {buy: [xgb_buy], sell: []}\n  C_xgb_xgb: {buy: [xgb_buy], sell: [xgb_sell]}\n"
    plan = plan_campaign(load_campaign(campaign_yaml(workflow, directory=tmp_path / "c",
                                                      configs=configs)))
    reviewer = FakeReviewer("APPROVE")
    stages: list[DecisionReviewStage] = []

    def inject(config, *, mode, **_kwargs):  # noqa: ANN001, ANN202
        if not config.active:
            return None
        stage = DecisionReviewStage(
            config, mode=mode, reviewer=reviewer, cache_store=MemoryReviewStore()
        )
        stages.append(stage)
        return stage

    monkeypatch.setattr("mike_trade.review.factory.build_stage", inject)
    review = {"enabled": True, "mode": "enforce", "source": "live", "review_sell": True}
    baseline = _execute_experiment(
        dataclasses.asdict(plan.jobs[1]), str(workflow["data"]), str(tmp_path / "base")
    )
    reviewed = _execute_experiment(
        {**dataclasses.asdict(plan.jobs[1]), "decision_review": review},
        str(workflow["data"]), str(tmp_path / "reviewed"),
    )
    assert "decision_review" not in baseline
    summary = reviewed["decision_review"]["summary"]
    assert summary["reviews"] > 0 and summary["model_calls"] == len(reviewer.calls)
    buy_requests = [c for c in reviewer.calls if c["proposal"]["action"] == "BUY"]
    assert buy_requests
    evidence = buy_requests[0]["strategies"][0]["evidence"]
    assert evidence["strategy_id"] == "xgboost_probability_buy"
    body = evidence["evidence"]
    assert set(body["probabilities"]) >= {"favorable", "adverse"}
    assert abs(sum(body["class_probabilities"].values()) - 1.0) < 1e-5
    assert body["features"] and all(isinstance(v, float) for v in body["features"].values())
    # an always-approving review leaves the trading outcome identical to the baseline
    assert reviewed["trades"] == baseline["trades"] and reviewed["net_return"] == baseline["net_return"]


def test_campaigns_compare_variants_and_planning_makes_no_model_calls(workflow, tmp_path):  # noqa: F811
    from mike_trade.modeling.campaign import CampaignError, load_campaign, plan_campaign
    from mike_trade.modeling.preflight import format_preflight, preflight

    replay_dir = tmp_path / "recorded"
    replay_dir.mkdir()
    review = "{enabled: true, mode: enforce, source: replay, replay_from: [%s]" % replay_dir
    configs = (
        "  base: {buy: [xgb_buy], sell: []}\n"
        f"  buy_only: {{buy: [xgb_buy], sell: [], decision_review: {review}, review_sell: false}}}}\n"
        f"  both: {{buy: [xgb_buy], sell: [], decision_review: {review}}}}}\n"
    )
    path = campaign_yaml(workflow, directory=tmp_path / "c", configs=configs)
    plan = plan_campaign(load_campaign(path))
    by_name = {job.strategy: job for job in plan.jobs}
    assert by_name["base"].decision_review is None  # the baseline never has a review
    assert by_name["buy_only"].decision_review["review_sell"] is False
    assert by_name["both"].decision_review["source"] == "replay"
    report = preflight(plan, workers=1)
    assert report["decision_review"]["jobs_with_review"] == 2
    assert report["decision_review"]["jobs_without_review"] == 1
    assert report["decision_review"]["sources"] == ["replay"]
    assert "planning makes no model calls" in report["decision_review"]["note"]
    assert "decision review" in format_preflight(report)
    # an existing campaign without reviews reports exactly what it reported before
    plain = campaign_yaml(workflow, directory=tmp_path / "p")
    assert "decision_review" not in preflight(plan_campaign(load_campaign(plain)), workers=1)

    # research must choose how reviews are produced; nothing defaults to paid inference
    missing_source = path.read_text().replace("source: replay, replay_from", "x: 1, replay_from")
    path.with_name("bad.yaml").write_text(missing_source)
    with pytest.raises(CampaignError):
        load_campaign(path.with_name("bad.yaml"))
    live = path.read_text().replace("source: replay, replay_from: [%s]" % replay_dir,
                                    "source: live")
    path.with_name("live.yaml").write_text(live)
    assert plan_campaign(load_campaign(path.with_name("live.yaml"))).jobs[2].decision_review[
        "source"] == "live"


def test_experiment_specs_validate_the_review_settings():
    from mike_trade.research.commands import ExperimentSpec

    base = dict(symbol="AAA", strategy="vwap_bounce", start="2026-03-23T00:00:00Z",
                end="2026-03-26T00:00:00Z")
    ExperimentSpec(**base).validate()
    ExperimentSpec(**base, decision_review={"enabled": True, "source": "live",
                                            "mode": "advisory"}).validate()
    with pytest.raises(ValueError, match="explicitly"):
        ExperimentSpec(**base, decision_review={"enabled": True}).validate()
    with pytest.raises(ValueError, match="unknown keys"):
        ExperimentSpec(**base, decision_review={"enabled": True, "source": "live",
                                                "sneaky": 1}).validate()
