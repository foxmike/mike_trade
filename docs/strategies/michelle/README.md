# Michelle strategy source map

Status: **specification complete; Python implementation blocked pending operator decisions and
approved infrastructure work**.

Michelle supplied two authoritative Word documents. They remain outside Git in
`/home/mike/dev/mike/stocks/michelle`; they are referenced here by filename only.

| Original document | Original title | Proposed BUY implementation | Proposed SELL implementation | Status |
|---|---|---|---|---|
| `Five_Minute_ORB_Formatted.docx` | 5-MINUTE OPENING RANGE BREAKOUT (ORB) STRATEGY (LONG ONLY) | [`five_minute_orb_michelle_buy`](five_minute_orb_michelle_buy.md) | [`five_minute_orb_michelle_sell`](five_minute_orb_michelle_sell.md) | Both blocked |
| `VWAP_Bounce_Break_Formatted.docx` | VWAP BOUNCE AND BREAK ORIGINAL STRATEGY | [`vwap_bounce_break_michelle_buy`](vwap_bounce_break_michelle_buy.md) | [`vwap_bounce_break_michelle_sell`](vwap_bounce_break_michelle_sell.md) | Both blocked |

The identifiers are independent. Neither SELL strategy is paired to, enabled by, or selected
automatically with its same-stem BUY strategy.

## Source-document completeness review

Both DOCX files were text-extracted and rendered page by page (one page for the ORB document;
two pages for the VWAP document). All rendered text was legible. OOXML inspection found no tables,
embedded images, drawings, charts, equations, text boxes, comments, tracked changes, footnotes,
endnotes, headers, footers, or embedded objects. Therefore no rule-bearing non-text content was
silently omitted.

## Why implementation is stopped

The existing strategy contract supports only completed-bar triggers: `StrategySpec` rejects
`completed_bars_only=False`, and its `TriggerKind` vocabulary contains only closed-bar edge and
level triggers. Both source BUY rules require an immediate intrabar trade-price crossing. Evaluating
the latest trade only when a bar closes would not establish the first crossing and would introduce
different timing.

The source exits are fill-aware protective-stop/trailing-stop policies. A SELL strategy receives
market context and its own opaque persisted state, but it is not supplied the actual position fill,
VWAP captured at fill, position-cycle high-water mark, or protective-stop state. Strategy code also
must not submit, replace, or cancel protective orders. Exact implementation therefore requires an
approved engine contract for fill/cycle facts and tick-driven evaluation, while preserving the
existing mandatory protective-stop precedence.

Each linked specification separates explicit rules, inferences, missing information, proposed
assumptions requiring approval, and unsupported behavior. No Python implementation or executable
test is claimed by these documents.

## Approval-gated infrastructure required for exact implementation

The following is the minimum credible design scope. It is recorded for review only; none of it has
been implemented.

1. **Intrabar strategy contract and identity.** Add an SDK trigger mode for ordered trade events
   (separate from completed-bar edge/level triggers), carry an immutable event timestamp/sequence,
   and derive replay-safe signal identity from that event rather than reusing one completed-bar id
   for every tick in the minute.
2. **Live event wiring.** Route stream trades (and the required quote snapshot) through the
   scheduler and lifecycle in serialized provider order. The scheduler has generic market-data and
   quote trigger declarations, but the current composition root registers every strategy on M1
   candle close and the live adapter invokes lifecycle evaluation only from closed stream bars.
3. **Historical parity or an explicit mode restriction.** The historical research feed contains
   bars, not ordered trades or bid/ask quotes. Exact backtesting therefore needs a versioned tick/
   quote dataset and deterministic replay semantics. Without it, the two BUY strategies and both
   stop exits cannot honestly be marked ready for historical backtesting.
4. **Point-in-time indicator definitions.** Supply or deterministically calculate the full VWAP,
   ATR, and EMA series required at each rule timestamp, after Michelle resolves the calculation
   ambiguities. Scalar latest-indicator snapshots are insufficient for cycle-low VWAP comparisons.
5. **Fill and position-cycle facts.** Expose a read-only, engine-owned strategy input containing
   actual/partial fill facts and cycle identity, or add a dedicated lifecycle callback. Capture
   fill-time VWAP and the chosen previous-candle low atomically and make them available to an
   independently configured SELL strategy without coupling it to a BUY implementation.
6. **Protection transition policy.** Extend engine-owned protection to express an absolute initial
   stop followed by activation of a percentage trail while preserving the tighter stop and safety
   precedence. Current protection policies select one fixed, native trailing, or stepped-trailing
   shape; they do not express Michelle's documented fixed-to-trailing transition.
7. **Post-fill rejection semantics.** Define what “reject” means when a rule depends on actual fill:
   pre-submit estimate, broker price protection, cancel remainder, or immediate risk exit. This is
   execution behavior and cannot be invented inside an advisory BUY strategy.
8. **Lifecycle feedback.** Define the engine event that consumes an ORB attempt and the cycle reset
   events for VWAP Bounce and Break, then persist those events restart-safely.

These changes affect shared market-data, scheduling, strategy identity, execution/protection, and
historical research behavior. The project instruction requires approval before any such change.

