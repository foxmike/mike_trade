"""Compose a :class:`DecisionReviewStage` from configuration and the provider catalog.

Reviewer *providers* are registered in :data:`REVIEWER_PROVIDERS` (OpenRouter today); a reviewer
*name* (``decision_review.reviewer``, e.g. ``jev``) selects an entry of the provider catalog's
``reviewers:`` section, which names the provider, the model and non-secret settings::

    reviewers:
      jev:
        provider: openrouter
        model: typesafe/jev-router        # any OpenRouter model identifier
        api_key_env: OPENROUTER_API_KEY   # environment variable NAME; never the value
        temperature: 0
        max_concurrent_requests: 16
        requests_per_minute: null
        point_in_time_safe: false

When the catalog has no ``jev`` entry the built-in default (OpenRouter + the verified JEV model
identifier) is used, so the only thing an operator must supply is ``OPENROUTER_API_KEY``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx
import yaml

from mike_trade.domain.errors import ConfigurationError
from mike_trade.review.config import DecisionReviewConfig
from mike_trade.review.openrouter import (
    DEFAULT_JEV_MODEL,
    OPENROUTER_API_KEY_ENV,
    OPENROUTER_BASE_URL,
    OpenRouterReviewer,
)
from mike_trade.review.reviewer import TradeDecisionReviewer
from mike_trade.review.stage import DecisionReviewStage
from mike_trade.review.store import JsonlReviewStore, MemoryReviewStore

__all__ = [
    "REVIEWER_PROVIDERS",
    "DEFAULT_REVIEWER_ENTRIES",
    "validate_reviewer_entries",
    "load_reviewer_entries",
    "resolve_reviewer_entry",
    "build_reviewer",
    "build_stage",
    "default_review_path",
]

_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_ENTRY_KEYS = {
    "provider", "model", "api_key_env", "base_url", "temperature", "max_tokens",
    "response_format", "max_concurrent_requests", "requests_per_minute",
    "point_in_time_safe", "http_referer", "app_title", "http_timeout_seconds",
}

DEFAULT_REVIEWER_ENTRIES: Mapping[str, Mapping[str, Any]] = {
    "jev": {"provider": "openrouter", "model": DEFAULT_JEV_MODEL},
}


def _bad(message: str, path: str) -> ConfigurationError:
    return ConfigurationError(
        f"reviewers.{path}: {message}", code="REVIEWER_CATALOG_INVALID", context={"path": path}
    )


def validate_reviewer_entries(raw: object) -> dict[str, dict[str, Any]]:
    """Shape-validate the catalog's ``reviewers`` mapping (env var *names*, never secrets)."""
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise _bad("must be a mapping of reviewer names to settings", "")
    entries: dict[str, dict[str, Any]] = {}
    for name, entry in raw.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", name):
            raise _bad(f"invalid reviewer name {name!r}", str(name))
        if not isinstance(entry, Mapping):
            raise _bad("must be a mapping", name)
        unknown = sorted(set(entry) - _ENTRY_KEYS)
        if unknown:
            raise _bad(f"unknown keys {unknown}", name)
        provider = entry.get("provider")
        if provider not in REVIEWER_PROVIDERS:
            raise _bad(
                f"provider must be one of {sorted(REVIEWER_PROVIDERS)}; got {provider!r}", name
            )
        model = entry.get("model")
        if not isinstance(model, str) or not model.strip():
            raise _bad("model must be a non-empty model identifier", name)
        env = entry.get("api_key_env", OPENROUTER_API_KEY_ENV)
        if not isinstance(env, str) or not _ENV_NAME.match(env):
            raise _bad(f"api_key_env must be an environment variable NAME; got {env!r}", name)
        for key in ("max_concurrent_requests",):
            value = entry.get(key, 16)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise _bad(f"{key} must be a positive integer", name)
        rate = entry.get("requests_per_minute")
        if rate is not None and (
            isinstance(rate, bool) or not isinstance(rate, (int, float)) or rate <= 0
        ):
            raise _bad("requests_per_minute must be a positive number or null", name)
        if not isinstance(entry.get("point_in_time_safe", False), bool):
            raise _bad("point_in_time_safe must be true or false", name)
        entries[name] = dict(entry)
    return entries


