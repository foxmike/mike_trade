# MIKE_TRADE
## Complete Product Architecture & Implementation Plan

**Document status:** Implementation specification / design baseline (mike_trade)  
**Version:** 1.3  
**Prepared:** October 8, 2026  
**Deployment target:** Ubuntu / Dell PowerEdge R730 (also runnable on the development desktop)  
**Initial broker and market-data source:** Alpaca Trading API + Alpaca Basic (IEX) market data  
**Primary implementation language:** Python 3.12+  
**Intended implementation mechanism:** One substantial Abracapocus development program with bounded, reviewable work units  

> **Product mandate:** Build a complete, maintainable application—not a demo, throwaway MVP, or partial proof of concept. “Initial long-only” means the production architecture supports other directions through extensions, while only long trading is authorized at launch. Do not implement fake broker behavior as though it were live execution; unverified provider capabilities must be tested and gated.

---

## 1. Executive product definition

mike_trade is a self-hosted, configuration-driven stock-trading system. It monitors one or more user-selected U.S. equities (usually 1–10), ingests market data once per symbol, calculates shared indicators, evaluates independently selectable BUY and SELL strategies, enforces account and per-stock budgets, and manages one buy → one complete sell lifecycle per stock. It operates with a persistent live-broker mode and multiple independent shadow portfolios using the same signals, with historical backtesting using the same strategy logic. It is supervised by the operator through Makefile/CLI controls and a read-only web dashboard; no GUI is required for trading.

**User workflow:** Configure symbols, choose buy and sell strategies, set budgets, schedules and protection → start the service → monitor statuses, trades and performance → intervene by Makefile commands if desired. All trade decisions, observations, order events and account changes are auditable and restart-safe.

**Non-negotiables:**

1. Production-quality, end-to-end scope; no demo substitute or deferred core architecture.
2. Plug-in Python strategy classes with ordinary code, not a restrictive generic rule DSL.
3. Buy and sell strategies independently enabled per symbol; preserve source-paired strategies as testable presets. Ship a real, end-to-end **reference VWAP bounce BUY + VWAP breakdown SELL** strategy pair with separately executable classes, configuration, deterministic fixtures, and documented shadow/backtest use; this reference is not asserted to be profitable.
4. One intentional entry per cycle and one complete discretionary exit; no averaging down, adding, pyramiding, or planned partial exits. Broker partial fills are handled correctly.
5. Long-only active trading initially; short/cover concepts belong in the type system and capability model but must be disabled until deliberately authorized and implemented.
6. Single shared real-time market-data connection and cache used by live and every shadow scenario; strategy code never opens its own market-data connections.
7. Per-stock allocation and optional planned-loss budget plus total application budget; enforce configurable daily entry-cycle caps, consecutive-loss entry suspensions and post-sale cooldowns; do not assume deposits/funding can be created by the software.
8. Internal configurable scheduler; per-strategy event/timeframe triggers; bounded concurrent calculations with serialized order decisions.
9. Configurable per-stock broker-held protective stops: native continuous trailing is the preferred default, with a provider-independent stepped ratchet and fixed stops supported through broker adapters. Protection starts after confirmed entry fill, moves only upward for long positions, and must avoid over-selling.
10. Human-supervised operation. Manual end-of-day closing is primary; configurable optional auto-close and new-entry cutoff (e.g., 15:00 ET).
11. Shadow portfolios support persistent cash, fills, fees/estimates, positions, reconciled account values and detailed independent performance; experimental strategies can run beside the live strategy setup.
12. Makefile and CLI provide the complete operator surface. Dashboard is primarily read-only and explicitly reports OFFLINE/stale data.
13. Broker, market data, fundamental/research/news integrations are replaceable ports/adapters; Alpaca is the first production implementation.
14. No fabricated performance, unmeasured assumptions treated as alpha, guessed executions, or fake provider feature support. No obligatory notifications, stock scanner, earnings/economic blackout calendar, proprietary trading chart suite, raw tick archive, or configuration-history/preview workflow.

### 1.1 What “complete” means

The deliverable must encompass all production modules, migrations, operational commands, dashboard, reporting, broker integration, shadow engine, backtester, hard risk controls, documented setup, authentication and deployment. It is acceptable to build in coordinated phases for engineering safety; it is **not** acceptable to declare the product complete while essential operations are stubs, mock-only, or require a later rewrite.

### 1.2 Explicit exclusions, not placeholders

Out of product scope unless a change request is approved: options/futures/crypto trading; fully automated overnight strategies; an external paid rules platform; browser automation to access a broker; smart order routing at exchange/microsecond timescales; capital transfers/deposits; multi-user brokerage administration; mandatory ML or LLM inference in the decision path. Short selling is designed as an extension but live-short activation requires separate underwriting and tests. Research and data ports are implemented even if no paid provider is configured. Declined workflows (stock screening, automated blackout calendars, external notifications, session-wide tick recording, config history/preview tooling and full charting) must not be silently added as product requirements.

## 2. Design principles and engineering standards

- **Domain-first and object-oriented:** Typed entities/value objects with explicit invariants; cohesive classes with single responsibility.
- **Ports and adapters (hexagonal):** Strategy core, policy, portfolio and accounting know no Alpaca SDK classes, URLs or exchange-specific IDs.
- **Dependency inversion and dependency injection:** Construct adapters, clocks, repositories and configuration at the application composition root.
- **Strategy pattern:** Any buy/sell algorithm implements a small stable evaluator contract, including pure rule-based, stateful and model-based strategies.
- **Adapter pattern:** Brokerage, streaming/historical market data, optional strategy-requested research inputs and clocks.
- **Observer / pub-sub:** Market ticks/candle closures/scheduled milestones notify consumers, without per-strategy data fetching.
- **State machine:** Explicit transitions for symbol cycle, signal eligibility, order lifecycle, stop protection and service health.
- **Command pattern:** Every operator action and desired order is a structured command with identity, validation and auditable outcome.
- **Repository / unit-of-work:** Transactional persistence of orders, fills, positions, cash events, strategy activity and controls.
- **Policy objects:** Budget, per-session entry limit, consecutive-loss circuit breaker, post-sale cooldown, session, stop, conflict and entry-cutoff policies—not buried conditional logic.
- **Single-writer consistency:** Parallel strategy computation; serialized account/position/order mutations. Enforce symbol-level and account-wide locks.
- **Replayability:** Clock, market events, indicator inputs, fees, slippage and seeds are injectable; same evaluators run live/shadow/backtest.
- **No accidental trade side effects:** Importing modules, reading dashboards and running tests can never place live orders.
- **Fail closed for entries:** Unknown session, stale quote, unreconciled broker state, unknown stop status, unresolved pending orders or failed limits blocks new orders. Existing-position protection/reconciliation continues.
- **Operational clarity over framework sprawl:** One Python deployment with SQLite and a lightweight dashboard; no Kafka/Celery/Kubernetes by default.
- **Deterministic behavior and audit:** Decisions generated from validated configuration identified by a run-level checksum, immutable decision-input evidence, precise timestamps and reasons; no user-facing config version-history or rollback feature.
- **Least privilege / separation:** Paper and live credentials, databases and activation must be clearly isolated. Makefile targets require affirmative live activation and optional confirmation for destructive commands.

## 3. Product users and supported operation modes

**Operator:** One primary human operator who chooses stocks, budgets, strategies and schedule, runs Makefile commands, supervises dashboard, reconciles trading discrepancies and normally decides when to stop/close for the day.

**Modes** (not interchangeable):

| Mode | Data | Execution | Cash/positions | Intended use |
|---|---|---|---|---|
| Historical backtest | Timestamped historical events, known point-in-time inputs | Deterministic simulated broker | Independent backtest ledger | Research, validation, regression |
| Shadow | Live shared market feed | Local simulated fills; **no external order** | Independent persistent shadow account per configuration | Continuous comparison and new signal experiments |
| Alpaca paper | Live/shared data | Real requests to Alpaca **paper** account | Broker's paper balances/positions | End-to-end integration and broker order semantics |
| Alpaca live | Live/shared data | Actual Alpaca live account orders | Authoritative broker positions/cash | Authorized capital deployment |

**Critical distinction:** Shadow mode is *our own* simulation (arbitrary named accounts and scenarios). Alpaca paper mode is a broker-hosted simulation and must not be presented as equivalent to the local shadow fill model. The platform may run LIVE + N shadow accounts simultaneously; paper brokerage can be enabled as a distinct deployment/account workflow but must not submit from the same strategy run to both broker accounts without explicit selection.

Recommended environment isolation: `state/live/`, `state/paper/`, `state/shadow/<scenario_id>/`, `state/backtest/<run_id>/` or partitioned SQLite by environment. Credentials and API endpoints are mode-scoped. Cross-mode trade writes are forbidden.

## 4. Architecture overview

```text
OPERATOR
  |  versioned YAML configuration / CLI + Makefile
  v
CONTROL PLANE --- persistent controls, mode authorizations, health, journal
  |
  +--> SERVICE SUPERVISOR / SCHEDULER <--> MARKET CLOCK + exchange calendar
  |                  |
  |                  +--> BROKER / ORDER-UPDATE ADAPTER (Alpaca paper or live)
  |                  |
  |                  +--> MARKET DATA PORT --> Alpaca IEX WS / historical REST
  |                  |                                      |
  |                  |                                alternative adapter
  |                  |                                      |
  |                  +--------------> SHARED EVENT CACHE <---+
  |                                         |
  |                              CANDLE + INDICATOR ENGINE
  |                                         |
  |                          immutable MarketContext snapshots
  |                                         |
  |                          bounded STRATEGY EVALUATION POOL
  |                                |                    |
  |                         BUY RULE PLUGINS       SELL RULE PLUGINS
  |                                +----------+---------+
  |                                           |
  |                                   SIGNAL DECISIONS
  |                                           |
  |                               serialized ACCOUNT COORDINATOR
  |                               stock cycles + funding + risk
  |                                           |
  |                                   ORDER MANAGER
  |                                /         |          \
  |                     SHADOW ADAPTER   PAPER ADAPTER   LIVE ADAPTER
  |                              \          |          /
  |                            EVENT JOURNAL + ACCOUNT LEDGERS
  |                                           |
  +--------------------> REPORTING / DASHBOARD READ MODEL <---+
                             heartbeat, last known state, OFFLINE

RESEARCH / NEWS / FUNDAMENTALS PROVIDER PORTS → timestamped optional context
```

**Data plane** shares feed and indicator computations. **Decision plane** evaluates rules; **control plane** manages safety, sessions, configuration, and operating commands. **Execution plane** translates authorized, persisted intents into broker-specific operations. **Read plane** serves reports/dashboard; it never brokers trades directly.

### 4.1 Process model

- Single application **engine process** owns account/ledger writes, scheduler, market WebSocket consumer and strategy execution pool.
- Separate **dashboard process** reads a WAL-enabled read-only SQLite snapshot/API and renders status. It can remain online and show **OFFLINE** if the trading engine stops. A dashboard embedded only in the engine would disappear when the engine dies and could not reliably show OFFLINE.
- CLI sends authenticated local control commands via a Unix-domain socket or durable command inbox managed by engine; when engine is down, CLI can inspect/read state and persist safe desired actions. Never silently queue an executable live trade until reconnection.
- Optional `systemd --user` or system units for engine/dashboard start/stop and restart; make targets wrap service operations rather than implementing trading logic in Make recipes.
- SQLite WAL for the anticipated single host and 1–10 stocks, one authoritative writer per operational database, bounded retention of high-frequency snapshots, transactional backups.

## 5. Domain model, object contracts and identifiers

**Core immutable value objects** (Python dataclasses/Pydantic at boundaries; Decimal for money/quantities):

