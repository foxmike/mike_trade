# `vwap_bounce_break_michelle_corrected_buy` / `_sell`

## Identity and status

- Authority: `docs/strategies/michelle/vwap_bounce_break_business_rules_final_2.docx` (**V2**, 51 numbered
  business rules). It supersedes `vwap_bounce_break_business_rules_final.docx` (V1, 55 rules) wherever the
  rules differ, and supersedes the original `vwap_bounce_break_michelle_*` behavior. The originals and the
  V1 document are kept for history only.
- Registry ids (unchanged): `vwap_bounce_break_michelle_corrected_buy` (BUY, **2.0.0**, was 1.0.0) and
  `vwap_bounce_break_michelle_corrected_sell` (SELL, 1.0.0 — its rules are unchanged; only the rule
  numbers moved). Results of V1 runs are not comparable with V2 runs; the BUY version records which
  rule set produced a run.
- Modules: `mike_trade/strategies/buy/vwap_bounce_break_michelle_corrected_buy.py`,
  `mike_trade/strategies/sell/vwap_bounce_break_michelle_corrected_sell.py`; pure rules in
  `mike_trade/strategies/michelle_corrected.py` and `mike_trade/risk/michelle_corrected_stop.py`;
  account control in `mike_trade/risk/daily_loss.py`.
- Status: implemented and verified by focused tests (`tests/unit/test_michelle_corrected_*.py`).
  No historical V2 campaign, PAPER or LIVE session has been run.
- The corrected BUY **requires** the corrected SELL on the same symbol (refused at build time).

## What changed from V1 (55 rules) to V2 (51 rules)

| Area | V1 | V2 | Code impact |
|---|---|---|---|
| QQQ in VWAP anchoring (R3) | "all traded symbols and market reference symbols (QQQ)" | traded symbol only | none (QQQ VWAP is no longer used) |
| Earliest-entry rule (V1 R4) | explicit "Candles 1-4 / 9:34" derivation | folded into the 9:34:00 entry time (R4) + R12 | none |
| Setup-start candle (R8) | only a start time | the start candle is part of the pullback and may form/contribute to a pattern | tests; behavior already held |
| Confirmation timing (R11, R29) | confirmation had to *complete* within 30 min | candle must *start* within 30 min of the setup start (inclusive) | no change (start-to-start was already used) |
| Support Shelf Base (R20-22) | 3-6 candles, **lows** within $0.50 | **at least 2** candles, **closes** within $0.25; extends while closes stay in range; locks when a close goes higher; pattern low = **lowest close** | `extend_shelf`, `shelf_pattern`, `_refresh_shelf` |
| Pattern 1 invalidation (R15) | any **lower low (wick)** disqualified Pattern 1; a third pattern was prohibited | only a **completed close** strictly below Pattern 1's pattern low discards it; wicks and lower-wick patterns are tolerated; same rule for Pattern 2 (a close below it cancels) | `scan` |
| Entry price band (V1 R34-37) | `confirmation close <= price <= contemporaneous VWAP + $0.50`, strictly above VWAP; VWAP moved with the candle | **Entry ceiling = Candle C's VWAP + $0.50, frozen**; benchmark VWAP = most recently **completed** candle | `scan` (`entry_ceiling`), `price_qualifies`, `entry_band` |
| Two scenarios (R33-34) | one rule | Scenario 1 (confirmation close <= ceiling) and **Scenario 2** (close > ceiling: wait for a pullback to the ceiling while above VWAP, EMA9 and EMA20 of the same completed candle; entry below the confirmation close allowed) | `EntryScenario`, `price_qualifies` |
| Entry trigger (V1 R34-36 open leg / resting trigger) | "open leg" + resting trigger at the confirmation close | first qualifying moment of C+1..C+5 (R33-35) | `bar_entry`, `live_entry` |
| EMA momentum (R39) | same test | same test (boundaries restated) | none (test coverage extended) |
| QQQ filter (V1 R42-43) | 3 rising closes before 09:45; price > QQQ VWAP afterwards | **QQQ must pass the same EMA momentum test as the stock**; no VWAP condition | `qqq_momentum` replaces `qqq_check` |
| VWAP-crossing filter (V1 R44) | blocked at >= 7 crossings in 20 candles | **removed** | `vwap_crossings` deleted |
| Slope (R41) | same | same | none |
| Stops / daily loss / EOD / purge / stop-first (V1 R46-55) | same rules | same rules, renumbered 42-51 | docstrings only |

