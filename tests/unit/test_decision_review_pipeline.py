"""Decision review inside the historical runner: approve/reject, advisory, replay, no look-ahead.

Scripted strategy doubles on synthetic bars and a fake reviewer; nothing here calls a model.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from review_support import FakeReviewer, review_registry

from mike_trade.backtest.clock import ReplayClock
from mike_trade.backtest.execution import ExecutionAssumptions, HistoricalExecutionSimulator
from mike_trade.backtest.feed import DataProvenance, HistoricalReplayFeed, ReplayEvent
from mike_trade.backtest.manifest import PeriodRole
from mike_trade.backtest.runner import BacktestRequest, BacktestRunner
from mike_trade.domain.errors import ConfigurationError
from mike_trade.domain.market import Candle, FeedProvenance, Timeframe
from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.domain.trading import (
    OrderIntent,
    OrderPurpose,
    OrderSide,
    OrderType,
    TimeInForce,
)
from mike_trade.review.analysis import compare_variants, load_run, summarize_run
from mike_trade.review.config import parse_decision_review
from mike_trade.review.evidence import UnsupportedEvidenceError
from mike_trade.review.reviewer import ReviewerTimeout
from mike_trade.review.stage import DecisionReviewStage, ReviewReplayMiss
from mike_trade.review.store import JsonlReviewStore, MemoryReviewStore, load_review_records

OPEN = datetime(2025, 3, 10, 13, 30, tzinfo=UTC)  # 09:30 ET
MIN = timedelta(minutes=1)
PROV = DataProvenance("synthetic", "IEX", False, "r1")
FEED = FeedProvenance("replay", "IEX", "synthetic", False)
Bar = tuple[str, str, str, str]


def flat(price: str = "100") -> Bar:
    return (price, price, price, price)


def events(symbol: str, bars: list[Bar]) -> list[ReplayEvent]:
    out = []
    for index, (o, h, low, c) in enumerate(bars):
        start = OPEN + index * MIN
        out.append(
            ReplayEvent.bar(
                Candle(symbol, Timeframe.M1, start, start + MIN, D(o), D(h), D(low), D(c),
                       D("100000"), FEED)
            )
        )
    return out


def intent_factory(sim: HistoricalExecutionSimulator, quantity: str = "10"):
    def factory(evaluation, context, request):  # noqa: ANN001, ANN202
        buy = evaluation.side is StrategyRole.BUY
        return OrderIntent(
            intent_id=f"i-{evaluation.signal_id}",
            client_order_id=f"c-{evaluation.signal_id}",
            account_scope_id="backtest:test",
            cycle_id=f"cycle-{evaluation.symbol}",
            symbol=evaluation.symbol,
            side=OrderSide.BUY if buy else OrderSide.SELL,
            purpose=OrderPurpose.ENTRY if buy else OrderPurpose.STRATEGY_EXIT,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            created_at=context.as_of,
            quantity=D(quantity) if buy else sim.held_quantity(evaluation.symbol),
        )

    return factory


def review_config(**overrides):  # noqa: ANN003, ANN201
    return parse_decision_review(
        {"enabled": True, "mode": "enforce", "source": "live", **overrides}, research=True
    )


def make_stage(reviewer=None, *, store=None, **overrides) -> DecisionReviewStage:  # noqa: ANN001, ANN003
    config = review_config(**overrides)
    if config.source == "replay":
        return DecisionReviewStage(config, mode="BACKTEST", replay_store=store)
    return DecisionReviewStage(
        config, mode="BACKTEST", reviewer=reviewer, cache_store=MemoryReviewStore()
    )


def run(
    bars: list[Bar],
    strategy_ids: tuple[str, ...],
    params: dict,
    result_dir: Path,
    *,
    stage: DecisionReviewStage | None = None,
    config: dict | None = None,
    symbol: str = "AAA",
):
    evts = events(symbol, bars)
    clock = ReplayClock(OPEN)
    feed = HistoricalReplayFeed(evts, PROV, clock=clock, seed=3)
    sim = HistoricalExecutionSimulator(feed, clock, ExecutionAssumptions())
    cfg = dict(config or {})
    if stage is not None:
        cfg["decision_review"] = stage.config.to_dict()
    request = BacktestRequest(
        symbols=(symbol,),
        window_start=OPEN,
        window_end=OPEN + timedelta(hours=6),
        period_role=PeriodRole.RESEARCH,
        config=cfg,
        seed=3,
        starting_capital=D("100000"),
        strategy_ids=strategy_ids,
        strategy_params=params,
        context_history_bars=50,
        result_directory=result_dir,
    )
    runner = BacktestRunner(
        review_registry(), feed, clock, sim, intent_factory=intent_factory(sim),
        review_stage=stage,
    )
    return asyncio.run(runner.run(request))


QUIET = [flat()] * 12
BUY_SELL = ("ev_buy_a", "ev_sell_a")
BUY_AT_3_SELL_AT_7 = {"ev_buy_a": {"fire_at": 3}, "ev_sell_a": {"fire_at": 7}}


def fills(result, side: str) -> list[dict]:
    return [f for f in result.fills if f["side"] == side]


# ----------------------------------------------------------------------------- approve / reject


def test_approve_permits_the_ordinary_buy_and_sell(tmp_path):
    reviewer = FakeReviewer("APPROVE")
    stage = make_stage(reviewer)
    result = run(QUIET, BUY_SELL, BUY_AT_3_SELL_AT_7, tmp_path, stage=stage)
    assert len(fills(result, "BUY")) == 1 and len(fills(result, "SELL")) == 1
    assert [r.action for r in stage.records] == ["BUY", "SELL"]
    assert [r.effect for r in stage.records] == ["APPROVED", "APPROVED"]


def test_reject_blocks_the_ordinary_buy_so_no_order_exists(tmp_path):
    stage = make_stage(FakeReviewer(lambda req: "REJECT" if req["proposal"]["action"] == "BUY"
                                    else "APPROVE"))
    result = run(QUIET, BUY_SELL, BUY_AT_3_SELL_AT_7, tmp_path, stage=stage)
    assert not result.fills and not result.intents
    assert [r.effect for r in stage.records] == ["REJECTED_BLOCKED"]


def test_reject_blocks_a_strategy_sell_and_leaves_the_protective_stop_in_place(tmp_path):
    stage = make_stage(FakeReviewer(lambda req: "REJECT" if req["proposal"]["action"] == "SELL"
                                    else "APPROVE"))
    result = run(
        QUIET, BUY_SELL, BUY_AT_3_SELL_AT_7, tmp_path, stage=stage,
        config={"protective_stop": {"type": "fixed", "stop_percent": "2"}},
    )
    assert len(fills(result, "BUY")) == 1 and not fills(result, "SELL")
    assert result.cycles[0]["closed_at"] is None  # still open, still protected
    events_ = [e["event_type"] for e in result.protection_events]
    assert "INITIAL_STOP_ESTABLISHED" in events_ and "STOP_CANCELED" not in events_
    sell_review = stage.records[-1]
    assert sell_review.action == "SELL" and sell_review.effect == "REJECTED_BLOCKED"
    # the SELL request carried the real position and the existing stop
    request = sell_review.request
    assert D(request["position"]["quantity"]) == 10
    assert D(request["position"]["entry_price"]) == 100
    assert request["protective_stop"]["kind"] and request["protective_stop"]["current_stop"]


def test_advisory_records_the_opinion_without_changing_execution(tmp_path):
    baseline = run(QUIET, BUY_SELL, BUY_AT_3_SELL_AT_7, tmp_path / "a")
    stage = make_stage(FakeReviewer("REJECT"), mode="advisory")
    advised = run(QUIET, BUY_SELL, BUY_AT_3_SELL_AT_7, tmp_path / "b", stage=stage)
    assert advised.fills == baseline.fills
    assert advised.cycles == baseline.cycles
    assert [r.effect for r in stage.records] == [
        "ADVISORY_REJECT_NOT_ENFORCED", "ADVISORY_REJECT_NOT_ENFORCED"
    ]


def test_disabled_review_leaves_results_and_payload_untouched(tmp_path):
    baseline = run(QUIET, BUY_SELL, BUY_AT_3_SELL_AT_7, tmp_path / "a")
    from mike_trade.review.factory import build_stage  # an inactive config yields no stage

    assert build_stage(parse_decision_review({"enabled": False}), mode="BACKTEST") is None
    assert build_stage(parse_decision_review({"enabled": True, "mode": "disabled"}),
                       mode="BACKTEST") is None
    again = run(QUIET, BUY_SELL, BUY_AT_3_SELL_AT_7, tmp_path / "b")
    assert again.fills == baseline.fills and again.decisions == baseline.decisions
    payload = json.loads(next((tmp_path / "a").rglob("*.results.json")).read_text())
    assert "reviews" not in payload and "review_validity" not in payload


def test_only_buy_or_only_sell_review_scope(tmp_path):
    buy_only = make_stage(FakeReviewer("APPROVE"), review_sell=False)
    run(QUIET, BUY_SELL, BUY_AT_3_SELL_AT_7, tmp_path / "a", stage=buy_only)
    assert [r.action for r in buy_only.records] == ["BUY"]
    sell_only = make_stage(FakeReviewer("APPROVE"), review_buy=False)
    run(QUIET, BUY_SELL, BUY_AT_3_SELL_AT_7, tmp_path / "b", stage=sell_only)
    assert [r.action for r in sell_only.records] == ["SELL"]


def test_strategy_and_symbol_selection_leave_other_strategies_unreviewed(tmp_path):
    reviewer = FakeReviewer("REJECT")
    stage = make_stage(reviewer, strategies=["ev_buy_b"])
    result = run(QUIET, ("ev_buy_a", "ev_buy_b"), {"ev_buy_a": {"fire_at": 3}}, tmp_path,
                 stage=stage)
    assert len(fills(result, "BUY")) == 1 and not stage.records  # ev_buy_a not selected
    other_symbol = make_stage(FakeReviewer("REJECT"), symbols=["ZZZ"])
    result = run(QUIET, ("ev_buy_a",), {"ev_buy_a": {"fire_at": 3}}, tmp_path / "x",
                 stage=other_symbol)
    assert len(fills(result, "BUY")) == 1 and not other_symbol.records


def test_multiple_contributing_strategies_are_all_named_with_their_own_evidence(tmp_path):
    reviewer = FakeReviewer("APPROVE")
    stage = make_stage(reviewer)
    run(QUIET, ("ev_buy_a", "ev_buy_b"),
        {"ev_buy_a": {"fire_at": 3}, "ev_buy_b": {"fire_at": 3}}, tmp_path, stage=stage)
    (record,) = stage.records
    assert record.strategy_ids == ("ev_buy_a", "ev_buy_b")
    strategies = {s["strategy_id"]: s for s in record.request["strategies"]}
    assert set(strategies) == {"ev_buy_a", "ev_buy_b"}
    assert strategies["ev_buy_a"]["evidence"]["evidence"]["ev_buy_a_only"] is True
    assert strategies["ev_buy_b"]["evidence"]["evidence"]["ev_buy_b_only"] is True
    assert sum(1 for s in strategies.values() if s["selected"]) == 1
    assert record.request["proposal"]["originating_strategy_ids"] == ["ev_buy_a", "ev_buy_b"]


# ------------------------------------------------------------------------ unsupported strategies


def test_unsupported_strategy_in_scope_is_a_clear_configuration_error(tmp_path):
    stage = make_stage(FakeReviewer("APPROVE"))
    with pytest.raises(UnsupportedEvidenceError) as caught:
        run(QUIET, ("plain_buy",), {"plain_buy": {"fire_at": 3}}, tmp_path, stage=stage)
    assert "plain_buy" in str(caught.value) and caught.value.code == "STRATEGY_EVIDENCE_UNSUPPORTED"
    assert isinstance(caught.value, ConfigurationError)


def test_unsupported_strategy_is_fine_when_it_is_not_reviewed(tmp_path):
    # excluded by strategies:, by review_buy: false, and with no stage at all
    run(QUIET, ("plain_buy", "ev_buy_a"), {"ev_buy_a": {"fire_at": 3}}, tmp_path / "a",
        stage=make_stage(FakeReviewer(), strategies=["ev_buy_a"]))
    run(QUIET, ("plain_buy",), {"plain_buy": {"fire_at": 3}}, tmp_path / "b",
        stage=make_stage(FakeReviewer(), review_buy=False))
    result = run(QUIET, ("plain_buy",), {"plain_buy": {"fire_at": 3}}, tmp_path / "c")
    assert len(fills(result, "BUY")) == 1


def test_naming_a_strategy_that_is_not_configured_is_an_error(tmp_path):
    with pytest.raises(ConfigurationError, match="not configured"):
        run(QUIET, ("ev_buy_a",), {"ev_buy_a": {"fire_at": 3}}, tmp_path,
            stage=make_stage(FakeReviewer(), strategies=["ghost"]))


# ------------------------------------------------------------------------------- error policies


@pytest.mark.parametrize(
    "error",
    [ReviewerTimeout("slow"), RuntimeError("provider exploded")],
)
def test_reviewer_errors_follow_the_configured_policy(tmp_path, error):
    bypass = make_stage(FakeReviewer(error=error), on_error="bypass")
    result = run(QUIET, BUY_SELL, BUY_AT_3_SELL_AT_7, tmp_path / "a", stage=bypass)
    assert len(fills(result, "BUY")) == 1 and len(fills(result, "SELL")) == 1
    assert {r.effect for r in bypass.records} == {"ERROR_BYPASSED"}
    assert all(r.error and r.response is None for r in bypass.records)
    reject = make_stage(FakeReviewer(error=error), on_error="reject")
    result = run(QUIET, BUY_SELL, BUY_AT_3_SELL_AT_7, tmp_path / "b", stage=reject)
    assert not result.fills
    assert {r.effect for r in reject.records} == {"ERROR_REJECTED_BLOCKED"}


def test_a_real_timeout_is_enforced_and_recorded(tmp_path):
    stage = make_stage(FakeReviewer(delay=1.0), timeout_seconds=0.05, on_error="reject")
    result = run(QUIET, ("ev_buy_a",), {"ev_buy_a": {"fire_at": 3}}, tmp_path, stage=stage)
    assert not result.fills
    assert stage.records[0].error["type"] == "ReviewerTimeout"


def test_advisory_mode_never_blocks_even_with_reject_on_error(tmp_path):
    stage = make_stage(FakeReviewer(error=RuntimeError("x")), mode="advisory", on_error="reject")
    result = run(QUIET, BUY_SELL, BUY_AT_3_SELL_AT_7, tmp_path, stage=stage)
    assert len(fills(result, "BUY")) == 1 and len(fills(result, "SELL")) == 1
    assert {r.effect for r in stage.records} == {"ADVISORY_ERROR"}


# --------------------------------------------------------------------- protective / emergency


def test_protective_and_end_of_day_exits_are_never_reviewed(tmp_path):
    stage = make_stage(FakeReviewer("REJECT"), review_buy=False)  # only SELL review is on
    bars = [flat()] * 4 + [("100", "100", "90", "90")] + [flat("90")] * 8
    result = run(
        bars, ("ev_buy_a",), {"ev_buy_a": {"fire_at": 3}}, tmp_path / "a", stage=stage,
        config={"protective_stop": {"type": "fixed", "stop_percent": "2"}},
    )
    stop_exit = next(f for f in result.fills if f["side"] == "SELL")
    assert stop_exit["exit_reason"] == "PROTECTIVE_STOP"
    assert not stage.records

    eod = make_stage(FakeReviewer("REJECT"), review_buy=False)
    result = run(
        [flat()] * 9, ("ev_buy_a",), {"ev_buy_a": {"fire_at": 3}}, tmp_path / "b", stage=eod,
        config={"end_of_day_close": {"enabled": True, "at": "09:35"},
                "protective_stop": {"type": "fixed", "stop_amount": "5"}},
    )
    assert any(f["purpose"] == "EOD_CLOSE" for f in result.fills) and not eod.records


def test_mandatory_exit_sources_cannot_be_put_through_review():
    from mike_trade.review.proposal import Contributor, TradeProposal
    from mike_trade.review.stage import MandatoryExitReviewError

    from review_core_helpers import position_view, simple_context  # noqa: PLC0415

    stage = make_stage(FakeReviewer())
    context = simple_context()
    for source in ("PROTECTIVE_STOP", "EMERGENCY_LIQUIDATION", "EOD", "KILL_SWITCH", None):
        proposal = TradeProposal(
            mode="BACKTEST", action="SELL", symbol="AAA", as_of=context.as_of,
            selected_strategy_id="ev_sell_a", selected_signal_id="s1",
            contributors=(Contributor("ev_sell_a", "1.0.0", "s1", True),),
            position=position_view(), exit_source=source,
        )
        with pytest.raises(MandatoryExitReviewError):
            asyncio.run(stage.review(proposal, context))


# ------------------------------------------------------------------------- replay & caching


def test_recorded_reviews_replay_the_run_without_calling_a_model(tmp_path):
    reviewer = FakeReviewer(lambda req: "REJECT" if req["proposal"]["action"] == "SELL"
                            else "APPROVE")
    stage = make_stage(reviewer)
    live = run(QUIET, BUY_SELL, BUY_AT_3_SELL_AT_7, tmp_path / "live", stage=stage,
               config={"protective_stop": {"type": "fixed", "stop_percent": "2"}})
    assert len(reviewer.calls) == 2
    sidecars = list((tmp_path / "live").rglob("*.reviews.jsonl"))
    assert len(sidecars) == 1
    recorded = load_review_records(sidecars)
    assert [r.source for r in recorded] == ["live_inference", "live_inference"]
    assert all(r.usage and r.latency_ms is not None and r.requested_at for r in recorded)

    replay_stage = make_stage(
        None, store=JsonlReviewStore(read_paths=[tmp_path / "live"]),
        source="replay", replay_from=[str(tmp_path / "live")],
    )
    replayed = run(QUIET, BUY_SELL, BUY_AT_3_SELL_AT_7, tmp_path / "replay", stage=replay_stage,
                   config={"protective_stop": {"type": "fixed", "stop_percent": "2"}})
    assert replayed.fills == live.fills and replayed.cycles == live.cycles
    assert [r.source for r in replay_stage.records] == ["replay", "replay"]
    assert [r.review_id for r in replay_stage.records] == [r.review_id for r in stage.records]
    # the deterministic review ledger embedded in the result is identical too
    live_payload = json.loads(next((tmp_path / "live").rglob("*.results.json")).read_text())
    replay_payload = json.loads(next((tmp_path / "replay").rglob("*.results.json")).read_text())
    assert replay_payload["reviews"] == live_payload["reviews"]
    assert reviewer.calls.__len__() == 2  # replay made no further calls


def test_replay_with_a_missing_recording_fails_loudly_instead_of_bypassing(tmp_path):
    reviewer = FakeReviewer("APPROVE")
    run(QUIET, ("ev_buy_a",), {"ev_buy_a": {"fire_at": 3}}, tmp_path / "live",
        stage=make_stage(reviewer))
    other_bars = [flat("101")] * 12  # different prices -> different complete input
    replay_stage = make_stage(
        None, store=JsonlReviewStore(read_paths=[tmp_path / "live"]),
        source="replay", replay_from=[str(tmp_path / "live")],
    )
    with pytest.raises(ReviewReplayMiss):
        run(other_bars, ("ev_buy_a",), {"ev_buy_a": {"fire_at": 3}}, tmp_path / "r",
            stage=replay_stage)


def test_identical_inputs_are_not_paid_for_twice_across_runs(tmp_path):
    reviewer = FakeReviewer("APPROVE")
    cache = JsonlReviewStore(tmp_path / "cache.reviews.jsonl")
    config = review_config()
    first = DecisionReviewStage(config, mode="BACKTEST", reviewer=reviewer, cache_store=cache)
    run(QUIET, BUY_SELL, BUY_AT_3_SELL_AT_7, tmp_path / "a", stage=first)
    assert len(reviewer.calls) == 2
    second = DecisionReviewStage(
        config, mode="BACKTEST", reviewer=reviewer,
        cache_store=JsonlReviewStore(tmp_path / "cache.reviews.jsonl"),
    )
    run(QUIET, BUY_SELL, BUY_AT_3_SELL_AT_7, tmp_path / "b", stage=second)
    assert len(reviewer.calls) == 2  # served from the shared cache
    assert [r.source for r in second.records] == ["cache", "cache"]
    # a different reviewer identity does not reuse them
    different = FakeReviewer("APPROVE", model="another-model")
    third = DecisionReviewStage(
        config, mode="BACKTEST", reviewer=different,
        cache_store=JsonlReviewStore(tmp_path / "cache.reviews.jsonl"),
    )
    run(QUIET, BUY_SELL, BUY_AT_3_SELL_AT_7, tmp_path / "c", stage=third)
    assert len(different.calls) == 2


# ----------------------------------------------------------------------- no future information


def test_requests_never_contain_prices_or_candles_from_after_the_decision(tmp_path):
    secret = "987.65"
    bars = [flat()] * 6 + [(secret, secret, secret, secret)] * 6  # the future, after bar 3's signal
    reviewer = FakeReviewer("APPROVE")
    stage = make_stage(reviewer)
    run(bars, ("ev_buy_a",), {"ev_buy_a": {"fire_at": 3}}, tmp_path, stage=stage)
    (request,) = reviewer.calls
    decision_time = request["proposal"]["decision_time"]
    assert secret not in json.dumps(request)
    for series in request["market"]["candles"].values():
        assert all(candle["end"] <= decision_time for candle in series)
    assert request["provenance"]["latest_candle_end"]["1m"] <= decision_time


def test_the_request_builder_refuses_a_candle_after_the_decision_time():
    from mike_trade.review.proposal import (
        Contributor,
        ReviewLookAheadError,
        TradeProposal,
        build_review_request,
    )

    from review_core_helpers import simple_context  # noqa: PLC0415

    context = simple_context()
    proposal = TradeProposal(
        mode="BACKTEST", action="BUY", symbol="AAA", as_of=context.as_of - timedelta(minutes=2),
        selected_strategy_id="ev_buy_a", selected_signal_id="s1",
        contributors=(Contributor("ev_buy_a", "1.0.0", "s1", True),),
    )
    with pytest.raises(ReviewLookAheadError):
        build_review_request(proposal, context)


# --------------------------------------------------------------- analysis & point-in-time honesty


def test_outcome_analysis_separates_executed_results_from_counterfactuals(tmp_path):
    base = run(QUIET, BUY_SELL, BUY_AT_3_SELL_AT_7, tmp_path / "base")
    buy_review = make_stage(FakeReviewer("APPROVE"), review_sell=False)
    sell_reject = make_stage(
        FakeReviewer(lambda req: "REJECT" if req["proposal"]["action"] == "SELL" else "APPROVE")
    )
    run(QUIET, BUY_SELL, BUY_AT_3_SELL_AT_7, tmp_path / "buy", stage=buy_review)
    run(QUIET, BUY_SELL, BUY_AT_3_SELL_AT_7, tmp_path / "both", stage=sell_reject,
        config={"protective_stop": {"type": "fixed", "stop_percent": "2"}})
    summaries = {}
    for name in ("base", "buy", "both"):
        found = load_run(next((tmp_path / name).rglob("*.results.json")))
        summaries[name] = summarize_run(found["payload"], found["records"])
    summaries["baseline"] = summaries.pop("base")
    both = summaries["both"]
    blocked = [r for r in both["rows"] if r["blocked_by_review"]]
    assert len(blocked) == 1 and blocked[0]["action"] == "SELL"
    assert blocked[0]["executed"] is False
    assert blocked[0]["counterfactual"] is None  # never invented
    assert blocked[0]["counterfactual_status"] == "not_estimated"
    approved = [r for r in both["rows"] if r["executed"]]
    assert approved and approved[0]["actual_outcome"]["cycle_id"]
    assert both["reviews"]["total_cost"] != "0" and both["reviews"]["model_calls"] == 2
    comparison = compare_variants(summaries)
    assert set(comparison["variants"]) == {"baseline", "buy", "both"}
    assert "separate runs" in comparison["caveat"]
    assert base.fills  # sanity


def test_a_reviewer_not_attested_point_in_time_is_flagged_in_the_run_artifact(tmp_path):
    stage = make_stage(FakeReviewer("APPROVE", point_in_time_safe=False))
    run(QUIET, ("ev_buy_a",), {"ev_buy_a": {"fire_at": 3}}, tmp_path / "a", stage=stage)
    payload = json.loads(next((tmp_path / "a").rglob("*.results.json")).read_text())
    assert payload["review_validity"]["point_in_time_valid"] is False
    attested = make_stage(FakeReviewer("APPROVE", point_in_time_safe=True))
    run(QUIET, ("ev_buy_a",), {"ev_buy_a": {"fire_at": 3}}, tmp_path / "b", stage=attested)
    payload = json.loads(next((tmp_path / "b").rglob("*.results.json")).read_text())
    assert payload["review_validity"]["point_in_time_valid"] is True