| Entity | Key properties |
|---|---|
| `Instrument` | canonical symbol, broker asset ID, exchange, equity class, tradable/fractionable flags, min qty/precision |
| `MarketEvent` | symbol, provider/feed, provider timestamp, received timestamp, sequence, quote/trade/candle type, price/size |
| `Candle` | symbol, timeframe, open/high/low/close/volume, start/end, **closed/final** flag, provenance |
| `MarketContext` | immutable as-of snapshot of candles/quotes/trades/indicators, session, optional research data, data freshness |
| `StrategyDefinition` | registered id, version, side, input requirements, supported sessions, parameter schema, trigger specification |
| `StrategyInstance` | strategy id + config instance id + symbol/scenario binding and versioned validated parameters |
| `Evaluation` | event ID, symbol, strategy instance, input snapshot hash, status, evidence/explanation, elapsed time |
| `Signal` | unique ID, symbol, `ENTER_LONG` or `EXIT_LONG` (extensible), strength if calibrated, generated/as-of/expires, rationale, strategy version |
| `SignalDecision` | ACCEPT/REJECT/IGNORE/DEFER with reason and policy trace, conflict/precedence outcome |
| `PositionCycle` | cycle id, account/scenario, symbol, state, entry intent/order(s), exit intent/order(s), fills, protective stop IDs |
| `Position` | symbol, signed quantity, avg basis, value, long/short, broker ID when applicable, acquisition origin |
| `OrderIntent` | deduplicated command id, purpose ENTRY/EXIT/PROTECTIVE, quantity/notional, type/TIF, protective linkage |
| `BrokerOrder` | venue order/client IDs, parent/children, status, filled/remaining, provider timestamps |
| `Fill` | external fill/activity ID, order ID, qty, price, fee if available, timestamp, source |
| `AccountSnapshot` | cash, buying power, equity, market value, outstanding commitments, as-of, mode, authoritative/estimated |
| `BudgetPolicy` | total reserved allocation, per-symbol caps, max loss, max open positions, leverage policy |
| `ProtectionPolicy` | required, fixed/continuous/stepped trailing mode, offset and optional advance step, order type/TIF, min replace interval, failure mode and renewal rules |
| `TrailingStopState` | position cycle, policy version, entry reference, raw high watermark, last ratchet anchor, last confirmed stop, broker order ID, protection status and update times |
| `TradingSessionPolicy` | regular-hours eligibility, opening delay, per-symbol latest entry time, optional automatic close |
| `EntryEligibilityPolicy` | per-symbol and optional per-buy-strategy daily entry caps, consecutive-loss thresholds/reset behavior, post-sale cooldown, precedence and override constraints |
| `EntryEligibilityState` | durable per-account/scenario symbol and strategy counters, current streak, lockout reason, session key, cooldown-until, last closing fill timestamp |
| `ControlCommand` | command ID, operator identity, mode, name, parameters, request time, acknowledgment/outcome |
| `HealthSnapshot` | heartbeat, current feed/broker state, degraded reasons, last reconciliation, persistence health |

`Symbol` is NOT an account: cash lives at account level, while per-symbol capital usage and accumulated P&L are views calculated from trades. The stock status refers to one **account + symbol + scenario** tuple. Actual broker accounts may contain positions bought outside this software; external positions are **unmanaged** until explicitly adopted through a guarded workflow, never attributed to a strategy automatically.

**Numerics:** use `Decimal` for USD, shares, fees, fills and budgets; ensure price tick and share precision are broker-specific. No floating-point accumulation in ledger or trade sizing. All event times UTC timezone-aware; present scheduling/UI in `America/New_York` (DST aware), consult exchange calendar for holidays and early closes.

### 5.1 Proposed typed ports (representative, not a frozen Python signature)

```python
class BuyStrategy(Protocol):
    @property
    def spec(self) -> StrategySpec: ...
    def evaluate(self, ctx: MarketContext, state: StrategyState) -> Evaluation: ...

class SellStrategy(Protocol):
    @property
    def spec(self) -> StrategySpec: ...
    def evaluate(self, ctx: MarketContext, holding: PositionView,
                 state: StrategyState) -> Evaluation: ...

class MarketDataProvider(Protocol):
    async def subscribe(self, symbols: set[str], channels: set[str]) -> None: ...
    async def events(self) -> AsyncIterator[MarketEvent]: ...
    async def history(self, query: HistoryQuery) -> HistoricalBatch: ...

class ResearchDataProvider(Protocol):
    async def get_snapshot(self, symbol: str, as_of: datetime,
                           required_fields: set[str]) -> ResearchSnapshot: ...

class Broker(Protocol):
    async def capabilities(self, symbol: str) -> BrokerCapabilities: ...
    async def account(self) -> AccountSnapshot: ...
    async def positions(self) -> list[BrokerPosition]: ...
    async def open_orders(self) -> list[BrokerOrder]: ...
    async def place(self, intent: OrderIntent) -> BrokerOrder: ...
    async def cancel(self, broker_order_id: str) -> CancelResult: ...
    async def order(self, broker_order_id: str) -> BrokerOrder: ...
    async def activities(self, cursor: str | None) -> list[BrokerActivity]: ...
    async def updates(self) -> AsyncIterator[BrokerEvent]: ...

class Clock(Protocol):
    def now(self) -> datetime: ...

class PortfolioRepository(Protocol):
    def transactional_apply(self, event: DomainEvent) -> None: ...
```

An evaluation returns **NO_SIGNAL**, **SIGNAL**, **INSUFFICIENT_DATA**, **STALE_DATA**, **ERROR**, or **NOT_DUE**; no exception should silently become a buy. Buy/sell strategy classes must not use broker SDK calls, filesystem secrets, or order submission; providers are injected into higher-level services.

## 6. Strategy library and rule semantics

### 6.1 Directory and registration

```text
mike_trade/strategies/
  base.py                  # Protocols, specifications, typed output, registry contracts
  registry.py              # Explicit stable registration; no arbitrary code eval
  buy/
    vwap_bounce.py
    opening_range_breakout.py
    momentum_breakout.py
  sell/
    vwap_breakdown.py
    momentum_reversal.py
    rsi_reversal.py
  indicators/              # Pure, reusable numerical functions (not new providers)
  presets/                 # Historical published buy/sell pairings and parameter sets
```

Names above designate extensibility and example plugin classes, **not authorization to invent their parameters or claim profitability**. Build a documented baseline rules library with explicit data requirements, methodology reference, default off unless supported, and tests. No strategy requires editing the execution engine. Plugin discovery may be a safe registry table/entry points with schema-validated metadata; import untrusted plugin code only from installed/trusted packages.

Each strategy declares: `id`, semantic version, side, required fields/indicators (including optional reference-only symbols when genuinely used by that strategy), timeframes, warmup bars, trigger kind, optional schedule, parameters/types/ranges, state needs, research citations/methodology, execution limitations, and diagnostic explanation codes. Rules output conditions and evidence; multiple strategies on a symbol are independently configured.

### 6.2 Independent buy and sell

For a stock in `WATCHING_BUY`, **only buy strategies** evaluate (plus risk/session/global monitoring). After confirmed acquisition the stock becomes `WATCHING_SELL`; **only sell strategies** evaluate, except shared protective stops/risk handlers. One accepted buy signal produces one entry cycle; every subsequent buy signal is ignored until the position is flat and rearmed. A signal can be edge-triggered (crosses), level-triggered (condition remains true) or a multi-step setup; plugin state persists so repetitive cycles do not repeat orders.

Independent **protective fixed/trailing stops live in the risk/protection subsystem**, not as ordinary `sell/` strategy plugins; stop triggers bypass discretionary timing and minimum-hold restrictions. Any enabled sell signal can request closing **100% of the existing managed position**, unless explicit conflict/eligibility policy vetoes it; no need to pair exits with their originating entry. Nonetheless retain matched-method presets for backtest comparison and entry/exit attribution. Configurable per-stock minimum hold only blocks discretionary sell signals; protective stops, manual sell, hard risk exits and forced liquidation bypass it.

Default discretionary policy: **first valid exit signal wins** after its prerequisites and minimum hold; simultaneous buy conflicts resolve by configured priority then deterministic strategy ID, with complete audit. Risk exit outranks every optional signal. If no buy signal, do nothing. Entry after cutoff is rejected. No automatic opposite-side trading.

### 6.3 Rule and research integrity

A rule definition must include an objective rationale, input requirements, boundary behavior, cooldown/rearm handling and test cases. Historical tests must report costs and compare with a simple buy/hold or no-trade baseline. Indicator constants, stop multipliers or empirical strength values cannot be represented as researched defaults without evidence. Preset strategy pairs remain available to reproduce source strategy behavior before testing arbitrary recombinations.

ML-based signal plugins may later implement the same protocols, but must record model version, feature lineage, train/validation windows and prediction confidence calibration. No strategy is claimed profitable by virtue of being implemented.

### 6.4 Fully working reference implementation: VWAP Bounce BUY + VWAP Breakdown SELL

The product **must ship a working, installable, independently testable reference strategy pair**, not a placeholder, `return BUY`, or an illustrative code fragment. Reuse the production classes `mike_trade.strategies.buy.vwap_bounce.VWAPBounceBuy` and `mike_trade.strategies.sell.vwap_breakdown.VWAPBreakdownSell` (exact class names are normative for the reference), registered as `vwap_bounce` and `vwap_breakdown`. These are two distinct signal modules assembled by configuration; either can be replaced without editing the other or the core engine. A preset (`vwap_reference`) binds them only for convenience, while independent per-stock buy/sell selection remains the general contract.

**Market inputs and timing:** Completed one-minute regular-session OHLCV bars; a shared volume-weighted average price (VWAP) that resets at the session boundary; data freshness and warmup metadata. Use the common feed/candle/indicator services; never open API connections from strategy code. The evaluator sees the previous and current **completed** bars and their corresponding VWAP values. Require the configured number of completed bars to warm up. Crossings must be edge-triggered, deterministic, and assigned stable identifiers so five-second scheduler reevaluations do not duplicate orders.

**Reference BUY predicate** (only while `WATCHING_BUY`): previous completed close `<=` previous completed VWAP **AND** most recent completed close `>` its completed VWAP. Optional, explicitly configured volume confirmation may be added as a filter; it must be disabled by default and never silently baked into the reference rule. Reject missing/stale/zero-volume/incomplete bars, invalid indicator values, outside-session signals, insufficient warmup, or an entry cutoff; report reason codes. The coordinator, not this strategy, enforces budgets, one-entry-per-cycle, loss-streak lockouts, daily entry caps and post-sale cooldowns.

**Reference SELL predicate** (only while a positive long quantity is held): previous completed close `>=` previous completed VWAP **AND** most recent completed close `<` its completed VWAP. Return an `EXIT_LONG` signal requesting the **entire position** via the shared order manager, respecting the configured discretionary minimum hold; a broker-held protective stop remains independent and can always fire. No sell evaluation may open a short or cancel a still-required stop prematurely.

**Reference configuration:** `config/reference_vwap_shadow.yaml` must be a valid, complete, documented config selecting one named, operator-editable symbol, `vwap_bounce` BUY and `vwap_breakdown` SELL, `1m` completed-candle triggers, Alpaca IEX shared input, a named local shadow account with starting cash, per-stock/total budget and a configured protective stop. The supplied example must be **shadow-only**, must not enable live API execution or contain secrets, and must allow symbol substitution with a config edit. Use the existing `make shadow-start CONFIG=config/reference_vwap_shadow.yaml` operator pathway (or an equivalent documented `CONFIG` argument on the existing Make target), not a hardcoded example-only execution path. The production reference config is runnable only with the required data and valid operator configuration, and its presence does not turn the product into a demo.

