"""Focused checks for the 5-minute / 15-minute candle-window XGBoost BUY experiments.

Synthetic bars only; no real market data, no network, and no production-size training run.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from mike_trade.modeling.aggregation import (
    AggregationPolicy,
    AggregationStats,
    CandleAggregator,
    aggregate_bars,
)
from mike_trade.modeling.builder import AggregatedTrainingDatasetBuilder
from mike_trade.modeling.candle_features import (
    CandleWindowFeatureConfig,
    CandleWindowFeaturePipeline,
)
from mike_trade.modeling.config import load_experiment_config
from mike_trade.modeling.contracts import ModelBar
from mike_trade.modeling.labels import (
    AggregatedTripleBarrierConfig,
    AggregatedTripleBarrierLabelGenerator,
    ExecutionCostAssumptions,
)
from mike_trade.scheduling.calendar import session_for

MIN = timedelta(minutes=1)
DAY = date(2026, 3, 10)  # a regular 09:30-16:00 ET session
NEXT_DAY = date(2026, 3, 11)
EARLY_CLOSE_DAY = date(2025, 11, 28)  # 09:30-13:00 ET


def minute(
    day: date,
    offset: int,
    o: str,
    h: str | None = None,
    low: str | None = None,
    c: str | None = None,
    *,
    volume: str = "1000",
    vwap: str | None = None,
    symbol: str = "AAA",
) -> ModelBar:
    session = session_for(day)
    start = session.open_utc + offset * MIN
    close = D(c or o)
    return ModelBar(
        symbol, start, start + MIN, D(o), D(h or max(D(o), close)), D(low or min(D(o), close)),
        close, D(volume), "iex", "1m", "raw", session.session_id,
        vwap=D(vwap) if vwap else close,
    )


def flat_minutes(day: date, first: int, count: int, price: str = "100") -> list[ModelBar]:
    return [minute(day, first + i, price) for i in range(count)]


# ------------------------------------------------------------------- 5m / 15m aggregation


def test_five_and_fifteen_minute_candles_align_to_the_session_open_and_aggregate_ohlcv():
    bars = [
        minute(DAY, 0, "100", "100.5", "99.9", "100.2", volume="100", vwap="100.1"),
        minute(DAY, 1, "100.2", "100.9", "100.1", "100.7", volume="300", vwap="100.5"),
        minute(DAY, 2, "100.7", "101.0", "100.6", "100.8", volume="100", vwap="100.9"),
        minute(DAY, 3, "100.8", "100.9", "100.0", "100.1", volume="200", vwap="100.4"),
        minute(DAY, 4, "100.1", "100.3", "99.8", "100.0", volume="300", vwap="100.0"),
    ]
    (five,) = aggregate_bars(bars, AggregationPolicy(5))
    session = session_for(DAY)
    assert (five.start, five.end) == (session.open_utc, session.open_utc + 5 * MIN)
    assert five.timeframe == "5m" and five.session_id == session.session_id
    assert (five.open, five.high, five.low, five.close) == (D("100"), D("101.0"), D("99.8"), D("100.0"))
    assert five.volume == D("1000")
    expected_vwap = (D("100.1") * 100 + D("100.5") * 300 + D("100.9") * 100 + D("100.4") * 200
                     + D("100.0") * 300) / D(1000)
    assert five.vwap == expected_vwap
    assert five.trade_count is None  # not every minute reports trades -> not invented

    day_minutes = flat_minutes(DAY, 0, 390)
    starts5 = [c.start for c in aggregate_bars(day_minutes, AggregationPolicy(5))]
    starts15 = [c.start for c in aggregate_bars(day_minutes, AggregationPolicy(15))]
    assert len(starts5) == 78 and len(starts15) == 26
    assert [s.astimezone(UTC).minute for s in starts5[:4]] == [30, 35, 40, 45]  # 09:30 ET = 13:30Z
    assert [s - session.open_utc for s in starts15[:3]] == [0 * MIN, 15 * MIN, 30 * MIN]
    assert starts15[-1] + 15 * MIN == session.close_utc  # the last candle ends at the close


def test_early_close_sessions_use_the_same_grid_and_never_extend_past_the_close():
    session = session_for(EARLY_CLOSE_DAY)
    assert session.is_early_close
    minutes = [minute(EARLY_CLOSE_DAY, i, "100") for i in range(210)]
    for width, expected in ((5, 42), (15, 14)):
        candles = aggregate_bars(minutes, AggregationPolicy(width))
        assert len(candles) == expected
        assert candles[-1].end == session.close_utc


def test_incomplete_candles_are_not_emitted_until_their_window_has_elapsed():
    policy = AggregationPolicy(5)
    aggregator = CandleAggregator(policy)
    for i in range(4):  # 09:30..09:33 only: the 09:30 candle is still forming
        assert aggregator.update(minute(DAY, i, "100")) == []
    assert aggregator.open_bucket_start == session_for(DAY).open_utc
    assert aggregator.flush(session_for(DAY).open_utc + 4 * MIN) == []  # not elapsed yet
    emitted = aggregator.flush(session_for(DAY).open_utc + 5 * MIN)  # the window ended
    assert len(emitted) == 1 and emitted[0].volume == D("4000")
    # a later minute also completes (and does not contain) the earlier window
    other = CandleAggregator(policy)
    other.update(minute(DAY, 0, "100"))
    done = other.update(minute(DAY, 5, "101"))
    assert [c.start for c in done] == [session_for(DAY).open_utc] and done[0].close == D("100")
    # without a completion signal, a trailing partial bucket never leaks out of the batch helper
    assert aggregate_bars(flat_minutes(DAY, 0, 3), policy) == ()
    assert len(aggregate_bars(flat_minutes(DAY, 0, 3), policy,
                              complete_through=session_for(DAY).open_utc + 5 * MIN)) == 1


def test_missing_minutes_are_never_bridged_and_empty_windows_leave_a_gap():
    policy = AggregationPolicy(5)
    stats = AggregationStats()
    bars = flat_minutes(DAY, 0, 5)  # candle 1: complete
    bars += [minute(DAY, 5, "100"), minute(DAY, 7, "100")]  # candle 2: 3 of 5 minutes exist
    bars += flat_minutes(DAY, 15, 5)  # window 10-14 has no bar at all -> no candle (gap)
    candles = aggregate_bars(bars, policy, stats=stats, complete_through=session_for(DAY).close_utc)
    offsets = [(c.start - session_for(DAY).open_utc) // MIN for c in candles]
    assert offsets == [0, 5, 15]  # nothing invented for 10-14
    assert candles[1].volume == D("2000")  # exactly the minutes that exist
    assert stats.candles == 3 and stats.candles_with_all_minutes == 2
    assert stats.candles_with_missing_minutes == 1
    strict = AggregationStats()
    assert [
        c.start for c in aggregate_bars(
            bars, AggregationPolicy(5, minimum_minutes=5), stats=strict,
            complete_through=session_for(DAY).close_utc,
        )
    ] == [candles[0].start, candles[2].start]
    assert strict.buckets_dropped_below_minimum == 1


def test_candles_never_mix_sessions_or_extended_hours():
    both_days = flat_minutes(DAY, 385, 5) + flat_minutes(NEXT_DAY, 0, 5)
    candles = aggregate_bars(both_days, AggregationPolicy(5))
    assert [c.session_id for c in candles] == [session_for(DAY).session_id,
                                                session_for(NEXT_DAY).session_id]
    assert candles[0].end == session_for(DAY).close_utc
    premarket = ModelBar(
        "AAA", session_for(DAY).open_utc - 10 * MIN, session_for(DAY).open_utc - 9 * MIN,
        D("100"), D("100"), D("100"), D("100"), D("10"), "iex", "1m", "raw",
        session_for(DAY).session_id, vwap=D("100"),
    )
    with pytest.raises(ValueError, match="precedes its session open"):
        CandleAggregator(AggregationPolicy(5)).update(premarket)
    with pytest.raises(ValueError, match="divide"):
        AggregationPolicy(7)


def test_streaming_and_batch_aggregation_are_the_same_computation():
    bars = flat_minutes(DAY, 0, 40) + flat_minutes(DAY, 50, 30, "101") + flat_minutes(NEXT_DAY, 3, 20)
    batch = aggregate_bars(bars, AggregationPolicy(5), complete_through=session_for(NEXT_DAY).close_utc)
    aggregator = CandleAggregator(AggregationPolicy(5))
    streamed: list[ModelBar] = []
    for bar in bars:
        streamed.extend(aggregator.update(bar))
    streamed.extend(aggregator.flush(session_for(NEXT_DAY).close_utc))
    assert streamed == list(batch)


# ------------------------------------------------------------------------- feature windows


def candle_series(count: int, *, day: date = DAY, width: int = 5, first: int = 0) -> list[ModelBar]:
    """Deterministic, varied 5/15-minute candles built through the real aggregator."""
    minutes: list[ModelBar] = []
    price = D("100")
    for i in range(count * width):
        drift = D(((i * 7) % 11) - 5) / D(100)
        close = price + drift
        minutes.append(
            minute(
                day, first * width + i, str(price), str(max(price, close) + D("0.05")),
                str(min(price, close) - D("0.04")), str(close),
                volume=str(1000 + (i * 37) % 500), vwap=str((price + close) / 2),
            )
        )
        price = close
    return list(
        aggregate_bars(minutes, AggregationPolicy(width),
                       complete_through=session_for(day).close_utc)
    )


def pipeline(lookback: int = 10, half_life: float = 3.0, width: int = 5, refs=()):
    return CandleWindowFeaturePipeline(
        CandleWindowFeatureConfig(width, lookback, half_life, reference_symbols=refs)
    )


def test_feature_names_identify_each_candles_position_and_stay_within_the_lookback():
    for config, expected in (
        (CandleWindowFeatureConfig(5, 10, 3.0, reference_symbols=("SPY",)), 83),
        (CandleWindowFeatureConfig(15, 6, 2.0, reference_symbols=("SPY",)), 55),
    ):
        names = config.names()
        assert len(names) == len(set(names)) == expected
        n = config.lookback_bars
        lags = {int(name[3 : name.index("_")]) for name in names if name.startswith("lag")}
        assert lags == set(range(n))  # exactly the configured lookback, nothing older
        assert "lag0_body_pct" in names and f"lag{n - 1}_body_pct" in names
        assert f"lag{n}_body_pct" not in names
        assert f"lag{n - 1}_return" not in names  # needs a candle older than the window
        assert f"lag{n - 2}_acceleration" not in names
        for metric in ("body_pct", "upper_wick", "lower_wick", "range_pct", "relative_volume",
                       "return", "acceleration"):
            assert f"rw_{metric}" in names
        assert config.maximum_history == n


def test_features_need_exactly_the_lookback_and_never_cross_a_session_or_a_gap():
    candles = candle_series(18)
    features = list(pipeline().transform(candles))
    assert len(features) == 18 - 10 + 1
    assert features[0].bar_start == candles[9].start  # first row once 10 candles exist
    # a missing candle resets the window: rows resume only after 10 full candles follow the gap
    gapped = candles[:6] + candles[7:]  # candle 6 absent -> 11 contiguous candles remain
    assert [f.bar_start for f in pipeline().transform(gapped)] == [
        candles[16].start, candles[17].start,
    ]
    # a new session starts a fresh window
    two_days = candle_series(12) + candle_series(12, day=NEXT_DAY)
    starts = [f.bar_start for f in pipeline().transform(two_days)]
    next_open = session_for(NEXT_DAY).open_utc
    assert all(s >= next_open + 9 * 5 * MIN for s in starts if s >= next_open)  # not before 10 candles


def test_most_recent_candle_is_lag0_and_per_candle_values_are_exact():
    candles = candle_series(10)
    vector = next(pipeline().transform(candles))
    latest, previous = candles[-1], candles[-2]
    values = vector.values
    o, h, low, c = (float(latest.open), float(latest.high), float(latest.low), float(latest.close))
    assert values["lag0_body_pct"] == pytest.approx((c - o) / o)
    assert values["lag0_upper_wick"] == pytest.approx((h - max(o, c)) / (h - low))
    assert values["lag0_lower_wick"] == pytest.approx((min(o, c) - low) / (h - low))
    assert values["lag0_range_pct"] == pytest.approx((h - low) / c)
    assert values["lag0_return"] == pytest.approx(c / float(previous.close) - 1)
    oldest = candles[0]
    assert values["lag9_body_pct"] == pytest.approx(
        (float(oldest.close) - float(oldest.open)) / float(oldest.open)
    )
    mean_volume = sum(float(x.volume) for x in candles) / 10
    assert values["lag0_relative_volume"] == pytest.approx(float(latest.volume) / mean_volume)
    assert values["lag0_acceleration"] == pytest.approx(
        values["lag0_return"] - values["lag1_return"]
    )
    assert values["window_return"] == pytest.approx(c / float(oldest.open) - 1)
    assert vector.as_of == latest.end and vector.bar_start == latest.start


def test_recency_weighted_summaries_use_the_configured_exponential_decay():
    candles = candle_series(10)
    for half_life in (3.0, 1.5):
        vector = next(pipeline(half_life=half_life).transform(candles))
        weights = [0.5 ** (lag / half_life) for lag in range(10)]
        body = [vector.values[f"lag{lag}_body_pct"] for lag in range(10)]
        assert vector.values["rw_body_pct"] == pytest.approx(
            sum(w * b for w, b in zip(weights, body, strict=True)) / sum(weights)
        )
        returns = [vector.values[f"lag{lag}_return"] for lag in range(9)]
        assert vector.values["rw_return"] == pytest.approx(
            sum(w * r for w, r in zip(weights[:9], returns, strict=True)) / sum(weights[:9])
        )
    fast = next(pipeline(half_life=1.0).transform(candles)).values["rw_body_pct"]
    slow = next(pipeline(half_life=50.0).transform(candles)).values["rw_body_pct"]
    assert fast != pytest.approx(slow)  # recent candles weigh more under a short half-life
    config = CandleWindowFeatureConfig(5, 10, 3.0)
    weights = config.weights()
    assert weights[0] == 1.0 and weights[3] == pytest.approx(0.5) and weights[-1] < weights[0]
    with pytest.raises(ValueError):
        CandleWindowFeatureConfig(5, 10, 0.0)


def test_features_use_only_the_lookback_and_never_see_future_candles():
    candles = candle_series(20)
    pipe = pipeline()
    full = {f.bar_start: f for f in pipe.transform(candles)}
    # the vector at candle i is identical when only the last 10 candles up to i are supplied
    for index in (9, 12, 19):
        alone = list(pipeline().transform(candles[index - 9 : index + 1]))
        assert len(alone) == 1
        assert dict(alone[0].values) == dict(full[candles[index].start].values)
    # ...and when every FUTURE candle is replaced by garbage
    altered = candles[:13] + [
        ModelBar(c.symbol, c.start, c.end, D("1"), D("500"), D("1"), D("250"), c.volume * 9,
                 c.feed, c.timeframe, c.adjustment, c.session_id, vwap=D("300"))
        for c in candles[13:]
    ]
    changed = {f.bar_start: f for f in pipeline().transform(altered)}
    for candle in candles[9:13]:
        assert dict(changed[candle.start].values) == dict(full[candle.start].values)


def test_streaming_and_batch_features_are_identical_for_aggregated_minutes():
    minutes = []
    price = D("100")
    for i in range(130):
        close = price + D(((i * 5) % 9) - 4) / D(100)
        minutes.append(minute(DAY, i, str(price), str(max(price, close)), str(min(price, close)),
                              str(close), volume=str(900 + i % 40), vwap=str(close)))
        price = close
    aggregator = CandleAggregator(AggregationPolicy(5))
    streaming = pipeline()
    streamed = []
    for bar in minutes:  # what a live process does: minute -> candle -> features
        for candle in aggregator.update(bar):
            vector = streaming.update(candle)
            if vector is not None:
                streamed.append(vector)
    batch = list(pipeline().transform(aggregate_bars(minutes, AggregationPolicy(5))))
    assert [v.bar_start for v in streamed] == [v.bar_start for v in batch] and streamed
    for left, right in zip(streamed, batch, strict=True):
        assert dict(left.values) == dict(right.values)


def test_reference_series_must_share_the_candle_grid():
    config = CandleWindowFeatureConfig(5, 4, 2.0, reference_symbols=("SPY",))
    primary = candle_series(8)
    spy = [
        ModelBar("SPY", c.start, c.end, c.open, c.high, c.low, c.close, c.volume, c.feed,
                 c.timeframe, c.adjustment, c.session_id, vwap=c.vwap)
        for c in primary
    ]
    rows = list(CandleWindowFeaturePipeline(config).transform(primary, references={"SPY": spy}))
    assert len(rows) == 8 - 4 + 1
    assert rows[0].values["relative_strength_spy"] == pytest.approx(0.0)  # identical series
    missing = list(CandleWindowFeaturePipeline(config).transform(primary, references={"SPY": spy[:5]}))
    assert len(missing) == 2  # candles without a same-start reference produce no row


# -------------------------------------------------------------------------------- labels


def label_generator(horizon: int = 4, width: int = 5, up: str = "0.010", down: str = "0.005",
                    costs: ExecutionCostAssumptions | None = None):
    return AggregatedTripleBarrierLabelGenerator(
        AggregatedTripleBarrierConfig(D(up), D(down), horizon, width, costs or ExecutionCostAssumptions())
    )


def path(day: date, candles: list[tuple[str, str, str, str]], *, width: int = 5, first: int = 0,
         inner: dict[int, list[tuple[str, str, str]]] | None = None):
    """Build 1m bars for the given candle OHLCs (+ optional per-minute (o, h, l, c) detail)."""
    minutes: list[ModelBar] = []
    for index, (o, h, low, c) in enumerate(candles):
        detail = (inner or {}).get(index)
        for step in range(width):
            offset = (first + index) * width + step
            if detail:
                mo, mh, ml, mc = detail[step]
                minutes.append(minute(day, offset, mo, mh, ml, mc))
            elif step == 0:
                minutes.append(minute(day, offset, o, h, low, c if width == 1 else o))
            else:
                minutes.append(minute(day, offset, o, o, o, o))
    return minutes


def build_candles(day, spec, **kw):
    minutes = path(day, spec, **kw)
    return minutes, list(aggregate_bars(minutes, AggregationPolicy(kw.get("width", 5)),
                                        complete_through=session_for(day).close_utc))


def test_barriers_resolve_win_loss_and_timeout_on_candles_with_the_entry_at_the_next_open():
    flat = ("100", "100.2", "99.9", "100")
    spec = [flat,                                       # 0 signal
            ("100", "100.2", "99.9", "100.1"),         # 1 entry candle (open 100)
            ("100", "100.4", "99.8", "100.2"),         # 2
            ("100.2", "101.2", "100.1", "101.1"),      # 3 touches +1.0% (101.0)
            ("101", "101.1", "100.9", "101")]          # 4
    minutes, candles = build_candles(DAY, spec)
    labels = {l.signal_bar_start: l for l in label_generator().generate(candles, minute_bars={m.start: m for m in minutes})}
    first = labels[candles[0].start]
    assert first.outcome == "WIN" and first.value == 1
    assert first.entry_time == candles[1].start and first.entry_price == D("100")
    assert first.outcome_price == D("101.00")  # exactly the barrier: no price improvement assumed

    loss_spec = [flat, ("100", "100.2", "99.9", "100"), ("100", "100.1", "99.4", "99.5"),
                 ("99.5", "99.7", "99.4", "99.6"), ("99.6", "99.7", "99.5", "99.6")]
    minutes, candles = build_candles(DAY, loss_spec)
    first = next(iter(label_generator().generate(candles, minute_bars={m.start: m for m in minutes})))
    assert first.outcome == "LOSS" and first.value == -1 and first.outcome_price == D("99.5")

    timeout_spec = [flat] + [("100", "100.3", "99.8", "100.1")] * 5
    minutes, candles = build_candles(DAY, timeout_spec)
    first = next(iter(label_generator().generate(candles, minute_bars={m.start: m for m in minutes})))
    assert first.outcome == "TIMEOUT" and first.value == 0
    # horizon is counted in CANDLES: four 5-minute candles = 20 minutes after the signal candle
    assert first.outcome_time == candles[4].end
    assert first.outcome_time - first.entry_time == timedelta(minutes=5 * 4)


def test_same_candle_barrier_order_comes_from_the_one_minute_bars_else_loss():
    signal = ("100", "100.2", "99.9", "100")
    # entry candle 1 touches both +1.0% (101.0) and -0.5% (99.5)
    quiet = [("100", "100.1", "99.9", "100")] * 3
    both = ("100", "101.2", "99.4", "100")

    def labelled(detail):
        spec = [signal, both, *quiet]
        minutes, candles = build_candles(DAY, spec, inner={1: detail})
        generator = label_generator()
        label = next(iter(generator.generate(candles, minute_bars={m.start: m for m in minutes})))
        return label, generator.stats

    up_first = [("100", "101.2", "100", "101.1"), ("101.1", "101.1", "99.4", "99.5"),
                ("99.5", "99.6", "99.5", "99.6"), ("99.6", "99.6", "99.6", "99.6"),
                ("99.6", "99.7", "99.6", "99.7")]
    label, stats = labelled(up_first)
    assert label.outcome == "WIN" and stats["resolved_by_one_minute_order"] == 1

    down_first = [("100", "100.1", "99.4", "99.5"), ("99.5", "101.2", "99.5", "101.1"),
                  ("101.1", "101.1", "101.0", "101.0"), ("101", "101", "101", "101"),
                  ("101", "101", "101", "101")]
    label, stats = labelled(down_first)
    assert label.outcome == "LOSS" and stats["resolved_by_one_minute_order"] == 1

    same_minute = [("100", "101.3", "99.3", "100"), ("100", "100", "100", "100"),
                   ("100", "100", "100", "100"), ("100", "100", "100", "100"),
                   ("100", "100", "100", "100")]
    label, stats = labelled(same_minute)
    assert label.outcome == "LOSS"  # still ambiguous inside one minute -> never the positive
    assert stats["ambiguous_after_one_minute_treated_as_loss"] == 1

    # without the one-minute bars the conservative policy applies as well
    minutes, candles = build_candles(DAY, [signal, both, *quiet], inner={1: up_first})
    blind = label_generator()
    assert next(iter(blind.generate(candles))).outcome == "LOSS"
    assert blind.stats["same_candle_both_barriers"] == 1


def test_a_gap_through_the_stop_is_filled_at_the_open_and_a_gap_up_win_at_the_barrier():
    minutes, candles = build_candles(DAY, [
        ("100", "100.2", "99.9", "100"), ("100", "100.1", "99.9", "100"),
        ("99.0", "99.1", "98.8", "99.0"), ("99", "99.1", "98.9", "99"), ("99", "99", "99", "99")])
    label = next(iter(label_generator().generate(candles, minute_bars={m.start: m for m in minutes})))
    assert label.outcome == "LOSS" and label.outcome_price == D("99.0")  # open is worse than 99.5
    minutes, candles = build_candles(DAY, [
        ("100", "100.2", "99.9", "100"), ("100", "100.1", "99.9", "100"),
        ("102", "102.5", "101.9", "102"), ("102", "102", "102", "102"), ("102", "102", "102", "102")])
    label = next(iter(label_generator().generate(candles, minute_bars={m.start: m for m in minutes})))
    assert label.outcome == "WIN" and label.outcome_price == D("101.00")  # no improvement assumed


def test_labels_require_the_full_horizon_inside_the_session_and_never_cross_the_close():
    # 12 candles of a session ending at the close; 4-candle horizon
    width, total = 5, 78
    spec = [("100", "100.1", "99.95", "100")] * total
    minutes, candles = build_candles(DAY, spec)
    next_day_minutes, next_candles = build_candles(NEXT_DAY, [("90", "90.1", "89.9", "90")] * 10)
    both = candles + next_candles
    generator = label_generator()
    labels = list(generator.generate(both, minute_bars={m.start: m for m in (*minutes, *next_day_minutes)}))
    signal_starts = [l.signal_bar_start for l in labels]
    last_allowed = candles[total - 1 - 4]  # the signal needs 4 more candles inside the session
    assert max(s for s in signal_starts if s < session_for(NEXT_DAY).open_utc) == last_allowed.start
    assert candles[total - 4].start not in signal_starts  # truncated windows are excluded
    assert all(l.outcome_time <= session_for(DAY).close_utc or l.signal_bar_start >=
               session_for(NEXT_DAY).open_utc for l in labels)
    # the overnight gap (100 -> 90) is therefore never seen by a day-one label
    assert all(l.outcome == "TIMEOUT" for l in labels if l.signal_bar_start < session_for(NEXT_DAY).open_utc)
    assert generator.stats["skipped_incomplete_horizon"] > 0


def test_label_schema_records_that_horizon_bars_are_candles_not_minutes():
    generator = label_generator(horizon=12)
    schema = generator.schema
    assert schema.horizon_bars == 12 and schema.label_id.startswith("triple_barrier")
    assert schema.parameters["bar_minutes"] == "5" and schema.parameters["horizon_minutes"] == "60"
    assert schema.parameters["positive_barrier"] == "0.010"
    assert schema.parameters["ambiguity_policy"] == "resolve_with_1m_bars_then_LOSS"
    assert label_generator(horizon=6, width=15).schema.parameters["horizon_minutes"] == "90"


def test_the_dataset_builder_joins_candle_features_to_future_only_labels():
    minutes: list[ModelBar] = []
    price = D("100")
    for i in range(240):
        close = price + D(((i * 3) % 7) - 3) / D(50)
        minutes.append(minute(DAY, i, str(price), str(max(price, close) + D("0.02")),
                              str(min(price, close) - D("0.02")), str(close), vwap=str(close)))
        price = close
    policy = AggregationPolicy(5)
    builder = AggregatedTrainingDatasetBuilder(
        pipeline(lookback=6), label_generator(horizon=4, up="0.002", down="0.001"), policy
    )
    examples = list(builder.build_symbol(minutes))
    assert examples
    for example in examples:
        assert example.features.as_of <= example.label.entry_time  # features end before entry
        assert example.label.entry_time == example.features.as_of
        assert example.label.outcome_time > example.features.as_of
    # the first example needs six candles of history
    assert examples[0].features.bar_start == session_for(DAY).open_utc + 5 * 5 * MIN
    assert builder.aggregation_stats.candles == 48 and builder.label_generator.stats["labels"] > 0


# ------------------------------------------------------------------------- configurations


EXISTING_IDS = {
    "xgboost_buy_tb_pooled", "lightgbm_buy_tb_pooled",
    "xgboost_sell_continuation_pooled", "lightgbm_sell_continuation_pooled",
}


@pytest.mark.parametrize(
    ("name", "minutes", "lookback", "horizon", "half_life", "features"),
    [("5m", 5, 10, 12, 3.0, 83), ("15m", 15, 6, 6, 2.0, 55)],
)
def test_experiment_configs_validate_and_match_the_requested_design(
    name, minutes, lookback, horizon, half_life, features
):
    config = load_experiment_config(f"config/modeling/xgboost_buy_{name}_tb100_050.yaml")
    baseline = load_experiment_config("config/modeling/xgboost_buy.yaml")
    assert config.algorithm.value == "xgboost" and config.role.value == "buy"
    assert config.model_id == f"xgboost_buy_{name}_tb100_050" and config.model_version == "1.0.0"
    assert config.model_id not in EXISTING_IDS and config.experiment_id != baseline.experiment_id
    assert config.timeframe == "1m" and config.feed == "iex" and config.adjustment == "raw"
    assert config.candles.minutes == minutes
    assert config.feature_config.lookback_bars == lookback
    assert config.feature_config.half_life_bars == half_life
    assert len(config.selected_feature_names) == features
    assert config.objective_parameters["type"] == "triple_barrier_aggregated"
    assert config.objective_parameters["positive_barrier"] == "0.010"
    assert config.objective_parameters["negative_barrier"] == "0.005"
    assert int(config.objective_parameters["horizon_bars"]) == horizon
    # identical split, universe, seed, hyperparameters and cost assumptions to the original BUY model
    assert config.ranges == baseline.ranges and config.seed == baseline.seed
    assert config.universe_id == baseline.universe_id and config.universe_file == baseline.universe_file
    assert dict(config.hyperparameters) == dict(baseline.hyperparameters)
    assert config.objective_parameters["entry_bps"] == baseline.objective_parameters["entry_bps"]
    assert not config.search.candidates and config.search.max_trials == 1  # no search
    assert config.probability_calibration and config.ranges.calibration is not None
    from mike_trade.modeling.splits import FINAL_HOLDOUT

    assert not config.ranges.overall.overlaps(FINAL_HOLDOUT)
    assert config.diagnostics["favorable_class"] == "1"
    assert config.algorithm.value != "lightgbm" and config.role.value != "sell"


def test_inconsistent_candle_settings_are_rejected(tmp_path: Path):
    import yaml

    document = yaml.safe_load(Path("config/modeling/xgboost_buy_5m_tb100_050.yaml").read_text())

    def load(mutate) -> None:
        copy = yaml.safe_load(yaml.safe_dump(document))
        mutate(copy)
        path = tmp_path / "x.yaml"
        path.write_text(yaml.safe_dump(copy))
        load_experiment_config(path)

    load(lambda d: None)
    with pytest.raises(ValueError, match="candle"):
        load(lambda d: d["objective"].update(type="triple_barrier"))
    with pytest.raises(ValueError):
        load(lambda d: d.pop("candles"))
    with pytest.raises(ValueError, match="divide"):
        load(lambda d: d["candles"].update(bar_minutes=7))
    with pytest.raises(ValueError, match="recency_decay"):
        load(lambda d: d["features"].update(recency_decay={"half_life_bars": 3, "extra": 1}))
    with pytest.raises(ValueError):
        load(lambda d: d["features"].update(lookback_bars=2))
    with pytest.raises(ValueError, match="holdout"):
        load(lambda d: d["ranges"].update(evaluation_end="2026-09-30T00:00:00Z"))
    with pytest.raises(ValueError, match="favorable_class"):
        load(lambda d: d["diagnostics"].update(favorable_class="7"))
    old = Path("config/modeling/xgboost_buy.yaml")
    plain = yaml.safe_load(old.read_text())
    plain["candles"] = {"bar_minutes": 5}
    path = tmp_path / "old.yaml"
    path.write_text(yaml.safe_dump(plain))
    with pytest.raises(ValueError, match="candles is only valid"):
        load_experiment_config(path)


def test_the_original_model_definition_is_unchanged_by_the_new_feature_family():
    from mike_trade.modeling.features import IntradayFeatureConfig

    original = load_experiment_config("config/modeling/xgboost_buy.yaml")
    assert isinstance(original.feature_config, IntradayFeatureConfig)
    assert original.feature_config.feature_id == "intraday_core" and original.candles is None
    assert len(original.selected_feature_names) == 19
    assert original.objective_parameters["type"] == "triple_barrier"
    assert int(original.objective_parameters["horizon_bars"]) == 30
    assert math.isclose(float(original.objective_parameters["positive_barrier"]), 0.004)
