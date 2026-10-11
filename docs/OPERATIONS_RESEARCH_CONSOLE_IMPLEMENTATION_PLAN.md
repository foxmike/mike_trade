# Operations & Research Console: Parallel Implementation Plan

**Project:** Mike_Trade  
**Document:** `docs/OPERATIONS_RESEARCH_CONSOLE_IMPLEMENTATION_PLAN.md`  
**Status:** Approved Parallel Implementation Plan  
**Repository Path:** `/home/mike/dev/mike/stocks/python/mike_trade`  

---

## 1. Overview & Parallel Workstream Strategy

This plan organizes the complete development of the **Operations and Research Console** into **nine self-contained workstreams**. Each workstream is designed for execution by an independent coding agent working within an isolated Git worktree.

### 1.1 Dependency Graph & Integration Pipeline

```
Phase 1: Foundation (Data & Indexing)
  ├── WS 1: Persistence & Market Bar Streaming Writer (Branch: feature/console-bar-streaming)
  ├── WS 2: Research Indexer & Attribution Reconciler (Branch: feature/console-research-indexer)
  └── WS 5: Market Status Transitions & Projections  (Branch: feature/console-market-status)
       │
Phase 2: Backend API Layer
  ├── WS 3: Console Backend Core & REST API v2       (Branch: feature/console-backend-core)  <-- Depends on WS 1, 2
  └── WS 4: Command Gateway & Engine Control Bridge  (Branch: feature/console-command-gateway) <-- Depends on WS 3
       │
Phase 3: Frontend Foundation & Charting
  ├── WS 6: Frontend Foundation & Design Shell       (Branch: feature/console-frontend-shell)
  └── WS 7: Trade Explorer & Candlestick Charting    (Branch: feature/console-trade-explorer) <-- Depends on WS 6, 1, 3
       │
Phase 4: Feature Screens & Operator Controls
  ├── WS 8: Research Console & Analytics UI          (Branch: feature/console-research-ui)   <-- Depends on WS 6, 2, 3
  └── WS 9: Operational Cockpit & Control Modals     (Branch: feature/console-operations-ui) <-- Depends on WS 6, 4, 5
       │
Phase 5: Unified Integration & Production Build
  └── Integration Branch: feature/console-final-integrated (Merged into main)
```

---

## 2. Workstream Breakdown

---

### Workstream 1: Persistence & Market Bar Streaming Writer
- **Branch:** `feature/console-bar-streaming`
- **Worktree:** `/home/mike/dev/mike/stocks/python/work/mike_trade-console-bar-streaming`
- **Objective:** Fix the critical operational chart gap by persisting completed 1-minute bars to SQLite, and add observability indexes.
- **Scope & Code Locations:**
  - Create migration `mike_trade/persistence/migrations/0003_console_observability.sql`:
    - Index on `market_bars(account_scope_id, symbol, timeframe, bar_start)`.
    - Table `market_status_transitions`.
  - In `mike_trade/market_data/candles.py` and `mike_trade/app.py`:
    - Add `MarketBarWriter` hook invoked when `Candle.status == CandleStatus.CLOSED`.
    - Batch insert into `market_bars` using the existing engine `WriterSession` without blocking live message dispatch.
- **Deliverables:**
  - `mike_trade/persistence/migrations/0003_console_observability.sql`
  - `mike_trade/market_data/bar_persistence.py`
  - Focused test: `tests/unit/test_market_bar_persistence.py`
- **Targeted Acceptance Checks:**
  - Ingest 10 mock 1-minute bars; verify `SELECT COUNT(*) FROM market_bars` equals 10 with exact OHLCV and provenance.
  - Verify duplicate bars are safely ignored via `INSERT OR IGNORE`.

---

### Workstream 2: Unified Research Indexer & Attribution Reconciliation
- **Branch:** `feature/console-research-indexer`
- **Worktree:** `/home/mike/dev/mike/stocks/python/work/mike_trade-console-research-indexer`
- **Objective:** Dynamically restore missing strategy attribution in legacy backtests and build high-speed DuckDB indexing for thousands of runs.
- **Scope & Code Locations:**
  - Upgrade `mike_trade/dashboard/trades.py`:
    - Parse `manifest.strategies` and correlate entry/exit fills by `(symbol, time)` against `results.json` `signals` and `intents`.
    - Reconstruct `entry_strategy_id`, `exit_strategy_id`, and `exit_reason`.
  - Create `mike_trade/dashboard/research_catalog.py`:
    - Embedded DuckDB table `research_runs_index` in `research/data/catalog.duckdb`.
    - Crawls and indexes runs in background; exposes search, multi-symbol filter, and pagination methods.
- **Deliverables:**
  - `mike_trade/dashboard/research_catalog.py`
  - Refactored `mike_trade/dashboard/trades.py`
  - Focused test: `tests/unit/test_research_catalog_indexer.py`