**Acceptance proof:** Unit-test both predicates at the crossing boundary, unchanged level, missing/partial bars, stale data, warmup, session-reset and multiple scheduler polls; prove matching no-signal reasons and no duplicate entry/exit. In an **offline deterministic synthetic-price end-to-end test**, run a BUY crossing → one simulated entry fill → protective stop attachment → later SELL crossing → stop cancellation/confirmation → entire holding exits → accurate shadow cash/equity/P&L → state returns to `WATCHING_BUY`. A second scenario triggers the stop before the discretionary SELL and proves no double sale. Run the same reference pair through bounded deterministic historical replay without lookahead. No external credentials, live orders, fetched historical datasets or hand-entered fees are needed for those fixtures. Include sample input/output logs and operator steps in `docs/STRATEGIES.md`.

**Research limitation:** This is a genuine functioning example of architecture, not validated positive alpha. Do not assert expected profits or put it into live trading automatically. Real model/strategy efficacy must be demonstrated independently with costs and out-of-sample data.

## 7. Market and research data architecture

### 7.1 Centralized acquisition

One `MarketDataService` owns **one authenticated provider subscription** for the union of symbols and required channels; subscriptions are updated when watchlist/config changes. Default live plan uses Alpaca Basic's **IEX** feed; this is not consolidated SIP coverage. Its trade volume, tape, VWAP and some price signals may differ substantially from full-market conditions. Preserve feed ID and coverage in every input and evaluation. Never label IEX-derived VWAP as consolidated-market VWAP. [A1][A2]

A shared, bounded memory cache maintains latest quotes/trades, candle builders, completed multi-timeframe candles, indicator rolling state and freshness metadata. Do not build a full-session tick recorder or market-data archive: retain the compact inputs and timestamps needed to explain decisions, and fetch historical bars on demand for warmup/backtests. All strategies consume immutable snapshots; prohibit strategies from polling prices directly. Shadow/live decisions reuse one data graph while position/account state remains mode-specific.

Historical bars for warmup and backtests are accessed through `MarketDataProvider.history`. If feed is IEX, backtests should use comparable IEX history or clearly quantify feed mismatch; paid SIP data is **optional**, not assumed. Corporate actions, splits and adjusted/unadjusted OHLCV must be explicit; never mix adjusted and raw prices within a stop calculation.

### 7.2 Data quality and resilience

- Track provider timestamps and received timestamps separately; calculate end-to-end latency and freshness.
- Distinguish trade, bid/ask, completed bar and interim bar. Indicators may request closed candles only.
- Explicit per-strategy stale thresholds and warmup requirements; prevent trading on missing/gapped history.
- On WebSocket loss, mark feed DEGRADED, stop new entries, reconnect with backoff, retrieve bounded gap history where possible, rebuild indicators and resume only after validation.
- REST polling fallback is configurable but must honor documented rate limits; fetch once per symbol/cycle and fan out internally.
- Never silently merge quotes from differently entitled feeds without marking provenance.
- All network operations bounded by timeout, retry policy and circuit breaker; retries on trade placement require lookup by client ID before any resubmission.

### 7.3 Research data sources

`ResearchDataProvider` is separate from `MarketDataProvider` and `Broker`. Strategy-specific research inputs may include reference-symbol prices, filings, corporate actions, or licensed information, only when an enabled strategy actually requires them. No built-in earnings/economic-news blackout scheduling or generic stock-screening workflow is part of this product. Inputs carry source, publication time, observed time, ingestion time, revision identity, license restrictions, missing status and freshness. A rule can declare mandatory versus optional research fields; mandatory absent data = no signal. Paid provider integrations are not required for basic operation and must not introduce invisible network calls or manual file uploads.

## 8. Scheduling and concurrency

**One engine-owned scheduler** with default cadence and per-strategy triggers, not a separate timer/process per rule. Support triggers: market data arrival; quote change; completed 1m/5m/15m/etc candle; explicit fixed interval; session open, entry cutoff and session close; manually requested evaluation. A strategy is evaluated only when its declared inputs have become eligible or its schedule is due. A global evaluation interval acts as an upper cadence/control tick; not as a mandate to recompute closed-bar strategies every five seconds.

**Concurrency:** asyncio for WebSockets/broker I/O/scheduler; bounded `ThreadPoolExecutor` for independent lightweight evaluations; optional process workers for CPU-heavy models. Results are delivered to a single ordered decision queue with deterministic tie-breaking. **One serialized account coordinator** reserves budget and submits at most one active entry/exit lifecycle per account-symbol; it must use durable locks/unique constraints so restart or two workers cannot double-order.

Queue management: bounded queue, snapshot versioning, watermark/backpressure, discard stale unacted market evaluations, never discard order updates or fills. Each evaluation records as-of time and signal expiry; compare expiry and quote freshness again at order submission. Expose scheduler delay and dropped/coalesced update metrics.

Defaults are configuration examples, not immutable settings. **Safety exceptions** (broker order updates, stop reconciliation, exchange halt and emergency controls) process promptly independent of discretionary strategy frequency.

## 9. Per-stock lifecycle and transition policy

A *cycle* represents one logical purchase and one logical disposition of its entire acquired quantity, including partial exchange fills. Its protection controller runs independently of discretionary BUY/SELL scheduling and evaluates trailing updates as fresh eligible quotes/trades arrive.

**States:** `DISABLED`, `WATCHING_BUY`, `ENTRY_RESERVED`, `BUY_SUBMITTING`, `BUY_PENDING`, `PARTIALLY_OWNED`, `PROTECTIVE_PENDING`, `WATCHING_SELL`, `SELL_SUBMITTING`, `SELL_PENDING`, `CLOSING`, `RECONCILING`, `PAUSED`, `ERROR`, `UNMANAGED`, `CLOSED` (transient). Prefer orthogonal `service_health`, `order_state`, `protection_state` attributes rather than dozens of opaque combined states; show an understandable composite phase to the dashboard.

| Current condition | Event | Required behavior |
|---|---|---|
| Flat / WATCHING_BUY | Eligible buy signal | Reserve budget, validate broker buying power, order intent → BUY_PENDING |
| BUY_PENDING | First partial fill | Record actual shares; enter PARTIALLY_OWNED; reconcile adequate broker protection; no second entry |
| BUY_PENDING | Full fill | Record basis/time, verify stop, transition WATCHING_SELL |
| BUY_PENDING | Canceled / expired unfilled | Release reserves; return WATCHING_BUY after eligibility reset |
| BUY_PENDING | Canceled / expired partially filled | Keep actual shares, stop/reconcile remainder, monitor sell; do **not** automatically buy missing shares |
| WATCHING_SELL | Valid discretionary exit and minimum hold passed | Begin coordinated stop cancellation/close lifecycle |
| WATCHING_SELL | Protective stop triggers | Record broker fill(s); bypass minimum hold; reconcile remaining position |
| WATCHING_SELL | Fresh eligible price advances trailing watermark/step | Recompute candidate stop; only raise; update broker protection by verified supported method; persist confirmation, or mark DEGRADED and alert |
| SELL_PENDING | Partial exit fill | Update remaining quantity and protective coverage; prohibit new buy until flat |
| SELL_PENDING | Full exit fill | Verify zero quantity and no stale protective sell orders; release budget; transition WATCHING_BUY if eligible/rearmed |
| Any active state | Operator pause | Block **new entries**; keep broker events, exits, stops and reconciliation active |
| Any state | Unknown broker state / stale data | Stop new entries; reconcile or mark DEGRADED/ERROR; never manufacture position data |
| Restart | Any stored open cycle | Fetch broker truth, resolve pending actions and stop coverage, then resume only if safe |
| Existing external position | Broker reports unknown position | Display UNMANAGED; block automated symbol trading, request explicit adopt/ignore/close policy |

**Ordering principles:** A stop may fill during stop cancel, or between quote and order; confirmations alone cannot be trusted to freeze market state. Cancel requests are not cancellations. Before discretionary SELL, coordinate and confirm release of blocking protective sell order(s), re-read remaining position, submit close only for confirmed remaining shares, then reconcile. If cancellation/status cannot be established, **do not send an overlapping sell that could short the position**; alert the operator and maintain risk state. Where broker-native linked orders provide a stronger solution, prefer them after capability verification.

**Rearm and entry eligibility:** Once flat, a persistent level signal must rearm (new crossing or explicit strategy reset) before causing another entry. In addition, each newly proposed buy passes (1) the configured per-symbol maximum **filled entry cycles** for the current New York exchange session, (2) the consecutive losing-trade lockout for that symbol and any enabled strategy-wide threshold, and (3) the per-symbol post-sale cooldown measured from the final closing fill. Any failed check blocks the BUY with a specific, logged reason and next eligible time or reset instruction. All limits are independently configurable and may be disabled by setting them to `null`/`0` as documented. No buys run during a cooldown or lockout, while stop protection, fills, discretionary sells, manual closes and reconciliation remain fully operational.

**Count semantics:** One position cycle consumes one daily entry slot at its **first confirmed positive buy fill**, whether whole or fractional; subsequent partial fills are part of that same entry. Rejected or canceled orders with zero fills consume no slot. Counts are scoped per broker/shadow account, symbol, and exchange-local trading date; restart cannot erase them. Loss streaks advance on a completely closed managed cycle with negative **net realized P&L** (known actual costs or explicitly labeled estimates), reset on a positive result, and remain unchanged on exactly breakeven. Attribute each losing cycle to its originating BUY strategy, not the SELL strategy; optional strategy-wide streaks aggregate that strategy's own closed trades across symbols in the same execution account/scenario. Unknown/late fees require a clearly marked provisional classification and auditable correction, not zero-fee fabrication.

**Lockout/reset policy:** Configurable `loss_streak_reset: next_session | manual`. `next_session` permits new entries at the next regular market session; `manual` requires an explicit, audited CLI reset command. Daily entry-cycle allowance resets at the exchange's next session, not after 24 elapsed hours. A post-sale cooldown begins on confirmed **full closing fill**, applies after any kind of exit (including stop), and remains effective across restart. A fresh buy after any reset still requires a valid newly armed signal; resetting a streak does not bypass budgets, account risk or active stop requirements.

## 10. Capital, pricing and sizing policies

### 10.1 Budgets

- `total_budget_usd`: maximum total **application commitment** across managed open positions and outstanding entry orders for a given execution account/scenario; not the broker's account equity.
- `symbol_budget_usd`: maximum notional committed to a symbol's one entry cycle; no topping up.
- `max_planned_loss_usd` (optional or required by account policy): intended stop-based exposure, **not** a guarantee; cannot promise hard downside bound.
- `max_open_positions`, `daily_loss_limit_usd`, `max_entry_cycles_per_day` (per symbol), `max_consecutive_losses` (per symbol and optional entry-strategy scope), `post_sale_cooldown_seconds` (per symbol), `no_margin`, `max_bid_ask_spread_bps`, `minimum_liquidity`, reserve cushion, minimum order value and broker buying-power guards are independent limits.
- Only this application's managed positions count toward the application budget unless configured otherwise; **all** positions, including manual/unmanaged, count toward broker cash/buying-power and concentration risk where relevant.
- Account budget reservations cover pending buys and actual filled notional, update transactionally; release unused reserves on final cancel/partial fill. Budget is not per-symbol imaginary cash: account-level cash remains authoritative.

### 10.2 Sizing

For a valid long entry, size from remaining symbol budget, remaining total budget, broker buying power, estimated adverse price/slippage cushion, permitted share precision, and (if required) stop-distance loss cap. Round **down** to supported quantity; reject if no valid positive quantity or protection cannot be established. Fractional quantities supported only if asset and requested order/protection capabilities permit. If a per-symbol budget is $500, a $200/share whole-share trade may buy 2 shares; fractional orders may use remaining budget only when configured.

