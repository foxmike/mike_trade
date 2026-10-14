#!/usr/bin/env bash
# Train the two alternative XGBoost BUY experiments, one after the other:
#   5m  : last 10 five-minute candles,   +1.0% before -0.5% within 12 bars (60 min)
#   15m : last  6 fifteen-minute candles, +1.0% before -0.5% within  6 bars (90 min)
#
#   scripts/train_xgboost_intraday_5m_15m.sh
#
# Runs from the repository that contains this script (the isolated research worktree), uses only the
# stored IEX one-minute datasets (candles are derived locally; nothing is downloaded), and uses the
# existing project CLI: model-prepare (pins the exact data) -> model-train -> model-report.
# New model ids/versions are used, an already-stored id@version is never overwritten (the script
# refuses to start), no backtest campaign is launched and nothing is deployed anywhere.
# Exit status: 0 both trained, 1 at least one experiment failed, 2 a precondition is missing.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$ROOT" || exit 2
PY="$ROOT/.venv/bin/python"
MODEL_ROOT="research/models"
LOG_DIR="research/logs"
EXPERIMENTS=(
    "5m|config/modeling/xgboost_buy_5m_tb100_050.yaml"
    "15m|config/modeling/xgboost_buy_15m_tb100_050.yaml"
)

die() { echo "refused: $*" >&2; exit 2; }

[[ -x "$PY" ]] || die "$PY not found (run from the research worktree with its .venv)"
[[ -d research/data/snapshots && -f research/data/catalog.duckdb ]] \
    || die "research/data (the stored IEX datasets) is missing"
mkdir -p "$LOG_DIR" "$MODEL_ROOT"

# One training run at a time; the lock dies with the process.
exec 9>"$MODEL_ROOT/.train_xgboost_intraday_5m_15m.lock"
flock -n 9 || die "another run of this script holds $MODEL_ROOT/.train_xgboost_intraday_5m_15m.lock"

# Validate BOTH configurations and the pre-existing state before training anything.
declare -A MODEL_ID VERSION LOCK_FILE LOG
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
for item in "${EXPERIMENTS[@]}"; do
    name="${item%%|*}"; config="${item#*|}"
    [[ -f "$config" ]] || die "configuration $config not found"
    meta="$("$PY" - "$config" <<'PYEOF'
import sys
from mike_trade.modeling.config import load_experiment_config
from mike_trade.modeling.operations import lock_path
c = load_experiment_config(sys.argv[1])
if c.universe_file is not None and not c.universe_file.is_file():
    raise SystemExit(f"universe file {c.universe_file} not found")
print(c.model_id, c.model_version, lock_path(c))
PYEOF
    )" || die "configuration $config is invalid (see message above)"
    read -r id ver lock <<<"$meta"
    [[ -e "$MODEL_ROOT/manifests/$id/$ver.json" ]] \
        && die "$id@$ver is already stored in $MODEL_ROOT; stored models are never overwritten (bump model_version in $config to retrain)"
    MODEL_ID[$name]="$id"; VERSION[$name]="$ver"; LOCK_FILE[$name]="$lock"
    LOG[$name]="$ROOT/$LOG_DIR/${id}-${STAMP}.log"
done

run_experiment() {
    local name="$1" config="$2" id="${MODEL_ID[$1]}" ver="${VERSION[$1]}" log="${LOG[$1]}"
    {
        echo "### $id@$ver  ($config)  started $(date -u +%FT%TZ)"
        echo "### step 1/4 model-prepare: pin the exact datasets and count the observations"
        nice -n 10 "$PY" -m mike_trade.cli model-prepare --config "$config" || return 1
        echo "### step 2/4 model-train"
        nice -n 10 "$PY" -m mike_trade.cli model-train --config "$config" --lock "${LOCK_FILE[$name]}" \
            || return 1
        echo "### step 3/4 model-report"
        "$PY" -m mike_trade.cli model-report --model-id "$id" --version "$ver" || return 1
        echo "### step 4/4 outcome summary"
        local report_dir="$MODEL_ROOT/reports/$id/$ver"
        mkdir -p "$report_dir"
        "$PY" -m mike_trade.modeling.experiment_summary --root "$MODEL_ROOT" --model-id "$id" \
            --version "$ver" | tee "$report_dir/outcome_summary.txt" || return 1
        echo "### finished $(date -u +%FT%TZ)"
    } >"$log" 2>&1
}

status=0
declare -A RESULT
for item in "${EXPERIMENTS[@]}"; do
    name="${item%%|*}"; config="${item#*|}"
    echo "[$(date -u +%T)] training $name -> ${MODEL_ID[$name]}@${VERSION[$name]}  (log ${LOG[$name]})"
    if run_experiment "$name" "$config"; then RESULT[$name]="OK"; else RESULT[$name]="FAILED"; status=1; fi
    echo "[$(date -u +%T)] $name: ${RESULT[$name]}"
done

echo
echo "================ summary ================"
for item in "${EXPERIMENTS[@]}"; do
    name="${item%%|*}"; id="${MODEL_ID[$name]}"; ver="${VERSION[$name]}"
    echo "$name  ${RESULT[$name]}  $id@$ver"
    echo "   log       : ${LOG[$name]}"
    if [[ "${RESULT[$name]}" == "OK" ]]; then
        echo "   manifest  : $ROOT/$MODEL_ROOT/manifests/$id/$ver.json"
        echo "   artifacts : $ROOT/$MODEL_ROOT/artifacts/<artifact_sha256>  (see manifest)"
        echo "   lock      : ${LOCK_FILE[$name]}"
        echo "   report    : $ROOT/$MODEL_ROOT/reports/$id/$ver/{model_report.md,model_report.json,outcome_summary.txt,favorable_deciles.csv}"
    else
        echo "   (see the log for the failing step; nothing stored by a failed step is overwritten)"
    fi
done
exit "$status"
