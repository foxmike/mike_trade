from __future__ import annotations

import ast
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from mike_trade.strategies.indicators import core

D = Decimal


@dataclass(frozen=True)
class Bar:
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    session_id: str = "a"


def bar(high: str, low: str, close: str, volume: str = "1", session: str = "a") -> Bar:
    return Bar(D(close), D(high), D(low), D(close), D(volume), session)


def test_ema_not_ready_then_exact_sma_seed() -> None:
    calc = core.EMACalculator(3)
    assert calc.warmup_bars == 3
    assert [calc.update(D(v)) for v in ("1", "2")] == [None, None]
    assert not calc.ready
    assert calc.update(D("6")) == D("3")
    assert calc.ready
    assert calc.update(D("10")) == D("6.5")


def test_rsi_warmup_all_gains_and_flat_edge_case() -> None:
    calc = core.RSICalculator(2)
    assert [calc.update(D(v)) for v in ("10", "11")] == [None, None]
    assert not calc.ready
    assert calc.update(D("13")) == D("100")
    assert calc.ready
    assert core.rsi([D("5"), D("5"), D("5")], 2) == [None, None, D("50")]


def test_atr_includes_gap_in_true_range() -> None:
    calc = core.ATRCalculator(2)
    first = bar("10", "8", "9")
    gap_bar = bar("12", "10", "11")
    assert calc.update(first) is None  # TR = 2
    assert calc.update(gap_bar) == D("2.5")  # second TR = |12 - 9| = 3
    assert calc.ready


def test_session_vwap_resets_and_ignores_zero_volume() -> None:
    calc = core.VWAPCalculator()
    assert calc.update(bar("4", "2", "3", "0", "one")) is None
    assert not calc.ready
    assert calc.update(bar("4", "2", "3", "2", "one")) == D("3")
    assert calc.update(bar("100", "90", "95", "0", "one")) == D("3")
    assert calc.update(bar("4", "2", "3", "0", "two")) is None
    assert not calc.ready
    assert calc.update(bar("6", "3", "6", "1", "two")) == D("5")


def test_relative_volume_warmup_and_zero_average() -> None:
    calc = core.RelativeVolumeCalculator(2)
    assert calc.update(D("4")) is None
    assert calc.update(D("4")) == D("1")
    assert core.relative_volume([D("0"), D("0")], 2) == [None, None]


def test_batch_and_incremental_outputs_match() -> None:
    closes = [D(x) for x in ("10", "11", "9", "12", "13")]
    bars = [bar("11", "9", "10", "2", "a"), bar("12", "10", "11", "3", "a"),
            bar("11", "8", "9", "1", "a"), bar("13", "9", "12", "4", "b"),
            bar("14", "12", "13", "2", "b")]
    for batch, calc in (
        (core.ema(closes, 3), core.EMACalculator(3)),
        (core.rsi(closes, 2), core.RSICalculator(2)),
        (core.atr(bars, 2), core.ATRCalculator(2)),
        (core.session_vwap(bars), core.VWAPCalculator()),
    ):
        values = closes if isinstance(calc, (core.EMACalculator, core.RSICalculator)) else bars
        incremental = [calc.update(value) for value in values]
        assert batch == incremental


def test_numeric_outputs_are_decimals_and_never_floats() -> None:
    outputs = [
        *core.ema([D("1"), D("2")], 1),
        *core.rsi([D("1"), D("2")], 1),
        *core.atr([bar("2", "1", "2")], 1),
        *core.session_vwap([bar("2", "1", "2", "1")]),
        *core.volume_sma([D("1")], 1),
        *core.relative_volume([D("1")], 1),
        *core.cumulative_session_volume([(D("1"), "a")]),
    ]
    numeric = [value for value in outputs if value is not None]
    assert numeric
    assert all(isinstance(value, Decimal) for value in numeric)
    assert not any(isinstance(value, float) for value in outputs)


def test_indicator_module_has_no_io_or_market_data_imports() -> None:
    source = Path(core.__file__).read_text()
    tree = ast.parse(source)
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
    forbidden = ("network", "broker", "provider", "market_data", "urllib", "http")
    assert not [name for name in imports if any(part in name.lower() for part in forbidden)]
