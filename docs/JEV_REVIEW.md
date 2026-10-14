# JEV discretionary trade review

An **optional** reviewer ("JEV") can look at a proposed BUY or strategy SELL after the strategies have
signalled and the existing arbitration has chosen an action, and **APPROVE or REJECT** it before any
ordinary order is submitted. It adds discretionary judgement beyond the deterministic rules. It is
*not* a confidence or probability threshold: research on the XGBoost/LightGBM BUY models found that
higher model confidence did not correlate with higher profit, so the reviewer reads the structured
evidence and the market context instead of re-applying a number.

Nothing in this feature is active unless `decision_review.enabled: true`. With it off (the
default everywhere) no stage is built, no result artifact gains a field, and no configuration
fingerprint changes.

```
strategies signal ─▶ existing arbitration selects an action ─▶ TradeProposal + evidence
        ─▶ DecisionReviewStage.review()  ─▶ APPROVE / REJECT
        ─▶ ordinary order handling (only when allowed)           [the same path in every mode]
```

The stage sits at the same point in all four execution paths — BACKTEST (`backtest/runner.py`),
SHADOW (`app.VwapReferenceLifecycle`), PAPER and LIVE (`live/lifecycle.LiveStrategyLifecycle`) — and
there is no per-strategy execution path. A reviewer never places orders, never receives credentials
or account data, and never touches sizing, protective stops, reconciliation or broker risk controls.

## 1. The evidence contract

Code: `mike_trade/review/evidence.py` (interface version **1**, document `schema_version` **1**).

Every reviewed signal is accompanied by a `DecisionEvidence` document: a common *envelope* plus a
strategy-specific `evidence` object, similar to a MongoDB document.

```json
{
  "schema_version": 1,
  "strategy_id": "xgboost_probability_buy",
  "strategy_version": "1.1.0",
  "action": "BUY",
  "symbol": "NVDA",
  "timestamp": "2026-09-08T14:31:00.000000Z",
  "signal_id": "…sha256 of strategy/version/symbol/side/bar…",
  "reason_code": "MODEL_FAVORABLE",
  "reason": "P(favorable) exceeds P(adverse)",
  "evidence": {
    "model": {"model_id": "xgboost_buy_tb_pooled", "model_version": "1.0.0",
              "algorithm": "xgboost", "artifact_sha256": "…", "feature_schema": "intraday_core@1.0.0",
              "horizon_bars": 30, "observation_end": "2026-09-08T14:31:00+00:00", …},
    "decision_mode": "MODEL_DIRECTED",
    "thresholds": {"minimum_favorable_probability_bps": 5000, "maximum_adverse_probability_bps": 3000, …},
    "class_probabilities": {"-1": 0.2, "0": 0.32, "1": 0.48},
    "class_roles": {"favorable": "1", "adverse": "-1"},
    "probabilities": {"favorable": 0.48, "adverse": 0.2, "neutral": 0.32},
    "features": {"return_1": 0.0012, "vwap_distance": -0.0004, "…": 0.0},
    "decision_inputs": {"favorable_probability_bps": 4800, "adverse_probability_bps": 2000}
  }
}
```

Rules:

* The envelope is common; `evidence` is whatever the strategy needs to describe *why it fired*. A
  strategy is never forced into a shared set of indicators or probability fields.
* `evidence` is plain JSON (`Decimal` → exact string, `datetime` → UTC `Z` string, NaN/Infinity and
  unknown objects are rejected, not stringified).
* Evidence reports only facts the strategy computed or recorded: real model probabilities, the exact
  feature vector the model was evaluated on, the actual opening-range / VWAP-cycle levels. Nothing
  is estimated, scored or made up; when a fact is unavailable the key is omitted or flagged
  (`features_unavailable`).
* A decision made by several strategies lists **every contributing strategy** (the proposal's
  `originating_strategy_ids` and one `strategies[]` entry per signalling strategy, `selected: true`
  on the one arbitration chose), each with its own evidence.
