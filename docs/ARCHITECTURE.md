# mike_trade Architecture — Modular Single-Engine Product

No external requirements document is required to implement this architecture.
This document makes no claim about strategy profitability; strategies are
evaluated on correctness, determinism, and risk control, not expected returns.

## 1. Two-Process Topology

`mike_trade` is a modular single-engine product deployed as exactly two
operating-system processes on one Ubuntu host (primarily the Dell PowerEdge
R730, secondarily the development desktop):

1. **Trading engine process** — one single-engine Python 3.12+ process that
   owns market-data ingestion, candle/indicator computation, strategy
   evaluation, risk and lifecycle policy checks, order execution, protective
   coverage, control handling, and SQLite persistence. Exactly one engine
   instance writes to a given operational database.

2. **Dashboard process** — a separate read-only FastAPI process that serves a
   compiled React + TypeScript + Vite build (static assets only; no Vite
   development server in deployment). It exposes a read-only HTTP API and
   static UI for account KPIs, 1–10 symbol watch status, signals, positions,
   orders, protective/trailing state, eligibility limits, P&L, health,
   reconciliation, and recent trade detail. It remains reachable if the engine
   dies, independently determines OFFLINE from heartbeat age, and clearly marks
   stale last-known values. The dashboard never mutates state, never mutates
   configuration, and never places orders.

```
+-------------------------------------------------------+      +------------------------------------------+
|                 Trading Engine Process                |      |            Dashboard Process               |
|                 (single writer, one instance)         |      |            (separate OS process)           |
|                                                       |      |                                          |
|  [Market Data] -> [Candles/Indicators] -> [Strategies]|      |  [FastAPI Read-Only API]                 |
|       |                                        |      |      |  [Compiled React + TS + Vite UI]         |
|  [Broker Adapter] <- [Order Execution] <- [Risk Rules]|      |        (static build, no Vite dev server) |
|       |                                        |      |      |                   |                      |
|       +-------------------+--------------------+      |      |                   v                      |
|                           |                           |      |         [SQLite WAL Database]            |
|                           v                           |      |       (Read-Only Projections)            |
|                 [SQLite WAL Database]                 |      |                                          |
|                 (Authoritative Writer)                |------+--------------------->--------------------+
+-------------------------------------------------------+      +------------------------------------------+
```

Engine-to-dashboard communication is through SQLite WAL read projections and
a heartbeat row only. There is no shared in-process state, no RPC that
mutates engine state, and no dashboard-to-engine order path.

## 2. Package Map and Module Responsibilities

```
mike_trade/
  domain/          brokers/         market_data/      research/
  scheduling/      signals/         portfolio/        risk/
  execution/       persistence/     backtest/         reporting/
  controls/        dashboard/       observability/    config/
  strategies/      app.py           runtime.py        cli.py
  brokers/alpaca/  brokers/shadow/
tests/  docs/  docs/ADR/  config/  deploy/systemd/  state/ (gitignored)
```