- **Targeted Acceptance Checks:**
  - Load legacy run `7c2faaddfb6a2c8285feef1570be12aaf08864b20b48f064e8b0416e8b31ac46`.
  - Verify `entry_strategy_id == "vwap_bounce"`, `exit_strategy_id == "vwap_breakdown"`, and `attribution_complete == True`.
  - Verify DuckDB query executes in < 15ms across 1,000 indexed runs.

---

### Workstream 3: Console Backend Core & REST API v2
- **Branch:** `feature/console-backend-core`
- **Worktree:** `/home/mike/dev/mike/stocks/python/work/mike_trade-console-backend-core`
- **Objective:** Implement modular FastAPI `/api/v2/` endpoints, server-side pagination, and decouple backup verification.
- **Scope & Code Locations:**
  - Create `mike_trade/dashboard/v2/router.py`, `operations.py`, `research.py`, `trades.py`.
  - Replace synchronous 5-second `verify_backup` call in health reporting with a non-blocking manifest metadata reader.
  - Return typed Pydantic v2 responses with serialized Decimal strings and ISO UTC timestamps.
- **Deliverables:**
  - `mike_trade/dashboard/v2/` package
  - Updated `mike_trade/dashboard/server.py`
  - Focused test: `tests/integration/test_console_api_v2.py`
- **Targeted Acceptance Checks:**
  - `GET /api/v2/accounts/{account_id}/cockpit` returns HTTP 200 with typed KPIs and Sourced metadata.
  - `GET /api/v2/research/runs?page=1&page_size=50` returns paginated list with total count.
  - Health check responds in < 5ms without hanging on SAS drive disk checks.

---

### Workstream 4: Command Gateway & Engine Control Integration
- **Branch:** `feature/console-command-gateway`
- **Worktree:** `/home/mike/dev/mike/stocks/python/work/mike_trade-console-command-gateway`
- **Objective:** Provide secure, authenticated REST endpoints that submit commands directly to `ControlInbox`.
- **Scope & Code Locations:**
  - Create `mike_trade/dashboard/v2/controls.py` and `mike_trade/dashboard/command_gateway.py`.
  - Implement endpoints:
    - `POST /api/v2/controls/positions/close`
    - `POST /api/v2/controls/positions/close-all`
    - `POST /api/v2/controls/positions/flatten`
    - `POST /api/v2/controls/engine/pause`
    - `POST /api/v2/controls/engine/entries`
    - `POST /api/v2/controls/engine/stop`
    - `GET /api/v2/controls/commands/{command_id}`
  - Enforce engine heartbeat validation and `confirm_live="YES"` checks.
- **Deliverables:**
  - `mike_trade/dashboard/command_gateway.py`
  - `mike_trade/dashboard/v2/controls.py`
  - Focused test: `tests/integration/test_command_gateway.py`
- **Targeted Acceptance Checks:**
  - Submit `CLOSE_POSITION` on offline engine -> returns HTTP 409 Conflict with clear error.
  - Submit `PAUSE` on online engine -> inserts row in `operator_commands`, returns command ID, and status evaluates to `PENDING`.

---

### Workstream 5: Market Status Monitor Projections & Transition Tracking
- **Branch:** `feature/console-market-status`
- **Worktree:** `/home/mike/dev/mike/stocks/python/work/mike_trade-console-market-status`
- **Objective:** Add transition persistence to Market Status Monitor and expose `/api/v2/market-status` routes.
- **Scope & Code Locations:**
  - In `mike_trade/market_status/monitor.py` and `persistence.py`:
    - On every state change (`NORMAL` -> `WARNING`, etc.), append row to `market_status_transitions`.
  - Create `mike_trade/dashboard/v2/market_status.py`:
    - Expose `/api/v2/market-status/current` and `/api/v2/market-status/history`.
- **Deliverables:**
  - `mike_trade/dashboard/v2/market_status.py`
  - Updated `mike_trade/market_status/monitor.py`
  - Focused test: `tests/unit/test_market_status_transitions.py`
- **Targeted Acceptance Checks:**
  - Trigger synthetic crash transition; verify `market_status_transitions` contains recorded reason and metrics snapshot.

---

### Workstream 6: Frontend Foundation & Design System Shell
- **Branch:** `feature/console-frontend-shell`
- **Worktree:** `/home/mike/dev/mike/stocks/python/work/mike_trade-console-frontend-shell`
- **Objective:** Upgrade frontend architecture to React 19, Vite, TanStack Query v5, and implement the responsive console shell.
- **Scope & Code Locations:**
  - Update `dashboard/frontend/package.json` (add `@tanstack/react-query`, `lightweight-charts`, `lucide-react`).
  - Create `dashboard/frontend/src/app/AppShell.tsx`:
    - Header with Environment pill, Engine Liveness pill, Market Status badge, Account Switcher, Clock.
    - Navigation rail with 8 screen tabs.
    - Red Emergency Controls dropdown button.
    - Global Notification and Alert toast system.
