"""Independent review tests for BUY/SELL backlog batches C and D (182-strategy library).

Expected values come from small independent oracles written in this file (plain Decimal
arithmetic over the raw bars), never from the calculators the strategies use.
"""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from mike_trade import app
from mike_trade.backtest.clock import ReplayClock
from mike_trade.backtest.execution import ExecutionAssumptions, HistoricalExecutionSimulator
from mike_trade.backtest.feed import DataProvenance, HistoricalReplayFeed, ReplayEvent
from mike_trade.backtest.manifest import PeriodRole
from mike_trade.backtest.runner import BacktestRequest, BacktestRunner
from mike_trade.domain import market as domain_market
from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.domain.trading import (
    OrderIntent,
    OrderPurpose,
    OrderSide,
    OrderType,
    TimeInForce,
)
from mike_trade.market_data.candles import (
    Candle,
    CandleOrigin,
    CandleStatus,
    CandleTimeframe,
    PriceBasis,
)
from mike_trade.market_data.context import ContextFreshness, MarketContext
from mike_trade.research.commands import _research_registry
from mike_trade.runtime import ExecutionMode
from mike_trade.scheduling.calendar import SessionStatus
from mike_trade.strategies.base import ReasonCode
from mike_trade.strategies.registry import StrategyRegistryError

D = Decimal
NY = ZoneInfo("America/New_York")
OPEN = datetime(2026, 10, 8, 9, 30, tzinfo=NY).astimezone(UTC)
REGISTRY = _research_registry()
ARMED = {"eligibility": "WATCHING_BUY"}
BUYS = (
    "anchored_vwap_breakout",
    "vwap_rejection",
    "first_pullback_buy",
    "relative_volume_momentum",
    "moving_average_compression",
)
SELLS = ("high_volume_reversal",)
EQUIVALENT_CANDIDATES = (
    "vwap_mean_reversion",
    "vwap_trend_continuation",
    "morning_momentum_continuation",
    "moving_average_mean_reversion",
)


def bar(i: int, close: object, *, open_: object | None = None, high: object | None = None,
        low: object | None = None, volume: object = 100, base: datetime = OPEN) -> Candle:
    price = D(str(close))
    opening = price if open_ is None else D(str(open_))
    start = base + timedelta(minutes=i)
    return Candle(
        "SPY", CandleTimeframe.M1, start, start + timedelta(minutes=1),
        opening,
        max(price, opening) + D("0.5") if high is None else D(str(high)),
        min(price, opening) - D("0.5") if low is None else D(str(low)),
        price, D(str(volume)), "sip", True, CandleOrigin.REPLAY, PriceBasis.RAW, CandleStatus.CLOSED,
    )


def vwaps(bars: list[Candle]) -> list[Decimal]:
    weighted = volume = D(0)
    out = []
    for b in bars:
        weighted += (b.high + b.low + b.close) / 3 * b.volume
        volume += b.volume
        out.append(weighted / volume)
    return out


def ctx(bars: list[Candle], *, vwap: bool = False) -> MarketContext:
    values = vwaps(bars) if vwap and len(bars) > 1 else None
    return MarketContext(
        symbol="SPY", as_of=bars[-1].end + timedelta(seconds=1), version=len(bars), feed_id="sip",
        consolidated=True, price_basis=PriceBasis.RAW, session_status=SessionStatus.REGULAR,
        session_id=bars[-1].start.astimezone(NY).date().isoformat(),
        freshness=ContextFreshness(bars[-1].end, timedelta(seconds=1), timedelta(minutes=5),
                                   False, None),
        candles={CandleTimeframe.M1: tuple(bars)},
        indicators={"vwap": values[-1], "vwap_prev": values[-2]} if values else {},
    )


def evaluate(sid: str, bars: list[Candle], params: dict | None = None, state: dict | None = None):
    sell = sid in SELLS
    return REGISTRY.get(sid)().evaluate(
        ctx(bars, vwap=sid == "vwap_rejection"), params or {},
        ({} if sell else dict(ARMED)) if state is None else state)


