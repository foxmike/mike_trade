"""Reference-value tests for the technical series indicators in ``indicators.core``.

Every expected value below is derived by hand from the published definition (StockCharts
ChartSchool / the indicator creators); comments show the arithmetic.  The prefix test proves
no element depends on a later input (no look-ahead).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from mike_trade.strategies.indicators import core

D = Decimal
TOLERANCE = D("1e-20")


@dataclass(frozen=True)
class HLC:
    high: Decimal
    low: Decimal
    close: Decimal


def bars(*rows: tuple[str, str, str]) -> list[HLC]:
    return [HLC(D(h), D(low), D(c)) for h, low, c in rows]


def assert_series(actual: list[Decimal | None], expected: list[str | None]) -> None:
    assert len(actual) == len(expected)
    for index, (got, want) in enumerate(zip(actual, expected)):
        if want is None:
            assert got is None, f"index {index}: expected None, got {got}"
        else:
            assert got is not None, f"index {index}: expected {want}, got None"
            assert abs(got - D(want)) < TOLERANCE, f"index {index}: {got} != {want}"


# ---------------------------------------------------------------- moving averages


def test_sma_matches_definition() -> None:
    assert core.sma([D(v) for v in "12345"], 3) == [None, None, D(2), D(3), D(4)]
    assert core.sma([D("1")], 3) == [None]


def test_ema_is_sma_seeded_and_identical_to_the_incremental_calculator() -> None:
    values = [D(v) for v in ("22.27", "22.19", "22.08", "22.17", "22.18", "22.13", "22.23",
                             "22.43", "22.24", "22.29", "22.15", "22.39", "22.38", "22.61")]
    calc = core.EMACalculator(5)
    assert core.ema(values, 5) == [calc.update(v) for v in values]
    assert core.ema(values, 5)[:4] == [None] * 4
    assert core.ema(values, 5)[4] == sum(values[:5], D(0)) / 5  # SMA seed
    assert core.ema(values[:3], 5) == [None, None, None]


# ---------------------------------------------------------------- MACD


def test_macd_on_a_straight_line_has_constant_lag() -> None:
    # closes 1..6.  EMA(2): seed 1.5, then 2.5, 3.5, 4.5, 5.5 (alpha 2/3).
    # EMA(3): seed 2, then 3, 4, 5 (alpha 1/2).  MACD = 0.5 from index 2.
    # Signal(2) is seeded with SMA(0.5, 0.5) at index 3 and stays 0.5.
    line, signal, hist = core.macd([D(v) for v in "123456"], 2, 3, 2)
    assert_series(line, [None, None, "0.5", "0.5", "0.5", "0.5"])
    assert_series(signal, [None, None, None, "0.5", "0.5", "0.5"])
    assert_series(hist, [None, None, None, "0", "0", "0"])


def test_macd_default_periods_and_validation() -> None:
    closes = [D(100 + i % 7) for i in range(60)]
    line, signal, hist = core.macd(closes)
    assert line.index(next(v for v in line if v is not None)) == 25  # slow - 1
    first_signal = next(i for i, v in enumerate(signal) if v is not None)
    assert first_signal == 25 + 8  # slow - 1 + signal - 1
    assert hist[first_signal] == line[first_signal] - signal[first_signal]
    with pytest.raises(ValueError):
        core.macd(closes, 26, 12)


# ---------------------------------------------------------------- Stochastic


def test_stochastic_matches_lane_formula() -> None:
    data = bars(("10", "6", "8"), ("12", "7", "11"), ("11", "8", "9"), ("13", "9", "12"))
    # k_period 3, no smoothing: idx2 HH=12 LL=6 -> 100*(9-6)/6 = 50
    # idx3 HH=13 LL=7 -> 100*(12-7)/6 = 83.333...; %D(2) at idx3 = (50 + 83.33)/2
    k, d = core.stochastic(data, 3, 1, 2)
    assert k[:2] == [None, None] and d[:3] == [None, None, None]
    assert k[2] == D(50)
    assert abs(k[3] - D(250) / 3) < TOLERANCE
    assert abs(d[3] - (D(50) + D(250) / 3) / 2) < TOLERANCE
    # Slow stochastic: %K = SMA(raw, 2) -> first defined at idx3
    slow_k, _ = core.stochastic(data, 3, 2, 1)
    assert slow_k[2] is None and abs(slow_k[3] - (D(50) + D(250) / 3) / 2) < TOLERANCE


def test_stochastic_flat_range_is_undefined() -> None:
    flat = bars(*[("5", "5", "5")] * 6)
    k, d = core.stochastic(flat, 3, 1, 2)
    assert all(v is None for v in k) and all(v is None for v in d)


# ---------------------------------------------------------------- Williams %R, CCI


def test_williams_r_formula() -> None:
    data = bars(("10", "4", "5"), ("8", "0", "3"), ("6", "2", "2.5"))
    # HH=10, LL=0, close 2.5 -> -100*(10-2.5)/10 = -75
    assert core.williams_r(data, 3) == [None, None, D("-75")]
    assert core.williams_r(bars(*[("5", "5", "5")] * 3), 3) == [None, None, None]


def test_cci_formula() -> None:
    # typical prices 1, 2, 6 -> mean 3, mean deviation (2+1+3)/3 = 2,
    # CCI = (6 - 3) / (0.015 * 2) = 100
    data = bars(("1", "1", "1"), ("2", "2", "2"), ("6", "6", "6"))
    assert core.cci(data, 3) == [None, None, D(100)]
    assert core.cci(bars(*[("4", "4", "4")] * 3), 3) == [None, None, None]


# ---------------------------------------------------------------- Donchian, Aroon


def test_donchian_excludes_the_current_bar() -> None:
    data = bars(("5", "3", "4"), ("7", "4", "6"), ("6", "2", "3"), ("4", "1", "2"), ("8", "5", "7"))
    upper, lower = core.donchian(data, 3)
    assert upper == [None, None, None, D(7), D(7)]  # idx3: max(5,7,6); idx4: max(7,6,4)
    assert lower == [None, None, None, D(2), D(1)]  # idx3: min(3,4,2); idx4: min(4,2,1)


def test_aroon_formula_and_tie_handling() -> None:
    # period 2, window of 3 bars: high 7 is 1 bar ago -> up 50; low 2 is now -> down 100
    up, down = core.aroon(bars(("5", "3", "4"), ("7", "4", "6"), ("6", "2", "3")), 2)
    assert (up, down) == ([None, None, D(50)], [None, None, D(100)])
    # equal highs resolve to the most recent bar -> Aroon-Up 100
    up, _ = core.aroon(bars(("7", "3", "4"), ("7", "4", "6"), ("7", "5", "6")), 2)
    assert up[2] == D(100)


# ---------------------------------------------------------------- DMI


def test_directional_indicators_wilder_smoothing() -> None:
    data = bars(("10", "8", "9"), ("11", "9", "10"), ("12", "10", "11"), ("11.5", "9.5", "10"))
    # idx1: TR=2 +DM=1 -DM=0.  idx2: TR=2 +DM=1 -DM=0.  period 2 -> sums TR=4 +DM=2 -DM=0
    # -> +DI = 100*2/4 = 50, -DI = 0.
    # idx3: TR=max(2, |11.5-11|, |9.5-11|)=2, +DM=0, -DM=0.5 -> smoothed TR = 4 - 4/2 + 2 = 4,
    # +DM = 2 - 2/2 + 0 = 1, -DM = 0 - 0 + 0.5 = 0.5 -> +DI = 25, -DI = 12.5
    plus, minus = core.directional_indicators(data, 2)
    assert_series(plus, [None, None, "50", "25"])
    assert_series(minus, [None, None, "0", "12.5"])
    assert core.directional_indicators(data[:2], 2) == ([None, None], [None, None])


# ---------------------------------------------------------------- Parabolic SAR


def test_parabolic_sar_wilder_trace() -> None:
    data = bars(("10", "8", "9"), ("11", "9", "10.5"), ("12", "10", "11.5"), ("12.5", "11", "12"),
                ("11", "7", "7.5"), ("9", "6", "6.5"), ("10", "6.2", "9.8"), ("13", "9.5", "12.8"))
    # Start: second close >= first -> uptrend, SAR = first low 8, EP = first high 10, AF .02.
    # i1 SAR 8.04 clamped to prior low 8; high 11 > EP -> EP 11, AF .04           (8, up)
    # i2 SAR 8.12 clamped to min(prior two lows 9, 8) = 8; EP 12, AF .06          (8, up)
    # i3 SAR 8 + .06*(12-8) = 8.24; EP 12.5, AF .08                               (8.24, up)
    # i4 SAR 8.24 + .08*(12.5-8.24) = 8.5808; low 7 < SAR -> reversal:
    #    SAR = prior EP 12.5, EP = 7, AF .02                                      (12.5, down)
    # i5 SAR 12.39 raised to max(prior two highs 11, 12.5) = 12.5; EP 6, AF .04   (12.5, down)
    # i6 SAR 12.5 + .04*(6-12.5) = 12.24                                          (12.24, down)
    # i7 SAR 12.24 - .2496 = 11.9904; high 13 > SAR -> reversal: SAR = EP 6       (6, up)
    path = core.parabolic_sar(data)
    assert path == [
        None, (D("8"), True), (D("8"), True), (D("8.24"), True), (D("12.5"), False),
        (D("12.5"), False), (D("12.24"), False), (D("6"), True),
    ]


def test_parabolic_sar_parameter_validation_and_short_input() -> None:
    assert core.parabolic_sar(bars(("10", "8", "9"))) == [None]
    with pytest.raises(ValueError):
        core.parabolic_sar(bars(("10", "8", "9")), D("0.2"), D("0.1"))


# ---------------------------------------------------------------- no look-ahead / input checks


def _series(count: int = 70) -> list[HLC]:
    rows = []
    state = 12345
    price = 100
    for _ in range(count):
        state = (state * 1103515245 + 12345) % 2**31
        price += (state % 700 - 300) / 100
        spread = (state // 7 % 90) / 100 + 0.1
        rows.append((f"{price + spread:.2f}", f"{price - spread:.2f}", f"{price:.2f}"))
    return bars(*rows)


SERIES_CASES = {
    "sma": lambda b: core.sma([x.close for x in b], 5),
    "ema": lambda b: core.ema([x.close for x in b], 5),
    "macd_line": lambda b: core.macd([x.close for x in b], 5, 9, 4)[0],
    "macd_signal": lambda b: core.macd([x.close for x in b], 5, 9, 4)[1],
    "stoch_k": lambda b: core.stochastic(b, 7, 3, 3)[0],
    "stoch_d": lambda b: core.stochastic(b, 7, 3, 3)[1],
    "williams_r": lambda b: core.williams_r(b, 7),
    "cci": lambda b: core.cci(b, 8),
    "donchian_upper": lambda b: core.donchian(b, 6)[0],
    "donchian_lower": lambda b: core.donchian(b, 6)[1],
    "plus_di": lambda b: core.directional_indicators(b, 6)[0],
    "minus_di": lambda b: core.directional_indicators(b, 6)[1],
    "aroon_up": lambda b: core.aroon(b, 6)[0],
    "aroon_down": lambda b: core.aroon(b, 6)[1],
    "parabolic_sar": lambda b: core.parabolic_sar(b),
}


@pytest.mark.parametrize("name", sorted(SERIES_CASES))
def test_element_i_depends_only_on_inputs_up_to_i(name: str) -> None:
    data = _series()
    full = SERIES_CASES[name](data)
    assert len(full) == len(data)
    for cut in (12, 25, 40, 69):
        assert SERIES_CASES[name](data[:cut]) == full[:cut], f"{name} changed at prefix {cut}"
    assert any(v is not None for v in full)


def test_series_functions_reject_non_decimal_input_and_bad_periods() -> None:
    with pytest.raises(TypeError):
        core.sma([1, 2, 3], 2)  # type: ignore[list-item]
    with pytest.raises(ValueError):
        core.sma([D(1)], 0)
    with pytest.raises(ValueError):
        core.aroon(bars(("2", "1", "1")), 0)
