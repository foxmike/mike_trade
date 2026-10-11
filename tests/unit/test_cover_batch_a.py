"""COVER Batch A: the 15 additional COVER (short-exit) strategies.

Strategy-level tests use the real production strategy classes on deterministic synthetic
completed bars; the engine tests drive the real BACKTEST runner and SHADOW runtime.  Nothing here
touches a broker or the network.
"""

from __future__ import annotations

import inspect
import json
import math
import random
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from test_short_cover_config import _document, _load
from test_short_cover_engine import OPEN as ENGINE_OPEN
from test_short_cover_engine import SHORT_ON
from test_short_cover_engine import run as backtest_run
from test_short_cover_strategies import (
    candle,
    context,
    series,
)

from mike_trade import app
from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import (
    Candle,
    CandleOrigin,
    CandleStatus,
    CandleTimeframe,
    PriceBasis,
)
from mike_trade.market_data.context import MarketContext
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
OPEN = datetime(2026, 10, 8, 9, 30, tzinfo=ZoneInfo("America/New_York")).astimezone(UTC)

NEW_IDS = (
    "cover_sma_bullish_crossover",
    "cover_macd_bullish_crossover",
    "cover_macd_zero_line_crossover",
    "cover_rsi_oversold_reversal",
    "cover_rsi_centerline_crossover",
    "cover_dmi_bullish_crossover",
    "cover_aroon_bullish_reversal",
    "cover_parabolic_sar_bullish_reversal",
    "cover_vwap_reclaim",
    "cover_bollinger_lower_band_reversion",
    "cover_keltner_channel_reversion",
    "cover_cci_oversold_reversal",
    "cover_stochastic_bullish_crossover",
    "cover_williams_r_bullish_reversal",
    "cover_chaikin_money_flow_recovery",
)
OLD_IDS = ("cover_ema_bullish_crossover", "cover_supertrend_bullish_flip", "cover_donchian_breakout")
#: Covers whose indicator is recursive and therefore persists ``indicator_state``.
RECURSIVE = (
    "cover_macd_bullish_crossover", "cover_macd_zero_line_crossover", "cover_rsi_oversold_reversal",
    "cover_rsi_centerline_crossover", "cover_dmi_bullish_crossover",
    "cover_parabolic_sar_bullish_reversal", "cover_keltner_channel_reversion",
)
#: Covers evaluated from bar history alone (the VWAP rule also needs the indicator service).
BAR_ONLY = tuple(i for i in NEW_IDS if i != "cover_vwap_reclaim")

#: Small parameters so a handful of bars warms each indicator.
PARAMS: dict[str, dict[str, object]] = {
    "cover_sma_bullish_crossover": {"fast_period": 2, "slow_period": 4},
    "cover_macd_bullish_crossover": {"fast_period": 2, "slow_period": 4, "signal_period": 2},
    "cover_macd_zero_line_crossover": {"fast_period": 2, "slow_period": 4},
    "cover_rsi_oversold_reversal": {"rsi_period": 3},
    "cover_rsi_centerline_crossover": {"rsi_period": 3},
    "cover_dmi_bullish_crossover": {"period": 3},
    "cover_aroon_bullish_reversal": {"period": 3},
    "cover_parabolic_sar_bullish_reversal": {},
    "cover_vwap_reclaim": {},
    "cover_bollinger_lower_band_reversion": {"period": 4, "deviations": D(1)},
    "cover_keltner_channel_reversion": {"ema_period": 3, "atr_period": 2, "multiplier": D("0.5")},
    "cover_cci_oversold_reversal": {"period": 4},
    "cover_stochastic_bullish_crossover": {"k_period": 3, "k_smooth": 1, "d_period": 2},
    "cover_williams_r_bullish_reversal": {"period": 3},
    "cover_chaikin_money_flow_recovery": {"period": 3},
}
#: closes whose last bar is the bullish trigger for each close-driven strategy
TRIGGER_CLOSES: dict[str, list[int]] = {
    "cover_sma_bullish_crossover": [30, 29, 28, 27, 31],
    "cover_macd_bullish_crossover": [30, 29, 28, 27, 26, 27],
    "cover_macd_zero_line_crossover": [30, 29, 28, 27, 30],
    "cover_rsi_oversold_reversal": [30, 29, 28, 27, 28],
    "cover_rsi_centerline_crossover": [30, 29, 28, 27, 30],
    "cover_dmi_bullish_crossover": [30, 29, 28, 27, 30],
    "cover_aroon_bullish_reversal": [30, 29, 28, 27, 29],
    "cover_parabolic_sar_bullish_reversal": [30, 29, 28, 30],
    "cover_bollinger_lower_band_reversion": [30, 29, 28, 27, 28],
    "cover_keltner_channel_reversion": [30, 29, 28, 27, 28],
    "cover_cci_oversold_reversal": [30, 29, 28, 27, 28],
    "cover_stochastic_bullish_crossover": [30, 29, 28, 27, 28],
    "cover_williams_r_bullish_reversal": [30, 29, 28, 29],
}
VWAP = {"vwap": D(10), "vwap_prev": D(10)}


