# Strategies — Shipped Definitions and Reference Configuration

Sections 1–7 describe the original reference strategies and the strategy
contract; section 8 documents the first technical batch and section 9 records
its original candidate methods. Sections 10–12 document later finite-window,
indicator, and current-session strategy additions. It makes no claim
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
| `category` | Stable classification identifier, e.g. `technical` (see §1.7). |
| `sub_category` | Stable sub-classification identifier, e.g. `moving_average` (see §1.7). |
| `reference_symbols` | Declared reference symbols (empty tuple for all three shipped BUY strategies). |
| `required_timeframes` | Non-empty tuple of `CandleTimeframe` values the strategy reads. |
| `warmup_bars` | Completed trigger-timeframe bars needed at the strategy's default parameters (`int >= 0`). A strategy whose lookback depends on its parameters overrides the `required_bars(params)` classmethod (default: `warmup_bars`); the backtest runner and `SellRule` gate on `required_bars(params)`. |
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

### 1.7 Classification: `category` and `sub_category`

Every `StrategySpec` carries exactly two classification fields, both plain
immutable strings on the class-level spec (no registry, metadata service or
subclass hierarchy is involved, and neither field changes strategy behavior):

- `category` must be one of: `technical`, `statistical`, `machine_learning`,
  `deep_learning`, `reinforcement_learning`, `llm_ai`, `microstructure`,
  `event_fundamental`, `relative_value`, `alternative_data`
  (`STRATEGY_CATEGORIES` in `mike_trade/strategies/base.py`).
- `sub_category` must, for `category == "technical"`, be one of: `momentum`,
  `breakout`, `vwap`, `moving_average`, `mean_reversion`, `trend_following`,
  `volatility`, `price_action`, `gap_trading`, `intraday_session`,
  `oscillator`, `volume` (`TECHNICAL_SUB_CATEGORIES`). The other categories
  have no sub-category vocabulary yet, so any lowercase snake_case identifier
  is accepted for them until one is defined.

Both fields are required and validated when the spec is built; an unknown
`category`, or an unknown technical `sub_category`, raises `ValueError`.
Assignments for the six original strategies:

| Strategy | Role | `category` | `sub_category` |
| :--- | :--- | :--- | :--- |
| `vwap_bounce` | BUY | `technical` | `vwap` |
| `momentum_breakout` | BUY | `technical` | `momentum` |
| `opening_range_breakout` | BUY | `technical` | `breakout` |
| `vwap_breakdown` | SELL | `technical` | `vwap` |
| `momentum_reversal` | SELL | `technical` | `momentum` |
| `rsi_reversal` | SELL | `technical` | `oscillator` |

Strategy identifiers, parameter formats, signal behavior and trading
semantics of these six are unchanged. The new library strategies of section 8
are classified in their tables.

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

## 7. Strategy Specification Standard

This section is the authoritative handoff standard for strategies extracted
from books, videos, articles, or other educational sources. NotebookLM may
produce the trader-oriented extraction; a coding agent must turn that
extraction into this specification before writing implementation code. The
standard describes one executable strategy contract. A BUY/SELL lifecycle
pair remains two independently specified strategies, joined by an existing
configuration preset such as `vwap_reference`.

The standard does not add a DSL, interpreter, plugin loader, or second
strategy engine. `mike_trade/strategies/base.py` remains the executable
contract: `StrategySpec`, `ParameterSchema`, `StrategyEvaluation`, and the
`BuyStrategy`/`SellStrategy` interfaces. The registered Python implementation
is authoritative for execution. The YAML below is the canonical, reviewable
serialization exchanged between source extraction, implementation, review,
and research artifacts; it is not loaded as executable rules by the runtime.

### 7.1 Canonical serialization and document rules

Use YAML 1.2 with UTF-8 encoding, one document per file, and a top-level
mapping. JSON is a valid semantic subset for systems that cannot emit YAML,
but YAML is the canonical representation. Emit keys in the order shown in
§7.2, use lowercase snake_case for field names and identifiers, quote dates,
times, versions, decimal values, and free-form strings, and represent absent
optional values as `null` only where the field's table permits `null`.

Equivalent specifications have the same meaning when they have the same
normalized values. Normalization is: identifiers are lowercase snake_case;
symbols are uppercase; enum values use the documented lowercase spelling;
timeframes and session names use the documented values; timestamps are UTC
ISO-8601 strings with `Z`; decimal values are strings with no exponent; lists
whose order is not meaningful are sorted; and mappings are compared by key.
Do not use YAML anchors, aliases, custom tags, executable expressions, or
implicit numeric defaults. A serializer may add a final newline but must not
change field meaning.

`status: draft` is permitted while ambiguities are being resolved. A
specification is `implementation_ready` only when every execution-critical
rule is explicit and `ambiguities` is empty. An unresolved entry trigger,
entry timing, exit, stop, sizing, session, or position-state ambiguity blocks
implementation; the agent must return a question or a documented assumption
for source-owner review instead of silently choosing a rule.

### 7.2 Canonical structure

```yaml
schema_name: mike_trade_strategy_spec
schema_version: "1.0"
status: implementation_ready
identity:
  strategy_id: vwap_bounce
  name: "VWAP bounce"
  version: "1.0.0"
  description: "..."
  family: mean_reversion
  category: technical
  sub_category: vwap
  side: buy
  implementation_version: "1.0.0"
provenance:
  source_type: textbook_reference
  source_title: "..."
  source_locator: "..."
  extracted_by: "NotebookLM"
  extraction_date: "2026-01-01"
  source_rules: ["..."]
  demonstrated_behavior: ["..."]
markets:
  asset_classes: [us_equity]
  symbols: []
  venues: [iex]
  allowed_modes: [shadow]
  direction: long_only
time:
  timeframes: [1m]
  trigger_timeframe: 1m
  trigger_kind: edge_on_closed_bar
  session: regular
  timezone: America/New_York
  completed_bars_only: true
  warmup_bars: 2
  entry_cutoff: null
data:
  required_inputs:
    - name: candles
      kind: candle
      timeframe: 1m
      fields: [open, high, low, close, volume]
      required: true
    - name: vwap
      kind: indicator
      timeframe: 1m
      fields: [value]
      required: true
  freshness_required: true
  missing_data_behavior: no_signal
indicators:
  - id: vwap
    definition: "Shared session VWAP supplied by the indicator service"
    inputs: [candles]
    reset: session
    owner: market_data_service
    required: true
rules:
  setup_conditions: ["..."]
  entry_triggers: ["..."]
  entry_timing:
    trigger: completed_bar_close
    execution: next_bar_open
    order_type: market
  exit_conditions: ["..."]
  stop_loss:
    behavior: none
    value: null
  profit_targets: []
  trailing_stop:
    behavior: none
    value: null
position_sizing:
  method: delegated
  formula: null
  inputs: []
risk_constraints:
  maximum_positions: null
  maximum_loss: null
  protective_requirements: []
  enforcement_owner: risk_and_execution
reentry:
  policy: next_valid_crossing
  restrictions: [same_completed_bar_suppressed]
state:
  persistent_keys: [session_id, last_signal_bar]
  ownership: strategy
  reset: session
  transitions: ["..."]
reason_codes: [signal, no_signal, insufficient_data, stale_data, not_eligible]
parameters: []
ambiguities: []
implementation_assumptions: []
validation_scenarios:
  - id: upward_cross
    given: "..."
    expect: signal
  - id: exact_boundary
    given: "..."
    expect: no_signal
identity_links:
  strategy_spec_id: vwap_bounce@1.0.0
  implementation: mike_trade.strategies.buy.vwap_bounce:VwapBounceBuy
  registry_key: [vwap_bounce, "1.0.0"]
  parameter_configuration_hash: null
  research_experiment_ids: []
```

The example is deliberately explicit about `null`, empty lists, and
delegation. `null` means the strategy does not define that behavior; it never
means “choose a conventional stop, target, or quantity.” Execution defaults
such as next-bar-open fills or slippage belong to the execution/backtest
configuration and must be recorded there, not invented in a strategy spec.

### 7.3 Field contract

The following table defines every canonical field. “Required” means required
in a `implementation_ready` document; optional draft fields must still be
present when their section is relevant. Permitted values are closed wherever
the existing engine has a closed enum. Relationships name the existing
component that consumes or verifies the field.

| Field | Meaning; type | Required | Permitted values/default | Existing component relationship |
| :--- | :--- | :---: | :--- | :--- |
| `schema_name` | Serialization contract name; string | Yes | `mike_trade_strategy_spec` | Documentation contract; not a runtime strategy id |
| `schema_version` | This document format version; string | Yes | Semver-like string; current `1.0` | Allows this handoff format to evolve independently of strategy versions |
| `status` | Review/readiness state; string | Yes | `draft`, `implementation_ready`, `retired`; no implicit default | `implementation_ready` is a gate for the agent contract |
| `identity.strategy_id` | Stable executable identity; string | Yes | Lowercase snake_case | `StrategySpec.strategy_id`, registry key, signal ID |
| `identity.name` | Human-readable name; string | Yes | Non-empty; descriptive only | Documentation and reports |
| `identity.version` | Version of formal trading rules; string | Yes | `MAJOR.MINOR.PATCH` | `StrategySpec.version`; deterministic signal IDs and registry key |
| `identity.description` | Concise behavior summary; string | Yes | Non-empty | `StrategySpec.methodology`/review material |
| `identity.family` | Strategy category; string | Yes | Existing or reviewed taxonomy such as `breakout`, `momentum`, `mean_reversion`, `trend_following`, `exit`, `risk_overlay` | Classification only; does not select code |
| `identity.category` | Top-level classification; string | Yes | One of `STRATEGY_CATEGORIES` (§1.7) | `StrategySpec.category`; classification only, does not select code |
| `identity.sub_category` | Sub-classification; string | Yes | For `technical`, one of `TECHNICAL_SUB_CATEGORIES` (§1.7) | `StrategySpec.sub_category`; classification only |
| `identity.side` | Signal direction; enum | Yes | `buy`, `sell` | `StrategyRole.BUY`/`SELL`; class must be `BuyStrategy`/`SellStrategy` |
| `identity.implementation_version` | Version of Python implementation; string | Yes | Semver | Recorded in implementation metadata; separate from rule version |
| `provenance.source_type` | Kind of source evidence; enum | Yes | `book`, `video`, `article`, `textbook_reference`, `research_note`, `other` | Maps to `StrategySpec.source` and review provenance |
| `provenance.source_title` | Source title; string | Yes | Non-empty | `StrategySpec.source` |
| `provenance.source_locator` | Page, chapter, URL, timestamp, or citation; string | Yes | Non-empty or `unknown` with explanation in ambiguities | Reproducibility metadata |
| `provenance.extracted_by` | Extraction tool/person; string | Yes | Non-empty | Reproducibility metadata; commonly `NotebookLM` |
| `provenance.extraction_date` | Date of extraction; date string | Yes | `YYYY-MM-DD` | Reproducibility metadata |
| `provenance.source_rules` | Rules directly stated by source; list of strings | Yes | May be empty only if source has no rule, which blocks readiness | Must remain distinguishable from assumptions |
| `provenance.demonstrated_behavior` | Behavior shown in source examples; list of strings | Yes | May be empty; never promoted to a rule silently | Evidence used during formalization |
| `markets.asset_classes` | Instrument classes; list of enums | Yes | Existing domain classes such as `us_equity`, `crypto`, `future`; no invented class | Configuration/universe validation |
| `markets.symbols` | Fixed reference symbols; list of uppercase strings | Yes | Empty means symbol-agnostic | `StrategySpec.reference_symbols`; market-data scope |
| `markets.venues` | Intended venues/feeds; list of strings | Yes | Configured provider/venue names such as `iex` | Data provenance and experiment configuration |
| `markets.allowed_modes` | Execution modes; list of enums | Yes | `shadow`, `paper`, `live`, `backtest`; source safety restrictions must be explicit | Presets/pair validation; mode gates |
| `markets.direction` | Position direction capability; enum | Yes | `long_only`, `short_only`, `long_and_short` | Must agree with side and existing broker/risk support |
| `time.timeframes` | Required bar timeframes; list of enums | Yes | Existing `CandleTimeframe` values such as `1m` | `StrategySpec.required_timeframes` |
| `time.trigger_timeframe` | Timeframe on which evaluation can signal; enum | Yes | Member of `timeframes` | `StrategySpec.trigger_timeframe` |
| `time.trigger_kind` | Trigger evaluation mode; enum | Yes | `edge_on_closed_bar`, `level_on_closed_bar` | `StrategySpec.trigger_kind` (`TriggerKind`) |
| `time.session` | Session restriction; enum/string | Yes | `regular`, `extended`, `overnight`, `all`, or named reviewed session | `MarketContext.session_status` and calendar |
| `time.timezone` | Session timezone; IANA string | Yes | Valid IANA zone; equities commonly `America/New_York` | Session boundaries and bar grouping |
| `time.completed_bars_only` | Developing bars forbidden; boolean | Yes | `true` for current engine strategies; no default | `StrategySpec.completed_bars_only` |
| `time.warmup_bars` | Minimum completed bars; integer | Yes | `>= 0`; no default unless source/implementation defines it | `StrategySpec.warmup_bars`; missing warmup returns no signal |
| `time.entry_cutoff` | Latest allowed entry time; time or null | Yes | `HH:MM` in `timezone`, or `null` if delegated | Scheduler/configuration; do not infer a cutoff |
| `data.required_inputs` | Every market input; list of mappings | Yes | `kind`: `candle`, `quote`, `trade`, `indicator`, `fundamental`, `reference`; each input has `name`, `timeframe`, `fields`, `required` | `MarketContext`, market-data and research ports |
| `data.freshness_required` | Whether stale inputs block evaluation; boolean | Yes | `true` or `false`; current strategies use `true` | `MarketContext.freshness`, `STALE_DATA` |
| `data.missing_data_behavior` | Result when required input is absent/invalid; enum | Yes | `no_signal`, `error`, `block_strategy` | Existing strategies return `INSUFFICIENT_DATA`/no signal; fail closed |
| `indicators` | Indicator definitions; list of mappings | Yes | Each has `id`, `definition`, `inputs`, `reset`, `owner`, `required` | Shared indicator service; strategy must not recompute owned indicators |
| `rules.setup_conditions` | Preconditions before trigger; list of strings | Yes | Deterministic predicates; empty allowed when explicitly none | Implementation evaluation order |
| `rules.entry_triggers` | Exact signal predicates; list of strings | Yes | Deterministic boolean predicates with boundary operators | `evaluate`/`evaluate_trigger`; `StrategyEvaluation.for_signal` |
| `rules.entry_timing.trigger` | Event at which signal is decided; enum | Yes | `completed_bar_close`, `quote`, `trade`, `session_event` | Scheduler trigger and `trigger_kind` |
| `rules.entry_timing.execution` | When/how intent may fill; enum/string | Yes | `next_bar_open`, `same_bar`, `immediate`, `delegated`; `delegated` when strategy emits signal only | Execution/backtest semantics; never silently chosen |
| `rules.entry_timing.order_type` | Requested order semantics; enum/string | Yes | Existing `market`, `limit`, `stop`, `trailing_stop`, `signal_only`; `signal_only` for strategy SDK rules | Order intent/execution layer; strategy evaluation carries no order |
| `rules.exit_conditions` | Discretionary exit predicates; list of strings | Yes | Empty only for entry-only strategies | SELL strategy or lifecycle policy; strategy pairing is configuration |
| `rules.stop_loss.behavior` | Stop rule ownership; enum | Yes | `none`, `fixed_percent`, `fixed_price`, `atr`, `trailing`, `delegated` | Risk/protection policy; no stop is invented when `none` |
| `rules.stop_loss.value` | Stop value/definition; mapping/string/null | Yes | Must match behavior; `null` for `none`/`delegated` | Risk policy parameters |
| `rules.profit_targets` | Target rules; list of mappings | Yes | Empty when none specified | Execution/risk policy; no default target |
| `rules.trailing_stop.behavior` | Trailing ownership; enum | Yes | `none`, `fixed_percent`, `fixed_distance`, `stepwise`, `delegated` | Existing protection coordinator/pair preset |
| `rules.trailing_stop.value` | Trail definition; mapping/string/null | Yes | Must match behavior; `null` for `none`/`delegated` | Protection policy |
| `position_sizing.method` | Quantity owner and method; enum/string | Yes | `fixed_quantity`, `fixed_notional`, `percent_equity`, `risk_based`, `delegated`, `signal_only` | Portfolio sizing/intent factory; strategy never emits quantity |
| `position_sizing.formula` | Exact sizing formula; string/null | Yes | Required unless method is `delegated`/`signal_only` | Reproducibility and risk review |
| `position_sizing.inputs` | Sizing inputs; list of strings | Yes | Explicit names; empty when not applicable | Portfolio/risk configuration |
| `risk_constraints` | Risk limits; mapping | Yes | Explicit values or null; no numerical defaults | Risk controls, budgets, protection, entry gate |
| `risk_constraints.enforcement_owner` | Component enforcing each constraint; enum/string | Yes | `strategy`, `portfolio`, `risk`, `execution`, `scheduler`, `delegated` | Prevents duplicate or missing enforcement |
| `reentry.policy` | Reentry behavior; enum/string | Yes | `none`, `next_valid_crossing`, `after_exit`, `session_reset`, `delegated` | Eligibility and strategy state |
| `reentry.restrictions` | Explicit reentry limits; list of strings | Yes | Deterministic restrictions; empty only when none | Engine-owned deduplication/eligibility |
| `state.persistent_keys` | State keys and names; list of strings | Yes | Must match implementation-owned keys; `eligibility` is engine-owned when read | `StrategySpec.persistent_state_keys` and strategy-state repository |
| `state.ownership` | State owner; enum/string | Yes | `strategy`, `engine`, or mapping by key in `transitions` | Prevents strategy mutation of engine state |
| `state.reset` | Reset event; enum/string | Yes | `never`, `session`, `position`, `run`, `delegated` | Session lifecycle and persisted state |
| `state.transitions` | State transition rules; list of strings | Yes | Explicit read/write transitions | `state_updates` returned from pure evaluation |
| `reason_codes` | Outcomes the implementation may return; list of enums | Yes | `signal`, `no_signal`, `insufficient_data`, `stale_data`, `not_eligible` | `StrategySpec.reason_codes` (`ReasonCode`); must include every emitted outcome |
| `parameters` | Configurable parameters; list of mappings | Yes | Each has `name`, `kind`, `required`, `default`, `min`, `max`, `choices`, `description`, `provenance` | `ParameterSpec`, `ParameterSchema.validate`; floats are rejected for decimals |
| `ambiguities` | Unresolved source questions; list of mappings/strings | Yes | Empty for implementation-ready; each non-empty item needs impact and resolution owner | Blocks implementation when execution-critical |
| `implementation_assumptions` | Decisions made to make source deterministic; list of mappings | Yes | Each has `id`, `decision`, `reason`, `impact`, `review_required` | Must be recorded in implementation and research metadata |
| `validation_scenarios` | Deterministic acceptance cases; list of mappings | Yes | Each has `id`, `given`, `expect`; include boundaries, invalid data, state, and duplicate replay | Targeted unit tests and implementation review |
| `identity_links.strategy_spec_id` | Stable display identity/version; string | Yes | `{strategy_id}@{version}` | Research and reports |
| `identity_links.implementation` | Python class path; string | Yes after implementation | Import path plus class name | Review traceability; registry registration |
| `identity_links.registry_key` | Registered `(strategy_id, version)`; two-item list | Yes after implementation | Exact `StrategyRegistry` key | Explicit trusted discovery |
| `identity_links.parameter_configuration_hash` | Hash of normalized parameter mapping; string/null | Yes for a run, null before configuration | Deterministic hash | Backtest experiment identity |
| `identity_links.research_experiment_ids` | Associated run IDs; list of strings | Yes | Empty before research | `BacktestRequest`, manifest, and results artifacts |

