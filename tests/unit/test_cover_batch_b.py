"""COVER Batch B: ten price-action, volume and divergence COVER (short-exit) strategies.

Strategy-level tests drive the real production classes on deterministic synthetic completed bars;
the engine test drives the real BACKTEST runner.  Nothing here touches a broker or the network.
"""

from __future__ import annotations

import inspect
import json
import math
import random
import sys
from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal

import pytest
from test_cover_batch_a import (
    ENGINE_OPEN,
    OPEN,
    SHORT_PARAMS,
    ctx,
    direct_signal_indexes,
)
from test_cover_batch_a import NEW_IDS as BATCH_A_IDS
from test_short_cover_engine import SHORT_ON
from test_short_cover_engine import run as backtest_run
from test_short_cover_strategies import candle

from mike_trade import app
from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import (
    Candle,
    CandleTimeframe,
)
from mike_trade.research.commands import _research_registry
from mike_trade.scheduling.calendar import SessionStatus
from mike_trade.strategies.base import (
    BuyStrategy,
    CoverStrategy,
    ParameterValidationError,
    ReasonCode,
    SellStrategy,
    ShortStrategy,
    make_signal_id,
)
from mike_trade.strategies.closed_bar import STATE_INDICATOR, STATE_LAST_SIGNAL_BAR

D = Decimal
REGISTRY = _research_registry()

NEW_IDS = (
    "cover_bullish_engulfing",
    "cover_hammer_reversal",
    "cover_double_bottom_reversal",
    "cover_inverse_head_and_shoulders",
    "cover_trendline_breakout",
    "cover_previous_swing_high_breakout",
    "cover_higher_high_higher_low",
    "cover_bull_flag_breakout",
    "cover_obv_breakout",
    "cover_bullish_momentum_divergence",
)

#: Small parameters so a compact fixture warms each rule.
PARAMS: dict[str, dict[str, object]] = {
    "cover_bullish_engulfing": {},
    "cover_hammer_reversal": {"trend_bars": 2},
    "cover_double_bottom_reversal": {"strength": 1, "lookback": 20},
    "cover_inverse_head_and_shoulders": {"strength": 1, "lookback": 30},
    "cover_trendline_breakout": {"strength": 1, "lookback": 20},
    "cover_previous_swing_high_breakout": {"strength": 1, "lookback": 10},
    "cover_higher_high_higher_low": {"strength": 1, "lookback": 10},
    "cover_bull_flag_breakout": {"lookback": 20, "max_flag_bars": 12},
    "cover_obv_breakout": {"period": 3},
    "cover_bullish_momentum_divergence": {"momentum_period": 2, "strength": 1, "lookback": 10},
}


# ------------------------------------------------------------------------------- fixtures


def bars_from(rows: Sequence[tuple[object, object, object, object]], start: int = 0) -> list[Candle]:
    """Bars from ``(open, high, low, close)`` rows at consecutive minutes."""
    return [candle(start + i, o, h, low, c) for i, (o, h, low, c) in enumerate(rows)]


def simple(closes: Sequence[object], start: int = 0, half: str = "0.5") -> list[Candle]:
    """Bars with open == close and a symmetric ``half`` range."""
    return [candle(start + i, c, D(str(c)) + D(half), D(str(c)) - D(half), c)
            for i, c in enumerate(closes)]


def path(points: Sequence[tuple[int, object]]) -> list[Decimal]:
    """Piecewise-linear closes through ``(index, close)`` corner points."""
    out: list[Decimal] = []
    for (i0, c0), (i1, c1) in zip(points, points[1:], strict=False):
        a, b = D(str(c0)), D(str(c1))
        for i in range(i0, i1):
            out.append(a + (b - a) * (i - i0) / (i1 - i0))
    out.append(D(str(points[-1][1])))
    return out


def next_bar(bars: list[Candle], rise: object = 1) -> Candle:
    """A further bar that keeps rising by ``rise`` (open == close, 0.5 half range)."""
    close = bars[-1].close + D(str(rise))
    return candle(len(bars) + _offset(bars), close, close + D("0.5"), close - D("0.5"), close)