Unchanged in substance: premarket EMA seeding, 9:34/11:30 entry hours, setup alignment (R6-7), setup
low/associated VWAP (R9), $3.00 dip (R10), three closes below VWAP (R12), hammer and engulfing geometry,
pattern priority (R14), Candle C definition (R26-28), C+1..C+5 window (R32, R37-38), one setup = one
trade, $0.75/$0.50 stops, EMA9 exit, $150 daily loss, 15:55 / early-close liquidation, purge/reset.

## Required data

- One-minute candles with **premarket included** for the traded symbol **and for QQQ** (they seed
  EMA9/EMA20 only; VWAP, setup counts and patterns use regular-session candles). Without premarket
  candles the EMAs are undefined: no setup can start, and the QQQ filter fails closed.
- VWAP is the 09:30-anchored cumulative typical-price VWAP, so the candle history must reach back to
  09:30 (production: raise `market_data.warmup_bars` to at least 500 or a mid-day restart computes a
  partial VWAP; the context keeps 500 candles).
- **QQQ** one-minute candles (`MarketContext.reference_candles`, declared through
  `reference_symbols=("QQQ",)`). The latest completed QQQ candle must be the one that ended exactly at
  the start of the candle in which the entry would happen (a QQQ series that is behind fails closed).
  The contemporaneous QQQ *price* is no longer used. The backtest feeds QQQ events automatically.

## Setup, patterns and entry (rules 1-41, 49-50)

State is an explicit machine (`michelle_corrected.scan`): the only persisted state is a purge/
consumption *barrier* (`consumed_after`, `purge_barrier`); the setup is a deterministic replay of
completed candles after it, so nothing purged or consumed can be reused (rules 38, 49, 50), and the
barrier is ignored on a new New York session.

- Setup start (R6-8): close < VWAP, EMA9 and EMA20 with EMA9 and EMA20 < VWAP; no fresh crossover. The
  start candle belongs to the pullback and to any pattern; later bearish candles never reset the
  30-minute clock.
- Dip (R9-10): strictly lower lows re-anchor the VWAP of that candle; equal lows do not. `VWAP - low >
  3.00` purges (exactly 3.00 is allowed) until the entry is filled, including from the developing live
  candle.
- Pullback (R12): three consecutive completed closes below their own VWAP complete before Candle C.
- Bottoming (R13-25; OR; priority Hammer > Support Shelf Base > Bullish Engulfing):
  hammer (body strictly below VWAP, high > low, lower wick >= 2x body, open and close in the upper half,
  any color, no upper-wick cap); shelf (>= 2 adjacent candles, every close below its own VWAP, highest
  close - lowest close <= $0.25); engulfing (red then green, both closes below their VWAPs, green
  open <= red close and green close >= red open). Pattern low = lowest price in the pattern, except
  for a shelf: lowest close. Only candles from the setup-start candle on may take part.
- Pattern 1 / Pattern 2 (R15): the first pattern is Pattern 1. A completed candle closing strictly below
  Pattern 1's low discards it; a strictly lower Pattern 2 must then form before confirmation. A lower
  *wick* never discards a pattern, and a lower-wick pattern whose candle closes at or above the low does
  not count as another pattern. A completed close strictly below Pattern 2's low cancels the setup (this
  also makes the "third lower pattern" clause unreachable).
- Confirmation (R26-31): the first green candle with `low <= VWAP` and `close > VWAP` that *starts* within
  30 minutes of the setup start. Its close is the immutable `Confirmation_Close`; **Candle C's VWAP +
  $0.50 is the immutable `Entry_Ceiling`**. C can never be the entry candle.
