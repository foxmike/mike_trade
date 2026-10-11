# Operations & Research Console: Functional Specification

**Project:** Mike_Trade  
**Document:** `docs/OPERATIONS_RESEARCH_CONSOLE_SPEC.md`  
**Status:** Approved Functional Specification  
**Repository Path:** `/home/mike/dev/mike/stocks/python/mike_trade`  

---

## 1. System Overview & User Personas

The **Mike_Trade Operations and Research Console** is a unified, production-grade web application designed for monitoring live/shadow automated trading operations, inspecting risk and execution lifecycles, and conducting deep quantitative analysis across thousands of historical backtest experiments.

### 1.1 Target Personas

1. **Systems & Trading Operator:**
   - Monitors real-time engine liveness, feed latency, broker connectivity, and scheduled evaluation loops.
   - Manages active positions, tracks protective trailing stops, and executes authenticated emergency controls (close position, flatten, pause trading, disable entries, emergency stop).
   - Audits reconciliation drift and operational health errors.
2. **Quantitative Research Analyst:**
   - Navigates historical datasets and thousands of backtest experiments across hundreds of symbols.
   - Analyzes BUY, SELL, SHORT, and COVER strategy performance, parameter sensitivity, and stop configurations.
   - Evaluates portfolio equity curves, underwater drawdown profiles, and trade economics.
   - Drills down into individual historical trades with interactive candlestick charts and execution replays.
3. **Risk & Compliance Officer:**
   - Tracks the five-state Market Status Monitor (`NORMAL`, `WARNING`, `CRASH`, `STABILIZING`, `RECOVERY`) and its policy restrictions on new entries.
   - Audits order intent, broker submission, and fill execution timelines.
   - Verifies protective stop coverage and verifies that no unmanaged positions exist.

---

## 2. Global Layout & Shell Navigation

The console uses an ergonomic desktop-first layout designed for dense financial information, high contrast readability, and low cognitive overhead.

```
+-----------------------------------------------------------------------------------------------------------------------+
| MIKE_TRADE CONSOLE | [SHADOW: dev_shadow v] | Engine: [RUNNING (1.2s)] | Market: [NORMAL] | 14:35:12 ET | [EMERGENCY v] |
+-----------------------------------------------------------------------------------------------------------------------+
| NAV RAIL  |  SCREEN CONTENT AREA                                                                                      |
| [1] Cockpit|                                                                                                          |
| [2] Live   |                                                                                                          |
| [3] Watch  |                                                                                                          |
| [4] Market |                                                                                                          |
| [5] Resrch |                                                                                                          |
| [6] Trades |                                                                                                          |
| [7] Health |                                                                                                          |
| [8] Audit  |                                                                                                          |
+-----------------------------------------------------------------------------------------------------------------------+
| FOOTER: Mode: SHADOW | DB WAL: OK (640 KB) | Feed: IEX (Connected, 0.4s) | Broker: SIMULATED | Ver: 192 Strategies   |
+-----------------------------------------------------------------------------------------------------------------------+
```

### 2.1 Global Header Elements

1. **System Brand & Environment Badge:** Displays application name and environment tag (`LIVE` in red/amber border, `PAPER` in blue, `SHADOW` in purple, `BACKTEST` in gray).
2. **Account / Scope Selector:** Dropdown listing all registered `account_scopes` from SQLite (`live:alpaca:...`, `paper:alpaca:...`, `shadow:default:dev_shadow`). Selecting a scope instantly updates all operational tabs; balances, positions, and orders are never mixed across scopes.
3. **Engine Liveness Pill:** Real-time engine status derived from heartbeat age (`RUNNING` < 15s green, `DEGRADED` yellow, `PAUSED` blue, `RECONCILING` cyan, `OFFLINE` > 60s red).
4. **Market Status Pill:** Real-time market risk status from Market Status Monitor (`NORMAL` green, `WARNING` yellow, `CRASH` red, `STABILIZING` orange, `RECOVERY` blue). Clicking the pill opens the Market Status drilldown modal.
5. **Exchange Clocks:** Dual-time display showing exchange local time (`America/New_York` with active session status: Premarket, Regular Market Open, Postmarket, Closed) and UTC.
6. **Next Entry Cutoff Indicator:** Shows time remaining until daily entry cutoff (default `15:30:00 ET`).
7. **Emergency Controls Action Button:** Red button opening the authenticated emergency intervention drawer (Pause, Disable Entries, Close All, Flatten, Stop Engine).

