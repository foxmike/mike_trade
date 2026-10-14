#!/usr/bin/env bash
# Launch the corrected model-directed 792-backtest campaign in the background.
#
#   scripts/run_model_directed_corrected.sh
#
# Runs only from the isolated R730 research worktree, verifies the EFFECTIVE settings
# (2 bps/side slippage, 0.5% trailing stop, 792 jobs, iex, 16 workers, model-directed) before any
# job starts, refuses a duplicate or already-completed run, detaches from the terminal, and logs
# every failed job immediately (batch.failures.*.jsonl next to batch.json plus the progress log).
set -euo pipefail

RESEARCH_ROOT="${RESEARCH_ROOT:-/home/mike/dev/mike/stocks/python/mike_trade_ml}"
CAMPAIGN="config/modeling/campaigns/model_strategy_comparison_model_directed_corrected_v1.yaml"
NAME="model_directed_corrected_v1"
WORKERS=16

SCRIPT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
if [[ "$SCRIPT_ROOT" != "$(cd "$RESEARCH_ROOT" && pwd -P)" ]]; then
    echo "refused: run this from the research worktree $RESEARCH_ROOT (script is in $SCRIPT_ROOT)" >&2
    exit 2
fi
cd "$SCRIPT_ROOT"
PY="$SCRIPT_ROOT/.venv/bin/python"
[[ -x "$PY" ]] || { echo "refused: $PY not found" >&2; exit 2; }
[[ -f "$CAMPAIGN" ]] || { echo "refused: $CAMPAIGN not found" >&2; exit 2; }

EXPECT=(
    --expect slippage_bps=2
    --expect trail_percent=0.5
    --expect planned_jobs=792
    --expect feed=iex
    --expect workers="$WORKERS"
    --expect model_directed=true
    --expect trading_instruments=99
    --expect excluded_reference_instruments=SPY
    --expect configurations=8
    --expect evaluation_start=2026-08-10T00:00:00Z
    --expect evaluation_end=2026-09-21T00:00:00Z
)

mkdir -p research/logs research/model_campaigns
# One copy at a time: the lock is inherited by the background process and dies with it.
exec 9>"research/model_campaigns/.${NAME}.lock"
if ! flock -n 9; then
    echo "refused: $NAME is already running (lock research/model_campaigns/.${NAME}.lock)" >&2
    exit 3
fi

# Verify the effective settings and 792 jobs; nothing is started if any differs.
PREFLIGHT="$("$PY" -m mike_trade.cli model-backtest --campaign "$CAMPAIGN" --workers "$WORKERS" \
    --dry-run "${EXPECT[@]}")" || { echo "$PREFLIGHT" >&2; echo "refused: preflight failed" >&2; exit 4; }
OUTPUT_DIR="$(printf '%s\n' "$PREFLIGHT" | awk '/^  output directory/ {print $3}')"
if [[ -z "$OUTPUT_DIR" ]]; then echo "refused: could not resolve the output directory" >&2; exit 4; fi
if [[ -e "$OUTPUT_DIR/batch.json" ]]; then
    echo "refused: $OUTPUT_DIR already holds a completed run; previous results are preserved" >&2
    exit 5
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="$SCRIPT_ROOT/research/logs/${NAME}-${STAMP}.log"
{
    printf '%s\n' "$PREFLIGHT"
    echo "launched $STAMP; failures are appended to $OUTPUT_DIR/batch.failures.*.jsonl as they happen"
} > "$LOG"

setsid nohup "$PY" -m mike_trade.cli model-backtest --campaign "$CAMPAIGN" --workers "$WORKERS" \
    "${EXPECT[@]}" >> "$LOG" 2>&1 < /dev/null &
PID=$!
ln -sfn "$LOG" "$SCRIPT_ROOT/research/logs/${NAME}.latest.log"
echo "$PID" > "research/logs/${NAME}.pid"
echo "pid:    $PID"
echo "log:    $LOG"
echo "output: $OUTPUT_DIR"
echo "monitor: tail -f $SCRIPT_ROOT/research/logs/${NAME}.latest.log"