def _offset(bars: list[Candle]) -> int:
    return int((bars[0].start - OPEN).total_seconds() // 60)


def volume_series(closes: Sequence[object], volumes: Sequence[object]) -> list[Candle]:
    out = []
    for i, (c, v) in enumerate(zip(closes, volumes, strict=True)):
        base = candle(i, c, D(str(c)) + D("0.5"), D(str(c)) - D("0.5"), c)
        out.append(Candle(base.symbol, base.timeframe, base.start, base.end, base.open, base.high,
                          base.low, base.close, D(str(v)), base.feed_id, base.consolidated,
                          base.origin, base.price_basis, base.status))
    return out


def double_bottom() -> list[Candle]:
    closes = path([(0, 100), (3, 90), (6, 100), (9, 90), (13, 100)])
    closes += [D(x) for x in ("99.8", "99.6", "99.4", "99.2", "99", "98.8")] + [D(101)]
    assert len(closes) == 21
    return simple(closes)


def inverse_head_and_shoulders() -> list[Candle]:
    closes = path([(0, 100), (4, 92), (8, 98), (12, 88), (16, 98), (20, 92), (29, 98)]) + [D(99)]
    assert len(closes) == 31
    return simple(closes)


def trendline() -> list[Candle]:
    closes = path([(0, 100), (4, 110), (8, 103), (12, 106), (16, 98), (19, 100)]) + [D(104)]
    assert len(closes) == 21
    return simple(closes)


def swing_high() -> list[Candle]:
    # pivot highs at bars 2 and 8 (12.5); the close crosses 12.5 on the last bar
    return simple([10, 11, 12, 11, "10.5", 11, "11.5", "11.8", 12, "11.9", "12.6"])


def hhhl() -> list[Candle]:
    return simple([10, 12, 11, 10, 11, 13, "12.5", 12, "11.5", 11, 12])


def hammer() -> list[Candle]:
    rows = [(20, "20.5", "19.5", 20), (19, "19.5", "18.5", 19), (18, "18.5", "17.5", 18),
            ("17.8", "18.0", "16.0", "17.9"),   # hammer: 1.8 lower shadow, 0.1 body, 0.1 upper
            ("17.9", "18.6", "17.8", "18.5")]   # confirmation: closes above the hammer's high
    return bars_from(rows)


def bull_flag(flag: int = 11) -> list[Candle]:
    closes = path([(0, 100), (8, 110)]) + [D(x) for x in ("108", "107.5", "108.5")]
    closes += [D("108.2")] * (flag - 3)
    # the pole top is bar 8; the flag follows it; the final bar breaks the flag high
    closes += [D("109.5")]
    return simple(closes)


def obv() -> list[Candle]:
    return volume_series([10, 9, 8, 9, 10], [100] * 5)


def divergence() -> list[Candle]:
    closes = [15, "14.5", 14, 12, 10, 12, 13, "12.5", 12, 11, "9.4", "10.5"]
    return simple(closes)


TRIGGER = {
    "cover_bullish_engulfing": lambda: bars_from(
        [(11, "11.5", "9.5", 10), (10, "10.5", "8.5", 9), ("8.9", "10.8", "8.7", "10.5")]),
    "cover_hammer_reversal": hammer,
    "cover_double_bottom_reversal": double_bottom,
    "cover_inverse_head_and_shoulders": inverse_head_and_shoulders,
    "cover_trendline_breakout": trendline,
    "cover_previous_swing_high_breakout": swing_high,
    "cover_higher_high_higher_low": hhhl,
    "cover_bull_flag_breakout": bull_flag,
    "cover_obv_breakout": obv,
    "cover_bullish_momentum_divergence": divergence,
}


def with_last(bars: list[Candle], close: object) -> list[Candle]:
    """The same history with the final bar replaced by a ``close`` bar (open == close)."""
    i = len(bars) - 1 + _offset(bars)
    c = D(str(close))
    last = candle(i, c, c + D("0.5"), c - D("0.5"), c)
    if bars[-1].volume != last.volume:
        last = Candle(last.symbol, last.timeframe, last.start, last.end, last.open, last.high,
                      last.low, last.close, bars[-1].volume, last.feed_id, last.consolidated,
                      last.origin, last.price_basis, last.status)
    return bars[:-1] + [last]


def quiet(strategy_id: str) -> list[Candle]:
    """Same length as the trigger case, but the last bar does not complete the pattern."""
    bars = TRIGGER[strategy_id]()
    if strategy_id == "cover_bullish_engulfing":
        return bars_from([(11, "11.5", "9.5", 10), (10, "10.5", "8.5", 9),
                          ("8.9", "9.9", "8.7", "9.6")])  # bullish but does not engulf
    if strategy_id == "cover_hammer_reversal":
        return bars[:-1] + bars_from([("17.9", "18.0", "17.8", "17.95")], start=4)  # no confirmation
    if strategy_id == "cover_bullish_momentum_divergence":
        return simple([15, "14.5", 14, 12, 10, 12, 13, "12.5", 12, 11, "10.2", "10.5"])
    if strategy_id == "cover_obv_breakout":
        return volume_series([10, 9, 8, 9, 9], [100] * 5)
    if strategy_id == "cover_higher_high_higher_low":
        return simple([10, 12, 11, 10, 11, 13, "12.5", 12, "11.5", 9, 12])  # lower low
    last = {
        "cover_double_bottom_reversal": "100.4",
        "cover_inverse_head_and_shoulders": "98.0",
        "cover_trendline_breakout": "102",
        "cover_previous_swing_high_breakout": "12.4",
        "cover_bull_flag_breakout": "108.9",
    }[strategy_id]
    return with_last(bars, last)


def ev(strategy_id: str, bars: list[Candle], params: dict[str, object] | None = None,
       state: dict[str, object] | None = None, **kw: object):
    chosen = PARAMS[strategy_id] if params is None else params
    return REGISTRY.get(strategy_id)().evaluate(ctx(bars, **kw), chosen, {} if state is None else state)  # type: ignore[arg-type]


def signal(strategy_id: str, bars: list[Candle], params: dict[str, object] | None = None) -> bool:
    return ev(strategy_id, bars, params).is_signal


# ------------------------------------------------------------------ registration and roles


def test_the_registry_holds_146_strategies_with_28_cover_entries() -> None:
    counts = {role: len(REGISTRY.list_strategies(role)) for role in StrategyRole}
    assert counts == {StrategyRole.BUY: 75, StrategyRole.SELL: 40, StrategyRole.SHORT: 3,
                      StrategyRole.COVER: 28}
    assert len(REGISTRY.list_strategies()) == 146
    covers = {c.spec.strategy_id for c in REGISTRY.list_strategies(StrategyRole.COVER)}
    assert len(set(NEW_IDS)) == 10 and set(NEW_IDS) <= covers and set(BATCH_A_IDS) <= covers
    all_ids = [c.spec.strategy_id for c in REGISTRY.list_strategies()]
    assert len(all_ids) == len(set(all_ids))


def test_every_new_strategy_is_registered_in_production_and_research() -> None:
    production = inspect.getsource(app.build_runtime)
    research = inspect.getsource(sys.modules[_research_registry.__module__]._research_registry)
    for strategy_id in NEW_IDS:
        cls = REGISTRY.get(strategy_id)
        assert production.count(f"{cls.__name__},") == 1, strategy_id
        assert research.count(f"{cls.__name__},") == 1, strategy_id
        assert cls.__module__.endswith(strategy_id)


def test_the_new_strategies_compose_into_a_shadow_runtime_as_per_symbol_covers(tmp_path) -> None:
    from mike_trade.app import (
        AppSettings,
        ShadowScenarioSettings,
        StrategyAssignment,
        SymbolStrategySettings,
        build_runtime,
    )
    from mike_trade.runtime import ExecutionMode

    settings = AppSettings(
        mode=ExecutionMode.SHADOW, db_path=tmp_path / "t.sqlite3", symbols=("SPY",),
        shadow_scenarios=(ShadowScenarioSettings(short_selling_enabled=True),),
        symbol_strategies=(SymbolStrategySettings(
            "SPY", (StrategyAssignment("buy", "momentum_breakout"),),
            shorts=(StrategyAssignment("short", "short_ema_bearish_crossover"),),
            covers=tuple(StrategyAssignment(i, i) for i in NEW_IDS)),))
    covers = build_runtime(settings).lifecycle.collections("SPY").covers
    assert [b.strategy_id for b in covers] == sorted(NEW_IDS)
    assert {b.role for b in covers} == {"cover"}


@pytest.mark.parametrize("strategy_id", NEW_IDS)
def test_the_spec_is_a_complete_cover_contract(strategy_id: str) -> None:
    cls = REGISTRY.get(strategy_id)
    spec = cls.spec
    assert issubclass(cls, CoverStrategy)
    assert not issubclass(cls, (BuyStrategy, SellStrategy, ShortStrategy))
    assert spec.side is StrategyRole.COVER and spec.strategy_id == strategy_id
    assert spec.version == "1.0.0" and spec.category == "technical"
    assert spec.completed_bars_only and spec.trigger_timeframe is CandleTimeframe.M1
    assert spec.methodology and spec.source and spec.execution_limitations
    assert any("never implies a BUY" in text for text in spec.execution_limitations)
    assert spec.warmup_bars == cls.required_bars({})
    expected = {STATE_LAST_SIGNAL_BAR} | ({STATE_INDICATOR} if strategy_id == "cover_obv_breakout" else set())
    assert set(spec.persistent_state_keys) == expected
    assert spec.requires_volume is (strategy_id == "cover_obv_breakout")
    text = inspect.getsource(sys.modules[cls.__module__])
    assert "BuyStrategy" not in text and "OrderSide" not in text and "OrderIntent" not in text
    assert "market_status" not in text.lower() and "MarketState" not in text


# --------------------------------------------------------------------------- signal semantics


@pytest.mark.parametrize("strategy_id", NEW_IDS)
def test_bullish_trigger_no_trigger_and_signal_identity(strategy_id: str) -> None:
    cls = REGISTRY.get(strategy_id)
    bars = TRIGGER[strategy_id]()
    evaluation = ev(strategy_id, bars)
    assert evaluation.is_signal and evaluation.side is StrategyRole.COVER
    assert evaluation.reason_codes == (ReasonCode.SIGNAL,)
    assert evaluation.bar_time == bars[-1].start and evaluation.reference_price == bars[-1].close
    assert evaluation.signal_id == make_signal_id(
        strategy_id, "1.0.0", "SPY", StrategyRole.COVER, bars[-1].start)
    no = ev(strategy_id, quiet(strategy_id))
    assert not no.is_signal and no.reason_codes == (ReasonCode.NO_SIGNAL,)
    assert set(evaluation.state_updates) <= set(cls.spec.persistent_state_keys)
    assert set(no.state_updates) <= set(cls.spec.persistent_state_keys)


@pytest.mark.parametrize("strategy_id", NEW_IDS)
def test_warmup_is_required_bars_and_fewer_bars_never_signal(strategy_id: str) -> None:
    cls = REGISTRY.get(strategy_id)
    bars = TRIGGER[strategy_id]()
    need = cls.required_bars(PARAMS[strategy_id])
    assert need <= len(bars)
    assert ev(strategy_id, bars[-(need - 1):]).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)
    assert ev(strategy_id, bars[-need:], state={}).reason_codes in (
        (ReasonCode.SIGNAL,), (ReasonCode.NO_SIGNAL,))  # exactly enough bars is evaluable