Nested mappings use these member contracts. A list item may not omit a
required member merely because the surrounding list is present.

| Nested member | Meaning and type | Required / permitted values |
| :--- | :--- | :--- |
| `data.required_inputs[].name`, `.kind`, `.timeframe`, `.fields`, `.required` | Input identifier, input kind, sampling timeframe, field names, and whether absence blocks evaluation; string, enum, enum, list of strings, boolean | All required; `kind` is one of the values in the parent row; `fields` is non-empty; `required` has no implicit default |
| `indicators[].id`, `.definition`, `.inputs`, `.reset`, `.owner`, `.required` | Stable indicator key, calculation/provision definition, input names, reset boundary, owning component, and required flag | All required; `reset` is `never`, `session`, `position`, or `run`; `owner` is an existing service/component or `strategy` |
| `rules.stop_loss.value`, `rules.trailing_stop.value` | Numeric or structured rule value; decimal values are strings, or null | Required; null only when the matching behavior is `none` or `delegated`; no unapproved numeric default |
| `rules.profit_targets[]` | One target mapping containing an explicit target condition/value and ownership | Optional only as an empty list; every item must define its condition, value, and enforcement owner |
| `parameters[].name`, `.kind`, `.required`, `.default`, `.min`, `.max`, `.choices`, `.description`, `.provenance` | Existing parameter-schema name, type, requirement, default, inclusive bounds, enum choices, purpose, and origin | `name`, `kind`, `required`, `description`, and `provenance` required; `default` is null for required parameters; bounds/choices must agree with `ParameterSpec` |
| `ambiguities[]` | Unresolved question with `id`, `question`, `impact`, `owner`, and optional `resolution` | Required while non-empty; `impact` includes `execution_critical` or `non_critical`; any unresolved execution-critical item keeps `status: draft` |
| `implementation_assumptions[]` | Deterministic decision with `id`, `decision`, `reason`, `impact`, and `review_required` | All required; `impact` identifies the affected rule; assumptions must be visible in manifests |
| `validation_scenarios[]` | Deterministic acceptance case with `id`, `given`, and `expect` | All required; `expect` is an explicit result/reason/state assertion, not a prose goal |

For `parameters`, `kind` is one of the existing `ParameterKind` values:
`int`, `decimal`, `bool`, `enum`, or `time_of_day`. A parameter with
`required: true` has `default: null`; an optional parameter has an explicit
default. Bounds are inclusive. `choices` is required only for `enum`.
Defaults and bounds must be source-defined, explicitly approved assumptions,
or existing implementation values documented as such; they must never be
silently selected by the coding agent.

### 7.4 Evidence classes and ambiguity policy

Every rule must be classified in the working specification as one of four
evidence classes, even though the canonical YAML stores the class-specific
content in its dedicated fields:

* **Source-defined rule** — directly stated by the source. Preserve the
  source wording in `provenance.source_rules`, then formalize its predicate
  in `rules`.
* **Source-demonstrated behavior** — shown by an example, chart, or narrated
  trade but not stated as a general rule. Record it in
  `provenance.demonstrated_behavior`; do not generalize it without review.
* **Implementation assumption** — a deterministic decision needed by the
  existing engine, such as a boundary operator, missing-data response, or
  order timing. Record it in `implementation_assumptions` and in the
  relevant rule field.
* **Unresolved ambiguity** — a missing or conflicting rule. Record it in
  `ambiguities` with execution impact and owner. Any execution-critical item
  makes the document non-ready.

The implementation may preserve the trading idea while making mechanics
deterministic, but it must not silently reinterpret source rules. If the
source says “near VWAP,” “strong volume,” “tight stop,” or similar language,
the agent must ask for a definition or leave the specification in `draft`.

### 7.5 Versioning and experiment identity

These identities are separate:

| Identity | Changes when | Stored where |
| :--- | :--- | :--- |
| Strategy identity | The conceptual strategy is replaced or split into a different rule set | `strategy_id`, registry and reports |
| Strategy specification version | Any formal trading-rule change: trigger, boundary, session, required data, state, stop/target semantics, or a newly approved execution-critical assumption | `StrategySpec.version`, signal IDs, manifests |
| Strategy implementation version | Python behavior or implementation changes, including bug fixes, while the intended rules may remain the same | `identity.implementation_version`, code revision/manifest |
| Parameter configuration | A value changes within the same validated rule contract | `parameters`, preset, normalized parameter hash |
| Backtest experiment identity | Symbol, window, dataset, seed, capital, execution assumptions, parameter mapping, or code/spec revision changes | `BacktestRequest`, run manifest, result ledger |

Use a new strategy specification `MAJOR` version for incompatible rule or
meaning changes, `MINOR` for a backwards-compatible rule addition that is
explicitly part of the strategy contract, and `PATCH` for a clarified rule
that does not change accepted inputs or intended signal meaning. If the
clarification changes a signal boundary or historical result, use at least a
`MINOR` version and create a new implementation version. A parameter change
does not require a new strategy version when the parameter is already part of
the validated schema; it does require a new preset/configuration hash and a
new research experiment identity. A parameter-schema change is a strategy
specification change and requires a new version.

Every research run must preserve the strategy id/version, implementation code
revision, normalized parameter mapping/hash, dataset id, source provenance,
time window, seed, starting capital, and execution assumptions. Reusing a
run id for any changed item is forbidden. This is how an extracted
specification remains associated with immutable backtest manifests and raw
artifacts.

### 7.6 Conforming example: existing `vwap_bounce` 1.0.0

The following is a compact conforming serialization of the shipped
`mike_trade.strategies.buy.vwap_bounce:VwapBounceBuy`. It reflects the code,
not the strategy's descriptive name. Its rule is a completed-bar upward
cross of shared session VWAP values; the module does not calculate VWAP,
does not fetch data, and emits an entry signal only. It declares no strategy
parameters. The existing `vwap_reference` shadow-only pair separately pins
this BUY leg to `vwap_breakdown` SELL 1.0.0 and requires trailing protection;
that pair policy is not hidden inside this BUY strategy.

```yaml
schema_name: mike_trade_strategy_spec
schema_version: "1.0"
status: implementation_ready
identity:
  strategy_id: vwap_bounce
  name: "VWAP bounce"
  version: "1.0.0"
  description: "BUY when a completed regular-session bar crosses above shared session VWAP."
  family: mean_reversion
  category: technical
  sub_category: vwap
  side: buy
  implementation_version: "1.0.0"
provenance:
  source_type: textbook_reference
  source_title: "Session-VWAP bounce reference definition"
  source_locator: "Existing module StrategySpec.source"
  extracted_by: "Repository specification"
  extraction_date: "2026-10-09"
  source_rules:
    - "Prior close is at or below prior session VWAP and latest close is above latest session VWAP."
  demonstrated_behavior: []
markets:
  asset_classes: [us_equity]
  symbols: []
  venues: [iex]
  allowed_modes: [shadow]
  direction: long_only
time:
  timeframes: [1m]
  trigger_timeframe: 1m
  trigger_kind: edge_on_closed_bar
  session: regular
  timezone: America/New_York
  completed_bars_only: true
  warmup_bars: 2
  entry_cutoff: null
data:
  required_inputs:
    - name: candles
      kind: candle
      timeframe: 1m
      fields: [open, high, low, close, volume, status]
      required: true
    - name: vwap
      kind: indicator
      timeframe: 1m
      fields: [value]
      required: true
    - name: vwap_prev
      kind: indicator
      timeframe: 1m
      fields: [value]
      required: true
  freshness_required: true
  missing_data_behavior: no_signal
indicators:
  - id: vwap
    definition: "Latest completed-bar session VWAP from the shared indicator service"
    inputs: [candles]
    reset: session
    owner: market_data_service
    required: true
  - id: vwap_prev
    definition: "Prior completed-bar session VWAP from the shared indicator service"
    inputs: [candles]
    reset: session
    owner: market_data_service
    required: true
rules:
  setup_conditions:
    - "Context freshness is not stale."
    - "Session status is REGULAR."
    - "Engine eligibility equals WATCHING_BUY."
    - "At least two valid CLOSED M1 bars from the latest session are contiguous."
    - "Both evaluated bars have positive volume and both shared VWAP values are finite positive Decimals."
  entry_triggers:
    - "prior.close <= vwap_prev AND latest.close > vwap"
  entry_timing:
    trigger: completed_bar_close
    execution: delegated
    order_type: signal_only
  exit_conditions: []
  stop_loss:
    behavior: none
    value: null
  profit_targets: []
  trailing_stop:
    behavior: none
    value: null
position_sizing:
  method: signal_only
  formula: null
  inputs: []
risk_constraints:
  maximum_positions: null
  maximum_loss: null
  protective_requirements: []
  enforcement_owner: risk_and_execution
reentry:
  policy: next_valid_crossing
  restrictions: [same_completed_bar_suppressed, one_signal_per_completed_bar]
state:
  persistent_keys: [session_id, last_signal_bar]
  ownership: strategy
  reset: session
  transitions:
    - "Read engine-owned eligibility; do not write it."
    - "Write session_id for the current session."
    - "Write last_signal_bar when a signal or same-bar replay is evaluated."
reason_codes: [signal, no_signal, insufficient_data, stale_data, not_eligible]
parameters: []
ambiguities: []
implementation_assumptions:
  - id: vwap_boundary
    decision: "Prior equality is allowed; latest equality is not."
    reason: "Matches the implemented <= and > predicate."
    impact: signal_boundary
    review_required: false
  - id: no_strategy_execution
    decision: "Sizing, stops, targets, and order timing are delegated."
    reason: "The strategy SDK emits StrategyEvaluation only."
    impact: execution
    review_required: false
validation_scenarios:
  - id: upward_cross
    given: "Valid contiguous CLOSED M1 bars; prior.close <= vwap_prev; latest.close > vwap; fresh regular session."
    expect: signal
  - id: prior_equal
    given: "prior.close == vwap_prev and latest.close > vwap."
    expect: signal
  - id: latest_equal
    given: "latest.close == vwap."
    expect: no_signal
  - id: downward_or_no_cross
    given: "The upward predicate is false."
    expect: no_signal
  - id: stale
    given: "context.freshness.stale is true."
    expect: stale_data
  - id: invalid_or_missing
    given: "A bar is developing, invalid, zero-volume, non-contiguous, or a VWAP is missing/not warmed."
    expect: insufficient_data
  - id: same_bar_replay
    given: "state.last_signal_bar equals the latest bar start."
    expect: no_signal_and_no_duplicate
identity_links:
  strategy_spec_id: vwap_bounce@1.0.0
  implementation: mike_trade.strategies.buy.vwap_bounce:VwapBounceBuy
  registry_key: [vwap_bounce, "1.0.0"]
  parameter_configuration_hash: null
  research_experiment_ids: []
```

