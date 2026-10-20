# `vwap_bounce_break_michelle_corrected_buy` / `_sell`

## Identity and status

- Authority: `docs/strategies/michelle/vwap_bounce_break_business_rules_final.docx` (55 numbered
  business rules). It supersedes the original `vwap_bounce_break_michelle_*` behavior wherever
  trading rules differ. The originals are untouched and remain independent registry entries.
- Registry ids: `vwap_bounce_break_michelle_corrected_buy` (BUY, 1.0.0) and
  `vwap_bounce_break_michelle_corrected_sell` (SELL, 1.0.0).
- Modules: `mike_trade/strategies/buy/vwap_bounce_break_michelle_corrected.py`,
  `mike_trade/strategies/sell/vwap_bounce_break_michelle_corrected.py`; pure rules in
  `mike_trade/strategies/michelle_corrected.py` and `mike_trade/risk/michelle_corrected_stop.py`;
  account control in `mike_trade/risk/daily_loss.py`.
- Status: implemented and verified by focused tests (`tests/unit/test_michelle_corrected_*.py`).
  No historical campaign, PAPER or LIVE session has been run.
- The corrected BUY **requires** the corrected SELL on the same symbol (refused at build time).

## Required data

- One-minute candles with **premarket included** (they seed EMA9/EMA20 only; VWAP, setup counts and
  patterns use regular-session candles). Without premarket candles the EMAs are undefined and no
  setup can start (fails closed).
- VWAP is the 09:30-anchored cumulative typical-price VWAP, so the candle history must reach back to
  09:30 (production: raise `market_data.warmup_bars` to at least 500 or a mid-day restart computes a
  partial VWAP; the context keeps 500 candles).
- **QQQ** one-minute candles and a contemporaneous QQQ price (`MarketContext.reference_candles` /
  `reference_prices`, declared through `reference_symbols=("QQQ",)`). Production attaches the shared
  QQQ store and the latest QQQ trade (<= 30 s old, else the last completed close); the backtest feeds
  QQQ events automatically. Missing QQQ data fails the QQQ filter closed.

## Setup, patterns and entry (rules 1-45, 53-55)

State is an explicit machine (`michelle_corrected.scan`): the only persisted state is a purge/
consumption *barrier* (`consumed_after`, `purge_barrier`); the setup is a deterministic replay of
completed candles after it, so nothing purged or consumed can be reused (rules 40, 53, 54).

- Setup start: close < VWAP, EMA9, EMA20 with EMA9 and EMA20 < VWAP; no fresh crossover needed.
  The 30-minute clock runs from the first qualifying candle's start and is never reset.
- Dip: strictly lower lows re-anchor the VWAP of that candle; equal lows do not. `VWAP - low > 3.00`
  purges (exactly 3.00 is allowed), continuously, including from the developing live candle.
- Bottoming (strict OR; priority Hammer > Support Shelf Base > Bullish Engulfing): hammer (body
  strictly below VWAP, lower wick >= 2x body, body in the upper half, no upper-wick cap); 3-6 candle
  shelf (closes below VWAP, lows within $0.50 inclusive); engulfing (red then green below VWAP,
  inclusive body engulf). A lower low disqualifies Pattern 1; a strictly lower Pattern 2 must form;
  after it, wicks are fine, a close below Pattern 2's low purges, no third pattern.
- Confirmation: >= 3 consecutive closes below VWAP, then the first green candle with `low <= VWAP` and
  `close > VWAP` within 30 minutes (candle start to candle start, inclusive). Its close is the immutable
  `Confirmation_Close`; it can never be the entry candle.
- Entry window C+1..C+5. Entry = market buy when price >= `Confirmation_Close`, strictly above VWAP and
  <= VWAP + $0.50, 09:34:00 <= time < 11:30:00, and EMA momentum (rule 41), QQQ (42/43) and the
  VWAP-crossing filter (44, active after 20 candles) pass. A completed window candle closing below VWAP
  purges; C+5 without a fill purges; no C+6. Flat or falling VWAP never blocks (45).
- One setup = one trade: the engine marks the setup consumed when it submits the order; the exit is the
  purge instant for everything before it.

## Intrabar execution semantics

- **SHADOW / PAPER / LIVE**: the BUY is evaluated on every trade tick (`EDGE_ON_TRADE`) against the
  developing candle: the first print meeting the conditions triggers an immediate market buy (Rule 35),
  with no completed-green requirement. When a fresh ask is known it must also respect the VWAP ceiling.
- **Historical OHLC replay** cannot see intrabar ordering, so it is conservative and explicit:
  the C+1 open leg is exact; the deferred trigger fills **at `Confirmation_Close`, stamped at the candle
  end**, only if the candle opens below it, reaches it, and the VWAP band holds at *both* the prior-bar
  VWAP and the candle's final VWAP and this candle's own low does not breach the $3 dip. A candle that
  opens at/above the floor but outside the band, or fails a filter at the open, is never guessed into a
  fill. The fills are booked by `HistoricalExecutionSimulator.fill_entry_in_bar` and carry
  `MODELED_INTRABAR_ENTRY` plus the usual `BAR_ONLY` limitation. With quote evidence available nothing
  is modeled and the order fills from quotes after the candle (late).

## Protection (rules 46-50) - two stages