@pytest.mark.parametrize("strategy_id", NEW_IDS)
def test_stale_and_non_regular_sessions_never_signal(strategy_id: str) -> None:
    bars = TRIGGER[strategy_id]()
    assert ev(strategy_id, bars, stale=True).reason_codes == (ReasonCode.STALE_DATA,)
    for status in (SessionStatus.CLOSED, SessionStatus.PRE_OPEN, SessionStatus.HOLIDAY,
                   SessionStatus.UNKNOWN):
        assert ev(strategy_id, bars, session=status).reason_codes == (ReasonCode.NOT_ELIGIBLE,)


@pytest.mark.parametrize("strategy_id", NEW_IDS)
def test_a_cover_needs_no_entry_eligibility_and_no_market_state(strategy_id: str) -> None:
    bars = TRIGGER[strategy_id]()
    for state in ({}, {"eligibility": "WATCHING_BUY"}, {"eligibility": "WATCHING_SHORT"},
                  {"eligibility": "COOLDOWN", "market_state": "CRASH"}):
        assert ev(strategy_id, bars, state=dict(state)).is_signal


@pytest.mark.parametrize("strategy_id", NEW_IDS)
def test_the_same_completed_bar_never_signals_twice(strategy_id: str) -> None:
    bars = TRIGGER[strategy_id]()
    first = ev(strategy_id, bars)
    replay = ev(strategy_id, bars, state=dict(first.state_updates))
    assert first.is_signal and not replay.is_signal
    assert replay.reason_codes == (ReasonCode.NO_SIGNAL,)


@pytest.mark.parametrize("strategy_id", NEW_IDS)
def test_no_look_ahead_the_pattern_is_not_a_signal_before_its_completing_bar(strategy_id: str) -> None:
    bars = TRIGGER[strategy_id]()
    assert not ev(strategy_id, bars[:-1]).is_signal  # the completing bar is not yet known
    assert ev(strategy_id, bars).bar_time == bars[-1].start  # and the signal is never back-dated


@pytest.mark.parametrize("strategy_id", NEW_IDS)
def test_the_signal_is_an_edge_a_further_rising_bar_does_not_repeat_it(strategy_id: str) -> None:
    bars = TRIGGER[strategy_id]()
    assert signal(strategy_id, bars)
    extended = bars + [next_bar(bars)]
    assert not signal(strategy_id, extended)