**Important provider nuance:** Alpaca distinguishes quantity-based and notional orders and restricts replacing notional orders; decide and persist order sizing type before submission. Fractions and stops appear in current docs with DAY TIF, but advanced linked-order support must be proven separately. [A3][A4] Do not silently buy unprotectable fractions or switch to a client-only stop without explicit policy acknowledgment.

If the operator has insufficient actual broker buying power, reject with `INSUFFICIENT_BROKER_BUYING_POWER`. The system never creates deposits or assumes unverified cash. For multiple simultaneous buy signals, process deterministic configured symbol/strategy priority, under the shared budget lock; later signals that cannot be funded are recorded as rejected.

## 11. Protective stops, optional exits and closing behavior

**Default:** a protective stop policy is required for every automated live long entry, either defined per symbol or inherited from an account policy. A symbol may select `fixed`, `trailing_native` or `trailing_stepped`; the protection manager owns the single effective protective sell order, not a separate discretionary strategy. Stop price can derive from percent, fixed USD, ATR, technical support or an explicit strategy recommendation, subject to validation. The level is calculated from a well-defined reference (confirmed fill price when applicable) and rounded to broker tick rules. Protective stop is a *broker order*, not just an in-process threshold. Stop-market differs from stop-limit: a stop-limit may remain unfilled during a gap, whereas stop-market may fill far below trigger. Present that tradeoff clearly in config/docs/dashboard.

**Whole shares:** consider broker-linked OTO (entry plus one stop), depending on supported order type and chosen pricing; Alpaca documents OTO with one stop leg. Brackets include a take-profit and stop, which may impose unintended price-target behavior and **must not** be required merely to obtain a protective stop. [A5] If a chosen linked order cannot satisfy our strategy's exit semantics, use a documented alternate workflow and test its failure window.

**Fractional shares:** Alpaca's fractional-trading documentation currently lists market, limit, stop and stop-limit DAY orders, but linked OTO/bracket/OCO fractional eligibility and associated partial-fill behavior must be tested against the actual account (paper first). [A3][A4][A5] Implement a `BrokerCapabilities` decision matrix; reject the entry if the configured protection invariant cannot be satisfied. An entry-to-stop gap must be surfaced as `UNPROTECTED_POSITION` with an immediate operator alert and restrictive policy, not obscured.

**Stop after exit:** When a discretionary/manual sell happens, cancel associated protective orders **only as part of a coordinated close**. Confirm cancellation/fill/remaining quantity; do not assume OCO auto-cancel unless the actual linked order class guarantees it. Verify that no live stop remains after a fully flat position. If a stop partially executes, update remaining shares and downstream exit management.

**Day orders:** Fractional stop orders with `time_in_force=day` can expire at session end. That is acceptable only under a deliberate policy (the operator normally closes before market end); any overnight residual must be displayed as `OVERNIGHT / PROTECTION EXPIRED` and handled before a new day. Do not silently promise broker protection while markets are closed or stops are expired. [A4]

**Minimum hold:** per-stock seconds from first/last fill (choice explicitly configured; default from first completed entry) blocks only ordinary strategy SELL. Manual `close`, broker stop, emergency risk exits and optional automatic close bypass it. Dashboard shows remaining hold lock time.

**Close behavior:** `make close SYMBOL=...` closes full remaining managed quantity, using supported order type and broker truth. `make close-all` requires live confirmation and reconciles each result. `make flatten` is alias for intentionally closing managed open positions, NOT identical to `make stop`. Automated end-of-day closing is optional; disabled by default to match operator practice, with configurable cutoff/deadline and repeated verification if enabled. Entry cutoff (e.g., 15:00 ET) is separate and should be on by default with a user-chosen time. On half-days, reject entries after effective early-close buffer. We do **not** guarantee that all holdings can be liquidated (halts, rejections, illiquidity).

### 11.1 Automatic trailing-stop protection (required production feature)

**Operator behavior:** For each stock, configure whether trailing protection is enabled, the trailing **distance** (fixed dollars per share or percentage below its high), and—for stepped mode—the **upward advance increment** (fixed dollars or percent). After the first confirmed buy fill, automatically establish broker-held sell-side protection for the actual filled shares; when the buy completes or partially fills, reconcile the protected quantity with the actual remaining position. Protection is attached to the position cycle, independent of any buy/sell rule, and runs even when discretionary sell evaluations are paused or minimum hold time has not elapsed. Native continuous trailing is the preferred operating choice; `TrailingStopPolicy` is provider-neutral, and `trailing_stepped` computes its own increments in application code through the replaceable `Broker` port rather than through Alpaca-specific logic. If the configured quantity/order combination cannot be broker-protected, block the entry where preflight can determine that; otherwise declare `UNPROTECTED_POSITION`, alert and block new buys rather than concealing the gap.

**Modes (default preference: native continuous):**

- `trailing_native`: Submit an Alpaca standalone `trailing_stop` sell order using `trail_price` **or** `trail_percent`. Alpaca advances its own `hwm` and derived `stop_price` on favorable movements; it is continuous, not increment-based, and continues at the broker if the application disconnects while the order remains eligible. Alpaca documents DAY/GTC, no triggering outside regular hours, and standalone orders only—not trailing legs inside OTO/bracket/OCO. Read back broker `hwm`, `stop_price`, order status, and share coverage. Do not manufacture a stepped UI for native behavior. [A4][A13]
- `trailing_stepped`: Maintain a persisted, monotone `high_watermark` and `ratchet_anchor` from **fresh eligible regular-session market data**. Start with the stop at `entry_reference − distance` (or entry reference × (1 − percent/100)), appropriately rounded. Each time the observed favorable advance crosses **one or more full configured steps**, advance the anchor by all crossed steps, recalculate the candidate stop from that stepped anchor, and raise the stop only if `candidate_stop > last_confirmed_stop` after broker price rounding. A single price jump can traverse multiple steps. Price declines or new highs smaller than the configured step never lower or reprice the order. Use a broker-held standalone stop order with a verified `stop_price` replacement workflow where supported. Never fake continuity by relying only on an in-process threshold. [A4][A14]
- `fixed`: A fixed broker-held protective stop remains supported. Configure exactly **one effective protective sell order per holding**; a fixed-loss floor and a trailing rule may be composed into the same stepped target as `max(floor, stepped_target)`, but must not create two competing full-quantity sell orders. A native trailing order does not implicitly provide a separate broker-guaranteed entry-based loss floor.

**Deterministic stepped example:** Buy 10 shares at $100, with a $2 trailing distance and $1 upward step. Initial stop $98. At high $100.80 it remains $98. At high $101.00 it rises to $99; at $103.20 it rises to $101; if price falls to $102, it remains $101. A stop trigger requests sale of the **entire remaining quantity**, subject to actual broker fills. A sell stop-market may execute below its trigger (gaps/slippage); it is not a guaranteed profit or hard loss limit.

**Replacement and race safety:** Updating a stepped stop requires capability-checked broker replacement, **not** cancel/recreate by default. Alpaca's PATCH semantics are asynchronous; a successful response is not proof that the old order has been replaced. Persist intent, await trade update or fetch authoritative order state, then confirm the new stop before treating it as effective. If replacement is rejected/unknown, preserve the last verified broker protection if it still exists, suspend additional entry activity, surface `TRAIL_UPDATE_UNCONFIRMED`, and reconcile; never submit an overlapping full-position stop or discretionary SELL. Respect update rate limits, broker tick/quantity precision, replacement cooldown, market hours and restrictions on fractional/notional orders. Native stop trails cannot be converted from dollar to percent by PATCH. [A13][A14]

**Lifecycle and recovery:** Persist entry reference, high-water mark, step anchor, monotone desired and confirmed stop levels, broker order IDs/status and policy/version for every update. On restart, recover from broker orders and fills **before** resuming ratchets; reconcile broker native `hwm` and `stop_price` as authoritative, or reconstruct stepped state from durable events without guessing missing market highs. A stopped/offline app leaves its **last confirmed stepped** broker stop in force if still valid, but it cannot continue step updates until online; mark `TRAIL_STALE/OFFLINE` on dashboard. GTC versus DAY expiry and overnight gaps remain explicit risks. On strategy/manual full SELL, coordinate stop cancellation/fill and verify the position is flat with no orphan sell order; native trailing fill is itself an exit and bypasses minimum hold.

**Data/reporting/controls:** Dashboard and CLI show mode, distance, step, entry reference, observed/broker high watermark, anchor, desired/confirmed stop, protected shares, last update time, broker order ID, `ACTIVE/UPDATING/UNCONFIRMED/TRIGGERED/EXPIRED/OFFLINE` state, and warning if data feed/protection diverges. Append durable `trail_initialized`, `trail_high_updated`, `trail_step_crossed`, `trail_replace_requested/confirmed/rejected`, `trail_triggered`, and `trail_canceled` events; retain old/new prices and source timestamps. Shadow/paper/backtest must implement the same policy with explicit execution assumptions and compare continuous versus stepped exits without lookahead. Commands: `make trail-status SYMBOL=...`, `make trail-history SYMBOL=...`, `make trail-policy-show SYMBOL=...`, `make trail-policy-set SYMBOL=... ...` (validated, versioned YAML and guarded changes for open positions), and `make trail-reconcile SYMBOL=...`. Never silently disable active protection through config changes.

**Alpaca-specific gate:** Official docs list `trailing_stop` for standalone equity orders with `trail_price`/`trail_percent`, but do not establish all fractional-share trailing-stop or quantity-replacement combinations; paper-test each configured path and reject unsupported live paths rather than claiming fractional parity. Verify broker trigger reference versus free IEX quotes; provider-native hwm need not equal local IEX high. [A3][A4][A13][A14]

## 12. Order-management subsystem and Alpaca integration

### 12.1 First broker: Alpaca Trading API, not Broker API

Use the **individual Trading API** with separate API credentials and URLs for paper/live. The enterprise **Broker API** is a different product and must not be assumed for a personal account. [A6][A7]

**Required Alpaca adapter operations:**

1. Health/connectivity, account read, status flags, equity/cash/buying power, asset capability read.
2. Current positions, list/open orders, get by broker order ID and `client_order_id`.
3. Submit supported BUY/SELL market/limit, fixed protective stops, native trailing stops (`trail_price`/`trail_percent`) and capability-checked stepped stop replacements, including whole/fractional modes only where documented/validated.
4. Cancel order(s), track pending_cancel vs canceled; replace **only** when provider/order type permits and materially required. In particular, stepped trailing replacements require persisted intents, status confirmation and race-safe reconciliation.
5. Broker order/event stream (`trade_updates`) for accepted, partial fill, fill, reject, cancel, replace; periodic REST reconciliation to cover missed stream messages. [A8]
6. Broker trade/account activities pagination to import actual fills, corporate actions and fees when published. [A9]
7. Exchange clock/calendars plus per-asset metadata; avoid using the host clock as sole authority for market status.
8. Rate-limited retry/backoff, correlation/request IDs, instrument precision, per-account capability checks and structured errors.
9. No automatic paper-to-live endpoint crossover; broker fingerprint and account ID confirmation before live activation.

**Order lineage:** Generate stable `client_order_id` derived from account mode + cycle + purpose + attempt, persist order intent **before** network send. On timeout/unknown response query by client ID; never retry with a new ID until original status definitively resolved. Persist Alpaca response request IDs when available. [A7][A10]

**Connectivity:** Alpaca Basic allows an IEX real-time stock feed; official Trading API supports stock orders, free paper trading, and websocket order updates. [A1][A6][A8] Research and broker API rate limits must be configurable from documented limits and runtime headers, not invented.

### 12.2 Provider capability matrix and unresolved facts

