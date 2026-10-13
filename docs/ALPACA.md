# Alpaca Broker Setup — Operator Guide

This is the operator reference for the Alpaca integration **as
implemented**: account and key setup, endpoint isolation, env layout, the
Basic IEX feed and its provenance, the `trade_updates` stream, and the
order/protection capability matrix. Every `make` target and CLI command
named here exists in the `Makefile` / `mike_trade/cli.py`.

Implementation map (read-only unless noted):

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
| `mike_trade/market_data/alpaca_history.py`, `alpaca_stream.py` | Historical bars and the live stream; feed selection (`iex` default, `sip` only when entitled) |

No statement here claims profitability or live performance. Paper results
never prove live behavior.

## 1. Account and API key setup (paper and live)

1. Create **two separate** Alpaca individual-Trading-API accounts (or one
   account's paper + live pair): a **paper** account for verification and
   a **live** account reserved for later, operator-authorized live use.
   This product uses the **individual Trading API only** — enterprise
   Broker API credentials (`broker-api.*` hosts) are rejected at
   construction and can never be used here.
2. In each Alpaca dashboard, generate a key pair for that environment
   only. Paper keys authorize paper URLs; live keys authorize live URLs.
   Keys are never interchangeable across environments.
3. Install the keys as environment variables on the correct host only
   (see §4): paper variables on the paper host, live variables only on
   the live host, data credentials on any host that ingests market data.
   Never commit keys to git; never paste them into docs, issues, or chat.
4. Verify connectivity read-only before anything else:
   `make broker-check ACCOUNT=PAPER:main`,
   `make provider-check ACCOUNT=PAPER:main`,
   `make data-check ACCOUNT=SHADOW:default`. A failing check exits `5`
   and gates further steps.

## 2. Market-data feed: Basic IEX is not consolidated SIP

- The default Alpaca data feed is **Basic IEX** (`feed="iex"` in
  `mike_trade/market_data/alpaca_history.py` and
  `mike_trade/market_data/alpaca_stream.py`). **Basic IEX is exchange-only
  IEX data: it is not consolidated SIP data** and must never be mistaken
  for the full NBBO tape.
- Provenance is explicit in code: stream quotes/bars carry their feed id
  and `is_consolidated = (feed_id == "sip")`
  (`mike_trade/market_data/alpaca_stream.py`); the domain states that
  "Alpaca Basic IEX is represented with `is_consolidated=False` so IEX
  provenance is never mistaken for consolidated SIP data"
  (`mike_trade/domain/market.py`).
- `sip` is accepted only with an explicit entitlement flag
  (`sip_entitled=True`); without it the stream refuses to start with a
  configuration error. Any other feed name is rejected as unknown
  (expected: `iex` or `sip`).
- Operational consequence: signals, VWAP reference values, and fill
  estimates derived from IEX data reflect IEX-visible activity, not the
  consolidated market. Treat paper fills observed on this feed as
  feed-scoped evidence only (see §7).

## 3. Endpoint isolation (paper vs. live)

Exactly two environments exist. There is no third environment and no
fallback between them.

| Environment | REST base URL | Trade stream URL | Credential variables |
| :--- | :--- | :--- | :--- |
| `PAPER` | `https://paper-api.alpaca.markets` | `wss://paper-api.alpaca.markets/stream` | `ALPACA_PAPER_KEY_ID` / `ALPACA_PAPER_SECRET_KEY` |
| `LIVE` | `https://api.alpaca.markets` | `wss://api.alpaca.markets/stream` | `ALPACA_LIVE_KEY_ID` / `ALPACA_LIVE_SECRET_KEY` |

- Paper loads **only** from the paper variables; live loads **only** from
  the live variables. A missing variable raises; it never falls back to
  the other environment's variables
  (`AlpacaCredentials.from_env` in `mike_trade/brokers/alpaca/auth.py`).
- A client constructed for one environment refuses base URLs of the other
  (`EnvironmentMismatchError`). `broker-api.*` hosts are rejected.
- Persisted state is namespaced per `(environment, credential
  fingerprint)` where the fingerprint is a truncated SHA-256 over
  `environment:key_id` (no secret). The HTTP client accepts an optional
  `persisted_fingerprint` and refuses to start on mismatch — this catches
  paper/live database mix-ups at startup. Every normalized object carries
  its source environment and fingerprint; mismatched scope raises.
- Check isolation any time with `make broker-check` scoped to the intended
  account; it reports the environment it actually reached.

## 4. Env file layout and permissions

- **Layout (as implemented):** credentials come from the process
  environment, populated in development from the operator's normal repo
  `.env` (gitignored; copy `.env.example` to `.env` and fill in locally).
  Broker keys use the `ALPACA_*` names in §3. The repo template
  additionally documents `MIKE_TRADE_*` names (`MIKE_TRADE_CONFIG`,
  `MIKE_TRADE_MODE_CONFIG`, `MIKE_TRADE_DATA_DIR`,
  `MIKE_TRADE_DASHBOARD_BIND`, `MIKE_TRADE_ACCOUNT_MODE`, and the
  `MIKE_TRADE_ALPACA_*` aliases); the broker adapter itself reads the
  `ALPACA_*` names in §3, so keep both spellings in sync where the
  template uses them and never commit a populated `.env`.
- **Permissions:** the `.env` file must be owned by the operator account
  with mode `0600` (`chmod 600 .env`); state directories `0700` and
  database files `0600` (see `docs/SAFETY_AND_RISK.md` §10).
- **Per-mode separation:** shadow/dev hosts carry data credentials only;
  paper credentials live only on the paper host; live credentials live
  only on the live host. `MIKE_TRADE_ACCOUNT_MODE` cannot select live;
  live must be declared in a config file plus explicit CLI confirmation.
- **Deployment templates (t.1 artifacts):**
  `deploy/systemd/mike-trade-engine.service` and
  `deploy/systemd/mike-trade-dashboard.service` are optional future-use
  templates only — never copied, enabled, or started by this repository.
  When a future operator-approved deployment uses them, their
  `EnvironmentFile=` points at the operator's normal `.env` for that
  checkout (owned by the existing operator account, mode `0600`); the
  templates deliberately create no new logins, no separate secret stores,
  and embed no credentials.
- Secrets travel only in the `APCA-API-SECRET-KEY` header, are never
  logged, and never appear in `repr`. `make doctor` reports credential
  presence without printing secrets; `make config-show` redacts them.

## 5. `trade_updates` stream

`mike_trade/brokers/alpaca/updates.py` sends only authentication and
`listen` frames over an injected transport; it has no order-mutation
operations and replays fixture JSON without a socket in tests.

- Authentication and the `trade_updates` subscription acknowledgement are
  verified; failures raise `StreamAuthError`.
- Every message is scope-checked: a mismatched environment or account
  fingerprint raises `EnvironmentMismatchError`.
- Unknown event names normalize to `UNKNOWN` (`is_unknown`) instead of
  raising, so new broker events degrade visibly. `incremental_qty` is the
  message's execution quantity; `cumulative_qty` is the order's filled
  quantity.
- Reconnects use a bounded policy (`ReconnectPolicy`: `max_attempts`,
  exponential delay with jitter). Every recovered disconnect yields an
  explicit gap marker requiring REST reconciliation
  (`open_orders`, `orders_by_client_order_id`,
  `activities_since_last_seen`): after any gap the stream alone is never
  treated as complete.

## 6. Order/protection capability matrix

`mike_trade/brokers/alpaca/capabilities.py` is the fail-closed gate keyed
by **environment** (`PAPER` / `LIVE`) and descriptor: order type
(`market`, `limit`, `stop`, `stop_limit`, `trailing_stop`), order class
(`simple`, `oto`, `oco`, `bracket`), sizing (`whole`, `fractional`,
`notional`), time-in-force (`day`, `gtc`, `opg`, `cls`, `ioc`, `fok`),
side (`buy`, `sell`, `short`), operation (`submit`, `replace`). Each entry
is `PROVEN`, `UNPROVEN`, or `UNSUPPORTED` with evidence (doc URL or
verification record id with date). **Anything not explicitly proven is
`UNPROVEN` and fails closed** (`require()` raises
`CapabilityUnavailable`); lookups never raise `KeyError`. Paper evidence
never auto-promotes live; a live promotion needs a separate explicit
`operator_record` via `with_live_promotion()`.

| Combination | PAPER | LIVE | Meaning |
| :--- | :--- | :--- | :--- |
| Whole-share `market` / `limit`, `simple`, `day` / `gtc`, `buy` / `sell`, `submit` | supported (doc-proven, **pending paper verification**) | supported (doc-proven, **pending operator record**) | Baseline entry/exit path; live use still requires §8 prerequisites |
| Standalone whole-share `trailing_stop` sell (`trail_price` or `trail_percent`), `simple`, `day` / `gtc`, `submit` | supported (doc-proven, **pending paper verification**) | supported (doc-proven, **pending operator record**) | Native trailing protection path |
| Standalone whole-share `stop` sell, `simple`, `day` / `gtc`, `submit` | supported (doc-proven, **pending paper verification**) | supported (doc-proven, **pending operator record**) | Fixed-stop protection path |
| Stop-only `oto` | **blocked** (`UNPROVEN`) | **blocked** (`UNPROVEN`) | Fails closed until demonstrated in paper |
| `bracket` / `oco` (any combination) | **blocked** (`UNPROVEN`) | **blocked** (`UNPROVEN`) | Fails closed; standalone stops only |
| Fractional stop or linked orders; fractional trailing stop | **blocked** (`UNPROVEN`) | **blocked** (`UNPROVEN`) | Fails closed until demonstrated in paper |
| Fractional `market` / `limit` entry/exit | **blocked** (`UNPROVEN`) | **blocked** (`UNPROVEN`) | Fails closed until demonstrated in paper; additionally requires a fractionable asset and `DAY` |
| `notional` sizing with `submit` | **blocked** (`UNPROVEN`) | **blocked** (`UNPROVEN`) | Fails closed until demonstrated in paper |
| Stop-price / trailing (`trail_price` / `trail_percent`) / `oto` replace; quantity-based replace generally | **blocked** (`UNPROVEN`) | **blocked** (`UNPROVEN`) | Fails closed until demonstrated in paper |
| Extended TIFs (`opg`, `cls`, `ioc`, `fok`); fractional `gtc` | **blocked** (`UNPROVEN`) | **blocked** (`UNPROVEN`) | Fails closed until demonstrated in paper |
| `short_entry_sell` (short sale: whole-share `market`/`limit`, `simple`, `day`/`gtc`, side `short`) | **blocked** (`UNPROVEN`) | **blocked** (`UNPROVEN`) | Fails closed until a paper verification is recorded; LIVE is never configured for shorting. Whole shares only (fractional short sales are refused) |
| Standalone whole-share `stop` / `trailing_stop` **buy** (protective cover), `simple` | **blocked** (`UNPROVEN`) | **blocked** (`UNPROVEN`) | Short-side protection (`standalone_stop_buy_to_cover`, `standalone_trailing_stop_buy_to_cover`); a short needs it proven before it can be opened |
| `trailing_stop` with order class other than `simple` | **blocked** (`UNSUPPORTED`) | **blocked** (`UNSUPPORTED`) | Structural: standalone trailing stops only |
| `notional` + `replace` (any combination) | **blocked** (`UNSUPPORTED`) | **blocked** (`UNSUPPORTED`) | Structural: provider rejects notional replace |

Short-side position effect (`AlpacaOrderService.submit_order` /
`_check_position_effect`): Alpaca sends a short sale as `sell` and a cover as
`buy`, so the order service validates each order against the verified broker
position (`known_position_qty` magnitude + `known_position_side`): a short
entry requires a flat symbol, a cover requires a verified short and cannot
exceed it, a `sell` against a short and a `buy` entry against any position are
refused (never netted or reversed), and each requires the matching capability.
The app's Alpaca adapter queries the symbol's current position before **every**
order. A short entry additionally requires `execution.short_selling_enabled`
(PAPER only), a margin-enabled account that may short with buying power, a
`shortable` + `marginable` + easy-to-borrow asset (`borrow_status`, never a
stale or unverified value), and an injected borrowability/margin/protection
check. Borrow availability and fees that cannot be verified are never reported
as confirmed.

Fractional restrictions fail closed: fractional and notional sizing require
a fractionable asset and `DAY` time-in-force, sell quantities are checked
against authoritative `known_position_qty` (`OversellRejected`), and reads
are retried with bounded backoff while `POST`/`PATCH`/`DELETE` are
single-shot (a timeout raises `AlpacaUnknownOutcome`, resolved by
`client_order_id` lookup — `mt1-<purpose>-<attempt>-<digest>` — never by
blind re-POST).

## 7. Paper verification procedure (pending until performed by the operator)

The implemented credential-gated check is the `paper`-marked test in
`tests/contracts/test_broker_capabilities.py`
(`test_paper_readonly_account_clock_asset`):

```sh
export ALPACA_PAPER_KEY_ID='<paper key>'
export ALPACA_PAPER_SECRET_KEY='<paper secret>'
export MIKE_TRADE_ALPACA_PAPER_IT=1
python3 -m pytest tests/contracts/test_broker_capabilities.py -m paper -rs
```

What it does (read-only): asserts the client is PAPER-only (paper base
URL, no `broker-api` host), then exercises account, clock, and asset
(`AAPL`) reads against paper. It never touches live endpoints and never
places, replaces, or cancels orders. Without the opt-in variable and both
paper credentials it skips. Run the full gated selection with
`make test-paper`.

Recording outcomes: after observing a real paper fill for a previously
`UNPROVEN` combination, the operator records it with
`record_paper_verification()` (returns a **new** matrix with only the
PAPER entry promoted; the input is not mutated and LIVE is untouched).
Structurally `UNSUPPORTED` descriptors can never be promoted in either
environment.

**Status: paper verification is PENDING.** No paper order-flow
verification has been performed in this checkout, and this document does
not claim otherwise. Mocks, fixture fakes, and replayed JSON establish
contract behavior only and can never promote a capability.

**Warning: paper fills do not prove live performance.** Paper execution
quality, queue behavior, borrow/liquidity constraints, feed differences
(§2), and fee treatment differ from live. Treat paper evidence as
capability existence only — never as profitability, edge, or expected
live behavior.

## 8. Live enablement prerequisites

Live trading is a deliberate multi-gate sequence; each gate must pass in
order:

1. Paper verification for every capability the live book will use (§7),
   recorded as PAPER entries.
2. A separate explicit LIVE `operator_record` per capability via
   `with_live_promotion()` (paper or documentation evidence is rejected
   for live).
3. `make live-preflight ACCOUNT=LIVE:main` — refuses (exit `3`) on any
   failed check; nothing is enabled or started on failure.
4. `CONFIRM_LIVE=YES make live-enable ACCOUNT=LIVE:main` and
   `CONFIRM_LIVE=YES make live-start ACCOUNT=LIVE:main` — both re-run the
   preflight and require explicit confirmation. Live clients additionally
   require `live_authorized=True` in code.
5. Ongoing: `make health`, `make reconcile`, `make stops-audit`, and
   `make budget` monitoring; `make live-disable` (no confirmation hurdle)
   the moment live trading must halt.

Until all five steps are complete, live trading stays disabled and any
live order attempt fails closed.

## 8. PAPER / LIVE strategy execution and reconciliation

`mike_trade/live/` completes the Alpaca runtime. PAPER and LIVE now evaluate the same BUY/SELL
collections as SHADOW (inherited from `VwapReferenceLifecycle`; Michelle's rules are identical
in every mode) and route orders through `AlpacaBrokerAdapter`.

| Module | Responsibility |
| :--- | :--- |
| `live/gateway.py` | Synchronous Alpaca facade: consistent snapshots, executions (activity ids), stop submit/replace, attempt-numbered `client_order_id`s |
| `live/ledger.py` | Typed-repository accounting: cycle, intent, order, fill (activity id), stop state; external fills |
| `live/executor.py` | Port behind `CloseCoordinator`; establish / ratchet / resize / restore protective stops |
| `live/reconciler.py` | Startup + periodic + stream-triggered + pre-decision reconciliation |
| `live/lifecycle.py` | Strategy evaluation, entry/exit, EOD liquidation, safety exit |
| `live/stream.py` | `trade_updates` -> reconciler wake-up (advisory; REST/activities are the record) |
| `live/capability_records.py` | Operator capability evidence file (below) |

**Stop capabilities need operator evidence.** The default matrix proves only market/limit and
trailing-stop orders. Fixed and fixed-to-trailing (Michelle) protection needs a standalone
`stop` SELL (whole, GTC) and a `stop_price` replace. Record them in
`<data_dir>/alpaca_capabilities.json` after verifying on PAPER (see
`live/capability_records.py` for the format); a PAPER record never promotes LIVE, which needs an
`operator_record`. Without them the runtime refuses to start (`CAPABILITY_UNPROVEN:stop_submit`)
rather than trade unprotected.

**Protection.** After the broker reports a fill the engine places a GTC stop for the filled
quantity. Michelle (both strategies, per her confirmed clarification): the initial stop is the
confirmed BUY fill price - $0.25, rounded **up** to the tick; the trail is 0.2% of the highest
traded price since the fill (rounded down to the tick) and activates only when that tick-adjusted
candidate strictly exceeds the initial stop; the stop is only ever tightened.
A ratchet is a PATCH that is believed only once the broker shows the old order `replaced`; until
then the old stop is the protection (Alpaca's replace is not atomic and is never claimed to be).
A discretionary/EOD exit cancels and *confirms* the stop before selling and re-protects if the
sale fails. A protection that cannot be established or restored triggers a safety exit.

**Manual trades.** Broker state wins. A full external sale closes the cycle (recorded as
`ExternalFillApplied`, never a strategy win); a partial sale resizes the stop to the remaining
shares; an external buy is journaled and never adopted (`UNMANAGED_EXCESS`; a symbol held
outside the engine is never entered); a cancelled stop is re-placed; a pending exit cancelled by
a human's "close position" is reconciled without a second sale. Fills are keyed by Alpaca
activity id exactly once.

**EOD.** `execution.end_of_day_close` (`enabled`, `at`, default 15:55 ET) liquidates through the
normal broker path, starting `16:00 - at` before the actual close (early closes included),
blocks new entries from then on, cancels working entries, and never marks a position closed
without broker fills.

Known limitations: long-only (PAPER/LIVE shorts stay unverified/disabled); the stream does not
write fills (its execution id differs from the activity id); a fill whose activity record lags
beyond 2 minutes freezes entries for that symbol rather than being synthesized; Alpaca trailing-stop
quantity cannot be replaced, so native trailing coverage is resized by cancel + resubmit; REST
calls are synchronous.
