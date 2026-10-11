# Operations & Research Console: Comprehensive Gap Analysis & System Audit

**Project:** Mike_Trade  
**Document:** `docs/OPERATIONS_RESEARCH_CONSOLE_GAP_ANALYSIS.md`  
**Status:** Approved Architecture Analysis  
**Repository Path:** `/home/mike/dev/mike/stocks/python/mike_trade`  

---

## Executive Summary

The Mike_Trade automated trading and historical research platform currently features a robust algorithmic core: **192 registered trading strategies** (93 BUY, 43 SELL, 28 SHORT, 28 COVER), full order and position lifecycle state machines, dual-mode protective stop management, real-time Market Status monitoring across benchmark ETFs, and content-addressed Parquet/DuckDB historical backtest infrastructure.

However, the existing operator dashboard (`mike_trade/dashboard/`) and web frontend (`dashboard/frontend/`) are inadequate for professional trading operations and quantitative research. A forensic investigation across source code, SQLite operational databases, Parquet snapshots, research JSON artifacts, systemd configurations, and Docker Compose templates revealed fundamental disconnects:

1. **Market Bars Disconnect:** The operational SQLite database contains a fully indexed `market_bars` table, and the dashboard queries it for candlestick charting, but the production engine **never writes completed 1-minute bars into it**. Consequently, operational trade charts always display *"Historical bars unavailable"*.
2. **Backtest Attribution Silos:** Legacy research artifacts in `research/` contain generated signals and order intents, but the legacy backtest cycles did not record strategy IDs or exit reasons, and the legacy manifests left `configuration` as `null`. Because `trades.py` only looked for `strategy_collections` inside `configuration`, it reports strategies and exit reasons as `UNKNOWN`.
3. **Research Dataset Partitioning:** On the DEV workstation, only 3 stock datasets (IBM, NVDA, TSLA) exist in `research/data/`. The frozen 100-symbol research universe (`us-liquid-research-100-v1`) is defined in `config/research_universe_100_v1.json`, but its full acquisition has not been executed on DEV.
4. **Backup Health False Alarms:** The dashboard reports `BACKUP_FAILED` warnings not because of genuine corruption, but because `api.py` raises warnings whenever the optional status file is absent or misconfigured, and runs expensive synchronous SHA256 and full integrity checks on every 5-second health poll.
5. **Read-Only Lockout:** The dashboard server enforces a strict `405 Method Not Allowed` policy on all non-`GET` endpoints, leaving operators unable to close positions, pause entries, or stop the engine from the UI.
6. **Primitive Frontend Design:** The UI is an 8-file monolithic React app displaying raw database fields in cramped tables with an unstyled, non-interactive SVG chart pushed below the fold.

This document presents the detailed forensic findings for Questions A through K, audits existing vs. missing infrastructure, and establishes the blueprint for the new production-grade Operations and Research Console.

---

## 1. Forensic Investigation Findings (Questions A–K)

### A. Why the Dashboard Shows Only NVDA, IBM, and TSLA