def volume_bar(i: int, close: int, where: str, volume: object = 100) -> Candle:
    """A 1-wide bar whose close sits at its ``low`` or ``high`` (money-flow multiplier -1 / +1)."""
    start = OPEN + timedelta(minutes=i)
    low = D(close) if where == "low" else D(close) - 1
    return Candle("SPY", CandleTimeframe.M1, start, start + timedelta(minutes=1), D(close),
                  low + 1, low, D(close), D(str(volume)), "sip", True, CandleOrigin.REPLAY,
                  PriceBasis.RAW, CandleStatus.CLOSED)


def ctx(bars: list[Candle], *, stale: bool = False, session: SessionStatus = SessionStatus.REGULAR,
        indicators: dict[str, object] | None = None) -> MarketContext:
    base = context(bars, stale=stale, session=session)
    if indicators is None:
        return base
    return MarketContext(
        symbol=base.symbol, as_of=base.as_of, version=base.version, feed_id=base.feed_id,
        consolidated=base.consolidated, price_basis=base.price_basis,
        session_status=base.session_status, session_id=base.session_id, freshness=base.freshness,
        candles=base.candles, indicators=indicators)


def evaluate(strategy_id: str, bars: list[Candle], params: dict[str, object] | None = None,
             state: dict[str, object] | None = None, **kw: object):
    chosen = PARAMS[strategy_id] if params is None else params
    if strategy_id == "cover_vwap_reclaim" and "indicators" not in kw:
        kw["indicators"] = VWAP
    return REGISTRY.get(strategy_id)().evaluate(ctx(bars, **kw), chosen, {} if state is None else state)  # type: ignore[arg-type]


def case(strategy_id: str) -> tuple[list[Candle], list[Candle]]:
    """(bars whose last bar triggers, same-length bars that continue the decline instead)."""
    if strategy_id == "cover_vwap_reclaim":
        return series([10, "9.8", "10.2"]), series([10, "9.8", "9.9"])
    if strategy_id == "cover_chaikin_money_flow_recovery":
        head = [volume_bar(i, 10, "low") for i in range(3)]
        return head + [volume_bar(3, 10, "high", 500)], head + [volume_bar(3, 10, "low")]
    closes = TRIGGER_CLOSES[strategy_id]
    return series(closes), series(closes[:-1] + [closes[-2] - 1])


# ------------------------------------------------------------------- registration and roles


def test_the_registry_holds_176_strategies_with_28_cover_entries() -> None:
    counts = {role: len(REGISTRY.list_strategies(role)) for role in StrategyRole}
    assert counts == {
        StrategyRole.BUY: 80,
        StrategyRole.SELL: 40,
        StrategyRole.SHORT: 28,
        StrategyRole.COVER: 28,
    }
    assert len(REGISTRY.list_strategies()) == 176
    assert {c.spec.strategy_id for c in REGISTRY.list_strategies(StrategyRole.COVER)} >= set(
        OLD_IDS + NEW_IDS)
    assert len(set(NEW_IDS)) == 15
    all_ids = [c.spec.strategy_id for c in REGISTRY.list_strategies()]
    assert len(all_ids) == len(set(all_ids))


def test_every_new_strategy_is_registered_in_the_production_composition() -> None:
    source = inspect.getsource(app.build_runtime)
    for strategy_id in NEW_IDS:
        cls = REGISTRY.get(strategy_id)
        assert source.count(f"{cls.__name__},") == 1, strategy_id
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
    assert [b.strategy_id for b in covers] == sorted(NEW_IDS)  # only these were assigned
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
    expected_state = {STATE_LAST_SIGNAL_BAR} | ({STATE_INDICATOR} if strategy_id in RECURSIVE else set())
    if strategy_id == "cover_vwap_reclaim":
        expected_state = {STATE_LAST_SIGNAL_BAR, "session_id"}
    assert set(spec.persistent_state_keys) == expected_state
    assert spec.requires_volume is (strategy_id in ("cover_vwap_reclaim", "cover_chaikin_money_flow_recovery"))
    module = sys.modules[cls.__module__]
    text = inspect.getsource(module)
    assert "BuyStrategy" not in text and "OrderSide" not in text and "OrderIntent" not in text
    assert "market_status" not in text.lower() and "MarketState" not in text


@pytest.mark.parametrize("strategy_id", NEW_IDS)
def test_config_accepts_the_strategy_in_the_cover_role_only(strategy_id: str, tmp_path) -> None:
    doc = _document()
    doc["strategies"]["cover_ema"] = {"plugin": strategy_id, "side": "cover", "parameters": {}}
    entry = next(iter(doc["symbols"].values()))
    assert _load(tmp_path, doc)["symbols"]
    entry["buy_strategies"] = ["cover_ema"]
    doc["strategies"]["cover_ema"]["side"] = "buy"
    with pytest.raises(Exception):  # noqa: B017 - a COVER plugin cannot be bound as a BUY
        _load(tmp_path, doc)


# ------------------------------------------------------------------------ signal semantics


