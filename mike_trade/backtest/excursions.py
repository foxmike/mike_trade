"""Trade MFE / MAE accounting from in-position price observations.

Conventions (shared with the dashboard trade diagnostics):

* Excursions are PRICE movement relative to the actual entry fill, times the matched
  quantity.  Fees and borrow costs belong to net P&L only and never enter an excursion.
* ``mfe >= 0`` is the best unrealized gain seen; ``mae <= 0`` is the worst unrealized loss
  seen.  A short gains when the price falls.
* ``mfe_pct`` / ``mae_pct`` are percent (x100) of the entry price, i.e. the per-share move.

Observation window.  Bars are identified by their START time and last one minute (or an
explicit ``end``).  Replay fills are stamped with the bar start when filled at the open and
with the bar end when filled intrabar, so a bar is in-position only when it lies wholly inside
``[entry_time, exit_time]``: ``bar_start >= entry_time`` and ``bar_end <= exit_time``.  A bar
that straddles the entry (intrabar entry) or the exit (exit at the open of that bar) can hold
prices from outside the position and is excluded, because OHLC cannot order events within a
bar.  A bar containing an intrabar exit is wholly inside the interval and is counted; the
replay's ambiguity policy decides the fill, not the excursion window.

The entry and exit fill prices are themselves observed prices and are always included, so
excursions never understate what the realized fills already prove.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

__all__ = ["BAR_DURATION", "Excursion", "compute_excursion"]

ZERO = Decimal("0")
HUNDRED = Decimal(100)
BAR_DURATION = timedelta(minutes=1)


class Excursion(dict):
    """``mfe``, ``mae``, ``mfe_pct``, ``mae_pct``, ``excursion_source``, ``excursion_bars``."""


def _dt(value: Any) -> datetime:
    return (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    )


def _dec(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _bar_span(bar: Mapping[str, Any]) -> tuple[datetime, datetime]:
    start = _dt(bar["time"])
    end = bar.get("end")
    return start, _dt(end) if end is not None else start + BAR_DURATION


def compute_excursion(
    *,
    symbol: str,
    direction: str,
    entry_time: datetime,
    exit_time: datetime,
    entry_price: Decimal,
    exit_price: Decimal,
    quantity: Decimal,
    bars: Iterable[Mapping[str, Any]],
) -> Excursion:
    """MFE/MAE of one matched lot.  ``excursion_source`` is ``"bars"`` when at least one
    in-position bar was found, else ``"fills"`` (only the entry/exit fill prices were
    observed, so the values are lower bounds rather than guesses)."""
    symbol = symbol.upper()
    highs = [entry_price, exit_price]
    lows = [entry_price, exit_price]
    used = 0
    for bar in bars:
        if str(bar["symbol"]).upper() != symbol:
            continue
        start, end = _bar_span(bar)
        if start < entry_time or end > exit_time:
            continue
        highs.append(_dec(bar["high"]))
        lows.append(_dec(bar["low"]))
        used += 1
    high, low = max(highs), min(lows)
    if direction == "SHORT":
        favorable, adverse = entry_price - low, entry_price - high
    else:
        favorable, adverse = high - entry_price, low - entry_price
    favorable, adverse = max(ZERO, favorable), min(ZERO, adverse)
    return Excursion(
        mfe=favorable * quantity,
        mae=adverse * quantity,
        mfe_pct=favorable / entry_price * HUNDRED if entry_price else None,
        mae_pct=adverse / entry_price * HUNDRED if entry_price else None,
        excursion_source="bars" if used else "fills",
        excursion_bars=used,
    )