@pytest.mark.parametrize("strategy_id", NEW_IDS)
def test_a_missing_bar_before_the_completing_bar_is_not_a_signal(strategy_id: str) -> None:
    bars = TRIGGER[strategy_id]()
    last = bars[-1]
    skipped = Candle(last.symbol, last.timeframe, last.start + timedelta(minutes=1),
                     last.end + timedelta(minutes=1), last.open, last.high, last.low, last.close,
                     last.volume, last.feed_id, last.consolidated, last.origin, last.price_basis,
                     last.status)
    assert ev(strategy_id, bars[:-1] + [skipped]).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


# ----------------------------------------------------------------- per-pattern definitions


def test_bullish_engulfing_requires_a_bearish_bar_engulfed_by_a_bullish_real_body() -> None:
    sid = "cover_bullish_engulfing"
    bearish = (11, "11.5", "9.5", 10)
    assert signal(sid, bars_from([bearish, ("9.9", "11.3", "9.8", "11.2")]))
    # boundary: opening exactly at the prior close and closing exactly at the prior open engulfs
    assert signal(sid, bars_from([bearish, (10, "11.2", "9.9", 11)]))
    # not engulfing: closes below the prior open, or opens above the prior close
    assert not signal(sid, bars_from([bearish, ("9.9", "10.9", "9.8", "10.9")]))
    assert not signal(sid, bars_from([bearish, ("10.1", "11.9", "10.0", "11.5")]))
    # a bullish prior bar, or a bearish second bar, is not the pattern
    assert not signal(sid, bars_from([(10, "11.5", "9.5", 11), (10.5, "12.5", "10.4", 12)]))
    assert not signal(sid, bars_from([bearish, ("10.1", "10.2", "8.0", "8.5")]))
    # shadows are irrelevant: only the real bodies engulf
    assert signal(sid, bars_from([(11, "20", "5", 10), ("9.9", "11.2", "9.8", "11.1")]))
    # a missing minute between the pair
    gapped = [candle(0, *bearish), candle(2, "9.9", "10.9", "9.8", "10.8")]
    assert ev(sid, gapped).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


def test_hammer_signals_only_on_the_confirmation_bar_after_a_hammer_that_ends_a_decline() -> None:
    sid = "cover_hammer_reversal"
    bars = hammer()
    assert signal(sid, bars)
    # the hammer bar itself is never a signal (unconfirmed)
    assert not signal(sid, bars[:-1])
    # confirmation must close strictly above the hammer's high
    assert not signal(sid, with_last(bars, "18.0")) and signal(sid, with_last(bars, "18.01"))
    # no preceding decline: the bar before the hammer is not lower than trend_bars earlier
    flat = bars_from([(20, "20.5", "19.5", 20), (21, "21.5", "20.5", 21), (21, "21.5", "20.5", 21)]
                     + [(r[0], r[1], r[2], r[3]) for r in
                        [("17.8", "18.0", "16.0", "17.9"), ("17.9", "18.6", "17.8", "18.5")]])
    assert not signal(sid, flat)
    # a candle that is not a hammer: lower shadow too short, or a long upper shadow
    short_shadow = bars[:3] + bars_from([("17.8", "18.0", "17.6", "17.9")], start=3) + [bars[4]]
    assert not signal(sid, short_shadow)
    upper = bars[:3] + bars_from([("17.8", "18.8", "16.0", "17.9")], start=3) + [
        candle(4, "17.9", "19.4", "17.8", "19.0")]
    assert not signal(sid, upper)
    # the whole decline window must be consecutive, not only the three pattern bars
    gapped = [candle(0, 20, "20.5", "19.5", 20)] + [
        candle(i + 1, b.open, b.high, b.low, b.close) for i, b in enumerate(bars[1:], 1)]
    assert ev(sid, gapped).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


def test_double_bottom_needs_two_matching_confirmed_lows_and_a_first_neckline_break() -> None:
    sid = "cover_double_bottom_reversal"
    bars = double_bottom()
    assert signal(sid, bars)
    assert not signal(sid, with_last(bars, "100.5"))  # equal to the interim peak (100.5): no break
    assert signal(sid, with_last(bars, "100.51"))
    # lows that differ by more than the tolerance are not a double bottom
    uneven = simple(path([(0, 100), (3, 90), (6, 100), (9, 87), (13, 100)])
                    + [D(x) for x in ("99.8", "99.6", "99.4", "99.2", "99", "98.8")] + [D(101)])
    assert not signal(sid, uneven)
    # the rise between the lows is too small for min_rise_pct
    shallow = simple(path([(0, 90), (3, 90), (6, "90.4"), (9, 90), (13, 91)])
                     + [D(x) for x in ("90.9", "90.8", "90.7", "90.6", "90.5", "90.4")] + [D("90.6")])
    assert not signal(sid, shallow)
    # only the *first* close above the neckline signals: an earlier break disqualifies it
    broken = double_bottom()
    broken = broken[:15] + simple([102], start=15) + broken[16:]
    assert not signal(sid, broken)
    # no second low confirmed yet when the bar before the break is the only low
    assert not signal(sid, simple(path([(0, 100), (3, 90), (6, 100), (20, 98)]) + [D(101)][:0]))
    # a hole in the pattern span is never bridged
    holed = double_bottom()
    holed = holed[:5] + [candle(i + 1, b.open, b.high, b.low, b.close) for i, b in enumerate(holed[5:], 5)]
    assert ev(sid, holed).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


def test_double_bottom_confirmation_uses_only_pivots_confirmed_before_the_signal_bar() -> None:
    sid = "cover_double_bottom_reversal"
    bars = double_bottom()
    # the second low is the bar just before the breakout: it has no right-hand bar yet, so it is
    # not a confirmed pivot and nothing fires even though the close clears the interim peak
    fresh = simple(path([(0, 100), (3, 90), (6, 100), (19, 90)]) + [D(101)])
    assert not signal(sid, fresh)
    assert signal(sid, bars)


