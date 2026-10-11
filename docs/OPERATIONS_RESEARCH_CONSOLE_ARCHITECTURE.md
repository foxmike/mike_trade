# Operations & Research Console: Technical Architecture

**Project:** Mike_Trade  
**Document:** `docs/OPERATIONS_RESEARCH_CONSOLE_ARCHITECTURE.md`  
**Status:** Approved Technical Architecture  
**Repository Path:** `/home/mike/dev/mike/stocks/python/mike_trade`  

---

## 1. Architectural Principles & System Boundaries

The Mike_Trade Operations and Research Console architecture adheres to strict systems design principles to guarantee operational safety, data integrity, and high-performance analytical throughput:

```
+-------------------------------------------------------------------------------------------------------+
|                                  OPERATIONS & RESEARCH CONSOLE ARCHITECTURE                           |
+-------------------------------------------------------------------------------------------------------+
|                                                                                                       |
|  [ BROWSER CLIENT (React 19 / TypeScript / Vite / Lightweight Charts) ]                               |
|        │                                            ▲                                                 |
|        │ HTTP REST (JSON)                           │ SSE / Streaming Events                          |
|        ▼                                            │                                                 |
|  [ FASTAPI BACKEND GATEWAY (mike_trade/dashboard/server.py) ]                                         |
|    ├── Auth & Rate Limiting Middleware                                                                |
|    ├── Route Handlers (/api/v2/operations, /api/v2/research, /api/v2/trades, /api/v2/controls)       |
|    └── Background Event Broadcaster (SSE)                                                             |
|        │                                    │                                 │                       |
|        ▼                                    ▼                                 ▼                       |
|  [ COMMAND GATEWAY ]             [ OPERATIONAL QUERY ]             [ RESEARCH CATALOG SERVICE ]       |
|    │                                (ReadStore WAL)                   (DuckDB + Parquet Cache)        |
|    │ submits ControlCommand                 │                                 │                       |
|    ▼                                        ▼                                 ▼                       |
|  [ SQLite: operator_commands ]   [ SQLite: state/dev/shadow.db ]   [ research/data/catalog.duckdb ]   |
|        │                                                              [ research/data/snapshots/*.pq ] |
|        │                                                              [ research/**/*.results.json ]  |
|        ▼ (Engine drains)                                                                              |
|  [ TRADING ENGINE LOOP ] ──writes completed 1m bars──► [ SQLite: market_bars ]                        |
|                                                                                                       |
+-------------------------------------------------------------------------------------------------------+
```

### 1.1 Core Principles

1. **Single Authoritative Execution Path:**
   The console **never places orders directly with brokers** and never bypasses the trading engine. All operator actions (close position, flatten, pause, disable entries) are submitted as transactional `ControlCommand` records to the SQLite `operator_commands` table. The running engine drains this inbox, evaluates risk rules, coordinates order lifecycles, and executes via its authoritative brokers.
2. **Strict Read/Write Boundary Separation:**
   - **Read Path:** The console queries SQLite exclusively through `ReadStore` (`mode=ro` URI, `PRAGMA query_only=ON`, write-denying authorizer) with one `BEGIN DEFERRED` snapshot per request. It never acquires writer locks and never blocks the engine.
   - **Write Path:** The console writes solely through the dedicated `CommandGatewayService`, which opens a bounded `WriterSession` only to enqueue a command into `operator_commands` and `domain_events`.
3. **Decoupled Engine & Console Lifecycles:**
   The console runs as an independent system process (`mike-trade-api`). If the trading engine stops, crashes, or is restarted, the console remains fully operational, serving historical research, inspecting past trade ledgers, and accurately reporting the engine as `OFFLINE`.
4. **Push-Down Research Queries & Lazy Loading:**
   Research datasets span tens of millions of historical bars and thousands of runs. The console never loads entire Parquet tables or ledgers into memory. Queries push down column projections and date boundaries directly into DuckDB and SQLite indexes. Candlestick bars are loaded lazily on demand.

---

## 2. Backend Services Architecture

The backend lives inside the existing `mike_trade/dashboard/` package, organized into focused, single-responsibility services:

### 2.1 Backend Component Map

