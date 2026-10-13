#!/usr/bin/env bash
# Research-only launcher for the nine Michelle stop-clarification development backtests.
# Safe under nohup: no TTY or stdin is required, output is mirrored to <results>/run.log.
# Never starts trading services, never touches SHADOW/PAPER/LIVE state, never runs the holdout,
# and never writes into any existing results directory.
#
#   nohup research/campaigns/michelle_stop_clarification_oct2026/run_michelle_stop_clarification.sh \
#     > /dev/null 2>&1 &
#
# WORKERS (default 4) sets the process-pool size.

set -u

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
campaign_rel="research/campaigns/michelle_stop_clarification_oct2026"
baseline_rel="research/results/michelle_zero_slippage_oct2026/run-20261013T170714Z-3627337"
results_base_rel="research/results/michelle_stop_clarification_oct2026"
workers="${WORKERS:-4}"
overall_status=0

exec < /dev/null

if [[ ! "$workers" =~ ^[1-9][0-9]*$ ]]; then
  printf 'WORKERS must be a positive integer; received %s\n' "$workers"
  exit 2
fi
if [[ ! -f "$project_root/$baseline_rel/batch.json" ]]; then
  printf 'Baseline results are missing: %s\nRefusing to run without the comparison baseline.\n' \
    "$project_root/$baseline_rel"
  exit 2
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
result_rel="${results_base_rel}/run-${timestamp}-$$"
result_host="$project_root/$result_rel"
mkdir -p "$project_root/$results_base_rel"
if ! mkdir "$result_host"; then
  printf 'Could not create unique results directory: %s\n' "$result_host"
  exit 1
fi
exec > >(tee -a "$result_host/run.log") 2>&1

printf 'Michelle stop clarification | 9 experiments | workers: %s\n' "$workers"
printf 'Results: %s\nBaseline (OLD): %s\n' "$result_host" "$project_root/$baseline_rel"

git_commit="$(cd "$project_root" && git rev-parse HEAD 2>/dev/null || echo unknown)"
git_dirty="$(cd "$project_root" && git status --porcelain --untracked-files=no 2>/dev/null | wc -l)"
cp "$project_root/$campaign_rel/experiments.json" "$result_host/experiments.json"
cp "$project_root/$campaign_rel/campaign_manifest.json" "$result_host/campaign_manifest.json"
# Hash the stop-related sources of this working tree; the container must carry identical copies.
python3 - "$project_root" "$result_host" "$timestamp" "$workers" "$git_commit" "$git_dirty" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

root, out, started, workers, commit, dirty = sys.argv[1:]
manifest = json.loads((Path(out) / "campaign_manifest.json").read_text())
hashes = {rel: hashlib.sha256((Path(root) / rel).read_bytes()).hexdigest()
          for rel in manifest["stop_code_files"]}
(Path(out) / "code_hashes.json").write_text(json.dumps(hashes, indent=2))
json.dump({"campaign": manifest["campaign"], "started_utc": started, "workers": int(workers),
           "git_commit": commit, "tracked_files_modified": int(dirty), "holdout_jobs": 0},
          open(Path(out) / "run_info.json", "w"), indent=2)
PY
printf 'Code revision: %s (tracked modifications: %s)\n' "$git_commit" "$git_dirty"

container_jobs="/app/$campaign_rel/experiments.json"
container_manifest="/app/$campaign_rel/campaign_manifest.json"

if (cd "$project_root" && docker compose build mike-trade-research); then
  # Fail closed unless the container carries exactly this working tree's stop code and the jobs
  # are exact; no experiment starts otherwise.
  if (cd "$project_root" && docker compose run -T --rm --no-deps \
        --entrypoint python mike-trade-research \
        "/app/$campaign_rel/validate_campaign.py" \
        --experiments "$container_jobs" --manifest "$container_manifest" \
        --code-check --code-hashes "/app/$result_rel/code_hashes.json"); then
    (cd "$project_root" && docker compose run -T --rm --no-deps \
      mike-trade-research research-batch \
      --jobs "$container_jobs" \
      --root /app/research/data \
      --results "/app/$result_rel" \
      --output "/app/$result_rel/batch.json" \
      --workers "$workers")
    if [[ $? -ne 0 ]]; then
      overall_status=1
    fi
    if [[ -f "$result_host/batch.json" ]]; then
      (cd "$project_root" && docker compose run -T --rm --no-deps \
        --entrypoint python mike-trade-research \
        "/app/$campaign_rel/report_results.py" \
        --batch "/app/$result_rel/batch.json" \
        --experiments "$container_jobs" \
        --manifest "$container_manifest" \
        --results "/app/$result_rel" \
        --baseline "/app/$baseline_rel")
      if [[ $? -ne 0 ]]; then
        overall_status=1
      fi
      python3 - "$result_host/batch.json" <<'PY'
import json
import sys

batch = json.load(open(sys.argv[1], encoding="utf-8"))
print(f"Final batch: completed={batch['completed_count']} failed={batch['failed_count']} "
      f"workers_used={batch['workers_used']}")
PY
    else
      printf 'No batch.json was produced; inspect %s\n' "$result_host"
      overall_status=1
    fi
  else
    printf 'Campaign or container-code validation failed. No experiments were run.\n'
    overall_status=1
  fi
else
  printf 'Research image build failed. No experiments were run; trading services were not touched.\n'
  overall_status=1
fi

if [[ "$overall_status" -eq 0 ]]; then
  printf 'Campaign finished successfully. Report: %s/comparison.md\n' "$result_host"
else
  printf 'Campaign finished with failures. Inspect %s (run.log, batch.json, comparison.md)\n' "$result_host"
fi
exit "$overall_status"