def test_inverse_head_and_shoulders_needs_a_lower_head_alike_shoulders_and_a_neckline_break() -> None:
    sid = "cover_inverse_head_and_shoulders"
    bars = inverse_head_and_shoulders()
    assert signal(sid, bars)
    assert not signal(sid, with_last(bars, "98.0"))  # neckline is 98.5: no break
    assert not signal(sid, bars[:-1])
    def pattern(points):
        return simple(path(points) + [D(99)])
    # head not below the shoulders
    assert not signal(sid, pattern([(0, 100), (4, 92), (8, 98), (12, 93), (16, 98), (20, 92), (29, 98)]))
    # shoulders differ by more than shoulder_tolerance_pct
    assert not signal(sid, pattern([(0, 100), (4, 92), (8, 98), (12, 88), (16, 98), (20, 95), (29, 98)]))
    # an earlier close through the neckline since the right shoulder disqualifies the break
    early = bars[:25] + simple([100], start=25) + bars[26:]
    assert not signal(sid, early)
    # a sloping neckline is extended to the signal bar
    rising = pattern([(0, 100), (4, 92), (8, 96), (12, 88), (16, 100), (20, 92), (29, 100)])
    assert signal(sid, with_last(rising, 108)) and not signal(sid, with_last(rising, 100))
    # a hole inside the formation
    holed = bars[:10] + [candle(i + 1, b.open, b.high, b.low, b.close) for i, b in enumerate(bars[10:], 10)]
    assert ev(sid, holed).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


def test_trendline_breakout_requires_two_falling_confirmed_highs_and_a_first_close_through() -> None:
    sid = "cover_trendline_breakout"
    bars = trendline()
    assert signal(sid, bars)
    assert not signal(sid, with_last(bars, "102"))  # below the extended line (102.5 here)
    assert signal(sid, with_last(bars, "103"))
    # rising highs are not a falling resistance line
    rising = simple(path([(0, 100), (4, 104), (8, 103), (12, 110), (16, 98), (19, 100)]) + [D(111)])
    assert not signal(sid, rising)
    # a prior close through the line means this is not the first break
    broken = bars[:18] + simple([105], start=18) + bars[19:]
    assert not signal(sid, broken)
    # the line is extended with its slope: a level close above the *flat* H2 can still be below it
    assert not signal(sid, with_last(bars, "100"))
    # a missing minute between the first pivot and the signal bar
    holed = bars[:6] + [candle(i + 1, b.open, b.high, b.low, b.close) for i, b in enumerate(bars[6:], 6)]
    assert ev(sid, holed).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


def test_pivots_must_be_confirmed_by_completed_bars_before_the_signal_bar() -> None:
    sid = "cover_trendline_breakout"
    bars = trendline()
    # the second falling high is the bar right before the signal bar: with no right-hand bar it is
    # not a pivot, so the trendline is not yet established and nothing fires
    late = simple(path([(0, 100), (4, 110), (8, 103), (19, 106)]) + [D(111)])
    assert not signal(sid, late)
    assert signal(sid, bars)


def test_previous_swing_high_breakout_uses_the_latest_confirmed_pivot_not_the_channel_high() -> None:
    sid = "cover_previous_swing_high_breakout"
    bars = swing_high()
    assert signal(sid, bars)
    assert not signal(sid, with_last(bars, "12.5"))  # equal to the pivot high (12.5) is no break
    assert signal(sid, with_last(bars, "12.51"))
    # already above the pivot on the prior bar: a level, not an edge
    assert not signal(sid, simple([10, 11, 12, 11, "10.5", 11, "11.5", "11.8", 12, "12.7", "12.9"]))
    # no confirmed pivot high in the lookback (a monotone rise) -> no resistance
    assert not signal(sid, simple([10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]))
    # distinct from the Donchian channel: a higher older peak keeps the channel high above the close
    older = simple([10, 15, 12, 11, "10.5", 11, "11.5", "11.8", 12, "11.9", "12.6"])
    assert signal(sid, older)
    donchian = REGISTRY.get("cover_donchian_breakout")()
    assert not donchian.evaluate(ctx(older), {"period": 9}, {}).is_signal
    # the pivot immediately before the signal bar lacks a right-hand bar and is not yet resistance
    unconfirmed = simple([10, 11, 12, 11, "10.5", 11, "11.5", "11.8", 12, "11.9", "13"])
    assert signal(sid, unconfirmed)  # R is still the confirmed 12.5 pivot, crossed by this close
    # a pivot whose own bars contain a hole is not recognised (nothing is bridged): the sole
    # candidate pivot (bar 2) loses its right-hand neighbour's continuity
    lone = simple([10, 11, 12, 11, "10.5", "10.6", "10.7", "10.8", "10.9", 11, "12.6"])
    assert signal(sid, lone)
    holed = lone[:3] + [candle(i + 1, b.open, b.high, b.low, b.close) for i, b in enumerate(lone[3:], 3)]
    assert ev(sid, holed).reason_codes == (ReasonCode.NO_SIGNAL,)


def test_higher_high_higher_low_signals_exactly_on_the_bar_that_confirms_the_higher_low() -> None:
    sid = "cover_higher_high_higher_low"
    bars = hhhl()
    assert signal(sid, bars)
    assert not signal(sid, bars[:-1])  # the higher low is not yet confirmed one bar earlier
    assert not signal(sid, bars + [next_bar(bars)])  # and not re-signalled a bar later
    # a lower low breaks the structure
    assert not signal(sid, simple([10, 12, 11, 10, 11, 13, "12.5", 12, "11.5", 9, 12]))
    # the latest swing high is not higher than the previous one
    assert not signal(sid, simple([10, 12, 11, 10, 11, 12, "11.5", 12, "11.5", 11, 12]))
    # the higher low must be the pivot confirmed on this very bar, not an older one
    older = simple([10, 12, 11, 10, 11, 13, "12.5", 12, "11.5", 12, 12.5])
    assert not signal(sid, older)
    # each pivot's own bars must be adjacent
    holed = bars[:6] + [candle(i + 1, b.open, b.high, b.low, b.close) for i, b in enumerate(bars[6:], 6)]
    assert not signal(sid, holed)  # the hole splits the second high's bars: no bridged pivot