- Entry window (R32-38): C+1..C+5 only; 09:34:00 <= time < 11:30:00. Benchmarks come from the most
  recently *completed* candle (VWAP, and for Scenario 2 EMA9 and EMA20 of that same candle).
  - Scenario 1 (`Confirmation_Close <= Entry_Ceiling`): enter at the first moment
    `Confirmation_Close <= price <= Entry_Ceiling` and `price > benchmark VWAP`.
  - Scenario 2 (`Confirmation_Close > Entry_Ceiling`): the setup is **not** cancelled; enter at the first
    moment `price <= Entry_Ceiling` and `price > benchmark VWAP, EMA9, EMA20` — entry below the
    confirmation close is allowed.
  - Both: filters pass (stock EMA momentum R39; QQQ EMA momentum R40). A completed C+1..C+5 candle
    closing below its VWAP purges the setup; C+5 without a fill purges it; never an entry in C+6.
    A flat or falling VWAP never blocks (R41). There is no VWAP-crossing filter any more.
- One setup = one trade: the engine marks the setup consumed when it submits the order; the exit is the
  purge instant for everything before it.

## Intrabar execution semantics (rule 33-35)

- **SHADOW / PAPER / LIVE**: the BUY is evaluated on every trade tick (`EDGE_ON_TRADE`) against the
  developing candle: the first print meeting the conditions triggers an immediate market buy, with no
  completed-green requirement. Because the V2 benchmarks come from completed candles, the qualifying
  price band is fixed for the whole candle; only the moment of entry depends on the tick stream. When a
  fresh ask is known and exceeds the frozen `Entry_Ceiling`, the entry is *deferred* (an execution
  safeguard on top of the rules, which test the trade price; it can only delay an entry).
- **Historical OHLC replay** knows the band but not the path through it, so it never invents a favorable
  one (`bar_entry`):
  1. the candle's open already qualifies → exact fill at the open, stamped at the candle start;
  2. the open is *below* the band and the high reaches it → price must have risen through the band's
     lowest price (`Confirmation_Close`, or the first tick above the benchmark VWAP/EMAs): fill modeled
     there, stamped at the candle end (the latest possible instant). If the candle's own low would
     breach the $3.00 dip it may have printed first, so no fill is guessed;
  3. the open is *above* the band and the low reaches it → price must have fallen through the
     `Entry_Ceiling` (the highest, i.e. worst, qualifying price): fill modeled there;
  4. otherwise no entry — there is never a fall-back to a later close/open.
  A path that *jumps over* the band is not modeled (assumption: tick-level continuity). Fills are booked
  by `HistoricalExecutionSimulator.fill_entry_in_bar`, carry `MODELED_INTRABAR_ENTRY` plus the usual
  `BAR_ONLY` limitation, and use the configured slippage. With quote evidence nothing is modeled and the
  order fills from quotes after the candle (late).

## Protection (rules 42-46) - two stages (logic unchanged from V1)

- Stage 1 (R42): the moment the fill is confirmed, a fixed stop at `Fill - $0.75` (floored to the tick:
  the trigger set `price <= Fill - 0.75` is identical on the tick grid). It does not follow VWAP.
- Stage 2 (R43-45): when the market trades at `Fill + $0.50` the stop moves **once, upward** to
  breakeven (`Fill`, floored to the tick) and the EMA9 exit arms. Then 100% of the position (R46) exits
  at once when `price <= Fill` or `price < EMA9` of the last *completed* candle, intrabar.
- `ProtectionKind.FIXED_THEN_BREAKEVEN_STOP`; migration `0004_breakeven_protection_kind.sql`.
- Responsibilities. Broker/simulator-held: Stage 1 stop and the breakeven replacement. Application-
  managed: the EMA9 breach and the breakeven breach in the corrected SELL (every trade tick, plus the
  broker stop at breakeven). The corrected SELL signal is classified `ExitSource.PROTECTIVE_STOP`
  (priority 0, ignores minimum hold) in SHADOW, PAPER/LIVE and the backtest. **PAPER/LIVE do not provide
  continuous intrabar EMA9 protection**: during a process or feed outage only the broker-held stop
  protects (there is no broker-native EMA-trailing order).
