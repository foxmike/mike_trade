# Alpaca Broker Adapter — Operator Guide

This document is the operator reference for the Alpaca integration in
`mike_trade`: which API is used, how environments and credentials stay
isolated, the retry and idempotency semantics, the capability matrix with its
current statuses and evidence, and how to run credential-gated paper
verification.

Implementation map (Phases E.1–E.4, read-only unless noted):

| Module | Responsibility |
| :--- | :--- |
| `mike_trade/brokers/alpaca/auth.py` | Environments, credentials, fingerprints, live authorization, Broker API rejection |
| `mike_trade/brokers/alpaca/http.py` | Bounded HTTP client, retry policy, error mapping, unknown-outcome errors |
| `mike_trade/brokers/alpaca/account.py` | Read-only account / positions / clock queries |
| `mike_trade/brokers/alpaca/assets.py` | Read-only asset lookup, eligibility, Decimal quantization |
| `mike_trade/brokers/alpaca/orders.py` | Order submit / lookup / replace / cancel, `client_order_id` scheme |
| `mike_trade/brokers/alpaca/activities.py` | Read-only activity normalization and idempotent fill ingestion |
| `mike_trade/brokers/alpaca/updates.py` | `trade_updates` stream normalization and reconnect handling |
| `mike_trade/brokers/alpaca/capabilities.py` | Capability matrix, promotion workflow, adapter conformance check |

## 1. Trading API vs Broker API

`mike_trade` uses the **Alpaca individual Trading API only**. The enterprise
**Broker API** (`broker-api.alpaca.markets` and any `broker-api.*` host) is
never used and is actively rejected:

- `AlpacaEnvironment.from_base_url()` raises `EnvironmentMismatchError` for any
  `broker-api.*` host.
- `AlpacaHttpClient` rejects a `broker-api.*` base URL at construction.

If you hold Broker API credentials, they are unusable here by design. Use
individual Trading API keys.

## 2. Paper vs live endpoints

Exactly two environments exist. There is no third environment and no fallback
between them.

| Environment | REST base URL | Trade stream URL |
| :--- | :--- | :--- |
| `PAPER` | `https://paper-api.alpaca.markets` | `wss://paper-api.alpaca.markets/stream` |
| `LIVE` | `https://api.alpaca.markets` | `wss://api.alpaca.markets/stream` |

Paper and live are separate accounts with separate credentials, separate
fingerprints, and separate state. A client constructed for one environment
refuses base URLs belonging to the other (`EnvironmentMismatchError`).

## 3. Credentials

Credential environment variables (no cross-environment fallback):

| Environment | Key variable | Secret variable |
| :--- | :--- | :--- |
| `PAPER` | `ALPACA_PAPER_KEY_ID` | `ALPACA_PAPER_SECRET_KEY` |
| `LIVE` | `ALPACA_LIVE_KEY_ID` | `ALPACA_LIVE_SECRET_KEY` |

Rules:

- Paper loads **only** from the paper variables; live loads **only** from the
  live variables. A missing variable raises; it never falls back to the other
  environment's variables.
- Secrets travel in the `APCA-API-SECRET-KEY` header and are **never logged**.
  Only the redacted header mapping (`***REDACTED***`) is safe for logs.
- `AlpacaCredentials.__repr__` / `__str__` expose the key id, environment, and
  fingerprint — never the secret.

### 3.1 Fingerprints and isolated state directories

`credential_fingerprint()` is a truncated SHA-256 over `environment:key_id`.
It identifies the account without exposing the secret and is the isolation
key for persisted state:

- State directories, journals, and ledgers are namespaced per
  `(environment, fingerprint)`. Paper and live state are never shared.
- `AlpacaHttpClient` accepts an optional `persisted_fingerprint` and refuses to
  start when it disagrees with the supplied credentials — this catches
  paper/live database mix-ups at startup.
- Every normalized object (account snapshots, positions, orders, activities,
  trade updates) carries its source `environment` and `fingerprint`; the asset
  and stream services reject objects whose scope does not match the session.

## 4. Live authorization requirement

LIVE clients and LIVE mutations require explicit, separate authorization:

- Constructing an `AlpacaHttpClient` with LIVE credentials requires
  `live_authorized=True`; otherwise `LiveAuthorizationRequired` is raised.
- `AlpacaOrderService` submit / replace / cancel on a LIVE service requires
  the underlying client to carry the live-authorized marker; test doubles must
  set it explicitly.
- The capability matrix additionally gates LIVE order flow (Section 8): a
  LIVE promotion requires a separate explicit operator-recorded entry. Paper
  verification never auto-promotes LIVE.

## 5. Rate limits and retry semantics