def test_bull_flag_requires_the_advance_the_flag_and_a_close_above_the_flag_high() -> None:
    sid = "cover_bull_flag_breakout"
    bars = bull_flag()
    assert signal(sid, bars)
    assert not signal(sid, with_last(bars, "109"))  # equal to the flag's highest high (109.0)
    assert signal(sid, with_last(bars, "109.01"))
    # no flagpole: the advance is smaller than pole_pct
    assert not signal(sid, simple(path([(0, 100), (8, "100.4")]) + [D("100.2")] * 12 + [D("100.9")]))
    # the flag retraces more than max_retrace of the pole
    deep = bars[:12] + simple([103], start=12) + bars[13:]
    assert not signal(sid, deep)
    # the flag is shorter than min_flag_bars (breakout two bars after the pole top)
    assert not signal(sid, simple(path([(0, 100), (17, 110)]) + [D("108.5"), D("108.3"), D("110.6")]))
    # the flag outlasts max_flag_bars
    long_flag = simple(path([(0, 100), (8, 110)]) + [D("108.2")] * 12 + [D("109.5")])
    assert not signal(sid, long_flag, {"lookback": 20, "max_flag_bars": 5})
    # only the first breakout bar signals: a continuation bar above it does not, although the
    # BUY version of the same pattern would
    cont = bars + simple(["111"], start=len(bars))
    assert not signal(sid, cont)
    assert REGISTRY.get("bull_flag_breakout")().evaluate(
        ctx(cont), {"lookback": 20, "max_flag_bars": 12}, {"eligibility": "WATCHING_BUY"}).is_signal
    # a hole in the pole or flag
    holed = bars[:5] + [candle(i + 1, b.open, b.high, b.low, b.close) for i, b in enumerate(bars[5:], 5)]
    assert ev(sid, holed).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


def test_obv_breakout_threshold_excludes_the_current_bar_and_fires_once() -> None:
    sid = "cover_obv_breakout"
    assert signal(sid, obv())
    # equal to the previous window's maximum is not a breakout: [10, 9, 8, 9, 9] stays below
    assert not signal(sid, volume_series([10, 9, 8, 9, 9], [100] * 5))
    # the bar after the breakout is already above its own window's threshold's prior: no repeat
    assert not signal(sid, volume_series([10, 9, 8, 9, 10, 11], [100] * 6))
    # volume matters: a larger up-volume produces a breakout a flat price series would not
    assert signal(sid, volume_series([10, 10, 10, 10, 11], [100] * 4 + [100]))
    assert not signal(sid, volume_series([10, 10, 10, 10, 10], [100] * 4 + [500]))  # unchanged close
    # a down close subtracts volume
    assert not signal(sid, volume_series([10, 11, 12, 13, 12], [100] * 4 + [900]))


def test_bullish_momentum_divergence_samples_momentum_at_the_pivot_not_the_confirmation_bar() -> None:
    sid = "cover_bullish_momentum_divergence"
    bars = divergence()
    assert signal(sid, bars)
    assert not signal(sid, bars[:-1])  # the low is not confirmed one bar earlier
    assert not signal(sid, bars + [next_bar(bars)])  # the divergence fires once
    # a higher low is not a lower price pivot
    assert not signal(sid, simple([15, "14.5", 14, 12, 10, 12, 13, "12.5", 12, 11, "10.2", "10.5"]))
    # lower low with *lower* momentum at the pivot (a sharper fall into the second low): no divergence
    steeper = simple([15, "14.5", "10.5", "10.4", 10, 12, 13, "12.5", 12, 11, 4, 8])
    assert not signal(sid, steeper)
    # momentum at the later confirmation bar is higher, but at the pivot it is lower: still no signal
    sharp = simple([15, "14.5", 11, "10.8", 10, 12, 13, "12.5", 12, 11, "8.5", 14])
    assert not signal(sid, sharp)
    # the divergence uses the lookback window's two *latest* pivots
    only_one = simple([15, "14.5", 14, "13.5", 13, "12.5", 12, "11.5", 11, "10.5", "9.5", "10.5"])
    assert not signal(sid, only_one)
    # the whole momentum + structure window must be adjacent
    holed = bars[:3] + [candle(i + 1, b.open, b.high, b.low, b.close) for i, b in enumerate(bars[3:], 3)]
    assert ev(sid, holed).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


# ------------------------------------------------------------------------------ parameters

INVALID: tuple[tuple[str, dict[str, object]], ...] = (
    ("cover_bullish_engulfing", {"anything": 1}),
    ("cover_hammer_reversal", {"trend_bars": 1}),
    ("cover_hammer_reversal", {"trend_bars": 51}),
    ("cover_hammer_reversal", {"shadow_ratio": D("0.9")}),
    ("cover_hammer_reversal", {"max_upper_shadow": D("0.6")}),
    ("cover_double_bottom_reversal", {"strength": 0}),
    ("cover_double_bottom_reversal", {"lookback": 19}),
    ("cover_double_bottom_reversal", {"strength": 10, "lookback": 40}),  # lookback >= 8 * strength
    ("cover_double_bottom_reversal", {"tolerance_pct": D("0.01")}),
    ("cover_double_bottom_reversal", {"min_rise_pct": D(31)}),
    ("cover_inverse_head_and_shoulders", {"strength": 11}),
    ("cover_inverse_head_and_shoulders", {"strength": 5, "lookback": 40}),  # lookback >= 12 * strength
    ("cover_inverse_head_and_shoulders", {"lookback": 501}),
    ("cover_inverse_head_and_shoulders", {"shoulder_tolerance_pct": D(0)}),
    ("cover_inverse_head_and_shoulders", {"min_head_depth_pct": D(21)}),
    ("cover_trendline_breakout", {"strength": 0}),
    ("cover_trendline_breakout", {"strength": 5, "lookback": 30}),  # lookback >= 8 * strength
    ("cover_trendline_breakout", {"lookback": 401}),
    ("cover_previous_swing_high_breakout", {"strength": 11}),
    ("cover_previous_swing_high_breakout", {"lookback": 9}),
    ("cover_previous_swing_high_breakout", {"lookback": 301}),
    ("cover_higher_high_higher_low", {"strength": 0}),
    ("cover_higher_high_higher_low", {"strength": 10, "lookback": 40}),  # lookback >= 4 * s + 2
    ("cover_bull_flag_breakout", {"lookback": 19}),
    ("cover_bull_flag_breakout", {"min_flag_bars": 10, "max_flag_bars": 5}),
    ("cover_bull_flag_breakout", {"lookback": 20, "max_flag_bars": 19}),  # lookback > max_flag + 1
    ("cover_bull_flag_breakout", {"max_retrace": D("0.95")}),
    ("cover_bull_flag_breakout", {"pole_pct": D(0)}),
    ("cover_obv_breakout", {"period": 1}),
    ("cover_obv_breakout", {"period": 201}),
    ("cover_bullish_momentum_divergence", {"momentum_period": 1}),
    ("cover_bullish_momentum_divergence", {"strength": 11}),
    ("cover_bullish_momentum_divergence", {"strength": 5, "lookback": 20}),  # lookback >= 4 * s + 2
)


