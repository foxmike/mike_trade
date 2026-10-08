# BUY Strategies — Shipped Definitions and Reference Configuration

This document describes the shipped BUY strategies only. It makes no claim
about alpha, edge, or profitability. Nothing here is a profitability claim,
and there are no confidence scores anywhere in the strategy contract,
implementations, presets, or evaluations.

Related contract: `docs/ARCHITECTURE.md` (module boundaries, strategy
isolation). This file freezes no new architecture; it documents the
implemented specs in `mike_trade/strategies/`.

## 1. Strategy SDK contract

### 1.1 Spec fields

Each strategy declares one immutable `StrategySpec` (`mike_trade/strategies/base.py`):

| Field | Meaning |
| :--- | :--- |
| `strategy_id` | Lowercase snake_case identifier (e.g. `vwap_bounce`). |
| `version` | `MAJOR.MINOR.PATCH` semver string; numeric `version_key` orders versions. |
| `side` | `StrategyRole.BUY` or `StrategyRole.SELL`. `BuyStrategy` subclasses must declare `BUY`. |
| `reference_symbols` | Declared reference symbols (empty tuple for all three shipped BUY strategies). |
| `required_timeframes` | Non-empty tuple of `CandleTimeframe` values the strategy reads. |
| `warmup_bars` | Minimum completed bars before evaluation can signal (`int >= 0`). |
| `completed_bars_only` | Must be `True` for every shipped BUY strategy; developing bars never signal. |
| `requires_volume` | `True` for every shipped BUY strategy; volume is always checked. |
| `trigger_kind` | `EDGE_ON_CLOSED_BAR` for all three shipped BUY strategies (edge-triggered crossing on the latest completed bar). |
| `trigger_timeframe` | The timeframe the trigger is evaluated on; must be a member of `required_timeframes`. |
| `parameter_schema` | Ordered `ParameterSchema` of typed `ParameterSpec` entries (see §1.3). |
| `persistent_state_keys` | Strategy-owned state keys the engine persists (see §1.5). |
| `methodology` | Human-readable textbook definition; states it is not an alpha or profitability claim. |
| `source` | Literature provenance; states it is not backtested here. |
| `execution_limitations` | Entry-signal-only limits (no sizing, stops, targets, or order submission). |
| `reason_codes` | Declared subset of `ReasonCode` that must include `SIGNAL` and `NO_SIGNAL`. |

### 1.2 Pure evaluate on immutable MarketContext

`evaluate(context, params, state) -> StrategyEvaluation` must be pure:

- Inputs are the immutable strategy-facing `MarketContext`
  (`mike_trade/market_data/context.py`: symbol, `as_of`, freshness,
  `session_status`, `session_id`, CLOSED `candles` by timeframe, and the
  shared `indicators` mapping), already-validated `params`, and read-only
  persisted `state`.
- No I/O, no clock reads, no mutation of `params` or `state`. State changes
  are returned as `state_updates` for the engine to persist, never written
  directly.
- No broker, network, filesystem, order, sizing, confidence-score, or secrets
  access. Strategy modules import only the standard library and pure
  domain/market-data value types.
- `StrategyEvaluation` carries reason codes plus, for signals only, a
  deterministic `signal_id`, `bar_time`, optional positive finite
  `Decimal` `reference_price`, and `state_updates`. It carries no confidence
  score, no quantity, and no order data. The fields `confidence` and `score`
  do not exist on `StrategySpec` or `StrategyEvaluation`.

### 1.3 Parameters

- Each `ParameterSpec` has a `name`, `ParameterKind`
  (`INT`, `DECIMAL`, `BOOL`, `ENUM`, `TIME_OF_DAY`), an optional `default`,
  inclusive `min_value`/`max_value` bounds (only for `INT`, `DECIMAL`,
  `TIME_OF_DAY`), `choices` (required for, and only for, `ENUM`), and a
  description.
- A parameter with `default=None` is required; otherwise the default is
  applied by `ParameterSchema.validate`.
- `DECIMAL` parameters accept `Decimal` or `int` only; `float` is rejected
  for every parameter kind (including bounds and defaults). `int` inputs to
  `DECIMAL` are normalized to `Decimal`. Non-finite `Decimal` values
  (`NaN`, `Infinity`) are rejected.
- Unknown, missing-required, mistyped, out-of-range, or invalid-choice
  values raise `ParameterValidationError` with codes `unknown_parameter`,
  `missing_parameter`, `wrong_type`, `out_of_range`, or `invalid_choice`.
  Validation fails closed: an invalid parameter never produces a signal.