- Stage 1: the moment the fill is confirmed, a fixed stop at `Fill - $0.75` (floored to the tick: the
  trigger set `price <= Fill - 0.75` is identical on the tick grid). It does not follow VWAP and is not
  the original 0.2% trail.
- Stage 2: when the market trades at `Fill + $0.50` the stop moves **once, upward** to breakeven
  (`Fill`, floored to the tick) and the EMA9 exit arms. Then 100% of the position exits at once when
  `price <= Fill` or `price < EMA9` of the last *completed* candle, intrabar.
- New `ProtectionKind.FIXED_THEN_BREAKEVEN_STOP`; migration `0004_breakeven_protection_kind.sql`
  admits it in `trailing_stop_states` and `protection_orders`.
- Responsibilities. Broker/simulator-held: Stage 1 stop and the breakeven replacement. Application-
  managed: the EMA9 breach and the breakeven breach in the corrected SELL (every trade tick, plus the
  broker stop at breakeven). The corrected SELL signal is classified `ExitSource.PROTECTIVE_STOP`
  (priority 0, ignores minimum hold) in SHADOW, PAPER/LIVE and the backtest; protective candidates
  carry no strategy identity (existing contract). **PAPER/LIVE do not provide continuous intrabar EMA9
  protection**: during a process or feed outage only the broker-held stop protects.
- Backtest: one resting stop emulates Stages 1-2 for each next candle: `max(breakeven, EMA9 level)`
  (the EMA part may ease back toward breakeven by cancel-and-rearm; breakeven never drops). A candle
  that touches both the initial stop and `Fill + $0.50` is stop-first (rule 55); Stage 2 applies from
  the candle after the one that reached it. The entry candle's own stop test is stop-first.

## Account-level controls (rules 51-52)

- `risk.daily_loss.DailyLossGuard`: `realized trading-day P/L + unrealized P/L >= $150 loss` latches
  for the New York trading day: market exits for every open position (retried until flat) and no new
  entries from **any** strategy on that account. The latch resets next trading day. It is active in
  every scenario/run that has a corrected strategy; a stricter configured limit wins
  (`effective_limit`). Accounting sources: SHADOW `ShadowAccountState.realized_by_day` (ledger replay) +
  `unrealized_pnl()`; PAPER/LIVE closed-cycle P/L from the ledger + protected-position marks from trades,
  durably blocked via entry cooldowns until the next session; backtest equity delta, with the current
  candle's low as the adverse mark (liquidation then fills at the next open).
- Forced liquidation 15:55 NY, or 5 minutes before an early close, is mandatory for the corrected
  strategies: the backtest forces it on; SHADOW/PAPER/LIVE rely on `end_of_day_close.enabled: true`
  (`at: "15:55"`; early closes derive from the session close).

## JEV

No JEV evidence interface exists in this checkout (nothing to integrate). The BUY persists the facts
that exist at signal time as `entry_evidence` (setup start, setup low and associated VWAP, pattern
kind/stage/low, confirmation close, window number, EMA momentum, QQQ values, crossing count, entry
price, VWAP and distance) so a future adapter can map them. The protective exit source is
`PROTECTIVE_STOP`, which any review gate must treat as non-vetoable.

## Interpretations and open ambiguities

See the delivery notes: (A1) a gap above the ceiling or a failed filter at the C+1 open keeps the
trigger armed for the window (level-based reading of rules 34-35); (A2) the purging candle never seeds
the next setup; (A3) a close exactly on VWAP is neutral in the crossing count; (A4) the three-closes
pullback is not reset by a Pattern 1 disqualification; (A5) the hammer/shelf/engulfing low is the
lowest low of the pattern's candles; (A6) Stage 2 vs breakeven ordering inside one OHLC candle is
unresolvable and Stage 2 starts the next candle.

## Differences from the original Michelle VWAP strategy

| Topic | Original | Corrected |
|---|---|---|
| Entry | second green candle, filled at the next open | C+1..C+5 intrabar market buy, no extra green |
| Setup | EMA9<EMA20<VWAP, $2 dip, 0.15%-range base | EMA9/EMA20 either order, $3 dip, $0.50 shelf of 3-6 |
| Confirmation | EMA9 slope/spread test | 3 closes below VWAP, no slope test |
| Filters | optional SPY (ORB only) | EMA momentum, QQQ, crossing count |
| Stop | fill - $0.25 then 0.2% trail | fill - $0.75, breakeven at +$0.50, EMA9 exit |
| Attempt limiter | 2 per 10 min | none (one setup = one trade) |
| Daily loss | none | $150 account-level latch |

## Configuration examples

```yaml
strategies:
  michelle_corrected_buy:  {plugin: vwap_bounce_break_michelle_corrected_buy,  side: buy,  parameters: {}}
  michelle_corrected_sell: {plugin: vwap_bounce_break_michelle_corrected_sell, side: sell, parameters: {}}
symbols:
  AAPL:
    enabled: true
    budget: "2500.00"
    buy_strategies: [michelle_corrected_buy]
    sell_strategies: [michelle_corrected_sell]
execution:
  end_of_day_close: {enabled: true, at: "15:55"}
market_data:
  warmup_bars: 500      # premarket + 09:30 VWAP history
```

Backtest: `strategy_ids=("vwap_bounce_break_michelle_corrected_buy", "vwap_bounce_break_michelle_corrected_sell")`;
QQQ events are added to the feed automatically.
