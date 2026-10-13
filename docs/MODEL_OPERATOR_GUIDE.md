# Model workflow operator guide

How to take XGBoost and LightGBM models from historical data to an operator-approved SHADOW,
PAPER or LIVE deployment on Mike_Trade. Background and contracts are in
[MODEL_TRAINING_ARCHITECTURE.md](MODEL_TRAINING_ARCHITECTURE.md) (training and artifacts) and
[MODEL_STRATEGIES.md](MODEL_STRATEGIES.md) (the four strategies and inference).

**Nothing here claims a model is profitable.** A model is a probability estimator for the label it
was trained on. Whether trading on it earns money is an empirical question that only held-out,
out-of-sample simulation and then SHADOW evidence can start to answer. Every command below is
offline except the trading engine itself; **training never runs inside the engine**, and nothing
here enables PAPER or LIVE trading.

```text
historical snapshots ─► model-prepare ─► model-train ─► model-report / model-walk-forward
   (explicit feed)       (data lock)      (immutable      (predictive quality, calibration)
                                           artifact)                │
                                                                    ▼
 SHADOW ◄── model-approve shadow ◄── model-backtest (existing BacktestRunner, out-of-sample)
   │             (immutable)                │
   ▼             ▼                          ▼
 model-health   model-approve paper|live ◄─ evidence: the out-of-sample backtest report
 + dashboard         (operator authorization still required to run PAPER/LIVE)
```

## 1. Where everything is stored

| What | Path | Written by | Mutable? |
| --- | --- | --- | --- |
| Raw bars | `research/data/` (Parquet + DuckDB catalog) | `research-data` | never (immutable snapshots) |
| Training configs | `config/modeling/*.yaml` | operator (git) | versioned in git |
| Data locks | `research/models/prepared/<experiment_id>/<config_sha256>.json` | `model-prepare` | never (create-only) |
| Model artifacts | `research/models/artifacts/<sha256>/model.*` | `model-train` | never |
| Manifests / evaluations | `research/models/manifests/<id>/<version>.json`, `evaluations/<sha256>.json` | `model-train` | never |
| Walk-forward summaries | `research/models/walk_forward/<experiment_id>/<config_sha256>.json` | `model-walk-forward` | never (create-only) |
| Model reports | `research/models/reports/<id>/<version>/` (`model_report.md/json`, CSV tables) | `model-report` | re-derivable |
| Approvals | `research/models/approvals/<id>/<version>/<stage>.json` | `model-approve` | never (create-only) |
| Campaign files | `config/modeling/campaigns/*.yaml` | operator (git) | versioned in git |
| Backtest results | `research/model_campaigns/<campaign_id>/<file-sha12>/` | `model-backtest` | per run |

`research/models` is mounted **read-only** into the engine and API containers (`compose.yaml`) and is
the default `service.model_root`; `MIKE_TRADE_MODEL_DIR` points the CLI at another root. Run
`mkdir -p research/models` once before the first `docker compose up` so Docker does not create it as root.

## 2. Selecting historical data

* **The frozen universe** is `config/research_universe_100_v1.json` (100 instruments, including
  the reference series SPY, QQQ, IWM). Check data with `make research-data-status` and
  `make research-data-verify`; do not acquire more years for this workflow.
* **The feed is always explicit** (`feed: iex` or `feed: sip` in the training and campaign files).
  A training selection refuses to mix feeds, timeframes or adjustments; a model records its
  feed; the runtime refuses a model whose feed differs from `market_data.feed`; a backtest
  refuses a model/dataset feed mismatch; and every reference series (SPY) comes from the *same*
  feed. There is no fallback from SIP to IEX or back.
* **IEX now, SIP later.** Everything works on the existing IEX snapshots today. SIP acquisition
  runs elsewhere and is not required. When SIP snapshots are complete, copy a config, set
  `feed: sip` and a new `model_id`, retrain — a SIP model is a different model and is only usable
  with `market_data.feed: sip`.
* **The final holdout is reserved** (`2026-09-21` – `2026-10-09`). Training, tuning, calibration,
  walk-forward and campaigns reject any range touching it. Holdout evaluation is a separate,
  explicitly authorized step this guide does not perform.
* Only regular-session 1-minute RAW bars are used. A missing minute resets feature state; nothing is
  interpolated.

## 3. Training configuration

`config/modeling/{xgboost,lightgbm}_{buy,sell}.yaml` are versioned examples. Key sections:

* `model_id` / `model_version` — immutable identity. A new training is a **new version**; an existing
  `id@version` can never be overwritten.
* `feed`, `universe_file`/`symbols`, `ranges` — disjoint, chronological `fit`, `early_stopping`,
  optional `calibration`, and `evaluation` partitions with an `embargo_minutes` gap. Every label must
  resolve *before its own partition ends*, so no label peeks across a boundary.
* `features` — the shared `intraday_core` feature set (the same code the runtime uses), the
  reference symbols (`[SPY]`) and `selected_feature_names`.
* `objective` — BUY: `triple_barrier` (win/loss barriers, horizon, entry/exit cost bps) or
  `forward_return`; SELL: `sell_continuation` plus an `entry_policy` that generates realistic held
  positions independently of future winners.
* `hyperparameters`, `search` — bounded candidate list; selection uses **only** the early-stopping
  partition. `probability_calibration: true` fits Platt/temperature scaling on the separate
  calibration partition.
* `resources` — `threads` (default 4–12 in the examples) and `device: cpu|gpu` (see §10).
* `walk_forward` — folds for `model-walk-forward`.

## 4. Training execution (100-instrument universe)

`MODEL_CONFIG` is the only required variable. Each experiment is prepared, trained and reported with
three commands; run them one model at a time.

```bash
# XGBoost BUY   (then repeat for the other three configs)
make model-prepare  MODEL_CONFIG=config/modeling/xgboost_buy.yaml
make model-train    MODEL_CONFIG=config/modeling/xgboost_buy.yaml \
                    MODEL_LOCK=research/models/prepared/xgboost_buy_tb_pooled_v1/<config_sha256>.json
make model-report   MODEL_ID=xgboost_buy_tb_pooled MODEL_VERSION=1.0.0
```

The four configurations are `xgboost_buy.yaml`, `xgboost_sell.yaml`, `lightgbm_buy.yaml` and
`lightgbm_sell.yaml`. `model-prepare` prints (and writes) the lock path, which includes the SHA-256 of
the configuration file. `make model-prepare`/`model-train` call
`python -m mike_trade.cli model-prepare|model-train --config ... [--lock ...]`.

* **`model-prepare`** resolves and checksum-verifies every snapshot, builds features/labels once,
  counts the observations in each partition and the fit label mix, estimates memory (§10), and writes
  the lock. It stores no model.
* **`model-train --lock`** trains with *exactly* the locked snapshots and stops before storing anything if the
  bytes resolved differ. Without `--lock` the newest matching snapshots are used and recorded in the
  manifest (still reproducible, but a new snapshot could change the next run).
* Training is a foreground offline process. Do not run it while the R730 is acquiring SIP data at full
  tilt; keep `resources.threads` ≤ half the cores. Output summary: artifact, manifest and evaluation paths.

## 5. Evaluating a model

| Command | Purpose |
| --- | --- |
| `make model-report MODEL_ID=… MODEL_VERSION=…` | Operator report (`model_report.md`, `.json`, `feature_importance.csv`, `calibration.csv`, `class_calibration.csv`, `per_symbol.csv`, `per_period.csv`) |
| `make model-evaluate MODEL_CONFIG=…` | Re-evaluate the saved model on its configured evaluation range from the *pinned* snapshots |
| `make model-walk-forward MODEL_CONFIG=… [MODEL_LOCK=…]` | Purged, embargoed expanding walk-forward folds; stores one immutable summary, no fold model |
| `make model-compare MODELS=id@v,id@v` | Side-by-side predictive metrics; flags non-identical experiment inputs |
| `make model-list`, `make model-inspect …` | Catalogue with approval stage; full manifest |

The report contains: model name/id/algorithm/version, artifact checksum, feed and universe, training
and validation date ranges, **number of observations per partition**, hyperparameters, held-out log
loss / Brier / accuracy / per-class precision and recall / confusion matrix, **confidence calibration and
one-vs-rest reliability tables with expected calibration error**, the class-frequency baseline and the
model's improvement over it, per-symbol and per-period metrics, ranked feature importance (native gain,
weight, optional permutation), and the walk-forward table when one exists.

How to read it: a model is only interesting if its held-out log loss beats the baseline *and* the
walk-forward folds agree (small stdev, no fold much worse); calibration bins should hug the diagonal
(otherwise thresholds on probabilities mean something other than their face value); per-period metrics
should not collapse in later months. Feature importance describes the model, not market causation.
These are predictive metrics, **not trading results**.

## 6. Model-driven backtests

