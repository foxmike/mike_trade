#!/usr/bin/env bash
# Interactive research-only batch launcher. Does not start trading services.

set -u

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
campaign_rel="research/campaigns/sip_annual_oct2026"
phase="${1:-development}"
selected_configs="${2:-${SELECTED_CONFIGS:-}}"
workers="${WORKERS:-4}"
overall_status=0

if [[ "$phase" != "development" && "$phase" != "holdout" ]]; then
  printf 'Usage: %s [development|holdout "name1,name2"]\n' "$0"
  overall_status=2
elif [[ ! "$workers" =~ ^[1-9][0-9]*$ ]]; then
  printf 'WORKERS must be a positive integer; received %s\n' "$workers"
  overall_status=2
elif [[ "$phase" == "holdout" && -z "$selected_configs" ]]; then
  printf 'Holdout requires configuration names selected from development results.\n'
  overall_status=2
else
  jobs_file="experiments.json"
  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  result_rel="research/results/sip_annual_oct2026/${phase}-${timestamp}-$$"
  result_host="$project_root/$result_rel"
  mkdir -p "$project_root/research/results/sip_annual_oct2026"
  if mkdir "$result_host"; then
    printf 'Campaign: %s | workers: %s | results: %s\n' "$phase" "$workers" "$result_host"
    jobs_container="/app/$campaign_rel/$jobs_file"
    if [[ "$phase" == "holdout" ]]; then
      if python3 "$project_root/$campaign_rel/select_holdout.py" \
        --source "$project_root/$campaign_rel/holdout_experiments.json" \
        --output "$result_host/selected_holdout_experiments.json" \
        --configurations "$selected_configs"; then
        jobs_container="/app/$result_rel/selected_holdout_experiments.json"
      else
        overall_status=2
      fi
    fi
    if [[ "$overall_status" -eq 0 ]] && (cd "$project_root" && docker compose build mike-trade-research); then
      (cd "$project_root" && docker compose run --rm --no-deps \
        mike-trade-research research-batch \
        --jobs "$jobs_container" \
        --root /app/research/data \
        --results "/app/$result_rel" \
        --output "/app/$result_rel/batch.json" \
        --workers "$workers")
      batch_status=$?
      if [[ "$batch_status" -ne 0 ]]; then
        overall_status=1
      fi
      if [[ -f "$result_host/batch.json" ]]; then
        (cd "$project_root" && docker compose run --rm --no-deps \
          --entrypoint python mike-trade-research \
          "/app/$campaign_rel/summarize_results.py" \
          --batch "/app/$result_rel/batch.json" \
          --experiments "$jobs_container" \
          --manifest "/app/$campaign_rel/campaign_manifest.json" \
          --results "/app/$result_rel")
        summary_status=$?
        if [[ "$summary_status" -ne 0 ]]; then
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
        printf 'Batch summary was not produced; completed immutable artifacts, if any, remain in %s\n' "$result_host"
        overall_status=1
      fi
    else
      printf 'Research image build or holdout selection failed. Trading services were not started.\n'
      overall_status=1
    fi
    if [[ "$overall_status" -eq 0 ]]; then
      printf 'Campaign prepared results successfully: %s/summary.md\n' "$result_host"
    else
      printf 'Campaign finished with failures. Inspect %s\n' "$result_host"
    fi
  else
    printf 'Could not create unique results directory: %s\n' "$result_host"
    overall_status=1
  fi
fi

test "$overall_status" -eq 0