- HTTP 429 responses surface as `AlpacaRateLimited` carrying `retry_after`
  (parsed from `Retry-After`, including HTTP-date form) and
  `rate_limit_reset` (from `X-RateLimit-Reset`).
- **Reads are retried; mutations are not.** Only idempotent `GET` requests are
  retried with bounded exponential backoff plus jitter (`RetryPolicy`:
  `max_attempts`, `base_delay_seconds`, `max_delay_seconds`, `jitter_ratio`),
  honoring `Retry-After` / `X-RateLimit-Reset`. `POST`, `PATCH`, and `DELETE`
  are single-shot: they are never blindly retried.
- A timeout or connection loss on a non-idempotent request raises
  `AlpacaUnknownOutcome` (outcome UNKNOWN). The caller must resolve the
  outcome via `client_order_id` lookup (Section 6), never by re-sending the
  mutation.
- All requests carry explicit finite connect / read / write / pool timeouts.
  JSON bodies decode numbers to `Decimal`, never `float`.

## 6. `client_order_id` scheme and unknown-outcome resolution

Every submitted order carries a deterministic, audit-inspectable id, at most
48 characters:

```
mt1-<purpose>-<attempt-base36>-<32-hex-digest>
```

- `purpose` is a short code for the lineage (`en` entry, `ex` exit, `ps`
  protective stop, `ts` trailing stop, `mc` manual close, `se` safety exit,
  `ec` eod close).
- `attempt` is the base-36 retry attempt, directly inspectable.
- The digest is a truncated SHA-256 over length-prefixed canonical input
  `(mode, account fingerprint, cycle id, purpose, attempt)`, so ids are stable
  per logical attempt and distinct across accounts, cycles, purposes, and
  attempts.

Unknown-outcome resolution (no second POST):

1. The intended `client_order_id` is persisted **before** the POST
   (`persist_before_send` hook; a hook failure aborts the transport call).
2. If the POST outcome is unknown, the service looks the order up by
   `client_order_id`. If found, that order is the result (tracked while
   non-terminal). If lookup confirms absence, the service returns
   `NotSubmitted(retry_with_same_client_order_id=True)` — a retry reuses the
   same id, so at-most-once submission holds.
3. If the lookup itself fails, `OrderOutcomeUnresolved` is raised: the outcome
   genuinely remains unknown and must be reconciled, not retried.

Related guards: sell quantities are checked against the caller's authoritative
`known_position_qty` (`OversellRejected`); fractional and notional sizing
require a fractionable asset and DAY time-in-force; cancel is idempotent
(404/422 map to an already-terminal `CancelResult`).

## 7. `trade_updates` reconnect and gap handling

- The stream module sends only authentication and `listen` frames over an
  injected transport; it has no order-mutation operations and replays fixture
  JSON without a socket in tests.
- Authentication and the `trade_updates` subscription acknowledgement are
  verified; failures raise `StreamAuthError`.
- Every message is scope-checked: a mismatched environment or account
  fingerprint raises `EnvironmentMismatchError`.
- Unknown event names normalize to `TradeUpdateEvent.UNKNOWN` (`is_unknown`)
  instead of raising, so new broker events degrade visibly rather than
  crashing ingestion. `incremental_qty` is the execution quantity in the
  message; `cumulative_qty` is the order's `filled_qty`.
- Reconnects use a bounded policy (`ReconnectPolicy`: `max_attempts`,
  exponential delay with jitter). Every recovered disconnect yields an
  explicit `StreamReconnected` gap marker (`requires_reconciliation=True`)
  with `reconcile = (open_orders, orders_by_client_order_id,
  activities_since_last_seen)`: after any gap, REST reconciliation is required
  — the stream alone is never treated as complete.

## 8. Capability matrix

`mike_trade/brokers/alpaca/capabilities.py` defines the fail-closed matrix
keyed by **environment** (`PAPER` / `LIVE`) and **descriptor**:

- order type: `market`, `limit`, `stop`, `stop_limit`, `trailing_stop`
- order class: `simple`, `oto`, `oco`, `bracket`
- sizing: `whole` (whole-share qty), `fractional` (fractional qty), `notional`
- time-in-force: `day`, `gtc`, `opg`, `cls`, `ioc`, `fok`
- side: `buy`, `sell` (long exit), `short`
- operation: `submit`, `replace`

Each entry carries a status (`PROVEN`, `UNPROVEN`, `UNSUPPORTED`) plus an
evidence reference (doc URL or verification record id with date). **Any
combination not explicitly proven resolves to `UNPROVEN` and fails closed**:
`is_available()` returns `False` and `require()` raises
`CapabilityUnavailable` with the reason. Lookups never raise `KeyError` — the
matrix is complete over all 2,160 descriptors × 2 environments.

### 8.1 Current statuses