`make model-backtest CAMPAIGN=config/modeling/campaigns/<file>.yaml` runs the existing
`BacktestRunner` (via the research batch coordinator) — completed-candle timing, next-bar-open fills,
conservative stop-first ambiguity, position sizing, costs/slippage, protective stops, the market-status
monitor, the trade ledger and equity/drawdown accounting are all the existing code. The campaign only
chooses the inputs; nothing is retrained and no model sees the test period.

Shipped campaigns: `model_strategy_comparison_pilot_v1.yaml` (5 symbols × 8 configurations = 40 jobs) and
`model_strategy_comparison_v1.yaml` (the full 100-symbol universe, 800 jobs; requires `APPROVE_LARGE_RUN=1`).
Configurations: **A** XGBoost BUY + trailing stop; **B** LightGBM BUY + trailing stop; **C** XGBoost BUY +
XGBoost SELL; **D** LightGBM BUY + LightGBM SELL; **E** XGBoost BUY + LightGBM SELL; **F** technical BUY
(`vwap_bounce`) + model SELL; **G** model BUY + technical SELL (`vwap_breakdown`); **H** the technical
reference pair for comparison. Each is just `buy: […]`, `sell: […]` over named model pins or any registered
strategy id; add configurations freely (including several BUYs or SELLs) — nothing is hard-coded.

```bash
make model-backtest CAMPAIGN=config/modeling/campaigns/model_strategy_comparison_pilot_v1.yaml DRY_RUN=1   # plan only
make model-backtest CAMPAIGN=config/modeling/campaigns/model_strategy_comparison_pilot_v1.yaml MODEL_WORKERS=2
```

Safety checks happen *before* any job runs: the window must avoid the holdout; the feed must match every
model and snapshot; each model is pinned by id, version and its checksum (resolved from the immutable
repository); a window overlapping a model's training or tuning data is refused unless `allow_in_sample: true`
(and is labelled `in_sample` everywhere); missing symbol coverage is an error unless `SKIP_MISSING=1` (then
listed); more than 50 jobs need `APPROVE_LARGE_RUN=1`; workers default to 2 and are capped by CPU and memory.
Jobs run in `spawn`ed processes (a forked child of a process that used OpenMP is unsafe).

**Output** — `research/model_campaigns/<campaign_id>/<sha12>/`: `campaign.yaml` (the exact bytes), `plan.json`,
`batch.json`, the runner's `executions/<id>/<run_id>.{manifest,results}.json` (every bar's model probabilities
and decision are in the `model_audit` evaluation rows; the manifest records model lineage), `trade_ledgers/`,
and `report/` with `report.md`, `report.json`, `summary.csv`, `per_symbol.csv`, `per_period.csv`, `trades.csv`
(entry/exit timestamps and prices per trade), `baseline_buy_and_hold.csv` and `models.csv`.

Reported per configuration (all computed from the recorded ledgers and runs): trades; wins/losses/breakeven;
win rate; net profit; return on capital; profit factor; average winner and loser; maximum drawdown (on the
combined realized-P&L curve) ; trading costs; estimated slippage; exit reasons; entries refused by the
market-status gate; model audit counts. Also: per-stock and per-month tables, the **buy-and-hold** baseline
and the always-flat baseline, configuration H as a technical baseline, the model section (training
observations, predictive metrics, calibration, top features), warnings, failed jobs, and SHA-256 of every run
artifact. Each (configuration, symbol) is an independent account with the campaign's starting capital.

`make model-backtest-compare REPORTS=a/report/report.json,b/report/report.json` lines reports up and warns when
their windows, feeds, symbols, capital, costs or stops differ. A higher net profit in one simulation on a short
window is not evidence of an edge.

**Reproducing a run:** same campaign bytes + same pinned snapshots + same model artifacts + same seed ⇒ same
trades (the runner is deterministic; compare `results_hash` values and ledgers). Rerun `model-backtest` with the
same file to regenerate into a new execution directory.

## 7. Loading a trained model into the engine

Pin the exact artifact in the strategy parameters (`model-pin` prints the block; it refuses a model that
lacks the approval for the stage you ask for):

```bash
make model-pin MODEL_ID=xgboost_buy_tb_pooled MODEL_VERSION=1.0.0 STAGE=shadow PIN_NAME=aapl_xgb_buy
```