def load_reviewer_entries(path: str | os.PathLike[str] | None = None) -> dict[str, dict[str, Any]]:
    """Read ``reviewers:`` from the provider catalog (``config/providers.yaml``, else the example).

    A missing file yields no entries (the built-in ``jev`` default then applies).
    """
    candidates = (
        [Path(path)] if path is not None
        else [Path("config/providers.yaml"), Path("config/providers.example.yaml")]
    )
    for candidate in candidates:
        if candidate.is_file():
            document = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
            return validate_reviewer_entries(document.get("reviewers"))
    if path is not None:
        raise ConfigurationError(
            f"provider catalog not found: {path}", code="REVIEWER_CATALOG_INVALID"
        )
    return {}


def resolve_reviewer_entry(
    config: DecisionReviewConfig, entries: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    entry = entries.get(config.reviewer) or DEFAULT_REVIEWER_ENTRIES.get(config.reviewer)
    if entry is None:
        raise ConfigurationError(
            f"decision_review.reviewer {config.reviewer!r} is not defined in the provider "
            f"catalog's reviewers (known: {sorted({*entries, *DEFAULT_REVIEWER_ENTRIES})})",
            code="REVIEWER_UNKNOWN",
        )
    resolved = dict(entry)
    if config.model is not None:
        resolved["model"] = config.model
    return resolved


def _build_openrouter(
    name: str,
    entry: Mapping[str, Any],
    environ: Mapping[str, str],
    transport: httpx.AsyncBaseTransport | None,
) -> OpenRouterReviewer:
    return OpenRouterReviewer(
        reviewer_name=name,
        model=entry["model"],
        api_key_env=entry.get("api_key_env", OPENROUTER_API_KEY_ENV),
        environ=environ,
        base_url=entry.get("base_url", OPENROUTER_BASE_URL),
        temperature=entry.get("temperature", 0.0),
        max_tokens=entry.get("max_tokens"),
        response_format=entry.get("response_format", "json_schema"),
        point_in_time_safe=bool(entry.get("point_in_time_safe", False)),
        http_referer=entry.get("http_referer"),
        app_title=entry.get("app_title", "mike_trade"),
        http_timeout_seconds=float(entry.get("http_timeout_seconds", 120.0)),
        transport=transport,
    )


#: provider name -> factory(reviewer name, catalog entry, environ, optional transport).
#: Register further providers here; nothing else in the review pipeline is provider specific.
REVIEWER_PROVIDERS: dict[str, Callable[..., TradeDecisionReviewer]] = {
    "openrouter": _build_openrouter,
}


def build_reviewer(
    config: DecisionReviewConfig,
    entries: Mapping[str, Mapping[str, Any]],
    environ: Mapping[str, str] | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[TradeDecisionReviewer, dict[str, Any]]:
    entry = resolve_reviewer_entry(config, entries)
    reviewer = REVIEWER_PROVIDERS[entry["provider"]](
        config.reviewer, entry, os.environ if environ is None else environ, transport
    )
    return reviewer, entry


def default_review_path(data_dir: str | os.PathLike[str], mode: str) -> Path:
    return Path(data_dir) / "reviews" / f"{mode.lower()}.reviews.jsonl"


def build_stage(
    config: DecisionReviewConfig,
    *,
    mode: str,
    environ: Mapping[str, str] | None = None,
    entries: Mapping[str, Mapping[str, Any]] | None = None,
    record_path: str | os.PathLike[str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> DecisionReviewStage | None:
    """The stage for ``config``, or ``None`` when review is inactive (nothing changes).

    ``source: live`` builds the reviewer and checks its credential now (a missing
    ``OPENROUTER_API_KEY`` is a startup error, not a runtime surprise).  ``source: replay`` builds
    no reviewer and needs no credential or network.
    """
    if not config.active:
        return None
    source = config.source or "live"
    if source != config.source:
        from dataclasses import replace

        config = replace(config, source=source)
    record_store = (
        JsonlReviewStore(record_path)
        if record_path is not None and config.record_reviews else None
    )
    if source == "replay":
        return DecisionReviewStage(
            config, mode=mode, record_store=record_store,
            replay_store=JsonlReviewStore(read_paths=config.replay_from),
        )
    catalog = (
        entries if entries is not None else load_reviewer_entries(config.provider_catalog)
    )
    reviewer, entry = build_reviewer(config, catalog, environ, transport=transport)
    check = getattr(reviewer, "check_ready", None)
    if callable(check):
        check()
    cache = (
        JsonlReviewStore(config.cache_path) if config.cache_path else MemoryReviewStore()
    )
    return DecisionReviewStage(
        config, mode=mode, reviewer=reviewer, record_store=record_store, cache_store=cache,
        max_concurrent=int(entry.get("max_concurrent_requests", 16)),
        requests_per_minute=entry.get("requests_per_minute"),
    )