| Module | Responsibility |
| :--- | :--- |
| `domain` | Immutable broker-neutral value objects and entities: instruments, market events, candles, contexts, strategy types, signals, decisions, orders, positions, fills, budgets, protection, accounts, sessions, errors, deterministic serialization. No broker, SDK, adapter, or I/O imports. |
| `strategies` | Independently configurable Python BUY and SELL strategy plugin classes (e.g. `vwap_bounce`, `vwap_reference`). Pure functions of shared warmed/fresh completed-bar `MarketContext` plus parameters. Broker-neutral policy code. |
| `market_data` | One shared `MarketDataService`/cache/fanout owning one authenticated live connection for the union of watched symbols; historical-bar acquisition and bounded REST polling fallback; freshness/provenance/gap/rate-limit tracking. Acquires each symbol once. |
| `research` | Optional offline research-data port and adapter plus reference symbol declarations. Only strategy-declared required inputs are fetched; missing mandatory inputs yield no signal. No paid provider required for basic operation. |
| `scheduling` | One internal regular-hours scheduler: session calendar, entry cutoff, candle-finalization triggers, due-time queue, bounded worker dispatch. No external cron/Celery/Kafka scheduler. |
| `signals` | Signal routing, deduplication per bar, priority tie-breaking, and translation of accepted signals into order intents. Keeps Signal distinct from decision, intent, broker order, and fill. |
| `portfolio` | Position-cycle lifecycle (one intentional long entry then one complete discretionary exit per cycle), account/scenario-level cash and buying-power views, symbol commitment/P&L views, persistent strategy rearm state. Symbol is never an account. |
| `risk` | Broker-neutral entry-eligibility policy code: per-symbol and total budgets, buying-power checks, planned-loss sizing, max open positions, daily loss guard, max filled entry cycles per symbol/session, consecutive-loss lockouts, post-sale cooldown, minimum hold for ordinary strategy exits only, regular-hours and entry-cutoff gates. |
| `execution` | Provider-neutral order manager above brokers: durable intents, exact lifecycle state machine, per-account+symbol serialization, submission idempotency via stable `client_order_id`, unknown-outcome recovery by lookup, exactly-once fill ingestion, protective-stop coordination. |
| `brokers/alpaca` | Concrete Alpaca Trading API adapter (individual equities API, not enterprise Broker API). Separate paper and live environments. Bounded HTTP, error mapping, capabilities matrix, activity/trade-update normalization. |
| `brokers/shadow` | Persistent simulated broker for SHADOW forward emulation with named scenarios on one shared live feed, plus deterministic backtest ledgers with no broker access. |
| `persistence` | SQLite WAL schema, forward-only versioned migrations, one authoritative writer, transactions, append-only event journal, repositories, read projections, integrity/backup/restore/maintenance utilities. |
| `backtest` | Deterministic historical replay engine over versioned bars with isolated run manifests. No broker access, no live credentials. |
| `reporting` | Trade journals, P&L/account/strategy reports, audit exports, evidence retention. Reads projections; never writes trade state except report artifacts. |
| `controls` | Durable idempotent operator control inbox and state: pause/resume, entry kill switch, guarded config changes, close/flatten coordination, reconciliation triggers. Fail-closed. |
| `dashboard` | FastAPI read-only API plus static compiled React + TypeScript + Vite hosting. Reads projections only. Never places orders or mutates configuration. Not a charting terminal. |
| `observability` | Structured JSON logging, secret redaction, health snapshots, metrics, heartbeat writer. |
| `config` | Versioned strictly-validated YAML settings with defaults, environment/CLI precedence, atomic audited mutations, redacted effective-config views. Secrets come from environment/OS-protected service config, never YAML. |
| `tests` | Unit, contract, integration, and deterministic replay tests. Targeted per task; no live orders or paid data in ordinary tests. |
| `docs` | Architecture, ADRs, operator and database docs. This file freezes module boundaries. |
| `deploy/systemd` | Systemd unit files and service lifecycle scripts. Units run the two processes separately. |
| `state/` (gitignored) | Local SQLite files, WAL/SHM sidecars, locks, logs, transient cache. Never committed. Per-mode/per-scenario database separation lives here by convention. |

## 3. Dependency Rules

This section is normative. Violations are architecture drift.

### 3.1 Broker neutrality

`domain`, `strategies`, `risk`, and portfolio/risk policy code are
broker-neutral and **must not import** broker adapters, market-data adapters,
persistence implementations, or vendor SDKs. Concretely, they must not import
from `mike_trade.brokers.alpaca`, `mike_trade.brokers.shadow`,
`mike_trade.execution` (concrete submitter), `mike_trade.persistence`
(concrete store), `mike_trade.dashboard`, or `alpaca`-vendor packages.

### 3.2 Ports and adapters

All replaceable infrastructure — brokerage, market data, optional research,
persistence, clocks, schedulers, execution modes — lives behind ports defined
as Python `Protocol` interfaces (e.g. `mike_trade/brokers/ports.py`,
`mike_trade/market_data/ports.py`, `mike_trade/research/ports.py`). Adapters
implement ports; policy code depends only on ports and `domain`.

### 3.3 Composition root

Only `app.py` / `runtime.py` may wire concrete adapters through dependency
injection. Only the composition root instantiates the Alpaca broker, shadow
broker, market-data service, research adapter, SQLite store, clock,
scheduler, portfolio, risk, execution, reporting, and control services, and
injects them into the runtime object graph. No other module constructs a
concrete broker, database, or feed. Importing modules, reading status, or
running tests must never place a live order.

### 3.4 Strategy isolation

Strategies never submit orders, never call brokers, never open data
connections, never open sockets, and never fetch data. They receive a shared
immutable `MarketContext` (completed bars, indicators, freshness/session
flags, provenance) and return an evaluation (`NO_SIGNAL`, `SIGNAL`,
`INSUFFICIENT_DATA`, `STALE_DATA`, `ERROR`, `NOT_DUE`). Data acquisition,
sizing, risk gating, and submission happen outside strategies.

### 3.5 Dashboard isolation

The dashboard runs as a separate process, reads SQLite projections only, and
never mutates engine state, configuration, or orders. There is no write path
from dashboard code to execution, brokers, controls inbox, or config
mutations.

### 3.6 Import-direction table

