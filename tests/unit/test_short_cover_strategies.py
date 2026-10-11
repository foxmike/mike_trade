"""SHORT/COVER roles: contracts, registration and the six initial strategies."""

from __future__ import annotations

import inspect
import json
import random
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from mike_trade import app
from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import Candle, CandleOrigin, CandleStatus, CandleTimeframe, PriceBasis
from mike_trade.market_data.context import ContextFreshness, MarketContext
from mike_trade.research.commands import _research_registry
from mike_trade.scheduling.calendar import SessionStatus
from mike_trade.strategies.base import (
    BuyStrategy, CoverStrategy, ParameterValidationError, ReasonCode, SellStrategy, ShortStrategy,
    make_signal_id,
)
from mike_trade.strategies.registry import StrategyRegistry, StrategyRegistryError

D = Decimal
OPEN = datetime(2026, 10, 8, 9, 30, tzinfo=ZoneInfo("America/New_York")).astimezone(UTC)
REGISTRY = _research_registry()
SHORTS = ("short_ema_bearish_crossover", "short_supertrend_bearish_flip", "short_donchian_breakdown")
COVERS = ("cover_ema_bullish_crossover", "cover_supertrend_bullish_flip", "cover_donchian_breakout")
ARMED = {"short": {"eligibility": "WATCHING_SHORT"}, "cover": {}}


def candle(i: int, o: object, h: object, l: object, c: object) -> Candle:
    start = OPEN + timedelta(minutes=i)
    return Candle("SPY", CandleTimeframe.M1, start, start + timedelta(minutes=1),
                  D(str(o)), D(str(h)), D(str(l)), D(str(c)), D(100), "sip", True,
                  CandleOrigin.REPLAY, PriceBasis.RAW, CandleStatus.CLOSED)


def series(closes: Sequence[object], half: str = "0.5") -> list[Candle]:
    return [candle(i, c, D(str(c)) + D(half), D(str(c)) - D(half), c) for i, c in enumerate(closes)]


def context(bars: list[Candle], *, stale: bool = False,
            session: SessionStatus = SessionStatus.REGULAR) -> MarketContext:
    as_of = bars[-1].end + timedelta(seconds=1)
    return MarketContext(
        symbol="SPY", as_of=as_of, version=len(bars), feed_id="sip", consolidated=True,
        price_basis=PriceBasis.RAW, session_status=session, session_id="2026-10-08",
        freshness=ContextFreshness(bars[-1].end, timedelta(seconds=1), timedelta(minutes=5),
                                   stale, "DATA_TOO_OLD" if stale else None),
        candles={CandleTimeframe.M1: tuple(bars)}, indicators={},
    )


def side(strategy_id: str) -> str:
    return "short" if strategy_id.startswith("short_") else "cover"


def run(strategy_id: str, bars: list[Candle], params: dict[str, object] | None = None,
        state: dict[str, object] | None = None, **ctx: object):
    return REGISTRY.get(strategy_id)().evaluate(
        context(bars, **ctx), params or {}, dict(ARMED[side(strategy_id)]) if state is None else state)


FALL = [20, 19, 18, 17, 16, 15, 14, 15, 17]  # Supertrend(3, 1) flips up on the last bar
RISE = [10, 11, 12, 13, 14, 15, 16, 15, 13]  # mirror: flips down on the last bar


def cases() -> dict[str, tuple[dict[str, object], list[Candle], list[Candle]]]:
    """strategy -> (params, bars whose last bar signals, same-length bars that do not)."""
    ema = {"fast_period": 2, "slow_period": 4}
    st = {"atr_period": 3, "multiplier": D(1)}
    dn = {"period": 3}
    flat = [10, 10, 10, 10]
    return {
        "short_ema_bearish_crossover": (ema, series(flat + [8]), series(flat + [10])),
        "cover_ema_bullish_crossover": (ema, series(flat + [12]), series(flat + [10])),
        "short_supertrend_bearish_flip": (st, series(RISE), series(RISE[:-1] + [15.5])),
        "cover_supertrend_bullish_flip": (st, series(FALL), series(FALL[:-1] + [14.5])),
        "short_donchian_breakdown": (dn, series(flat[:3] + [9]), series(flat[:3] + [9.6])),
        "cover_donchian_breakout": (dn, series(flat[:3] + [11]), series(flat[:3] + [10.4])),
    }


