# Historical research operations

Research data is stored independently from the SHADOW account under
`research/data/` on the host (`/app/research/data` in Compose). Apache Parquet
stores the immutable observations; DuckDB provides a persistent metadata
catalog at `research/data/catalog.duckdb`, which can be opened directly in
JetBrains DataGrip. The catalog's `datasets` table lists snapshot identity and
coverage. To inspect observations in DataGrip, query the matching file, for
example `SELECT * FROM read_parquet('/path/to/research/data/snapshots/<file>.parquet')`.
The catalog writer uses an advisory file lock; research readers query immutable
Parquet with in-memory DuckDB connections, so multiple workers do not contend
for the catalog write lock or copy a full dataset into memory. Keep DataGrip in
read-only mode while acquisition/catalog updates are running because DuckDB
allows one database writer process at a time.

## Multicore execution

`research-six` and `research-batch` use a reusable `ProcessPoolExecutor`.
Each job is a serializable experiment specification; a worker opens the
selected immutable Parquet snapshot by identifier, builds the normal replay
feed, and invokes the existing `BacktestRunner`. Workers never receive a
serialized dataset and never share a writable DuckDB connection. Each process
has a two-snapshot LRU cache, bounded to avoid retaining an unbounded research
universe. The coordinator submits at most twice the worker count, aggregates
results in input order, and commits each run manifest/ledger under a short
cross-process lock. Completed run artifacts survive another job's failure.

`--workers auto` uses CPU affinity and cgroup CPU quota, then limits workers by
the experiment count and available memory at a conservative 512 MiB per
worker. An explicit worker count is also bounded by available CPUs and job
count. `--workers 1` runs the same code path in one child process, useful for
determinism checks and debugging. Progress reports successful/failed job counts
and elapsed time. A failed job is recorded with exception type and traceback;
the batch continues and the command exits nonzero if any job failed. Ctrl-C
cancels queued work and waits for already-running workers to shut down cleanly.

Example JSON batch file:

```json
{
  "experiments": [
    {
      "symbol": "NVDA",
      "strategy": "vwap_bounce",
      "start": "2026-07-09T00:00:00Z",
      "end": "2026-10-09T00:00:00Z",
      "seed": 271828,
      "starting_capital": "10000",
      "dataset_id": "alpaca-iex-NVDA-1m-raw-629a448f24922fcb4a10",
      "parameters": {"vwap_bounce": {}},
      "execution": {"slippage_bps": "5", "fee_per_order": "0", "fee_per_share": "0", "fee_bps": "0"}
    }
  ]
}
```

Without further fields a job runs the registered `vwap_reference` lifecycle:
`strategy` (`vwap_bounce` or `vwap_breakdown`) is only the recorded **focus
label** — both the `vwap_bounce` BUY and `vwap_breakdown` SELL legs always
execute, so the two labels of one symbol produce identical trades (they differ
only in run identity). Results report `strategy_is_label: true`, `legs`, the
`protective_stop` and an `exit_reasons` histogram. Symbols, date ranges,
snapshot IDs, seeds, strategy parameter mappings, capital and execution
assumptions are job-specific; omitted values use the documented $10,000
capital, 5 bps slippage and zero-fee defaults, and parameters override the
lifecycle defaults and enter the immutable backtest configuration hash.

To research an explicit collection, add `buy_strategies` and/or
`sell_strategies` (any registered ids, SELL optional) and optionally a
`protective_stop` in the operator `stop_policy` shape; `strategy` is then a
free label:

```json
{"symbol": "IBM", "strategy": "ibm_collection", "start": "2026-07-09T00:00:00Z",
 "end": "2026-10-09T00:00:00Z",
 "buy_strategies": ["vwap_bounce", "momentum_breakout"],
 "sell_strategies": ["vwap_breakdown", "momentum_reversal"],
 "protective_stop": {"type": "stepped_trailing", "initial_stop_percent": "1.5",
                     "step_trigger_percent": "1.0", "step_percent": "0.5"}}
```