```
mike_trade/dashboard/
├── api.py                    # Legacy v1 read-only routes (preserved for compatibility)
├── server.py                 # FastAPI application factory, middleware, static SPA host
├── service.py                # High-level ConsoleService facade coordinating subsystems
├── v2/
│   ├── router.py             # Root /api/v2 APIRouter aggregating sub-routers
│   ├── operations.py         # Account KPIs, positions, active orders, watchlist routes
│   ├── research.py           # Research run catalog, multi-symbol filter, comparisons
│   ├── trades.py             # Unified trade inspection, fills, events, excursion math
│   ├── charts.py             # Chunked candlestick bars (1m to 1d), VWAP, overlays
│   ├── controls.py           # Authenticated operator command routes (POST)
│   ├── market_status.py      # Real-time state, benchmark ETF metrics, transition log
│   └── events.py             # Server-Sent Events (SSE) broadcaster for live ticks
├── research_catalog.py       # DuckDB run indexer, metadata cache, dynamic attribution
└── command_gateway.py        # Validated gateway submitting commands to ControlInbox
```

### 2.2 Subsystem Responsibilities

#### 1. `OperationalQueryService`
- **Responsibility:** Executes high-performance, read-only queries against SQLite WAL for an active `account_scope_id`.
- **Implementation:** Wraps existing `ReadStore` and `mike_trade.persistence.projections` readers (`kpis`, `positions`, `orders`, `watchlist`, `reconciliation_status`).
- **Optimization:** Caches invariant metadata (symbol settings, account labels) and reuses connection pools across requests.

#### 2. `ResearchCatalogService` & Dynamic Attribution Engine
- **Responsibility:** Ingests, indexes, and provides sub-second search over thousands of backtest runs stored in `research/`.
- **Cache Store:** Maintains an embedded DuckDB table `research_runs_index` in `research/data/catalog.duckdb` (or a local SQLite index file).
- **Dynamic Attribution Restorer:**
  - When loading legacy backtest runs where `cycles` lack strategy names or exit reasons, correlates entry/exit fills by `(symbol, timestamp)` against the `signals` and `intents` lists in `results.json`.
  - Maps registered strategies from `manifest["strategies"]` using the 192-strategy `StrategyRegistry`.
  - Reconstructs complete attribution records on the fly without mutating immutable JSON files on disk.

#### 3. `MarketBarStreamingHook` (Engine Hook)
- **Responsibility:** Closes the gap where completed 1-minute bars were never persisted.
- **Implementation:** A lightweight listener attached to `CandleBuilder` / `StrategyScheduler` in the engine. When a 1-minute candle transitions to `CLOSED`:
  ```python
  def on_candle_closed(candle: Candle) -> None:
      writer.enqueue_bar(candle)
  ```
- **Performance:** Enqueues bars to a thread-safe ring buffer drained in batches (e.g. every 5 seconds) into `market_bars` within the engine's normal WAL transactions.

#### 4. `CommandGatewayService`
- **Responsibility:** Validates incoming operator requests, checks engine liveness, and submits typed commands to `ControlInbox`.
- **Contract:**
  - Instantiates `ControlCommand(type=..., symbol=..., reason=..., issued_by=..., live_confirmed=...)`.
  - Invokes `inbox.submit(command)`.
  - Returns immediate submission acknowledgement with `command_id` and tracking URI `/api/v2/controls/commands/{command_id}`.

#### 5. `HealthDiagnosticsService`
- **Responsibility:** Aggregates system health metrics with sub-millisecond latency.
- **Decoupled Backup Probing:**
  - Replaces the synchronous 5-second `verify_backup` call in `api.py`.
  - Reads the timestamp and SHA256 from the backup manifest file on disk.
  - If a status file exists (`backup-status.json`), parses it; if absent and not configured, cleanly reports `NOT_CONFIGURED` without raising false error alerts.

---

## 3. API Contract Specification (REST & SSE)

All responses conform to strict Pydantic v2 schemas. Monetary values, quantities, and prices serialize as JSON strings to avoid floating point truncation. Timestamps are timezone-aware ISO-8601 strings in UTC, accompanied by `*_et` siblings formatted in `America/New_York`.

### 3.1 Operations API (`/api/v2/operations`)

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/v2/accounts` | List registered account scopes with execution mode, scenario, and label. |
| `GET` | `/api/v2/accounts/{account_id}/cockpit` | High-level operations cockpit payload (KPIs, active positions, alerts, liveness). |
| `GET` | `/api/v2/accounts/{account_id}/positions` | Open and exiting positions with unrealized P&L, stop distance, and cycle IDs. |
| `GET` | `/api/v2/accounts/{account_id}/orders` | Active and historical broker orders with order intent attribution. |
| `GET` | `/api/v2/accounts/{account_id}/watchlist` | Monitored symbols, active strategies, cooldowns, and loss streak limits. |
| `GET` | `/api/v2/accounts/{account_id}/signals` | Real-time signal stream and arbitration decisions (accepted & rejected). |

### 3.2 Research API (`/api/v2/research`)

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/v2/research/runs` | Server-side paginated list of backtest runs with search, filtering, and sorting. |
| `GET` | `/api/v2/research/runs/{selection_id}` | Detailed experiment dossier (configuration, trade distribution, equity curve). |
| `POST` | `/api/v2/research/runs/compare` | Multi-run comparison endpoint accepting 2–5 `selection_id`s, returning comparative metrics and overlaid normalized equity curves. |
| `GET` | `/api/v2/research/datasets` | Inventory of historical Parquet snapshots and DuckDB catalog status. |
| `POST` | `/api/v2/research/datasets/verify` | Trigger asynchronous SHA256 integrity verification across dataset files. |