### 2.2 Navigation Rail (Screen Switching)

- **Screen 1: Operations Cockpit (`/cockpit`)** — High-level summary of capital, P&L, open risk, health cards, and active alerts.
- **Screen 2: Positions & Execution (`/positions`)** — Live positions, pending orders, order intents, fills, and broker activities.
- **Screen 3: Strategy Watchlist (`/watchlist`)** — Monitored symbols, active strategy assignments, evaluation countdowns, and recent signals.
- **Screen 4: Market Status Monitor (`/market-status`)** — Five-state risk classifier, benchmark ETF metrics, transition history, and policy rules.
- **Screen 5: Historical Research (`/research`)** — Multi-symbol backtest run explorer, dataset inventory, parameter comparisons, and batch results.
- **Screen 6: Interactive Trade Explorer (`/trades`)** — Unified trade journal (Operational and Backtest), full candlestick charting, overlays, and MFE/MAE analytics.
- **Screen 7: System Health & Maintenance (`/health`)** — Component links, feed latency, scheduler lag, database size, backup status, and doctor remediations.
- **Screen 8: Command Center & Audit Log (`/audit`)** — Operator command submission, pending command queue, and immutable event journal.

---

## 3. Screen Specifications

---

### Screen 1: Operations Cockpit (`/cockpit`)

#### Purpose
Provides the primary overview for a trading session. Displays capital utilization, daily/lifetime P&L, risk limits, active position counts, and immediate actionable alerts.

#### Layout & Panels
1. **Financial KPI Ribbon:**
   - **Equity:** Total account equity with daily change ($ and %).
   - **Cash & Buying Power:** Available non-margin cash and day buying power.
   - **Deployed Capital & Reserved Buys:** Active capital deployed in open positions plus reserved buying power committed to pending entries.
   - **Realized P&L (Today):** Net realized gain/loss for the current calendar session.
   - **Unrealized P&L:** Mark-to-market open position gain/loss.
   - **Lifetime P&L:** Cumulative realized performance since account inception.
   - **Daily Loss Cap Gauge:** Progress bar showing daily realized loss against `daily_loss_limit` (e.g., $300 cap).
2. **Operational Status Cards:**
   - **Engine & Scheduling:** Loop cadence (5s), scheduler lag (seconds), evaluation timeout thresholds.
   - **Market Data Feed:** Provider (Alpaca), feed venue (IEX), streaming status, last minute bar arrival timestamp.
   - **Protective Stops Summary:** Number of open positions with active stops, trailing stops, or protection gaps.
   - **Market Status Directive:** Current risk authorization (`ALLOW`, `BLOCK_NEW`, `RESTRICTED`) and active ETF trigger.
3. **Active Positions Quick-View:** Compact table showing open positions, unrealized P&L, distance to protective stop, and quick-action "Close" button.
4. **Actionable Alerts Banner:** Displays high-priority issues (engine offline, feed disconnected, protection gap, reconciliation drift, backup stale) with exact `make doctor` or UI remediation actions.

---

### Screen 2: Positions, Orders & Execution Lifecycle (`/positions`)

#### Purpose
Deep visibility into active holdings, order execution pipelines, and order state machines across both long and short lifecycles.

#### Features & User Interactions
1. **Managed Positions Table:**
   - Columns: Symbol, Direction (`LONG` / `SHORT`), Quantity, Entry Avg Price, Current Mark Price, Market Value, Unrealized P&L ($ / %), Holding Time, Protective Stop Price, Stop Distance (%), Status (`OPEN`, `EXITING`), Action.
   - Interaction: Clicking a row expands the **Cycle Order Tree**; clicking "Close Position" opens the authenticated close confirmation dialog.
2. **External / Unmanaged Holdings Panel:**
   - Displays broker positions detected by reconciliation that do not correlate with an active internal cycle (`UNMANAGED_HOLDING`).
   - Includes "Adopt Position" or "Liquidate External" actions.