| Capability | Documentation status | Required proof / action |
|---|---|---|
| Stock order API + paper account | Documented | Implement normal integration |
| Whole-share basic orders, stops and OTO | Documented | Verify stop-only OTO lifecycle with paper fills |
| Fractional basic market/limit/stop/stop-limit DAY | Described in current fractional/order docs | Confirm account capability, order acceptance, broker precision |
| Fractional OTO/bracket / linked stop | **Not established by general fractional-order docs** | Explicit integration tests; prohibit unsupported configurations |
| OTO replacement | Docs say **not supported yet** | Do not design protective-stop changes that depend on this [A5] |
| Notional-order replacement | Docs say **not supported** | Cancel/reconcile/resubmit only if safe [A4] |
| Broker-native trailing stop (whole share) | Alpaca documents `trailing_stop`, `trail_price`/`trail_percent`, `hwm`, and DAY/GTC | Implement as standalone order; verify lifecycle, regular-hours trigger and position coverage [A13] |
| Trailing stop as bracket/OTO/OCO leg | Alpaca docs say **not currently supported** | Do not use trailing as linked order leg [A4][A13] |
| Fractional trailing and stepped replacements | Not established by current fractional/TIF tables | Verify in Alpaca paper per symbol/order shape; reject live use if unsupported [A3][A4][A14] |
| Two competing sell orders on same position | Broker may reject or oversell in race | Single protective-order coordinator mandatory [A5] |
| Broker stop guarantees max loss | **Not guaranteed** | Warnings, planned loss vs actual risk separate |
| Whole/fractional automated end-of-day closing | Order/session restrictions vary | Verify supported TIF and early-close handling |
| Fees/fill attribution | Activity API available, account-specific charge detail varies | Import what actually exists, mark missing as unavailable [A9] |

**Do not confuse** statements from outdated Alpaca documentation with current order-type matrices. Official pages have had inconsistencies; implementation must isolate exact behavior behind feature probes and paper tests rather than relying on one summary. This is an explicit acceptance gate, not permission to skip the feature.

### 12.3 Order state machine

`INTENT_PERSISTED → SUBMITTING → ACCEPTED/PENDING → PARTIALLY_FILLED → FILLED`, with branches to `REJECTED`, `CANCEL_PENDING`, `CANCELED`, `EXPIRED`, `REPLACED`, `SUSPENDED`, `UNKNOWN`. State transitions consume deduplicated event IDs and can arrive out of order. Do not retroactively infer filled quantity from mere order acceptance. Broker state is authoritative for real execution; append corrections as events rather than rewriting history silently.

**Critical race tests:** stop fills while canceling; discretionary close partly fills; server dies after intent persistence but before response; duplicate order update; broker reports a position without corresponding local fill; account status disables trading; fraction precision mismatch; broker cancels DAY stop at market close; live feed disconnects while broker still receives fills.

## 13. Shadow trading and Alpaca paper trading

### 13.1 Local shadow simulator

Each named scenario has a durable starting cash (`set once`), explicit adjustments/transfers, independent budgets, independently enabled strategies and configuration revisions, positions, orders, cash, P&L and fee assumptions. Read live shared data without writing to the real broker. Multiple shadow scenarios may evaluate the same market snapshot concurrently; their decision/portfolio queues and ledgers remain isolated.

Simulated **buy execution** uses contemporaneous ask where available, **sell** uses bid, with configurable slippage and an explicit liquidity participation/size policy. Simulate `trailing_native` as continuously ratcheting only on observed eligible data and `trailing_stepped` with the identical production step/offset policy; record stop trigger versus resulting simulated fill separately. Do not fill at bar closes as though executions were certain. Model order latency, market-session restrictions, lot/fraction precision, market impact assumptions (even if negligible at 1–10 stocks), pending orders, partial fills where evidence allows, stops triggered according to available quote/trade granularity and bid/ask. If quote/size data is insufficient, report fill **uncertain** and take conservative/no-fill path rather than invent certainty.

Shadow fills are estimates and must carry the simulator version, assumptions, data feed and confidence of fill model. They are not broker-confirmed. Maintain cash/equity/P&L continuously and persist through restart; restarting must never reset a shadow account. Transaction costs use zero commission where applicable but include spreads/slippage and estimated statutory fees if configured; separate estimated from confirmed.

### 13.2 Concurrent scenarios

Support `baseline`, `experimental_vwap`, etc., with scenario-specific symbol assignments and strategy selection. The shared producer pays market-data cost once; no duplicate streaming connections. Only authorized execution account may submit real orders. Dashboard supports scenario selector, side-by-side comparison, mode badges and clear provenance. No strategy code change between modes.

### 13.3 Alpaca paper integration

Use separate paper credentials to test real Alpaca order API/status, fractional/stops and recovery semantics. Paper fills are simulated by Alpaca; they do not guarantee comparable live performance, but can prove endpoint behavior and overall control integration. Local shadow account is still required even when paper trading exists.

## 14. Historical backtester and strategy-validation workbench

Backtesting is a **finished product subsystem**, not a disposable test script. Reuse the actual strategy registry, clock, scheduler semantics, budget policies, position coordinator, and shadow fill simulator with a historical market-event provider. No real broker API access in replay. Each run has a stable manifest with config hash, data feed/source/revision, time window, code revision, starting capital, fees/slippage and algorithm versions.

- Chronological warmup, point-in-time historical facts, no use of an unclosed candle or revised/future source data.
- Historical quote/bid-ask fidelity flagged; reject/label tests that cannot substantiate claimed fill quality.
- Include market halts, early close, gaps, missing bars, splits and day-session rules. Model trailing-stop high-water updates only from point-in-time eligible ticks/bars, simulate crossing multiple steps in one event, order-replacement delay and stop-market slippage; coarse OHLC bars cannot establish an unknown intrabar ordering of high versus stop trigger, so mark uncertainty/use conservative sequencing.
- Reports: total/net return, cash/equity time series, per-trade profits, max drawdown, profit factor, win rate, average win/loss, expectancy net costs, holding period, entry/exit strategy pairing, maximum favorable/adverse excursion, symbol/month/time-of-day stratification, capital utilization, sample size and uncertainty.
- Baselines: source-original entry/exit pair; independent mix-and-match; no-trade and simple market/index benchmark as appropriate.
- Walk-forward validation; separate research/dev from untouched out-of-sample testing, pre-register evaluation metrics and forbid re-tuning on a locked holdout.
- Backtest **execution logic tests** can use synthetic fixtures. Claims about real profitability require sufficient real, time-consistent market history.

## 15. Persistence model, audit history and retention

Use SQLite with explicit migrations, foreign keys, WAL, integrity checks, backups, transactions, indexed UTC timestamps and schema/version metadata. Avoid one global text file as the source of truth. Plain text/JSONL logs remain useful for human troubleshooting and data export.

**Core tables (logical, normalized):**

| Table | Purpose / important keys |
|---|---|
| `run_config_fingerprints` | startup/session ID, effective config checksum, redacted values required to interpret trades; audit only, not a config history/rollback product |
| `operator_commands` | id, mode, command, actor, submitted time, result, confirmation |
| `services_health` | engine heartbeat, broker/feed/scheduler state, last error, timestamps |
| `instruments` | canonical symbols, asset IDs, precision, capabilities, last validated |
| `market_snapshots` | optional compact quoted snapshots, feed, symbol, as-of, received-at |
| `candles` | OHLCV, timeframe, close/final marker, provenance, session |
| `strategy_instances` | per-symbol+mode+scenario strategy binding, parameters, version |
| `strategy_evaluations` | evaluation ID, time, symbol, strategy, input hash, status, evidence, duration |
| `signals` | signal ID, strategy evaluation ID, intent, freshness, priority, reason |
| `signal_decisions` | accepted/rejected/deferred, rule/policy trace, state at decision |
| `position_cycles` | account+symbol unique open cycle, state, entry/exit signal refs |
| `account_reservations` | pending capital claims, amount, status, tied to intended order |
| `order_intents` | stable client ID, mode, phase, symbol, qty/notional, stop link, outcome |
| `broker_orders` | broker ID, client ID, class, status, fill qty, raw payload hash |
| `order_events` | event/provider ID, status change, message, received time, broker time |
| `fills` | external activity ID unique, order/cycle link, price, qty, timestamp |
| `protection_orders` | cycle, broker stop/order class, stop price, qty, last confirmed status |
| `trailing_stop_state` | cycle, mode, policy revision, fill reference, high watermark, step anchor, desired/confirmed stop, last replacement and broker reference |
| `trailing_stop_events` | append-only ratchet/replace/trigger/cancel journal with old/new level, quote source/time, broker confirmation and failure reason |
| `positions` | current position read model, account/mode, qty, avg basis, last reconciled |
| `cash_events` | debits, credits, deposits/adjustments, transfers, fees, corrections |
| `equity_snapshots` | broker account or shadow balances, estimated vs authoritative, as-of |
| `reconciliation_runs` | status, delta, external positions/orders, approved resolution |
| `strategy_state` | persistent rearm/setup progress keyed strategy+symbol+scenario |
| `entry_eligibility` | session-scoped filled-entry counts by account/symbol, per-symbol loss streak, optional buy-strategy loss streak, post-sale cooldown expiry, entry lockout status and audit |
| `backtest_runs` | immutable run config manifest, input window, outputs, validation gate |

**Two forms of truth:** broker live `account/positions/orders/fills` versus locally derived journal; discrepancies produce explicit drift and blocked entries. Event data uses unique external IDs + replay cursors for exactly-once *application*, not imaginary exactly-once network delivery. Retain every order, fill, signal, decision, active run-config fingerprint and balance change. Store compact timestamped evaluation evidence, not the entire market feed or a multi-day raw tick archive. On-demand historical data for backtests comes from the configured data provider; preserve references and the exact market observations used when an actual trade decision was made.

**Reports:** `daily`, `weekly`, `lifetime`, `symbol`, `entry-strategy`, `exit-strategy`, `strategy-pair`, `scenario-comparison`, `risk`, `fees`, `execution-quality`, `signal-evaluations`, `reconciliation`, `tax-data-export` (transaction export, not tax advice). Each report distinguishes realized from unrealized P&L; cash from equity; verified fees from estimates; user deposits from trading profit.

## 16. Dashboard product specification

### 16.1 Platform

Standalone FastAPI read API + lightweight responsive UI (server-rendered HTML/HTMX or similarly small framework), bound to loopback/LAN interface by config. No separate heavyweight frontend build necessary; no general-purpose stock charting terminal or chart history viewer; only small state/protection visuals if they clarify our own orders. Dashboard process never places orders or changes settings. If served beyond localhost, require authentication/TLS via private VPN/reverse proxy; no publicly exposed unauthenticated trading information.

### 16.2 Global status and summary

Persistent top banner: `LIVE / PAPER / SHADOW`, service `RUNNING / PAUSED / DEGRADED / OFFLINE / RECONCILING`, heartbeat age, broker connected, data feed connected, market `OPEN / CLOSED / HALTED`, exchange clock, active configuration summary and entry cutoff countdown. OFFLINE is computed independently by the dashboard from **missed heartbeat**, not a value that the dead engine must send. Last known values shown with stale watermark.

KPI ribbon for each selected account/scenario: available buying power/cash, **total configured budget**, reserved pending buys, deployed notional, open positions, current equity, daily realized/unrealized P&L, cumulative P&L, pending orders, unresolved protection and data errors.

### 16.3 Watchlist grid (required)