The example's `family: mean_reversion` is a classification label, not a
claim about profitability. The module's own source text calls this a
textbook/reference definition. The engine's `MarketContext` supplies the
indicator values, the strategy validates the data and state, and the
composition root explicitly registers the class. The existing `vwap_reference`
pair then supplies the production lifecycle's sell leg, position sizing,
minimum hold, entry cutoff, and trailing protection through the existing
shadow-only lifecycle configuration.

The example's `markets.allowed_modes: [shadow]` describes the existing
`vwap_reference` preset boundary. The standalone `VwapBounceBuy` class does
not inspect execution mode; mode enforcement remains the responsibility of
the preset/composition root, as required by the existing architecture.

### 7.7 Coding-agent implementation contract

A coding agent receiving this section and a NotebookLM extraction must:

1. **Collect inputs.** Preserve the source extraction, citations/locations,
   examples, stated rules, unstated demonstrations, and every unresolved
   question. Identify market, timeframe, session, direction, data, indicators,
   setup, trigger, exits, stops, targets, sizing, risk, reentry, and state.
2. **Formalize before coding.** Produce the canonical YAML with all fields in
   §7.2. Translate prose into explicit predicates, boundary operators,
   ordering, missing-data outcomes, session behavior, and state transitions.
   Mark each item as source-defined, demonstrated, assumed, or unresolved.
3. **Handle ambiguity explicitly.** Do not invent a number, indicator,
   timeframe, fill timing, stop, target, or sizing rule. Leave the spec as
   `draft`, record the ambiguity and its execution impact, and obtain a
   resolution. An implementation-critical ambiguity blocks readiness.
4. **Implement within the existing SDK.** Add a pure strategy class using
   `MarketContext`, validated `ParameterSchema` values, and returned
   `StrategyEvaluation`/`state_updates`. Keep broker, filesystem, network,
   clock, sizing, order, and risk logic out of the strategy. Use existing
   candle, session, indicator, reason-code, and deterministic signal-ID types.
5. **Register explicitly.** Register the class in the composition root or
   existing preset registry. Pin `(strategy_id, version)`, validate parameters,
   enforce allowed modes, and use the existing pair preset for BUY/SELL
   composition. Do not add dynamic discovery or a rule interpreter.
6. **Test deterministically.** Add targeted tests for every validation
   scenario, including signal boundaries, equality, stale/missing/invalid
   data, session changes, warmup, duplicate replay, state transitions,
   parameter rejection, and registry/version lookup. Use fixed timestamps,
   Decimal values, and explicit contexts; do not run the full suite merely for
   this contract.
7. **Record artifacts.** Deliver the YAML specification, Python implementation,
   targeted tests, registry/preset changes, implementation version, code
   revision, normalized parameter hash, and any research/backtest manifests.
   Preserve source provenance and assumptions with the artifacts.
8. **Meet success criteria.** The spec is implementation-ready, the class
   passes existing constructor and parameter validation, the registry resolves
   the pinned identity, signal IDs are deterministic, tests cover all
   execution-critical branches, and no existing strategy or mode behavior
   changes without an explicitly versioned specification and review.

Before handoff, the agent should run a focused consistency check: parse the
YAML, verify required keys and enum spellings, compare the declared identity,
timeframe, warmup, state keys, parameters, reason codes, and side to
`StrategySpec`, and confirm that no execution-critical ambiguity remains.
There is currently no repository-wide YAML strategy-schema loader; adding one
is not required for this documentation standard. Runtime validation continues
through the existing Python `StrategySpec`/`ParameterSchema`/`StrategyRegistry`
mechanisms, which remain the compatibility boundary.

## 8. Technical strategy library — first batch (14 BUY, 14 SELL)

Each strategy is one complete Python class (`mike_trade/strategies/buy/` or
`mike_trade/strategies/sell/`) that owns its calculation and its signal
condition, is registered by the existing explicit calls (`build_runtime` in
`mike_trade/app.py` and `_research_registry` in `mike_trade/research/commands.py`),
and runs unchanged through the historical `BacktestRunner` and the SHADOW
engine. The pure Decimal indicator functions they use live in the existing
shared module `mike_trade/strategies/indicators/core.py`; the few shared helpers
(stale/session/eligibility checks, adjacency, crossing comparison, same-bar
replay suppression, resuming recursive indicator state) are plain functions in
`mike_trade/strategies/closed_bar.py`. No
filter, composition, declarative-rule or base-class framework was added.
These are textbook definitions only: nothing here is an alpha or profitability
claim and none of it was backtested for this change.