3. **Active Orders & Execution FSM:**
   - Filter tabs: `All Orders`, `Open / Pending`, `Filled Today`, `Canceled / Rejected`.
   - Columns: Order ID, Client Order ID, Cycle ID, Symbol, Side (`BUY`, `SELL`, `SELL_SHORT`, `BUY_TO_COVER`), Type (`MARKET`, `LIMIT`, `STOP`, `TRAILING_STOP`), Status (`NEW`, `PENDING_NEW`, `ACCEPTED`, `PARTIALLY_FILLED`, `FILLED`, `REPLACED`), Quantity, Filled Qty, Limit/Stop Price, Submitted At, Updated At.
   - Expandable Order Detail: Raw broker JSON payload, state transition event history (`broker_order_events`), execution slippage vs. arrival price.
4. **Protective Stops & Trailing Stop Monitor:**
   - Tracks fixed stops, broker native trailing stops, and stepped trailing stops.
   - Columns: Symbol, Cycle ID, Mode (`FIXED_STOP`, `BROKER_TRAILING`, `STEPPED_TRAILING`), Status (`ACTIVE`, `REPLACING`, `TRIGGERED`), Covered Qty, Current Stop Price, Anchor/High Water Mark, Step Thresholds, Last Ratchet At.
   - Action: "Recalculate Stop" / "Manual Ratchet".
5. **Execution Fills & Fees Ledger:**
   - Chronological table of individual fills with exchange fill ID, execution venue, quantity, price, fee, and SEC/FINRA regulatory fee allocations.

---

### Screen 3: Strategy Watchlist & Signals Monitor (`/watchlist`)

#### Purpose
Displays the monitored universe, active strategy configurations, real-time evaluation schedules, and candidate signal arbitration.

#### Features & User Interactions
1. **Watchlist Grid / Table:**
   - Columns: Symbol, Enabled (`Toggle`), Market Price, Bid/Ask Spread, Data Freshness (age in seconds), Active BUY Strategies, Active SELL Strategies, Active SHORT/COVER Strategies, Evaluation Cadence, Next Due Trigger, Session Entry Count / Cap, Consecutive Loss Streak, Cooldown Status (`Active until HH:MM:SS` or `Eligible`).
   - Search & Filter: Search by symbol; filter by active position, lockout status, or strategy ID.
2. **Signal Generation & Arbitration Stream:**
   - Real-time audit log of signal evaluations triggered on completed 1-minute bars.
   - Columns: Timestamp, Symbol, Strategy ID, Version, Action (`ENTER_LONG`, `EXIT_LONG`, `ENTER_SHORT`, `COVER_SHORT`), Reference Price, Confidence, Arbitration Outcome (`SELECTED`, `REJECTED`), Rejection Reason (e.g. `ALREADY_IN_POSITION`, `BUDGET_EXCEEDED`, `MARKET_STATUS_BLOCK`, `DAILY_ENTRY_CAP_REACHED`, `LOCKOUT_ACTIVE`).
   - Detail Drawer: Clicking any signal displays raw indicators computed on the bar (RSI, ATR, VWAP, EMA, Bollinger Bands, Volume deviation) and the exact decision rule logic.

---

### Screen 4: Market Status Monitor & Systemic Risk (`/market-status`)

#### Purpose
Monitors overall systemic market conditions across benchmark indices (SPY, QQQ, IWM), providing early warning, crash detection, and stabilization tracking to protect portfolio capital.

#### State Machine & Visual Presentation
The 5 canonical states are presented in a prominent status badge with state-machine diagram overlays:
- **`NORMAL` (Green):** Standard market conditions; all strategy entries authorized.
- **`WARNING` (Yellow):** Elevated volatility or divergence detected; entries restricted or budget scaled back.
- **`CRASH` (Red):** Severe drop / waterfall event active; **all new entries blocked**; existing positions protected by tight trailing stops.
- **`STABILIZING` (Orange):** Volatility contracting, downward momentum slowing; entries remain blocked while bottom forms.
- **`RECOVERY` (Blue):** Confirmed upward reversal from depressed levels; selective entries authorized with conservative sizing.

#### Features & Interactions
1. **Real-Time Benchmark Index Metrics:**
   - Displays cards for **SPY**, **QQQ**, and **IWM**:
     - Completed 1m close, rolling 5m/15m change, intraday high/low range.
     - Normalized ATR(14), Realized Volatility, Distance from Session Open (%).
     - Composite Breadth Indicator & Market Velocity Index.
2. **State Transition History Timeline:**
   - Historical audit table of all transitions: Timestamp, From State, To State, Trigger Reason (e.g., `SPY 15m drawdown exceeded 2.1%`, `QQQ volatility contraction threshold met`), Triggering Metrics Snapshot.
