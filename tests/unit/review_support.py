"""Test doubles for decision review: a fake reviewer and evidence-producing scripted strategies.

No network, credentials or paid API is involved anywhere in the review tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from decimal import Decimal

from scripted_strategies import make_strategy

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.review.evidence import make_evidence
from mike_trade.review.reviewer import (
    ReviewDecision,
    ReviewerIdentity,
    ReviewResponse,
    ReviewUsage,
)
from mike_trade.strategies.registry import StrategyRegistry


class FakeReviewer:
    """Deterministic reviewer.  ``decide(request) -> 'APPROVE' | 'REJECT'`` or raises."""

    def __init__(
        self,
        decide: Callable[[Mapping], str] | str = "APPROVE",
        *,
        delay: float = 0.0,
        error: BaseException | None = None,
        model: str = "fake-model",
        point_in_time_safe: bool = False,
    ) -> None:
        self._decide = (lambda _request: decide) if isinstance(decide, str) else decide
        self.delay = delay
        self.error = error
        self.calls: list[Mapping] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self._identity = ReviewerIdentity(
            provider="fake", reviewer="jev", model=model, settings={"v": 1},
            point_in_time_safe=point_in_time_safe,
        )

    @property
    def identity(self) -> ReviewerIdentity:
        return self._identity

    async def review(self, request: Mapping) -> ReviewResponse:
        self.calls.append(request)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.error is not None:
                raise self.error
            verdict = self._decide(request)
            return ReviewResponse(
                ReviewDecision(verdict),
                f"fake reviewer says {verdict}",
                ("observation",),
                "fake-1",
                ReviewUsage(100, 20, 120, Decimal("0.0002"), served_model=self._identity.model),
            )
        finally:
            self.in_flight -= 1

    async def aclose(self) -> None:
        return None


def evidence_strategy(strategy_id: str, side: StrategyRole) -> type:
    """A scripted strategy that also implements the decision-evidence interface."""
    base = make_strategy(strategy_id, side)

    def build_evidence(self, context, params, state, evaluation):  # noqa: ANN001, ANN202
        return make_evidence(
            base.spec, evaluation, context,
            reason_code="SCRIPTED",
            reason="scripted test signal",
            evidence={
                "evaluation_count": state["count"],
                "last_close": context.candles[CandleTimeframe.M1][-1].close,
                f"{strategy_id}_only": True,
            },
        )

    return type(
        base.__name__, (base,),
        {"EVIDENCE_INTERFACE_VERSION": 1, "build_evidence": build_evidence},
    )


def review_registry() -> StrategyRegistry:
    """ev_buy_a, ev_buy_b, ev_sell_a (with evidence) and plain_buy / plain_sell (without)."""
    registry = StrategyRegistry()
    for sid in ("ev_buy_a", "ev_buy_b"):
        registry.register(evidence_strategy(sid, StrategyRole.BUY))
    registry.register(evidence_strategy("ev_sell_a", StrategyRole.SELL))
    registry.register(make_strategy("plain_buy", StrategyRole.BUY))
    registry.register(make_strategy("plain_sell", StrategyRole.SELL))
    return registry
