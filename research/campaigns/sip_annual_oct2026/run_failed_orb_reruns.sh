#!/usr/bin/env bash
# Run only the four annual ORB jobs that failed in the original development batch.

set -u

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
campaign_rel="research/campaigns/sip_annual_oct2026"
jobs_file="failed_orb_rerun_experiments.json"
workers="${WORKERS:-4}"
overall_status=0

if [[ ! "$workers" =~ ^[1-9][0-9]*$ ]]; then
  printf 'WORKERS must be a positive integer; received %s\n' "$workers"
  overall_status=2
else
  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  result_rel="research/results/sip_annual_oct2026/orb-rerun-${timestamp}-$$"
  result_host="$project_root/$result_rel"
  mkdir -p "$project_root/research/results/sip_annual_oct2026"
  if mkdir "$result_host"; then
    printf 'ORB corrective rerun: 4 jobs | workers: %s | results: %s\n' \
      "$workers" "$result_host"
    if (cd "$project_root" && docker compose build mike-trade-research); then
      (cd "$project_root" && docker compose run --rm --no-deps \
        mike-trade-research research-batch \
        --jobs "/app/$campaign_rel/$jobs_file" \
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
          --experiments "/app/$campaign_rel/$jobs_file" \
          --manifest "/app/$campaign_rel/campaign_manifest.json" \
          --results "/app/$result_rel")
        summary_status=$?
        if [[ "$summary_status" -ne 0 ]]; then
          overall_status=1
        fi
        python3 - "$result_host/batch.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    batch = json.load(stream)
print(f"Final ORB rerun: completed={batch['completed_count']} "
      f"failed={batch['failed_count']} workers_used={batch['workers_used']}")
PY
      else
        printf 'Batch summary was not produced; inspect %s\n' "$result_host"
        overall_status=1
      fi
    else
      printf 'Research image build failed. Trading services were not started.\n'
      overall_status=1
    fi
    if [[ "$overall_status" -eq 0 ]]; then
      printf 'ORB corrective rerun completed: %s/summary.md\n' "$result_host"
    else
      printf 'ORB corrective rerun finished with failures. Inspect %s\n' "$result_host"
    fi
  else
    printf 'Could not create unique results directory: %s\n' "$result_host"
    overall_status=1
  fi
fi

test "$overall_status" -eq 0
