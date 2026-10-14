"""``decision_review`` configuration: optional, off by default, identical in every mode.

Illustrative (all keys optional except ``enabled`` to turn it on)::

    decision_review:
      enabled: true
      reviewer: jev            # name of a reviewers: entry in the provider catalog
      review_buy: true
      review_sell: true
      mode: enforce            # disabled | advisory | enforce
      timeout_seconds: 10
      on_error: bypass         # bypass | reject
      record_reviews: true
      strategies: []           # empty = every strategy configured on a reviewed side
      symbols: []              # empty = every symbol
      source: live             # live | replay   (research must choose explicitly)

No secret ever appears here: the reviewer's credentials come from the environment variable named
by the provider catalog entry (OpenRouter: ``OPENROUTER_API_KEY``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from mike_trade.domain.errors import ConfigurationError

__all__ = [
    "REVIEW_MODES",
    "ERROR_POLICIES",
    "REVIEW_SOURCES",
    "DecisionReviewConfig",
    "parse_decision_review",
]

REVIEW_MODES = ("disabled", "advisory", "enforce")
ERROR_POLICIES = ("bypass", "reject")
REVIEW_SOURCES = ("live", "replay")

_KEYS = {
    "enabled", "reviewer", "model", "review_buy", "review_sell", "mode", "timeout_seconds",
    "on_error", "record_reviews", "strategies", "symbols", "source", "replay_from",
    "cache", "expect_reviewer", "max_proposal_age_seconds", "candle_count",
    "daily_candle_count", "provider_catalog",
}
_CACHE_KEYS = {"enabled", "path"}
_EXPECT_KEYS = {"provider", "reviewer", "model", "version"}
_SECRET_TOKENS = ("api_key", "secret", "password", "token", "key_id", "private_key")


def _bad(message: str, path: str) -> ConfigurationError:
    return ConfigurationError(
        f"decision_review: {message}", code="DECISION_REVIEW_INVALID", context={"path": path}
    )


@dataclass(frozen=True, slots=True)
class DecisionReviewConfig:
    """Validated review settings.  ``active`` is False for every configuration that changes
    nothing (the default), so call sites can treat an inactive config as no stage at all."""

    enabled: bool = False
    reviewer: str = "jev"
    #: Optional override of the provider catalog entry's model identifier.
    model: str | None = None
    review_buy: bool = True
    review_sell: bool = True
    mode: str = "advisory"
    timeout_seconds: float = 10.0
    on_error: str = "bypass"
    record_reviews: bool = True
    strategies: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    #: ``live`` calls the reviewer (reusing any cached identical review); ``replay`` serves only
    #: recorded reviews and never calls a model.  ``None`` means "not chosen" and is refused by
    #: research runs, which must state it explicitly.
    source: str | None = None
    replay_from: tuple[str, ...] = ()
    cache_enabled: bool = True
    cache_path: str | None = None
    expect_reviewer: Mapping[str, str] = field(default_factory=dict)
    #: A review answered later than this after the decision bar, while a newer completed bar
    #: exists, can no longer authorize the proposal.
    max_proposal_age_seconds: float = 60.0
    candle_count: int = 30
    daily_candle_count: int = 5
    #: Optional provider-catalog path (default ``config/providers.yaml``).
    provider_catalog: str | None = None

    @property
    def active(self) -> bool:
        return self.enabled and self.mode != "disabled" and (self.review_buy or self.review_sell)

    @property
    def enforcing(self) -> bool:
        return self.active and self.mode == "enforce"

    def reviews_action(self, action: str) -> bool:
        return self.active and (self.review_buy if action == "BUY" else self.review_sell)

    def to_dict(self) -> dict[str, Any]:
        """Plain, secret-free, float-free mapping (run manifests and config fingerprints).

        Numbers that are floats in memory are serialized as text; ``parse_decision_review``
        accepts that form back.
        """
        return {
            "enabled": self.enabled,
            "reviewer": self.reviewer,
            "model": self.model,
            "review_buy": self.review_buy,
            "review_sell": self.review_sell,
            "mode": self.mode,
            "timeout_seconds": repr(self.timeout_seconds),
            "on_error": self.on_error,
            "record_reviews": self.record_reviews,
            "strategies": list(self.strategies),
            "symbols": list(self.symbols),
            "source": self.source,
            "replay_from": list(self.replay_from),
            "cache": {"enabled": self.cache_enabled, "path": self.cache_path},
            "expect_reviewer": dict(self.expect_reviewer),
            "max_proposal_age_seconds": repr(self.max_proposal_age_seconds),
            "candle_count": self.candle_count,
            "daily_candle_count": self.daily_candle_count,
            "provider_catalog": self.provider_catalog,
        }


def _bool(node: Mapping[str, Any], key: str, default: bool) -> bool:
    value = node.get(key, default)
    if not isinstance(value, bool):
        raise _bad(f"{key} must be true or false; got {value!r}", key)
    return value


def _text_list(node: Mapping[str, Any], key: str, *, upper: bool = False) -> tuple[str, ...]:
    value = node.get(key, ())
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise _bad(f"{key} must be a list of strings", key)
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise _bad(f"{key} must contain non-empty strings", key)
        items.append(item.strip().upper() if upper else item.strip())
    if len(set(items)) != len(items):
        raise _bad(f"{key} must not repeat entries", key)
    return tuple(items)


def _number(node: Mapping[str, Any], key: str, default: float, *, minimum: float) -> float:
    value = node.get(key, default)
    if isinstance(value, str):  # the serialized form (see ``to_dict``) stores numbers as text
        try:
            value = float(value)
        except ValueError:
            raise _bad(f"{key} must be a number; got {value!r}", key) from None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum:
        raise _bad(f"{key} must be a number >= {minimum}; got {value!r}", key)
    return float(value)


def _int(node: Mapping[str, Any], key: str, default: int, *, minimum: int) -> int:
    value = node.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _bad(f"{key} must be an integer >= {minimum}; got {value!r}", key)
    return value


def parse_decision_review(
    raw: Mapping[str, Any] | None, *, research: bool = False
) -> DecisionReviewConfig:
    """Validate ``raw``.  ``None`` or ``{}`` is the (inactive) default.

    With ``research=True`` (historical replay) an active configuration must name its ``source``
    explicitly (live inference or recorded replay) - there is no implicit paid-inference default.
    """
    if raw is None:
        return DecisionReviewConfig()
    if not isinstance(raw, Mapping):
        raise _bad("must be a mapping", "decision_review")
    unknown = sorted(set(raw) - _KEYS)
    if unknown:
        raise _bad(f"unknown keys {unknown}", "decision_review")
    for key in raw:
        if any(token in str(key).lower() for token in _SECRET_TOKENS):
            raise _bad(f"{key!r} looks like a secret; credentials never go in configuration", key)

    enabled = _bool(raw, "enabled", False)
    mode = raw.get("mode", "advisory")
    if mode not in REVIEW_MODES:
        raise _bad(f"mode must be one of {list(REVIEW_MODES)}; got {mode!r}", "mode")
    on_error = raw.get("on_error", "bypass")
    if on_error not in ERROR_POLICIES:
        raise _bad(f"on_error must be one of {list(ERROR_POLICIES)}; got {on_error!r}", "on_error")
    reviewer = raw.get("reviewer", "jev")
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise _bad("reviewer must be a non-empty provider-catalog name", "reviewer")
    model = raw.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise _bad("model must be a non-empty string when given", "model")
    source = raw.get("source")
    if source is not None and source not in REVIEW_SOURCES:
        raise _bad(f"source must be one of {list(REVIEW_SOURCES)}; got {source!r}", "source")
    replay_from = _text_list(raw, "replay_from")
    cache = raw.get("cache", {})
    if not isinstance(cache, Mapping) or set(cache) - _CACHE_KEYS:
        raise _bad(f"cache accepts only {sorted(_CACHE_KEYS)}", "cache")
    cache_path = cache.get("path")
    if cache_path is not None and (not isinstance(cache_path, str) or not cache_path.strip()):
        raise _bad("cache.path must be a non-empty string", "cache.path")
    expect = raw.get("expect_reviewer", {})
    if not isinstance(expect, Mapping) or set(expect) - _EXPECT_KEYS or any(
        not isinstance(v, str) or not v for v in expect.values()
    ):
        raise _bad(f"expect_reviewer accepts only string {sorted(_EXPECT_KEYS)}", "expect_reviewer")
    catalog = raw.get("provider_catalog")
    if catalog is not None and (not isinstance(catalog, str) or not catalog.strip()):
        raise _bad("provider_catalog must be a path string", "provider_catalog")

    config = DecisionReviewConfig(
        enabled=enabled,
        reviewer=reviewer.strip(),
        model=None if model is None else model.strip(),
        review_buy=_bool(raw, "review_buy", True),
        review_sell=_bool(raw, "review_sell", True),
        mode=mode,
        timeout_seconds=_number(raw, "timeout_seconds", 10.0, minimum=0.001),
        on_error=on_error,
        record_reviews=_bool(raw, "record_reviews", True),
        strategies=_text_list(raw, "strategies"),
        symbols=_text_list(raw, "symbols", upper=True),
        source=source,
        replay_from=replay_from,
        cache_enabled=_bool(cache, "enabled", True),
        cache_path=cache_path,
        expect_reviewer=dict(expect),
        max_proposal_age_seconds=_number(raw, "max_proposal_age_seconds", 60.0, minimum=0.0),
        candle_count=_int(raw, "candle_count", 30, minimum=1),
        daily_candle_count=_int(raw, "daily_candle_count", 5, minimum=0),
        provider_catalog=catalog,
    )
    if config.source == "replay" and not config.replay_from:
        raise _bad("source: replay requires replay_from (recorded review files or directories)",
                   "replay_from")
    if config.source == "live" and config.replay_from:
        raise _bad("replay_from is only valid with source: replay", "replay_from")
    if research and config.active and config.source is None:
        raise _bad(
            "research runs must choose explicitly how reviews are produced: set "
            "source: live (call the reviewer; identical cached inputs are reused) or "
            "source: replay with replay_from (recorded reviews only, no model calls); "
            "mode: advisory records opinions without changing execution",
            "source",
        )
    return config