- Backtest (R51): one resting stop emulates Stages 1-2 for each next candle: `max(breakeven, EMA9 level)`.
  A candle that touches both the initial stop and `Fill + $0.50` is stop-first; Stage 2 applies from the
  candle after the one that reached it. The entry candle's own stop test is stop-first.

## Account-level controls (rules 47-48)

- `risk.daily_loss.DailyLossGuard` (R47): `realized trading-day P/L + unrealized P/L >= $150 loss`
  latches for the New York trading day: market exits for every open position (retried until flat) and no
  new entries from **any** strategy on that account. The latch resets next trading day. A stricter
  configured limit wins (`effective_limit`). Accounting sources: SHADOW `ShadowAccountState.realized_by_day`
  + `unrealized_pnl()`; PAPER/LIVE closed-cycle P/L from the ledger + protected-position marks, durably
  blocked via entry cooldowns until the next session; backtest equity delta, with the current candle's
  low as the adverse mark (liquidation then fills at the next open).
- Forced liquidation (R48) 15:55 NY, or 5 minutes before an early close, is mandatory for the corrected
  strategies: the backtest forces it on; SHADOW/PAPER/LIVE rely on `end_of_day_close.enabled: true`
  (`at: "15:55"`; early closes derive from the session close).

## Rule-to-implementation traceability (V2, rules 1-51)

Implementation paths are relative to `mike_trade/`; `mc` = `strategies/michelle_corrected.py`,
`stop` = `risk/michelle_corrected_stop.py`, `BUY`/`SELL` = the adapters under `strategies/buy|sell/`.
Tests are in `tests/unit/`: **R** = `test_michelle_corrected_rules.py`, **L** =
`test_michelle_corrected_lifecycle.py`, **P** = `test_michelle_corrected_paper.py`; each test name starts
with `test_rule<N>_` (several rules per test are joined, e.g. `test_rule33_35_...`).