```yaml
service: {model_root: research/models}
market_data: {feed: iex}              # must equal the model's training feed
strategies:
  aapl_xgb_buy:
    plugin: xgboost_probability_buy
    side: buy
    parameters:
      model_id: xgboost_buy_tb_pooled
      model_version: 1.0.0
      artifact_sha256: <from model-pin>
      minimum_favorable_probability_bps: 5500
      maximum_adverse_probability_bps: 2500
```

`make model-check MODEL_RUNTIME_CONFIG=config/mike_trade.yaml MODEL_MODE=shadow` performs the engine's startup
verification (artifact present and checksum-valid, algorithm, role, feed, reproducible feature schema) plus the
approval check, without starting anything. The engine repeats the same checks at startup and refuses to start on
any failure; the loaded artifact is held in memory and never reloaded or replaced while running.

## 8. Strategy, protection and crash configuration

Strategy parameters and combinations (any BUY with any SELL, model or technical, several per symbol) are
documented in [MODEL_STRATEGIES.md](MODEL_STRATEGIES.md) §6–7; a complete validated configuration is
`config/model_strategies.example.yaml`.

* **Protective stops** are per-symbol `stop_policy` (`fixed`, `native_trailing`, `stepped_trailing`) and
  are armed and managed by the engine independently of any strategy. A model can neither loosen nor cancel
  them. Always configure one. The campaigns use `native_trailing` 1.5 % and report which exits were protective.
* **Market-crash protection** is `execution.market_status_enabled: true`. The Market Status Monitor (SPY/QQQ/IWM)
  authorizes *new entries* by market state; in CRASH a model BUY is refused (`MARKET_CRASH_ACUTE_RISK`). It is
  not an automatic liquidation system; exits and stops stay authorized. Campaigns replay the same monitor
  (`market_status_enabled: true`) and report the refused entries.
* Position sizing, entry-cycle limits, buying-power checks, minimum hold and kill switches are unchanged.

## 9. SHADOW operation and monitoring

```bash
make model-check MODEL_RUNTIME_CONFIG=config/model_strategies.example.yaml MODEL_MODE=shadow
docker compose build && make up            # the existing SHADOW engine + dashboard
make model-health MODEL_DB=state/dev/shadow.sqlite3
```

* The engine publishes a `models` component each heartbeat: configured pins and `LOADED`/`UNAVAILABLE`
  state, per-model prediction/failure counts, last prediction time and symbol, last failure, the last decision
  (SIGNAL/NO_SIGNAL and reason) per strategy and symbol, and inference-error count. `make model-health` prints it;
  the dashboard **Health** page shows a read-only *Models* panel (`GET /api/v2/models/status`).
* Structured `mike_trade.model` log records mark every signal and each change of reason; unavailability is a
  WARNING. Detailed per-decision provenance is in the strategy's persisted `model_audit` state.
* SHADOW uses the existing simulated broker and persistent ledger. Use an isolated scenario/database for smoke
  tests; never point experiments at the production SHADOW database.

## 10. Resources, threads and GPU

Measured on this repository's synthetic generator (one symbol-year of 1-minute bars plus SPY; see
`docs/MODEL_TRAINING_ARCHITECTURE.md` §Performance):

| Measurement (host: 32 cores, 61 GB; `threads: 2`) | XGBoost | LightGBM |
| --- | --- | --- |
| Prepare 1 symbol-year (240 sessions, 93,600 bars) + SPY: features, labels, memmaps | 9.6 s | 9.4 s |
| Fit rows / evaluation rows produced (7-month fit, 3-week evaluation) | 50,182 / 5,390 | 50,182 / 5,390 |
| Fit, 60 boosting rounds, 1 candidate, calibration enabled | 0.7 s | 0.5 s |
| Peak resident memory of the whole process | 731 MB | 731 MB |

These are **synthetic random-walk bars**, so the fitted trees are shallow and early stopping triggers quickly;
they establish memory and preparation cost, not realistic fit time. Extrapolating linearly for the 100-symbol
universe: preparation ≈ 15–20 minutes single-process per configuration, ≈ 5 million fit rows, ≈ 0.4 GB of
float32 feature memmap (19 features) plus a few GB inside the native library — comfortably within the R730.
Real-data fit time (hundreds of rounds, search candidates) is expected to be minutes to tens of minutes per
candidate on 4–8 threads; measure the first real run (`model-prepare` prints its own estimate) before scheduling
the other three.

* Memory scales with the **fit** partition: `rows × features × 4 bytes` (disk-backed memmap) and roughly three
  times that inside the native library; `model-prepare` prints the estimate for your configuration. Feature
  generation holds one symbol's examples in RAM at a time (hundreds of MB), so peak RAM is dominated by one
  symbol, not by the universe.
