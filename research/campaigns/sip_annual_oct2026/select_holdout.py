"""Pin an operator-selected subset of reserved holdout jobs before execution."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--configurations", required=True,
                        help="comma-separated configuration names chosen from development results")
    args = parser.parse_args()
    selected = {name.strip() for name in args.configurations.split(",") if name.strip()}
    if not selected:
        raise ValueError("select at least one configuration")
    all_jobs = json.loads(args.source.read_text())["experiments"]
    available = {job["strategy"] for job in all_jobs}
    unknown = selected - available
    if unknown:
        raise ValueError(f"unknown configuration names: {sorted(unknown)}")
    jobs = [job for job in all_jobs if job["strategy"] in selected]
    if len(jobs) != 3 * len(selected):
        raise ValueError("selected holdout does not contain all three symbols")
    args.output.write_text(json.dumps({"experiments": jobs}, indent=2) + "\n")
    print(f"Selected {len(selected)} configurations and {len(jobs)} reserved holdout jobs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