@pytest.mark.parametrize("strategy_id", NEW_IDS)
def test_bullish_trigger_no_trigger_and_signal_identity(strategy_id: str) -> None:
    cls = REGISTRY.get(strategy_id)
    bars, quiet = case(strategy_id)
    evaluation = evaluate(strategy_id, bars)
    assert evaluation.is_signal and evaluation.side is StrategyRole.COVER
    assert evaluation.reason_codes == (ReasonCode.SIGNAL,)
    assert evaluation.bar_time == bars[-1].start and evaluation.reference_price == bars[-1].close
    assert evaluation.signal_id == make_signal_id(
        strategy_id, "1.0.0", "SPY", StrategyRole.COVER, bars[-1].start)
    assert evaluation.signal_id != make_signal_id(
        strategy_id, "1.0.0", "SPY", StrategyRole.BUY, bars[-1].start)
    quiet_eval = evaluate(strategy_id, quiet)
    assert not quiet_eval.is_signal and quiet_eval.reason_codes == (ReasonCode.NO_SIGNAL,)
    assert set(evaluation.state_updates) <= set(cls.spec.persistent_state_keys)
    assert set(quiet_eval.state_updates) <= set(cls.spec.persistent_state_keys)


@pytest.mark.parametrize("strategy_id", NEW_IDS)
def test_warmup_is_required_bars_and_fewer_bars_never_signal(strategy_id: str) -> None:
    cls = REGISTRY.get(strategy_id)
    bars, _ = case(strategy_id)
    need = cls.required_bars(PARAMS[strategy_id])
    assert 2 <= need <= len(bars)
    short = evaluate(strategy_id, bars[-(need - 1):])
    assert short.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)
    assert cls.required_bars({}) == cls.spec.warmup_bars


@pytest.mark.parametrize("strategy_id", NEW_IDS)
def test_stale_and_non_regular_sessions_never_signal(strategy_id: str) -> None:
    bars, _ = case(strategy_id)
    assert evaluate(strategy_id, bars, stale=True).reason_codes == (ReasonCode.STALE_DATA,)
    for status in (SessionStatus.CLOSED, SessionStatus.PRE_OPEN, SessionStatus.HOLIDAY,
                   SessionStatus.UNKNOWN):
        assert evaluate(strategy_id, bars, session=status).reason_codes == (ReasonCode.NOT_ELIGIBLE,)


@pytest.mark.parametrize("strategy_id", NEW_IDS)
def test_a_cover_needs_no_entry_eligibility_and_no_market_state(strategy_id: str) -> None:
    bars, _ = case(strategy_id)
    for state in ({}, {"eligibility": "WATCHING_BUY"}, {"eligibility": "WATCHING_SHORT"},
                  {"eligibility": "COOLDOWN", "market_state": "CRASH"}):
        assert evaluate(strategy_id, bars, state=dict(state)).is_signal


@pytest.mark.parametrize("strategy_id", NEW_IDS)
def test_the_same_completed_bar_never_signals_twice(strategy_id: str) -> None:
    bars, _ = case(strategy_id)
    first = evaluate(strategy_id, bars)
    replay = evaluate(strategy_id, bars, state=dict(first.state_updates))
    assert first.is_signal and not replay.is_signal
    assert replay.reason_codes == (ReasonCode.NO_SIGNAL,)
    assert replay.state_updates.get(STATE_LAST_SIGNAL_BAR) in (None, bars[-1].start.isoformat())


@pytest.mark.parametrize("strategy_id", NEW_IDS)
def test_no_look_ahead_the_signal_bar_is_the_newest_completed_bar(strategy_id: str) -> None:
    bars, _ = case(strategy_id)
    assert not evaluate(strategy_id, bars[:-1]).is_signal  # the trigger bar is not yet known
    triggered = evaluate(strategy_id, bars)
    assert triggered.bar_time == bars[-1].start  # never an earlier or later bar


@pytest.mark.parametrize("strategy_id", NEW_IDS)
def test_a_missing_bar_between_the_crossing_pair_is_not_a_crossing(strategy_id: str) -> None:
    bars, _ = case(strategy_id)
    gapped = bars[:-1]
    last = bars[-1]
    skipped = Candle(last.symbol, last.timeframe, last.start + timedelta(minutes=1),
                     last.end + timedelta(minutes=1), last.open, last.high, last.low, last.close,
                     last.volume, last.feed_id, last.consolidated, last.origin, last.price_basis,
                     last.status)
    result = evaluate(strategy_id, gapped + [skipped])
    assert result.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


# ------------------------------------------------------------------------ edge conventions


def closes_signal(strategy_id: str, closes: list[object]) -> bool:
    return evaluate(strategy_id, series(closes)).is_signal