* **Evidence is mandatory for every strategy that is reviewed.** If the selected signal has no
  evidence the error policy applies and nothing is sent to the reviewer.
* If a strategy that would be reviewed does not implement the interface, startup fails with
  `UnsupportedEvidenceError` (a `ConfigurationError`, code `STRATEGY_EVIDENCE_UNSUPPORTED`) naming the
  strategy and symbol. Strategies that are not reviewed are never required to implement it.

### Strategies that implement it today

| Strategy | Evidence |
|---|---|
| `xgboost_probability_buy`, `lightgbm_probability_buy` | model identity/provenance, decision mode and thresholds, class probabilities (raw and as favorable/adverse/neutral), the model's feature values |
| `xgboost_probability_sell`, `lightgbm_probability_sell` | the same for the continuation model: risk-exit / exit-favorable / hold-favorable probabilities, the actual position facts and position features |
| `five_minute_orb_michelle_buy` | trigger kind and event time, opening-range OHLC/height, ORB high, VWAP at entry, completed-minute VWAP slope, ATR/extension and risk gates, optional SPY confirmation |
| `vwap_bounce_break_michelle_buy` | the breakdown/recovery cycle (breakdown time, cycle low, bottom pattern, first confirmation), entry trigger and distance above VWAP, latest EMA9/EMA20/VWAP |
| `five_minute_orb_michelle_sell`, `vwap_bounce_break_michelle_sell` | implement the interface but declare `PROTECTIVE_EXIT = True`: these signals mirror the engine-owned protective stop and are **never reviewed**. Naming one in `decision_review.strategies` is a configuration error |

The older technical strategies are intentionally untouched.

### Adding evidence to a new strategy

```python
from mike_trade.review.evidence import EVIDENCE_INTERFACE_VERSION, make_evidence

class MyBuy(BuyStrategy):
    spec = ...
    EVIDENCE_INTERFACE_VERSION = EVIDENCE_INTERFACE_VERSION      # declares support (v1)

    def build_evidence(self, context, params, state, evaluation):
        # pure: no I/O, no clock, no mutation.  `state` already includes this evaluation's
        # own state_updates, so facts recorded at signal time can be read back.
        return make_evidence(
            self.spec, evaluation, context,
            reason_code="MY_REASON_CODE", reason="why it fired, in one sentence",
            evidence={"breakout_level": level, "volume_ratio": ratio},   # any JSON-able facts
        )
```

`collect_evidence()` calls the method and verifies the document describes the evaluated signal
(strategy, version, action, symbol, `signal_id`); a mismatch is an `EvidenceError`. Add a test that
builds the evidence from a real signal (see `tests/unit/test_decision_review_core.py`).

## 2. Configuration

```yaml
decision_review:
  enabled: true            # default false everywhere
  reviewer: jev            # a `reviewers:` entry of the provider catalog
  review_buy: true
  review_sell: true
  mode: enforce            # disabled | advisory | enforce
  timeout_seconds: 10
  on_error: bypass         # bypass | reject
  record_reviews: true
  strategies: []           # empty = every strategy configured on a reviewed side
  symbols: []              # empty = every symbol
  # source: live           # live | replay  (REQUIRED for research runs; default live operationally)
  # replay_from: [path…]   # recorded review files/directories (source: replay)
  # model: vendor/model    # override the catalog entry's model identifier
  # cache: {enabled: true, path: null}      # shared JSONL cache file (live source)
  # expect_reviewer: {model: …}            # pin which recorded reviewer to replay
  # max_proposal_age_seconds: 60 · candle_count: 30 · daily_candle_count: 5 · provider_catalog: path
```

* **Scope.** A proposal is reviewed when its action (`review_buy`/`review_sell`), symbol and *selected*
  strategy are enabled. Review can be enabled for selected strategies, selected symbols, or both,
  without touching any other strategy. Unknown names in `strategies` are an error.