```sh
# Baseline batch, automatic resource-aware worker count
docker compose run --rm --no-deps mike-trade-research research-six \
  --start 2026-07-09T00:00:00Z --end 2026-10-09T00:00:00Z --workers auto

# Determinism/debug run using one worker; 2 or 4 are also supported
docker compose run --rm --no-deps mike-trade-research research-six \
  --start 2026-07-09T00:00:00Z --end 2026-10-09T00:00:00Z --workers 1

# General experiment batch (mount a host JSON file read-only)
docker compose run --rm --no-deps \
  -v "$PWD/research/jobs.json:/app/research/jobs.json:ro" \
  mike-trade-research research-batch --jobs /app/research/jobs.json --workers 4
```

Compose imposes no single-core CPU quota. The worker resolver honors the
container's CPU affinity/quota and memory cgroup; Docker/host memory remains the
ultimate resource boundary. Larger worker counts increase peak memory because
each active process materializes its own replay events. Start with `auto`,
measure on DEV, and raise the explicit count on the R730 only within its actual
CPU and memory budget. Do not run duplicate large batches against the same
result directory concurrently; immutable artifact commits are safe, but a
single coordinator provides clearer progress and failure accounting.

Parquet snapshots
are immutable and content-addressed; JSON manifests record Alpaca, feed, symbol,
timeframe, adjustment, UTC coverage, acquisition time, bar count, schema, and
file SHA-256. New acquisition writes a new snapshot and does not rewrite any
snapshot referenced by a prior experiment. `research/results/` stores the
backtest runner's immutable manifests and ledgers.

The configured IEX feed is a single exchange venue, not consolidated SIP.
Intraday VWAP therefore reflects only observed IEX trades/bars and can differ
materially from consolidated-market VWAP. Historical bars contain no bid/ask
quotes; simulation is bar-only, with signal bars becoming executable at the
next bar open and 5 bps adverse slippage. Position sizing uses the existing
reference configuration: the lower of the $3,000 symbol budget and a $30
planned-loss budget at the 1.5% trailing-stop distance, rounded down to whole
shares. A 1.5% protective trailing stop is armed after the simulated entry
fill; entries stop 30 minutes before session close, are limited to one entry
cycle per session, and exits observe the 60-second minimum hold. Costs are otherwise the recorded commission-free
assumption. Results are descriptive baselines, not evidence of profitability.

The two VWAP strategies are separate registered lifecycle legs: `vwap_bounce`
is the BUY entry and `vwap_breakdown` is the SELL exit. Each default experiment
uses the production `vwap_reference` pair so exits have actual positions to
manage; the selected strategy is recorded as the experiment focus only.

## Commands

With the app image built (does not restart running services), acquire the
requested window using the existing IEX market-data credentials:

```sh
docker compose run --rm --no-deps mike-trade-research research-data acquire \
  --symbols NVDA,IBM,TSLA --start 2026-07-09T00:00:00Z --end 2026-10-09T00:00:00Z
docker compose run --rm --no-deps mike-trade-research research-data list
docker compose run --rm --no-deps mike-trade-research research-six \
  --start 2026-07-09T00:00:00Z --end 2026-10-09T00:00:00Z \
  --results /app/research/final-six
docker compose run --rm --no-deps mike-trade-research research-backtest \
  --symbol NVDA --strategy vwap_bounce \
  --start 2026-07-09T00:00:00Z --end 2026-10-09T00:00:00Z \
  --results /app/research/final-six
docker compose run --rm --no-deps mike-trade-research research-results \
  --results /app/research/final-six
```

Use `research-backtest --symbol NVDA --strategy vwap_bounce --start ... --end ...`
for one experiment. Acquisition reuses a snapshot that fully covers the request;
otherwise it acquires only uncovered leading/trailing ranges for that symbol.
It never downloads per strategy. An identical complete backtest rerun in the
same result directory verifies the content-addressed ledger and reuses it; a
different result fails closed rather than overwriting the artifact.

## Frozen 100-symbol historical universe (version 1)