| Rule | Requirement | Implementation | Focused test(s) |
|---|---|---|---|
| 1 | Long entries, one-minute US equities | `BUY._SPEC` (side, M1) | L `test_rule1_buy_is_a_long_entry_strategy_on_one_minute_candles` |
| 2 | RTH data; premarket only seeds EMAs | `mc.prepare_session` | R `test_rule2_premarket_seeds_emas_only_and_vwap_is_regular_session_only` |
| 3 | Session VWAP anchored 09:30, daily reset | `mc.prepare_session` | R `test_rule3_session_vwap_is_anchored_at_the_open_and_resets_every_day` |
| 4 | Entry only 09:34:00 <= t < 11:30:00 | `mc.time_gate` in `bar_entry`/`live_entry` | R `test_rule4_entry_hours`, `test_rule4_and_32_an_entry_candle_starting_at_1130_is_refused` |
| 5 | No signal on stale data / open position | `BUY.evaluate` | L `test_rule5_buy_ignores_stale_data_non_eligible_symbols_and_held_positions` |
| 6 | Setup alignment (close and both EMAs below VWAP) | `mc.starts_setup` | R `test_rule6_setup_requires_close_below_vwap_and_both_emas_with_emas_below_vwap`, `test_rule6_ema9_may_be_on_either_side_of_ema20` |
| 7 | No fresh crossover needed | `mc.scan` | R `test_rule7_an_existing_bearish_alignment_needs_no_fresh_crossover` |
| 8 | Setup start = first qualifying candle; part of pullback/pattern | `mc.scan` | R `test_rule8_*` (3 tests) |
| 9 | Setup low and associated VWAP | `mc.scan` | R `test_rule9_lower_low_updates_associated_vwap_and_equal_low_does_not` |
| 10 | Max dip $3.00 inclusive, until filled | `mc.scan`, `mc.live_dip_breached`, `mc.live_entry`, `mc.bar_entry` | R `test_rule10_*`; L `test_rule10_buy_intrabar_dip_breach_purges_the_setup_and_persists_the_barrier` |
| 11 | Confirmation must start within 30 min | `mc.scan` (`SETUP_TIMEOUT`) | R `test_rule11_confirmation_exactly_thirty_minutes_after_setup_start_is_allowed` |
| 12 | >= 3 consecutive closes below VWAP before C | `mc.scan` (`run`, `pullback_ok`) | R `test_rule12_requires_three_consecutive_closes_below_vwap_before_candle_c_starts` |
| 13 | One of three patterns, none from before the setup | `mc.detect_pattern`, `mc.scan` | R `test_rule13_*` |
| 14 | Priority Hammer > Shelf > Engulfing | `mc.detect_pattern` | R `test_rule14_pattern_priority_is_hammer_then_shelf_then_engulfing` |
| 15 | Pattern lows; Pattern 1 / 2 close-based invalidation; cancel | `mc.scan`, `mc._refresh_shelf`, `mc.detect_pattern` | R `test_rule15_*` (10 tests) |
| 16 | Hammer: body below VWAP, high > low | `mc.is_hammer` | R `test_rule16_17_18_19_hammer_geometry`, `test_rule16_the_whole_body_must_be_below_vwap` |
| 17 | Lower wick >= 2x body (doji passes) | `mc.is_hammer` | R `test_rule16_17_18_19_hammer_geometry` |
| 18 | Open and close in upper half (midpoint ok) | `mc.is_hammer` | R `test_rule16_17_18_19_hammer_geometry` |
| 19 | Any color; no upper-wick limit | `mc.is_hammer` | R `test_rule19_hammer_has_no_upper_wick_cap` |
| 20 | Shelf: >= 2 candles, extend, lock | `mc.extend_shelf`, `mc.shelf_pattern` | R `test_rule20_*` (3 tests) |
| 21 | Every shelf close below its own VWAP | `mc.extend_shelf` | R `test_rule21_every_close_must_be_strictly_below_its_own_vwap` |
| 22 | Shelf close range <= $0.25 | `mc.extend_shelf` | R `test_rule22_close_range_of_exactly_025_is_allowed_and_more_is_not` |
| 23 | Engulfing: red then green, closes below VWAP | `mc.is_bullish_engulfing` | R `test_rule23_24_bullish_engulfing_equal_boundaries_and_no_wick_requirement` |
| 24 | Green open <= red close, close >= red open | `mc.is_bullish_engulfing` | R `test_rule23_24_...` |
| 25 | Highs/wicks may reach VWAP; only closes/bodies bind | patterns above | R `test_rule25_highs_and_wicks_may_reach_or_exceed_vwap_for_all_three_patterns` |
| 26 | Candle C green | `mc.scan` | R `test_rule26_27_confirmation_candle_geometry` |
| 27 | Low <= VWAP, close > VWAP | `mc.scan` | R `test_rule26_27_confirmation_candle_geometry` |
| 28 | C need not open below / follow a below-VWAP candle | `mc.scan` | R `test_rule28_candle_c_may_open_above_vwap_and_follow_an_above_vwap_bar` |
| 29 | C starts within 30 min of the setup start | `mc.scan` | R `test_rule29_candle_c_must_start_within_thirty_minutes_of_the_setup_start` |
| 30 | First qualifying candle; frozen close and ceiling | `mc.scan` (`entry_ceiling`) | R `test_rule30_first_qualifying_candle_wins_and_close_and_ceiling_are_frozen`; L `test_rule30_33_38_buy_live_rejects_trades_above_the_frozen_ceiling_and_c_plus_six` |
| 31 | C is not the entry candle | `mc.window_number` | R `test_rule31_candle_c_cannot_be_the_entry_candle` |
| 32 | Entry only in C+1..C+5 | `mc.window_number`, `bar_entry`, `live_entry` | R `test_rule32_entry_is_allowed_only_in_c_plus_one_through_c_plus_five` |
| 33 | Scenario 1; entry during a developing candle | `mc.price_qualifies`, `mc.entry_band`, `bar_entry`, `live_entry` | R `test_rule33_*`; L `test_rule33_*` |
| 34 | Scenario 2 (pullback; EMA9/EMA20/VWAP of one candle) | same | R `test_rule34_*`; L `test_rule34_*` |
| 35 | Later move into the range triggers entry | `mc.bar_entry` (TRIGGER legs), `mc.live_entry` | R `test_rule33_35_a_later_move_into_the_range_triggers_the_entry`; L `test_rule35_*` |
| 36 | Price strictly above last completed candle's VWAP | `mc.price_qualifies`, `mc.entry_band` | R `test_rule36_*`; L `test_rule36_*` |
| 37 | Completed C+1..C+5 close below VWAP cancels | `mc.scan` | R `test_rule37_*`; L `test_rule37_*` |
| 38 | Expire after C+5; no C+6; one setup = one trade | `mc.scan`; `BUY` consumption barrier | R `test_rule38_*`; L `test_rule38_49_one_setup_one_trade_...` |
| 39 | Stock EMA momentum test | `mc.ema_momentum`, `mc._filters` | R `test_rule39_*` |
| 40 | Identical momentum test on QQQ; no QQQ VWAP condition | `mc.qqq_momentum`, `mc._filters` | R `test_rule40_*`; L `test_rule40_buy_requires_qqq_momentum_and_fails_closed_without_qqq_data` |
| 41 | Flat / falling VWAP never blocks | no slope test exists anywhere | R `test_rule41_a_flat_or_falling_vwap_never_blocks_setup_confirmation_or_entry` |
| 42 | Initial stop = fill - $0.75, fixed | `stop.initial_stop`, `stop.policy_for_fill`; runner/lifecycle/app protection | L `test_rule42_*`; P `test_rule42_entry_establishes_the_fixed_stage_one_stop_immediately` |
| 43 | Breakeven + EMA9 exit at fill + $0.50 | `stop.stage_two_reached`, `stop.activation_price` | L `test_rule43_*`; P `test_rule43_45_stage_two_replaces_the_stop_...` |
| 44 | Immediate full exit at fill or < EMA9 (last completed) | `stop.exit_trigger`, `SELL.evaluate` | L `test_rule44_*`; P `test_rule44_46_breakeven_breach_is_an_immediate_full_protective_exit_...` |
| 45 | Breakeven never moves down | `stop.effective_stop_level` | L `test_rule45_effective_stop_never_drops_below_breakeven_and_ema_part_can_ease` |
| 46 | Every protective exit closes 100% | `stop.policy_for_fill`, `ProtectionPolicy` validation | L `test_rule46_policy_validation_rejects_a_backward_ladder`, `test_rule43_44_46_backtest_stage_two_...` |
| 47 | $150 net daily loss: liquidate all, block entries | `risk/daily_loss.DailyLossGuard`; runner/lifecycle/app | L `test_rule47_*`; P `test_rule47_daily_loss_limit_liquidates_everything_...` |
| 48 | Close all at 15:55 / 5 min before early close | runner `submit_eod_close`; lifecycle | L `test_rule48_*` |
| 49 | Purge all setup state on every invalidation | `mc.scan` + consumption/purge barrier in `BUY` | R `test_rule49_*` (3 tests, one parametrized over five invalidations) |
| 50 | No reuse of earlier candles/patterns; fresh start | `mc.scan(barrier)`, `BUY` session reset | R `test_rule50_*`; L `test_rule50_setup_state_never_leaks_into_the_next_session` |
| 51 | OHLC bar touching stop and +$0.50: stop first | `SELL.evaluate` (bar replay), runner `reposition_two_stage` | L `test_rule51_*` (2 tests) |