@pytest.mark.parametrize("strategy_id,params", INVALID)
def test_invalid_parameters_are_rejected_before_any_evaluation(
    strategy_id: str, params: dict[str, object]
) -> None:
    with pytest.raises(ParameterValidationError):
        ev(strategy_id, TRIGGER[strategy_id](), params)


@pytest.mark.parametrize("strategy_id", NEW_IDS)
def test_unknown_and_float_parameters_are_rejected_and_defaults_validate(strategy_id: str) -> None:
    schema = REGISTRY.get(strategy_id).spec.parameter_schema
    assert schema.validate({}) is not None
    with pytest.raises(ParameterValidationError):
        REGISTRY.validate_parameters(strategy_id, {"unknown_parameter": 1})
    for spec in schema.parameters:
        with pytest.raises(ParameterValidationError):
            schema.validate({spec.name: 2.5})
        assert spec.description


@pytest.mark.parametrize("strategy_id", NEW_IDS)
def test_required_bars_follow_the_configured_lookback(strategy_id: str) -> None:
    cls = REGISTRY.get(strategy_id)
    expected = {
        "cover_bullish_engulfing": ({}, 2),
        "cover_hammer_reversal": ({"trend_bars": 9}, 12),
        "cover_double_bottom_reversal": ({"lookback": 40}, 41),
        "cover_inverse_head_and_shoulders": ({"lookback": 60}, 61),
        "cover_trendline_breakout": ({"lookback": 30}, 31),
        "cover_previous_swing_high_breakout": ({"lookback": 25}, 26),
        "cover_higher_high_higher_low": ({"lookback": 25}, 26),
        "cover_bull_flag_breakout": ({"lookback": 40}, 41),
        "cover_obv_breakout": ({"period": 7}, 9),
        "cover_bullish_momentum_divergence": ({"momentum_period": 5, "lookback": 30}, 35),
    }[strategy_id]
    assert cls.required_bars(expected[0]) == expected[1]


# ----------------------------------------------- OBV persistence, restart and advance-only


def obv_walk(n: int, seed: int) -> list[Candle]:
    rng = random.Random(seed)
    price = 100.0
    out = []
    for i in range(n):
        price += math.sin(i / 4) * 0.7 + rng.uniform(-0.8, 0.8)
        close = D(f"{price:.2f}")
        base = candle(i, close, close + D("0.3"), close - D("0.3"), close)
        out.append(Candle(base.symbol, base.timeframe, base.start, base.end, base.open, base.high,
                          base.low, base.close, D(rng.randint(50, 500)), base.feed_id,
                          base.consolidated, base.origin, base.price_basis, base.status))
    return out


def obv_signals(bars: list[Candle], params: dict[str, object], window: int | None,
                ineligible: frozenset[int] = frozenset(), restart: bool = False) -> list[int]:
    state: dict[str, object] = {}
    found = []
    for i in range(len(bars)):
        view = bars[: i + 1] if window is None else bars[max(0, i + 1 - window): i + 1]
        kw = {"stale": True} if i in ineligible else {}
        result = ev("cover_obv_breakout", view, params, {} if window is None else state, **kw)
        if window is not None:
            state = {**state, **result.state_updates}
            if restart:
                state = json.loads(json.dumps(state))
        if result.is_signal:
            found.append(i)
    return found


def test_obv_state_makes_a_bounded_window_equal_the_full_history_and_survives_restarts() -> None:
    params = {"period": 5}
    bars = obv_walk(200, seed=9)
    expected = obv_signals(bars, params, None)
    assert len(expected) >= 3
    assert obv_signals(bars, params, 8) == expected
    assert obv_signals(bars, params, 8, restart=True) == expected


def test_obv_state_keeps_advancing_while_the_cover_is_ineligible() -> None:
    params = {"period": 5}
    bars = obv_walk(200, seed=4)
    expected = obv_signals(bars, params, None)
    ineligible = frozenset(i for i in range(len(bars)) if i % 4 in (1, 2))
    assert obv_signals(bars, params, 8, ineligible) == [i for i in expected if i not in ineligible]
    blocked = ev("cover_obv_breakout", bars[:12], params, {}, stale=True)
    assert blocked.reason_codes == (ReasonCode.STALE_DATA,) and STATE_INDICATOR in blocked.state_updates


def test_obv_corrupt_foreign_or_changed_state_is_reseeded_and_does_not_leak() -> None:
    bars = TRIGGER["cover_obv_breakout"]()
    first = ev("cover_obv_breakout", bars)
    good = first.state_updates[STATE_INDICATOR]
    assert isinstance(good, dict)
    for junk in ({STATE_INDICATOR: {"junk": 1}}, {STATE_INDICATOR: "text"},
                 {STATE_INDICATOR: {**good, "signature": "x|y|z"}},
                 {STATE_INDICATOR: {**good, "calc": {"kind": "obv"}, "last_bar": bars[0].start.isoformat()}}):
        assert ev("cover_obv_breakout", bars, state=junk).is_signal
    other = ev("cover_obv_breakout", obv_walk(30, 1), {"period": 4}, dict(first.state_updates))
    assert other.state_updates[STATE_INDICATOR]["signature"] != good["signature"]  # type: ignore[index]