def walk_signals(sid: str, bars: list[Candle], params: dict | None = None):
    """Per-bar signal flags from (a) a continuously fed engine state and (b) a cold restart."""
    strategy = REGISTRY.get(sid)()
    base = {} if sid in SELLS else dict(ARMED)
    state = dict(base)
    continuous, cold = [], []
    for n in range(1, len(bars) + 1):
        view = bars[:n]
        result = strategy.evaluate(ctx(view, vwap=sid == "vwap_rejection"), params or {}, state)
        state = {**state, **result.state_updates}
        continuous.append(result.is_signal)
        fresh = strategy.evaluate(ctx(view, vwap=sid == "vwap_rejection"), params or {}, dict(base))
        cold.append(fresh.is_signal)
    return continuous, cold


# ------------------------------------------------------------------ registration / composition


def test_final_192_counts_parity_and_unique_ids() -> None:
    counts = {role: len(REGISTRY.list_strategies(role)) for role in StrategyRole}
    assert counts == {StrategyRole.BUY: 93, StrategyRole.SELL: 43,
                      StrategyRole.SHORT: 28, StrategyRole.COVER: 28}
    ids = [c.spec.strategy_id for c in REGISTRY.list_strategies()]
    assert len(ids) == len(set(ids)) == 192
    for sid in BUYS + SELLS:
        assert REGISTRY.get(sid).spec.warmup_bars == REGISTRY.get(sid).required_bars({})
    for candidate in EQUIVALENT_CANDIDATES:  # equivalences are not duplicated as strategies
        with pytest.raises(StrategyRegistryError):
            REGISTRY.get(candidate)


def test_six_strategies_compose_in_a_shadow_runtime(tmp_path: Path) -> None:
    settings = app.AppSettings(
        mode=ExecutionMode.SHADOW, db_path=tmp_path / "cd.sqlite3", symbols=("SPY",),
        shadow_scenarios=(app.ShadowScenarioSettings(),),
        symbol_strategies=(app.SymbolStrategySettings(
            "SPY",
            tuple(app.StrategyAssignment(s, s) for s in BUYS),
            sells=tuple(app.StrategyAssignment(s, s) for s in SELLS)),),
    )
    collections = app.build_runtime(settings).lifecycle.collections("SPY")
    assert {b.strategy_id for b in collections.buys} == set(BUYS)
    assert {b.strategy_id for b in collections.sells} == set(SELLS)


# ---------------------------------------------------------------------- anchored VWAP breakout


def avwap_oracle(bars: list[Candle], strength: int = 2) -> list[bool]:
    """Signal flag per bar: first fractal low, VWAP from that bar, close cross after confirmation."""
    out = [False] * len(bars)
    anchor = None
    for c in range(len(bars)):
        p = c - strength
        if anchor is None:
            if p >= strength and bars[p].low < min(
                bars[j].low for j in range(p - strength, p + strength + 1) if j != p
            ):
                anchor = p
            continue

        def vw(k: int) -> Decimal:
            part = bars[anchor:k + 1]
            return sum(((b.high + b.low + b.close) / 3 * b.volume for b in part), D(0)) / sum(
                (b.volume for b in part), D(0))

        out[c] = bars[c - 1].close <= vw(c - 1) and bars[c].close > vw(c)
    return out


def random_walk(seed: int, n: int, *, step: int = 4) -> list[Candle]:
    r = random.Random(seed)
    price, bars = D(100), []
    for i in range(n):
        close = max(D(1), price + D(r.choice([-3, -2, -1, -1, 0, 1, 1, 2, 3, step])) / 10)
        high = max(price, close) + D(r.randint(0, 3)) / 10
        low = min(price, close) - D(r.randint(0, 3)) / 10
        bars.append(bar(i, close, open_=price, high=high, low=low,
                        volume=r.choice([50, 80, 100, 150, 300, 600])))
        price = close
    return bars


def test_anchored_vwap_matches_independent_point_in_time_oracle_with_restarts() -> None:
    total = 0
    for seed in range(40):
        bars = random_walk(seed, 100)
        continuous, cold = walk_signals("anchored_vwap_breakout", bars)
        expected = avwap_oracle(bars)
        assert continuous == cold == expected, seed
        total += sum(expected)
    assert total > 100  # the comparison actually exercises signals