* **`mode: disabled`** — identical to `enabled: false`: no stage, behaviour unchanged.
* **`mode: advisory`** — the reviewer's opinion is obtained and recorded, execution is never altered.
  In SHADOW/PAPER/LIVE the call runs in the background and the order does not wait for it; in a
  backtest it is awaited (simulated time does not pass). Errors, timeouts and REJECTs never block.
* **`mode: enforce`** — APPROVE allows the ordinary action; REJECT blocks it.
* **Never active by default:** the shipped example config has `enabled: false`, and an inactive
  configuration is excluded from the config fingerprint.

### Error policy (`on_error`) — timeout, transport failure, malformed response, missing evidence

| Policy | BUY | SELL |
|---|---|---|
| `bypass` | the entry proceeds as if there were no review | the strategy exit proceeds as if there were no review |
| `reject` | the entry is blocked (no order, no budget held) | the strategy exit is **blocked**; the position is **not** closed by that strategy and stays held under its protective stop and the end-of-day close |

Consequences for SELL: with `reject`, a reviewer outage can leave a position the strategy wanted to
close open for longer, exposed to the very move the SELL was reacting to (only the protective stop,
EOD close and other mandatory exits remain). With `bypass`, an outage silently removes the review
and the strategy behaves as it would without JEV. Neither policy can touch a mandatory exit:
protective stops, end-of-day liquidation, kill-switch and emergency liquidations, manual and
hard-risk exits never enter the stage (a proposal for any exit source other than `STRATEGY_SELL`
raises `MandatoryExitReviewError`), and the Michelle protective SELL strategies are exempt.
Advisory mode ignores `on_error` for execution (it always proceeds).

## 3. Providers: OpenRouter only

JEV is reached **exclusively through OpenRouter** (`https://openrouter.ai/api/v1/chat/completions`)
with the existing **`OPENROUTER_API_KEY`** environment variable. There is no JEV-specific key,
account or endpoint, and the key is never written to configuration, records, logs or requests.

* **Model identifier:** `typesafe/jev-router` — listed by OpenRouter's public model catalog
  (`GET https://openrouter.ai/api/v1/models`, checked 2026-10-14) as *"TypeSafe: Jev Router"*. It is a
  **router**: it picks the underlying model and reasoning effort per request, so the model that
  actually served each review is recorded (`usage.served_model`) and included in
  `reviewer_version`. Its pricing is variable (reported per request in `usage.cost`), the catalog
  states no knowledge cutoff, and its `supported_parameters` include `response_format` and
  `structured_outputs`. The catalog description links a "Jev" base model (`~typesafe/jev-latest`),
  which does **not** appear as a model id in the public list, so it is not used. The identifier is
  configuration (`reviewers.jev.model` or `decision_review.model`), never hard-coded in the logic.
* **Provider catalog** (`config/providers.yaml`, example in `config/providers.example.yaml`):

  ```yaml
  reviewers:
    jev:
      provider: openrouter
      model: typesafe/jev-router
      api_key_env: OPENROUTER_API_KEY     # a variable NAME, never the value
      temperature: 0
      response_format: json_schema         # json_schema | json_object | none
      max_concurrent_requests: 16          # provider concurrency limit
      requests_per_minute: null            # null = no client-side rate limit
      point_in_time_safe: false            # operator attestation (see §5)
  ```

  Without a catalog entry the built-in default (OpenRouter + `typesafe/jev-router`) applies, so the
  only thing an operator must supply is `OPENROUTER_API_KEY`. Another OpenRouter model is a one-line
  change; another provider is a new entry in `review/factory.REVIEWER_PROVIDERS` implementing
  `TradeDecisionReviewer` (`identity`, `async review(request)`, `aclose()`) — nothing else in the
  pipeline is provider specific. No LLM framework was added: the adapter is a plain `httpx` call.