| Field | Example / semantics |
|---|---|
| Symbol / mode | `AAPL`, `LIVE` or scenario |
| Price / bid / ask / data age | IEX quote, last seen, stale flag |
| Phase | WATCHING BUY / BUY PENDING / OWNED-WATCHING SELL / SELL PENDING / PAUSED / ERROR / UNMANAGED |
| Strategies | enabled buy or sell IDs, next due trigger, last evaluation |
| Last signal | strategy ID, event time, rationale, accepted/rejected |
| Position | broker-confirmed shares (fractionals), cost basis, purchase time |
| Risk | per-stock budget, committed $, planned loss cap, stop price, stop broker status |
| Entry eligibility | today's completed/filled entry cycles vs cap; consecutive losing closes vs threshold; loss lockout/reset mode; post-sale cooldown remaining and next eligible buy |
| Trailing protection | native/stepped/fixed, offset, rise increment, high watermark, current confirmed stop, shares protected, last update, offline/stale/error state |
| P&L | unrealized, realized today, lifetime by symbol; avoid calling unrealized profit a cash balance |
| Timing | hold lock remaining; entry cutoff; next evaluation |
| Controls | read-only status; command string for Makefile action |

Click-through symbol detail: trailing-stop ratchet chart/history with broker confirmation and timestamps, entry/high/stop progression, and any unsafe gaps; trade-relevant input evidence with feed label (without recreating third-party charting software); signal/evaluation timeline (including NO_SIGNAL summaries); current order tree and stop; last 20 trade cycles; realized P&L, duration, maximum favorable/adverse excursion, execution slippage; policy decisions and data provenance. An account page shows broker truth, external/unmanaged holdings, settled vs unsettled cash as available, fees and corporate events. Shadow scenario summaries expose separate running net P&L and logs; no dedicated strategy-comparison dashboard is required. Health page details heartbeat, websocket reconnects, subscription count, rate-limit state, exception summaries, scheduler lag, backup status and reconciliation timestamp.

### 16.4 Dashboard behavior tests

- Engine process killed: dashboard remains reachable; within configured timeout shows OFFLINE and last known timestamps.
- Feed dead, engine alive: DEGRADED / stale feed and no new buys.
- Broker disconnected, market feed alive: cannot present broker positions as fresh.
- One partial fill, one live stop: exact remaining shares/stop status shown.
- Stepped trailing advances only on complete increments, never moves a stop down, shows last broker-confirmed stop separately from proposed price and flags stale/offline updates.
- Native trailing shows broker `hwm`/`stop_price` and does not falsely claim stepped updates.
- Mode switch: never merge shadow cash into live account equity.
- Symbol budget exhaustion and entry cutoff: clear reason in UI.

## 17. Configuration specification

One YAML configuration with an explicit schema version, validated automatically at startup and when a Makefile/CLI config edit is applied; optional `.env`/systemd encrypted or OS-protected secret variables. Precedence: defaults → main config → optional mode-specific config → environment for secrets/runtime → CLI one-shot explicit overrides. Show effective **redacted** active config on the dashboard/CLI and record an SHA-256 fingerprint against trading sessions; apply CLI edits atomically after schema validation. Do not build a user-facing configuration-history/diff/rollback system or separate validation-preview script. Changing selected symbols/strategies is allowed without restart where safe, but cannot orphan an open position/protective stop; disallow editing core live safety settings during an unresolved order.

Illustrative complete skeleton (numbers are placeholders; **not recommended settings**):

```yaml
schema_version: 1
service:
  name: mike-trade
  timezone: America/New_York
  data_directory: state
  dashboard_bind: 127.0.0.1
  dashboard_port: 8765
  engine_heartbeat_seconds: 5
  offline_timeout_seconds: 20

market_data:
  provider: alpaca
  feed: iex                 # Basic; not consolidated SIP
  mode: websocket
  polling_fallback_seconds: 5
  quote_max_age_seconds: 10
  candle_timeframes: [1m, 5m, 15m]
  history_warmup_days: 10

research:
  providers: []            # Optional, plugin adapters; no secret in YAML

scheduler:
  default_interval_seconds: 5
  evaluation_workers: 4
  queue_capacity: 2000

execution:
  broker: alpaca
  authorized_live: false    # Must also satisfy separate runtime authorization
  account_mode: shadow     # shadow | paper | live
  allow_shorting: false
  fractional_enabled: true
  entry_order_type: market
  discretionary_exit_order_type: market
  regular_hours_only: true
  entry_cutoff_et: '15:00'
  close_all_at_end_of_day: false
  automatic_close_deadline_et: null
  block_new_entries_when_unprotected: true
  stop_required: true
  permit_margin: false

risk:
  total_budget_usd: '5000.00'
  max_open_positions: 5
  daily_loss_limit_usd: '100.00'
  allocation_priority: [AAPL, NVDA]
  min_buying_power_cushion_usd: '50.00'
  max_spread_bps: 50
  default_max_entry_cycles_per_day: null  # No cap unless selected per stock
  default_max_consecutive_losses: null    # No automatic streak lockout unless set
  default_loss_streak_reset: next_session
  default_post_sale_cooldown_seconds: 0

# Optional buy-strategy-wide streak limit (across symbols, per account/scenario).
strategy_entry_limits:
  momentum_breakout:
    max_consecutive_losses: 3
    loss_streak_reset: next_session

symbols:
  AAPL:
    enabled: true
    budget_usd: '500.00'
    max_planned_loss_usd: '10.00'
    min_hold_seconds: 60
    max_entry_cycles_per_day: 2
    max_consecutive_losses: 3
    loss_streak_reset: next_session
    post_sale_cooldown_seconds: 300
    buy: [vwap_bounce, opening_range_breakout]
    sell: [vwap_breakdown, momentum_reversal]
    stop:
      required: true
      policy: trailing_native     # Preferred: continuous broker-managed stop
      offset: {kind: usd, value: '2.00'}
      time_in_force: gtc
      # To switch brokers / use portable stepped stops:
      # policy: trailing_stepped
      # step: {kind: usd, value: '1.00'}
      # replace_min_interval_seconds: 5
  NVDA:
    enabled: true
    budget_usd: '400.00'
    max_planned_loss_usd: '8.00'
    min_hold_seconds: 0
    max_entry_cycles_per_day: 3
    max_consecutive_losses: 2
    loss_streak_reset: manual
    post_sale_cooldown_seconds: 60
    buy: [momentum_breakout]
    sell: [momentum_reversal]
    stop:
      required: true
      policy: trailing_native     # continuous Alpaca broker-held trail
      offset: {kind: percent, value: '1.5'}
      time_in_force: gtc

strategy_parameters:
  vwap_bounce: {trigger: candle_close, timeframe: 1m}
  opening_range_breakout: {trigger: price_update, opening_range_minutes: 15}

shadow_scenarios:
  - id: baseline
    enabled: true
    starting_cash_usd: '5000.00'
    symbols: [AAPL, NVDA]
  - id: experimental
    enabled: false
    starting_cash_usd: '5000.00'
    strategy_overrides: {}

reporting:
  persist_evaluations: true
  snapshot_interval_seconds: 30
  persist_trade_decision_evidence: true
  event_retention_days: null  # Preserve signals/trades/events permanently; no raw tick archive
```

Avoid runtime hot reload of credentials/authorization by file edits alone. A `live` mode setting is **not** sufficient permission to trade; require positive operator live-enable control with configured risk limits, broker fingerprint, successful reconciliation and explicit expiry of authorization if desired. Passwords/keys never committed or shown in reports.

## 18. Complete Makefile + CLI operator contract

All commands delegate to `python -m mike_trade.cli ...`; Make targets are a stable ergonomic facade. Provide `make help` with descriptions. Live-mutating commands must show target account/mode and demand `CONFIRM_LIVE=YES` or an interactive confirmation (noninteractive invocation still requires explicit confirmation). No unexpected long automatic tests or massive data downloads during routine status commands.

| Make target | Required behavior |
|---|---|
| `install`, `update`, `doctor` | reproducible environment installation, dependency update with lockfile, system/clock/SDK checks |
| `config-show` | show current redacted settings, active symbol/strategy selection and trade limits; config loading/editing performs schema validation automatically |
| `providers`, `provider-check`, `broker-check`, `data-check` | enumerate adapters/capabilities; authenticate read-only; report feed entitlements/freshness |
| `strategies`, `strategy-info`, `strategy-check` | registered plugins, parameter schemas, requirements, independent strategy validation |
| `symbols`, `symbol-add`, `symbol-remove`, `symbol-enable`, `symbol-disable` | inspect/change watchlist safely, persist edited config atomically with an audit log, validate open-position implications |
| `strategy-enable`, `strategy-disable`, `strategy-set` | select buy/sell rules for a symbol and set validated parameters without source edits |
| `start`, `stop`, `restart`, `status` | manage supervised engine process; stop does **not** liquidate or cancel broker stops |
| `dashboard-start`, `dashboard-stop`, `dashboard`, `health` | manage dashboard independently, display URL, health and heartbeat |
| `shadow-start`, `shadow-stop`, `shadow-list`, `shadow-create`, `shadow-reset` | lifecycle of named shadow scenarios; reset requires explicit confirmation and archives history |
| `paper-start`, `paper-stop`, `paper-status` | independent Alpaca paper trading mode and account fingerprint |
| `live-preflight`, `live-enable`, `live-start`, `live-disable` | guarded live flow: credentials, risk, account, reconciliation, operator approval |
| `pause`, `resume`, `pause-symbol`, `resume-symbol` | block new buys (and optionally discretionary orders by policy), do not disable stops |
| `entries-disable`, `entries-enable` | global discretionary entry kill switch, persistent across restarts |
| `close`, `close-all`, `flatten` | close managed positions; mode-aware confirmation, cancel/cohere stops, reconcile |
| `stop-status`, `stops-audit` | list protective stop coverage, detect missing/orphan/expired stops |
| `trail-status`, `trail-history`, `trail-policy-show` | inspect per-symbol trailing state, broker-confirmed stop and ratchet/change audit |
| `trail-policy-set`, `trail-reconcile` | persist/validate per-symbol native/stepped trailing parameters with guarded open-position changes; read/reconcile broker stop and update state safely |
| `entry-limits`, `entry-limits-reset`, `cooldown-status` | inspect day/session entry counts, per-stock/strategy losing streaks and lockout status, cooldown remaining; audited manual reset where configured (never bypass stops or account risk) |
| `positions`, `orders`, `balance`, `budget`, `reconcile` | authoritative broker/shadow state and internal drift reports |
| `signals`, `evaluations`, `events`, `logs` | bounded, filterable structured history / human logs |
| `report`, `report-daily`, `report-symbol`, `report-strategy`, `report-pairs` | comprehensive P&L, attribution, costs, timings; use filter args |
| `report-shadow`, `report-comparison`, `report-export` | compare named scenarios and export CSV/JSON reports |
| `backtest`, `backtest-status`, `backtest-report`, `backtest-list` | parameterized historical replay and results; no implicit broad history fetch |
| `cache-status`, `cache-refresh` | explicitly requested warmup/cache management and historical fetch |
| `db-check`, `backup`, `restore-dry-run` | SQLite integrity, snapshot backup, verified restoration rehearsal |
| `test-fast`, `test-unit`, `test-integration`, `test-broker-paper`, `test-end-to-end`, `lint`, `typecheck` | bounded test tiers; paper integration must never use live credentials |
| `service-install`, `service-uninstall`, `service-status` | systemd deployment and clean service management |
| `help` | all targets and mode/confirmation argument contract |

Examples: `make entry-limits SYMBOL=AAPL`, `make cooldown-status SYMBOL=AAPL`, `make entry-limits-reset SYMBOL=AAPL MODE=shadow CONFIRM=YES`, `make trail-status SYMBOL=AAPL MODE=shadow`, `make trail-policy-show SYMBOL=AAPL`, `make trail-policy-set SYMBOL=AAPL POLICY=trailing_stepped OFFSET_KIND=usd OFFSET=2 STEP_KIND=usd STEP=1`, `make trail-reconcile SYMBOL=AAPL MODE=paper`, `make status`, `make symbols`, `make strategy-enable SYMBOL=AAPL SIDE=buy STRATEGY=vwap_bounce`, `make positions MODE=shadow SCENARIO=baseline`, `make report SYMBOL=AAPL`, `make close SYMBOL=AAPL MODE=live CONFIRM_LIVE=YES`, `make live-preflight`. `make stop` may cease engine monitoring; show warning and outstanding stops/positions. `make pause` must keep engine online and protective reconciliation functioning.