- **Deliverables:**
  - `dashboard/frontend/src/app/`
  - `dashboard/frontend/src/components/ui/`
  - Focused test: `npm run typecheck` & `npm run build`
- **Targeted Acceptance Checks:**
  - Production bundle builds successfully to `dist/` with zero TypeScript errors.

---

### Workstream 7: High-Performance Trade Explorer & Candlestick Charting
- **Branch:** `feature/console-trade-explorer`
- **Worktree:** `/home/mike/dev/mike/stocks/python/work/mike_trade-console-trade-explorer`
- **Objective:** Implement interactive TradingView Lightweight Charts with multi-layer overlays and trade inspection.
- **Scope & Code Locations:**
  - Create `dashboard/frontend/src/features/charts/CandlestickChart.tsx`:
    - Canvas rendering of 1m/5m/15m/1h/1d candles.
    - Overlays: VWAP line, entry/exit markers, stepped protective stop lines, MFE/MAE levels, holding interval shading.
  - Create `dashboard/frontend/src/features/trades/TradeInspector.tsx`:
    - Dual source selector (Account vs Backtest).
    - Diagnostic metrics grid, attribution dossier, and chronological event timeline.
- **Deliverables:**
  - `dashboard/frontend/src/features/charts/`
  - `dashboard/frontend/src/features/trades/`
- **Targeted Acceptance Checks:**
  - Selecting a trade renders chart with correct entry marker, stop line, and exit marker without NaN coordinate errors.

---

### Workstream 8: Research Console & Experiment Analytics UI
- **Branch:** `feature/console-research-ui`
- **Worktree:** `/home/mike/dev/mike/stocks/python/work/mike_trade-console-research-ui`
- **Objective:** Implement research navigation for 10,000+ experiments, multi-filter drawer, equity curves, and comparison modal.
- **Scope & Code Locations:**
  - Create `dashboard/frontend/src/features/research/ResearchBrowser.tsx`:
    - Virtualized table with server-side pagination, multi-column sort, search bar, and filter drawer.
  - Create `dashboard/frontend/src/features/research/ExperimentDossier.tsx`:
    - Equity curve chart, drawdown waterfall, trade distributions, and configuration JSON viewer.
  - Create `dashboard/frontend/src/features/research/ExperimentCompareModal.tsx`:
    - Overlaid normalized equity curves and side-by-side metric diff.
- **Deliverables:**
  - `dashboard/frontend/src/features/research/`
- **Targeted Acceptance Checks:**
  - Filter by strategy and win rate; table updates with server-side results in < 100ms.
  - Compare two experiments; renders normalized comparative chart.

---

### Workstream 9: Operational Cockpit, Controls & Live Management UI
- **Branch:** `feature/console-operations-ui`
- **Worktree:** `/home/mike/dev/mike/stocks/python/work/mike_trade-console-operations-ui`
- **Objective:** Build Cockpit, Positions/Execution FSM, Watchlist, and command confirmation modals.
- **Scope & Code Locations:**
  - Create `dashboard/frontend/src/features/cockpit/CockpitScreen.tsx`:
    - KPI ribbon, loss limit gauge, open positions quick-table, active alerts banner.
  - Create `dashboard/frontend/src/features/positions/PositionsScreen.tsx`:
    - Positions table, cycle order tree, protective stop ratchets, external holdings.
  - Create `dashboard/frontend/src/features/watchlist/WatchlistScreen.tsx`:
    - Symbols grid with countdown timers and candidate signal stream.
  - Create `dashboard/frontend/src/features/controls/CommandModals.tsx`:
    - Close Position, Flatten, Pause, Disable Entries modals with two-step `CONFIRM` typing for LIVE mode.
- **Deliverables:**
  - `dashboard/frontend/src/features/cockpit/`
  - `dashboard/frontend/src/features/positions/`
  - `dashboard/frontend/src/features/watchlist/`
  - `dashboard/frontend/src/features/controls/`
- **Targeted Acceptance Checks:**
  - Clicking "Close Position" opens modal, prompts for reason, submits to backend, and displays progress spinner until acknowledged.

---

## 3. Shared Interfaces & Contracts Between Agents

To prevent merge conflicts and ensure clean integration, agents must strictly adhere to the following contracts:

### 3.1 Pydantic Model Contract (`mike_trade/dashboard/v2/models.py`)