### 1.4 Reason codes and deterministic signal IDs

- `ReasonCode` values: `SIGNAL`, `NO_SIGNAL`, `INSUFFICIENT_DATA`,
  `STALE_DATA`, `NOT_ELIGIBLE`.
- `for_signal` always carries exactly `(SIGNAL,)` plus a `signal_id`.
  `no_signal` carries one or more declared codes; undeclared codes raise.
- `make_signal_id(strategy_id, version, symbol, side, bar_time)` is the
  SHA-256 hex digest of the canonical JSON list
  `[strategy_id, version, SYMBOL, side, bar_start_UTC]`, where `bar_time` is
  the triggering completed bar's start normalized to UTC. The same bar
  therefore always yields the same ID; any change to strategy id, version,
  symbol, side, or bar start yields a different ID. `bar_time` must be
  timezone-aware.

### 1.5 Rearm and duplicate suppression

- The engine owns the `"eligibility"` state key. Every shipped BUY strategy
  only reads it: any value other than `"WATCHING_BUY"` returns
  `NOT_ELIGIBLE`. Strategies never write `"eligibility"`.
- Each strategy owns the keys listed in its `persistent_state_keys`:
  `session_id` (new-session detection; falls back to the session date when
  `context.session_id` is absent), `last_signal_bar` (ISO start of the bar
  that fired; replaying the same completed bar never emits twice), and — for
  the two breakout strategies only — a `fired` latch (see per-strategy
  rules below). `vwap_bounce` carries no `fired` latch and rearms naturally.
- A new session resets strategy-owned firing state; evaluation windows never
  span sessions.

### 1.6 Explicit trusted registry; no DSL or dynamic loading

- `StrategyRegistry` (`mike_trade/strategies/registry.py`) maps
  `(strategy_id, version)` to a registered class. Registration is an
  explicit call by the composition root (`registry.register(MyBuyStrategy)`)
  after a normal import.
- There is no dynamic code evaluation, no entry-point or path-based plugin
  discovery, no rules DSL, and no `eval`/`exec`/`compile`/`open`/`__import__`
  in strategy, base, or registry modules.
- One registry holds both sides keyed by `(strategy_id, version)`. BUY and
  SELL entries are independent: registering or evaluating a BUY strategy
  never requires a SELL partner. `get` with no version returns the highest
  registered semantic version; unknown ids or versions raise
  `StrategyRegistryError` (`not_found`), duplicates raise
  (`duplicate_registration`), and `validate_parameters` delegates to the
  spec's parameter schema.

## 2. `opening_range_breakout` (BUY, 1.0.0)

### Objective trigger definition

Classic opening-range breakout on completed 1-minute bars: the opening range
is built from the first `opening_range_minutes` completed regular-session
1-minute bars after the 09:30 America/New_York session open (range high =
maximum high, range low = minimum low). After the range window has fully
completed, an edge-triggered BUY signal fires when the latest completed bar
closes strictly above the range high while the prior completed bar closed at
or below it:

```text
prior.close <= range_high AND latest.close > range_high  ->  SIGNAL
```

A close exactly equal to the range high is not a breakout (`NO_SIGNAL`).
A prior close already above the range high is not an edge (`NO_SIGNAL`).

### Parameters, ranges, defaults, provenance

| Parameter | Kind | Range | Default | Provenance |
| :--- | :--- | :--- | :--- | :--- |
| `opening_range_minutes` | `INT` | `[1, 120]` inclusive | `30` (`DEFAULT_OPENING_RANGE_MINUTES`) | Shipped spec default; the conventional 30-minute opening range carried from the implemented spec. Reference value only, not fitted or optimized here. |
| `min_breakout_volume` | `DECIMAL` | `>= 0`, no upper bound | `Decimal("0")` | Shipped spec default; `0` disables the volume filter. Reference value only, not fitted or optimized here. `int` is accepted and normalized to `Decimal`; `float` is rejected. |

### Data, closed-bar, volume, and warmup requirements

- `required_timeframes = (M1,)`, `trigger_timeframe = M1`,
  `trigger_kind = EDGE_ON_CLOSED_BAR`, `completed_bars_only = True`,
  `requires_volume = True`.
- Every evaluated bar must be a valid CLOSED 1-minute bar for the context
  symbol: finite positive `Decimal` OHLC with `low <= open <= high`,
  `low <= close <= high`, `high >= low`, finite `Decimal` volume `>= 0`,
  and matching symbol/timeframe/status. Any invalid bar returns
  `INSUFFICIENT_DATA`.