3. **Active Policy Restrictions Card:**
   - Explains current entry gating: `Allow New Entries: YES / NO`, `Allowed Direction: LONG ONLY / SHORT ONLY / BOTH / NONE`, `Position Sizing Multiplier: 1.0x / 0.5x / 0.0x`.
4. **Historical Market Status Chart Overlay:**
   - Synchronized SPY candlestick chart with colored background bands representing the 5 market states over time.

---

### Screen 5: Historical Research & Experiment Explorer (`/research`)

#### Purpose
Allows quantitative researchers to browse, search, filter, and compare thousands of backtest experiments across hundreds of stocks, with full visibility into strategy configurations and performance metrics.

#### Features & User Interactions
1. **Research Run Browser (Server-Side Paginated Table):**
   - Handles 10,000+ experiments via efficient server-side pagination (10, 25, 50, 100 rows per page).
   - Columns:
     - **Execution Time:** Wall-clock start & completion time (`YYYY-MM-DD HH:MM:SS ET`).
     - **Historical Window:** In-sample / Out-of-sample date range (`YYYY-MM-DD` to `YYYY-MM-DD`).
     - **Period Role:** `RESEARCH`, `DEV`, `HOLDOUT` (with badge styling).
     - **Symbol(s):** Ticker list (e.g. `NVDA`, `IBM`, `AAPL, MSFT`).
     - **Strategies:** Assigned BUY, SELL, SHORT, COVER strategy names.
     - **Trades:** Total completed cycle count.
     - **Win Rate:** Percentage of winning trades (%).
     - **Net P&L:** Total dollar profit/loss (green/red).
     - **Return %:** Return on starting capital.
     - **Profit Factor:** Gross profits / gross losses.
     - **Max Drawdown:** Peak-to-trough equity drop (%).
     - **Execution ID / Fingerprint:** Truncated hash with copy-to-clipboard.
   - Comprehensive Filter Suite:
     - Search: Free-text search matching symbol, strategy name, experiment ID, or run purpose.
     - Strategy Multi-Select: Filter by any of the 192 registered strategies.
     - Symbol Multi-Select: Filter by any symbol in the 100-stock universe.
     - Metric Sliders / Bounds: Min trades, min win rate, min profit factor, max drawdown.
     - Date Pickers: Historical window start/end filter.
     - Sort: By any column (P&L, return, win rate, drawdown, execution time, trade count).
2. **Experiment Detail & Configuration Inspector:**
   - Clicking an experiment opens the **Experiment Dossier**:
     - **Capital Economics:** Starting capital, ending equity, peak equity, total commission/fees, total slippage impact.
     - **Trade Distribution:** Win/loss ratio, average winning trade, average losing trade, payoff ratio, profit factor, Kelly criterion, Expectancy ($ per trade).
     - **Time Diagnostics:** Average holding time (winners vs. losers), market exposure time (%).
     - **Configuration Inspector:** Syntax-highlighted JSON viewer displaying exact strategy parameters, stop loss percentages, fill assumptions (Next Bar Open, Bid/Ask bounce), and data vintage hashes.
     - **Interactive Equity Curve & Underwater Drawdown Chart:** Zoomable dual-chart showing portfolio equity trajectory alongside drawdown percentage over time.
3. **Experiment Comparison Tool:**
   - Operators can select 2 to 5 experiments and click **"Compare Experiments"**:
     - Side-by-side metric comparison table (delta highlighted in green/red).
     - Overlaid equity curves on a normalized 100-base scale.
     - Parameter diff highlighting parameter discrepancies between runs.
4. **Dataset Inventory & Acquisition Browser:**
   - Displays all historical datasets in `research/data/`:
     - Dataset ID, Provider, Feed (`iex`, `sip`), Symbol, Timeframe, Bar Count, Date Range, File Size, SHA256 Verification Status (`VERIFIED` / `CORRUPT`).
     - "Verify Datasets" button triggering DuckDB/Parquet checksum validation.

---

### Screen 6: Interactive Trade Explorer & Candlestick Inspector (`/trades`)

#### Purpose
The centerpiece of trade diagnostics. Click any trade from an operational account (SHADOW, PAPER, LIVE) or a backtest experiment to open a full candlestick chart with multi-layer overlays, execution markers, stop adjustments, and economic analytics.