```python
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict

class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

class Sourced[T](_Model):
    value: T | None = None
    as_of: datetime | None = None
    fresh: bool
    stale_reason: str | None = None
    provenance: str | None = None

class CandleBar(_Model):
    timestamp: datetime
    timestamp_et: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    vwap: Decimal | None = None

class TradeDetailRecord(_Model):
    trade_id: str
    symbol: str
    execution_mode: str
    account_id: str
    cycle_id: str
    direction: Literal["LONG", "SHORT"]
    entry_timestamp: datetime | None
    exit_timestamp: datetime | None
    entry_price: Decimal | None
    exit_price: Decimal | None
    quantity: Decimal
    realized_pnl: Decimal | None
    realized_return_percent: Decimal | None
    entry_strategy_id: str | None
    exit_strategy_id: str | None
    exit_reason: str | None
    protective_stop_mode: str | None
    initial_stop_price: Decimal | None
    final_stop_price: Decimal | None
    attribution_complete: bool

class ControlCommandRequest(_Model):
    command_type: Literal["CLOSE_POSITION", "CLOSE_ALL", "FLATTEN", "PAUSE", "RESUME", "DISABLE_ENTRIES", "ENABLE_ENTRIES", "STOP"]
    account_id: str
    symbol: str | None = None
    reason: str
    confirm_live: bool = False
    idempotency_key: str
```

### 3.2 TypeScript Client Contract (`dashboard/frontend/src/api/types.ts`)

```typescript
export interface Sourced<T> {
  value: T | null;
  as_of: string | null;
  fresh: boolean;
  stale_reason: string | null;
  provenance: string | null;
}

export interface CandleBar {
  timestamp: string;
  timestamp_et: string;
  open: string;
  high: string;
  low: string;
  close: string;
  volume: string;
  vwap: string | null;
}

export interface TradeDetailRecord {
  trade_id: string;
  symbol: string;
  execution_mode: "LIVE" | "PAPER" | "SHADOW" | "BACKTEST";
  account_id: string;
  cycle_id: string;
  direction: "LONG" | "SHORT";
  entry_timestamp: string | null;
  exit_timestamp: string | null;
  entry_price: string | null;
  exit_price: string | null;
  quantity: string;
  realized_pnl: string | null;
  realized_return_percent: string | null;
  entry_strategy_id: string | null;
  exit_strategy_id: string | null;
  exit_reason: string | null;
  protective_stop_mode: string | null;
  initial_stop_price: string | null;
  final_stop_price: string | null;
  attribution_complete: boolean;
}
```

---

## 4. End-to-End Usability Scenarios & Acceptance Tests

Before merging into `main`, the integrated console must pass five end-to-end usability scenarios:

### Scenario 1: Quantitative Research Exploration
1. Operator navigates to `/research`.
2. Sets filter to `Strategy: vwap_bounce`, `Symbol: NVDA`, `Win Rate > 50%`.
3. Table instantly filters to matching runs. Operator selects the top run.
4. Experiment Dossier opens displaying equity curve, parameters, and trade table.
5. Operator clicks a winning trade. Trade Explorer opens with candlestick chart, entry fill marker, VWAP curve, and stepped stop lines.

### Scenario 2: Emergency Risk Flatten
1. Operator is in `SHADOW` mode on `/positions` during high market volatility.
2. Market Status pill transitions to `CRASH` (red).
3. Operator clicks header `EMERGENCY` -> `Flatten Account`.
4. Modal requires typing reason `MARKET CRASH EMERGENCY`.
5. Command is submitted; `operator_commands` records `FLATTEN`.
6. Engine drains command, cancels open orders, closes positions at market, and records fills.
7. Cockpit updates: open positions count drops to 0, cash updates.

### Scenario 3: Real-Time Operational Watchlist & Signal Audit
1. Operator opens `/watchlist`.
2. Inspects NVDA countdown timer for the next 1-minute candle close.
3. Candle closes: new bar arrives.
4. Strategy evaluates: signal generated for `momentum_breakout`.
5. Signal arbitration rejects entry due to `DAILY_ENTRY_CAP_REACHED`.
6. Operator clicks signal in the live stream; drawer displaysIndicator values and rejection rule.

### Scenario 4: Historical Run Cross-Comparison
1. Operator navigates to `/research`.
2. Selects two experiments (Run A: Fixed Stop vs. Run B: Stepped Trailing Stop).
3. Clicks `Compare Experiments`.
4. Comparison modal displays side-by-side metric table and overlaid equity curves showing the stepped trailing stop achieved a lower maximum drawdown.

### Scenario 5: Infrastructure Health & Backup Verification
1. Operator opens `/health`.
2. Verifies Engine status is `RUNNING` (heartbeat age 1.5s), Market Data is `CONNECTED`, Scheduler lag is 0.8s.
3. Checks Persistence card: Database WAL size is 32 KB.
4. Backup status displays `VERIFIED` with timestamp from the latest backup manifest, with zero false alarm warnings.