def test_anchored_vwap_never_signals_on_or_before_the_confirmation_bar() -> None:
    bars = [bar(0, 10, high=11, low=10), bar(1, 9, high=10, low=8), bar(2, 10, high=10, low=9),
            bar(3, 9, high=D("9.5"), low=D("8.8")), bar(4, 11, high=D("11.5"), low=9)]
    flags = walk_signals("anchored_vwap_breakout", bars, {"strength": 1})[0]
    # the pivot (bar 1) is only knowable after bar 2 closes; bar 3 closes below the AVWAP,
    # bar 4 crosses it.
    assert flags == [False, False, False, False, True]
    assert flags == avwap_oracle(bars, 1)


def test_anchored_vwap_premarket_bars_cannot_confirm_a_session_pivot() -> None:
    pre = [bar(-2, 10, high=11, low=10), bar(-1, 10, high=11, low=10)]
    # session bar 0 is lower than both pre-market neighbours and both session neighbours; only
    # the pre-market bars would make it a "pivot"
    session = [bar(0, 9, high=10, low=8), bar(1, 10, high=10, low=9), bar(2, 10, high=10, low=9)]
    result = evaluate("anchored_vwap_breakout", pre + session)
    assert result.state_updates["anchor_state"]["anchor_time"] is None
    # a pivot centred on a pre-market bar is never an anchor either
    pre_pivot = [bar(-4, 10, high=11, low=10), bar(-3, 10, high=11, low=10),
                 bar(-2, 9, high=10, low=8), bar(-1, 10, high=11, low=9), bar(0, 10, high=11, low=9),
                 bar(1, 10, high=11, low=9)]
    result = evaluate("anchored_vwap_breakout", pre_pivot)
    assert result.state_updates["anchor_state"]["anchor_time"] is None


def test_anchored_vwap_differs_from_session_vwap_and_resets_per_session() -> None:
    bars = random_walk(3, 40)
    result = evaluate("anchored_vwap_breakout", bars)
    snap = result.state_updates["anchor_state"]
    assert snap["anchor_time"] is not None
    assert snap["calc"]["session_id"] == f"2026-10-08|{snap['anchor_time']}"
    anchor_index = next(i for i, b in enumerate(bars) if b.start.isoformat() == snap["anchor_time"])
    assert anchor_index > 0  # anchored after the session open, so not the plain session VWAP
    assert D(snap["last_vwap"]) != vwaps(bars)[-1]
    # next day: the old snapshot is discarded and the new session needs its own pivot
    next_day = [bar(i, 100, base=OPEN + timedelta(days=1)) for i in range(3)]
    result = REGISTRY.get("anchored_vwap_breakout")().evaluate(
        ctx(next_day), {}, {**ARMED, "anchor_state": snap})
    assert result.state_updates["anchor_state"]["session"] == "2026-10-09"
    assert result.state_updates["anchor_state"]["anchor_time"] is None


# ---------------------------------------------------------------------------- VWAP rejection


def vwap_rejection_oracle(bars: list[Candle]) -> list[bool]:
    values = vwaps(bars)
    out = [False] * len(bars)
    for k in range(2, len(bars)):
        prior, latest = bars[k - 1], bars[k]
        out[k] = (prior.low > values[k - 1] and latest.low < values[k]
                  and latest.low >= values[k] * (1 - D("0.005"))
                  and latest.close > values[k] and latest.close > latest.open)
    return out


def drifting(seed: int, n: int = 80) -> list[Candle]:
    r = random.Random(seed)
    price, bars = D(100), []
    for i in range(n):
        close = price + D(r.choice([-4, -2, -1, 0, 1, 2, 3, 4, 5])) / 100
        bars.append(bar(i, close, open_=price, high=max(price, close) + D(r.randint(0, 4)) / 100,
                        low=min(price, close) - D(r.randint(0, 6)) / 100,
                        volume=r.choice([100, 200, 400])))
        price = close
    return bars