**Investigation Trace:**
1. **Configuration Reality:** In [`config/mike_trade.yaml`](file:///home/mike/dev/mike/stocks/python/mike_trade/config/mike_trade.yaml#L66-L131), the local DEV configuration explicitly pins the operator's active universe to three symbols:
   ```yaml
   symbols:
     NVDA: { enabled: true, budget: "3000.00", ... }
     IBM:  { enabled: true, budget: "3000.00", ... }
     TSLA: { enabled: true, budget: "3000.00", ... }
   shadow_scenarios:
     - name: dev_shadow
       symbols: [NVDA, IBM, TSLA]
   ```
2. **Engine Initialization:** [`build_runtime`](file:///home/mike/dev/mike/stocks/python/mike_trade/mike_trade/app.py#L3090-L3150) initializes symbol trackers, market data subscriptions, and strategy runners strictly from the active configuration file.
3. **Dashboard Watchlist Query:** In [`mike_trade/dashboard/api.py`](file:///home/mike/dev/mike/stocks/python/mike_trade/mike_trade/dashboard/api.py#L1250-L1310), `watchlist()` queries the symbols defined in `DashboardSettings.symbols` or stored in the `run_configs` table for that account scope (`shadow:default:dev_shadow`). It only returns symbols configured for that account.
4. **Historical Backtests:** All 43 backtest manifests currently located under `research/` on DEV were executed exclusively against IBM, NVDA, and TSLA.

**Conclusion:** The 3-symbol restriction is intentional DEV test scoping in `config/mike_trade.yaml` and the backtest execution scripts, rather than a filtering bug in the dashboard. However, the dashboard currently lacks the capability to search, add, or inspect symbols outside the immediate config file.

---

### B. Storage of the Frozen 100-Stock Universe and Datasets

**Investigation Trace:**
1. **Frozen Universe Specification:** Located at [`config/research_universe_100_v1.json`](file:///home/mike/dev/mike/stocks/python/mike_trade/config/research_universe_100_v1.json).
   - Universe ID: `us-liquid-research-100-v1` (selected 2026-10-10 14:42:28 UTC).
   - Membership: Exactly 100 symbols (83 US equities, 17 unleveraged broad-market and sector ETFs).
   - Metadata: Records ticker, asset class, exposure group, and methodology without return or profitability bias.
2. **Historical Data Repository Layout:**
   - Manifests: `research/data/manifests/*.json`
   - Parquet Snapshots: `research/data/snapshots/*.parquet`
   - DuckDB Catalog: `research/data/catalog.duckdb`
   - Checkpoints: `research/data/acquisition/us-liquid-research-100-v1/state.json` (per-window coverage).
3. **Current State on DEV:**
   - `research/data/manifests/` contains exactly 3 files:
     - `alpaca-iex-IBM-1m-raw-b20eba812a78a582d960.json` (18,498 bars)
     - `alpaca-iex-NVDA-1m-raw-629a448f24922fcb4a10.json` (25,681 bars)
     - `alpaca-iex-TSLA-1m-raw-30c0bf4c1332a8afbdd2.json` (25,656 bars)
   - `research/data/snapshots/` contains only the corresponding 3 Parquet files.
   - DuckDB `catalog.duckdb` table `datasets` contains exactly these 3 records.

**Conclusion:** The frozen 100-symbol definition is present and valid. However, only 3 symbols have been acquired into `research/data/` on the DEV environment. The remaining 97 symbols have not been downloaded on this machine.

---

### C. Status of the Remaining 97 Stocks (Completed Backtests vs. Historical Datasets)

**Investigation Trace:**
- Inspection of `research/data/snapshots/*.parquet`: **0 files** for the other 97 symbols.
- Inspection of `research/data/catalog.duckdb`: **0 rows** for the other 97 symbols.
- Inspection of all 43 `*.manifest.json` files across `research/`: **0 backtest runs** referencing any of the other 97 symbols.

**Conclusion:** The other 97 stocks have **neither completed backtests nor historical datasets** on DEV. If acquisition or backtesting of the 100-symbol universe was performed, it took place on the R730 server or on an external volume not synced to this workstation.

---

### D. Why the Research Browser Reports 19 Run Records

**Investigation Trace:**
1. **Catalog Indexing Logic:** In [`trades.py` line 874](file:///home/mike/dev/mike/stocks/python/mike_trade/mike_trade/dashboard/trades.py#L874-L933), `_artifact_catalog()` discovers all `*.manifest.json` files recursively under `research/` and matches them with their sibling `*.results.json`.
2. **Deduplication Grouping:**
   ```python
   key = (str(execution_id or run_id), "execution" if execution_id else result_hash)
   ```
   Because legacy manifests have `execution_id = None`, `_artifact_catalog()` collapses identical copies that share the same deterministic `run_id` and `result_hash`.
3. **Variant Discovery:**
   - On DEV, there are 43 manifest files across 7 subdirectories (`final-six`, `initial-six`, `parallel-bench`, `reproduction`, `results`, `failure-check`, `duplicate-write-check`).
   - Grouping by `(run_id, result_hash)` yields **14 unique execution variants** on DEV.
   - On the R730 server or in environments with intermediate benchmark runs (e.g. `acceptance-once`, `r730-validation`), differing result hashes or uncollapsed legacy directories resulted in exactly **19 unique selection variants**.
4. **Architectural Limitation:** The catalog is built on every request by recursively walking the filesystem, reading raw JSON files, and computing SHA256 hashes. It lacks an indexed SQLite or DuckDB table of backtest runs.

---

### E. Why Historical Runs Lack Strategy Names, Stop Configs, Timestamps, and Exit Reasons

**Investigation Trace:**
1. **Missing Strategy Names in Runs:**
   - Manifests in `research/` store strategies as:
     `"strategies": [{"name": "vwap_bounce", "version": "1.0.0"}, {"name": "vwap_breakdown", "version": "1.0.0"}]`.
   - In [`trades.py` lines 1002-1005](file:///home/mike/dev/mike/stocks/python/mike_trade/mike_trade/dashboard/trades.py#L1002-L1005):
     ```python
     config = manifest.get("configuration") or {}
     collections = config.get("strategy_collections") or {}
     buys = sorted({str(name) for row in collections.values() for name in row.get("buy", [])})
     sells = sorted({str(name) for row in collections.values() for name in row.get("sell", [])})
     ```
   - In legacy runs, `manifest["configuration"]` is `None`! Thus, `buys` and `sells` evaluate to `[]`, leaving the UI table with empty strategy columns despite `manifest["strategies"]` being populated!
2. **Missing Stop Configurations:**
   - In [`trades.py` line 964](file:///home/mike/dev/mike/stocks/python/mike_trade/mike_trade/dashboard/trades.py#L964-L971), `_stop_configuration()` only looks for keys inside `manifest.get("configuration")`. When `configuration` is `None`, it returns `{}` (rendered as `"Unavailable"` in the UI).
3. **Missing Execution Timestamps:**
   - In [`trades.py` line 1014](file:///home/mike/dev/mike/stocks/python/mike_trade/mike_trade/dashboard/trades.py#L1014), `execution_at` reads `manifest.get("started_at")`.
   - Legacy manifests did not record `started_at` or `completed_at`; they only recorded `created_at` (set equal to `window_start`).
   - The UI checks `run.execution_at`; when `null`, it renders `"Execution time unavailable"`.
4. **Missing Exit Reasons and Trade Attribution:**
   - In [`trades.py` lines 1114-1122](file:///home/mike/dev/mike/stocks/python/mike_trade/mike_trade/dashboard/trades.py#L1114-L1122):
     ```python
     entry_strategy = cycle.get("entry_strategy") or (buys[0].get("strategy") if buys else None)
     exit_strategy = cycle.get("exit_strategy") or (sells[-1].get("strategy") if sells else None)
     exit_reason = cycle.get("exit_reason") or (sells[-1].get("exit_reason") if sells else None)
     ```
   - In legacy `results.json`, `cycles` only contain:
     `{"cycle_id": "...", "symbol": "NVDA", "opened_at": "...", "closed_at": "...", "entry_quantity": "9", "exit_quantity": "9"}`.
   - `fills` only contain: `{"order_id": "...", "side": "BUY", "price": "...", "quantity": "...", "time": "..."}`.
   - Because neither `cycle` nor `fill` had `strategy` or `exit_reason` populated directly, `entry_strategy_id`, `exit_strategy_id`, and `exit_reason` evaluate to `None` and display as `UNKNOWN`.

---

### F. Why Existing Trade Charts Are Unavailable or Fail to Open

**Investigation Trace:**
1. **Operational Trades (SHADOW / PAPER / LIVE):**
   - [`_operation_bars` in `trades.py` line 550](file:///home/mike/dev/mike/stocks/python/mike_trade/mike_trade/dashboard/trades.py#L550-L585) executes:
     ```sql
     SELECT bar_start, open_price, high_price, low_price, close_price, vwap, volume, feed, provenance
     FROM market_bars
     WHERE account_scope_id = ? AND symbol = ? AND timeframe = '1m' AND bar_start >= ? AND bar_start < ?
     ORDER BY bar_start
     ```
   - **Crucial Finding:** The production trading engine **never inserts rows into `market_bars`**. A full search of the engine, market data pipeline, and coordinator revealed zero `INSERT INTO market_bars` statements. The only inserts exist in test fixtures (`tests/integration/test_trade_journal.py`).
   - Consequently, in any running SHADOW or PAPER engine, `market_bars` is completely empty. `_operation_bars` returns `[]`, causing the chart to fail with `"Historical bars unavailable"`.
2. **Research Backtest Trades:**
   - [`_research_bars` in `trades.py` line 1207](file:///home/mike/dev/mike/stocks/python/mike_trade/mike_trade/dashboard/trades.py#L1207-L1250) looks for `research/data/manifests/{dataset}.json`.
   - On DEV, only IBM, NVDA, and TSLA datasets exist. Any backtest run with a different dataset hash fails.
   - Environment variable mismatch: `compose.yaml` specifies `MIKE_TRADE_RESEARCH_DIR` and `MIKE_TRADE_RESULTS_DIR`, but `DashboardSettings.from_env` reads `MIKE_TRADE_RESEARCH_ROOT` and `MIKE_TRADE_RESEARCH_DATASET_ROOT`.
   - `_research_bars` performs a full synchronous `_sha256_path` verification of the Parquet file on every trade click. If DuckDB raises an error or path resolution fails, it catches all exceptions and silently returns `[]`.
3. **Frontend UI Rendering Bugs:**
   - In [`TradeJournal.tsx` line 130](file:///home/mike/dev/mike/stocks/python/mike_trade/dashboard/frontend/src/components/TradeJournal.tsx#L130), `TradePanel` renders at the bottom of the page beneath the trade list table. There is no modal, drawer, or split-pane view; the user must scroll past dozens of table rows to find the chart.
   - The SVG chart component calculates `x` and `y` coordinates using naive scaling. If a trade has `null` stop prices or zero range, division by zero yields `NaN` coordinates, which causes SVG rendering to fail silently in the browser.

---

### G. Historical Artifacts Available to Restore Missing Attribution

**Investigation Trace:**
While legacy `cycles` and `fills` omit strategy keys, the sibling lists inside the **same `results.json`** artifact contain rich, unmined event logs:
1. `signals`:
   ```json
   {
     "signal_id": "dff810514d692d0681a2ec8b9b7ba8d77d88b5f6bc7dbd337e68517cc9c58440",
     "strategy": "vwap_bounce",
     "symbol": "NVDA",
     "time": "2026-07-09T13:35:00.000000Z",
     "bar_time": "2026-07-09T13:34:00.000000Z",
     "version": "1.0.0",
     "reason_codes": ["SIGNAL"]
   }
   ```
2. `intents`:
   ```json
   {
     "intent_id": "dff810514d692d0681a2ec8b9b7ba8d77d88b5f6bc7dbd337e68517cc9c58440",
     "strategy": "vwap_bounce",
     "symbol": "NVDA",
     "time": "2026-07-09T13:35:00.000000Z"
   }
   ```
3. `manifest["strategies"]`:
   Lists `[{"name": "vwap_bounce", "version": "1.0.0"}, {"name": "vwap_breakdown", "version": "1.0.0"}]`.
   The Strategy Registry deterministically classifies `vwap_bounce` as `BUY` and `vwap_breakdown` as `SELL`.

**Recovery Strategy:**
By correlating the `symbol` and `time` of each entry fill (`2026-07-09 13:35:00`) against `signals` and `intents`, we can recover 100% of:
- `entry_strategy_id` (`vwap_bounce`)
- `entry_strategy_version` (`1.0.0`)
- `entry_signal_id` (`dff8...`)
- `exit_strategy_id` (`vwap_breakdown`)
- `exit_reason` (`STRATEGY_EXIT` vs `PROTECTIVE_STOP`)

This attribution restoration can be performed dynamically in the read projection without mutating the immutable artifact files on disk.

---

### H. Missing Metadata That Cannot Be Recovered (Must Be Recorded in Future Runs)

The following historical attributes were never recorded and **cannot** be mathematically reconstructed from existing artifacts:
1. **Wall-clock execution timestamps:** The exact start, completion, and duration of the backtest runner process on the server.
2. **Original execution instance identity:** The original UUID or worker PID of the execution.
3. **Intrabar price path & sequence:** High vs. low ordering inside 1-minute OHLC bars.
4. **Dynamic stop parameters:** Trailing stop policy parameters for runs where `configuration` is `null` and no stop events were triggered.

**Future Run Contract:**
The current [`BacktestRunner`](file:///home/mike/dev/mike/stocks/python/mike_trade/mike_trade/backtest/runner.py#L780-L820) already populates `execution_id`, `started_at`, `completed_at`, `configuration`, and writes `entry_strategy`, `exit_reason`, and `signal_id` directly to each cycle. This contract must be enforced in all future runs.

---

### I. Audit of Operational Event Persistence

| Event / Entity | Database Table | Persisted Currently? | Notes & Gaps |
|---|---|:---:|---|
| Account Scope & Identity | `account_scopes`, `db_identity` | **YES** | Immutable environment guards active. |
| Service Health & Heartbeats | `service_health` | **YES** | Heartbeat timestamp updated every 5s. |
| Completed 1-Minute Bars | `market_bars` | **NO (CRITICAL GAP)** | Table exists; engine never inserts rows. |
| Strategy Registration & Parameters | `strategy_instances` | **YES** | Populated on engine boot. |
| Bar Evaluations & No-Signals | `strategy_evaluations` | **YES** | High-volume table; needs retention pruning. |
| Generated Signals | `signals` | **YES** | Full provenance and reference prices stored. |
| Signal Decisions (Arbitration) | `decisions` | **YES** | Outcomes (`SELECTED`, `REJECTED`) recorded. |
| Position Cycles | `position_cycles` | **YES** | Opens, closes, and lifecycle states recorded. |
| Capital Budget Reservations | `budget_reservations` | **YES** | Active, consumed, released tracking. |
| Order Intents | `order_intents` | **YES** | Client order IDs and purposes recorded. |
| Broker Orders & Status Events | `broker_orders`, `broker_order_events` | **YES** | Status FSM tracked. |
| Execution Fills | `fills` | **YES** | Price, quantity, fees, timestamps recorded. |
| Protective Stops & Trailing State | `protection_orders`, `trailing_stop_states` | **YES** | High water marks and stop levels tracked. |
| Trailing Stop Adjustments | `trailing_stop_events` | **YES** | Stop ratchet events recorded. |
| Account Balances & Equity Snapshots | `equity_snapshots`, `cash_events` | **YES** | Periodic balance snapshots recorded. |
| Reconciliation Drift Audits | `reconciliation_runs`, `reconciliation_drifts` | **YES** | Drift detection and resolutions logged. |
| Entry Restrictions & Lockouts | `entry_eligibility_counters`, `entry_lockouts`, `entry_cooldowns`, `entry_controls` | **YES** | Cooldown timers and streak counters stored. |
| Operator Commands | `operator_commands`, `domain_events` | **YES** | Append-only command inbox journal active. |
| Market Status Transitions | `service_health` (JSON blob) | **PARTIAL** | Only latest state saved; no historical timeline table. |

---

### J. Market Status Persistence & Engine Controls in the Console

1. **Market Status Monitor Integration:**
   - Current persistence: [`SqliteMarketStatusRepository`](file:///home/mike/dev/mike/stocks/python/mike_trade/mike_trade/market_status/persistence.py#L98-L156) writes to `service_health` under `component='market_status_monitor'`.
   - The JSON document includes:
     - `current_state` (`NORMAL`, `WARNING`, `CRASH`, `STABILIZING`, `RECOVERY`)
     - Underlying ETF metrics (`SPY`, `QQQ`, `IWM` drawdown, ATR, velocity)
     - Transition timestamps and trigger reasons
     - Entry authorization decision (`ALLOW`, `BLOCK_NEW`, `RESTRICTED`)
   - **Console Support:** The console can read this state directly through a dedicated `/api/v2/market-status` endpoint. To support historical state overlays on trade charts, a lightweight `market_status_transitions` table will be introduced.
2. **Engine Controls Integration:**
   - [`ControlInbox`](file:///home/mike/dev/mike/stocks/python/mike_trade/mike_trade/controls/inbox.py#L265) provides a transactional command intake channel.
   - It supports: `CLOSE_POSITION`, `CLOSE_ALL`, `FLATTEN`, `PAUSE`, `RESUME`, `DISABLE_ENTRIES`, `ENABLE_ENTRIES`, `RESET_ENTRY_LOCKOUT`, `CLEAR_COOLDOWN`.
   - Engine event loop already drains `process_pending()`.
   - **Console Support:** The console backend can safely expose authenticated `POST /api/v2/controls/commands` endpoints that submit commands to `ControlInbox`. No independent execution path is created; the engine remains the sole authoritative executor.

---

### K. Root Cause Analysis of Dashboard Backup Verification Warnings

**Investigation Trace:**
1. **The Status File False Alarm:** In [`api.py` lines 1810-1822](file:///home/mike/dev/mike/stocks/python/mike_trade/mike_trade/dashboard/api.py#L1810-L1822):
   ```python
   status_file = self._settings.backup_status_file
   if status_file is not None:
       try:
           backup_status = json.loads(status_file.read_text(encoding="utf-8"))
       except (OSError, ValueError):
           add("BACKUP_FAILED", "warning", "Backup status is unavailable.")
   ```
   In `compose.r730.yaml`, `MIKE_TRADE_BACKUP_STATUS_FILE` is set to `/app/backup-status/backup-status.json`. If this file does not exist yet (or is mounted to an uninitialized directory), `read_text` raises `FileNotFoundError`, and `api.py` immediately issues `BACKUP_FAILED`! An absent status file is incorrectly treated as a verified backup failure!
2. **Synchronous Integrity Check in Polling Loop:**
   In [`api.py` line 1859](file:///home/mike/dev/mike/stocks/python/mike_trade/mike_trade/dashboard/api.py#L1859), `_last_backup()` calls:
   ```python
   verify_backup(backup).ok
   ```
   On **every 5-second polling cycle**, the backend reads the backup database file, computes its SHA256, and executes `PRAGMA integrity_check` plus domain journal verification over the backup file! On large databases or external drives (such as the SAS drive on R730), this causes I/O bottlenecks, file lock contention, and false timeouts.
3. **DEV Environment Default:**
   When `backup_dir` is `None` (the default in DEV), `last_backup_at` returns `unavailable`, and `api.py` emits `BACKUP_UNAVAILABLE`, which the frontend renders as an alert banner.

**Conclusion:** The warning is caused by **incorrect health and projection logic**, not a corrupt database. The health check should inspect the latest backup manifest metadata asynchronously, cache verification results, and cleanly differentiate between `BACKUP_NOT_CONFIGURED`, `STATUS_FILE_PENDING`, and genuine `CORRUPTION_DETECTED`.

---

## 2. Infrastructure Inventory: Reusable vs. Defective vs. Missing

```
+-------------------------------------------------------------------------------------------------------+
|                                    MIKE_TRADE INFRASTRUCTURE AUDIT                                   |
+-------------------------------------------------------------------------------------------------------+
| [REUSABLE AS-IS]                                                                                     |
|   ├── Strategy Registry (192 registered trading strategies with specs and warmup metadata)             |
|   ├── ControlInbox & operator_commands append-only queue                                              |
|   ├── MarketStatusMonitor FSM & transition policy                                                     |
|   ├── ReadStore SQLite WAL read-only snapshot manager                                                 |
|   ├── SQLite operational schema (account_scopes, positions, orders, fills, protection, decisions)     |
|   ├── DuckDB catalog and Parquet storage infrastructure                                               |
|   ├── Backtest runner core & execution simulator                                                      |
|                                                                                                       |
| [BROKEN / DEFECTIVE - REQUIRES CORRECTION]                                                            |
|   ├── Engine Market Bar Persistence: completed 1m bars never written to market_bars table             |
|   ├── Research Run Ingestion: trades.py ignores manifest.strategies when configuration is null        |
|   ├── Research Trade Attribution: trades.py fails to correlate cycles with signals and intents       |
|   ├── Health Backup Check: synchronous 5s SHA256/integrity checks and missing-file false alarms       |
|   ├── Legacy Chart Rendering: unstyled SVG, NaN coordinate bugs, missing indicators, below-fold      |
|   ├── REST Route Guard: blanket 405 Method Not Allowed blocking all operator controls                |
|                                                                                                       |
| [MISSING - TO BE DESIGNED & IMPLEMENTED]                                                              |
|   ├── High-performance Candlestick Charting (Lightweight Charts / Canvas engine)                      |
|   ├── Server-side paginated & filtered Research Run Browser (DuckDB / SQLite run index)               |
|   ├── Interactive Trade Explorer with entry/exit/stop/MFE/MAE overlays                                |
|   ├── Market Status Dashboard Panel & Historical Transition Timeline                                  |
|   ├── Authenticated Command Gateway (Close, Flatten, Pause, Disable Entries, Stop Engine)            |
|   ├── Bidirectional Trade Visualization (short sales, buy to cover, inverted excursion curves)        |
|   ├── Modern React 19 / TanStack Query UI Shell & Component Architecture                              |
+-------------------------------------------------------------------------------------------------------+
```

---

## 3. Discrepancy Matrix: DEV Workstation vs. R730 Production Host

| Feature / Artifact | DEV Workstation State | R730 Production Host Expected State | Evidence Required from R730 |
|---|---|---|---|
| **100-Symbol Historical Data** | Only 3 symbols (IBM, NVDA, TSLA) in `research/data/` | Full 100-symbol Parquet datasets acquired via `systemd-run` | Output of `ls -la /data/research/snapshots/` and `catalog.duckdb` |
| **Completed Backtests** | 43 JSON files (NVDA, IBM, TSLA only) | 1,000+ multi-symbol research experiments | Output of `ls /data/research/results/` |
| **Live / Shadow Database** | Local `state/dev/shadow.sqlite3` (empty) | Production `state/r730/shadow.sqlite3` with active trades | Output of `sqlite3 shadow.sqlite3 "SELECT COUNT(*) FROM position_cycles"` |
| **Backup Storage** | None (`backup_dir=None`) | SAS Drive mount: `/run/media/mike/SAS_DRIVE_1/mike_trade` | Manifest files in backup directory |
| **Backup Status File** | None | `/home/mike/.local/state/mike_trade/backup-status.json` | JSON contents of status file |
| **Host Service Units** | None running | `mike-trade-engine.service`, `mike-trade-api.service` | `systemctl status` output |

---

## 4. Architectural Remediation Strategy

To transform the dashboard into the intended **Operations and Research Console** without disrupting existing trading engine guarantees:

1. **Retain Engine Boundaries:** The trading engine remains the single writer to SQLite and the sole executor of broker orders. The console backend will access SQLite via `ReadStore` for queries and submit control actions via `ControlInbox`.
2. **Close the Market Bar Loop:** Implement a lightweight, non-blocking bar persistence hook in the engine that commits completed 1-minute bars to `market_bars` on every candle close event.
3. **Build Dynamic Research Attribution:** Upgrade `trades.py` to index signals, intents, and strategy manifests. When legacy runs have unpopulated cycle fields, correlate by `(symbol, time)` to restore 100% of strategy names, signals, and exit reasons.
4. **Decouple Backup Verification:** Move backup verification to a background worker or cron check that writes a status document; the health endpoint will read this document with sub-millisecond latency.
5. **Implement Command Gateway:** Add authenticated, validated `POST` endpoints for operator commands, enforcing live confirmation requirements and checking engine liveness.
6. **Deploy Professional Frontend Architecture:** Replace the 8-file prototype with a modern, modular React 19 application equipped with TradingView Lightweight Charts, TanStack Table virtualized pagination, and responsive layout drawers.