#### Dual Source Architecture
A top toggle switch smoothly alternates between:
- **`Account Trades`:** Real-time cycles from the selected operational account scope.
- **`Backtest Trades`:** Cycles from the selected research experiment.
Both sources share the exact same unified presentation model and data contract.

#### Trade List Navigation
- Searchable, sortable table with status filters (`ALL`, `OPEN`, `PARTIALLY_CLOSED`, `CLOSED`, `WIN`, `LOSS`).
- Fast keyboard navigation: Use Up/Down arrow keys to cycle through trades while the inspection panel updates instantly.

#### Interactive Candlestick Chart Specification
Powered by a high-performance Canvas/WebGL engine (TradingView Lightweight Charts):
1. **Timeframe Selector:** 1m (default), 5m, 15m, 1h, Daily. Intraday aggregation derived causally from 1m bars.
2. **Zoom Presets:**
   - `Trade Span + Padding` (default): Focuses on 15 minutes before entry to 15 minutes after exit.
   - `Full Trading Session`: Displays the complete 09:30–16:00 ET trading day.
   - `Custom Range`: Free-form pan and pinch-zoom with mouse wheel / trackpad.
3. **Visual Chart Layers & Overlays:**
   - **OHLC Candlesticks:** High contrast green/red candles.
   - **Volume Histogram:** Sub-chart showing volume bars with 20-bar volume moving average.
   - **VWAP Curve:** Blue solid line showing session Volume-Weighted Average Price.
   - **Holding Interval Shading:** Shaded background band highlighting the exact entry-to-exit duration.
   - **Entry Execution Marker:** Green up-triangle (Long) or Red down-triangle (Short) placed at fill timestamp and fill price, with tooltip showing fill quantity, average price, and slippage.
   - **Exit Execution Marker:** Marker placed at exit fill timestamp and price with icon indicating exit cause (target exit, stop loss, time cutoff).
   - **Protective Stop Trajectory:** Stepped orange/red dashed line displaying the active stop price at every minute. Shows initial stop level and every subsequent trailing ratchet.
   - **Excursion Price Levels:** Dotted horizontal guide lines marking Maximum Favorable Excursion (MFE) high/low and Maximum Adverse Excursion (MAE).
   - **Evaluated Signal Markers:** Small diamond markers showing bars where the entry strategy generated candidate signals (including candidates rejected by arbitration).

```
Price ($)
  |                                                  [MFE Peak]
  |                             /\  /\    /\           -----
  |                [Entry]     /  \/  \  /  \
  |                  ^        /        \/    \     [Exit]
  |                 / \      /                \       v
  |----------------(===)----/                  \-----(===)------- Fill Price
  |                 |                                 |
  |   ..............|.................................|.......... [MAE Low]
  |                 +-------+ (Initial Stop)
  |                         +-------+ (Ratchet 1)
  |                                 +-------+ (Ratchet 2)
  +---------------------------------------------------------------- Time
        09:35      09:40    09:45    09:50    09:55    10:00
```

#### Diagnostic & Attribution Dossier (Side / Bottom Drawer)
1. **Economic Metrics Grid:**
   - Entry Price, Exit Price, Traded Quantity, Notional Value ($).
   - Realized P&L ($), Return (%), Holding Duration (minutes/seconds).
   - Maximum Favorable Excursion ($ and %): Maximum potential gain during holding.
   - Maximum Adverse Excursion ($ and %): Maximum drawdown experienced during holding.
   - Realized Slippage: Arrival price vs. fill price ($ per share and bps).
2. **Strategy Attribution Panel:**
   - **Entry Strategy:** Registered Strategy ID, Version, Signal Timestamp, Signal ID, Decision Reason.
   - **Exit Strategy / Cause:** Strategy ID (if discretionary exit) OR Cause (`PROTECTIVE_STOP`, `TRAILING_STOP`, `MANUAL_CLOSE`, `SAFETY_EXIT`, `EOD_CLOSE`).
   - **Protection Mode:** `FIXED_STOP`, `BROKER_TRAILING_STOP`, `STEPPED_TRAILING_STOP`.