| Group | PAPER | LIVE | Evidence |
| :--- | :--- | :--- | :--- |
| Whole-share `market`/`limit`, `simple`, `day`/`gtc`, `buy`/`sell`, `submit` | PROVEN | PROVEN | Provider docs + operator record `OP-ALPACA-DOC-2026-001` (2026-10-08); pending paper verification |
| Standalone whole-share `trailing_stop` sell (`trail_price` or `trail_percent`), `simple`, `day`/`gtc`, `submit` | PROVEN | PROVEN | Provider docs + operator record `OP-ALPACA-DOC-2026-001` (2026-10-08); pending paper verification |
| Stop-only OTO; bracket / OCO; fractional stop or linked orders; fractional trailing stop; stop-price replace; trailing replace; OTO replace; fractional GTC; extended TIFs (`opg`, `cls`, `ioc`, `fok`); qty-based replace generally | UNPROVEN | UNPROVEN | None — fails closed |
| `short` side (any combination) | UNSUPPORTED | UNSUPPORTED | Structural: long-only desk |
| `trailing_stop` with `order_class` other than `simple` | UNSUPPORTED | UNSUPPORTED | Structural: standalone trailing stops only — never available |
| `notional` + `replace` (any combination) | UNSUPPORTED | UNSUPPORTED | Structural: Alpaca rejects notional replace |

### 8.2 Promotion workflow

- `record_paper_verification(capabilities, descriptor, record_id, verified_on,
  note)` returns a **new** matrix with only the PAPER entry promoted to
  `PROVEN` (evidence kind `paper_verification`). The LIVE entry is untouched
  and the input matrix is not mutated. It refuses structurally UNSUPPORTED
  descriptors.
- A LIVE promotion requires a separate explicit operator entry:
  `BrokerCapabilities.with_live_promotion(descriptor, evidence)` with
  `evidence.kind == "operator_record"` (paper or documentation evidence is
  rejected for LIVE). Structurally UNSUPPORTED descriptors can never be
  promoted in either environment.

### 8.3 Wiring into the order service

`BrokerCapabilities.as_legacy_checker(environment)` returns the
`Callable[[str], bool]` injected as `capability_check` into
`AlpacaOrderService.submit_stop_sell`, `submit_trailing_stop_sell`, and
`replace_order`. It maps the service's string capability names onto
descriptors; unknown names fail closed (`False`). Descriptor-level gating for
new code paths should call `is_available()` / `require()` directly.

## 9. Adapter conformance

`check_adapter_conformance(adapter)` accepts a single adapter or a mapping /
collection of services (the e.1–e.4 Alpaca services are separate objects) and
reports `{"ok", "present", "missing"}` over the required broker-port
operations: `account`, `positions`, `assets`, `clock`, `open_orders`,
`lookup_by_id`, `lookup_by_client_order_id`, `submit`, `replace`, `cancel`,
`activities`, `trade_update_stream`.

**Known gap (recorded, not edited):** no unified `AlpacaBrokerAdapter` class
exists yet — the account, asset, order, activity, and stream services are
separate objects behind the broker port. This is captured as an `xfail` test
(`test_unified_alpaca_adapter_exists`) rather than by modifying the e.1–e.4
modules. A future unified adapter must expose all twelve operations above
before that test can pass.

## 10. Paper verification procedure

The credential-gated integration test lives in
`tests/contracts/test_broker_capabilities.py::test_paper_readonly_account_clock_asset`
(marked `paper`, so it is deselected from default runs).

Run it only with operator-provided paper credentials and explicit opt-in:

```bash
export ALPACA_PAPER_KEY_ID='<paper key>'
export ALPACA_PAPER_SECRET_KEY='<paper secret>'
export MIKE_TRADE_ALPACA_PAPER_IT=1
python3 -m pytest tests/contracts/test_broker_capabilities.py -m paper -rs
```

What it does (read-only): asserts the client is PAPER-only (paper base URL,
no `broker-api` host), then exercises the real `AlpacaAccountService`
(`get_account`, `get_clock`) and `AlpacaAssetService` (`get_asset("AAPL")`)
against paper. It never references LIVE endpoints and never places, replaces,
or cancels orders. Without the opt-in variable and both paper credentials it
skips.

To record a paper verification outcome (e.g. after observing a paper fill for
a previously UNPROVEN combination), the operator calls
`record_paper_verification()` with the record id and date, and separately
records a LIVE `operator_record` only after reviewing the paper evidence. See
Section 8.2.

## 11. Mocks do not count as broker integration

Fixture fakes, `FakeClient` doubles, and replayed JSON establish contract
behavior only. They are not evidence of broker compatibility and can never
promote a capability: only explicit paper-verification records (PAPER) and
explicit operator records (LIVE) change the matrix.