Use **file-based config** for persistent policy, not ad hoc `make` edits scattered through text replacements. Makefile changes go through a config service that validates and saves YAML atomically, recording only a minimal effective checksum/audit event. Runtime controls like pause/kill switch persist in DB with command IDs. `make` is a frontend, not a scheduler or business-logic implementation.

## 19. Operational safety, market regulation and human responsibilities

- Respect market session rules including U.S. holidays, special halts and early close; do not infer from weekday alone.
- Check broker account status and applicable day-trading/PDT rules, settled/unsettled funds for cash accounts, buying power, margin permissions, and any broker-imposed day-trade protection flags. Regulations and broker practices may change; implement read/guard capabilities and link to current official rules rather than hardcode an obsolete static threshold.
- Support corporate actions and symbol changes; halt automatic decisions on unexplained quantity/cost-basis shifts.
- No automatic end-of-day sale by default. Always enforce latest-entry cutoff, and highlight overnight exposure/expired DAY stops if the operator leaves a position open.
- Separate `pause`, `stop`, `shutdown`, `disable-live`, `cancel-entry-orders`, `close`, and `flatten` semantics. None may imply another action unexpectedly.
- Credential storage: environment/keyring/protected systemd credentials, filesystem `0600`, sanitized logs; no default live write access in test commands.
- Dashboard read-only; LAN-only by default; remotely access through trusted VPN/SSH tunnel, not an exposed public port.
- Guard resource use: single engine instance per broker account, DB locks, shutdown drain for order updates, watchdog and health check; do not issue new entries during graceful stop.
- Operator responsibilities: fund account, understand risk and stop behavior, approve live, monitor/close positions as desired, authorize any provider terms/licenses.
- Failure model: broker rejects/restricts, data stale, scheduler lateness, partial fills, reboot, process kill, disconnect, credential expiry, stop expiration, overnight corporate action, time drift, disk full. Each has deterministic fail-closed entry semantics and dashboard/CLI explanation.

## 20. Startup, shutdown, reconciliation and recovery

**Startup sequence:**

1. Obtain instance lock; open/migrate/check DB, load last control state and redacted config hash.
2. Validate mode/credentials/clock/provider capabilities, market calendar and watchlist asset eligibility.
3. Retrieve broker account, open orders (including linked legs), current positions and recent activities; do not enable new entries yet.
4. Match broker facts to durable order intents/fills by client/broker IDs, apply missing fills exactly once, detect external/unmanaged positions and orphan stops, resolve pending cancel/fill races.
5. Verify budget reservations, stop coverage, cash/account flags and strategy persistent state; recompute snapshots and warmup caches.
6. Subscribe to order updates/data and establish freshness watermark; complete reconciliation with explicit outcome.
7. Only if state is healthy and operator live authorization remains valid, enter RUNNING; otherwise DEGRADED/RECONCILING and block entries.

**Graceful stop:** reject new entry intents; flush intent/events and snapshot; unsubscribe cleanly; **do not cancel broker-held protective stops automatically**, do not liquidate unless separate explicit command; write final heartbeat/status. Dashboard remains available and displays OFFLINE after heartbeat timeout. If account still has positions, dashboard prominently shows `OPEN LIVE POSITIONS — ENGINE OFFLINE` with last authoritative timestamp.

**Unclean crash:** dashboard detects stale heartbeat; broker-held DAY/GTC orders may remain as per broker rules; restart never blindly repeats an ambiguous intent. Reconcile broker before evaluating further. For unresolved protective order or position, block entries and raise actionable intervention instruction. Operator closing externally is detected next reconciliation; externally closed managed cycle is audited as external/manual exit, not as a sell-strategy win.

## 21. Reporting and analytical definitions

**Balances and P&L:**

- `cash` = spendable account cash per broker/shadow, not per symbol.
- `buying_power` = broker-specific account capacity; never equate margin buying power to authorized project budget.
- `equity` = cash plus marked positions, according to account source.
- `per-symbol committed` = pending reserved buys plus open cost/value under documented budget policy.
- `unrealized P&L` = marked position value minus remaining cost basis; mark type and timestamp stated.
- `realized P&L` = fill sale proceeds minus purchase basis and actual attributable trading costs; separate deposits/transfers.
- `net P&L` = gross price-based profit less known actual fees and estimated costs **with explicitly labeled methodology**.
- `performance` = include capital/time weighted exposure, no mixing shadow estimated fees with broker authoritative charges.

**Fees:** retrieve broker activities / fee transactions automatically; classify `confirmed`, `estimated`, `unavailable`; do not force operator to hand-load fee files. Alpaca trade activity objects include fill prices and quantities, but a precise per-order all-in cost may not be directly provided on every fill; report attribution limitations and account-level fees separately. [A9]

**Analyses:** strategy contributions and pairing, how many NO_SIGNAL vs SIGNAL evaluations, latency from detection → order → fill, rejected signal reasons, missed signals from cutoff/limits, best/worst symbols, holding-time buckets, excursion statistics, commission/statutory fee impact, trade-by-trade provenance, live vs shadow comparison (same market time), IEX-vs-SIP feed limitations when applicable. Export CSV/JSON with stable schemas. Reports must reconcile starting cash + external flows + realized/unrealized P&L = ending equity (within precise rounding/valuation convention).

## 22. Logging, observability and dashboard fault visibility

- Use structured JSON logs with trace ID (`run_id`, `cycle_id`, `signal_id`, `intent_id`, `client_order_id`) plus human-readable CLI summaries.
- Logging levels and bounded retention; persist all trade-affecting commands, order state, fills, cooldown/entry-limit/loss-streak decisions and policy decisions permanently, with compact evaluation records and trade-decision evidence. No bulk tick archive.
- Counters: market feed age, tick/candle rates, scheduler lag, evaluation duration, signal counts, rejection reasons, broker REST/WS rate/errors, unknown order states, unmatched position drifts, shadow fills, realized/unrealized account P&L, heartbeat.
- Surface failed broker stop placement, orphan stops, unconfirmed stepped replacement, stale trailing watermark, uncovered partial fills, open live position while engine offline, unknown order state, unexpected short position, risk-limit breach, drift, stale feed and unhandled exceptions in dashboard and logs. Do not implement email/SMS/phone/push notifications or external notification accounts.
- Do not produce secrets, personal tokens, full account identifiers or raw unfiltered provider payloads in diagnostic logs.

## 23. Testing, quality and acceptance criteria

**Targeted verification during development** (avoid pointless repeated broad full-suite runs): unit tests for each new rule and pure policy; contract tests for adapters; state transition, persistence and concurrency tests; endpoint simulation/in-memory fake for service layers; paper-broker tests only when authorized, bounded and separately invoked; one deliberate final full verification/acceptance stage.

### 23.1 Non-negotiable acceptance scenarios

1. Configure 1, then 10 symbols; exactly one live market-data subscription per symbol/channel (subject to provider connection architecture), multiple strategy consumers, no strategy-owned data polling.
2. Each symbol configurable independently with one or many buy/sell rules, budgets, min hold, fixed/native/stepped trailing protection (distance and increment), cutoff, and strategy parameters.
3. Trigger one eligible BUY; reserve budget; submit only one order; ignore recurrent buy evaluations while order/position exists; track partial fill without second entry.
4. Activate SELL monitoring once a real quantity is held; a qualifying sell after minimum hold requests the entire remaining holding. Protective/manual/risk exits bypass hold restriction.
4a. `max_entry_cycles_per_day` counts only position cycles with a confirmed positive buy fill, prevents an additional entry in that symbol/account/session and resets at the next New York exchange session; zero-fill canceled entries do not count, partial fills count once, and reboot cannot reset counts.
4b. Consecutive negative-net realized cycles block **new entries** according to per-symbol and optional entry-strategy rules. Exactly breakeven does not reset; an actual positive net cycle resets. Next-session or audited manual-reset modes work, and fees unavailable/estimated status is honest. Shadow account counters never leak into live.
4c. Post-sale cooldown begins after the last confirmed exit fill, survives restart, and blocks even a newly armed buy signal until expiry. Stops, manual closes, sell signals, fills and reconciliation continue regardless of cooldown or loss-streak lockout.
5. Concurrent AAPL+NVDA triggers cannot oversubscribe global budget, even under thread races and restart.
6. Broker protective stop exists/confirmed when protection is mandated, or state and risk gate explicitly expose failure. A strategy sell cannot accidentally cause an overlapping protective stop to create a short position.
6a. On partial/full entry fills, automatically install and reconcile stop coverage; stepwise stop at $100 entry/$2 distance/$1 step follows $98 → $99 at $101 high → $101 at $103 high, never decreases or moves for sub-step highs; multi-step jumps catch up.
6b. Native Alpaca trailing follows the broker `hwm` with fixed dollars/percent; stepped replacements require confirmed provider status; failed/unknown replacement, gaps, fast stop trigger and concurrent discretionary close are all covered by fault-injection tests.
6c. Trailing stop management bypasses hold period, runs independently of discretionary strategy cadence, preserves confirmed broker protection when the engine is offline (but correctly flags stepped ratchet suspended), and rejects unsupported fractional configurations.
7. Verify live stop cancellation/position reconciliation on discretionary exit, protective-trigger exit, partial exit and external manual close.
8. Fractional share orders and supported protective-stop order combinations, including trailing-native and stepped modes where approved, demonstrated **against Alpaca paper** before allowing corresponding live configuration; unsupported combinations visibly rejected.
9. Shadow ledger tracks cash/equity/fees/P&L across restarts; experimental shadow scenario shares live feed but never submits orders.
10. Alpaca paper and live credentials, endpoints and ledgers cannot cross; read-only tests cannot submit live orders.
11. Dashboard remains reachable when engine crashes, marks OFFLINE within heartbeat threshold, retains clearly stale last-known position/balance information, and displays per-stock entry cap, loss streak/lockout and cooldown status.
12. Engine restarted with outstanding stop/order and an external manual trade: detect/reconcile before new entries, no duplicate submission, accurate provenance.
13. Configurable 15:00 ET entry cutoff works and is adjusted for exchange early closes; default end-of-day liquidation is off; optional liquidation path includes close confirmation and failure reporting.
14. App exits/stops without canceling remaining protective broker stops or unexpectedly liquidating; commands distinguish stop/pause/close/flatten.
15. At least one end-to-end shadow trade cycle; one authorized Alpaca paper buy/stop/sell cycle; detailed reports and reconciliation pass from originating signals to final cash.
16. Ledger invariants and reports reconcile exact fees/cash/positions/flows for whole and fractional fills; no negative shadow cash without explicitly enabled leverage.
17. Historical replay is deterministic with frozen data/clock/seed; no lookahead; strategy code unmodified between replay and live evaluation.
18. `make help`, `make entry-limits`, `make entry-limits-reset`, `make cooldown-status`, `make trail-status`, `make trail-history`, `make trail-policy-set`, `make trail-reconcile`, `make status`, `make signals`, `make balance`, `make report`, `make dashboard`, `make config-show`, `make reconcile`, live preflight, pause/resume, close/close-all and provider checks work and are documented.
18a. The `vwap_reference` preset and `config/reference_vwap_shadow.yaml` select two independent real Python implementations; a bounded offline completed-candle fixture confirms BUY → one entry → broker-independent simulated protection → SELL/stop path → full close → exact shadow ledger → watch state, plus repeated ticks/no-signal, partial candles, stop cancellation and a separate deterministic replay. This works without Alpaca credentials or any live orders.
19. Actual source/reference for every shipped signal strategy is documented; no claims of positive expectancy without research results and out-of-sample evidence.
20. Git clean, reproducible dependency lock, migrations, user installation guide, R730 systemd instructions, secrets template, architecture decision records and operator runbook delivered.