### 3.3 Trade Explorer & Chart API (`/api/v2/trades`)

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/v2/trades` | Unified trade list across operational accounts (`?source=account&account_id=...`) or backtests (`?source=backtest&run_id=...`). |
| `GET` | `/api/v2/trades/{trade_id}` | Complete trade inspection (fills, events, MFE/MAE diagnostics, attribution). |
| `GET` | `/api/v2/trades/{trade_id}/candles` | Chunked historical candlestick bars for chart rendering with requested resolution (`1m`, `5m`, `15m`, `1h`, `1d`) and time bounds. |
| `GET` | `/api/v2/trades/{trade_id}/events` | Authoritative trade lifecycle events (signals, intents, fills, stop adjustments). |

### 3.4 Market Status API (`/api/v2/market-status`)

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/v2/market-status/current` | Active state (`NORMAL` to `RECOVERY`), ETF metrics (SPY, QQQ, IWM), and entry policy. |
| `GET` | `/api/v2/market-status/history` | Historical audit log of state transitions with trigger reasons and metric snapshots. |
| `GET` | `/api/v2/market-status/candles` | Benchmark ETF candles with market status state overlays for charting. |

### 3.5 Authenticated Controls API (`/api/v2/controls`)

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/v2/controls/positions/close` | Request closing a single open position at market. |
| `POST` | `/api/v2/controls/positions/close-all` | Emergency close of all managed open positions. |
| `POST` | `/api/v2/controls/positions/flatten` | Emergency liquidation of all positions and cancellation of open orders. |
| `POST` | `/api/v2/controls/engine/pause` | Pause/resume evaluation of new entry signals. |
| `POST` | `/api/v2/controls/engine/entries` | Enable/disable entry kill switch. |
| `POST` | `/api/v2/controls/engine/stop` | Gracefully drain and terminate the trading engine. |
| `GET` | `/api/v2/controls/commands/{command_id}` | Poll execution outcome of a submitted command (`PENDING`, `APPLIED`, `REJECTED`). |
| `GET` | `/api/v2/controls/audit` | Chronological audit log of all operator commands submitted through CLI and UI. |

---

## 4. UI Architecture & State Management

The frontend is built using **React 19**, **TypeScript**, and **Vite**, organized into modular domain slices:

```
dashboard/frontend/src/
├── app/
│   ├── AppShell.tsx              # Global header, nav rail, emergency drawer, footer
│   ├── routes.tsx                # SPA routing table
│   └── store.ts                  # Global client state (active scope, active trade, theme)
├── api/
│   ├── client.ts                 # Axios / Fetch client with typed error handling
│   ├── operations.ts             # Operational queries and mutations
│   ├── research.ts               # Research run queries, comparison mutations
│   ├── trades.ts                 # Trade journal, candle fetching, inspection
│   └── controls.ts               # Authenticated control command mutations
├── features/
│   ├── cockpit/                  # Operations Overview panels, KPI ribbon, alert cards
│   ├── positions/                # Positions table, order tree, protective stop ratchets
│   ├── watchlist/                # Monitored symbols grid, countdown timers, signal stream
│   ├── market_status/            # 5-state risk widget, benchmark ETF metrics, transition log
│   ├── research/                 # Experiment table, filter drawer, dossier, compare modal
│   ├── trades/                   # Trade list, filter bar, trade dossier drawer
│   ├── charts/                   # TradingView Lightweight Charts canvas component
│   ├── health/                   # Infrastructure health cards, backup monitor, drift audit
│   └── controls/                 # Command confirmation modals (close, flatten, pause)
└── components/
    ├── ui/                       # Design system primitives (Button, Modal, Badge, Tooltip)
    └── tables/                   # Virtualized, sortable tables (TanStack Table + Virtual)