| Layer | May import | Must never import |
| :--- | :--- | :--- |
| `domain` | Python standard library only | Any `mike_trade` module, any third-party library |
| `strategies` | `domain`, standard library | `brokers`, `brokers/alpaca`, `brokers/shadow`, `execution`, `market_data` (impl), `persistence`, `dashboard`, vendor SDKs |
| `risk`, `portfolio` (policy) | `domain`, standard library, ports (`Protocol`s) | `brokers` adapters, `execution` (impl), `persistence` (impl), `dashboard`, vendor SDKs |
| `signals`, `scheduling` | `domain`, `config` models, ports | Broker/market-data concrete adapters, `dashboard` |
| `market_data` (impl), `research` (impl) | `domain`, their own `ports.py`, standard library + HTTP/WS client libs | `brokers`, `execution`, `strategies`, `dashboard` |
| `brokers/alpaca`, `brokers/shadow` (impl) | `domain`, `brokers/ports.py`, vendor SDK / stdlib | `strategies`, `execution` (impl), `market_data` (impl), `dashboard` |
| `execution` (impl) | `domain`, `brokers/ports.py`, `persistence` ports, standard library | `strategies`, `dashboard`, concrete broker classes (receives them via ports) |
| `persistence`, `backtest`, `reporting`, `controls` | `domain`, ports, standard library | Concrete broker adapters, `strategies`, `dashboard` impl |
| `dashboard` | `domain`, `persistence` read projections, `config` redacted views | `execution`, `brokers`, `strategies`, `controls` inbox writes, `market_data` live feed |
| `observability`, `config` | Standard library + declared typing/validation libs | `brokers`, `execution`, `strategies`, `dashboard` |
| `app.py`, `runtime.py` (composition root) | All modules (wires concretes to ports) | N/A — sole exception; must not contain strategy or risk math |

Enforcement intent: keep `domain`/`strategies`/`risk` importable without
broker credentials, network, or database; keep `dashboard` importable without
execution/broker capability.

## 4. Data Flow

1. **Acquire once per symbol.** One `MarketDataService` owns one authenticated
   live connection for the union of watched symbols/channels. Each
   symbol/channel is subscribed once and fanned out as immutable events to
   every live/paper/shadow consumer. Bounded REST polling fallback fetches
   once per symbol/cycle and respects rate limits. Strategies never poll.
2. **Centralized candles/indicators.** Closed-bar finalization (e.g. 1-minute
   regular-session bars) and pure indicators (VWAP, EMA, RSI, ATR, volume) are
   computed once centrally and published as an immutable `MarketContext` with
   feed provenance (IEX vs SIP), source/receive timestamps, freshness, and
   session flags. IEX provenance is preserved because IEX is not consolidated
   SIP.
3. **One internal scheduler.** A single in-engine scheduler fires on completed
   bars during regular hours, enforces entry cutoff and optional
   end-of-day-close (disabled by default), and dispatches due evaluations.
4. **Bounded concurrent strategy evaluation.** A bounded worker pool evaluates
   independently registered BUY/SELL plugins against the shared context;
   duplicate signals on the same bar are suppressed; evaluations never block
   the feed.
   Each symbol owns independent BUY and SELL strategy collections (any number
   per side, SELL optional). All strategies of a symbol receive the same
   immutable `MarketContext`; flat symbols run entry arbitration and held
   symbols run exit arbitration (`signals/decisions.py`, pure functions shared
   by the SHADOW lifecycle and the historical `BacktestRunner`). Protective
   stops are a third, independent mechanism: they are armed after the
   confirmed fill, evaluated before any SELL strategy, and work with an empty
   SELL collection.
5. **Serialized mutations through a single writer.** Signal routing, budget
   reservation, and per-account+symbol order serialization funnel into one
   authoritative SQLite writer / execution coordinator. Account/portfolio/order
   mutations are serialized so concurrent strategies cannot double-spend cash,
   oversell quantity, or race protective stops. Only `app.py`/`runtime.py`
   construct this writer.

On feed loss, stale/gapped data, or DEGRADED health, new entries fail closed
while broker fill/order-update ingestion continues on its separate channel.

## 5. Execution Modes

Four distinct modes with strict credential/ledger isolation. Cross-mode
credentials, account IDs, state directories, databases, positions, orders,
balances, limits, and ledgers must never leak or be shared.