@pytest.mark.parametrize("strategy_id", tuple(i for i in NEW_IDS if i != "cover_obv_breakout"))
def test_pattern_strategies_keep_no_indicator_state(strategy_id: str) -> None:
    bars = TRIGGER[strategy_id]()
    for evaluation in (ev(strategy_id, bars), ev(strategy_id, quiet(strategy_id)),
                       ev(strategy_id, bars, stale=True)):
        assert STATE_INDICATOR not in evaluation.state_updates


# ------------------------------------------------------------------------- BACKTEST parity


def motif_closes(points: Sequence[tuple[int, object]], cycles: int) -> list[float]:
    one = [float(c) for c in path(points)][:-1]
    return one * cycles


def tape(kind: str = "random", n: int = 330, seed: int = 7
         ) -> tuple[list[tuple[str, str, str, str]], list[Candle]]:
    """A noisy tape with random wicks, as engine rows and as strategy-test candles.

    ``random`` is a cyclical walk; ``double_bottom`` and ``head_shoulders`` repeat the textbook
    formation (with noise) so the chart-pattern strategies have formations to complete.
    """
    rng = random.Random(seed)
    rows, candles = [], []
    previous = D("100.00")
    motifs = {
        "double_bottom": motif_closes([(0, 100), (3, 90), (6, 100), (9, 90), (12, 99), (13, 103),
                                       (20, 104), (30, 100)], 12),
        "head_shoulders": motif_closes([(0, 100), (4, 92), (8, 98), (12, 88), (16, 98), (20, 92),
                                        (29, 98), (30, 100), (36, 106), (50, 100)], 7),
    }
    for i in range(n):
        if kind == "random":
            target = 100 + 5 * math.sin(2 * math.pi * i / 45) + 1.5 * math.sin(i / 2.3)
        else:
            target = motifs[kind][min(i, len(motifs[kind]) - 1)]
        close = D(f"{target + rng.uniform(-0.3, 0.3):.2f}")
        up, down = D(f"{rng.uniform(0.05, 0.6):.2f}"), D(f"{rng.uniform(0.05, 0.9):.2f}")
        high, low = max(previous, close) + up, min(previous, close) - down
        rows.append((str(previous), str(high), str(low), str(close)))
        base = candle(i, previous, high, low, close)
        candles.append(Candle(base.symbol, base.timeframe, base.start, base.end, base.open,
                              base.high, base.low, base.close, D(100000), base.feed_id,
                              base.consolidated, base.origin, base.price_basis, base.status))
        previous = close
    return rows, candles


TAPE_KIND = {"cover_double_bottom_reversal": "double_bottom",
             "cover_inverse_head_and_shoulders": "head_shoulders"}
BACKTEST_PARAMS: dict[str, dict[str, object]] = {
    "cover_bullish_engulfing": {},
    "cover_hammer_reversal": {"trend_bars": 2, "shadow_ratio": D(1), "max_upper_shadow": D("0.5")},
    "cover_double_bottom_reversal": {"strength": 1, "lookback": 20, "tolerance_pct": D(3),
                                     "min_rise_pct": D("0.5")},
    "cover_inverse_head_and_shoulders": {"strength": 1, "lookback": 30, "shoulder_tolerance_pct": D(5),
                                         "min_head_depth_pct": D("0.1")},
    "cover_trendline_breakout": {"strength": 1, "lookback": 20},
    "cover_previous_swing_high_breakout": {"strength": 1, "lookback": 10},
    "cover_higher_high_higher_low": {"strength": 1, "lookback": 10},
    "cover_bull_flag_breakout": {"lookback": 20, "pole_pct": D("0.3"), "min_flag_bars": 2,
                                 "max_flag_bars": 8, "max_retrace": D("0.8")},
    "cover_obv_breakout": {"period": 5},
    "cover_bullish_momentum_divergence": {"momentum_period": 3, "strength": 1, "lookback": 12},
}


@pytest.mark.parametrize("strategy_id", NEW_IDS)
def test_backtest_covers_only_on_bars_the_strategy_signals_and_never_buys(
    strategy_id: str, tmp_path
) -> None:
    rows, candles = tape(TAPE_KIND.get(strategy_id, "random"))
    params = BACKTEST_PARAMS[strategy_id]
    result = backtest_run(
        rows, ("short_ema_bearish_crossover", strategy_id),
        {"short_ema_bearish_crossover": SHORT_PARAMS, strategy_id: params},
        config={**SHORT_ON, "minimum_hold_seconds": 60}, registry=REGISTRY, result_dir=tmp_path)
    order = [fill["side"] for fill in result.fills]
    assert set(order) <= {"SELL_SHORT", "BUY_TO_COVER"} and "BUY" not in order
    assert order[0::2] == ["SELL_SHORT"] * len(order[0::2])
    assert order[1::2] == ["BUY_TO_COVER"] * len(order[1::2])
    covers = [row for row in result.intents if row["side"] == "BUY_TO_COVER"]
    assert covers, "the tape should drive at least one real cover"
    direct = direct_signal_indexes(strategy_id, params, candles)
    cover_bars = {int((row["signal_bar_time"] - ENGINE_OPEN).total_seconds() // 60) for row in covers}
    assert cover_bars <= direct, (strategy_id, sorted(cover_bars - direct))
    for opened, closed in zip(result.fills[0::2], result.fills[1::2], strict=False):
        assert opened["quantity"] == closed["quantity"]  # the full short is covered


@pytest.mark.parametrize("strategy_id", NEW_IDS)
def test_backtest_never_covers_without_a_short(strategy_id: str, tmp_path) -> None:
    rows, _ = tape(TAPE_KIND.get(strategy_id, "random"))
    result = backtest_run(rows, (strategy_id,), {strategy_id: BACKTEST_PARAMS[strategy_id]},
                          config=SHORT_ON, registry=REGISTRY, result_dir=tmp_path)
    assert result.fills == () and [i for i in result.intents if i["side"] == "BUY_TO_COVER"] == []


def test_the_synthetic_tape_starts_at_the_regular_session_open() -> None:
    assert OPEN.hour == 13 and ENGINE_OPEN.hour == 13  # 09:30 America/New_York (EDT / EDT)