def test_adjacent_bar_crossings_use_prior_at_or_below_and_latest_strictly_above() -> None:
    # SMA(2)/SMA(4): flat 10s then 12 -> fast 11 > slow 10.5; a latest equal to the slow average
    # (fast == slow) is not a cross
    assert closes_signal("cover_sma_bullish_crossover", [10, 10, 10, 10, 12])
    assert not closes_signal("cover_sma_bullish_crossover", [10, 10, 10, 10, 10])
    # already above on the prior bar: a level, not an edge
    assert not closes_signal("cover_sma_bullish_crossover", [10, 10, 10, 10, 12, 12])
    # MACD zero line: flat MACD == 0 on the prior bar counts as "at or below"
    assert closes_signal("cover_macd_zero_line_crossover", [10, 10, 10, 10, 11])
    assert not closes_signal("cover_macd_zero_line_crossover", [10, 10, 10, 10, 10])
    assert not closes_signal("cover_macd_zero_line_crossover", [10, 10, 10, 10, 11, 12])


def test_rsi_oversold_reversal_is_the_recovery_of_the_oversold_level_not_an_overbought_exit() -> None:
    falling = [30, 29, 28, 27]
    assert closes_signal("cover_rsi_oversold_reversal", falling + [28])
    assert not closes_signal("cover_rsi_oversold_reversal", falling + [26])
    # RSI never oversold (rises throughout and reaches overbought): no oversold recovery
    assert not closes_signal("cover_rsi_oversold_reversal", [10, 11, 12, 13, 14, 15])
    # the oversold level is a parameter: RSI(3) after 27 -> 28 is above 1 but the prior RSI
    # (0) is at or below it either way; a level of 1 still gives a signal, bounds are enforced
    assert evaluate("cover_rsi_oversold_reversal", series(falling + [28]),
                    {"rsi_period": 3, "oversold": D(1)}).is_signal
    # RSI prior above a lowered oversold level cannot cross it from below
    mixed = [10, 12, 11, 13, 12]
    assert not evaluate("cover_rsi_oversold_reversal", series(mixed),
                        {"rsi_period": 3, "oversold": D(1)}).is_signal


def test_rsi_centerline_requires_a_cross_of_exactly_fifty() -> None:
    assert closes_signal("cover_rsi_centerline_crossover", [30, 29, 28, 27, 30])
    assert not closes_signal("cover_rsi_centerline_crossover", [30, 29, 28, 27, 26])
    # flat bars give RSI exactly 50 (at-or-below the centerline); the first uptick crosses it
    assert closes_signal("cover_rsi_centerline_crossover", [10, 10, 10, 10, 11])
    assert not closes_signal("cover_rsi_centerline_crossover", [10, 10, 10, 10, 10])


def test_aroon_requires_a_strictly_bearish_prior_bar() -> None:
    # Down above Up, then Up above Down
    assert closes_signal("cover_aroon_bullish_reversal", [30, 29, 28, 27, 29])
    # Up > Down already on the prior bar: no edge
    assert not closes_signal("cover_aroon_bullish_reversal", [10, 11, 12, 13, 14, 15])
    # a tie on the prior bar (flat highs and lows tie at 100 / 100) is neutral, not bearish, and
    # does not qualify (unlike the aroon_trend BUY, which accepts Up <= Down)
    flat = series([10, 10, 10, 10])
    ties = flat + [candle(4, 11, 11.5, 10.5, 11)]
    assert not evaluate("cover_aroon_bullish_reversal", ties).is_signal
    assert evaluate("aroon_trend", ties, {"period": 3}, state={"eligibility": "WATCHING_BUY"}).is_signal


def test_parabolic_sar_signals_only_on_the_downtrend_to_uptrend_bar() -> None:
    falling = [30, 29, 28]
    assert closes_signal("cover_parabolic_sar_bullish_reversal", falling + [30])
    assert not closes_signal("cover_parabolic_sar_bullish_reversal", falling + [27])
    # the bar after the flip stays an uptrend: no second signal
    assert not closes_signal("cover_parabolic_sar_bullish_reversal", falling + [30, 31])


def test_bollinger_requires_a_completed_close_below_the_lower_band_first() -> None:
    assert closes_signal("cover_bollinger_lower_band_reversion", [30, 29, 28, 27, 28])
    # never below the lower band: a recovering close alone is not enough
    assert not closes_signal("cover_bollinger_lower_band_reversion", [27, 28, 29, 30, 31])
    # a close above the *upper* band is a breakout, not a lower-band reversion
    assert not closes_signal("cover_bollinger_lower_band_reversion", [10, 10, 10, 10, 20])
    # still below the lower band after the dip: no re-entry yet
    assert not closes_signal("cover_bollinger_lower_band_reversion", [30, 29, 28, 27, 26])
    # the band uses the configured deviations: a wide band keeps a shallow dip outside it
    wide = {"period": 4, "deviations": D(5)}
    assert not evaluate("cover_bollinger_lower_band_reversion", series([30, 29, 28, 27, 28]),
                        wide).is_signal


def test_keltner_requires_a_close_below_the_lower_channel_first() -> None:
    assert closes_signal("cover_keltner_channel_reversion", [30, 29, 28, 27, 28])
    assert not closes_signal("cover_keltner_channel_reversion", [27, 28, 29, 30, 31])
    assert not closes_signal("cover_keltner_channel_reversion", [30, 29, 28, 27, 26])