* **Structured responses.** The model is constrained to a JSON schema
  (`response_format: json_schema`, `strict`, `provider.require_parameters: true` so a provider that
  cannot honour it is not used). The answer is parsed *strictly*:
  `{"decision": "APPROVE"|"REJECT", "reason": str, "observations": [str]}` — exact upper-case
  decision, no extra keys, no coercion, no code fences, no free-text interpretation. `reviewer_version`
  is stamped by the adapter (`<model>|served:<routed model>|<protocol>|prompt:<n>`), not asserted by
  the model. Anything else is a `MalformedReviewResponse` and follows `on_error`.
* **What is sent** (`review/proposal.build_review_request`): the proposal (action, symbol, decision time,
  originating strategies and their evidence), recent **completed** 1m/5m candles (default 30) and
  daily candles (default 5), indicators, last quote/trade, time of day and session position, feed and
  provenance with data timestamps, and for a SELL the position (quantity, fill-derived entry price,
  holding time, mark, unrealized P&L) and the existing protective stop. No credentials, account ids,
  balances or buying power. Requests are compact; history is bounded by `candle_count`.
* **Cost.** OpenRouter's `usage` (tokens and `cost`) is requested (`usage.include`) and stored on each
  record; `DecisionReviewStage.summary()` and `review.analysis` total it.

## 4. Order of operations, validity and concurrency

* The review happens **after** arbitration and every deterministic pre-check (cutoff, attempt limits,
  sizing) and **before** any budget reservation, order, or protective-stop cancellation. A REJECTed SELL
  therefore leaves the protective stop exactly as it was.
* In SHADOW/PAPER/LIVE an enforced review is *deferred*: the symbol's evaluation completes, the review
  is awaited **outside** the reconciler lock and concurrently with other symbols' reviews (bounded by
  `max_concurrent_requests`), and approved actions then re-enter the serialized order path one at a
  time. A slow reviewer therefore never blocks unrelated symbols, trade-update processing, stop
  ratcheting or reconciliation.
* Before acting, `DecisionReviewStage.authorize()` re-checks the state the proposal was made in
  (flat/held, cycle id, quantity, pending entry/exit, protective-order status, entry gate, end-of-day,
  entry cutoff). Any change withdraws the approval (`STALE_BLOCKED`, recorded as a disposition record).
  If a newer completed bar exists and the proposal is older than `max_proposal_age_seconds` it also
  expires. A delayed approval can never authorize a proposal that is no longer valid. Execution after a
  delay uses the freshest snapshot when one is available.
* Identical requests are never paid for twice: concurrent identical proposals share one call, and a
  completed review is served from the cache for any later identical complete input from the same
  reviewer identity.

## 5. Recording, caching and replay

Every review — live inference, cache hit or replay — is a `ReviewRecord` (`review/store.py`) with the
stable **input hash** (SHA-256 of the canonical request), reviewer identity (provider, reviewer,
model, protocol, non-secret settings incl. the system-prompt hash), request timestamp, structured
request, structured response, decision, explanation and observations, usage/cost, latency, error, mode,
enforcement, and the effect on execution. Records are JSON Lines (`*.reviews.jsonl`):

* BACKTEST: `<run_id>.reviews.jsonl` next to the run's `results.json`; SHADOW/PAPER/LIVE:
  `<data_dir>/reviews/<mode>.reviews.jsonl`.
* The immutable `results.json` of a reviewed backtest gains `reviews` (a deterministic summary of each
  review — no wall clock, latency or cost) and `review_validity`. Runs without review are unchanged.

**Replay.** `source: replay` + `replay_from: [files or directories]` re-runs an experiment using only
recorded decisions: the reviewer is never constructed, no credential or network is needed, and the
fills/cycles/review ledger reproduce the original exactly. A proposal with no recording raises
`ReviewReplayMiss` (never a silent bypass). If several reviewer identities recorded the same input,
pin one with `expect_reviewer`. **Cache reuse** (`source: live`) applies the same rule: a recorded
review is reused only when the complete input hash and the reviewer identity/settings match.