* Defaults are CPU with `threads` ≤ half the cores. Training is one process; hyperparameter candidates run
  sequentially (no nested pools). `model-backtest` uses 2 workers by default and caps by CPU/memory.
  Inference in the engine is single-threaded per prediction.
* `resources.device: gpu` opts in to the library GPU path where the installed build supports it; CPU
  results are the reference. Reproducibility across devices and thread counts is not bit-guaranteed — the
  immutable artifact, not a retrain, is the source of truth.
* Docker images include `libgomp1` (OpenMP) for both libraries.

## 11. Safe model promotion

Training and approval are separate. A model reaches a trading mode only through recorded, immutable
operator decisions:

1. **Shadow approval** — `make model-approve MODEL_ID=… MODEL_VERSION=… STAGE=shadow REASON="…"`. Required to start
   *any* SHADOW/PAPER/LIVE engine with the model. Verifies the artifact checksum and binds the approval to it.
2. **Run in SHADOW** for a meaningful period; review `model-health`, rejected-prediction counts, trades and the
   report-vs-live behavior.
3. **Paper approval** — `STAGE=paper EVIDENCE=research/model_campaigns/…/report/report.json`. Requires the shadow
   approval and an *out-of-sample* model-backtest report that contains this exact artifact.
4. **Live approval** — `STAGE=live EVIDENCE=… CONFIRM_LIVE=YES`. Requires the paper approval. Running PAPER/LIVE
   still needs the existing authorization, capability records, reconciliation and operator confirmations;
   approval only permits *this artifact* to load.

Checklist before each step: out-of-sample (not in-sample) evidence; walk-forward stability; calibration; costs
and slippage included; the protective stop and market-status gate enabled; no unexplained failures in SHADOW;
config pinned by version *and* checksum; rollback plan = restore the previous pin (older versions stay immutable).
An approval can never be edited; to withdraw a model, remove it from the configuration. A retrained model is a new
version with no approvals.

## 12. Reproducing an experiment

A completed experiment is defined by: the configuration/campaign file (its SHA-256 is in every artifact), the
lock (dataset ids and checksums), the seed, the code revision (recorded in run manifests), and the artifact
checksum. To reproduce: check out the same revision, confirm snapshots with `make research-data-verify`, run
`model-prepare` (it must produce the identical lock), `model-train --lock`, and compare the new manifest's
metrics; for backtests rerun the same campaign file and compare ledgers. If a result cannot be reproduced the
tooling says so rather than overwriting: locks, walk-forward summaries, manifests, approvals and artifacts are
create-only.

## 13. Failure handling

| Symptom | Meaning | Action |
| --- | --- | --- |
| `model-train`: "immutable artifact already exists" | `id@version` already stored | bump `model_version` in a new config |
| "resolved datasets differ from the locked preparation" | snapshots changed since `model-prepare` | rerun `model-prepare`, review, retrain |
| "training lock … different configuration" | lock belongs to another config file | prepare again for this config |
| Campaign "…is in_sample/tuning_overlap" | window touches fit/tuning data | move the window after the model's data, or `allow_in_sample` knowingly |
| Campaign "…overlaps the reserved final holdout" | refused by design | change the window |
| Campaign "lack IEX coverage" | missing snapshots | acquire data, or `SKIP_MISSING=1` (listed in the report) |
| Engine start `MODEL_MODEL_MISSING` / `MODEL_CHECKSUM_MISMATCH` | pin not in repository / artifact altered | fix the pin or restore the repository; never edit artifacts |
| Engine start `MODEL_NOT_APPROVED` | no valid approval for this mode | `model-approve` (and evidence for paper/live) |
| Engine start `MODEL_FEED_MISMATCH` | model trained on another feed | use a model of the configured feed |
| Dashboard/`model-health`: prediction `REFERENCE_BAR_MISSING`, `VWAP_UNAVAILABLE`, `STALE_OBSERVATION` | input data problem; that model strategy is silent | check SPY subscription and `warmup_bars ≥ 25`; protective stops and technical strategies are unaffected |
| `model-health` "not published" | no model strategies configured, or engine not running | `make model-check`; check engine status |
| Out of memory in `model-prepare`/`model-train` | fit partition too large for the host | fewer symbols, shorter `fit` range, lower `threads`, more RAM |

A model failure fails *closed for that strategy only*: no new model-driven entry is produced, and nothing
disables broker-native protection, deterministic strategies, or the market-status gate.