- At least `opening_range_minutes + 1` total bars are required, with exactly
  `opening_range_minutes` contiguous session bars starting at 09:30 ET and
  one post-window bar. `spec.warmup_bars = 31` (default range 30 plus one
  breakout bar). A missing or duplicated minute inside the window, a window
  that has not fully completed, or a prior bar dated before the window open
  returns `INSUFFICIENT_DATA`.
- Volume: a breakout bar with `volume <= 0`, or with
  `volume < min_breakout_volume`, returns `NO_SIGNAL` (the filter step, not
  a data-validity failure).

### Session behavior

- Regular session only: `session_status != REGULAR` returns `NOT_ELIGIBLE`.
  No overnight or pre/post-market handling.
- The range is rebuilt each session; `session_id` (or the session date
  fallback) detects a new session and forgets any prior firing.

### Rearm and duplicate-suppression rules

- `persistent_state_keys = (session_id, fired, last_signal_bar)`.
- Replaying the same completed bar (`last_signal_bar == latest bar start`)
  returns `NO_SIGNAL` and records `fired = True`.
- After a signal, further closes that stay strictly above the range high
  return `NO_SIGNAL` (no rearm while extended).
- A close at or below the range high clears the latch (`fired = False`,
  `NO_SIGNAL`); the next valid edge-triggered crossing emits a fresh signal
  with a new deterministic ID.

### No-signal reason codes

- `NO_SIGNAL`: non-crossing bar (including close exactly at the range high),
  prior close already above the high, same-bar replay, fired-hold while
  extended, pullback-rearm step, or volume-filtered breakout bar.
- `INSUFFICIENT_DATA`: fewer than `range + 1` bars, any invalid bar,
  incomplete range window, missing/duplicated minute inside the window, or
  latest bar still inside the window.
- `STALE_DATA`: `context.freshness.stale` is true (checked first).
- `NOT_ELIGIBLE`: non-regular session or `eligibility != "WATCHING_BUY"`.

### Methodology / source notes

Methodology: classic opening-range breakout definition as quoted in the spec
(range high/low from the first `opening_range_minutes` completed
regular-session 1-minute bars; edge-triggered cross strictly above the range
high). Source: classic opening-range breakout literature (textbook
definition); not an alpha claim and not backtested here.

### Execution limitations

Entry signal only: no sizing, stops, targets, or order submission. Long
entries only; no short signals. Regular session only. Requires complete
1-minute bars; a missing or invalid bar invalidates the evaluation instead
of signalling. One signal per crossing: rearms only after the close returns
to or below the opening range high.

## 3. `momentum_breakout` (BUY, 1.0.0)

### Objective trigger definition

Classic N-bar high breakout with volume confirmation on completed
regular-session 1-minute bars. The lookback high is the highest high of the
`lookback_bars` completed bars preceding the latest completed bar. An
edge-triggered BUY signal fires when the latest completed close crosses
strictly above that lookback high while the prior completed close was at or
below its own corresponding lookback high, and the breakout bar's volume is
at least `volume_multiple` times the average volume of the lookback bars:

```text
prior.close <= prior_lookback_high
  AND latest.close > lookback_high
  AND latest.volume >= volume_multiple * mean(lookback volumes)  ->  SIGNAL
```

A close exactly equal to the lookback high is not a breakout (`NO_SIGNAL`).
A prior close already above its own lookback high is not an edge
(`NO_SIGNAL`).

### Parameters, ranges, defaults, provenance

| Parameter | Kind | Range | Default | Provenance |
| :--- | :--- | :--- | :--- | :--- |
| `lookback_bars` | `INT` | `[2, 100]` inclusive | `20` (`DEFAULT_LOOKBACK_BARS`) | Shipped spec default; the conventional 20-bar lookback carried from the implemented spec. Reference value only, not fitted or optimized here. |
| `volume_multiple` | `DECIMAL` | `[1, 10]` inclusive | `Decimal("1.5")` (`DEFAULT_VOLUME_MULTIPLE`) | Shipped spec default; the conventional 1.5x volume-confirmation multiple carried from the implemented spec. Reference value only, not fitted or optimized here. `int` is accepted and normalized to `Decimal`; `float` is rejected. |

### Data, closed-bar, volume, and warmup requirements

- `required_timeframes = (M1,)`, `trigger_timeframe = M1`,
  `trigger_kind = EDGE_ON_CLOSED_BAR`, `completed_bars_only = True`,
  `requires_volume = True`.
