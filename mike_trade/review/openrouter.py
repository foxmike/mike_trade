"""OpenRouter reviewer provider - the first working :class:`TradeDecisionReviewer`.

JEV is reached **only through OpenRouter** (``https://openrouter.ai/api/v1/chat/completions``)
using the existing ``OPENROUTER_API_KEY`` environment variable; there is no JEV-specific endpoint,
account or key.  The model identifier is configuration (default ``typesafe/jev-router``, the
identifier listed as "TypeSafe: Jev Router" in OpenRouter's public model catalog), and any other
OpenRouter model can be named instead.  No additional LLM framework is used: this is a plain
``httpx`` call to OpenRouter's OpenAI-compatible chat-completions API.

The model is constrained to the :data:`~mike_trade.review.reviewer.REVIEW_RESPONSE_SCHEMA` JSON
schema (``response_format: json_schema`` with ``provider.require_parameters`` so a provider that
cannot honour it is not used) and its answer is parsed strictly; free text is never interpreted.
OpenRouter's ``usage`` object (tokens and ``cost``) and the model that actually served the request
are recorded.  ``typesafe/jev-router`` is a *router*: the underlying model can differ between
requests, which is why the served model is stored on every record.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from mike_trade.domain.errors import ConfigurationError
from mike_trade.review.reviewer import (
    REVIEW_PROTOCOL,
    REVIEW_RESPONSE_SCHEMA,
    MalformedReviewResponse,
    ReviewerIdentity,
    ReviewerUnavailable,
    ReviewResponse,
    ReviewUsage,
    canonical_json,
    parse_review_payload,
)

__all__ = [
    "OPENROUTER_API_KEY_ENV",
    "OPENROUTER_BASE_URL",
    "DEFAULT_JEV_MODEL",
    "PROMPT_VERSION",
    "SYSTEM_PROMPT",
    "OpenRouterReviewer",
]

#: The existing environment variable holding the OpenRouter credential (value never logged).
OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
#: Verified against OpenRouter's public model catalog ("TypeSafe: Jev Router").
DEFAULT_JEV_MODEL = "typesafe/jev-router"
PROMPT_VERSION = "1"

SYSTEM_PROMPT = (
    "You are a discretionary trade reviewer for an automated equity trading system. A strategy "
    "(or several) proposed one action - BUY to open a long position, or SELL to close one - and "
    "the system asks you to APPROVE or REJECT that proposal before any order is placed.\n"
    "You receive one JSON document with: the proposal; each originating strategy's structured "
    "evidence (the facts that actually caused its signal - model class probabilities and "
    "features, opening-range or VWAP levels, and so on, whatever that strategy reports); recent "
    "completed OHLCV candles; indicators; the quote and last trade; time of day and session "
    "position; for a SELL, the open position, its entry price, unrealized P&L and its existing "
    "protective stop; and data provenance and timestamps.\n"
    "Rules:\n"
    "- Judge only from the document. It contains everything known at the decision time. Do not "
    "use any information about events after that time, and do not assume news that is not in the "
    "document.\n"
    "- Do not treat a model probability or any single number as a pass/fail threshold; weigh the "
    "evidence against the market context. Strategy confidence has not been shown to predict "
    "profit.\n"
    "- A protective stop stays in force whatever you decide; you cannot place, move or cancel "
    "orders.\n"
    "- Reply with a single JSON object and nothing else: {\"decision\": \"APPROVE\" or "
    "\"REJECT\", \"reason\": a concise explanation grounded in the supplied data, "
    "\"observations\": a list of short factual observations you relied on}."
)


class OpenRouterReviewer:
    """Review proposals with any OpenRouter chat model (default: JEV)."""

    def __init__(
        self,
        *,
        reviewer_name: str = "jev",
        model: str = DEFAULT_JEV_MODEL,
        api_key_env: str = OPENROUTER_API_KEY_ENV,
        environ: Mapping[str, str] | None = None,
        base_url: str = OPENROUTER_BASE_URL,
        temperature: float | None = 0.0,
        max_tokens: int | None = None,
        response_format: str = "json_schema",
        point_in_time_safe: bool = False,
        http_referer: str | None = None,
        app_title: str | None = "mike_trade",
        http_timeout_seconds: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ConfigurationError("the OpenRouter model identifier must be configured",
                                     code="REVIEWER_MODEL_MISSING")
        if response_format not in ("json_schema", "json_object", "none"):
            raise ConfigurationError(
                f"response_format must be json_schema, json_object or none; got {response_format!r}",
                code="REVIEWER_INVALID",
            )
        self._model = model.strip()
        self._api_key_env = api_key_env
        self._environ = os.environ if environ is None else environ
        self._base_url = base_url.rstrip("/")
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._response_format = response_format
        self._http_referer = http_referer
        self._app_title = app_title
        self._http_timeout = http_timeout_seconds
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._identity = ReviewerIdentity(
            provider="openrouter",
            reviewer=reviewer_name,
            model=self._model,
            protocol=REVIEW_PROTOCOL,
            settings={
                "temperature": temperature,
                "max_tokens": max_tokens,
                "response_format": response_format,
                "prompt_version": PROMPT_VERSION,
                "prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
            },
            point_in_time_safe=point_in_time_safe,
        )

    @property
    def identity(self) -> ReviewerIdentity:
        return self._identity

    # ------------------------------------------------------------------ readiness

    def check_ready(self) -> None:
        """Startup check: the credential variable is set (the value is never inspected further)."""
        if not self._environ.get(self._api_key_env):
            raise ConfigurationError(
                f"environment variable {self._api_key_env} is not set; the OpenRouter reviewer "
                "cannot run without it (use source: replay for recorded reviews)",
                code="REVIEWER_CREDENTIAL_MISSING",
                context={"env": self._api_key_env},
            )

    # ------------------------------------------------------------------ request

    def build_payload(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """The exact JSON body sent to OpenRouter (public so tests and docs can show it)."""
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": canonical_json(request)},
            ],
            "stream": False,
            "usage": {"include": True},
        }
        if self._temperature is not None:
            payload["temperature"] = self._temperature
        if self._max_tokens is not None:
            payload["max_tokens"] = self._max_tokens
        if self._response_format == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "trade_review",
                    "strict": True,
                    "schema": REVIEW_RESPONSE_SCHEMA,
                },
            }
            payload["provider"] = {"require_parameters": True}
        elif self._response_format == "json_object":
            payload["response_format"] = {"type": "json_object"}
            payload["provider"] = {"require_parameters": True}
        return payload

    def _headers(self) -> dict[str, str]:
        key = self._environ.get(self._api_key_env)
        if not key:
            raise ReviewerUnavailable(f"{self._api_key_env} is not set")
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        if self._http_referer:
            headers["HTTP-Referer"] = self._http_referer
        if self._app_title:
            headers["X-Title"] = self._app_title
        return headers

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url, timeout=self._http_timeout, transport=self._transport
            )
        return self._client

    # ------------------------------------------------------------------ review

    async def review(self, request: Mapping[str, Any]) -> ReviewResponse:
        headers = self._headers()
        try:
            http_response = await self._http().post(
                "/chat/completions", headers=headers, json=self.build_payload(request)
            )
        except httpx.TimeoutException as exc:
            raise ReviewerUnavailable(f"OpenRouter request timed out: {exc!r}") from exc
        except httpx.HTTPError as exc:
            raise ReviewerUnavailable(f"OpenRouter transport error: {type(exc).__name__}") from exc
        if http_response.status_code != 200:
            raise ReviewerUnavailable(
                f"OpenRouter returned HTTP {http_response.status_code}: "
                f"{self._error_text(http_response)}"
            )
        try:
            body = http_response.json()
        except ValueError as exc:
            raise MalformedReviewResponse("OpenRouter returned a non-JSON body") from exc
        return self._parse(body)

    @staticmethod
    def _error_text(response: httpx.Response) -> str:
        try:
            error = response.json().get("error")
            if isinstance(error, Mapping):
                return str(error.get("message", ""))[:300]
        except (ValueError, AttributeError):
            pass
        return response.text[:300]

    def _parse(self, body: object) -> ReviewResponse:
        if not isinstance(body, Mapping):
            raise MalformedReviewResponse("OpenRouter body is not a JSON object")
        if "error" in body and not body.get("choices"):
            error = body["error"]
            message = error.get("message") if isinstance(error, Mapping) else error
            raise ReviewerUnavailable(f"OpenRouter reported an error: {str(message)[:300]}")
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            raise MalformedReviewResponse("OpenRouter response has no choices")
        choice = choices[0]
        if choice.get("finish_reason") == "length":
            raise MalformedReviewResponse("the reviewer's answer was truncated (finish_reason=length)")
        message = choice.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, str) or not content.strip():
            raise MalformedReviewResponse("the reviewer returned no message content")
        try:
            payload = json.loads(content)
        except ValueError as exc:
            raise MalformedReviewResponse("the reviewer's answer is not valid JSON") from exc
        served = body.get("model")
        version = f"{self._model}|served:{served or 'unknown'}|{REVIEW_PROTOCOL}|prompt:{PROMPT_VERSION}"
        return parse_review_payload(
            payload, reviewer_version=version, usage=self._usage(body, served)
        )

    @staticmethod
    def _usage(body: Mapping[str, Any], served: object) -> ReviewUsage:
        usage = body.get("usage")
        usage = usage if isinstance(usage, Mapping) else {}

        def count(name: str) -> int | None:
            value = usage.get(name)
            return value if isinstance(value, int) and not isinstance(value, bool) else None

        cost: Decimal | None = None
        raw_cost = usage.get("cost")
        if isinstance(raw_cost, (int, float, str)) and not isinstance(raw_cost, bool):
            try:
                cost = Decimal(str(raw_cost))
            except InvalidOperation:
                cost = None
        provider = body.get("provider")
        return ReviewUsage(
            input_tokens=count("prompt_tokens"),
            output_tokens=count("completion_tokens"),
            total_tokens=count("total_tokens"),
            cost=cost,
            served_model=served if isinstance(served, str) else None,
            generation_id=body.get("id") if isinstance(body.get("id"), str) else None,
            served_by=provider if isinstance(provider, str) else None,
        )

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()
