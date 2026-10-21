#!/usr/bin/env bash

# Operator-started, resumable repair of the frozen 100-symbol IEX archive.
# Redirect this script under nohup when the terminal may disconnect.

PROJECT_ROOT="/home/mike/dev/mike/stocks/python/mike_trade"
DATA_ROOT="$PROJECT_ROOT/research/data"
UNIVERSE="$PROJECT_ROOT/config/research_universe_100_v1.json"
CAMPAIGN="$PROJECT_ROOT/config/research_campaigns/michelle_corrected_vwap_iex_tsla_2024_2025_v2.json"
PYTHON="$PROJECT_ROOT/.venv/bin/python"
LOCK_FILE="$DATA_ROOT/.research-universe-iex-repair.lock"
WORKERS="${REPAIR_WORKERS:-4}"
REQUESTS_PER_MINUTE="${REPAIR_RPM:-180}"
MAX_ATTEMPTS="${REPAIR_MAX_ATTEMPTS:-5}"

cd "$PROJECT_ROOT" || {
  echo "ERROR: cannot enter $PROJECT_ROOT" >&2
  exit 1
}

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "ERROR: another research-universe IEX repair is already running" >&2
  exit 2
fi

set -a
# shellcheck disable=SC1091
. "$PROJECT_ROOT/.env"
set +a

printf '[repair] universe=%s symbols=100 workers=%s global_rpm=%s attempts=%s\n' \
  "$UNIVERSE" "$WORKERS" "$REQUESTS_PER_MINUTE" "$MAX_ATTEMPTS"
echo "[repair] window=2024-10-09T00:00:00Z..2026-10-09T00:00:00Z (end exclusive)"
echo "[repair] provider=alpaca feed=iex timeframe=1m adjustment=raw archive_gate=RTH_sessions"

"$PYTHON" -m mike_trade.cli research-data repair-universe \
  --universe "$UNIVERSE" \
  --start 2024-10-09T00:00:00Z \
  --end 2026-10-09T00:00:00Z \
  --workers "$WORKERS" \
  --requests-per-minute "$REQUESTS_PER_MINUTE" \
  --max-attempts "$MAX_ATTEMPTS" \
  --root "$DATA_ROOT" \
  --update-campaign "$CAMPAIGN"
status=$?

if [ "$status" -eq 0 ]; then
  echo "[repair] SUCCESS: all 100 symbols passed observed-session integrity checks"
else
  echo "[repair] FAILED (exit=$status); inspect structured progress and retry safely" >&2
fi

exit "$status"