## JEV

No JEV evidence interface exists in this checkout (nothing to integrate). The BUY persists the facts
that exist at signal time as `entry_evidence` (setup start, setup low and associated VWAP, pattern
kind/stage/low, confirmation close, entry ceiling, entry scenario, window number, the stock's and QQQ's EMA
momentum values, benchmark VWAP, entry price and distance above it) so a future adapter can map them.
The protective exit source is `PROTECTIVE_STOP`, which any review gate must treat as non-vetoable.

## Interpretations and open ambiguities

The V2 text is unambiguous on boundaries; these points needed a decision (all documented in the code):

- **A1 Shelf life cycle (R20).** "Qualifies once at least 2 candles ... locked in once a candle closes
  higher": a base is a *qualified* Pattern from its second candle (it can be Pattern 1 / confirm the
  setup immediately). While later closes keep extending it (within the $0.25 range, adjacent candles),
  its pattern low follows the lowest close; the extending close is part of the base and never a "close
  below the pattern low". A candle that does not extend (higher, lower, or at/above VWAP) ends the base
  and — if it still closes below VWAP — starts the next candidate.
- **A2 Pattern 2 may include the candle that discarded Pattern 1** (e.g. the discarding candle is
  itself a lower hammer, or the first candle of a lower shelf); it must be strictly lower than Pattern 1.