### 23.2 Explicit completion gates

A component is **DONE** only with its functional implementation, positive + negative tests, documented user controls, persisted/auditable behavior and dashboard/report integration where appropriate. Mock broker implementations support tests but **do not count** as Alpaca integration. A cosmetically complete dashboard with static examples does not meet acceptance. A strategy returning hardcoded BUY is a test fixture, not a production strategy. Core unsupported Alpaca capabilities must be gated and listed accurately; never marked working without account-level paper verification.

## 24. Implementation roadmap for Abracapocus

This is a **single complete-product program with dependency-ordered work packages**, not an MVP ladder. Materialize the plan first; review every work package for oversized tasks, missing verification steps, cross-boundary dependencies and risk before coding. Each package has measurable output and contract tests; a final integrated acceptance run proves the full application.

| Work package | Complete deliverable | Dependency / verification |
|---|---|---|
| WP01 | Repository, lint/typing/lockfile, configuration schemas, CLI/Makefile help, docs/ADRs | Config validation + imports/CLI tests |
| WP02 | Domain entities, interfaces, events, budgets and policy contracts | Value-object tests, Decimal arithmetic invariants |
| WP03 | SQLite schema/migrations, event journal, repositories, backup/check | Atomicity, idempotency and migration tests |
| WP04 | Alpaca Trading API paper/live adapters, trade updates, assets, account/orders/activities, fixed/native trailing orders, safe stop replacement, capability matrix | Contract tests; account-dependent paper tests separately |
| WP05 | Alpaca IEX WebSocket + historical feed, normalization/candles/indicator context, optional research adapters | One-feed fanout, gap/staleness, warmup/replay tests |
| WP06 | Scheduler, bounded worker pool, event router, serial portfolio coordinator | Trigger semantics, ordering, concurrency/budget races |
| WP07 | Strategy registry, buy/sell plugin SDK, production VWAP buy/sell reference pair and documented shadow-only preset, published presets and documented rules | State/rearm/candle-close tests + methodology docs |
| WP08 | Position cycle, order manager, fixed and stepwise/native trailing stop orchestration, conflict and manual controls | Monotone ratchet, replacement confirmation, partial-fill, stop/close race and restart fault injection |
| WP09 | Budget, per-stock/session trade caps, consecutive-loss entry lockouts, post-sale cooldown, market calendar/cutoff and optional EOD close | Limits/reset/restart isolation, consecutive-loss net accounting, overnight session boundary, insufficient cash and stop-required tests |
| WP10 | Shadow broker, persistent accounts, multiple simultaneous scenarios, trailing-stop simulation | Independent ledgers, ratchet parity, price realism, restart tests |
| WP11 | Historic event replay/backtester, walk-forward reporting and manifests | No-lookahead, deterministic reproduction, realistic cost checks |
| WP12 | Dashboard read API/UI, independent heartbeat/offline, per-stock trade caps/loss streak/cooldown, modes/positions and stop status | Engine kill/restart screenshot + entry eligibility/stop status tests; no brokerage charting UI |
| WP13 | Reports, fee import, strategy attribution, exports and reconciliation audits | Cash/order/fill/P&L ledger cross-check |
| WP14 | systemd deployment, secrets, backup/restore, operational Make targets, operator documentation | Fresh machine install and service recovery drills |
| WP15 | Integrated reference VWAP shadow/backtest cycle, paper end-to-end, live authorization dry run (no live order), final risk and performance QA | Acceptance scenarios 1–20, source review and operator sign-off |

**Agent execution boundaries:** The coding agent writes, inspects and tests only within explicitly authorized scope; operator supplies Alpaca keys later. No auto-placed live trades. No expensive historical fetch/backtest or broad full-suite loops unless planned and approved. Use small targeted tests during changes, one selected final full suite, and explicit human acceptance gates for live API activation. Do not silently change held-out research periods based on runs. Every work package must record what changed, targeted tests run, pending account-dependent verification and any blockers.

**Definition of engineering success:** Most of the software can be implemented in one coordinated Abracapocus program because boundaries and acceptance gates are defined in advance. A live-ready deployment still depends on verified Alpaca account credentials, authorized paper checks, asset permissions and the operator's explicit live-enablement decision.

## 25. Delivery artifacts and repository structure

```text
mike_trade/
  pyproject.toml              # Reproducible build + locked dependencies
  uv.lock                     # Or equivalent single dependency lockfile
  Makefile
  .env.example                # Names only, never credentials
  config/
    mike_trade.example.yaml
    reference_vwap_shadow.yaml  # Fully executable shadow VWAP BUY/SELL preset
    providers.example.yaml
  docs/
    ARCHITECTURE.md
    REQUIREMENTS.md
    OPERATIONS.md
    ALPACA.md
    SAFETY_AND_RISK.md
    STRATEGIES.md
    DATABASE.md
    DASHBOARD.md
    TEST_PLAN.md
    ADR/                     # Why major design choices were made
  mike_trade/
    domain/                  # Value objects, enums, events, invariants
    strategies/              # real buy/, sell/ VWAP reference pair, indicators/, presets/, registry
    market_data/             # data port, streams, history, candles, indicators
    research/                # typed optional provider ports and integration
    scheduling/              # clocks, calendar, event triggers, pool
    signals/                 # evaluation, validation, conflict/priority
    portfolio/               # cycles, budgets, position manager, reservations
    risk/                    # protection, session, daily loss, broker flags
      trailing.py             # monotone native/stepped trailing state and coordinator
    execution/               # order manager, state machines, intents
    brokers/                 # ports/, alpaca/, shadow/
    persistence/             # SQLite repositories, migrations, snapshots
    backtest/                # replay provider, simulator, workbench
    reporting/               # metrics, reconciliations, exports
    controls/                # CLI commands, authorizations, durable controls
    dashboard/               # read API, templates, assets, heartbeat
    observability/           # structured logs, metrics, health and dashboard fault flags
    app.py                   # Dependency-injected composition root
    cli.py
  tests/
    unit/
    contracts/
    integration/
    paper_broker/            # Opt-in credentials only
    end_to_end/
    fixtures/
  deploy/
    systemd/
    nginx_optional/
  scripts/
  state/                     # gitignored; DBs, logs, caches, backups
```

No strategy imports from `brokers/alpaca/`; no provider owns decision-making. `Makefile` command contract remains provider-neutral where possible. `README.md` contains setup, make-help pointers and dashboard URL; docs above contain operational details, not just architectural aspiration.

## 26. Decisions, defaults, and genuinely unresolved broker-dependent work

**Locked by operator:** Product/repository/package name `mike_trade`; runnable shadow-only VWAP reference pair; 1–10 stocks; continuous broker-held trailing preferred, broker-independent stepped stop engine supported, both attached after confirmed buy; code/classes for rules; independently chosen buy and sell; one buy/one full sell; no deliberate position scaling; per-symbol and total budget; per-symbol maximum filled entries/day, consecutive losing-trade buy lockout (symbol or entry strategy), and post-sale cooldown; min hold configurable; regular hours only; configurable entry cutoff; manual EOD closing preferred; optional auto EOD; shadow alongside live; fractionals; Alpaca initially; replaceable broker/data/research; Makefile control; read-only dashboard that can show OFFLINE; comprehensive auditable reporting; real complete product.

**Chosen architectural defaults (editable by configuration, not disguised as operator decisions):** WebSocket feed preferred, polling fallback; SQLite WAL; separate dashboard process; one coordinating ledger writer; shadow cost model conservative; native continuous trailing stop preferred where broker supports it; optional loss-streak/trade caps/cooldown disabled unless configured; no live trades until explicit allow; live session starts gated; broker stops required by default; no unverified cross-account position management; entry cutoff sample 15:00 ET; no auto-close unless requested.

**Must validate when Alpaca account is available:** standalone native trailing-stop placement/updates and broker `hwm`; stepped stop `stop_price` replacement behavior and race/fill transitions; fractional protective and trailing stop workflows and linked-order compatibility; exact order-type/TIF support by asset/account; stop-only OTO + discretionary exit interaction; partial-fill stop coverage; actual account flags/day-trading eligibility; expense/fee record coverage; stream entitlements/connectivity and IEX bar history for strategy inputs. These are *capability checks* with safe fallback/rejection, not excuses to ship a nonfunctional integration.

**Explicitly declined extras for this release:** daily `arm` ritual, earnings/economic blackout calendar, end-of-day warning notifications, phone/email/push notifications, configuration history/diff/preview UI, dedicated signal-explanation UI beyond good logs, automated stock screener, separate profit-lock schedule beyond trailing stops, standalone strategy-comparison dashboard, raw tick recording/replay and a stock charting terminal. Internal config validation and trade-audit evidence remain required. Additional reference-market indicators are permitted only as declared inputs to specific strategies. **Potential later decisions** include short-selling permission and licensed consolidated-market feeds; neither blocks the current specified product.

## 27. Reference documentation (checked October 7, 2026)

Only official documentation is used here for broker-specific capability claims. The implementation must re-check details when it connects to the actual account and when Alpaca changes APIs. This specification does **not** assert that an order class absent from the docs is secretly supported.

- **[A1]** Alpaca Market Data API & Basic IEX limits: https://docs.alpaca.markets/us/docs/about-market-data-api
- **[A2]** Alpaca Market Data FAQ / IEX and SIP feeds: https://docs.alpaca.markets/us/docs/market-data-faq
- **[A3]** Alpaca Fractional Trading: https://docs.alpaca.markets/us/docs/fractional-trading
- **[A4]** Alpaca Placing Orders (TIF table, linked order classes, notional restrictions): https://docs.alpaca.markets/us/docs/orders-at-alpaca
- **[A5]** Alpaca Order Classes (Bracket / OCO / OTO), within [A4]: https://docs.alpaca.markets/us/docs/orders-at-alpaca
- **[A6]** Alpaca Trading API overview and paper trading: https://docs.alpaca.markets/us/docs/trading-api
- **[A7]** Alpaca Trading API Getting Started/auth: https://docs.alpaca.markets/us/docs/getting-started-with-trading-api
- **[A8]** Alpaca WebSocket `trade_updates`: https://docs.alpaca.markets/us/docs/websocket-streaming
- **[A9]** Alpaca Account Activities and fee/activity types: https://docs.alpaca.markets/us/docs/account-activities
- **[A10]** Alpaca Create Order endpoint / `client_order_id`: https://docs.alpaca.markets/us/reference/postorder
- **[A11]** Alpaca Assets endpoint / equity eligibility: https://docs.alpaca.markets/us/reference/get-v2-assets-1
- **[A12]** Alpaca real-time stock stream and `v2/iex`: https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data
- **[A13]** Alpaca standalone trailing-stop behavior, `hwm`, `stop_price`, `trail_percent`/`trail_price`, session restriction: https://docs.alpaca.markets/us/docs/orders-at-alpaca
- **[A14]** Alpaca Trading API replace order PATCH, asynchronous replacement caveats: https://docs.alpaca.markets/us/reference/patchorderbyorderid-1

---

### Final engineering directive

Build **one real, configurable, operator-supervised, thoroughly documented stock-trading product** whose strategy code can be freely extended and whose broker/data connections can be replaced without rewriting trading decisions. The software must prove what it can and cannot do. An unverified provider feature is gated, never faked. Complete means executable functionality, audit history, dashboard, Makefile controls, restart/reconciliation, shadow, backtest, and approved broker integration—not simply a folder structure and sample rules.
