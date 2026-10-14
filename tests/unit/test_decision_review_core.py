"""Decision review building blocks: evidence, strict responses, OpenRouter adapter, config, stage.

No test makes a network call: the OpenRouter adapter runs on ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from decimal import Decimal as D

import httpx
import pytest
from review_core_helpers import AS_OF, contributor, position_view, simple_context
from review_support import FakeReviewer

from mike_trade.domain.errors import ConfigurationError
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.review.config import parse_decision_review
from mike_trade.review.evidence import (
    DecisionEvidence,
    EvidenceError,
    UnsupportedEvidenceError,
    collect_evidence,
    require_evidence_support,
    supports_evidence,
    to_json_value,
)
from mike_trade.review.factory import (
    build_stage,
    load_reviewer_entries,
    resolve_reviewer_entry,
    validate_reviewer_entries,
)
from mike_trade.review.openrouter import (
    DEFAULT_JEV_MODEL,
    OPENROUTER_API_KEY_ENV,
    OpenRouterReviewer,
)
from mike_trade.review.proposal import Contributor, StateToken, TradeProposal
from mike_trade.review.reviewer import (
    MalformedReviewResponse,
    ReviewerUnavailable,
    parse_review_payload,
)
from mike_trade.review.stage import DecisionReviewStage
from mike_trade.strategies.base import StrategyEvaluation
from mike_trade.review.store import (
    JsonlReviewStore,
    MemoryReviewStore,
    ReviewStoreError,
)

# --------------------------------------------------------------------- evidence: real strategies


def test_model_strategies_produce_evidence_from_the_actual_prediction():
    from test_model_strategies import (  # noqa: PLC0415
        BUY_CLASSES,
        SELL_CLASSES,
        context,
        decide,
        position,
        served,
    )

    assert all(supports_evidence(cls) for cls in (*BUY_CLASSES, *SELL_CLASSES))
    for cls in BUY_CLASSES:
        features = {"return_1": 0.0012, "vwap_distance": -0.0004}
        prediction = served(
            cls, probabilities={"-1": 0.1, "0": 0.2, "1": 0.7}, feature_values=features
        )
        ctx = context(cls, prediction)
        evaluation = decide(cls, ctx)
        state = {"eligibility": "WATCHING_BUY"}
        evidence = collect_evidence(
            cls(), ctx,
            cls.spec.parameter_schema.validate({"model_id": "buy_model", "model_version": "1.0.0"}),
            state, evaluation,
        )
        document = evidence.to_dict()
        assert document["strategy_id"] == cls.spec.strategy_id and document["action"] == "BUY"
        assert document["signal_id"] == evaluation.signal_id
        assert document["reason_code"] == "MODEL_FAVORABLE"
        body = document["evidence"]
        assert body["probabilities"] == {"favorable": 0.7, "adverse": 0.1, "neutral": 0.2}
        assert body["features"] == features  # the actual vector, not an estimate
        assert body["model"]["model_id"] == "buy_model"
        assert body["model"]["algorithm"] == cls.ALGORITHM
        assert body["decision_mode"] == "MODEL_DIRECTED"
        json.dumps(document)  # fully JSON-serializable
    for cls in SELL_CLASSES:
        actual = position("99.5")
        prediction = served(
            cls, probabilities={"-1": 0.15, "0": 0.55, "1": 0.30}, position=actual,
            position_features={"position_unrealized_return": 0.005},
            feature_values={"return_1": 0.001, "position_unrealized_return": 0.005},
        )
        ctx = context(cls, prediction, position=actual)
        evaluation = decide(cls, ctx)
        evidence = collect_evidence(
            cls(), ctx,
            cls.spec.parameter_schema.validate(
                {"model_id": "sell_model", "model_version": "1.0.0"}
            ),
            {}, evaluation,
        )
        body = evidence.to_dict()["evidence"]
        assert evidence.action == "SELL" and evidence.reason_code == "MODEL_EXIT_PREFERRED"
        assert body["probabilities"] == {
            "risk_exit": 0.15, "exit_favorable": 0.55, "hold_favorable": 0.30
        }
        assert body["position"]["entry_price"] == "99.5"
        assert body["position_features"] == {"position_unrealized_return": 0.005}


def test_a_model_signal_without_its_prediction_still_reports_the_recorded_probabilities():
    from test_model_strategies import BUY_CLASSES, context, decide, served  # noqa: PLC0415

    cls = BUY_CLASSES[0]
    prediction = served(cls)
    evaluation = decide(cls, context(cls, prediction))
    bare = context(cls, None, inputs=False)  # the prediction object is gone from the context
    evidence = collect_evidence(
        cls(), bare,
        cls.spec.parameter_schema.validate({"model_id": "buy_model", "model_version": "1.0.0"}),
        {"eligibility": "WATCHING_BUY"}, evaluation,
    )
    body = evidence.evidence
    assert body["probabilities"]["favorable"] == 0.7  # from the decision audit
    assert "features" not in body and "features_unavailable" in body  # never invented


def test_michelle_buy_strategies_describe_their_own_facts_not_a_shared_template():
    from test_michelle_strategies import (  # noqa: PLC0415
        FiveMinuteOrbMichelleBuy,
        context,
        daily_history,
        orb_bars,
        vwap_cycle,
        watching,
    )

    from mike_trade.strategies.buy.vwap_bounce_break_michelle import VwapBounceBreakMichelleBuy

    orb = FiveMinuteOrbMichelleBuy()
    orb_ctx = context(orb_bars(), daily=daily_history())
    orb_eval = orb.evaluate(orb_ctx, {}, watching())
    assert orb_eval.is_signal
    orb_ev = collect_evidence(orb, orb_ctx, {}, watching(), orb_eval)
    assert orb_ev.reason_code == "ORB_BREAKOUT"
    assert D(orb_ev.evidence["trigger"]["orb_high"]) == D("100.60")
    assert D(orb_ev.evidence["trigger"]["reference_price"]) == D("100.65")
    assert D(orb_ev.evidence["opening_range"]["high"]) == D("100.60")
    assert "daily_wilder_atr14" in orb_ev.evidence["risk_gates"]

    vwap = VwapBounceBreakMichelleBuy()
    bars = vwap_cycle(intervening_red=True)
    vwap_ctx = context(bars)
    vwap_eval = vwap.evaluate(vwap_ctx, {}, watching())
    assert vwap_eval.is_signal
    vwap_ev = collect_evidence(vwap, vwap_ctx, {}, watching(), vwap_eval)
    assert vwap_ev.reason_code == "VWAP_RECOVERY_SECOND_GREEN"
    assert vwap_ev.evidence["cycle"]["bottom_pattern"] == "hammer"
    assert D(vwap_ev.evidence["trigger"]["distance_above_vwap"]) <= D("0.50")

    # completely different evidence schemas, same envelope
    assert set(orb_ev.evidence) != set(vwap_ev.evidence)
    keys = {"schema_version", "strategy_id", "strategy_version", "action", "symbol",
            "timestamp", "signal_id", "reason_code", "reason", "evidence"}
    assert set(orb_ev.to_dict()) == set(vwap_ev.to_dict()) == keys
    # a model strategy has yet another shape
    assert "probabilities" not in orb_ev.evidence and "opening_range" not in vwap_ev.evidence


def test_michelle_protective_sells_are_documented_but_marked_never_reviewable():
    from mike_trade.review.stage import protective_exit
    from mike_trade.strategies.sell.five_minute_orb_michelle import FiveMinuteOrbMichelleSell
    from mike_trade.strategies.sell.vwap_bounce_break_michelle import VwapBounceBreakMichelleSell

    for cls in (FiveMinuteOrbMichelleSell, VwapBounceBreakMichelleSell):
        assert supports_evidence(cls) and protective_exit(cls)
    from mike_trade.strategies.buy.five_minute_orb_michelle import FiveMinuteOrbMichelleBuy
    from mike_trade.strategies.model_probability import ModelProbabilityBuy

    assert not protective_exit(FiveMinuteOrbMichelleBuy) and not protective_exit(
        ModelProbabilityBuy
    )


def test_older_strategies_do_not_have_to_implement_the_interface():
    from mike_trade.strategies.buy.vwap_bounce import VwapBounceBuy

    assert not supports_evidence(VwapBounceBuy)
    with pytest.raises(UnsupportedEvidenceError) as caught:
        require_evidence_support(VwapBounceBuy, where="AAA BUY")
    assert "vwap_bounce" in str(caught.value) and "AAA BUY" in str(caught.value)


def test_evidence_documents_are_validated_and_json_safe():
    base = dict(
        strategy_id="s", strategy_version="1.0.0", action="BUY", symbol="AAA", timestamp=AS_OF,
        reason_code="X", reason="because",
    )
    evidence = DecisionEvidence(**base, evidence={"d": D("1.50"), "t": AS_OF, "n": [1, 2.5, None]})
    assert evidence.evidence["d"] == "1.50" and evidence.evidence["t"].endswith("Z")
    round_trip = DecisionEvidence.from_dict(json.loads(json.dumps(evidence.to_dict())))
    assert round_trip.evidence == evidence.evidence
    for bad in ({"x": float("nan")}, {"x": object()}, {1: "a"}):
        with pytest.raises(EvidenceError):
            DecisionEvidence(**base, evidence=bad)
    with pytest.raises(EvidenceError):
        DecisionEvidence(**{**base, "action": "HOLD"}, evidence={})
    assert to_json_value((D("1"), (2, 3))) == ["1", [2, 3]]


def test_collected_evidence_must_describe_the_evaluated_signal():
    from review_support import evidence_strategy

    from mike_trade.domain.strategy_types import StrategyRole
    from mike_trade.review.evidence import make_evidence

    cls = evidence_strategy("ev_buy_a", StrategyRole.BUY)

    class Wrong(cls):  # type: ignore[valid-type, misc]
        def build_evidence(self, context, params, state, evaluation):  # noqa: ANN001, ANN201
            other = StrategyEvaluation.for_signal(
                cls.spec, "AAA", evaluation.bar_time + timedelta(minutes=1),
                reference_price=D("1"),
            )
            return make_evidence(cls.spec, other, context, reason_code="X", reason="y", evidence={})

    ctx = simple_context()
    evaluation = StrategyEvaluation.for_signal(
        cls.spec, "AAA", ctx.candles[CandleTimeframe.M1][-1].start, reference_price=D("100")
    )
    with pytest.raises(EvidenceError, match="not the evaluated signal"):
        collect_evidence(Wrong(), ctx, {}, {"count": 1}, evaluation)


# --------------------------------------------------------------------------- strict responses


def test_only_the_structured_decision_shape_is_accepted():
    ok = parse_review_payload(
        {"decision": "APPROVE", "reason": " solid ", "observations": ["a"]},
        reviewer_version="v1",
    )
    assert ok.decision.value == "APPROVE" and ok.reason == "solid"
    assert ok.to_dict() == {
        "decision": "APPROVE", "reason": "solid", "observations": ["a"], "reviewer_version": "v1",
    }
    malformed = [
        "APPROVE", ["APPROVE"], {"decision": "approve", "reason": "r", "observations": []},
        {"decision": "MAYBE", "reason": "r", "observations": []},
        {"decision": "I would approve this trade", "reason": "r", "observations": []},
        {"decision": "APPROVE", "reason": "", "observations": []},
        {"decision": "APPROVE", "reason": "r"},
        {"decision": "APPROVE", "reason": "r", "observations": "x"},
        {"decision": "APPROVE", "reason": "r", "observations": [1]},
        {"decision": "APPROVE", "reason": "r", "observations": [], "extra": 1},
    ]
    for payload in malformed:
        with pytest.raises(MalformedReviewResponse):
            parse_review_payload(payload, reviewer_version="v1")


# --------------------------------------------------------------- OpenRouter adapter (mocked)


def openrouter(handler, *, environ=None, **kwargs):
    return OpenRouterReviewer(
        environ={OPENROUTER_API_KEY_ENV: "sk-test-not-real"} if environ is None else environ,
        transport=httpx.MockTransport(handler), **kwargs,
    )


def completion(content, *, model="served/model-x", finish="stop", usage=None, extra=None):
    body = {
        "id": "gen-123", "model": model, "provider": "SomeHost",
        "choices": [{"finish_reason": finish, "message": {"role": "assistant", "content": content}}],
        "usage": usage if usage is not None else {
            "prompt_tokens": 900, "completion_tokens": 80, "total_tokens": 980, "cost": 0.00123,
        },
    }
    body.update(extra or {})
    return httpx.Response(200, json=body)


GOOD = json.dumps({"decision": "REJECT", "reason": "extended", "observations": ["x", "y"]})


def test_openrouter_defaults_to_the_verified_jev_model_and_the_existing_api_key_variable():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return completion(GOOD)

    reviewer = openrouter(handler)
    assert DEFAULT_JEV_MODEL == "typesafe/jev-router"
    assert OPENROUTER_API_KEY_ENV == "OPENROUTER_API_KEY"
    response = asyncio.run(reviewer.review({"proposal": {"action": "BUY"}}))
    assert seen["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-test-not-real"
    body = seen["body"]
    assert body["model"] == "typesafe/jev-router" and body["stream"] is False
    assert body["usage"] == {"include": True}
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["provider"] == {"require_parameters": True}
    assert json.loads(body["messages"][1]["content"]) == {"proposal": {"action": "BUY"}}
    assert response.decision.value == "REJECT" and response.observations == ("x", "y")
    assert "typesafe/jev-router" in response.reviewer_version
    assert "served/model-x" in response.reviewer_version  # the routed model is recorded
    usage = response.usage
    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (900, 80, 980)
    assert usage.cost == D("0.00123") and usage.served_model == "served/model-x"
    assert usage.generation_id == "gen-123"


def test_the_model_is_configuration_and_part_of_the_reviewer_identity():
    a = openrouter(lambda r: completion(GOOD), model="anthropic/claude-sonnet-4.5")
    b = openrouter(lambda r: completion(GOOD))
    assert a.identity.model == "anthropic/claude-sonnet-4.5"
    assert a.identity.key != b.identity.key
    assert a.identity.provider == "openrouter"
    assert openrouter(lambda r: completion(GOOD), temperature=0.7).identity.key != b.identity.key
    with pytest.raises(ConfigurationError):
        OpenRouterReviewer(model=" ", environ={})


def test_missing_credential_is_a_startup_error_and_never_logged():
    reviewer = openrouter(lambda r: completion(GOOD), environ={})
    with pytest.raises(ConfigurationError, match="OPENROUTER_API_KEY"):
        reviewer.check_ready()
    with pytest.raises(ReviewerUnavailable):
        asyncio.run(reviewer.review({}))


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(401, json={"error": {"message": "bad key"}}), ReviewerUnavailable),
        (httpx.Response(402, json={"error": {"message": "credits"}}), ReviewerUnavailable),
        (httpx.Response(429, json={"error": {"message": "slow down"}}), ReviewerUnavailable),
        (httpx.Response(503, text="down"), ReviewerUnavailable),
        (httpx.Response(200, json={"error": {"message": "upstream"}}), ReviewerUnavailable),
        (httpx.Response(200, text="not json"), MalformedReviewResponse),
        (completion("I think you should buy it"), MalformedReviewResponse),
        (completion('{"decision":"APPROVE"}'), MalformedReviewResponse),
        (completion("```json\n" + GOOD + "\n```"), MalformedReviewResponse),
        (completion(GOOD, finish="length"), MalformedReviewResponse),
        (completion(""), MalformedReviewResponse),
        (httpx.Response(200, json={"choices": []}), MalformedReviewResponse),
    ],
)
def test_transport_and_malformed_failures_are_typed_errors(response, expected):
    reviewer = openrouter(lambda r: response)
    with pytest.raises(expected):
        asyncio.run(reviewer.review({}))


def test_the_credential_never_appears_in_the_request_body_or_the_record():
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content.decode())
        return completion(GOOD)

    reviewer = openrouter(handler)
    stage = DecisionReviewStage(
        parse_decision_review({"enabled": True, "mode": "enforce", "source": "live"}),
        mode="BACKTEST", reviewer=reviewer, cache_store=MemoryReviewStore(),
    )
    ctx = simple_context()
    proposal = TradeProposal(
        mode="BACKTEST", action="BUY", symbol="AAA", as_of=ctx.as_of,
        selected_strategy_id="ev_buy_a", selected_signal_id="s1",
        contributors=(contributor(),),
    )
    outcome = asyncio.run(stage.review(proposal, ctx))
    assert outcome.record.usage["cost"] == "0.00123"
    assert all("sk-test-not-real" not in body for body in bodies)
    assert "sk-test-not-real" not in json.dumps(outcome.record.to_dict())


# -------------------------------------------------------------------------- configuration


def test_review_is_off_by_default_and_requires_explicit_research_inputs():
    assert not parse_decision_review(None).active
    assert not parse_decision_review({}).active
    assert not parse_decision_review({"enabled": True, "mode": "disabled"}).active
    config = parse_decision_review(
        {"enabled": True, "reviewer": "jev", "review_buy": True, "review_sell": True,
         "mode": "enforce", "timeout_seconds": 10, "on_error": "bypass", "record_reviews": True}
    )
    assert config.active and config.enforcing and config.source is None
    with pytest.raises(ConfigurationError, match="explicitly"):
        parse_decision_review({"enabled": True, "mode": "enforce"}, research=True)
    assert parse_decision_review(
        {"enabled": True, "mode": "advisory", "source": "live"}, research=True
    ).active
    with pytest.raises(ConfigurationError, match="replay_from"):
        parse_decision_review({"enabled": True, "source": "replay"}, research=True)
    # the serialized form (manifests, fingerprints) round-trips and contains no floats
    again = parse_decision_review(config.to_dict())
    assert again == config
    assert not any(isinstance(v, float) for v in config.to_dict().values())


@pytest.mark.parametrize(
    "bad",
    [
        {"enabled": True, "mode": "maybe"},
        {"enabled": True, "on_error": "retry"},
        {"enabled": "yes"},
        {"enabled": True, "timeout_seconds": 0},
        {"enabled": True, "unknown_key": 1},
        {"enabled": True, "api_key": "x"},
        {"enabled": True, "strategies": "ev_buy_a"},
        {"enabled": True, "source": "magic"},
        {"enabled": True, "cache": {"bogus": 1}},
    ],
)
def test_invalid_review_settings_are_rejected(bad):
    with pytest.raises(ConfigurationError):
        parse_decision_review(bad)


def test_provider_catalog_resolves_reviewers_and_defaults_to_the_verified_jev_model(tmp_path):
    config = parse_decision_review({"enabled": True})
    assert resolve_reviewer_entry(config, {})["model"] == "typesafe/jev-router"
    assert resolve_reviewer_entry(config, {})["provider"] == "openrouter"
    catalog = tmp_path / "providers.yaml"
    catalog.write_text(
        "schema_version: 1\nreviewers:\n  jev:\n    provider: openrouter\n"
        "    model: some/other-model\n    max_concurrent_requests: 4\n"
        "    requests_per_minute: 30\n",
        encoding="utf-8",
    )
    entries = load_reviewer_entries(catalog)
    assert resolve_reviewer_entry(config, entries)["model"] == "some/other-model"
    override = parse_decision_review({"enabled": True, "model": "vendor/model-y"})
    assert resolve_reviewer_entry(override, entries)["model"] == "vendor/model-y"
    with pytest.raises(ConfigurationError, match="not defined"):
        resolve_reviewer_entry(parse_decision_review({"enabled": True, "reviewer": "nope"}), {})
    for bad in (
        {"jev": {"provider": "directjev", "model": "m"}},
        {"jev": {"provider": "openrouter"}},
        {"jev": {"provider": "openrouter", "model": "m", "api_key": "sk-123"}},
        {"jev": {"provider": "openrouter", "model": "m", "api_key_env": "sk-123"}},
    ):
        with pytest.raises(ConfigurationError):
            validate_reviewer_entries(bad)


def test_stage_factory_needs_the_openrouter_key_only_for_live_inference(tmp_path):
    live = parse_decision_review({"enabled": True, "mode": "enforce", "source": "live"})
    with pytest.raises(ConfigurationError, match="OPENROUTER_API_KEY"):
        build_stage(live, mode="PAPER", environ={}, entries={})
    stage = build_stage(live, mode="PAPER", environ={OPENROUTER_API_KEY_ENV: "k"}, entries={})
    assert stage is not None and stage.reviewer.identity.model == "typesafe/jev-router"
    replay = parse_decision_review(
        {"enabled": True, "mode": "enforce", "source": "replay", "replay_from": [str(tmp_path)]}
    )
    assert build_stage(replay, mode="BACKTEST", environ={}, entries={}).reviewer is None


def test_example_catalog_and_config_files_declare_the_optional_review_sections():
    import yaml

    from mike_trade.controls.config_commands import load_provider_catalog

    catalog = load_provider_catalog("config/providers.example.yaml")
    assert catalog["reviewers"]["jev"]["provider"] == "openrouter"
    assert catalog["reviewers"]["jev"]["model"] == "typesafe/jev-router"
    assert catalog["reviewers"]["jev"]["api_key_env"] == "OPENROUTER_API_KEY"
    document = yaml.safe_load(open("config/mike_trade.example.yaml"))
    assert document["decision_review"]["enabled"] is False  # never on by default


def test_operator_config_accepts_decision_review_and_keeps_existing_fingerprints(tmp_path):
    import copy

    from mike_trade.controls.config_commands import (
        compute_config_fingerprint,
        load_effective_config,
    )

    base = open("config/mike_trade.example.yaml").read()
    plain = tmp_path / "plain.yaml"
    plain.write_text(base)
    settings = load_effective_config(str(plain), environ={})
    assert settings["decision_review"]["enabled"] is False
    off_fingerprint = compute_config_fingerprint(settings)
    assert "decision_review" not in __import__(
        "mike_trade.controls.config_commands", fromlist=["x"]
    ).trade_relevant_config(settings)
    on = tmp_path / "on.yaml"
    on.write_text(base.replace("decision_review:\n  enabled: false",
                               "decision_review:\n  enabled: true"))
    reviewed = load_effective_config(str(on), environ={})
    assert reviewed["decision_review"]["enabled"] is True
    assert compute_config_fingerprint(reviewed) != off_fingerprint
    assert compute_config_fingerprint(copy.deepcopy(settings)) == off_fingerprint
    bad = tmp_path / "bad.yaml"
    bad.write_text(base.replace("decision_review:\n  enabled: false",
                                "decision_review:\n  enabled: true")
                   .replace("on_error: bypass", "on_error: retry"))
    from mike_trade.controls.config_commands import ConfigValidationError

    with pytest.raises(ConfigValidationError):
        load_effective_config(str(bad), environ={})


# ---------------------------------------------------------------------------------- the stage


def buy_proposal(*, state=None, signal="s1", as_of=None):
    ctx = simple_context()
    return ctx, TradeProposal(
        mode="BACKTEST", action="BUY", symbol="AAA", as_of=as_of or ctx.as_of,
        selected_strategy_id="ev_buy_a", selected_signal_id=signal,
        contributors=(contributor(signal),), state=state,
    )


def stage_for(reviewer, **overrides):
    config = parse_decision_review(
        {"enabled": True, "mode": "enforce", "source": "live", **overrides}
    )
    return DecisionReviewStage(
        config, mode="BACKTEST", reviewer=reviewer, cache_store=MemoryReviewStore()
    )


def test_identical_concurrent_proposals_share_one_paid_call():
    reviewer = FakeReviewer("APPROVE", delay=0.05)
    stage = stage_for(reviewer)
    ctx, proposal = buy_proposal()

    async def go():
        return await asyncio.gather(*(stage.review(proposal, ctx) for _ in range(5)))

    outcomes = asyncio.run(go())
    assert len(reviewer.calls) == 1
    assert all(o.allow for o in outcomes)
    assert sorted(r.source for r in stage.records).count("live_inference") == 1
    assert stage.summary()["model_calls"] == 1 and stage.summary()["served_from_cache"] == 4
    asyncio.run(stage.review(proposal, ctx))
    assert len(reviewer.calls) == 1  # and later repeats come from the cache


def test_different_symbols_review_concurrently_within_the_provider_limit():
    reviewer = FakeReviewer("APPROVE", delay=0.05)
    config = parse_decision_review({"enabled": True, "mode": "enforce", "source": "live"})
    stage = DecisionReviewStage(
        config, mode="BACKTEST", reviewer=reviewer, cache_store=MemoryReviewStore(),
        max_concurrent=3,
    )

    async def go():
        jobs = []
        for index in range(9):
            ctx, proposal = buy_proposal(signal=f"s{index}")
            jobs.append(stage.review(proposal, simple_context(as_of=ctx.as_of)))
        return await asyncio.gather(*jobs)

    asyncio.run(go())
    assert reviewer.max_in_flight == 3  # concurrent, but bounded by the configured limit


def test_a_slow_review_does_not_block_unrelated_work():
    reviewer = FakeReviewer("APPROVE", delay=0.3)
    stage = stage_for(reviewer, timeout_seconds=5)
    ctx, proposal = buy_proposal()

    async def go():
        slow = asyncio.create_task(stage.review(proposal, ctx))
        started = asyncio.get_running_loop().time()
        await asyncio.sleep(0.01)  # unrelated work proceeds while the review is pending
        elapsed = asyncio.get_running_loop().time() - started
        await slow
        return elapsed

    assert asyncio.run(go()) < 0.2


def test_a_delayed_approval_cannot_authorize_a_changed_position_or_stopped_out_state():
    reviewer = FakeReviewer("APPROVE")
    stage = stage_for(reviewer)
    ctx = simple_context()
    flat = StateToken({"flat": True, "entry_pending": False}, AS_OF)
    proposal = TradeProposal(
        mode="BACKTEST", action="BUY", symbol="AAA", as_of=ctx.as_of,
        selected_strategy_id="ev_buy_a", selected_signal_id="s9",
        contributors=(contributor("s9"),), state=flat,
    )
    outcome = asyncio.run(stage.review(proposal, ctx))
    assert outcome.allow
    same = stage.authorize(outcome, proposal, flat, ctx.as_of)
    assert same.allow
    changed = stage.authorize(
        outcome, proposal, StateToken({"flat": False, "entry_pending": True}, AS_OF), ctx.as_of
    )
    assert not changed.allow and changed.effect == "STALE_BLOCKED"
    assert set(changed.stale_fields) == {"flat", "entry_pending"}
    # an expired proposal (newer bar exists and it is old) is refused too
    late = stage.authorize(
        outcome, proposal, StateToken(dict(flat.facts), AS_OF + timedelta(minutes=1)),
        ctx.as_of + timedelta(seconds=120),
    )
    assert not late.allow and late.stale_fields == ("market_moved_on",)
    # ...but a newer bar alone, inside the validity window, does not invalidate it
    fresh = stage.authorize(
        outcome, proposal, StateToken(dict(flat.facts), AS_OF + timedelta(minutes=1)),
        ctx.as_of + timedelta(seconds=5),
    )
    assert fresh.allow
    assert any(r.effect == "STALE_BLOCKED" and r.source == "disposition" for r in stage.records)


def test_a_stale_state_never_turns_an_advisory_review_into_a_block():
    stage = stage_for(FakeReviewer("APPROVE"), mode="advisory")
    ctx, proposal = buy_proposal(state=StateToken({"flat": True}, AS_OF))
    outcome = asyncio.run(stage.review(proposal, ctx))
    result = stage.authorize(outcome, proposal, StateToken({"flat": False}, AS_OF), ctx.as_of)
    assert result.allow


def test_advisory_reviews_in_live_modes_run_in_the_background():
    reviewer = FakeReviewer("REJECT", delay=0.2)
    stage = stage_for(reviewer, mode="advisory")
    ctx, proposal = buy_proposal()

    async def go():
        task = stage.review_nonblocking(proposal, ctx)
        immediately = list(stage.records)
        await stage.drain()
        return immediately, task.result()

    immediately, outcome = asyncio.run(go())
    assert immediately == [] and outcome.effect == "ADVISORY_REJECT_NOT_ENFORCED"
    assert outcome.allow  # advisory never blocks


def test_recording_can_be_turned_off_but_the_run_ledger_stays_available(tmp_path):
    path = tmp_path / "paper.reviews.jsonl"
    config = parse_decision_review(
        {"enabled": True, "mode": "enforce", "source": "live", "record_reviews": False}
    )
    stage = DecisionReviewStage(
        config, mode="PAPER", reviewer=FakeReviewer(), record_store=JsonlReviewStore(path),
        cache_store=MemoryReviewStore(),
    )
    ctx, proposal = buy_proposal()
    asyncio.run(stage.review(proposal, ctx))
    assert not path.exists() and len(stage.records) == 1
    recording = DecisionReviewStage(
        parse_decision_review({"enabled": True, "mode": "enforce", "source": "live"}),
        mode="PAPER", reviewer=FakeReviewer(), record_store=JsonlReviewStore(path),
        cache_store=MemoryReviewStore(),
    )
    asyncio.run(recording.review(proposal, ctx))
    stored = json.loads(path.read_text().splitlines()[0])
    assert stored["mode"] == "PAPER" and stored["decision"] == "APPROVE"
    assert stored["request"]["proposal"]["symbol"] == "AAA"
    assert stored["latency_ms"] is not None and stored["usage"]["cost"] == "0.0002"
    assert stored["reviewer"]["provider"] == "fake" and stored["input_hash"]


def test_recorded_reviews_with_different_reviewer_identities_must_be_disambiguated(tmp_path):
    ctx, proposal = buy_proposal()
    store = MemoryReviewStore()
    for model in ("model-a", "model-b"):
        stage = DecisionReviewStage(
            parse_decision_review({"enabled": True, "mode": "enforce", "source": "live"}),
            mode="BACKTEST", reviewer=FakeReviewer(model=model), record_store=store,
            cache_store=MemoryReviewStore(),
        )
        asyncio.run(stage.review(proposal, ctx))
    replay = DecisionReviewStage(
        parse_decision_review(
            {"enabled": True, "mode": "enforce", "source": "replay",
             "replay_from": [str(tmp_path)]}
        ),
        mode="BACKTEST", replay_store=store,
    )
    with pytest.raises(ReviewStoreError, match="different"):
        asyncio.run(replay.review(proposal, ctx))
    pinned = DecisionReviewStage(
        parse_decision_review(
            {"enabled": True, "mode": "enforce", "source": "replay",
             "replay_from": [str(tmp_path)], "expect_reviewer": {"model": "model-b"}}
        ),
        mode="BACKTEST", replay_store=store,
    )
    outcome = asyncio.run(pinned.review(proposal, ctx))
    assert outcome.record.reviewer["model"] == "model-b" and outcome.record.source == "replay"


def test_position_details_pnl_and_stop_travel_with_a_sell_request():
    from mike_trade.review.proposal import StopView, build_review_request

    ctx = simple_context()
    proposal = TradeProposal(
        mode="PAPER", action="SELL", symbol="AAA", as_of=ctx.as_of,
        selected_strategy_id="ev_sell_a", selected_signal_id="s1",
        contributors=(Contributor("ev_sell_a", "1.0.0", "s1", True),),
        position=position_view("99.5", "10"),
        stop=StopView("FIXED_THEN_TRAILING_STOP", D("99.25"), D("100.19"), D("0.2"), True),
        exit_source="STRATEGY_SELL", reference_price=D("100.31"),
    )
    request = build_review_request(proposal, ctx, candle_count=5)
    assert request["proposal"]["action"] == "SELL" and request["proposal"]["execution_mode"] == "PAPER"
    position = request["position"]
    assert D(position["unrealized_pnl"]) == (D("100.31") - D("99.5")) * 10  # last trade mark
    assert position["mark_source"] == "last_trade" and position["holding_minutes"] == 20.0
    assert request["protective_stop"]["current_stop"] == "100.19"
    assert request["market"]["time_of_day_et"] == "10:30:00"
    assert len(request["market"]["candles"]["1m"]) == 5
    assert request["market"]["indicators"] == {"vwap": "100.1"}  # None-valued ones dropped
    assert request["provenance"]["feed"] == "iex" and request["provenance"]["stale"] is False
    flattened = json.dumps(request).lower()
    for forbidden in ("account", "credential", "secret", "api_key", "balance", "buying_power"):
        assert forbidden not in flattened


def test_a_reviewed_signal_without_evidence_follows_the_error_policy_never_a_blind_review():
    reviewer = FakeReviewer("APPROVE")
    for policy, allowed in (("bypass", True), ("reject", False)):
        stage = stage_for(reviewer, on_error=policy)
        ctx = simple_context()
        proposal = TradeProposal(
            mode="BACKTEST", action="BUY", symbol="AAA", as_of=ctx.as_of,
            selected_strategy_id="ev_buy_a", selected_signal_id="s1",
            contributors=(Contributor("ev_buy_a", "1.0.0", "s1", True, None),),
        )
        outcome = asyncio.run(stage.review(proposal, ctx))
        assert outcome.allow is allowed and outcome.record.error["type"] == "EvidenceError"
    assert reviewer.calls == []  # nothing was sent without evidence