def test_cci_and_williams_recover_from_their_oversold_levels() -> None:
    assert closes_signal("cover_cci_oversold_reversal", [30, 29, 28, 27, 28])
    assert not closes_signal("cover_cci_oversold_reversal", [27, 28, 29, 30, 31])
    assert closes_signal("cover_williams_r_bullish_reversal", [30, 29, 28, 29])
    assert not closes_signal("cover_williams_r_bullish_reversal", [28, 29, 30, 31])
    assert not closes_signal("cover_williams_r_bullish_reversal", [30, 29, 28, 27])
    # a flat range leaves %R / CCI undefined: no signal rather than a division error
    for strategy_id in ("cover_cci_oversold_reversal", "cover_williams_r_bullish_reversal",
                        "cover_stochastic_bullish_crossover"):
        flat = [candle(i, 10, 10, 10, 10) for i in range(8)]
        assert evaluate(strategy_id, flat).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


def test_stochastic_requires_k_to_cross_above_d() -> None:
    assert closes_signal("cover_stochastic_bullish_crossover", [30, 29, 28, 27, 28])
    assert not closes_signal("cover_stochastic_bullish_crossover", [30, 29, 28, 27, 26])
    # K already above D on the prior bar: no edge
    assert not closes_signal("cover_stochastic_bullish_crossover", [30, 29, 28, 27, 28, 29])


def test_chaikin_money_flow_recovery_trigger_is_non_positive_to_positive() -> None:
    sid = "cover_chaikin_money_flow_recovery"
    head = [volume_bar(i, 10, "low") for i in range(3)]
    assert evaluate(sid, head + [volume_bar(3, 10, "high", 500)]).is_signal
    # still negative after the new bar
    assert not evaluate(sid, head + [volume_bar(3, 10, "high", 100)]).is_signal
    # already positive on the prior bar: no edge
    positive = [volume_bar(i, 10, "high") for i in range(4)]
    assert not evaluate(sid, positive).is_signal
    # an exact-zero prior window (zero-range bars) counts as the non-positive side
    zero = [candle(i, 10, 10, 10, 10) for i in range(3)]
    assert evaluate(sid, zero + [volume_bar(3, 10, "high", 100)]).is_signal
    # zero volume across the window leaves CMF undefined
    dead = [volume_bar(i, 10, "low", 0) for i in range(4)]
    assert evaluate(sid, dead).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


# ----------------------------------------------------------------------------- VWAP reclaim


def test_vwap_reclaim_is_a_strict_cross_from_below_to_above_session_vwap() -> None:
    sid = "cover_vwap_reclaim"
    vwap = {"vwap": D(10), "vwap_prev": D(10)}

    def go(prior: object, latest: object, **ind: object) -> object:
        return evaluate(sid, series([10, prior, latest]), indicators={**vwap, **ind})

    assert go("9.8", "10.2").is_signal
    assert not go("9.8", "10").is_signal  # latest exactly on VWAP is not above it
    assert not go("10", "10.2").is_signal  # prior exactly on VWAP is not below it (BUY: <=)
    assert not go("10.1", "10.2").is_signal  # already above: no edge
    assert not go("9.8", "9.9").is_signal
    # each bar is compared with its own VWAP reading
    assert go("9.8", "10.2", vwap_prev=D("9.9"), vwap=D("10.1")).is_signal
    assert not go("9.8", "10.2", vwap_prev=D("9.7")).is_signal


def test_vwap_reclaim_fails_closed_on_missing_or_invalid_observations() -> None:
    sid = "cover_vwap_reclaim"
    bars = series([10, "9.8", "10.2"])
    insufficient = (ReasonCode.INSUFFICIENT_DATA,)
    for indicators in ({}, {"vwap": D(10)}, {"vwap": None, "vwap_prev": D(10)},
                       {"vwap": D(0), "vwap_prev": D(10)}, {"vwap": D(10), "vwap_prev": D("-1")}):
        assert evaluate(sid, bars, indicators=indicators).reason_codes == insufficient
    assert evaluate(sid, bars[-1:]).reason_codes == insufficient
    # a missing minute between the pair
    assert evaluate(sid, [bars[0], candle(2, "9.8", "10.3", "9.3", "9.8"),
                          candle(4, 10, "10.5", "9.5", "10.2")]).reason_codes == insufficient
    # a zero-volume latest bar carries no VWAP information
    zero = Candle("SPY", CandleTimeframe.M1, OPEN + timedelta(minutes=2),
                  OPEN + timedelta(minutes=3), D("10.2"), D("10.3"), D("10.1"), D("10.2"), D(0),
                  "sip", True, CandleOrigin.REPLAY, PriceBasis.RAW, CandleStatus.CLOSED)
    assert evaluate(sid, bars[:2] + [zero]).reason_codes == insufficient


def test_vwap_reclaim_never_pairs_a_bar_with_a_prior_session() -> None:
    sid = "cover_vwap_reclaim"
    yesterday = candle(-1000, "9.8", "10.3", "9.3", "9.8")  # the previous New York session
    today_first = candle(0, 10, "10.5", "9.5", "10.2")
    assert evaluate(sid, [yesterday, today_first]).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)
    result = evaluate(sid, [yesterday, today_first, candle(1, "9.8", "10.3", "9.3", "9.8"),
                            candle(2, "9.9", "10.9", "9.4", "10.5")])
    assert result.is_signal
    assert result.state_updates["session_id"] == "2026-10-08"