| Mode | Feed | Broker access | Ledger | Isolation |
| :--- | :--- | :--- | :--- | :--- |
| `BACKTEST` | Deterministic historical bars only | None — no broker access, no credentials loaded | Ephemeral/isolated run manifest | Separate run key; cannot read live/paper/shadow state |
| `SHADOW` | Live shared feed (same cache as paper/live) | None — simulated broker only | Persistent per-scenario simulated ledger; multiple named scenarios may run concurrently on one shared stream | Per-scenario account/scenario key + database/ledger separation |
| `PAPER` | Live shared feed | Alpaca Paper (`https://paper-api.alpaca.markets`) with paper credentials only | Broker paper account + local paper ledger mirror | Paper endpoint/keys/DB/ledger separated from live and shadow |
| `LIVE` | Live shared feed | Alpaca Live (`https://api.alpaca.markets`) with live credentials only | Broker live account + local live ledger mirror | Live endpoint/keys/DB/ledger separated from all other modes |

**LIVE is explicitly gated.** Nothing in this architecture authorizes live
orders by itself. Live requires explicit operator enablement (explicit
live-mode selection plus `MIKE_TRADE_ENABLE_LIVE_TRADING=1`-style
confirmation), preflight, reconciliation, risk-cap review, broker
fingerprint/account verification, and stop/fractional capability checks.
Live-mutating CLI commands require explicit live confirmation. Tests, imports,
status reads, backtests, and shadow runs must never load or use live
credentials.

## 6. Persistence

- **Engine:** SQLite with Write-Ahead Logging
  (`PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;`), foreign keys on,
  indexed UTC timestamps, transactions for atomic reservation/fill/cycle
  updates.
- **One authoritative writer:** exactly one engine process holds the write
  connection per operational database. Dashboard, reports, and CLI status read
  via read-only WAL projections without lock contention.
- **Migrations:** forward-only versioned SQL migrations (e.g.
  `mike_trade/persistence/migrations/0001_initial.sql` onward) applied in
  order with integrity checks; no destructive rewrites of trade history.
- **Append-only trade-affecting events:** signals, decisions, intents, order
  events, fills (keyed by unique external activity ID), protection orders,
  trailing state/events, budget reservations, cash events, equity snapshots,
  reconciliation runs, control commands, and config fingerprints are appended,
  never updated in place. Failed journal writes must not permit unrecorded
  accepted intent. Broker activity ingestion is idempotent.
- **Separation:** LIVE, PAPER, each named SHADOW scenario, and each BACKTEST
  run use explicit account/scenario/run keys; cross-mode writes are forbidden.
  No bulk raw tick archive; the cache keeps only latest quotes/trades,
  decision evidence, candle inputs, completed candles, rolling indicator state,
  and freshness metadata.
- **Backups:** backup/restore checks and maintenance utilities preserve
  ledger isolation and detect corruption.

## 7. Fail-Closed Entry Policy and Broker Truth

- **Fail-closed entries:** new entries are blocked (while fills, stops,
  reconciliation, manual closes, and safety exits remain enabled) on
  stale/unknown data, DEGRADED feed, unresolved/unknown orders, unreconciled
  broker state, unknown stop state, unsafe account flags, insufficient buying
  power, unsupported fractional/order combinations, or missing mandatory
  research inputs. Shutdown never implicitly liquidates or cancels valid
  broker-held protective stops. Startup reconciles broker account, positions,
  orders, fills, stops, budgets, and persistent strategy state before
  permitting new entries. External positions are UNMANAGED until explicit
  adoption/handling.
- **Broker truth is authoritative:** for live orders/positions the broker is
  authoritative. Local/broker discrepancies produce explicit reconciliation
  drift and block new entries until resolved; history is never silently
  rewritten. Every automated live long position requires broker-held
  protective coverage when the configured provider capability supports it;
  protection begins after confirmed positive fill, covers actual remaining
  quantity, never moves downward for a long, and is coordinated with
  discretionary exits so overlapping sells cannot create a short.

## 8. Non-Goals

Explicitly out of scope unless separately approved:

- No Kafka, Celery, Redis, RabbitMQ, or other generic message brokers.
- No Kubernetes, Helm, or multi-node container orchestration.
- No generic rules engines, DSLs, visual strategy builders, or throwaway
  untyped scripts; strategies are typed Python plugin classes.
- No Makefile business logic; recipes delegate to application services/CLI.
- No professional charting terminal; the dashboard is an operational status
  UI, not a charting product.
- No configuration-history/diff/preview product beyond an audited
  SHA-256/redacted effective-config fingerprint.
- No full-session raw tick recording; no bulk raw tick archive.
- No options, futures, crypto/forex; no automated stock screening; no
  mandatory ML/LLM decision-path inference; no browser broker automation; no
  exchange/microsecond smart routing; no capital transfers; no mandatory
  notifications; no earnings/economic blackout automation; no automatic live
  short selling (initial active trading is long-only; type/capability
  boundaries may anticipate future short/cover without enabling it).