`config/research_universe_100_v1.json` is the authoritative frozen member list
(`us-liquid-research-100-v1`, selected 2026-10-10 14:42:28 UTC): 83 US-listed
equities and 17 unleveraged US-listed ETFs. The file records ticker identity at
selection, asset class, a broad exposure group, source links, the methodology,
selection timestamp, Alpaca/IEX/raw provenance, and the exact research window.
Its SHA-256 is fixed in acquisition progress. Editing it after acquisition
starts is refused; publish a new version for a different membership or method.

This is a **selection-date candidate universe**, not historical point-in-time
membership. It intentionally spans technology, finance, consumer, health care,
industrials, energy/materials/utilities, actively traded smaller companies,
broad-market ETFs, and sector ETFs. It excludes OTC listings, leveraged and
inverse funds, duplicate share classes, and recently introduced symbols that
cannot span the one-year window. The stock candidates were checked against
[Nasdaq's stock screener](https://www.nasdaq.com/market-activity/stocks/screener)
on 2026-10-10: all 83 appeared in the current listing export; all 17 ETFs
appeared in Nasdaq's current ETF export. A current share-volume screen found
82 of 83 stocks at or above 500,000 shares; BLK showed 437,376 shares at a
$1,065.35 last sale (roughly $466 million of turnover). This is candidate
metadata, not a historical liquidity guarantee. The ETF identities and
sector roles are drawn from sponsor lists,
including [State Street's sector funds](https://www.ssga.com/us/en/individual/capabilities/equities/sector-investing/select-sector-etfs),
[iShares' fund list](https://www.ishares.com/us/products/etf-investments),
and the listed SPDR market funds. No return, strategy score, or future
profitability was used to select members. A full year of IEX activity is not
asserted until acquisition and verification finish. Smaller names and funds
may have sparse IEX bars, and corporate actions remain unadjusted by design.
Future point-in-time universes need instrument identifiers and per-member
effective intervals, recorded as new immutable universe versions; this v1
file makes no claim about membership before its selection date.

The requested observation interval is `[2025-10-09T00:00:00Z,
2026-10-09T00:00:00Z)`. Alpaca's historical endpoint documents an inclusive
`end`, so the adapter filters returned timestamps to the project's exclusive
end convention. Bars retain the provider's UTC event timestamps. Monthly UTC
request windows are capped at 31 days and paginated with a 10,000-bar page
limit and three-page safety cap; regular NYSE sessions are selected with the
existing New York calendar, including daylight-saving and scheduled early
closes. The feed is explicitly `iex` and the adjustment is explicitly `raw`;
neither is silently substituted. IEX is one venue rather than the consolidated
market, so missing minute bars may reflect no IEX trade rather than an outage.
See [Alpaca's historical bars reference](https://docs.alpaca.markets/us/reference/stockbars)
and [market-data plan limits](https://docs.alpaca.markets/us/docs/about-market-data-api).

Run the new commands from the isolated worktree at
`/home/mike/dev/mike/stocks/python/mike_trade-research-universe`:

```sh
make research-data-plan       # no network request or download
make research-data-preflight  # one bounded five-minute IEX probe
make research-data-status     # progress, failures and per-symbol coverage
make research-data-verify     # manifests, SHA-256, Parquet rows and coverage
```

`make research-data-acquire` is the operator-started foreground command.
The default is one worker and 20 historical requests per minute; at most two
workers and 60 requests/minute can be configured with `RESEARCH_WORKERS` and
`RESEARCH_RPM`. This is deliberately below Alpaca Basic's published 200/minute
historical limit and does not assume a higher account entitlement. Every
HTTP request retains the existing 20-second timeout, and 429, transient 5xx,
and transport failures receive capped exponential backoff with jitter and
bounded retries. An entitlement/permission failure stops the affected window
with its HTTP status and is visible in `status`; it never selects SIP.

For a durable detached run, start a transient **system** unit. It runs as
`mike`, persists independently of the SSH session or an AI session, and logs
to the system journal. The timestamp makes each invocation's unit name
unique; the acquisition's nonblocking file lock prevents simultaneous jobs.
Do not run this command until ready to acquire:

```sh
cd /home/mike/dev/mike/stocks/python/mike_trade-research-universe
sudo systemd-run --unit="mike-trade-research-100-v1-$(date -u +%Y%m%dT%H%M%SZ)" \
  --uid=mike --gid=mike --working-directory="$PWD" --property=Type=exec \
  --remain-after-exit \
  /usr/bin/make research-data-acquire
```

The unit name prints at launch. Monitor progress with `make
research-data-status`, read logs with `sudo journalctl -u UNIT_NAME -f`, and
inspect the process exit with `sudo systemctl show UNIT_NAME -p ActiveState
-p Result -p ExecMainStatus`. A zero exit code means all 100 full-range
snapshots completed with no unresolved window failures. A nonzero exit or a
recorded failure needs review. To resume after interruption, rerun the same
`systemd-run` command (its timestamp gives a new unit name), or run `make
research-data-acquire` in the foreground. No container restart or live
subscription change is involved.

Persistent output is under the existing host
`/home/mike/dev/mike/stocks/python/mike_trade/research/data`, which is mounted
outside disposable research containers. The existing DuckDB catalog and
immutable Parquet/manifests are reused. Each completed monthly window is
published as an immutable snapshot; progress is checkpointed atomically under
`research/data/acquisition/us-liquid-research-100-v1/state.json`. Per-window
coverage files in its `coverage/` directory record the source ticker metadata,
snapshot ID and checksum, observed bar count, first/last provider timestamp,
expected regular-session minutes, and every missing interval. A crash after
snapshot publication but before checkpoint can discover and verify that
snapshot on restart. Existing fully covering snapshots are verified and
reused. Once a symbol's 12 windows are complete, a full-range immutable
snapshot is published for existing `research-backtest` and `research-batch`
selection. The full snapshot ID and checksum are in the progress state.
Failed windows remain listed with symbol/window and error, are retried on the
next acquire run, and cannot erase an earlier valid snapshot.

The theoretical regular-session upper bound is roughly 10 million one-minute
symbol slots (100 symbols × about 252 sessions × 390 minutes); IEX may have
fewer actual bars. Plan for roughly 1–10 GiB of Parquet, manifests, coverage
metadata, and final full-range copies until measured compression is known.
The preflight requires at least 10 GiB free and reports actual free space,
cached windows, and request estimates. The first plan found 9 monthly windows
covered by existing NVDA, IBM, and TSLA snapshots, leaving 1,191 monthly
windows before pagination/retries. The 2026-10-10 bounded preflight queried
the oldest 2025-10-09 five-minute window, returned six NVDA IEX bars, and
confirmed configured data credentials and historical
API access. It does not establish the other 99 symbols' completeness.

For backtests, pin the full-range `dataset_id` from status or the progress
state in the existing experiment JSON. Preserve the universe file/hash,
window coverage files, snapshot manifests, and backtest manifests together.
Future ML work should use the same immutable bars and explicit training/test
time splits; acquisition intentionally computes no hindsight indicators or
features. A study of dates before 2026-10-10 using this selected-today list
has survivorship/selection bias even when every bar checksum verifies.

## R730 research server

The R730 checkout is `/home/mike/dev/mike/stocks/python/mike_trade` on
`mike@192.168.254.147`. DEV remains the source of code and historical data.
The R730 uses the same private GitHub repository as DEV. Its SSH key can read
the repository; no GitHub token is stored in the checkout. For future code
updates, commit and push on DEV, then update the R730 manually:

```sh
ssh mike@192.168.254.147
cd /home/mike/dev/mike/stocks/python/mike_trade
git pull --ff-only origin main
make research-build
```

The initial configuration was transferred directly over SSH. For a fresh
checkout, copy files only if the server does not already have them:

```sh
rsync -a --ignore-existing --chmod=F600 .env mike@192.168.254.147:/home/mike/dev/mike/stocks/python/mike_trade/.env
rsync -a --ignore-existing config/mike_trade.yaml mike@192.168.254.147:/home/mike/dev/mike/stocks/python/mike_trade/config/mike_trade.yaml
ssh mike@192.168.254.147 'cd /home/mike/dev/mike/stocks/python/mike_trade || exit; if ! grep -q "^COMPOSE_FILE=" .env; then printf "\nCOMPOSE_FILE=compose.yaml:compose.r730.yaml\n" >> .env; fi; if ! grep -q "^COMPOSE_PROFILES=" .env; then printf "COMPOSE_PROFILES=trading\n" >> .env; fi; chmod 600 .env'
```

On a fresh R730 checkout, create its own state directory and adapt the copied
non-secret config before starting the engine. Never transfer `state/dev/`:

```sh
ssh mike@192.168.254.147
cd /home/mike/dev/mike/stocks/python/mike_trade
mkdir -p state/r730
python3 - <<'PY'
from pathlib import Path
p = Path('config/mike_trade.yaml')
s = p.read_text()
assert '- name: dev_shadow' in s
assert 'data_dir: /home/mike/dev/mike/stocks/python/mike_trade/state/dev' in s
s = s.replace('- name: dev_shadow', '- name: r730_shadow', 1)
s = s.replace('data_dir: /home/mike/dev/mike/stocks/python/mike_trade/state/dev', 'data_dir: /app/state', 1)
p.write_text(s)
PY
make up
```

For later configuration changes, transfer an incoming copy with `rsync` and
review it against the server settings before replacing them. Keep the server's
`COMPOSE_FILE` and `COMPOSE_PROFILES=trading` settings. Credentials stay in
`.env` and are never committed.

From DEV, `make research-sync-r730` uses rsync over SSH to copy new Parquet
snapshots, manifests, the initial DuckDB catalog, and research artifacts. It
does not delete or overwrite existing server files. It holds the source
catalog lock while transferring and excludes disposable validation runs.
Run `make research-sync-r730 RESEARCH_SSH='ssh -S /path/to/socket'` when using
an authenticated SSH control socket. After later transfers, run the following
on the R730 to backfill its persistent catalog from new manifests:

```sh
docker compose run --rm --no-deps mike-trade-research research-data list
```

The R730 `.env` and `config/mike_trade.yaml` were copied directly from DEV
over SSH during initial deployment. Its local `.env` selects
`COMPOSE_FILE=compose.yaml:compose.r730.yaml` and `COMPOSE_PROFILES=trading`;
preserve these lines when refreshing credentials. The R730 engine and API use
their own `state/r730/` directory and `r730_shadow` scenario. Research jobs
remain independent of these services: the research container has no trading
journal mount or broker order credentials and is run explicitly with
`--no-deps`. Use `make up` only when starting the SHADOW engine and dashboard.

Run a foreground batch and inspect results with:

```sh
make research-six START=2026-07-09T00:00:00Z END=2026-10-09T00:00:00Z WORKERS=auto
make research-batch JOBS=research/jobs.json WORKERS=18
make research-results
docker compose run --rm --no-deps mike-trade-research research-results --results /app/research/parallel-bench/auto
docker ps --filter label=com.docker.compose.service=mike-trade-research
docker stats --no-stream
lscpu
free -h
```

Progress and completion counts print to the terminal. Ctrl-C cancels queued
work and waits for active workers to finish; completed per-run results remain
available. The research container exits when the command finishes. To stop a
running foreground job gracefully, press Ctrl-C in its terminal; `make stop`
stops the engine and dashboard and is not needed for research jobs. Use
`docker ps` afterward to confirm the research container has exited.

## Lifecycle semantics (generation 2)

`BacktestRunner` follows the same lifecycle policy as the SHADOW engine and
calls the same pure functions rather than a private copy:

- Each symbol has its own BUY and SELL collections
  (`BacktestRequest.symbol_strategies` / `symbol_strategy_params`; the global
  `strategy_ids` is the default). Every strategy keeps independent parameters
  and persisted-style state keyed by `(symbol, strategy_id)`; all strategies of
  a symbol read one shared completed-bar context of the last 500 bars
  (`context_history_bars`, parity with the engine's `max_history=500`; a
  smaller value is faster but only valid for strategies that need fewer bars —
  `opening_range_breakout` needs the opening range still in view).
  Strategies that need a timeframe the replay feed does not supply (for
  example the 5-minute `rsi_reversal`) fail the run instead of never firing.
- One position per symbol. BUY strategies are evaluated only while the symbol
  is flat with no entry order working; `arbitrate_entry_decisions` selects at
  most one. Entry cutoff and the per-session entry-cycle cap then gate the
  selected entry (counted per filled cycle).
- SELL strategies are evaluated while a position is held;
  `arbitrate_exit_decisions` applies the minimum hold to strategy exits only,
  refuses a second exit while one is working, and selects one full-remainder
  exit. Rejected candidates are kept in `BacktestResult.decisions`.
- The protective stop is armed after the confirmed entry fill from the
  confirmed average price, as a GTC order (it survives sessions): `fixed`
  (percent or amount), `native_trailing` (percent or amount) or
  `stepped_trailing` (engine-ratcheted through order replacement). A stop never
  moves down. It is cancelled only to make way for a selected strategy exit and
  re-armed if that exit lapses unfilled. `protective_trailing_percent` remains
  an alias for a native trailing percent.
- Every fill records `purpose`, originating `strategy` and, for exits,
  `exit_reason` (`PROTECTIVE_STOP` or `STRATEGY_SELL`); trade records and cycles
  carry `entry_strategy`, `exit_strategy` and `exit_reason`.
- Fills are bar-only: a signal on a completed bar becomes executable at the
  next bar open with the configured adverse slippage; protective stops fill
  intrabar at the stop price (or at the open when it gaps through). Positions
  are not liquidated at session end (end-of-day close is disabled by default,
  as in the engine).

Runs carry `lifecycle_semantics: "2"` in their configuration hash, so they never
alias an earlier artifact. Generation-1 results remain valid and comparable
for the VWAP reference lifecycle: a rerun of NVDA `vwap_bounce` for
2026-07-09..2026-10-09 reproduced the stored ledger fill-for-fill (63 trades,
62 `STRATEGY_SELL` and 1 `PROTECTIVE_STOP` exit). Runs are slower than before
because every strategy validates the 500-bar window.

Known limits: a plugin may appear once per symbol and side; the backtester
sizes entries through the supplied `intent_factory` (the research worker uses
`min($3,000 budget, $30 planned loss / initial stop distance)`), whereas the
SHADOW engine sizes by the per-symbol budget alone; quotes, bid/ask spread and
partial fills beyond the bar-volume participation cap are not modelled.

## Trade inspection artifacts

The dashboard discovers paired `*.manifest.json` and `*.results.json` files
recursively under `MIKE_TRADE_RESEARCH_ROOT` and de-duplicates run IDs. It does
not download data, rewrite artifacts, or copy Parquet into SQLite. Chart bars
are read from the immutable dataset named by `data_revision`/`data_vintage`
under `MIKE_TRADE_RESEARCH_DATASET_ROOT` (default `research/data`) after its
stored SHA-256 is verified.

Artifact schema version 2 adds future trade-inspection evidence: strategy
version, accepted signal ID/time, cycle ID on fills, entry and exit attribution
on cycles, protection mode, initial/final stop, and a compact
`protection_events` list containing initialization and actual moves only.
Arbitration records still preserve every selected and rejected candidate. No
per-bar event stream or redundant price copy is written.

Earlier results remain readable. Their fills and persisted cycle boundaries
produce the common model while absent signal, version, decision, and stop
fields remain null/`UNKNOWN`. Decisions are never recomputed with current
strategy code. If referenced Parquet is absent on the host, chart and excursion
diagnostics state that limitation while recorded fills remain inspectable.
This supports R730 artifacts in place on the R730 and deterministic local DEV
fixtures without copying the R730 datasets.
