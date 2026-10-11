"""Shared closed-bar evaluation for the cumulative-volume-line breakout BUY strategies."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from mike_trade.strategies.base import ParameterKind, ParameterSchema, ParameterSpec, StrategyEvaluation, StrategySpec
from mike_trade.strategies.closed_bar import adjacent, advance, blocked, closed_bars, finish, gate, insufficient, known, parts
from mike_trade.strategies.indicators.cumulative import CumulativeLineCalculator

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

DEFAULT_PERIOD = 20
SCHEMA = ParameterSchema((
    ParameterSpec("period", ParameterKind.INT, DEFAULT_PERIOD, 2, 200,
                  description="Bars of line history the breakout must exceed. Default 20."),
))


def required(params: Mapping[str, object]) -> int:
    """Completed bars needed: ``period`` earlier line values for both the prior and latest bar."""
    period = SCHEMA.validate(params)["period"]
    assert isinstance(period, int)
    return period + 2


def evaluate_breakout(spec: StrategySpec, kind: str, context: MarketContext,
                      params: Mapping[str, object], state: Mapping[str, object]) -> StrategyEvaluation:
    """BUY when the cumulative line exceeds its previous ``period`` values for the first time."""
    period = SCHEMA.validate(params)["period"]
    assert isinstance(period, int)
    reason = gate(spec, context, state)
    bars = closed_bars(spec, context, period + 2)
    if bars is None:
        return insufficient(spec, context)
    adv = advance(state, bars, f"{spec.strategy_id}|{spec.version}|{kind}|{period}",
                  fresh=lambda: CumulativeLineCalculator(kind, period),
                  restore=CumulativeLineCalculator.restore,
                  feed=lambda calc, bar: calc.update(bar))
    if reason is not None:
        return blocked(spec, context, reason, adv.updates)
    line_prior, high_prior = parts(adv.prior, 2)
    line_latest, high_latest = parts(adv.latest, 2)
    if not adjacent(bars[-2], bars[-1]) or not known(line_prior, high_prior, line_latest, high_latest):
        return insufficient(spec, context, adv.updates)
    triggered = line_prior <= high_prior and line_latest > high_latest
    return finish(spec, context, bars[-1], state, triggered, adv.updates)