# -------------------------------------------------------------------------------- parameters

INVALID: tuple[tuple[str, dict[str, object]], ...] = (
    ("cover_sma_bullish_crossover", {"fast_period": 1}),
    ("cover_sma_bullish_crossover", {"fast_period": 201}),
    ("cover_sma_bullish_crossover", {"slow_period": 401}),
    ("cover_sma_bullish_crossover", {"fast_period": 60, "slow_period": 50}),
    ("cover_macd_bullish_crossover", {"fast_period": 26, "slow_period": 26}),
    ("cover_macd_bullish_crossover", {"signal_period": 1}),
    ("cover_macd_zero_line_crossover", {"fast_period": 30}),
    ("cover_macd_zero_line_crossover", {"slow_period": 201}),
    ("cover_rsi_oversold_reversal", {"rsi_period": 1}),
    ("cover_rsi_oversold_reversal", {"oversold": D(0)}),
    ("cover_rsi_oversold_reversal", {"oversold": D(51)}),
    ("cover_rsi_centerline_crossover", {"rsi_period": 101}),
    ("cover_dmi_bullish_crossover", {"period": 1}),
    ("cover_aroon_bullish_reversal", {"period": 1}),
    ("cover_aroon_bullish_reversal", {"period": 201}),
    ("cover_parabolic_sar_bullish_reversal", {"step": D(0)}),
    ("cover_parabolic_sar_bullish_reversal", {"step": D("0.6")}),
    ("cover_parabolic_sar_bullish_reversal", {"step": D("0.3"), "maximum": D("0.1")}),
    ("cover_bollinger_lower_band_reversion", {"period": 1}),
    ("cover_bollinger_lower_band_reversion", {"deviations": D(0)}),
    ("cover_bollinger_lower_band_reversion", {"deviations": D(6)}),
    ("cover_keltner_channel_reversion", {"ema_period": 1}),
    ("cover_keltner_channel_reversion", {"atr_period": 0}),
    ("cover_keltner_channel_reversion", {"multiplier": D(0)}),
    ("cover_cci_oversold_reversal", {"period": 1}),
    ("cover_cci_oversold_reversal", {"period": 51}),
    ("cover_cci_oversold_reversal", {"oversold": D(-49)}),
    ("cover_cci_oversold_reversal", {"oversold": D(-301)}),
    ("cover_stochastic_bullish_crossover", {"k_period": 1}),
    ("cover_stochastic_bullish_crossover", {"k_smooth": 0}),
    ("cover_stochastic_bullish_crossover", {"d_period": 21}),
    ("cover_williams_r_bullish_reversal", {"period": 1}),
    ("cover_williams_r_bullish_reversal", {"oversold": D(-49)}),
    ("cover_williams_r_bullish_reversal", {"oversold": D(-101)}),
    ("cover_chaikin_money_flow_recovery", {"period": 1}),
    ("cover_chaikin_money_flow_recovery", {"period": 51}),
    ("cover_vwap_reclaim", {"anything": 1}),
)


@pytest.mark.parametrize("strategy_id,params", INVALID)
def test_invalid_parameters_are_rejected_before_any_evaluation(
    strategy_id: str, params: dict[str, object]
) -> None:
    bars, _ = case(strategy_id)
    with pytest.raises(ParameterValidationError):
        evaluate(strategy_id, bars, params)


@pytest.mark.parametrize("strategy_id", NEW_IDS)
def test_unknown_and_float_parameters_are_rejected_and_defaults_validate(strategy_id: str) -> None:
    schema = REGISTRY.get(strategy_id).spec.parameter_schema
    assert schema.validate({}) is not None  # every default is within its own bounds
    with pytest.raises(ParameterValidationError):
        REGISTRY.validate_parameters(strategy_id, {"unknown_parameter": 1})
    for spec in schema.parameters:
        with pytest.raises(ParameterValidationError):
            schema.validate({spec.name: 2.5})
        assert spec.description  # explicitly documented, configurable


# ------------------------------------------------- recursive state continuity and restart

def walk(n: int, seed: int) -> list[Candle]:
    rng = random.Random(seed)
    price = 100.0
    previous = D("100.00")
    bars = []
    for i in range(n):
        price += math.sin(i / 5) * 0.8 + rng.uniform(-0.6, 0.6)
        close = D(f"{price:.2f}")
        bars.append(candle(i, previous, max(previous, close) + D("0.3"),
                           min(previous, close) - D("0.3"), close))
        previous = close
    return bars


def signal_bars_stateless(strategy_id: str, bars: list[Candle], params: dict[str, object]) -> list[int]:
    """Every index at which the full history from bar 0 signals, with no persisted state."""
    return [i for i in range(len(bars))
            if evaluate(strategy_id, bars[: i + 1], params, {}).is_signal]