def test_vwap_rejection_matches_oracle_and_is_disjoint_from_vwap_bounce() -> None:
    fired = 0
    for seed in range(40):
        bars = drifting(seed)
        continuous, cold = walk_signals("vwap_rejection", bars)
        expected = vwap_rejection_oracle(bars)
        assert continuous == cold == expected, seed
        bounce, _ = walk_signals("vwap_bounce", bars)
        # prior.low > prior VWAP implies prior.close > VWAP, so a crossing from at/below is impossible
        assert not any(a and b for a, b in zip(expected, bounce, strict=True))
        fired += sum(expected)
    assert fired > 30


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda b: b, True),
        (lambda b: {**b, "low2": D("101.0"), "open2": D("101.1")}, False),  # no pierce below VWAP
        (lambda b: {**b, "low2": D("90")}, False),  # penetration deeper than 0.5%
        (lambda b: {**b, "close2": D("100.9"), "open2": D("101.0")}, False),  # bearish close
        (lambda b: {**b, "low1": D("100.3")}, False),  # prior bar not wholly above VWAP
    ],
)
def test_vwap_rejection_boundaries(mutate, expected) -> None:
    base = {"low1": D("100.8"), "low2": D("100.4"), "close2": D("101.2"), "open2": D("100.8")}
    p = mutate(base)
    bars = [bar(0, 100, open_=100, high=101, low=99),
            bar(1, D("101.5"), open_=101, high=102, low=p["low1"]),
            bar(2, p["close2"], open_=p["open2"], high=D("101.5"), low=p["low2"])]
    assert evaluate("vwap_rejection", bars).is_signal is expected


# --------------------------------------------------------------------------- first pullback buy


def impulse() -> list[Candle]:
    return [
        bar(0, D("10.1"), open_=10, high=D("10.2"), low=D("9.9")),
        bar(1, D("10.3"), open_=D("10.1"), high=D("10.4"), low=10),
        bar(2, D("10.6"), open_=D("10.3"), high=D("10.7"), low=D("10.2"), volume=200),
    ]


def test_first_pullback_signals_once_and_never_consumes_a_second_pullback() -> None:
    bars = impulse() + [
        bar(3, D("10.5"), open_=D("10.6"), high=D("10.65"), low=D("10.4"), volume=50),
        bar(4, D("10.8"), open_=D("10.5"), high=D("10.9"), low=D("10.5")),  # first pullback ends
        # a second, perfectly shaped pullback and breakout must not be labelled "first"
        bar(5, D("11.0"), open_=D("10.8"), high=D("11.1"), low=D("10.8"), volume=150),
        bar(6, D("10.9"), open_=D("11.0"), high=D("11.0"), low=D("10.85"), volume=40),
        bar(7, D("11.2"), open_=D("10.9"), high=D("11.3"), low=D("10.9"), volume=120),
    ]
    continuous, cold = walk_signals("first_pullback_buy", bars)
    assert continuous == cold
    assert [i for i, flag in enumerate(continuous) if flag] == [4]


def test_first_pullback_invalidated_setup_stays_dead_for_the_session() -> None:
    deep = impulse() + [
        # retraces (10.7-10.2)/(10.7-9.9) = 62% > 50%: the one first pullback is invalid
        bar(3, D("10.3"), open_=D("10.6"), high=D("10.6"), low=D("10.2"), volume=50),
        bar(4, D("10.9"), open_=D("10.3"), high=D("11.0"), low=D("10.3"), volume=200),
        bar(5, D("11.1"), open_=D("10.9"), high=D("11.2"), low=D("10.9"), volume=150),
        bar(6, D("11.4"), open_=D("11.1"), high=D("11.5"), low=D("11.0"), volume=300),
        bar(7, D("11.3"), open_=D("11.4"), high=D("11.45"), low=D("11.2"), volume=40),
        bar(8, D("11.6"), open_=D("11.3"), high=D("11.7"), low=D("11.3"), volume=120),
    ]
    continuous, cold = walk_signals("first_pullback_buy", deep)
    assert continuous == cold
    assert not any(continuous)
    heavy = impulse() + [  # pullback volume as large as the impulse average is not "lower volume"
        bar(3, D("10.5"), open_=D("10.6"), high=D("10.65"), low=D("10.4"), volume=150),
        bar(4, D("10.8"), open_=D("10.5"), high=D("10.9"), low=D("10.5")),
    ]
    assert not any(walk_signals("first_pullback_buy", heavy)[0])