- **A3 "Third lower pattern" (R15)** can never occur after a close below Pattern 2's low because that
  close already cancels the setup; no separate state is kept for it.
- **A4 QQQ alignment (R40).** "Two most recently completed candles" is read as the candles ending at the
  start of the entry candle; a stale or missing QQQ series fails closed. QQQ premarket seeds its EMAs.
- **A5 Purging candle.** The candle that purges a setup (dip, Pattern 2 break, window close below VWAP,
  expiry) never seeds the next setup, except that the candle which starts *after* the 30-minute clock has
  expired is a fresh candle and may (R50: "after the reset").
- **A6 Three closes (R12)** are consecutive in the stored sequence; a one-minute gap in the data does not
  reset the count (a gap does break shelf/engulfing adjacency). A Pattern 1 discard does not reset it.
- **A7 Ask safeguard (live).** An ask above the frozen ceiling defers the entry although the rules test the
  trade price.
- **A8 Stage 2 inside one OHLC candle** is unresolvable and starts the next candle (stop-first, R51).

## Execution-model limitations

- OHLC replay (see above) cannot know when price first enters the qualifying band inside a candle. It uses
  exact opens and conservative band edges (the worst qualifying price for a buyer) and refuses fills it
  cannot support; it does not model a jump over the band, and the real first-qualifying print could be
  better or worse by the width of the band (at most `Entry_Ceiling - Confirmation_Close`, or the width
  between the benchmark and the ceiling).
- Modeled trigger fills are stamped at the candle end, so the first protective check happens in the next
  candle.
- PAPER/LIVE: the broker holds only the fixed Stage 1 / breakeven stop; EMA9 is application-managed.
- IEX-only bars can omit minutes; missing QQQ minutes fail the QQQ filter closed.

## Differences from the original Michelle VWAP strategy

| Topic | Original | Corrected (V2) |
|---|---|---|
| Entry | second green candle, filled at the next open | C+1..C+5 first-qualifying-moment market buy (two scenarios), no extra green |
| Setup | EMA9<EMA20<VWAP, $2 dip, 0.15%-range base | EMA9/EMA20 either order, $3 dip, 2+ candle $0.25 close shelf |
| Confirmation | EMA9 slope/spread test | 3 closes below VWAP, no slope test |
| Filters | optional SPY (ORB only) | stock EMA momentum + QQQ EMA momentum |
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
  warmup_bars: 500      # premarket + 09:30 VWAP history (the stock AND QQQ)
```

Backtest: `strategy_ids=("vwap_bounce_break_michelle_corrected_buy", "vwap_bounce_break_michelle_corrected_sell")`;
QQQ events are added to the feed automatically. The pinned 100-symbol campaign
(`config/research_campaigns/michelle_corrected_vwap_iex_2024_2025_v1.json`, launcher
`scripts/run_michelle_corrected_vwap_iex_2024_2025.py`) uses these ids and the same $0.75 / $0.50 / 15:55 /
$150 / stop-first settings, so it needs no change for V2; its datasets must include QQQ **and** extended-hours
candles for QQQ (`data.session_input: extended_hours_for_ema_seed`).