def signal_bars_resumed(strategy_id: str, bars: list[Candle], params: dict[str, object], window: int,
                        ineligible: set[int] = frozenset(), restart: bool = False) -> list[int]:
    """Bounded sliding window + persisted state (optionally JSON round-tripped each bar)."""
    state: dict[str, object] = {}
    signals = []
    for i in range(len(bars)):
        view = bars[max(0, i + 1 - window): i + 1]
        kw = {"stale": True} if i in ineligible else {}
        result = evaluate(strategy_id, view, params, state, **kw)
        state = {**state, **result.state_updates}
        if restart:
            state = json.loads(json.dumps(state))
        if result.is_signal:
            signals.append(i)
    return signals


@pytest.mark.parametrize("strategy_id", RECURSIVE)
def test_recursive_indicator_state_makes_a_bounded_window_equal_the_full_history(
    strategy_id: str,
) -> None:
    params = PARAMS[strategy_id]
    bars = walk(160, seed=11)
    need = REGISTRY.get(strategy_id).required_bars(params)
    expected = signal_bars_stateless(strategy_id, bars, params)
    assert expected  # the series exercises the trigger
    window = need + 2
    assert signal_bars_resumed(strategy_id, bars, params, window) == expected


@pytest.mark.parametrize("strategy_id", RECURSIVE)
def test_snapshot_restore_through_json_equals_uninterrupted_processing(strategy_id: str) -> None:
    params = PARAMS[strategy_id]
    bars = walk(160, seed=23)
    need = REGISTRY.get(strategy_id).required_bars(params)
    expected = signal_bars_stateless(strategy_id, bars, params)
    assert signal_bars_resumed(strategy_id, bars, params, need + 2, restart=True) == expected


@pytest.mark.parametrize("strategy_id", RECURSIVE)
def test_indicator_state_keeps_advancing_while_the_cover_is_ineligible(strategy_id: str) -> None:
    params = PARAMS[strategy_id]
    bars = walk(160, seed=5)
    need = REGISTRY.get(strategy_id).required_bars(params)
    expected = signal_bars_stateless(strategy_id, bars, params)
    # stale/ineligible stretches still advance the persisted calculator (advance-only)
    ineligible = {i for i in range(len(bars)) if i % 4 in (1, 2)}
    resumed = signal_bars_resumed(strategy_id, bars, params, need + 2, ineligible)
    assert resumed == [i for i in expected if i not in ineligible]
    blocked = evaluate(strategy_id, bars[: need + 2], params, {}, stale=True)
    assert blocked.reason_codes == (ReasonCode.STALE_DATA,)
    assert STATE_INDICATOR in blocked.state_updates


@pytest.mark.parametrize("strategy_id", RECURSIVE)
def test_corrupt_foreign_or_missing_state_is_reseeded_from_history(strategy_id: str) -> None:
    bars, _ = case(strategy_id)
    clean = evaluate(strategy_id, bars)
    good = clean.state_updates[STATE_INDICATOR]
    assert isinstance(good, dict)
    for junk in ({STATE_INDICATOR: {"junk": 1}}, {STATE_INDICATOR: "text"},
                 {STATE_INDICATOR: {**good, "signature": "another|strategy|1"}},
                 {STATE_INDICATOR: {**good, "calc": {"period": "x"}, "last_bar": bars[0].start.isoformat()}}):
        assert evaluate(strategy_id, bars, state=junk).is_signal


@pytest.mark.parametrize("strategy_id", RECURSIVE)
def test_a_parameter_change_invalidates_the_persisted_state(strategy_id: str) -> None:
    bars = walk(60, seed=2)
    first = evaluate(strategy_id, bars)
    other = dict(PARAMS[strategy_id] or {"step": D("0.03")})
    key = next(iter(other))
    other[key] = other[key] + (D("0.01") if isinstance(other[key], D) else 1)  # type: ignore[operator]
    changed = evaluate(strategy_id, bars, other, dict(first.state_updates))
    assert changed.state_updates[STATE_INDICATOR]["signature"] != first.state_updates[STATE_INDICATOR]["signature"]  # type: ignore[index]


@pytest.mark.parametrize("strategy_id", BAR_ONLY)
def test_signals_are_deterministic_for_identical_history(strategy_id: str) -> None:
    bars = walk(120, seed=3)
    params = PARAMS[strategy_id]
    first = [evaluate(strategy_id, bars[: i + 1], params).reason_codes for i in range(len(bars))]
    again = [evaluate(strategy_id, bars[: i + 1], params).reason_codes for i in range(len(bars))]
    assert first == again


@pytest.mark.parametrize("strategy_id", BAR_ONLY)
def test_signals_do_not_repeat_on_consecutive_bars(strategy_id: str) -> None:
    bars = walk(200, seed=17)
    params = PARAMS[strategy_id]
    signals = signal_bars_resumed(strategy_id, bars, params, 40)
    assert signals, "the walk should produce at least one bullish trigger"
    assert all(b - a > 1 for a, b in zip(signals, signals[1:], strict=False))


# ------------------------------------------------------------- BACKTEST and SHADOW parity