- Same valid-closed-bar definition as `opening_range_breakout` (CLOSED M1,
  valid OHLC band, finite volume `>= 0`).
- Requires `lookback_bars + 2` total bars and at least `lookback_bars + 2`
  contiguous same-session bars in the evaluation window (a missing minute
  inside the window returns `INSUFFICIENT_DATA`).
  `spec.warmup_bars = 22` (default lookback 20 plus prior-bar and breakout
  bar).
- Volume confirmation uses its own `INSUFFICIENT_DATA` reason, distinct from
  the `NO_SIGNAL` returned for an exact-boundary non-breakout: zero or
  non-positive latest/average volume, or volume below
  `volume_multiple * average`, returns `INSUFFICIENT_DATA`. Volume exactly
  at the threshold signals.

### Session behavior

- Regular session only: `session_status != REGULAR` returns `NOT_ELIGIBLE`.
  No overnight or pre/post-market handling.
- Lookback windows never span sessions: only bars from the latest bar's
  session participate, and a new session forgets any prior firing.

### Rearm and duplicate-suppression rules

- `persistent_state_keys = (session_id, fired, last_signal_bar)`.
- Same-bar replay returns `NO_SIGNAL` with `fired = True` recorded.
- While `fired` and the latest close stays strictly above the lookback high,
  returns `NO_SIGNAL` (no rearm while extended).
- A close at or below the lookback high clears the latch (`fired = False`,
  `NO_SIGNAL`); the next valid edge-triggered crossing with volume
  confirmation emits a fresh signal with a new deterministic ID.

### No-signal reason codes

- `NO_SIGNAL`: non-crossing bar (including close exactly at the lookback
  high), prior close already above its own high, same-bar replay,
  fired-hold while extended, or pullback-rearm step.
- `INSUFFICIENT_DATA`: fewer than `lookback + 2` bars, any invalid bar,
  fewer than `lookback + 2` same-session bars, a missing minute inside the
  window, or failed/insufficient volume confirmation (including zero
  volume).
- `STALE_DATA`: `context.freshness.stale` is true (checked first).
- `NOT_ELIGIBLE`: non-regular session or `eligibility != "WATCHING_BUY"`.

### Methodology / source notes

Methodology: classic N-bar high breakout with volume confirmation as quoted
in the spec. Source: classic N-bar high breakout literature (textbook
definition with volume confirmation); not an alpha claim and not backtested
here.

### Execution limitations

Entry signal only: no sizing, stops, targets, or order submission. Long
entries only; no short signals. Regular session only. Requires complete
1-minute bars; a missing or invalid bar invalidates the evaluation instead
of signalling. Lookback windows never span sessions. One signal per
crossing: rearms only after the close returns to or below the lookback
high.

## 4. `vwap_bounce` (BUY, 1.0.0)

### Objective trigger definition

Session-VWAP upward-crossing reference rule on completed 1m regular-session
bars; prior close <= prior VWAP AND latest close > latest VWAP;
session-reset warmed/fresh VWAP. Formally, with both VWAP values read from
the `MarketContext` indicator service under the `"vwap"` (latest completed
bar) and `"vwap_prev"` (prior completed bar) keys:

```text
prior.close <= vwap_prev AND latest.close > vwap_latest  ->  SIGNAL
```

Boundary behavior: a prior close exactly equal to `vwap_prev` with the
latest close strictly above `vwap_latest` signals; a latest close exactly
equal to `vwap_latest`, a prior close already above `vwap_prev`, or any
downward crossing is `NO_SIGNAL`. The strategy never computes VWAP itself
and never fetches data.

### Parameters, ranges, defaults, provenance

This strategy declares no parameters (`parameter_schema` is empty). Any
supplied parameter is rejected as `unknown_parameter`. There are therefore
no ranges, no defaults, and no default provenance to record. The only
numeric provenance is `spec.warmup_bars = 2`
(`WARMUP_BARS`: two completed session bars plus a warmed session VWAP).

### Data, closed-bar, volume, and warmup requirements

- `required_timeframes = (M1,)`, `trigger_timeframe = M1`,
  `trigger_kind = EDGE_ON_CLOSED_BAR`, `completed_bars_only = True`,
  `requires_volume = True`.
- Same valid-closed-bar definition as above; any invalid bar returns
  `INSUFFICIENT_DATA`. At least two completed same-session bars are
  required, and they must be contiguous minutes (a gap returns
  `INSUFFICIENT_DATA`). A developing bar never participates.
