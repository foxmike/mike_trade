#!/usr/bin/env bash
# Backtest the two intraday candle-window XGBoost BUY models on IBM and TSLA.
#
#   scripts/backtest_xgboost_intraday_ibm_tsla.sh [--dry-run]
#
# Four independent jobs, run concurrently (4 workers) through the existing model-backtest campaign
# runner on the real strategy / lifecycle / simulator / native trailing-stop code:
#
#   IBM  + xgboost_buy_5m_tb100_050@1.0.0      TSLA + xgboost_buy_5m_tb100_050@1.0.0
#   IBM  + xgboost_buy_15m_tb100_050@1.0.0     TSLA + xgboost_buy_15m_tb100_050@1.0.0
#
# MODEL_DIRECTED decisions (favorable vs adverse probability, no thresholds), 0.5% trailing stop,
# 15:55 end-of-day liquidation, no SELL strategy, 2 bps slippage per side, 10,000 starting capital
# per run, window 2026-08-10..2026-09-21 (the models' out-of-sample evaluation window), JEV off.
# The +1% training barrier is NOT a take-profit order.  Nothing is trained or downloaded.
#
# Results (campaign output, then the consolidated comparison):
#   research/analysis/xgboost_intraday_ibm_tsla/campaign/xgboost_intraday_ibm_tsla_v1/<sha12>/
#   research/analysis/xgboost_intraday_ibm_tsla/{comparison.md,comparison.csv,comparison.json}
#   research/analysis/xgboost_intraday_ibm_tsla/runs/<SYMBOL>_<CONFIG>/{trades.csv,summary.json}
#   research/logs/xgboost_intraday_ibm_tsla-<UTC stamp>.log
#
# Exit status: 0 all four runs completed; 1 a run failed (partial report still written);
#              2 a precondition is missing; 3 another copy is running; 5 results already exist.
set -euo pipefail

RESEARCH_ROOT="${RESEARCH_ROOT:-/home/mike/dev/mike/stocks/python/mike_trade_ml}"
CAMPAIGN="config/modeling/campaigns/xgboost_intraday_ibm_tsla_v1.yaml"
NAME="xgboost_intraday_ibm_tsla"
OUT_ROOT="research/analysis/xgboost_intraday_ibm_tsla"
MODELS=(xgboost_buy_5m_tb100_050 xgboost_buy_15m_tb100_050)
WORKERS=4
NICE="${NICE_LEVEL:-10}"
DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

SCRIPT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
if [[ "$SCRIPT_ROOT" != "$(cd "$RESEARCH_ROOT" && pwd -P)" ]]; then
    echo "refused: run this from the research worktree $RESEARCH_ROOT (script is in $SCRIPT_ROOT)" >&2
    exit 2
fi
cd "$SCRIPT_ROOT"
PY="$SCRIPT_ROOT/.venv/bin/python"
die() { echo "refused: $*" >&2; exit 2; }

[[ -x "$PY" ]] || die "$PY not found"
[[ -f "$CAMPAIGN" ]] || die "$CAMPAIGN not found"
[[ -f research/data/catalog.duckdb || -d research/data/snapshots ]] || die "research/data has no stored datasets"
for model in "${MODELS[@]}"; do
    [[ -f "research/models/manifests/$model/1.0.0.json" ]] \
        || die "model $model@1.0.0 is not trained (research/models/manifests/$model/1.0.0.json)"
done
# JEV (decision review) must stay off for these tests.
if grep -Eq '^[[:space:]]*decision_review[[:space:]]*:' "$CAMPAIGN"; then
    die "$CAMPAIGN configures decision_review; JEV must be disabled for these tests"
fi

EXPECT=(
    --expect slippage_bps=2
    --expect trail_percent=0.5
    --expect planned_jobs=4
    --expect feed=iex
    --expect workers="$WORKERS"
    --expect model_directed=true
    --expect trading_instruments=2
    --expect configurations=2
    --expect evaluation_start=2026-08-10T00:00:00Z
    --expect evaluation_end=2026-09-21T00:00:00Z
)

# One copy at a time (the lock dies with this process).
mkdir -p research/logs "$OUT_ROOT"
exec 9>"$OUT_ROOT/.${NAME}.lock"
if ! flock -n 9; then
    echo "refused: $NAME is already running (lock $OUT_ROOT/.${NAME}.lock)" >&2
    exit 3
fi

# Validate everything and resolve the output directory; nothing starts if a setting differs.
PREFLIGHT="$("$PY" -m mike_trade.cli model-backtest --campaign "$CAMPAIGN" --workers "$WORKERS" \
    --dry-run "${EXPECT[@]}")" || { echo "$PREFLIGHT" >&2; echo "refused: preflight failed" >&2; exit 4; }
RUN_DIR="$(printf '%s\n' "$PREFLIGHT" | awk '/^  output directory/ {print $3}')"
[[ -n "$RUN_DIR" ]] || die "could not resolve the campaign output directory"
printf '%s\n' "$PREFLIGHT" | sed -n '1,/^{/p' | sed '$d'
if [[ "$DRY_RUN" == 1 ]]; then
    echo "dry run only: validated 4 jobs, output would go to $RUN_DIR"
    exit 0
fi
if [[ -e "$RUN_DIR/batch.json" ]]; then
    echo "refused: $RUN_DIR already holds a completed run; previous results are preserved" >&2
    echo "         (change the campaign config, or move that directory, to run again)" >&2
    exit 5
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="$SCRIPT_ROOT/research/logs/${NAME}-${STAMP}.log"
echo "log: $LOG"
echo "starting 4 jobs with $WORKERS workers at nice $NICE (single-threaded inference per worker)"

# Each worker is one process; keep native libraries to a single thread so four jobs cannot
# oversubscribe the host or disturb the production services.
export OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
set +e
nice -n "$NICE" "$PY" -m mike_trade.cli model-backtest --campaign "$CAMPAIGN" --workers "$WORKERS" \
    "${EXPECT[@]}" 2>&1 | tee "$LOG"
status="${PIPESTATUS[0]}"
set -e

if [[ -f "$RUN_DIR/report/trades.csv" && -f "$RUN_DIR/report/per_symbol.csv" ]]; then
    "$PY" -m mike_trade.modeling.candle_backtest_report --campaign-dir "$RUN_DIR" --out "$OUT_ROOT" \
        2>&1 | tee -a "$LOG"
else
    echo "no campaign report was written (campaign exit status $status); see $LOG" >&2
    exit 1
fi

echo
echo "campaign output : $RUN_DIR"
echo "comparison      : $SCRIPT_ROOT/$OUT_ROOT/comparison.md (+ .csv, .json)"
echo "per-run results : $SCRIPT_ROOT/$OUT_ROOT/runs/<SYMBOL>_<CONFIG>/{trades.csv,summary.json}"
echo "log             : $LOG"
[[ "$status" == 0 ]] || { echo "campaign finished with exit status $status (a job failed)" >&2; exit 1; }