**Research must choose explicitly** between live inference (`source: live`), recorded replay
(`source: replay`) and advisory recording (`mode: advisory`); there is no default, and planning /
preflight (`research-batch`, `model-backtest --dry-run`) make **no** model calls — reviews are produced
only while a job runs. There is no campaign-size approval gate: scale is whatever the campaign defines,
and the cache deduplicates identical inputs.

**Point-in-time honesty.** Requests contain only information available at the decision time (the
shared context holds only completed candles; the builder additionally refuses any candle ending after
the decision time, and quotes/trades stamped later are omitted). But a *model* may still know about
the future or the news. A reviewer is attested point-in-time only through `point_in_time_safe: true`
(default `false`); results from any other reviewer are flagged `review_validity.point_in_time_valid:
false` and must not be presented as point-in-time-valid historical evidence. `typesafe/jev-router`
is not attested, so historical JEV backtests are indicative only.

## 6. Running research

```yaml
# campaign YAML (modeling/campaign.py): a campaign-wide default, per-configuration overrides
decision_review: {enabled: true, mode: enforce, source: replay, replay_from: [research/reviews/run1]}
configurations:
  A_baseline:    {buy: [xgb_buy], sell: [xgb_sell], decision_review: {}}          # no review
  B_review_buy:  {buy: [xgb_buy], sell: [xgb_sell], decision_review: {enabled: true, mode: enforce,
                  source: live, review_sell: false}}
  C_review_both: {buy: [xgb_buy], sell: [xgb_sell], decision_review: {enabled: true, mode: enforce,
                  source: live}}
```

`research-batch` jobs accept the same `decision_review` mapping. Live inference needs
`OPENROUTER_API_KEY` in the worker environment (it is checked when each job starts).

## 7. Comparing JEV-reviewed trading with the baseline

Run the variants as separate experiments — baseline, BUY only, SELL only, both — then:

```
python -m mike_trade.review.analysis \
  --run baseline=…/<run>.results.json --run buy_only=… --run sell_only=… --run both=…
```

(the sibling `.reviews.jsonl` is read automatically). The report keeps **executed outcomes** (facts:
closed trades, realized P&L from fills, win rate, profit factor) apart from **counterfactual
estimates** (rejected proposals that never executed). Counterfactuals are absent unless an estimator
is supplied (`review.analysis.review_rows(..., counterfactual=…)`; `path_counterfactual` is a simple
mark-to-later-price helper). They use prices that did not exist at decision time, are labelled
`ESTIMATE_NOT_EXECUTED`, and are never added to executed totals. Removing a trade changes capital,
sequencing and later entries, so variant differences are differences between runs, not a sum of
individual trade outcomes; a rejection is never assumed to have helped. Each row carries the proposal,
supporting strategies, decision, explanation, whether it executed or was blocked, the actual outcome
when executed, latency and cost.

## 8. Broker and live-execution considerations

* **Latency and failure.** Enforced review adds the reviewer's latency (typically seconds) between a
  signal and its order; entries are time-sensitive. Use `timeout_seconds` to bound it; `on_error`
  decides what a failure means (see §2). Advisory mode adds no delay.
* **No broker interaction from the reviewer.** The reviewer returns a decision only. A REJECT does not
  cancel, replace or place anything; the protective stop and reconciliation operate independently.
* **Serialization is preserved.** Orders are still decided one at a time under the existing
  serialization (the reconciler lock in PAPER/LIVE); only the *waiting* for the reviewer is concurrent.
* **Data egress.** Market data, evidence and position/stop details for a proposal are sent to
  OpenRouter and its upstream provider; no credentials or account data are. Review that against your
  data-handling requirements before enabling it for LIVE.
* **Startup is fail-closed:** an in-scope strategy without evidence support, an unknown reviewer, or a
  missing `OPENROUTER_API_KEY` (live source) stops startup with a clear `ConfigurationError`.
* **Not applicable to** SHORT/COVER roles (unchanged), BACKTEST through `build_runtime` (reviews run
  through the research runner), or mandatory exits.