- Either bar carrying zero (or negative-impossible) volume returns
  `INSUFFICIENT_DATA`: zero-volume bars carry no VWAP information.
- Both shared VWAP readings must be warmed, fresh, session-reset values:
  each must be a finite positive `Decimal`. Missing keys, `None`
  (not-warmed), strings, floats, non-finite, zero, or negative values all
  return `INSUFFICIENT_DATA`.
- `spec.warmup_bars = 2`.

### Session behavior

- Regular session only: `session_status != REGULAR` returns `NOT_ELIGIBLE`.
  No overnight or pre/post-market handling.
- Bars and VWAP windows never span sessions; VWAP resets each session via
  the indicator service. Only bars from the latest bar's session
  participate, so a prior-session bar is never used as the prior bar.

### Rearm and duplicate-suppression rules

- `persistent_state_keys = (session_id, last_signal_bar)`; there is no
  `fired` latch.
- Replaying the same completed bar (`last_signal_bar == latest bar start`)
  returns `NO_SIGNAL` and never emits twice.
- The strategy rearms naturally: the next valid upward crossing on a later
  bar emits a fresh signal with a new deterministic ID. One signal per
  completed bar.

### No-signal reason codes

- `NO_SIGNAL`: evaluated crossing is false (latest at VWAP, prior above
  prior VWAP, downward cross), or same-bar replay.
- `INSUFFICIENT_DATA`: fewer than two completed same-session bars, any
  invalid bar, a missing minute between the evaluated bars, zero volume on
  either evaluated bar, or missing/not-warmed/invalid shared VWAP.
- `STALE_DATA`: `context.freshness.stale` is true (checked first).
- `NOT_ELIGIBLE`: non-regular session or `eligibility != "WATCHING_BUY"`.

### Methodology / source notes

Methodology: session-VWAP upward-crossing reference as quoted in the spec
(edge-triggered cross of the warmed, fresh, session-reset shared VWAP
values; the strategy never computes VWAP and never fetches data). Source:
session-VWAP bounce reference definition (textbook reference); not an alpha
claim and not backtested here.

### Execution limitations

Entry signal only: no sizing, stops, targets, or order submission. Long
entries only; no short signals. Regular session only. Requires completed
1-minute bars with positive volume; a missing, stale, invalid, or
zero-volume bar invalidates the evaluation instead of signalling. Requires
warmed per-bar session VWAP (`vwap` and `vwap_prev`); a missing or
not-warmed VWAP invalidates the evaluation. Bars and VWAP windows never
span sessions; VWAP resets each session. One signal per completed bar:
replaying the same bar never emits twice; the strategy rearms naturally on
the next valid crossing.

## 5. Not an alpha or profitability claim; no confidence scores

- Each shipped implementation states a widely known textbook definition
  only. The specs' `methodology` and `source` fields explicitly say the
  definition makes no alpha, edge, or profitability claim and was not
  backtested here. This document repeats that: describing a trigger is not a
  claim that it is profitable.
- Preset parameter values in `mike_trade/strategies/presets/buy_presets.py`
  are the shipped spec defaults (reference values), not fitted, optimized,
  or "profitable" defaults. No preset implies expected returns.
- No confidence scores exist: evaluations carry reason codes and (for
  signals) a deterministic signal ID only. There is no confidence field,
  threshold, or weighting in the SDK, specs, strategies, presets, or
  validation.

## 6. `vwap_reference` configuration contract

- `vwap_bounce` BUY is paired with the `vwap_breakdown` SELL only through
  configuration/presets. The `vwap_bounce` implementation has no hard code
  dependency on `vwap_breakdown`: its module contains no reference to that
  id, no SELL import, and no partner lookup. The registry likewise keeps BUY
  and SELL entries independent — `vwap_bounce` registers, validates, and
  evaluates with no SELL partner present.
- The executable reference to that pairing lives in
  `mike_trade/strategies/presets/buy_presets.py` as the shadow-only
  `vwap_reference` BUY preset: it references `vwap_bounce` by
  `(strategy_id, version)` with an (empty) parameter dict, and names its
  SELL partner id `vwap_breakdown` by string only. The preset module does
  not import any SELL strategy module.
- The reference configuration is shadow-only: the `vwap_reference` preset's
  allowed modes are `("shadow",)` only. It is never enabled for `paper` or
  `live` modes. Baseline presets for `opening_range_breakout` and
  `momentum_breakout` carry their shipped spec defaults as executable,
  registry-validated parameter dicts, and every preset is checked by a
  validation function that fails closed on an unknown strategy or invalid
  parameters.
