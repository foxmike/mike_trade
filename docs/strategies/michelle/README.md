# Michelle strategy source map

Status: **implemented and targeted-test verified**.

The source Word documents remain outside Git in `/home/mike/dev/mike/stocks/michelle`.
The approved October 2026 clarifications supersede contradictory source wording.

| Original document | BUY implementation | SELL implementation | Status |
|---|---|---|---|
| `Five_Minute_ORB_Formatted.docx` | [`five_minute_orb_michelle_buy`](five_minute_orb_michelle_buy.md) | [`five_minute_orb_michelle_sell`](five_minute_orb_michelle_sell.md) | Implemented |
| `VWAP_Bounce_Break_Formatted.docx` | [`vwap_bounce_break_michelle_buy`](vwap_bounce_break_michelle_buy.md) | [`vwap_bounce_break_michelle_sell`](vwap_bounce_break_michelle_sell.md) | Implemented |

| `vwap_bounce_break_business_rules_final.docx` (55 rules, supersedes the row above) | [`vwap_bounce_break_michelle_corrected_buy`](vwap_bounce_break_michelle_corrected.md) | [`vwap_bounce_break_michelle_corrected_sell`](vwap_bounce_break_michelle_corrected.md) | Implemented (focused tests only) |

All six identifiers are independent registry entries; the corrected pair follows its own rule set
and protection (see its specification) and never shares the original attempt limiter.

The first four are independent registry entries. A same-stem SELL is optional; when it is
configured for a position opened by its matching BUY, the lifecycle installs the approved
fixed-then-trailing protection. Without that SELL, the symbol's ordinary mandatory stop policy
continues to apply.

## Shared approved behavior

- Regular US equity session, America/New_York; no Michelle BUY at or after 11:30 ET.
- MARKET entry, engine-owned sizing/risk/submission, no pyramiding or partial strategy exit.
- At most two submitted Michelle BUY attempts per symbol in a rolling ten minutes, shared by both
  BUY strategies, and never on consecutive one-minute candles. The attempt is recorded immediately
  before broker submission, so submitted rejects and unfilled orders count.
- VWAP starts at 09:30; EMA uses completed M1 bars; ATR is Wilder's 14-day seed from 15 completed
  regular-session D1 bars (14 true ranges with their required prior close).
- Live entries evaluate ordered trade events and a developing M1 candle. Historical replay uses
  only completed M1 information and submits a market order for the next eligible M1 open.
- Protection is installed immediately after a positive fill and is identical for both strategies:
  `InitialStop = actual BUY fill - $0.25` (rounded up to the tick), then a **0.2%** trail of the
  highest traded price since the fill that activates only when the tick-adjusted candidate strictly
  exceeds the initial stop (Michelle's confirmed clarification; see the two SELL specifications).
  The stop never decreases. Engine protection runs before advisory SELL evaluation.
- Existing configurable EOD liquidation remains authoritative.

## Deterministic engineering decisions

- Session VWAP uses cumulative typical price `(high + low + close) / 3`, volume-weighted from
  09:30. Live VWAP includes the causally observed developing trade-built minute.
- EMA9/EMA20 are SMA-seeded exponential moving averages. Spread comparison uses absolute spread.
- Base consolidation uses the third candle's close as its reference price. Engulfing boundaries
  are inclusive. Zero-range hammers do not qualify.
- Historical same-bar stop/trail ordering is conservative: a stop ratcheted from a completed bar's
  high becomes effective after that bar, so its low cannot retrospectively trigger the tighter stop.
  A gap through the stop fills at the gapped open; the stop is a level, not a guaranteed price.
- The ORB BUY entry gate (risk/share `<= 1%` against `ORB_High - $0.25`) is an entry qualification
  rule and is unchanged; it no longer describes the installed stop, which derives from the fill.
- PAPER/LIVE are not enabled for this new transition unless the active broker adapter can preserve
  broker-native protection through safe replacement. SHADOW and historical replay are supported.

## Source review

Both DOCX files were extracted and rendered completely. They contain no rule-bearing tables,
images, drawings, charts, equations, comments, tracked changes, headers, footers, or embedded
objects. No original document was copied into Git.