def jittered_setup(seed: int) -> list[Candle]:
    """The scripted impulse/pullback/resumption with random noise and a random tail."""
    r = random.Random(seed)
    closes = [D("10.1"), D("10.3"), D("10.6"), D("10.5"), D("10.8"), D("10.7"), D("11.0")]
    volumes = [100, 100, 200, 50, 100, 60, 130]
    bars, previous = [], D(10)
    for i, (close, volume) in enumerate(zip(closes, volumes, strict=True)):
        close += D(r.randint(-4, 4)) / 100
        bars.append(bar(i, close, open_=previous, high=max(previous, close) + D(r.randint(0, 8)) / 100,
                        low=min(previous, close) - D(r.randint(0, 8)) / 100,
                        volume=volume * r.choice([1, 1, 2, 3]) // r.choice([1, 1, 2])))
        previous = close
    for i in range(7, 30):
        close = previous + D(r.randint(-8, 10)) / 100
        bars.append(bar(i, close, open_=previous, high=max(previous, close) + D(r.randint(0, 5)) / 100,
                        low=min(previous, close) - D(r.randint(0, 5)) / 100,
                        volume=r.choice([40, 80, 120, 250])))
        previous = close
    return bars


def test_first_pullback_state_restore_matches_cold_replay() -> None:
    fired = 0
    for seed in range(120):
        bars = jittered_setup(seed) if seed % 2 else random_walk(seed + 1000, 100)
        continuous, cold = walk_signals("first_pullback_buy", bars)
        assert continuous == cold, seed
        assert sum(continuous) <= 1  # one first pullback per session, ever
        fired += sum(continuous)
    assert fired >= 8


def test_first_pullback_premarket_bars_cannot_form_the_impulse() -> None:
    pre = [bar(-2, D("10.1"), open_=10, high=D("10.2"), low=D("9.9")),
           bar(-1, D("10.3"), open_=D("10.1"), high=D("10.4"), low=10)]
    first = bar(0, D("10.6"), open_=D("10.3"), high=D("10.7"), low=D("10.2"), volume=200)
    result = evaluate("first_pullback_buy", pre + [first])
    assert result.state_updates["setup_state"]["phase"] == "SEEK"
    # the same three bars all inside the session do arm the setup
    inside = impulse()
    assert evaluate("first_pullback_buy", inside).state_updates["setup_state"]["phase"] == "ADVANCE"


def test_first_pullback_morning_cutoff_and_missing_bar() -> None:
    prefix = [bar(i, 10) for i in range(90)]
    late = prefix + [bar(i + 90, b.close, open_=b.open, high=b.high, low=b.low, volume=b.volume)
                     for i, b in enumerate(impulse())]
    assert evaluate("first_pullback_buy", late).state_updates["setup_state"]["phase"] == "DONE"
    allowed = evaluate("first_pullback_buy", late, {"entry_end_minute": 180})
    assert allowed.state_updates["setup_state"]["phase"] == "ADVANCE"
    gap = impulse()
    gap[1] = bar(5, D("10.3"), open_=D("10.1"), high=D("10.4"), low=10)  # leaves a hole
    result = evaluate("first_pullback_buy", [gap[0]] + [gap[2]])
    assert result.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


# ------------------------------------------------------------------- relative volume momentum


def rvol_oracle(bars: list[Candle], p: int = 20, th: Decimal = D(2), m: int = 3,
                min_pct: Decimal = D("0.1")) -> list[bool]:
    out = [False] * len(bars)
    for t in range(max(p, m) + 1, len(bars)):
        avg = sum(b.volume for b in bars[t - p:t]) / p
        avg_prior = sum(b.volume for b in bars[t - 1 - p:t - 1]) / p
        if avg <= 0 or avg_prior <= 0:
            continue
        move = (bars[t].close - bars[t - m].close) / bars[t - m].close * 100
        out[t] = (bars[t - 1].volume / avg_prior < th and bars[t].volume / avg >= th
                  and bars[t].close > bars[t].open and bars[t].close > bars[t - 1].close
                  and move >= min_pct)
    return out


def volume_walk(seed: int, n: int = 90) -> list[Candle]:
    r = random.Random(seed)
    price, bars = D(100), []
    for i in range(n):
        close = price + D(r.choice([-2, -1, 0, 1, 2, 3])) / 10
        bars.append(bar(i, close, open_=price, high=max(price, close) + D("0.1"),
                        low=min(price, close) - D("0.1"),
                        volume=r.choice([100, 100, 100, 100, 100, 250, 400])))
        price = close
    return bars


def test_rvol_momentum_matches_rolling_oracle() -> None:
    fired = 0
    for seed in range(40):
        bars = volume_walk(seed)
        continuous, cold = walk_signals("relative_volume_momentum", bars)
        expected = rvol_oracle(bars)
        assert continuous == cold == expected, seed
        fired += sum(expected)
    assert fired > 20


def test_rvol_baseline_excludes_the_current_bar_and_uses_its_own_prior_baseline() -> None:
    bars = [bar(i, 100 + D(i) / 5, open_=100 + D(i) / 5 - D("0.1"), volume=100) for i in range(23)]
    bars.append(bar(23, D("104.8"), open_=D("104.5"), volume=300))
    assert evaluate("relative_volume_momentum", bars).is_signal
    # a second consecutive elevated bar does not repeat the signal (prior RVOL is >= threshold)
    bars.append(bar(24, D("105.0"), open_=D("104.8"), volume=300))
    assert not evaluate("relative_volume_momentum", bars).is_signal
    # a flat bar volume of exactly 2.0x the baseline qualifies (inclusive), 1.99x does not
    edge = bars[:23] + [bar(23, D("104.8"), open_=D("104.5"), volume=199)]
    assert not evaluate("relative_volume_momentum", edge).is_signal
    edge[-1] = bar(23, D("104.8"), open_=D("104.5"), volume=200)
    assert evaluate("relative_volume_momentum", edge).is_signal


def test_rvol_zero_prior_baseline_is_undefined_not_below_threshold() -> None:
    # the baseline of bar t-1 is entirely zero volume, so its RVOL is unbounded, not "0 < 2"
    bars = [bar(i, 100 + D(i) / 5, open_=100 + D(i) / 5 - D("0.1"), volume=0) for i in range(21)]
    bars += [bar(21, D("104.2"), open_=D("104.0"), volume=300),
             bar(22, D("104.4"), open_=D("104.2"), volume=300)]
    assert not evaluate("relative_volume_momentum", bars).is_signal


# ----------------------------------------------------------------- moving average compression


def sma(bars: list[Candle], k: int, end: int) -> Decimal:
    return sum((b.close for b in bars[end - k + 1:end + 1]), D(0)) / k


def compression_oracle(bars: list[Candle]) -> list[bool]:
    def cluster(i: int) -> tuple[Decimal, Decimal]:
        values = [sma(bars, 10, i), sma(bars, 20, i), sma(bars, 50, i)]
        return max(values), (max(values) - min(values)) / values[2] * 100

    out = [False] * len(bars)
    for t in range(54, len(bars)):
        if any(cluster(i)[1] > D("0.75") for i in range(t - 5, t)):
            continue
        out[t] = bars[t - 1].close <= cluster(t - 1)[0] and bars[t].close > cluster(t)[0] \
            and bars[t].close > bars[t].open
    return out


def squeeze_walk(seed: int, n: int = 110) -> list[Candle]:
    r = random.Random(seed)
    price, bars = D(100), []
    for i in range(n):
        drift = D(r.choice([-2, -1, 0, 0, 1, 2])) / 100
        if r.random() < 0.06:
            drift = D(r.choice([-8, 8, 15])) / 10
        close = price + drift
        bars.append(bar(i, close, open_=price))
        price = close
    return bars


def test_ma_compression_matches_independent_sma_oracle() -> None:
    fired = 0
    for seed in range(40):
        bars = squeeze_walk(seed)
        continuous, cold = walk_signals("moving_average_compression", bars)
        expected = compression_oracle(bars)
        assert continuous == cold == expected, seed
        fired += sum(expected)
    assert fired > 3


def test_ma_compression_signals_only_on_the_cross_out_of_a_flat_cluster() -> None:
    flat = [bar(i, 100) for i in range(60)]
    assert evaluate("moving_average_compression", flat + [bar(60, 101, open_=100)]).is_signal
    # ordinary continuing uptrend: MAs fan out and the prior close is already above the cluster
    trend = [bar(i, 100 + D(i) / 4) for i in range(70)]
    assert not any(walk_signals("moving_average_compression", trend)[0])
    # one non-compressed bar among the five preceding ones blocks the signal
    spiky = flat[:57] + [bar(57, 110, open_=100), bar(58, 100), bar(59, 100)]
    assert not evaluate("moving_average_compression", spiky + [bar(60, 101, open_=100)]).is_signal
    # prior close already above the cluster: no re-signal on the next bar
    two = flat + [bar(60, 101, open_=100), bar(61, 101.5, open_=101)]
    assert not evaluate("moving_average_compression", two).is_signal


# --------------------------------------------------------------------- high-volume reversal


def hvr_oracle(bars: list[Candle], trend: int = 5, p: int = 20, mult: Decimal = D(2)) -> list[bool]:
    def candle_event(t: int) -> bool:
        avg = sum(b.volume for b in bars[t - p:t]) / p
        b = bars[t]
        rng = b.high - b.low
        return (avg > 0 and b.volume >= mult * avg and b.close < b.open
                and b.close < bars[t - 1].close and (b.close - b.low) <= rng / 2)

    out = [False] * len(bars)
    for t in range(max(p, trend) + 1, len(bars)):
        out[t] = (bars[t - 1].close > bars[t - 1 - trend].close and candle_event(t)
                  and not candle_event(t - 1))
    return out


def reversal_walk(seed: int, n: int = 90) -> list[Candle]:
    r = random.Random(seed)
    price, bars = D(100), []
    for i in range(n):
        close = price + D(r.choice([-4, -2, 1, 2, 3, 4])) / 10
        high = max(price, close) + D(r.randint(0, 3)) / 10
        low = min(price, close) - D(r.randint(0, 3)) / 10
        bars.append(bar(i, close, open_=price, high=high, low=low,
                        volume=r.choice([100, 100, 100, 100, 250, 400])))
        price = close
    return bars


def test_high_volume_reversal_matches_oracle() -> None:
    fired = 0
    for seed in range(40):
        bars = reversal_walk(seed)
        continuous, cold = walk_signals("high_volume_reversal", bars)
        expected = hvr_oracle(bars)
        assert continuous == cold == expected, seed
        fired += sum(expected)
    assert fired > 10


def test_high_volume_reversal_is_one_signal_per_run_of_heavy_bearish_bars() -> None:
    bars = [bar(i, 100, high=100.5, low=99.5) for i in range(15)]
    bars += [bar(i, 100 + (i - 15), high=100.5 + (i - 15), low=99.5 + (i - 15)) for i in range(15, 21)]
    bars += [bar(21, 103.5, open_=106, high=107, low=103, volume=250),
             bar(22, 102.0, open_=103.4, high=103.5, low=101.5, volume=400),
             bar(23, 100.5, open_=101.9, high=102, low=100, volume=500)]
    flags = walk_signals("high_volume_reversal", bars)[0]
    assert [i for i, f in enumerate(flags) if f] == [21]


# --------------------------------------------------------- regular-session-only history windows


def positive_cases() -> dict[str, list[Candle]]:
    rvol = [bar(i, 100 + D(i) / 5, open_=100 + D(i) / 5 - D("0.1")) for i in range(23)]
    rvol.append(bar(23, D("104.8"), open_=D("104.5"), volume=300))
    comp = [bar(i, 100) for i in range(60)] + [bar(60, 101, open_=100)]
    hvr = [bar(i, 100, high=100.5, low=99.5) for i in range(15)]
    hvr += [bar(i, 100 + (i - 15), high=100.5 + (i - 15), low=99.5 + (i - 15)) for i in range(15, 21)]
    hvr.append(bar(21, 103.5, open_=106, high=107, low=103, volume=250))
    return {"relative_volume_momentum": rvol, "moving_average_compression": comp,
            "high_volume_reversal": hvr}


@pytest.mark.parametrize("sid", sorted(positive_cases()))
def test_batch_d_windows_must_lie_inside_the_regular_session(sid: str) -> None:
    bars = positive_cases()[sid]
    assert evaluate(sid, bars).is_signal
    early = OPEN - timedelta(minutes=len(bars) - 5)  # first bars are pre-market, last 5 regular
    shifted = [bar(i, b.close, open_=b.open, high=b.high, low=b.low, volume=b.volume, base=early)
               for i, b in enumerate(bars)]
    result = evaluate(sid, shifted)
    assert not result.is_signal and result.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)
    overnight = [bar(i, b.close, open_=b.open, high=b.high, low=b.low, volume=b.volume,
                     base=OPEN if i < 10 else OPEN + timedelta(days=1, minutes=-10))
                 for i, b in enumerate(bars)]
    assert not evaluate(sid, overnight).is_signal


# ------------------------------------------------------------------------------- backtest


def run_backtest(bars: list[Candle], ids: tuple[str, ...], params: dict, tmp_path: Path):
    provenance = domain_market.FeedProvenance("replay", "IEX", "synthetic", False)
    events = [ReplayEvent.bar(domain_market.Candle(
        "SPY", domain_market.Timeframe.M1, b.start, b.end, b.open, b.high, b.low, b.close,
        b.volume, provenance)) for b in bars]
    clock = ReplayClock(OPEN)
    feed = HistoricalReplayFeed(events, DataProvenance("synthetic", "IEX", False, "cd-review"),
                                clock=clock, seed=77)
    simulator = HistoricalExecutionSimulator(feed, clock, ExecutionAssumptions())

    def intent(evaluation, market_context, request):  # noqa: ANN001, ARG001
        buying = evaluation.side is StrategyRole.BUY
        return OrderIntent(
            intent_id=f"i-{evaluation.signal_id}", client_order_id=f"c-{evaluation.signal_id}",
            account_scope_id="backtest:test", cycle_id="cycle-SPY", symbol="SPY",
            side=OrderSide.BUY if buying else OrderSide.SELL,
            purpose=OrderPurpose.ENTRY if buying else OrderPurpose.STRATEGY_EXIT,
            order_type=OrderType.MARKET, time_in_force=TimeInForce.DAY,
            created_at=market_context.as_of,
            quantity=D(10) if buying else simulator.held_quantity("SPY"))

    request = BacktestRequest(
        symbols=("SPY",), window_start=OPEN, window_end=OPEN + timedelta(hours=2),
        period_role=PeriodRole.RESEARCH, config={}, seed=77, starting_capital=D(100000),
        strategy_ids=ids, strategy_params=params,
        initial_strategy_state={i: dict(ARMED) for i in ids if i not in SELLS},
        context_history_bars=100, result_directory=tmp_path)
    return asyncio.run(BacktestRunner(REGISTRY, feed, clock, simulator, intent_factory=intent).run(request))


def test_rvol_buy_then_high_volume_reversal_sell_in_backtest(tmp_path: Path) -> None:
    bars = [bar(i, 100 + D(i) / 5, open_=100 + D(i) / 5 - D("0.1")) for i in range(23)]
    bars.append(bar(23, D("104.8"), open_=D("104.5"), volume=300))  # RVOL BUY
    bars += [bar(24, D("105.0"), open_=D("104.8")), bar(25, D("105.2"), open_=D("105.0")),
             bar(26, D("105.4"), open_=D("105.2"))]
    bars.append(bar(27, D("103.6"), open_=D("105.6"), high=D("105.7"), low=D("103.5"), volume=500))
    bars += [bar(28, D("103.6"), volume=100)]
    result = run_backtest(bars, ("relative_volume_momentum", "high_volume_reversal"), {}, tmp_path)
    buys = [r for r in result.signals if r["strategy"] == "relative_volume_momentum"]
    sells = [r for r in result.signals if r["strategy"] == "high_volume_reversal"]
    assert [r["bar_time"] for r in buys] == [bars[23].start]
    assert [r["bar_time"] for r in sells] == [bars[27].start]
    assert {f["side"] for f in result.fills} == {"BUY", "SELL"}
    assert all(r["trigger_bar_end"] <= r["time"] for r in result.signals)


def test_compression_rejection_and_pullback_backtests_match_direct_evaluation(tmp_path: Path) -> None:
    comp = [bar(i, 100) for i in range(60)] + [bar(60, 101, open_=100)]
    result = run_backtest(comp, ("moving_average_compression",), {}, tmp_path / "a")
    rows = [r for r in result.signals if r["strategy"] == "moving_average_compression"]
    assert [r["bar_time"] for r in rows] == [comp[-1].start]

    rejection = [bar(0, 100, open_=100, high=101, low=99),
                 bar(1, D("101.5"), open_=101, high=102, low=D("100.8")),
                 bar(2, D("101.2"), open_=D("100.8"), high=D("101.5"), low=D("100.4"))]
    assert evaluate("vwap_rejection", rejection).is_signal
    result = run_backtest(rejection, ("vwap_rejection",), {}, tmp_path / "b")
    assert [r["bar_time"] for r in result.signals if r["strategy"] == "vwap_rejection"] == [
        rejection[-1].start]