def test_roles_are_independent_and_registered() -> None:
    assert [r.value for r in StrategyRole] == ["BUY", "SELL", "SHORT", "COVER"]
    counts = {r: len(REGISTRY.list_strategies(r)) for r in StrategyRole}
    assert counts == {StrategyRole.BUY: 80, StrategyRole.SELL: 40,
                      StrategyRole.SHORT: 3, StrategyRole.COVER: 3}
    assert len(REGISTRY.list_strategies()) == 126
    source = inspect.getsource(app.build_runtime)
    for ids, role, base in ((SHORTS, StrategyRole.SHORT, ShortStrategy),
                            (COVERS, StrategyRole.COVER, CoverStrategy)):
        assert {c.spec.strategy_id for c in REGISTRY.list_strategies(role)} == set(ids)
        for strategy_id in ids:
            cls = REGISTRY.get(strategy_id)
            assert cls.spec.side is role and issubclass(cls, base)
            assert not issubclass(cls, (BuyStrategy, SellStrategy))
            assert cls.spec.category == "technical" and cls.spec.source
            assert cls.spec.trigger_timeframe is CandleTimeframe.M1
            assert cls.spec.warmup_bars == cls.required_bars({})
            assert source.count(f"{cls.__name__},") == 1
    for ids in (SHORTS, COVERS):
        assert not {c.spec.strategy_id for c in REGISTRY.list_strategies(StrategyRole.BUY)} & set(ids)


def test_role_base_classes_enforce_the_declared_side() -> None:
    cls = REGISTRY.get("short_donchian_breakdown")
    with pytest.raises(TypeError):
        type("Wrong", (BuyStrategy,), {"spec": cls.spec})
    registry = StrategyRegistry()
    registry.register(cls)
    with pytest.raises(StrategyRegistryError):
        registry.register(cls)  # duplicate id/version
    assert registry.list_strategies(StrategyRole.SHORT) == (cls,)
    assert registry.list_strategies(StrategyRole.COVER) == ()


def test_signal_ids_are_deterministic_and_role_specific() -> None:
    bar = OPEN
    ids = {role: make_signal_id("x_strategy", "1.0.0", "SPY", role, bar) for role in StrategyRole}
    assert len(set(ids.values())) == 4
    assert ids[StrategyRole.SHORT] == make_signal_id("x_strategy", "1.0.0", "SPY", StrategyRole.SHORT, bar)


@pytest.mark.parametrize("strategy_id", SHORTS + COVERS)
def test_trigger_non_trigger_warmup_guards_and_one_signal_per_bar(strategy_id: str) -> None:
    params, bars, quiet = cases()[strategy_id]
    cls = REGISTRY.get(strategy_id)
    need = cls.required_bars(params)
    assert need == len(bars) or strategy_id.endswith(("flip",))
    evaluation = run(strategy_id, bars, params)
    assert evaluation.is_signal and evaluation.side is cls.spec.side
    assert evaluation.bar_time == bars[-1].start and evaluation.reference_price == bars[-1].close
    assert evaluation.signal_id == make_signal_id(
        cls.spec.strategy_id, cls.spec.version, "SPY", cls.spec.side, bars[-1].start)
    assert not run(strategy_id, quiet, params).is_signal
    assert run(strategy_id, bars[-(need - 1):], params).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)
    replay = {**ARMED[side(strategy_id)], **evaluation.state_updates}
    assert not run(strategy_id, bars, params, replay).is_signal  # the same bar never fires twice
    assert run(strategy_id, bars, params, stale=True).reason_codes == (ReasonCode.STALE_DATA,)
    assert run(strategy_id, bars, params, session=SessionStatus.CLOSED).reason_codes == (
        ReasonCode.NOT_ELIGIBLE,)


@pytest.mark.parametrize("strategy_id", SHORTS)
def test_short_requires_the_engine_to_arm_it(strategy_id: str) -> None:
    params, bars, _ = cases()[strategy_id]
    for state in ({}, {"eligibility": "WATCHING_BUY"}, {"eligibility": "WATCHING"}):
        assert run(strategy_id, bars, params, state).reason_codes == (ReasonCode.NOT_ELIGIBLE,)


@pytest.mark.parametrize("strategy_id", COVERS)
def test_cover_carries_no_entry_eligibility_gate(strategy_id: str) -> None:
    params, bars, _ = cases()[strategy_id]
    assert run(strategy_id, bars, params, {}).is_signal
    assert run(strategy_id, bars, params, {"eligibility": "WATCHING_BUY"}).is_signal


@pytest.mark.parametrize("strategy_id", SHORTS + COVERS)
def test_missing_latest_minute_cannot_create_an_edge(strategy_id: str) -> None:
    bars = series([100 + (i % 3) for i in range(260)])
    bars[-1] = candle(261, 100, "100.5", "99.5", 100)  # minute 259 is absent
    assert run(strategy_id, bars).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


@pytest.mark.parametrize("strategy_id", ("short_donchian_breakdown", "cover_donchian_breakout"))
def test_donchian_reference_window_must_be_consecutive(strategy_id: str) -> None:
    params, bars, _ = cases()[strategy_id]
    # a hole between the first and second reference bar; the last two bars stay adjacent
    gapped = [bars[0]] + [candle(i + 1, b.open, b.high, b.low, b.close) for i, b in enumerate(bars[1:], 1)]
    assert run(strategy_id, gapped, params).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