```

### 4.1 State Management with TanStack Query
- All remote server data is managed via **TanStack Query (React Query v5)**.
- Query keys are strictly scoped (e.g. `['operations', accountId, 'positions']`, `['research', 'runs', filters]`).
- Switching accounts automatically invalidates and refetches queries with zero cross-account state pollution.
- Active polling cadence:
  - Cockpit / KPIs / Liveness: 2,500ms
  - Positions & Active Orders: 2,500ms
  - Research Runs & Backtest Trades: Stale time 5 minutes (refetch on user filter change)

### 4.2 High-Performance Charting Engine
- The primitive inline SVG chart is replaced with **TradingView Lightweight Charts** (Canvas/WebGL):
  - Renders 10,000+ 1-minute candlesticks at 60 frames per second.
  - Native hardware-accelerated pan, pinch-zoom, and crosshair snapping.
  - Custom series plugins render stepped protective stop lines, VWAP curves, execution fill markers, and holding period background bands.
  - Excursion levels (MFE / MAE) are rendered as horizontal price guide lines with exact numeric callouts.

---

## 5. Persistence Changes & Database Evolution

The architecture preserves all 38 existing tables in SQLite and all Parquet/DuckDB schemas. To fulfill new requirements, three targeted, non-breaking schema additions are introduced via migration `0003_console_observability.sql`:

### 5.1 SQLite Schema Extensions

```sql
-- Migration: 0003_console_observability.sql

-- 1. Market Status Transition History Table
CREATE TABLE market_status_transitions (
  transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_scope_id TEXT NOT NULL REFERENCES account_scopes(account_scope_id),
  from_state TEXT NOT NULL CHECK (from_state IN ('NORMAL','WARNING','CRASH','STABILIZING','RECOVERY')),
  to_state TEXT NOT NULL CHECK (to_state IN ('NORMAL','WARNING','CRASH','STABILIZING','RECOVERY')),
  reason TEXT NOT NULL,
  metrics_json TEXT NOT NULL DEFAULT '{}',
  occurred_at TEXT NOT NULL
);
CREATE INDEX market_status_transitions_occurred_at_idx 
ON market_status_transitions(account_scope_id, occurred_at);

-- 2. Index verification on market_bars for instant candlestick range queries
CREATE INDEX IF NOT EXISTS market_bars_range_idx 
ON market_bars(account_scope_id, symbol, timeframe, bar_start);
```

### 5.2 Research DuckDB Run Index
In `research/data/catalog.duckdb`, an analytical table `research_runs_index` is created to cache parsed backtest run metadata:
```sql
CREATE TABLE IF NOT EXISTS research_runs_index (
  selection_id VARCHAR PRIMARY KEY,
  run_id VARCHAR,
  execution_id VARCHAR,
  period_role VARCHAR,
  window_start TIMESTAMP,
  window_end TIMESTAMP,
  symbols VARCHAR[],
  buy_strategies VARCHAR[],
  sell_strategies VARCHAR[],
  trade_count INTEGER,
  win_rate DECIMAL(6,2),
  net_pnl DECIMAL(16,4),
  return_percent DECIMAL(8,4),
  max_drawdown DECIMAL(6,2),
  profit_factor DECIMAL(8,4),
  dataset_id VARCHAR,
  manifest_path VARCHAR,
  results_path VARCHAR,
  indexed_at TIMESTAMP
);
```
A background scanning job crawls `research/` on startup or on demand, populating this index. All subsequent research browser queries execute in single-digit milliseconds directly through DuckDB SQL.

---

## 6. Authentication, Authorization & Operator Safety

### 6.1 Security Models
1. **Local Workstation (Loopback):**
   When `bind_host` is `127.0.0.1`, access is local. Viewing is unrestricted. Command submission checks the OS user (`mike`).
2. **Remote Host / Server Exposure (R730 / Tailscale / Private LAN):**
   - Non-loopback binds require setting `MIKE_TRADE_DASHBOARD_ALLOW_NON_LOOPBACK=1`.
   - The FastAPI server enforces **Bearer Token Authentication** (`MIKE_TRADE_CONSOLE_TOKEN`) or HTTP-only Secure Session Cookies.
   - All control endpoints (`POST /api/v2/controls/*`) require the `Operator` or `Admin` role.

### 6.2 Operator Safety Guardrails
- **Idempotency Keys:** Every UI command generates a client UUID `idempotency_key` to prevent double execution on network retries.
- **Engine Liveness Pre-Check:** If the engine heartbeat is older than 15 seconds, executable commands (`close`, `flatten`, `pause`) are rejected immediately with HTTP 409 Conflict.
- **Two-Step Live Confirmation:** Any command submitted in `LIVE` mode opens a high-visibility modal requiring the operator to type the word `CONFIRM` before enabling the submission button.