Conventions that apply to every strategy below (any deviation is stated in the
strategy's own interpretation notes):

- **Timeframe is declared, not assumed.** Each strategy states its trigger and
  required timeframe in its own `StrategySpec`; the shared helpers in
  `closed_bar.py` read `spec.trigger_timeframe` and hold no universal interval.
  All 28 strategies below declare completed **1-minute** bars: 1m is the only
  interval available both to the historical replay (1m, plus 5m derived from 1m)
  and to the SHADOW stream. Periods are bar counts at that interval. A strategy
  declaring another timeframe uses the same helpers unchanged. Separate
  limitation: the SHADOW stream builds only a 1m context, so SHADOW configuration
  rejects any strategy needing other timeframes (`STRATEGY_TIMEFRAME_UNSUPPORTED`,
  for example the existing 5m `rsi_reversal`); the backtest runner runs both.
- **Missing candles.** The two bars of an edge must be adjacent
  (`latest.start == prior.end`, the same gap convention as
  `market_data.warmup` and the existing `rsi_reversal`); inside bar needs three
  consecutive bars. Otherwise the result is `INSUFFICIENT_DATA`: a missing
  interval or the overnight close is never read as one continuous observation,
  nothing is filled in, and a crossing hidden in a gap is not reported later.
  Indicator windows count *observed* completed bars (a bar that never printed is
  neither filled nor counted) and may span sessions like a continuous chart. Level
  rules need no sequence and are unaffected. The policy lives in the strategy, so
  backtest and SHADOW behave identically.
- **Recursive indicators are resumed, not recomputed.** EMA, MACD, RSI, DMI and
  Parabolic SAR depend on every earlier bar through their seed and smoothing, so
  their value must not depend on how many candles a bounded history window
  (500 in SHADOW and the backtest default) happens to hold. Each such strategy
  keeps a JSON-safe snapshot of its calculator in its own persisted state
  (`indicator_state`: calculator state, last processed bar, the outputs on that bar
  and the one before, and a signature of strategy, version and calculation
  parameters; roughly 0.3–0.7 KB). Each evaluation feeds only the bars after the
  last processed one, so the result equals having processed every bar in order,
  across window advances, sessions and process restarts. State is also advanced
  when the context is stale, outside the regular session or (BUY) not eligible, so
  gating never makes it lag. The same bar replayed returns the stored outputs. If
  no usable state exists (first evaluation, changed parameters, a gap the history
  no longer reaches, a corrupt snapshot) the calculator is re-seeded from the
  supplied history using the published seed at its oldest bar; nothing is guessed
  about unseen bars. Published seeds: EMA = SMA of the first n closes
  (StockCharts); MACD signal = SMA seed over its first values; Wilder RSI and DMI
  = simple-sum seed then Wilder smoothing; Parabolic SAR = Wilder's recursion.
  Windowed indicators (SMA, Stochastic, Williams %R, CCI, Donchian, Aroon) are
  exact and stateless.
- **Crossings.** `a` crosses above `b` on bar `t` when `a[t-1] <= b[t-1]` and
  `a[t] > b[t]`; crossing below mirrors it (`>=` then `<`). For a level the
  fixed convention is the same: the prior bar at or on the level, the latest bar
  strictly beyond it. Evaluation is on the latest completed bar only, so no
  future candle is read.
- **Eligibility and reasons.** Stale context → `STALE_DATA`; session not
  `REGULAR` → `NOT_ELIGIBLE`; BUY additionally requires the engine-owned state
  `eligibility == "WATCHING_BUY"` (read only) → `NOT_ELIGIBLE`; fewer completed
  bars than the configured lookback, an undefined indicator value (for example a
  flat range or zero deviation) or non-adjacent bars → `INSUFFICIENT_DATA`;
  otherwise `SIGNAL` or `NO_SIGNAL`. Invalid or inconsistent parameters raise
  `ParameterValidationError` and never signal.
- **Replay suppression.** `last_signal_bar` (ISO start of the bar that fired) is
  the other strategy-owned state key. The same completed bar never emits twice;
  the signal id is the deterministic SHA-256 of strategy, version, symbol, side
  and bar start. Edge rules rearm naturally on the next crossing; level rules
  (`price_below_ema`, `price_below_sma`, `donchian_lower_channel_exit`) signal on
  every completed bar while the condition holds, each with its own id.
- **Warm-up.** `spec.warmup_bars` / `WARMUP_BARS` is the requirement at the
  default parameters (the tables below). `required_bars(params)` returns the
  exact requirement of a configured lookback, and both the backtest runner and
  `SellRule` gate on it, so a valid short configuration (for example SMA 3/5) is
  evaluated as soon as it has history and no configuration signals early.
- **Roles.** BUY strategies emit an entry signal only and add no stop, sizing,
  target or order logic. SELL strategies extend the existing `SellRule` and emit an
  advisory full exit of an owned long; position checks, minimum hold, arbitration
  and the protective stop remain with the existing exit and stop machinery.
- **No added confirmation.** No volume, trend, ADX or other filter is added to any
  rule beyond what the cited definition states.

### Known limitations

- A strategy that is not evaluated for longer than the history window (a BUY
  strategy while a position is open, for instance) cannot catch up from the
  window and re-seeds from it on its next evaluation; recursive values then
  converge to the continuous ones geometrically but are no longer guaranteed
  identical. Removing this needs an engine-level decision (advance every bound
  strategy on every closed bar, or an indicator service with its own persistence).
- SHADOW fetches `market_data.warmup_bars` (default 60) historical bars at
  start-up, fewer than several defaults need (for example 201 for SMA 50/200), so
  those strategies return `INSUFFICIENT_DATA` until enough live bars accrue;
  configure the warm-up to at least the lookback in the tables.
- Each evaluation persists the indicator snapshot and emits the engine's
  existing state-saved event (roughly 0.3–0.7 KB per strategy, symbol and bar).
- The shared `context.indicators` service (`vwap`, `rsi`) is parameterless and
  recomputes over its own bounded window; the existing `rsi_reversal` uses it
  and is unchanged.

### Overview

| Identifier | Name | Role | Sub-category | Trigger | Default parameters |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `sma_crossover` | SMA Crossover | BUY | `moving_average` | edge | fast_period=50, slow_period=200 |
| `ema_crossover` | EMA Crossover | BUY | `moving_average` | edge | fast_period=10, slow_period=50 |
| `macd_signal_crossover` | MACD Signal Crossover | BUY | `momentum` | edge | fast_period=12, slow_period=26, signal_period=9 |
| `macd_zero_line_crossover` | MACD Zero-Line Crossover | BUY | `momentum` | edge | fast_period=12, slow_period=26 |
| `rsi_centerline_crossover` | RSI Centerline Crossover | BUY | `oscillator` | edge | rsi_period=14 |
| `rsi_oversold_reversal` | RSI Oversold Reversal | BUY | `oscillator` | edge | rsi_period=14, oversold=30 |
| `donchian_channel_breakout` | Donchian Channel Breakout | BUY | `breakout` | edge | period=20 |
| `stochastic_crossover` | Stochastic Crossover | BUY | `oscillator` | edge | k_period=14, k_smooth=3, d_period=3 |
| `williams_r_reversion` | Williams %R Reversion | BUY | `oscillator` | edge | period=14, oversold=-80 |
| `cci_zero_line_crossover` | CCI Zero-Line Crossover | BUY | `oscillator` | edge | period=20 |
| `parabolic_sar` | Parabolic SAR | BUY | `trend_following` | edge | step=0.02, maximum=0.20 |
| `dmi_crossover` | DMI Crossover | BUY | `trend_following` | edge | period=14 |
| `aroon_trend` | Aroon Trend | BUY | `trend_following` | edge | period=25 |
| `inside_bar_breakout` | Inside Bar Breakout | BUY | `price_action` | edge | - |
| `macd_bearish_crossover` | MACD Bearish Crossover | SELL | `momentum` | edge | fast_period=12, slow_period=26, signal_period=9 |
| `macd_zero_line_breakdown` | MACD Zero-Line Breakdown | SELL | `momentum` | edge | fast_period=12, slow_period=26 |
| `ema_bearish_crossover` | EMA Bearish Crossover | SELL | `moving_average` | edge | fast_period=10, slow_period=50 |
| `sma_bearish_crossover` | SMA Bearish Crossover | SELL | `moving_average` | edge | fast_period=50, slow_period=200 |
| `price_below_ema` | Price Below EMA | SELL | `moving_average` | level | period=20 |
| `price_below_sma` | Price Below SMA | SELL | `moving_average` | level | period=50 |
| `rsi_centerline_breakdown` | RSI Centerline Breakdown | SELL | `oscillator` | edge | rsi_period=14 |
| `stochastic_bearish_crossover` | Stochastic Bearish Crossover | SELL | `oscillator` | edge | k_period=14, k_smooth=3, d_period=3 |
| `williams_r_reversal` | Williams %R Reversal | SELL | `oscillator` | edge | period=14, overbought=-20 |
| `cci_zero_line_breakdown` | CCI Zero-Line Breakdown | SELL | `oscillator` | edge | period=20 |
| `parabolic_sar_reversal` | Parabolic SAR Reversal | SELL | `trend_following` | edge | step=0.02, maximum=0.20 |
| `dmi_bearish_crossover` | DMI Bearish Crossover | SELL | `trend_following` | edge | period=14 |
| `aroon_bearish_reversal` | Aroon Bearish Reversal | SELL | `trend_following` | edge | period=25 |
| `donchian_lower_channel_exit` | Donchian Lower Channel Exit | SELL | `breakout` | level | period=20 |

### 8.1 `sma_crossover` — SMA Crossover (BUY, 1.0.0)

| Item | Value |
| :--- | :--- |
| Strategy name | SMA Crossover |
| Strategy identifier | `sma_crossover` (version `1.0.0`) |
| Category / sub-category | `technical` / `moving_average` |
| Role | BUY (long entry signal) |
| Trigger | `EDGE_ON_CLOSED_BAR` on the latest completed 1-minute bar |
| Supported candle intervals | 1m only (trigger and required timeframe) |
| Required lookback | `slow_period + 1` completed bars (201 by default) |
| Source / reference | StockCharts ChartSchool, 'Moving Averages - Simple and Exponential' (SMA/EMA formulas; price crossover: bearish when price moves below the average; two-moving-average crossover: bullish when the shorter average crosses above the longer, bearish when it crosses below). |

| Parameter | Type | Default | Allowed range |
| :--- | :--- | :--- | :--- |
| `fast_period` | int | 50 | 2 to 200 |
| `slow_period` | int | 200 | 3 to 400 |

Constraint: `fast_period < slow_period` (otherwise the evaluation fails closed with `ParameterValidationError`).

Indicator calculation:

```text
SMA(n)[t] = (close[t-n+1] + ... + close[t]) / n
```

Exact signal condition:

```text
SMA(fast)[t-1] <= SMA(slow)[t-1]  AND  SMA(fast)[t] > SMA(slow)[t]   ->  BUY
```

Interpretation decisions: The averages are the conventional 50/200 SMA (golden cross) applied to 1-minute closes.

### 8.2 `ema_crossover` — EMA Crossover (BUY, 1.0.0)

| Item | Value |
| :--- | :--- |
| Strategy name | EMA Crossover |
| Strategy identifier | `ema_crossover` (version `1.0.0`) |
| Category / sub-category | `technical` / `moving_average` |
| Role | BUY (long entry signal) |
| Trigger | `EDGE_ON_CLOSED_BAR` on the latest completed 1-minute bar |
| Supported candle intervals | 1m only (trigger and required timeframe) |
| Required lookback | `slow_period + 1` completed bars (51 by default); the whole supplied history seeds the EMA |
| Source / reference | StockCharts ChartSchool, 'Moving Averages - Simple and Exponential' (SMA/EMA formulas; price crossover: bearish when price moves below the average; two-moving-average crossover: bullish when the shorter average crosses above the longer, bearish when it crosses below). |

| Parameter | Type | Default | Allowed range |
| :--- | :--- | :--- | :--- |
| `fast_period` | int | 10 | 2 to 200 |
| `slow_period` | int | 50 | 3 to 400 |

Constraint: `fast_period < slow_period` (otherwise the evaluation fails closed with `ParameterValidationError`).

Indicator calculation:

```text
EMA(n)[t] = (2 / (n + 1)) * close[t] + (1 - 2 / (n + 1)) * EMA(n)[t-1]
EMA(n) is seeded with the SMA of the first n closes (StockCharts convention).
```

Exact signal condition:

```text
EMA(fast)[t-1] <= EMA(slow)[t-1]  AND  EMA(fast)[t] > EMA(slow)[t]   ->  BUY
```

Interpretation decisions: The published page gives 10/50 (and 5/35, 50/200) as example EMA pairs and no single default; 10/50 is used. With fast=12 and slow=26 this equals the MACD zero-line crossover. The EMA is seeded at the start of the supplied history, so early values are seed-dominated (a published convention, not a defect).

### 8.3 `macd_signal_crossover` — MACD Signal Crossover (BUY, 1.0.0)

| Item | Value |
| :--- | :--- |
| Strategy name | MACD Signal Crossover |
| Strategy identifier | `macd_signal_crossover` (version `1.0.0`) |
| Category / sub-category | `technical` / `momentum` |
| Role | BUY (long entry signal) |
| Trigger | `EDGE_ON_CLOSED_BAR` on the latest completed 1-minute bar |
| Supported candle intervals | 1m only (trigger and required timeframe) |
| Required lookback | `slow_period + signal_period` completed bars (35 by default) |
| Source / reference | Gerald Appel's MACD as documented in StockCharts ChartSchool, 'MACD (Moving Average Convergence/Divergence Oscillator)' (MACD = 12-EMA - 26-EMA, signal = 9-EMA of MACD; signal-line and centerline crossovers). |

| Parameter | Type | Default | Allowed range |
| :--- | :--- | :--- | :--- |
| `fast_period` | int | 12 | 2 to 100 |
| `slow_period` | int | 26 | 3 to 200 |
| `signal_period` | int | 9 | 2 to 100 |

Constraint: `fast_period < slow_period` (otherwise the evaluation fails closed with `ParameterValidationError`).

Indicator calculation:

```text
MACD[t]   = EMA(fast)[t] - EMA(slow)[t]            (closes; EMAs SMA-seeded)
Signal[t] = EMA(signal) of MACD, SMA-seeded over its first `signal` values
```

Exact signal condition:

```text
MACD[t-1] <= Signal[t-1]  AND  MACD[t] > Signal[t]   ->  BUY   (bullish crossover)
```

Interpretation decisions: StockCharts does not state the EMA seeding; the SMA seed shared with the EMA indicator is used for all three EMAs.

### 8.4 `macd_zero_line_crossover` — MACD Zero-Line Crossover (BUY, 1.0.0)

| Item | Value |
| :--- | :--- |
| Strategy name | MACD Zero-Line Crossover |
| Strategy identifier | `macd_zero_line_crossover` (version `1.0.0`) |
| Category / sub-category | `technical` / `momentum` |
| Role | BUY (long entry signal) |
| Trigger | `EDGE_ON_CLOSED_BAR` on the latest completed 1-minute bar |
| Supported candle intervals | 1m only (trigger and required timeframe) |
| Required lookback | `slow_period + 1` completed bars (27 by default) |
| Source / reference | Gerald Appel's MACD as documented in StockCharts ChartSchool, 'MACD (Moving Average Convergence/Divergence Oscillator)' (MACD = 12-EMA - 26-EMA, signal = 9-EMA of MACD; signal-line and centerline crossovers). |

| Parameter | Type | Default | Allowed range |
| :--- | :--- | :--- | :--- |
| `fast_period` | int | 12 | 2 to 100 |
| `slow_period` | int | 26 | 3 to 200 |

Constraint: `fast_period < slow_period` (otherwise the evaluation fails closed with `ParameterValidationError`).

Indicator calculation:

```text
MACD[t] = EMA(fast)[t] - EMA(slow)[t]            (closes; EMAs SMA-seeded)
```

Exact signal condition:

```text
MACD[t-1] <= 0  AND  MACD[t] > 0   ->  BUY   (bullish centerline crossover)
```

Interpretation decisions: The signal line is not used. With the default 12/26 this is the same event as an EMA(12)/EMA(26) crossover; both are kept as separately published strategies.

### 8.5 `rsi_centerline_crossover` — RSI Centerline Crossover (BUY, 1.0.0)

| Item | Value |
| :--- | :--- |
| Strategy name | RSI Centerline Crossover |
| Strategy identifier | `rsi_centerline_crossover` (version `1.0.0`) |
| Category / sub-category | `technical` / `oscillator` |
| Role | BUY (long entry signal) |
| Trigger | `EDGE_ON_CLOSED_BAR` on the latest completed 1-minute bar |
| Supported candle intervals | 1m only (trigger and required timeframe) |
| Required lookback | `rsi_period + 2` completed bars (16 by default) |
| Source / reference | J. Welles Wilder, 'New Concepts in Technical Trading Systems' (1978) as documented in StockCharts ChartSchool, 'Relative Strength Index (RSI)' (14-period Wilder smoothing; 70/30 overbought/oversold; 50 centerline). |

| Parameter | Type | Default | Allowed range |
| :--- | :--- | :--- | :--- |
| `rsi_period` | int | 14 | 2 to 100 |

Indicator calculation:

```text
Wilder RSI = 100 - 100 / (1 + avg gain / avg loss); first averages are simple means
of the first n changes, then avg = (previous avg * (n - 1) + current) / n.
```

Exact signal condition:

```text
RSI[t-1] <= 50  AND  RSI[t] > 50   ->  BUY   (centerline = 50)
```

Interpretation decisions: StockCharts names RSI 'centerline crossovers' as signals without stating the level in text; 50 is the oscillator's midpoint and is fixed (not a parameter).

### 8.6 `rsi_oversold_reversal` — RSI Oversold Reversal (BUY, 1.0.0)

| Item | Value |
| :--- | :--- |
| Strategy name | RSI Oversold Reversal |
| Strategy identifier | `rsi_oversold_reversal` (version `1.0.0`) |
| Category / sub-category | `technical` / `oscillator` |
| Role | BUY (long entry signal) |
| Trigger | `EDGE_ON_CLOSED_BAR` on the latest completed 1-minute bar |
| Supported candle intervals | 1m only (trigger and required timeframe) |
| Required lookback | `rsi_period + 2` completed bars (16 by default) |
| Source / reference | J. Welles Wilder, 'New Concepts in Technical Trading Systems' (1978) as documented in StockCharts ChartSchool, 'Relative Strength Index (RSI)' (14-period Wilder smoothing; 70/30 overbought/oversold; 50 centerline). |

| Parameter | Type | Default | Allowed range |
| :--- | :--- | :--- | :--- |
| `rsi_period` | int | 14 | 2 to 100 |
| `oversold` | decimal | 30 | 1 to 50 |

Indicator calculation:

```text
Wilder RSI as in the RSI Centerline Crossover strategy.
```

Exact signal condition:

```text
RSI[t-1] <= oversold  AND  RSI[t] > oversold   ->  BUY   (default oversold = 30)
```

Interpretation decisions: Wilder defines readings below 30 as oversold; the simple re-cross above the level is used as the reversal trigger (mirror of the existing rsi_reversal SELL). Wilder's failure-swing pattern is a different, multi-step rule and is not implemented.

### 8.7 `donchian_channel_breakout` — Donchian Channel Breakout (BUY, 1.0.0)

| Item | Value |
| :--- | :--- |
| Strategy name | Donchian Channel Breakout |
| Strategy identifier | `donchian_channel_breakout` (version `1.0.0`) |
| Category / sub-category | `technical` / `breakout` |
| Role | BUY (long entry signal) |
| Trigger | `EDGE_ON_CLOSED_BAR` on the latest completed 1-minute bar |
| Supported candle intervals | 1m only (trigger and required timeframe) |
| Required lookback | `period + 2` completed bars (22 by default) |
| Source / reference | Richard Donchian's price channel as documented in StockCharts ChartSchool, 'Price Channels' (also called Donchian Channels; 20 default; the channel uses prices prior to the current period). |

| Parameter | Type | Default | Allowed range |
| :--- | :--- | :--- | :--- |
| `period` | int | 20 | 2 to 400 |

Indicator calculation:

```text
Upper[t] = highest high of bars t-n .. t-1   (the current bar is excluded)
```

Exact signal condition:

```text
close[t-1] <= Upper[t-1]  AND  close[t] > Upper[t]   ->  BUY
```

Interpretation decisions: The signal uses the completed bar's close breaking the channel built from the prior n bars (StockCharts: channels use prices prior to the current period); an intraday-touch variant is not used. Unlike momentum_breakout there is no volume confirmation.

### 8.8 `stochastic_crossover` — Stochastic Crossover (BUY, 1.0.0)

| Item | Value |
| :--- | :--- |
| Strategy name | Stochastic Crossover |
| Strategy identifier | `stochastic_crossover` (version `1.0.0`) |
| Category / sub-category | `technical` / `oscillator` |
| Role | BUY (long entry signal) |
| Trigger | `EDGE_ON_CLOSED_BAR` on the latest completed 1-minute bar |
| Supported candle intervals | 1m only (trigger and required timeframe) |
| Required lookback | `k_period + k_smooth + d_period - 1` completed bars (19 by default) |
| Source / reference | George Lane's Stochastic Oscillator as documented in StockCharts ChartSchool, 'Stochastic Oscillator' (Full Stochastic 14,3,3; %K/%D signal-line cross). |

| Parameter | Type | Default | Allowed range |
| :--- | :--- | :--- | :--- |
| `k_period` | int | 14 | 2 to 100 |
| `k_smooth` | int | 3 | 1 to 20 |
| `d_period` | int | 3 | 1 to 20 |

Indicator calculation:

```text
raw %K = 100 * (close - lowest low(n)) / (highest high(n) - lowest low(n))
%K = SMA(raw %K, k_smooth)        %D = SMA(%K, d_period)     (Full Stochastic 14,3,3)
```

Exact signal condition:

```text
%K[t-1] <= %D[t-1]  AND  %K[t] > %D[t]   ->  BUY   (bullish signal-line cross)
```

Interpretation decisions: No overbought/oversold qualification is applied (StockCharts defines the cross itself as the signal). A flat range (highest high == lowest low) leaves %K undefined and suppresses the signal.

### 8.9 `williams_r_reversion` — Williams %R Reversion (BUY, 1.0.0)

| Item | Value |
| :--- | :--- |
| Strategy name | Williams %R Reversion |
| Strategy identifier | `williams_r_reversion` (version `1.0.0`) |
| Category / sub-category | `technical` / `oscillator` |
| Role | BUY (long entry signal) |
| Trigger | `EDGE_ON_CLOSED_BAR` on the latest completed 1-minute bar |
| Supported candle intervals | 1m only (trigger and required timeframe) |
| Required lookback | `period + 1` completed bars (15 by default) |
| Source / reference | Larry Williams' %R as documented in StockCharts ChartSchool, 'Williams %R' (formula, 14 default, -20/-80 levels); the -80/-20 re-cross trigger follows the cTrader and TrendSpider indicator documentation (sources differ on the trigger). |

| Parameter | Type | Default | Allowed range |
| :--- | :--- | :--- | :--- |
| `period` | int | 14 | 2 to 100 |
| `oversold` | decimal | -80 | -100 to -50 |

Indicator calculation:

```text
%R = -100 * (highest high(n) - close) / (highest high(n) - lowest low(n))
```

Exact signal condition:

```text
%R[t-1] <= oversold  AND  %R[t] > oversold   ->  BUY   (default oversold = -80)
```

Interpretation decisions: StockCharts also describes a cross above -50 after an oversold reading, which needs an unspecified look-back; the single-step -80 re-cross documented by cTrader/TrendSpider is used. A flat range leaves %R undefined and suppresses the signal.

### 8.10 `cci_zero_line_crossover` — CCI Zero-Line Crossover (BUY, 1.0.0)

| Item | Value |
| :--- | :--- |
| Strategy name | CCI Zero-Line Crossover |
| Strategy identifier | `cci_zero_line_crossover` (version `1.0.0`) |
| Category / sub-category | `technical` / `oscillator` |
| Role | BUY (long entry signal) |
| Trigger | `EDGE_ON_CLOSED_BAR` on the latest completed 1-minute bar |
| Supported candle intervals | 1m only (trigger and required timeframe) |
| Required lookback | `period + 1` completed bars (21 by default) |
| Source / reference | Donald Lambert's Commodity Channel Index as documented in StockCharts ChartSchool, 'Commodity Channel Index (CCI)' (20 periods, 0.015 constant; CCI positive favors bulls, negative favors bears). |

| Parameter | Type | Default | Allowed range |
| :--- | :--- | :--- | :--- |
| `period` | int | 20 | 2 to 200 |

Indicator calculation:

```text
TP = (high + low + close) / 3
CCI = (TP - SMA(TP, n)) / (0.015 * mean absolute deviation of TP from SMA(TP, n))
```

Exact signal condition:

```text
CCI[t-1] <= 0  AND  CCI[t] > 0   ->  BUY
```

Interpretation decisions: StockCharts notes zero-line crossovers produce many whipsaws; no +100/-100 requirement is added. A zero mean deviation (flat typical price) leaves CCI undefined and suppresses the signal.

### 8.11 `parabolic_sar` — Parabolic SAR (BUY, 1.0.0)

| Item | Value |
| :--- | :--- |
| Strategy name | Parabolic SAR |
| Strategy identifier | `parabolic_sar` (version `1.0.0`) |
| Category / sub-category | `technical` / `trend_following` |
| Role | BUY (long entry signal) |
| Trigger | `EDGE_ON_CLOSED_BAR` on the latest completed 1-minute bar |
| Supported candle intervals | 1m only (trigger and required timeframe) |
| Required lookback | 3 completed bars minimum; the whole supplied history defines the SAR path |
| Source / reference | J. Welles Wilder, 'New Concepts in Technical Trading Systems' (1978) as documented in StockCharts ChartSchool, 'Parabolic SAR' (AF 0.02 step, 0.20 maximum; prior-two-bar clamp; reversal when price penetrates SAR). |

| Parameter | Type | Default | Allowed range |
| :--- | :--- | :--- | :--- |
| `step` | decimal | 0.02 | 0.001 to 0.5 |
| `maximum` | decimal | 0.20 | 0.001 to 1 |

Constraint: `maximum >= step` (otherwise the evaluation fails closed with `ParameterValidationError`).

Indicator calculation:

```text
SAR[t] = SAR[t-1] + AF * (EP - SAR[t-1]);  AF starts at `step`, +step on each new EP,
capped at `maximum`.  Uptrend: SAR never above the prior two lows (downtrend: never
below the prior two highs).  Reversal when low < SAR (uptrend) or high > SAR
(downtrend): new SAR = prior EP, new EP = the penetrating bar's extreme, AF = step.
```

Exact signal condition:

```text
trend[t-1] = down  AND  trend[t] = up   ->  BUY   (SAR flips below price)
```

Interpretation decisions: StockCharts and Wilder's text leave the starting trend open; the first trend is up if the second close >= the first (SAR = first low, EP = first high) else down. The SAR path depends on the supplied history, so it is evaluated over the whole history and only the latest flip is used. Penetration is strict (low < SAR / high > SAR).

### 8.12 `dmi_crossover` — DMI Crossover (BUY, 1.0.0)

| Item | Value |
| :--- | :--- |
| Strategy name | DMI Crossover |
| Strategy identifier | `dmi_crossover` (version `1.0.0`) |
| Category / sub-category | `technical` / `trend_following` |
| Role | BUY (long entry signal) |
| Trigger | `EDGE_ON_CLOSED_BAR` on the latest completed 1-minute bar |
| Supported candle intervals | 1m only (trigger and required timeframe) |
| Required lookback | `period + 2` completed bars (16 by default); the whole supplied history smooths the values |
| Source / reference | J. Welles Wilder, 'New Concepts in Technical Trading Systems' (1978) as documented in StockCharts ChartSchool, 'Average Directional Index (ADX)' (14-period Wilder smoothing; +DI/-DI crossover buy and sell signals). |

| Parameter | Type | Default | Allowed range |
| :--- | :--- | :--- | :--- |
| `period` | int | 14 | 2 to 100 |

Indicator calculation:

```text
TR, +DM, -DM from the second bar; first smoothed value = sum of the first n raw values,
then smoothed = previous - previous / n + current.
+DI = 100 * smoothed +DM / smoothed TR      -DI = 100 * smoothed -DM / smoothed TR
```

Exact signal condition:

```text
+DI[t-1] <= -DI[t-1]  AND  +DI[t] > -DI[t]   ->  BUY
```

Interpretation decisions: Wilder/StockCharts also require ADX above 25 (many traders use 20) and place the initial stop at the signal bar's low. Neither is added here: the strategy is the plain +DI/-DI crossover and stops belong to the stop policy.

### 8.13 `aroon_trend` — Aroon Trend (BUY, 1.0.0)

| Item | Value |
| :--- | :--- |
| Strategy name | Aroon Trend |
| Strategy identifier | `aroon_trend` (version `1.0.0`) |
| Category / sub-category | `technical` / `trend_following` |
| Role | BUY (long entry signal) |
| Trigger | `EDGE_ON_CLOSED_BAR` on the latest completed 1-minute bar |
| Supported candle intervals | 1m only (trigger and required timeframe) |
| Required lookback | `period + 2` completed bars (27 by default) |
| Source / reference | Tushar Chande's Aroon (1995) as documented in StockCharts ChartSchool, 'Aroon' (25 periods; Aroon-Up crossing above Aroon-Down is the first stage of an uptrend, the reverse of a downtrend). |

| Parameter | Type | Default | Allowed range |
| :--- | :--- | :--- | :--- |
| `period` | int | 25 | 2 to 200 |

Indicator calculation:

```text
Aroon-Up   = 100 * (n - bars since highest high) / n
Aroon-Down = 100 * (n - bars since lowest low)  / n
(extreme searched over the latest n + 1 bars; ties resolve to the most recent bar)
```

Exact signal condition:

```text
Up[t-1] <= Down[t-1]  AND  Up[t] > Down[t]   ->  BUY   (an uptrend emerges)
```

Interpretation decisions: StockCharts does not define the search window or ties; the Chande/TA-Lib convention (n + 1 bars, most recent tie) is used. The 50/70/100 strength levels are not required.

### 8.14 `inside_bar_breakout` — Inside Bar Breakout (BUY, 1.0.0)

| Item | Value |
| :--- | :--- |
| Strategy name | Inside Bar Breakout |
| Strategy identifier | `inside_bar_breakout` (version `1.0.0`) |
| Category / sub-category | `technical` / `price_action` |
| Role | BUY (long entry signal) |
| Trigger | `EDGE_ON_CLOSED_BAR` on the latest completed 1-minute bar |
| Supported candle intervals | 1m only (trigger and required timeframe) |
| Required lookback | 3 completed bars |
| Source / reference | Inside-bar breakout as described in published trading documentation (TradingView 'Inside Bar Breakout' scripts and LuxAlgo 'Inside Bar Breakouts'): an inside bar has a lower high and higher low than the mother bar; a long breaks above the mother bar's high. Weakly standardized: no single authoritative rule exists. |

Parameters: none.

Indicator calculation:

```text
mother = bar[t-2], inside = bar[t-1], breakout = bar[t]
inside bar: inside.high < mother.high AND inside.low > mother.low
```

Exact signal condition:

```text
inside bar at t-1  AND  close[t] > mother.high   ->  BUY
```

Interpretation decisions: Only the first bar after the inside bar can break out (no multi-bar inside sequences), the breakout is a completed close above the mother bar's high (not an intraday touch), and the three bars must be consecutive completed bars (no missing bar between them). The 'body-only' inside-bar variant is not used.

### 8.15 `macd_bearish_crossover` — MACD Bearish Crossover (SELL, 1.0.0)

| Item | Value |
| :--- | :--- |
| Strategy name | MACD Bearish Crossover |
| Strategy identifier | `macd_bearish_crossover` (version `1.0.0`) |
| Category / sub-category | `technical` / `momentum` |
| Role | SELL (advisory full exit of an owned long) |
| Trigger | `EDGE_ON_CLOSED_BAR` on the latest completed 1-minute bar |
| Supported candle intervals | 1m only (trigger and required timeframe) |
| Required lookback | `slow_period + signal_period` completed bars (35 by default) |
| Source / reference | Gerald Appel's MACD as documented in StockCharts ChartSchool, 'MACD (Moving Average Convergence/Divergence Oscillator)' (MACD = 12-EMA - 26-EMA, signal = 9-EMA of MACD; signal-line and centerline crossovers). |

| Parameter | Type | Default | Allowed range |
| :--- | :--- | :--- | :--- |
| `fast_period` | int | 12 | 2 to 100 |
| `slow_period` | int | 26 | 3 to 200 |
| `signal_period` | int | 9 | 2 to 100 |

Constraint: `fast_period < slow_period` (otherwise the evaluation fails closed with `ParameterValidationError`).

Indicator calculation:

```text
MACD[t]   = EMA(fast)[t] - EMA(slow)[t]            (closes; EMAs SMA-seeded)
Signal[t] = EMA(signal) of MACD, SMA-seeded over its first `signal` values
```

Exact signal condition:

```text
MACD[t-1] >= Signal[t-1]  AND  MACD[t] < Signal[t]   ->  SELL   (bearish crossover)
```

Interpretation decisions: StockCharts does not state the EMA seeding; the SMA seed shared with the EMA indicator is used for all three EMAs.

### 8.16 `macd_zero_line_breakdown` — MACD Zero-Line Breakdown (SELL, 1.0.0)

| Item | Value |
| :--- | :--- |
| Strategy name | MACD Zero-Line Breakdown |
| Strategy identifier | `macd_zero_line_breakdown` (version `1.0.0`) |
| Category / sub-category | `technical` / `momentum` |
| Role | SELL (advisory full exit of an owned long) |
| Trigger | `EDGE_ON_CLOSED_BAR` on the latest completed 1-minute bar |
| Supported candle intervals | 1m only (trigger and required timeframe) |
| Required lookback | `slow_period + 1` completed bars (27 by default) |
| Source / reference | Gerald Appel's MACD as documented in StockCharts ChartSchool, 'MACD (Moving Average Convergence/Divergence Oscillator)' (MACD = 12-EMA - 26-EMA, signal = 9-EMA of MACD; signal-line and centerline crossovers). |

| Parameter | Type | Default | Allowed range |
| :--- | :--- | :--- | :--- |
| `fast_period` | int | 12 | 2 to 100 |
| `slow_period` | int | 26 | 3 to 200 |

Constraint: `fast_period < slow_period` (otherwise the evaluation fails closed with `ParameterValidationError`).

Indicator calculation:

```text
MACD[t] = EMA(fast)[t] - EMA(slow)[t]            (closes; EMAs SMA-seeded)
```

Exact signal condition:

```text
MACD[t-1] >= 0  AND  MACD[t] < 0   ->  SELL   (bearish centerline crossover)
```

Interpretation decisions: The signal line is not used. With the default 12/26 this is the same event as an EMA(12)/EMA(26) bearish crossover; both are kept as separately published strategies.

### 8.17 `ema_bearish_crossover` — EMA Bearish Crossover (SELL, 1.0.0)

| Item | Value |
| :--- | :--- |
| Strategy name | EMA Bearish Crossover |
| Strategy identifier | `ema_bearish_crossover` (version `1.0.0`) |
| Category / sub-category | `technical` / `moving_average` |
| Role | SELL (advisory full exit of an owned long) |
| Trigger | `EDGE_ON_CLOSED_BAR` on the latest completed 1-minute bar |
| Supported candle intervals | 1m only (trigger and required timeframe) |
| Required lookback | `slow_period + 1` completed bars (51 by default); the whole supplied history seeds the EMA |
| Source / reference | StockCharts ChartSchool, 'Moving Averages - Simple and Exponential' (SMA/EMA formulas; price crossover: bearish when price moves below the average; two-moving-average crossover: bullish when the shorter average crosses above the longer, bearish when it crosses below). |

| Parameter | Type | Default | Allowed range |
| :--- | :--- | :--- | :--- |
| `fast_period` | int | 10 | 2 to 200 |
| `slow_period` | int | 50 | 3 to 400 |

Constraint: `fast_period < slow_period` (otherwise the evaluation fails closed with `ParameterValidationError`).

Indicator calculation:

```text
EMA(n)[t] = (2 / (n + 1)) * close[t] + (1 - 2 / (n + 1)) * EMA(n)[t-1]
EMA(n) is seeded with the SMA of the first n closes (StockCharts convention).
```

Exact signal condition:

```text
EMA(fast)[t-1] >= EMA(slow)[t-1]  AND  EMA(fast)[t] < EMA(slow)[t]   ->  SELL
```

Interpretation decisions: The published page gives 10/50 (and 5/35, 50/200) as example EMA pairs and no single default; 10/50 is used. The EMA is seeded at the start of the supplied history, so early values are seed-dominated.

### 8.18 `sma_bearish_crossover` — SMA Bearish Crossover (SELL, 1.0.0)

| Item | Value |
| :--- | :--- |
| Strategy name | SMA Bearish Crossover |
| Strategy identifier | `sma_bearish_crossover` (version `1.0.0`) |
| Category / sub-category | `technical` / `moving_average` |
| Role | SELL (advisory full exit of an owned long) |
| Trigger | `EDGE_ON_CLOSED_BAR` on the latest completed 1-minute bar |
| Supported candle intervals | 1m only (trigger and required timeframe) |
| Required lookback | `slow_period + 1` completed bars (201 by default) |
| Source / reference | StockCharts ChartSchool, 'Moving Averages - Simple and Exponential' (SMA/EMA formulas; price crossover: bearish when price moves below the average; two-moving-average crossover: bullish when the shorter average crosses above the longer, bearish when it crosses below). |

| Parameter | Type | Default | Allowed range |
| :--- | :--- | :--- | :--- |
| `fast_period` | int | 50 | 2 to 200 |
| `slow_period` | int | 200 | 3 to 400 |

Constraint: `fast_period < slow_period` (otherwise the evaluation fails closed with `ParameterValidationError`).

Indicator calculation:

```text
SMA(n)[t] = (close[t-n+1] + ... + close[t]) / n
```

Exact signal condition:

```text
SMA(fast)[t-1] >= SMA(slow)[t-1]  AND  SMA(fast)[t] < SMA(slow)[t]   ->  SELL
```

Interpretation decisions: The averages are the conventional 50/200 SMA (death cross) applied to 1-minute closes.

### 8.19 `price_below_ema` — Price Below EMA (SELL, 1.0.0)

| Item | Value |
| :--- | :--- |
| Strategy name | Price Below EMA |
| Strategy identifier | `price_below_ema` (version `1.0.0`) |
| Category / sub-category | `technical` / `moving_average` |
| Role | SELL (advisory full exit of an owned long) |
| Trigger | `LEVEL_ON_CLOSED_BAR` on the latest completed 1-minute bar |
| Supported candle intervals | 1m only (trigger and required timeframe) |
| Required lookback | `period` completed bars (20 by default); the whole supplied history seeds the EMA |
| Source / reference | StockCharts ChartSchool, 'Moving Averages - Simple and Exponential' (SMA/EMA formulas; price crossover: bearish when price moves below the average; two-moving-average crossover: bullish when the shorter average crosses above the longer, bearish when it crosses below). |

| Parameter | Type | Default | Allowed range |
| :--- | :--- | :--- | :--- |
| `period` | int | 20 | 2 to 400 |

Indicator calculation:

```text
EMA(n)[t] = (2 / (n + 1)) * close[t] + (1 - 2 / (n + 1)) * EMA(n)[t-1]  (SMA-seeded)
```

Exact signal condition:

```text
close[t] < EMA(n)[t]   ->  SELL   (level condition, not an edge)
```

Interpretation decisions: A level (state) rule, so it signals on every completed bar that closes below the EMA while a position is held; a deterministic per-bar signal id keeps replays idempotent. StockCharts describes price moving below an MA as the bearish signal but gives no single period; 20 is used.

### 8.20 `price_below_sma` — Price Below SMA (SELL, 1.0.0)

| Item | Value |
| :--- | :--- |
| Strategy name | Price Below SMA |
| Strategy identifier | `price_below_sma` (version `1.0.0`) |
| Category / sub-category | `technical` / `moving_average` |
| Role | SELL (advisory full exit of an owned long) |
| Trigger | `LEVEL_ON_CLOSED_BAR` on the latest completed 1-minute bar |
| Supported candle intervals | 1m only (trigger and required timeframe) |
| Required lookback | `period` completed bars (50 by default) |
| Source / reference | StockCharts ChartSchool, 'Moving Averages - Simple and Exponential' (SMA/EMA formulas; price crossover: bearish when price moves below the average; two-moving-average crossover: bullish when the shorter average crosses above the longer, bearish when it crosses below). |

| Parameter | Type | Default | Allowed range |
| :--- | :--- | :--- | :--- |
| `period` | int | 50 | 2 to 400 |

Indicator calculation:

```text
SMA(n)[t] = (close[t-n+1] + ... + close[t]) / n
```

Exact signal condition:

```text
close[t] < SMA(n)[t]   ->  SELL   (level condition, not an edge)
```

Interpretation decisions: A level (state) rule, so it signals on every completed bar that closes below the SMA while a position is held; a deterministic per-bar signal id keeps replays idempotent. StockCharts describes price moving below an MA as the bearish signal but gives no single period; 50 (its medium-term example) is used.

### 8.21 `rsi_centerline_breakdown` — RSI Centerline Breakdown (SELL, 1.0.0)

| Item | Value |
| :--- | :--- |
| Strategy name | RSI Centerline Breakdown |
| Strategy identifier | `rsi_centerline_breakdown` (version `1.0.0`) |
| Category / sub-category | `technical` / `oscillator` |
| Role | SELL (advisory full exit of an owned long) |
| Trigger | `EDGE_ON_CLOSED_BAR` on the latest completed 1-minute bar |
| Supported candle intervals | 1m only (trigger and required timeframe) |
| Required lookback | `rsi_period + 2` completed bars (16 by default) |
| Source / reference | J. Welles Wilder, 'New Concepts in Technical Trading Systems' (1978) as documented in StockCharts ChartSchool, 'Relative Strength Index (RSI)' (14-period Wilder smoothing; 70/30 overbought/oversold; 50 centerline). |

| Parameter | Type | Default | Allowed range |
| :--- | :--- | :--- | :--- |
| `rsi_period` | int | 14 | 2 to 100 |

Indicator calculation:

```text
Wilder RSI = 100 - 100 / (1 + avg gain / avg loss); first averages are simple means
of the first n changes, then avg = (previous avg * (n - 1) + current) / n.
```

Exact signal condition:

```text
RSI[t-1] >= 50  AND  RSI[t] < 50   ->  SELL   (centerline = 50)
```

Interpretation decisions: StockCharts names RSI 'centerline crossovers' as signals without stating the level in text; 50 is the oscillator's midpoint and is fixed (not a parameter).

### 8.22 `stochastic_bearish_crossover` — Stochastic Bearish Crossover (SELL, 1.0.0)

| Item | Value |
| :--- | :--- |
| Strategy name | Stochastic Bearish Crossover |
| Strategy identifier | `stochastic_bearish_crossover` (version `1.0.0`) |
| Category / sub-category | `technical` / `oscillator` |
| Role | SELL (advisory full exit of an owned long) |
| Trigger | `EDGE_ON_CLOSED_BAR` on the latest completed 1-minute bar |
| Supported candle intervals | 1m only (trigger and required timeframe) |
| Required lookback | `k_period + k_smooth + d_period - 1` completed bars (19 by default) |
| Source / reference | George Lane's Stochastic Oscillator as documented in StockCharts ChartSchool, 'Stochastic Oscillator' (Full Stochastic 14,3,3; %K/%D signal-line cross). |

| Parameter | Type | Default | Allowed range |
| :--- | :--- | :--- | :--- |
| `k_period` | int | 14 | 2 to 100 |
| `k_smooth` | int | 3 | 1 to 20 |
| `d_period` | int | 3 | 1 to 20 |

Indicator calculation:

```text
raw %K = 100 * (close - lowest low(n)) / (highest high(n) - lowest low(n))
%K = SMA(raw %K, k_smooth)        %D = SMA(%K, d_period)     (Full Stochastic 14,3,3)
```

Exact signal condition:

```text
%K[t-1] >= %D[t-1]  AND  %K[t] < %D[t]   ->  SELL   (bearish signal-line cross)
```

Interpretation decisions: No overbought/oversold qualification is applied (StockCharts defines the cross itself as the signal). A flat range leaves %K undefined and suppresses the signal.

### 8.23 `williams_r_reversal` — Williams %R Reversal (SELL, 1.0.0)

| Item | Value |
| :--- | :--- |
| Strategy name | Williams %R Reversal |
| Strategy identifier | `williams_r_reversal` (version `1.0.0`) |
| Category / sub-category | `technical` / `oscillator` |
| Role | SELL (advisory full exit of an owned long) |
| Trigger | `EDGE_ON_CLOSED_BAR` on the latest completed 1-minute bar |
| Supported candle intervals | 1m only (trigger and required timeframe) |
| Required lookback | `period + 1` completed bars (15 by default) |
| Source / reference | Larry Williams' %R as documented in StockCharts ChartSchool, 'Williams %R' (formula, 14 default, -20/-80 levels); the -80/-20 re-cross trigger follows the cTrader and TrendSpider indicator documentation (sources differ on the trigger). |

| Parameter | Type | Default | Allowed range |
| :--- | :--- | :--- | :--- |
| `period` | int | 14 | 2 to 100 |
| `overbought` | decimal | -20 | -50 to 0 |

Indicator calculation:

```text
%R = -100 * (highest high(n) - close) / (highest high(n) - lowest low(n))
```

Exact signal condition:

```text
%R[t-1] >= overbought  AND  %R[t] < overbought   ->  SELL   (default overbought = -20)
```

Interpretation decisions: Sources differ on the sell trigger; the cross back below -20 after an overbought reading (cTrader/TrendSpider) is used. StockCharts' cross below -50 after an overbought reading needs an unspecified look-back and is not used. A flat range leaves %R undefined and suppresses the signal.

### 8.24 `cci_zero_line_breakdown` — CCI Zero-Line Breakdown (SELL, 1.0.0)

| Item | Value |
| :--- | :--- |
| Strategy name | CCI Zero-Line Breakdown |
| Strategy identifier | `cci_zero_line_breakdown` (version `1.0.0`) |
| Category / sub-category | `technical` / `oscillator` |
| Role | SELL (advisory full exit of an owned long) |
| Trigger | `EDGE_ON_CLOSED_BAR` on the latest completed 1-minute bar |
| Supported candle intervals | 1m only (trigger and required timeframe) |
| Required lookback | `period + 1` completed bars (21 by default) |
| Source / reference | Donald Lambert's Commodity Channel Index as documented in StockCharts ChartSchool, 'Commodity Channel Index (CCI)' (20 periods, 0.015 constant; CCI positive favors bulls, negative favors bears). |

| Parameter | Type | Default | Allowed range |
| :--- | :--- | :--- | :--- |
| `period` | int | 20 | 2 to 200 |

Indicator calculation:

```text
TP = (high + low + close) / 3
CCI = (TP - SMA(TP, n)) / (0.015 * mean absolute deviation of TP from SMA(TP, n))
```

Exact signal condition:

```text
CCI[t-1] >= 0  AND  CCI[t] < 0   ->  SELL
```

Interpretation decisions: StockCharts notes zero-line crossovers produce many whipsaws; no +100/-100 requirement is added. A zero mean deviation leaves CCI undefined and suppresses the signal.

### 8.25 `parabolic_sar_reversal` — Parabolic SAR Reversal (SELL, 1.0.0)

| Item | Value |
| :--- | :--- |
| Strategy name | Parabolic SAR Reversal |
| Strategy identifier | `parabolic_sar_reversal` (version `1.0.0`) |
| Category / sub-category | `technical` / `trend_following` |
| Role | SELL (advisory full exit of an owned long) |
| Trigger | `EDGE_ON_CLOSED_BAR` on the latest completed 1-minute bar |
| Supported candle intervals | 1m only (trigger and required timeframe) |
| Required lookback | 3 completed bars minimum; the whole supplied history defines the SAR path |
| Source / reference | J. Welles Wilder, 'New Concepts in Technical Trading Systems' (1978) as documented in StockCharts ChartSchool, 'Parabolic SAR' (AF 0.02 step, 0.20 maximum; prior-two-bar clamp; reversal when price penetrates SAR). |

| Parameter | Type | Default | Allowed range |
| :--- | :--- | :--- | :--- |
| `step` | decimal | 0.02 | 0.001 to 0.5 |
| `maximum` | decimal | 0.20 | 0.001 to 1 |

Constraint: `maximum >= step` (otherwise the evaluation fails closed with `ParameterValidationError`).

Indicator calculation:

```text
SAR[t] = SAR[t-1] + AF * (EP - SAR[t-1]);  AF starts at `step`, +step on each new EP,
capped at `maximum`.  Uptrend: SAR never above the prior two lows (downtrend: never
below the prior two highs).  Reversal when low < SAR (uptrend) or high > SAR
(downtrend): new SAR = prior EP, new EP = the penetrating bar's extreme, AF = step.
```

Exact signal condition:

```text
trend[t-1] = up  AND  trend[t] = down   ->  SELL   (SAR flips above price)
```

Interpretation decisions: StockCharts and Wilder's text leave the starting trend open; the first trend is up if the second close >= the first (SAR = first low, EP = first high) else down. The SAR path depends on the supplied history, so it is evaluated over the whole history and only the latest flip is used. Penetration is strict (low < SAR / high > SAR).

### 8.26 `dmi_bearish_crossover` — DMI Bearish Crossover (SELL, 1.0.0)

| Item | Value |
| :--- | :--- |
| Strategy name | DMI Bearish Crossover |
| Strategy identifier | `dmi_bearish_crossover` (version `1.0.0`) |
| Category / sub-category | `technical` / `trend_following` |
| Role | SELL (advisory full exit of an owned long) |
| Trigger | `EDGE_ON_CLOSED_BAR` on the latest completed 1-minute bar |
| Supported candle intervals | 1m only (trigger and required timeframe) |
| Required lookback | `period + 2` completed bars (16 by default); the whole supplied history smooths the values |
| Source / reference | J. Welles Wilder, 'New Concepts in Technical Trading Systems' (1978) as documented in StockCharts ChartSchool, 'Average Directional Index (ADX)' (14-period Wilder smoothing; +DI/-DI crossover buy and sell signals). |

| Parameter | Type | Default | Allowed range |
| :--- | :--- | :--- | :--- |
| `period` | int | 14 | 2 to 100 |

Indicator calculation:

```text
TR, +DM, -DM from the second bar; first smoothed value = sum of the first n raw values,
then smoothed = previous - previous / n + current.
+DI = 100 * smoothed +DM / smoothed TR      -DI = 100 * smoothed -DM / smoothed TR
```

Exact signal condition:

```text
-DI[t-1] <= +DI[t-1]  AND  -DI[t] > +DI[t]   ->  SELL   (-DI crosses above +DI)
```

Interpretation decisions: Wilder/StockCharts also require ADX above 25 (many traders use 20) and place the initial stop at the signal bar's high. Neither is added here: the strategy is the plain -DI/+DI crossover and stops belong to the stop policy.

### 8.27 `aroon_bearish_reversal` — Aroon Bearish Reversal (SELL, 1.0.0)

| Item | Value |
| :--- | :--- |
| Strategy name | Aroon Bearish Reversal |
| Strategy identifier | `aroon_bearish_reversal` (version `1.0.0`) |
| Category / sub-category | `technical` / `trend_following` |
| Role | SELL (advisory full exit of an owned long) |
| Trigger | `EDGE_ON_CLOSED_BAR` on the latest completed 1-minute bar |
| Supported candle intervals | 1m only (trigger and required timeframe) |
| Required lookback | `period + 2` completed bars (27 by default) |
| Source / reference | Tushar Chande's Aroon (1995) as documented in StockCharts ChartSchool, 'Aroon' (25 periods; Aroon-Up crossing above Aroon-Down is the first stage of an uptrend, the reverse of a downtrend). |

| Parameter | Type | Default | Allowed range |
| :--- | :--- | :--- | :--- |
| `period` | int | 25 | 2 to 200 |

Indicator calculation:

```text
Aroon-Up   = 100 * (n - bars since highest high) / n
Aroon-Down = 100 * (n - bars since lowest low)  / n
(extreme searched over the latest n + 1 bars; ties resolve to the most recent bar)
```

Exact signal condition:

```text
Down[t-1] <= Up[t-1]  AND  Down[t] > Up[t]   ->  SELL   (a downtrend emerges)
```

Interpretation decisions: StockCharts does not define the search window or ties; the Chande/TA-Lib convention (n + 1 bars, most recent tie) is used. The 50/70/100 strength levels are not required.

### 8.28 `donchian_lower_channel_exit` — Donchian Lower Channel Exit (SELL, 1.0.0)

| Item | Value |
| :--- | :--- |
| Strategy name | Donchian Lower Channel Exit |
| Strategy identifier | `donchian_lower_channel_exit` (version `1.0.0`) |
| Category / sub-category | `technical` / `breakout` |
| Role | SELL (advisory full exit of an owned long) |
| Trigger | `LEVEL_ON_CLOSED_BAR` on the latest completed 1-minute bar |
| Supported candle intervals | 1m only (trigger and required timeframe) |
| Required lookback | `period + 1` completed bars (21 by default) |
| Source / reference | Richard Donchian's price channel as documented in StockCharts ChartSchool, 'Price Channels' (also called Donchian Channels; 20 default; the channel uses prices prior to the current period). |

| Parameter | Type | Default | Allowed range |
| :--- | :--- | :--- | :--- |
| `period` | int | 20 | 2 to 400 |

Indicator calculation:

```text
Lower[t] = lowest low of bars t-n .. t-1   (the current bar is excluded)
```

Exact signal condition:

```text
close[t] < Lower[t]   ->  SELL   (level condition, not an edge)
```

Interpretation decisions: An exit rule evaluated as a level: a completed close below the prior-n-bar low exits an owned long. The channel length defaults to the published 20; shorter exit channels (for example the Turtle System 1 exit of 10 bars) are available through `period`. The close, not an intraday touch, is compared.

### Sources

- StockCharts ChartSchool: [Moving Averages — Simple and Exponential](https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-overlays/moving-averages-simple-and-exponential),
  [MACD](https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/macd-moving-average-convergence-divergence-oscillator),
  [RSI](https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/relative-strength-index-rsi),
  [Stochastic Oscillator](https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/stochastic-oscillator-fast-slow-and-full),
  [Williams %R](https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/williams-r),
  [CCI](https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/commodity-channel-index-cci),
  [Parabolic SAR](https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-overlays/parabolic-sar),
  [ADX / DMI](https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/average-directional-index-adx),
  [Aroon](https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/aroon),
  [Price Channels (Donchian)](https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-overlays/price-channels),
  [Bollinger Bands](https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-overlays/bollinger-bands).
- Original publications named by those pages: J. Welles Wilder, *New Concepts in
  Technical Trading Systems* (1978) for RSI, Parabolic SAR and DMI/ADX; Gerald
  Appel (MACD); George Lane (Stochastic); Larry Williams (%R); Donald Lambert
  (CCI, 1980); Tushar Chande (Aroon, 1995); Richard Donchian (price channels).
- Weaker, vendor/community documentation (used only where StockCharts gives no
  trigger): cTrader and TrendSpider indicator documentation for the Williams %R
  −80/−20 re-cross; TradingView community scripts and LuxAlgo, "Inside Bar
  Breakouts" for the inside-bar rule. These are flagged in the individual
  interpretation notes.


## 9. Implementation inventory of candidate methods

The inventory is documentation only; it is not a runtime configuration
mechanism and nothing reads it. Status values:

- `implemented` — a faithful strategy class exists and is registered.
- `existing_equivalent` — an already shipped strategy covers the method.
- `requires_source_definition` — no sufficiently precise published rule could be
  established; deliberately not implemented rather than invented.
- `pending` — not yet attempted.

### 9.1 First batch (30 requested methods)

| # | Candidate | Role | Status | Identifier / note |
| :--- | :--- | :--- | :--- | :--- |
| 1 | SMA Crossover | BUY | implemented | `sma_crossover` |
| 2 | EMA Crossover | BUY | implemented | `ema_crossover` |
| 3 | MACD Signal Crossover | BUY | implemented | `macd_signal_crossover` |
| 4 | MACD Zero-Line Crossover | BUY | implemented | `macd_zero_line_crossover` |
| 5 | RSI Centerline Crossover | BUY | implemented | `rsi_centerline_crossover` |
| 6 | RSI Oversold Reversal | BUY | implemented | `rsi_oversold_reversal` |
| 7 | Bollinger Band Mean Reversion | BUY | requires_source_definition | See §9.2 |
| 8 | Donchian Channel Breakout | BUY | implemented | `donchian_channel_breakout` (distinct from `momentum_breakout`: no volume filter, channel excludes the current bar) |
| 9 | Stochastic Crossover | BUY | implemented | `stochastic_crossover` |
| 10 | Williams %R Reversion | BUY | implemented | `williams_r_reversion` |
| 11 | CCI Zero-Line Crossover | BUY | implemented | `cci_zero_line_crossover` |
| 12 | Parabolic SAR | BUY | implemented | `parabolic_sar` |
| 13 | DMI Crossover | BUY | implemented | `dmi_crossover` |
| 14 | Aroon Trend | BUY | implemented | `aroon_trend` |
| 15 | Inside Bar Breakout | BUY | implemented | `inside_bar_breakout` |
| 1 | MACD Bearish Crossover | SELL | implemented | `macd_bearish_crossover` |
| 2 | MACD Zero-Line Breakdown | SELL | implemented | `macd_zero_line_breakdown` |
| 3 | EMA Bearish Crossover | SELL | implemented | `ema_bearish_crossover` |
| 4 | SMA Bearish Crossover | SELL | implemented | `sma_bearish_crossover` |
| 5 | Price Below EMA | SELL | implemented | `price_below_ema` (level rule) |
| 6 | Price Below SMA | SELL | implemented | `price_below_sma` (level rule) |
| 7 | RSI Centerline Breakdown | SELL | implemented | `rsi_centerline_breakdown` |
| 8 | Stochastic Bearish Crossover | SELL | implemented | `stochastic_bearish_crossover` |
| 9 | Williams %R Reversal | SELL | implemented | `williams_r_reversal` |
| 10 | CCI Zero-Line Breakdown | SELL | implemented | `cci_zero_line_breakdown` |
| 11 | Parabolic SAR Reversal | SELL | implemented | `parabolic_sar_reversal` |
| 12 | DMI Bearish Crossover | SELL | implemented | `dmi_bearish_crossover` |
| 13 | Aroon Bearish Reversal | SELL | implemented | `aroon_bearish_reversal` |
| 14 | Donchian Lower Channel Exit | SELL | implemented | `donchian_lower_channel_exit` (level rule) |
| 15 | Bollinger Middle Band Exit | SELL | requires_source_definition | See §9.2 |

First-batch totals: 28 `implemented` (14 BUY, 14 SELL), 2
`requires_source_definition`.

### 9.2 Methods that need a source definition

- **Bollinger Band Mean Reversion (BUY).** The band formulas are published
  (20-period SMA ± 2 standard deviations of closes; StockCharts does not say
  whether the deviation is the population or sample form), but StockCharts states
  that band "tags" are not signals, gives no mean-reversion entry rule, and says
  the bands should be combined with other indicators, which would be an added
  confirmation condition. Vendor and community implementations disagree on the
  trigger (enter on a close below the lower band, on the re-cross back above it,
  or after a W-bottom with confirmation). A canonical entry trigger and the
  deviation form must be chosen from a cited source before implementation.
- **Bollinger Middle Band Exit (SELL).** Published usage splits into opposite
  rules: take profit when price returns *up* to the middle band after a
  mean-reversion entry, versus exit when price closes *down through* the middle
  band that acts as trend support. The right rule depends on which entry it is
  paired with and on the source chosen for the previous item.

### 9.3 Existing strategies (classification only)

| Strategy | Role | `category` / `sub_category` |
| :--- | :--- | :--- |
| `vwap_bounce` | BUY | `technical` / `vwap` |
| `momentum_breakout` | BUY | `technical` / `momentum` |
| `opening_range_breakout` | BUY | `technical` / `breakout` |
| `vwap_breakdown` | SELL | `technical` / `vwap` |
| `momentum_reversal` | SELL | `technical` / `momentum` |
| `rsi_reversal` | SELL | `technical` / `oscillator` |

These six are mapped to inventory candidates as `existing_equivalent` only when
the matching candidate name is recorded. The complete candidate list and current
status are in [TECHNICAL_STRATEGY_INVENTORY.md](TECHNICAL_STRATEGY_INVENTORY.md).

### 9.4 Remaining candidates

The original inventory is 106 BUY and 50 SELL candidate methods. See
[TECHNICAL_STRATEGY_INVENTORY.md](TECHNICAL_STRATEGY_INVENTORY.md) for every
candidate and the current status counts. This section records the first batch's
methods only.
## 10. Finite-window strategy additions (6 BUY, 4 SELL)

These methods use completed 1-minute candles, declared through each strategy's own
`StrategySpec` (the section 8 conventions apply unchanged). Their indicators are exact
finite-window calculations over the latest completed candles, so they keep no recursive
indicator state. Period parameters are bounded at 50. `StrategySpec.warmup_bars` is the
requirement at the default period and `required_bars(params)` gives the exact requirement of
the configured period (the "history" column is that requirement), so short configurations are
evaluated as soon as they have enough candles and none signals earlier. Rolling periods count
observed completed candles; a missing candle is never filled. Every edge compares the two
adjacent completed candles (`latest.start == prior.end`); a missing interval gives
`INSUFFICIENT_DATA`. The SELL classes emit only advisory exits and continue through the
existing `SellRule` position/eligibility contract. With 34 earlier strategies the registered
library holds 23 BUY and 21 SELL.

| Strategy ID | Role / category | Signal rule and calculation | Default; history | Source and interpretation |
|---|---|---|---|---|
| `bollinger_band_breakout` | BUY / breakout | Close crosses above the current upper band; upper = SMA(close, n) + deviations × population standard deviation(close, n). | n=20, deviations=2; n+1 bars for prior/latest comparison. | John Bollinger's official description confirms population standard deviation; StockCharts documents the completed-close band-break interpretation. No squeeze filter. |
| `cci_reversion` | BUY / mean_reversion | CCI crosses upward through -100. CCI uses typical price, rolling SMA, mean absolute deviation, and Lambert's 0.015 constant. | n=20; n+1 bars. | Donald Lambert; StockCharts CCI. A zero mean deviation is undefined. |
| `stochastic_oversold_reversal` | BUY / oscillator | Fast %K crosses upward through 20; %K = 100 × (close - n-bar low) / (n-bar high - n-bar low). | n=14; n+1 bars. | George Lane; StockCharts Stochastic. No %K/%D confirmation is added; flat windows are undefined. |
| `money_flow_index` | BUY / volume | MFI crosses upward through 20. MFI uses n typical-price × volume flows, signed by typical-price direction. | n=14; n+2 bars for both adjacent MFI readings. | StockCharts MFI. Equal typical prices add to neither positive nor negative flow; zero total signed flow is undefined. |
| `chaikin_money_flow` | BUY / volume | Rolling CMF crosses above zero; sum(money-flow multiplier × volume) / sum(volume), where multiplier = ((close-low) - (high-close)) / (high-low). | n=20; n+1 bars. | Marc Chaikin; StockCharts CMF. A zero-range candle contributes multiplier zero; a zero-volume window is undefined. |
| `engulfing_candle_reversal` | BUY / price_action | Bearish real body followed by bullish real body that fully contains it, including equality at body boundaries. | Two adjacent bars. | StockCharts bullish engulfing pattern. Shadows are excluded, and no broad downtrend filter or next-bar confirmation is added. |
| `cci_bearish_reversal` | SELL / oscillator | CCI crosses downward through +100, using the same CCI definition as the BUY CCI strategy. | n=20; n+1 bars. | Donald Lambert; StockCharts CCI. Advisory full exit only. |
| `money_flow_index_reversal` | SELL / volume | MFI crosses downward through 80, using the same finite rolling calculation as the BUY MFI strategy. | n=14; n+2 bars. | StockCharts MFI. Advisory full exit only. |
| `chaikin_money_flow_reversal` | SELL / volume | Rolling CMF crosses below zero, using the same finite rolling calculation as the BUY CMF strategy. | n=20; n+1 bars. | Marc Chaikin; StockCharts CMF. Advisory full exit only. |
| `bearish_engulfing_exit` | SELL / price_action | Bullish real body followed by bearish real body that fully contains it. Bars must be adjacent; a gap suppresses the pattern. | Two adjacent bars. | StockCharts bearish engulfing pattern. Advisory full exit only; no separate trend filter is added. |

For the complete 156-candidate implementation status, see
[`TECHNICAL_STRATEGY_INVENTORY.md`](TECHNICAL_STRATEGY_INVENTORY.md).

## 11. Technical strategy batch 2 (9 BUY, 3 SELL)

The following rules use completed M1 bars and retain only the shared last-signal-bar
state. Every configurable lookback implements `required_bars(params)`; the declared
warmup is the requirement at its default. Rolling indicators use observed bars
without filling missing minutes. Signal edges require adjacent completed bars;
NR7 requires its entire setup to be consecutive, and the five-bar swing pivot
requires consecutive candles around the pivot. SELL signals remain advisory
through `SellRule` and the existing exit arbitration. The registered library now
contains 32 BUY and 24 SELL methods.

| Strategy ID | Role | Exact signal; default lookback | Published interpretation |
|---|---|---|---|
| `rate_of_change_momentum` | BUY | ROC(close,12) crosses above zero; 14 bars | StockCharts ROC centerline crossover. |
| `moving_average_envelope` | BUY | Close crosses upper SMA(20) × 1.025; 21 bars | StockCharts percentage moving-average envelope; SMA center. |
| `moving_average_reclaim` | BUY | Close crosses above SMA(20); 21 bars | StockCharts price-to-moving-average crossover. |
| `triple_moving_average` | BUY | SMA(5) first exceeds both SMA(10) and SMA(20); 21 bars | StockCharts triple moving-average crossover; configured periods must increase. |
| `hull_moving_average` | BUY | HMA(20) turns from flat/falling to rising; 25 bars | Alan Hull turning-point method; nearest-integer half and square-root lengths. |
| `linear_regression_trend` | BUY | 20-bar least-squares close slope crosses above zero; 21 bars | StockCharts Slope bullish centerline crossing. |
| `nr7_breakout` | BUY | Prior bar is strictly narrowest of 7 and next close exceeds its high; 8 bars | Toby Crabel NR7, adapted to M1 bars. Parameter `period=4` covers NR4. |
| `awesome_oscillator` | BUY | SMA(5, median price) − SMA(34, median price) crosses above zero; 35 bars | Bill Williams AO zero-line buy as documented by TradingView. |
| `keltner_channel_breakout` | BUY | Close crosses above SMA(10, typical price) + SMA(10, high−low); 11 bars | Chester Keltner's original finite channel, not the later EMA/ATR variant. |
| `awesome_oscillator_reversal` | SELL | AO crosses below zero; 35 bars | Bill Williams AO zero-line sell as documented by TradingView. |
| `keltner_channel_breakdown` | SELL | Close crosses below original Keltner lower channel; 11 bars | Chester Keltner original SMA/range channel. |
| `previous_swing_low_break` | SELL | Close breaks latest confirmed five-bar fractal low within 20 prior bars; 21 bars | Bill Williams fractal level break; two right-hand bars must already be closed. |

The complete source links, parameter ranges, and interpretation limits are in
the individual `StrategySpec` declarations. These strategies provide signals;
they do not select order size, submit orders, or change protective stops.

## 12. Technical SELL strategy batch 3 (8 SELL)

These advisory long exits use completed regular-session M1 bars and the existing
`SellRule` contract. Edges require adjacent completed bars; structure and opening-range
rules additionally require every candle used to define the pattern or range. Recursive
ADX and Stochastic RSI calculations use persisted snapshot/restore state rather than a
moving history seed. The integrated library now contains 54 BUY and 32 SELL methods.

| Strategy ID | Exact exit condition | Default parameters; history | Source interpretation |
|---|---|---|---|
| `adx_trend_weakening` | Wilder ADX crosses from at-or-above to below 20. | period=14, threshold=20; 29 bars | Wilder ADX; loss of trend strength, independent of direction. |
| `stochastic_rsi_reversal` | Raw StochRSI crosses down through 80. | RSI=14, stochastic=14; 29 bars | Chande/Kroll StochRSI; flat RSI ranges are undefined. |
| `ultimate_oscillator_reversal` | Williams 4:2:1 Ultimate Oscillator crosses down through 70. | 7/14/28, threshold=70; 30 bars | Larry Williams; no divergence confirmation is added. |
| `lower_low_confirmation` | The pivot confirmed two bars ago is below the preceding confirmed five-bar pivot low. | lookback=30; 31 bars | Dow lower-low structure using Williams fractal pivots; no future data. |
| `trendline_breakdown` | Close crosses below the rising line through the two latest confirmed higher pivot lows. | lookback=30; 31 bars | Classical rising-support trendline with objective Williams pivots. |
| `failed_breakout_exit` | Prior close exceeds the preceding n-bar high, then the next close falls back below that level. | period=20; 22 bars | Failed-breakout pattern with a Donchian-style prior-period level. |
| `opening_range_breakdown` | Two post-range closes cross below the low of all opening M1 bars from 09:30 New York. | range=30 minutes; 32 bars | Crabel opening-range method adapted strictly as a long exit. |
| `bollinger_upper_band_reversal` | A close above the upper band is followed by a close back inside it. | period=20, deviations=2; 21 bars | Bollinger population-SD bands; a tag alone is not a signal. |

Chandelier Exit and ATR Trailing Exit remain protective-stop responsibilities.
Supertrend and cumulative OBV/Accumulation-Distribution methods remain deferred because
their recursive/cumulative continuity is not available to an independent strategy module;
Ichimoku remains deferred pending explicit displaced-span point-in-time semantics. Support
Breakdown is already covered by `donchian_lower_channel_exit` and is not duplicated.

## 13. Technical strategy batch 3 (16 BUY)

All methods are long-entry, edge-triggered on completed M1 bars, with the section 8 and 11
conventions unchanged: adjacent latest/prior bars (a missing minute gives `INSUFFICIENT_DATA`),
regular session, BUY eligibility, `required_bars(params)` as the exact parameter-specific
requirement and `spec.warmup_bars` as its value at the defaults. Recursive indicators (EMA,
Wilder ATR/ADX, KAMA, RSI, MACD, Supertrend) are resumed from the strategy's own persisted
`indicator_state` via the shared `advance()` mechanism, so their values do not depend on the
candle window; the calculators added for them live in `indicators/extended.py` (every
calculator has `snapshot()`/`restore()`), and the finite-window helpers in
`indicators/windows.py`. Neither module changes the shared indicators. The integrated library
now contains 54 BUY and 32 SELL methods.

| Strategy ID | Category | Exact signal; default bars | Source and interpretation |
|---|---|---|---|
| `adx_trend_following` | trend_following | ADX(14) crosses above 25 and +DI > -DI; 29 | Wilder / StockCharts ADX. First ADX = mean of first n DX; a flat window has no DI and DX = 0. |
| `supertrend` | trend_following | Supertrend turns from down to up (close above the final upper band of hl2 + 3 × ATR10); 11 | Olivier Seban; TradingView `ta.supertrend`. First ATR bar starts down (convention). |
| `ichimoku_cloud_breakout` | trend_following | Close crosses above max(Span A, Span B) as plotted at the bar; 79 | Hosoda / StockCharts: spans from the 9/26/52 windows ending 26 bars earlier, so no later bar is read. Fixed settings; Chikou unused. |
| `atr_expansion_breakout` | volatility | close − prior close > 2 × Wilder ATR14 of the prior bar; 15 | Wilder / Kaufman volatility-breakout idea, M1 close-to-close adaptation. |
| `bollinger_squeeze` | volatility | Close crosses above the upper band (20, 2σ) with BandWidth at its 125-bar low on some bar of the previous 5; 149 | Bollinger's Squeeze. The 125-bar look-back is this strategy's parameter. |
| `volatility_squeeze_breakout` | breakout | Bollinger(20,2) inside Keltner(SMA20 ± 1.5 × SMA TR) on the prior bar, not on this bar, momentum > 0; 39 | John Carter's TTM Squeeze (StockCharts); momentum = linear-regression value of close − mean(mid-range, SMA). |
| `ema_pullback` | moving_average | EMA13 > EMA26, EMA26 rising, low ≤ EMA13, close > EMA26, previous low > previous EMA13; 27 | Elder's value zone. |
| `moving_average_ribbon` | moving_average | min(EMA 3,5,8,10,12,15) crosses above max(EMA 30,35,40,45,50,60); 61 | Guppy Multiple Moving Average. |
| `adaptive_moving_average` | moving_average | Close crosses above KAMA(10,2,30); 11 | Kaufman; SMA seed per StockCharts. |
| `keltner_channel_reversion` | mean_reversion | Prior close < lower band and close > lower band (EMA20 − 2 × ATR10); 21 | Raschke EMA/ATR Keltner; fades a lower-band excursion. |
| `connors_rsi_mean_reversion` | mean_reversion | ConnorsRSI(3,2,100) crosses below 10; 103 | Connors / StockCharts. PercentRank uses the previous 100 changes. No trend filter. |
| `elder_impulse_system` | momentum | EMA13 and MACD(12,26,9) histogram both rise after a bar where they did not; 36 | Elder's Impulse System (the MACD-histogram momentum candidate). |
| `ease_of_movement` | volume | SMA14 of Arms' EMV crosses above zero; 16 | Arms / StockCharts. Zero range → 0; zero volume → `INSUFFICIENT_DATA`. |
| `hammer_reversal` | price_action | Hammer (lower shadow ≥ 2 × body, upper ≤ 10% of range) after a 5-bar decline, then a close above its high; 8 | Nison, shadow limits as TA-Lib; confirmation is mandatory by design. |
| `higher_high_higher_low` | price_action | Bar that confirms a swing low above the previous one while the last two swing highs rise (2-bar fractals, 50-bar window); 51 | Dow theory / Williams fractals; entry is `strength` bars after the low. |
| `resistance_breakout` | price_action | Close crosses above the latest confirmed swing high of the prior 50 bars; 51 | Edwards & Magee / Murphy; most recent pivot, not the highest. |

Deferred candidates and the reason for each are recorded in
[`TECHNICAL_STRATEGY_INVENTORY.md`](TECHNICAL_STRATEGY_INVENTORY.md). Operationally,
SHADOW's `market_data.warmup_bars` (60 in the example configuration) fetches only that many
bars at start-up, so the 79-, 103- and 149-bar strategies stay `INSUFFICIENT_DATA` until live
bars accumulate, unless the setting is raised to cover them.

## 14. Current-session BUY strategies (6 BUY)

These methods operate only on completed regular-session M1 bars. Before using a
session reference they require an exact contiguous series beginning at the NYSE
open returned by the exchange calendar. A missing opening bar, any missing minute,
or a partial rolling window yields `INSUFFICIENT_DATA`; no oldest-available candle
is treated as a session boundary. New York wall-clock parameters are converted via
the exchange timezone, so the same rules retain their 09:30–16:00 semantics across
DST. Early closes naturally suppress windows that never open.

| Strategy ID | Exact BUY rule | Defaults; warmup | Published source |
|---|---|---|---|
| `opening_drive` | Signal exactly at the end of the opening window when every bar is bullish, closes strictly rise, and no bar after the first trades below the session open. | 5-minute drive; 5 bars. | Mike Bellafiore, *The PlayBook*; Swoopr, “Opening Drive in Trading Explained.” |
| `opening_range_retest` | After the first completed close above the opening-range high, signal on the first later bar that trades at/below that level and closes above it. A first retest closing at/below the level invalidates the setup. | 15-minute range; 17 bars minimum. | Ninjacators, *The Opening Range Strategy Guide*, breakout-and-retest method. |
| `high_of_day_momentum` | During the entry window, the latest completed close must exceed every high observed earlier in the current session. | 10:00–15:30 ET; 31 bars. | Warrior Trading, *Day Trading Guide*, High of Day Momentum setup. |
| `midday_range_breakout` | Consecutive completed closes cross above the frozen high of the complete lunch range. | Range 12:00–13:00, entries before 15:00 ET; 211 bars. | TradingView open-source “Lunch Range - Retracement Hunter.” |
| `afternoon_breakout` | During the afternoon window, consecutive completed closes cross above the frozen high of the first regular-session hour. | First 60 minutes; entries 13:00–15:00 ET; 211 bars. | Alan S. Farley, *The Master Swing Trader*, first-hour/mid-afternoon breakout setup. |
| `power_hour_momentum` | If the last pre-window close is above the session open, consecutive completed closes cross above the frozen pre-window high-of-day. | Window begins 15:00 ET; 331 bars. | Edgeful, “Power Hour Breakout + Continuation.” |

All six are entry signals only. They do not size positions, submit orders, set
stops or targets, use developing candles, or inspect prices after the triggering
bar. `last_signal_bar` suppresses replay of the same completed bar.

Gap, previous-close, previous-day-high, and previous-day-low candidates remain
deferred. The bounded 500-bar context and default 60-bar live warmup do not expose
confirmed previous-session OHLC fields and may retain pre-market or only partial
prior-session observations. Those references cannot be reconstructed safely from
the oldest available candle, so no gap is inferred and no previous-day extreme is
calculated.

## 15. Technical strategy batch 4 (15 BUY)

Same conventions as sections 11 and 13: long entries, edge-triggered on completed M1 bars,
adjacent prior/latest bars, regular session, BUY eligibility, `required_bars(params)` exact and
`spec.warmup_bars` its value at the defaults. Recursive state (cumulative volume lines, TSI, StochRSI,
Wilder RSI/ADX, EMA) is persisted under `indicator_state` and resumed with `advance()`; the
calculators are in `indicators/cumulative.py` and `indicators/oscillators.py`, the finite-window
helpers in `indicators/patterns.py`. Swing-based patterns use the confirmed fractal pivots of
`indicators/windows.py` computed only from bars before the signal bar, require every bar from the
first pivot to the signal bar to be consecutive, and fire only on the first close beyond their
trigger line. Percentage thresholds of the chart patterns are explicit parameters with intraday
defaults because Bulkowski/Murphy give daily-chart or qualitative guidelines.

| Strategy ID | Exact signal; default bars | Source and interpretation |
|---|---|---|
| `obv_breakout` | OBV exceeds its previous 20 values (prior bar did not); 22 | Granville / StockCharts: OBV breakouts confirm or lead price. The n-bar-high form is this implementation's reading. |
| `accumulation_distribution_breakout` | Chaikin A/D line exceeds its previous 20 values; 22 | Chaikin / StockCharts ADL; breakout form by analogy with OBV. |
| `volume_price_trend_breakout` | VPT (cumulative volume × % change) exceeds its previous 20 values; 22 | CFI Volume Price Trend; breakout form by analogy with OBV. |
| `tsi_momentum` | TSI(25,13) crosses above 0; 39 | Blau / StockCharts TSI centerline cross; SMA-seeded EMAs. |
| `stochastic_rsi` | Raw StochRSI(14,14) crosses above 0.20; 29 | Chande & Kroll; Fidelity/StockCharts. Flat RSI window undefined. |
| `ultimate_oscillator` | Lower price swing low + higher UO(7,14,28) low below 30, then UO exceeds the highest UO between the lows (first time after the second low); 78 | Williams' bullish-divergence buy rule (StockCharts). Fractal swing lows (2 bars each side); default 30 is a parameter. |
| `holy_grail_pullback` | ADX(14) > 30 and rising, +DI > −DI, setup bar low ≤ EMA(20), next bar closes above the setup bar's high; 30 | Raschke & Connors, Street Smarts. Sources differ on EMA/SMA and stop vs close entry; rare on M1 at defaults. |
| `rsi2_trend_pullback` | RSI(2) crosses below 10 and close > SMA(200); 200 | Connors & Alvarez RSI(2) pullback; exits not included. |
| `z_score_mean_reversion` | z of the close over 20 bars crosses below −2; 21 | Chan / ArbitrageLab Bollinger z-score entry; population SD. |
| `double_bottom_reversal` | Last two swing lows within 0.5%, interim peak ≥ 1% above them, close first exceeds the peak; 101 | Edwards & Magee, Bulkowski Double Bottom. No prior-downtrend requirement. |
| `inverse_head_and_shoulders` | Head lowest by ≥ 0.3%, shoulders within 1%, close first exceeds the sloped neckline through the two interim peaks; 151 | Bulkowski Head-and-Shoulders Bottom; Murphy. |
| `ascending_triangle_breakout` | Three swing highs within 0.2%, rising last two swing lows, close first exceeds the flat top; 101 | Bulkowski Ascending Triangle (3+ tops, 2+ rising lows). |
| `trendline_breakout` | Close first exceeds the line through the last two falling swing highs; 101 | Edwards & Magee, Murphy ch. 4. Two-point line, closes never above it since the first point. |
| `triangle_breakout` | As `trendline_breakout` plus the last two swing lows since the first high rising; 101 | Bulkowski Symmetrical Triangle; upward break only. |
| `bull_flag_breakout` | Pole ≥ 1% within 15 bars to the highest high, 3–20 flag bars retracing ≤ 50% of the pole, close above the flag high; 61 | Bulkowski / Murphy flags; no published minimum pole, so thresholds are parameters. |

Conservative defaults mean several patterns are sparse on 1-minute data: in the real-data
check (TSLA, 12 sessions) `holy_grail_pullback`, `double_bottom_reversal` and
`inverse_head_and_shoulders` produced no signals at the defaults and did fire with relaxed
parameters. The 78-, 101-, 151- and 200-bar strategies need `market_data.warmup_bars` raised from
the example 60 (see section 13) to be ready at start-up.