def test_donchian_boundaries_and_signal_candle_excluded_from_reference() -> None:
    params = {"period": 3}
    base = [10, 10, 10]  # highs 10.5 / lows 9.5
    # equality with the reference extreme is not a break
    assert not run("short_donchian_breakdown", series(base + [9.5]), params).is_signal
    assert not run("cover_donchian_breakout", series(base + [10.5]), params).is_signal
    assert run("short_donchian_breakdown", series(base + ["9.49"]), params).is_signal
    assert run("cover_donchian_breakout", series(base + ["10.51"]), params).is_signal
    # a wide signal candle: its own extreme is excluded, so the close still breaks the older range
    wide = candle(3, 10, 10, 8, 9)
    assert run("short_donchian_breakdown", series(base) + [wide], params).is_signal
    # only the latest `period` bars before the signal count: an older, lower low is ignored
    older = [candle(0, 5, 6, 4, 5)] + series(base) + [candle(4, 9, 9.2, 8.8, 9)]
    older = [candle(i, b.open, b.high, b.low, b.close) for i, b in enumerate(older)]
    assert run("short_donchian_breakdown", older, params).is_signal


@pytest.mark.parametrize("strategy_id,invalid", [
    ("short_ema_bearish_crossover", {"fast_period": 30}), ("cover_ema_bullish_crossover", {"fast_period": 26}),
    ("short_ema_bearish_crossover", {"slow_period": 2}), ("cover_ema_bullish_crossover", {"fast_period": 1}),
    ("short_supertrend_bearish_flip", {"multiplier": D(0)}), ("cover_supertrend_bullish_flip", {"atr_period": 0}),
    ("short_donchian_breakdown", {"period": 1}), ("cover_donchian_breakout", {"period": 401}),
    ("short_donchian_breakdown", {"unknown": 1}), ("cover_donchian_breakout", {"period": 2.5}),
])
def test_invalid_parameters_are_rejected(strategy_id: str, invalid: dict[str, object]) -> None:
    with pytest.raises(ParameterValidationError):
        run(strategy_id, series([100 + (i % 3) for i in range(60)]), invalid)


def test_required_bars_follow_the_configured_parameters() -> None:
    assert REGISTRY.get("short_ema_bearish_crossover").required_bars({"slow_period": 50}) == 51
    assert REGISTRY.get("cover_supertrend_bullish_flip").required_bars({"atr_period": 5}) == 6
    assert REGISTRY.get("short_donchian_breakdown").required_bars({"period": 55}) == 56
    for strategy_id, params in (("short_ema_bearish_crossover", {"fast_period": 3, "slow_period": 7}),
                                ("cover_supertrend_bullish_flip", {"atr_period": 5}),
                                ("cover_donchian_breakout", {"period": 6})):
        cls = REGISTRY.get(strategy_id)
        bars = series([100 + (i % 3) for i in range(cls.required_bars(params) - 1)])
        assert run(strategy_id, bars, params).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


# ------------------------------------------------- math and recursive continuity


def random_bars(seed: int, count: int = 400) -> list[Candle]:
    rng, price, bars = random.Random(seed), 100.0, []
    for i in range(count):
        price *= 1 + rng.uniform(-0.012, 0.012)
        close = round(price, 3)
        bars.append(candle(i, close, close * 1.004, close * 0.996, close))
    return bars


def stream(strategy_id: str, bars: list[Candle], params: dict[str, object],
           window: int | None) -> tuple[list[int], dict[str, object]]:
    """Signal bar indexes when run bar by bar with persisted state over a (bounded) window."""
    cls = REGISTRY.get(strategy_id)
    state: dict[str, object] = dict(ARMED[side(strategy_id)])
    need, hits = cls.required_bars(params), []
    for n in range(need, len(bars) + 1):
        view = bars[:n] if window is None else bars[max(0, n - window):n]
        result = cls().evaluate(context(view), params, state)
        state.update(result.state_updates)
        if result.is_signal:
            hits.append(n - 1)
    return hits, state


def ema_values(values: list[Decimal], n: int) -> list[Decimal | None]:
    out: list[Decimal | None] = [None] * (n - 1) + [sum(values[:n]) / n]
    for value in values[n:]:
        out.append(out[-1] + D(2) / (n + 1) * (value - out[-1]))  # type: ignore[operator]
    return out


@pytest.mark.parametrize("strategy_id,down", [("short_ema_bearish_crossover", True),
                                             ("cover_ema_bullish_crossover", False)])
