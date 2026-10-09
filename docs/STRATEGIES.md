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
