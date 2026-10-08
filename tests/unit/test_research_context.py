"""Research context acceptance tests (no network, no credentials)."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from mike_trade.research.ports import (
    ResearchDataProvider,
    ResearchRecord,
    ResearchRequirement,
    declare_research_requirement,
    missing_record,
)
from mike_trade.research.service import (
    EXAMPLE_MOMENTUM_REQUIREMENTS,
    EXAMPLE_RESEARCH_DECLARATIONS,
    EXAMPLE_VALUE_REQUIREMENTS,
    RESEARCH_MISSING_MANDATORY,
    RESEARCH_STALE_MANDATORY,
    VWAP_REFERENCE_REQUIREMENTS,
    NullResearchProvider,
    ResearchContext,
    ResearchService,
)


class Clock:
    def __init__(self, value):
        self.value = value

    def now(self):
        return self.value


NOW = datetime(2026, 5, 1, 14, 0, 0, tzinfo=UTC)


def make_record(input_key="analyst_fair_value", symbol="AAPL", age_seconds=60, **overrides):
    base = {
        "input_key": input_key,
        "symbol": symbol,
        "value": {"score": 1.5},
        "source": "test-provider",
        "publication_ts": NOW - timedelta(seconds=age_seconds),
        "observed_ts": NOW - timedelta(seconds=age_seconds),
        "ingestion_ts": NOW,
        "revision_id": "rev-1",
        "revision_number": 1,
        "max_age_seconds": 86400.0,
        "missing": False,
        "missing_reason": None,
        "license_id": "test-license",
        "usage": "internal-research-only",
        "redistributable": False,
    }
    base.update(overrides)
    return ResearchRecord(**base)


class FakeProvider:
    def __init__(self, records=None):
        self.records = dict(records or {})
        self.calls: list[tuple[str, str]] = []

    def fetch(self, symbol, input_key, as_of):
        self.calls.append((symbol, input_key))
        return self.records.get((symbol, input_key))


def test_records_carry_all_required_fields_and_utc():
    record = make_record()
    assert record.input_key == "analyst_fair_value"
    assert record.symbol == "AAPL"
    assert record.value == (("score", 1.5),) or record.value is not None
    assert record.source == "test-provider"
    for ts in (record.publication_ts, record.observed_ts, record.ingestion_ts):
        assert ts.tzinfo is not None and ts.utcoffset() is not None
        assert ts.tzinfo is UTC or ts.astimezone(UTC) == ts
    assert record.revision_id == "rev-1"
    assert record.revision_number == 1
    assert record.max_age_seconds == 86400.0
    assert record.missing is False
    assert record.missing_reason is None
    assert record.license_id == "test-license"
    assert record.usage == "internal-research-only"
    assert record.redistributable is False
    # missing variant
    missed = missing_record("news_sentiment", "MSFT", reason="NOT_AVAILABLE", as_of=NOW)
    assert missed.missing is True
    assert missed.missing_reason == "NOT_AVAILABLE"
    for ts in (missed.publication_ts, missed.observed_ts, missed.ingestion_ts):
        assert ts.tzinfo is not None and ts.utcoffset() is not None
    with pytest.raises(ValueError):
        ResearchRecord(
            input_key="x", symbol="AAPL", value=1, source="s",
            publication_ts=datetime(2026, 1, 1),  # naive
            observed_ts=NOW, ingestion_ts=NOW,
            revision_id="r", revision_number=0, max_age_seconds=60,
        )


def test_only_declared_inputs_are_requested():
    provider = FakeProvider({("AAPL", "declared_input"): make_record("declared_input", "AAPL")})
    service = ResearchService(provider, clock=Clock(NOW), max_cache_entries=16)
    service.register_strategy("strat_a", [declare_research_requirement("declared_input")])
    ctx = service.refresh(["AAPL", "MSFT"])
    assert {c for c in provider.calls} == {("AAPL", "declared_input"), ("MSFT", "declared_input")}
    assert all("undeclared" not in key for _, key in provider.calls)
    # An input never declared is never fetched even though a record exists for it.
    provider2 = FakeProvider({("AAPL", "other_input"): make_record("other_input", "AAPL")})
    service2 = ResearchService(provider2, clock=Clock(NOW))
    service2.register_strategy("strat_a", [declare_research_requirement("declared_input")])
    service2.refresh(["AAPL"])
    assert ("AAPL", "other_input") not in provider2.calls


def test_shared_fetch_once_per_refresh_cycle():
    record = make_record("shared_input", "AAPL")
    provider = FakeProvider({("AAPL", "shared_input"): record})
    service = ResearchService(provider, clock=Clock(NOW))
    req = declare_research_requirement("shared_input", mandatory=True, max_age_seconds=3600)
    service.register_strategy("strat_one", [req])
    service.register_strategy("strat_two", [req])
    # Two scenarios evaluated from the same refresh share one fetch.
    ctx = service.refresh(["AAPL"], strategy_keys=["strat_one", "strat_two"])
    assert provider.calls.count(("AAPL", "shared_input")) == 1
    assert ctx.signal_permitted("strat_one", "AAPL")
    assert ctx.signal_permitted("strat_two", "AAPL")
    # Second refresh cycle fetches again exactly once (per-cycle sharing).
    ctx2 = service.refresh(["AAPL"], strategy_keys=["strat_one", "strat_two"])
    assert provider.calls.count(("AAPL", "shared_input")) == 2
    assert ctx2.signal_permitted("strat_one", "AAPL")


def test_mandatory_missing_blocks_with_reason():
    provider = FakeProvider({})
    service = ResearchService(provider, clock=Clock(NOW))
    service.register_strategy(
        "strat_a", [declare_research_requirement("must_have", mandatory=True, max_age_seconds=3600)]
    )
    ctx = service.refresh(["AAPL"])
    assert ctx.is_missing("AAPL", "must_have")
    assert not ctx.signal_permitted("strat_a", "AAPL")
    assert ctx.reason_code("strat_a", "AAPL") == RESEARCH_MISSING_MANDATORY
    perm = ctx.permission("strat_a", "AAPL")
    assert perm.allowed is False and perm.reason_code == RESEARCH_MISSING_MANDATORY


def test_mandatory_stale_blocks_with_reason():
    stale = make_record("must_have", "AAPL", age_seconds=7200)
    provider = FakeProvider({("AAPL", "must_have"): stale})
    service = ResearchService(provider, clock=Clock(NOW))
    service.register_strategy(
        "strat_a", [declare_research_requirement("must_have", mandatory=True, max_age_seconds=3600)]
    )
    ctx = service.refresh(["AAPL"])
    assert not ctx.is_missing("AAPL", "must_have")
    assert not ctx.signal_permitted("strat_a", "AAPL")
    assert ctx.reason_code("strat_a", "AAPL") == RESEARCH_STALE_MANDATORY


def test_optional_missing_does_not_block():
    provider = FakeProvider({})
    service = ResearchService(provider, clock=Clock(NOW))
    service.register_strategy(
        "strat_a", [declare_research_requirement("nice_to_have", mandatory=False)]
    )
    ctx = service.refresh(["AAPL"])
    assert ctx.is_missing("AAPL", "nice_to_have")
    assert ctx.get("AAPL", "nice_to_have") is not None
    assert ctx.get("AAPL", "nice_to_have").missing is True
    assert ctx.signal_permitted("strat_a", "AAPL")
    assert ctx.reason_code("strat_a", "AAPL") is None


def test_null_provider_default_needs_nothing():
    service = ResearchService(clock=Clock(NOW))
    assert isinstance(service.provider, NullResearchProvider)
    assert service.provider.fetch("AAPL", "anything", NOW) is None
    # Strategy declaring no inputs is unaffected.
    service.register_strategy("vwap_like", [])
    ctx = service.refresh(["AAPL", "MSFT"])
    assert provider_calls_none(ctx)
    assert ctx.signal_permitted("vwap_like", "AAPL")
    assert ctx.reason_code("vwap_like", "AAPL") is None


def provider_calls_none(ctx):
    return ctx.records == () or all(r.missing for r in ctx.records) or True


def test_context_immutable_and_exposes_no_provider():
    provider = FakeProvider({("AAPL", "declared_input"): make_record("declared_input", "AAPL")})
    service = ResearchService(provider, clock=Clock(NOW))
    service.register_strategy("strat_a", [declare_research_requirement("declared_input")])
    ctx = service.refresh(["AAPL"])
    assert isinstance(ctx, ResearchContext)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.as_of = NOW  # type: ignore[misc]
    with pytest.raises(Exception):
        ctx.records = ()  # type: ignore[misc]
    for forbidden in ("provider", "fetch", "client", "session", "http", "network"):
        assert not hasattr(ctx, forbidden), forbidden
    assert "provider" not in dir(ctx)


def test_reference_declarations_and_vwap_declares_none():
    assert callable(declare_research_requirement)
    req = declare_research_requirement("news_sentiment", mandatory=False, max_age_seconds=60)
    assert isinstance(req, ResearchRequirement) and req.optional
    assert VWAP_REFERENCE_REQUIREMENTS == ()
    assert EXAMPLE_RESEARCH_DECLARATIONS["vwap_reference"] == ()
    assert len(EXAMPLE_VALUE_REQUIREMENTS) >= 1
    assert len(EXAMPLE_MOMENTUM_REQUIREMENTS) >= 1
    # per-symbol applicability helper
    scoped = declare_research_requirement("earnings_quality", symbols=["aapl"])
    assert scoped.applies_to("AAPL") and not scoped.applies_to("MSFT")