def test_ema_crossover_matches_independent_reference_and_ignores_window(strategy_id: str, down: bool) -> None:
    params = {"fast_period": 5, "slow_period": 13}
    bars = random_bars(11)
    closes = [b.close for b in bars]
    fast, slow = ema_values(closes, 5), ema_values(closes, 13)
    expected = [i for i in range(14, len(bars))
                if (fast[i - 1] >= slow[i - 1] and fast[i] < slow[i]) == down
                and (fast[i - 1] <= slow[i - 1] and fast[i] > slow[i]) == (not down)
                and fast[i - 1] is not None]
    assert expected  # the random series produces crossings
    unbounded, _ = stream(strategy_id, bars, params, None)
    assert unbounded == expected
    assert stream(strategy_id, bars, params, 16)[0] == expected
    assert stream(strategy_id, bars, params, 40)[0] == expected


def reference_supertrend(bars: list[Candle], period: int, mult: Decimal) -> list[bool | None]:
    """Independent TradingView ta.supertrend: returns is_uptrend per bar (None before the first ATR)."""
    trs: list[Decimal] = []
    atr: Decimal | None = None
    upper = lower = prev_close = None
    up = False
    out: list[bool | None] = []
    for bar in bars:
        tr = bar.high - bar.low if prev_close is None else max(
            bar.high - bar.low, abs(bar.high - prev_close), abs(bar.low - prev_close))
        if atr is None:
            trs.append(tr)
            atr = sum(trs) / period if len(trs) == period else None
        else:
            atr = (atr * (period - 1) + tr) / period
        if atr is None:
            out.append(None)
        else:
            mid = (bar.high + bar.low) / 2
            bu, bl = mid + mult * atr, mid - mult * atr
            if upper is None:
                upper, lower, up = bu, bl, False
            else:
                upper = bu if bu < upper or prev_close > upper else upper
                lower = bl if bl > lower or prev_close < lower else lower
                up = (bar.close >= lower) if up else (bar.close > upper)
            out.append(up)
        prev_close = bar.close
    return out


@pytest.mark.parametrize("strategy_id,to_up", [("short_supertrend_bearish_flip", False),
                                              ("cover_supertrend_bullish_flip", True)])
def test_supertrend_flips_match_independent_reference_and_ignore_window(strategy_id: str, to_up: bool) -> None:
    params = {"atr_period": 7, "multiplier": D("1.5")}
    bars = random_bars(5)
    trend = reference_supertrend(bars, 7, D("1.5"))
    expected = [i for i in range(1, len(bars))
                if trend[i - 1] is not None and trend[i - 1] is (not to_up) and trend[i] is to_up]
    assert len(expected) >= 4
    assert stream(strategy_id, bars, params, None)[0] == expected
    assert stream(strategy_id, bars, params, 10)[0] == expected
    assert stream(strategy_id, bars, params, 60)[0] == expected


@pytest.mark.parametrize("strategy_id", ("short_ema_bearish_crossover", "cover_supertrend_bullish_flip"))
def test_persisted_indicator_state_is_json_safe_and_resumes_exactly(strategy_id: str) -> None:
    params = {"fast_period": 5, "slow_period": 13} if "ema" in strategy_id else {"atr_period": 7}
    bars = random_bars(8, 200)
    hits_full, state = stream(strategy_id, bars, params, None)
    snapshot = json.loads(json.dumps(state))  # survives a JSON round trip (database persistence)
    assert {"indicator_state", "last_signal_bar"} <= set(snapshot)
    # restart from the stored snapshot after bar 150 and continue over a window of only 20 bars
    cls = REGISTRY.get(strategy_id)
    state2: dict[str, object] = dict(ARMED[side(strategy_id)])
    resumed = []
    for n in range(cls.required_bars(params), 201):
        if n == 150:  # simulated restart: state rebuilt from JSON
            state2 = json.loads(json.dumps(state2))
        result = cls().evaluate(context(bars[max(0, n - 20):n]), params, state2)
        state2.update(result.state_updates)
        if result.is_signal:
            resumed.append(n - 1)
    assert resumed == hits_full
    assert state2["indicator_state"]["latest"] == snapshot["indicator_state"]["latest"]  # type: ignore[index]


def test_foreign_or_corrupt_indicator_state_is_reseeded() -> None:
    params, bars, _ = cases()["short_ema_bearish_crossover"]
    for bad in ({"version": 1, "signature": "other", "calc": {}}, "garbage", {"version": 2}):
        state = {**ARMED["short"], "indicator_state": bad}
        assert run("short_ema_bearish_crossover", bars, params, state).is_signal
    # parameters changed since the snapshot was written: the old state is not reused
    first = run("short_ema_bearish_crossover", bars, params)
    stale_params = {**ARMED["short"], **first.state_updates}
    other = run("short_ema_bearish_crossover", bars, {"fast_period": 3, "slow_period": 4}, stale_params)
    assert other.state_updates["indicator_state"]["signature"].endswith("|3|4")  # type: ignore[index]