def engine_bars(n: int = 300) -> tuple[list[tuple[str, str, str, str]], list[Candle]]:
    """The same synthetic sine-wave tape as engine rows and as strategy-test candles."""
    rows, candles = [], []
    previous = D("100.00")
    for i in range(n):
        close = D(f"{100 + 4 * math.sin(2 * math.pi * i / 40) + 0.7 * math.sin(i / 3):.2f}")
        high, low = max(previous, close) + D("0.30"), min(previous, close) - D("0.30")
        rows.append((str(previous), str(high), str(low), str(close)))
        candles.append(candle(i, previous, high, low, close))
        previous = close
    # the engine feed carries 100000 shares a bar
    candles = [Candle(c.symbol, c.timeframe, c.start, c.end, c.open, c.high, c.low, c.close,
                      D(100000), c.feed_id, c.consolidated, c.origin, c.price_basis, c.status)
               for c in candles]
    return rows, candles


def direct_signal_indexes(strategy_id: str, params: dict[str, object], candles: list[Candle],
                          window: int = 50) -> set[int]:
    """Closed-bar evaluation with persisted state and the engine's 50-bar context window."""
    state: dict[str, object] = {}
    found = set()
    for i in range(len(candles)):
        result = evaluate(strategy_id, candles[max(0, i + 1 - window): i + 1], params, state)
        state = {**state, **result.state_updates}
        if result.is_signal:
            found.add(i)
    return found


SHORT_PARAMS = {"fast_period": 3, "slow_period": 8}


@pytest.mark.parametrize("strategy_id", BAR_ONLY)
def test_backtest_covers_only_on_bars_the_strategy_signals_and_never_buys(
    strategy_id: str, tmp_path
) -> None:
    rows, candles = engine_bars()
    params = PARAMS[strategy_id]
    result = backtest_run(
        rows, ("short_ema_bearish_crossover", strategy_id),
        {"short_ema_bearish_crossover": SHORT_PARAMS, strategy_id: params},
        config={**SHORT_ON, "minimum_hold_seconds": 60}, registry=REGISTRY, result_dir=tmp_path)
    order = [fill["side"] for fill in result.fills]
    assert set(order) <= {"SELL_SHORT", "BUY_TO_COVER"} and "BUY" not in order
    assert order[0::2] == ["SELL_SHORT"] * len(order[0::2])
    assert order[1::2] == ["BUY_TO_COVER"] * len(order[1::2])  # alternating, full-position
    covers = [row for row in result.intents if row["side"] == "BUY_TO_COVER"]
    assert covers  # the tape produces real covers, so the subset check below is not vacuous
    direct = direct_signal_indexes(strategy_id, params, candles)
    cover_bars = {int((row["signal_bar_time"] - ENGINE_OPEN).total_seconds() // 60) for row in covers}
    assert cover_bars <= direct, (strategy_id, sorted(cover_bars - direct))
    assert direct  # the tape triggers the strategy at least once
    for fill_pair in zip(result.fills[0::2], result.fills[1::2], strict=False):
        assert fill_pair[0]["quantity"] == fill_pair[1]["quantity"]


@pytest.mark.parametrize("strategy_id", BAR_ONLY)
def test_backtest_never_covers_without_a_short(strategy_id: str, tmp_path) -> None:
    rows, _ = engine_bars()
    result = backtest_run(
        rows, (strategy_id,), {strategy_id: PARAMS[strategy_id]}, config=SHORT_ON,
        registry=REGISTRY, result_dir=tmp_path)
    assert result.fills == () and [i for i in result.intents if i["side"] == "BUY_TO_COVER"] == []


async def test_shadow_acts_on_a_representative_new_cover(tmp_path) -> None:
    from test_short_cover_shadow import Clock, _history_context, _wave_closes, start, symbol

    from mike_trade.domain.trading import OrderSide

    clock = Clock()
    cover_id = "cover_macd_zero_line_crossover"
    runtime, shadow = await start(tmp_path, clock, symbol(
        shorts=[("short_ema_bearish_crossover", SHORT_PARAMS)],
        covers=[(cover_id, PARAMS[cover_id])]), name="a.sqlite3", real=True)
    closes = _wave_closes()
    try:
        for index in range(len(closes)):
            clock.value += timedelta(minutes=1)
            await runtime.lifecycle.process({"SPY": _history_context(closes, index, clock.value)})
        sides = [f.side for f in sorted(shadow.account.state.fills.values(),
                                        key=lambda f: f.filled_at)]
    finally:
        await runtime.stop()
    assert OrderSide.BUY not in sides and OrderSide.SELL not in sides
    assert sides and set(sides) == {OrderSide.SELL_SHORT, OrderSide.BUY_TO_COVER}
    assert sides[0::2] == [OrderSide.SELL_SHORT] * len(sides[0::2])
    assert sides[1::2] == [OrderSide.BUY_TO_COVER] * len(sides[1::2])


def test_the_engine_open_matches_the_regular_session_open() -> None:
    assert ENGINE_OPEN == datetime(2025, 3, 10, 9, 30, tzinfo=ZoneInfo("America/New_York")).astimezone(UTC)