3. **Chronological Trade Event Timeline:**
   - Vertical timeline tracing every milestone:
     - `SIGNAL_GENERATED` (Strategy evaluation triggered)
     - `SIGNAL_ARBITRATED` (Decision outcome `SELECTED`)
     - `INTENT_RECORDED` (Order intent created)
     - `ORDER_SUBMITTED` (Sent to broker/sim)
     - `FILL_CONFIRMED` (Shares bought/sold)
     - `PROTECTION_ARMED` (Stop order placed at $X)
     - `STOP_RATCHETED` (Stop moved from $X to $Y due to trail step)
     - `EXIT_SIGNAL` / `STOP_TRIGGERED`
     - `POSITION_CLOSED`

---

### Screen 7: System Health & Diagnostics (`/health`)

#### Purpose
Operational observability center. Monitors background daemons, feed latencies, database WAL performance, backup status, and reconciliation drifts.

#### Features & Indicators
1. **Infrastructure Component Matrix:**
   - **Engine Core:** State (`RUNNING`, `PAUSED`, `OFFLINE`), Heartbeat age, Loop cycle duration.
   - **Market Data Feed:** Connected status, Reconnect count, Dropped message counter, Bar delay (seconds).
   - **Broker Connectivity:** Gateway status, Rate-limit count, Token expiry countdown.
   - **Scheduler Engine:** Queue depth, Worker pool utilization, Lag seconds (alert if > 30s).
   - **Persistence / SQLite:** Database path, Schema version (v2), Main DB file size, WAL file size, Checkpoint status.
2. **Backup Verification Panel (Decoupled & Accurate):**
   - Displays status of operational and research database backups.
   - Shows Last Backup Timestamp, Age, Verification Status (`VERIFIED`, `PENDING`, `NOT_CONFIGURED`).
   - Cleanly handles environments where external backup drives (e.g. SAS drive) are detached without raising false alarms.
3. **Reconciliation & Drift Inspector:**
   - Displays timestamp of last reconciliation run.
   - List of unresolved drift items: Drift ID, Type (`POSITION_QUANTITY_MISMATCH`, `UNTRACKED_ORDER`, `BALANCE_DRIFT`), Expected Value, Actual Broker Value, Detected At.
4. **Actionable Remediation Center:**
   - Table of active warnings and errors with severity badges (`CRITICAL`, `WARNING`, `INFO`).
   - Provides copyable terminal remediation commands (`make doctor`, `mike-trade reconcile`, `make restart`).

---

### Screen 8: Authenticated Engine Controls (`/controls`)

#### Purpose
Provides secure, authenticated operator command dispatch directly into the engine's transactional `ControlInbox`.

#### Safety & Authorization Rules
- **No Independent Execution Path:** Every command is packaged as a typed `ControlCommand` and submitted to SQLite `operator_commands`. The running engine drains and executes the command within its authoritative loop.
- **Role-Based Access Control:** Viewing is unrestricted; command execution requires valid operator session authentication.
- **Live Safety Confirmations:** In `LIVE` mode, risk-loosening commands (e.g. `RESUME`, `ENABLE_ENTRIES`) or liquidations require explicit typed confirmation (`confirm_live="YES"`).
- **Engine Liveness Verification:** Commands that require an online engine (e.g. `CLOSE_POSITION`) verify engine heartbeat before submission, rejecting commands immediately if the engine is offline.

#### Supported UI Commands & Modals
1. **Close Position Modal:**
   - Select symbol -> Select reason (`OPERATOR_DISCRETION`, `RISK_LIMIT`, `NEWS_EVENT`) -> Submit.
   - Engine issues market order to close open shares and releases protective stops.
2. **Close All / Flatten Modal:**
   - "Close All Positions": Cancels open entry orders, closes all managed positions at market.
   - "Flatten Account": Closes all positions (managed and unmanaged) and cancels all broker orders.
   - Red high-visibility confirmation prompt with symbol count and total open value.
3. **Pause / Resume Entries:**
   - Toggle switch updating `entry_controls.paused`.
   - Halts evaluation of new entry signals while allowing protective stops and existing position management to continue uninterrupted.
4. **Disable / Enable Entries (Kill Switch):**
   - Toggle switch setting `entry_controls.kill_switch_enabled`.
5. **Stop Engine Action:**
   - Submits `STOP` command to gracefully drain the engine event loop, commit WAL, and terminate.
6. **Command Audit Log & Result Tracker:**
   - Table showing all submitted commands: Command ID, Type, Symbol, Issued By, Issued At, Status (`PENDING`, `APPLIED`, `REJECTED`, `EXPIRED`), Processing Duration, Error Detail.
