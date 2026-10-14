"""Host CPU and memory discovery for research workloads (affinity- and cgroup-aware)."""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "available_cpu_count",
    "available_memory_bytes",
    "auto_worker_ceiling",
    "default_native_threads",
]


def available_cpu_count() -> int:
    """Return CPUs available to this process, honoring affinity and cgroup quota."""
    affinity = (
        len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 1)
    )
    quota_count = affinity
    for quota_path in ("/sys/fs/cgroup/cpu.max", "/sys/fs/cgroup/cpu/cpu.cfs_quota_us"):
        try:
            values = Path(quota_path).read_text().strip().split()
            if quota_path.endswith("cpu.max"):
                quota, period = values
            else:
                quota, period = (
                    values[0],
                    Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text().strip(),
                )
            if quota != "max" and int(quota) > 0:
                quota_count = max(1, int((int(quota) + int(period) - 1) // int(period)))
                break
        except (OSError, ValueError, IndexError):
            continue
    return max(1, min(affinity, quota_count))


def available_memory_bytes() -> int | None:
    """Memory this process may still use: the tighter of the cgroup headroom and the host."""
    candidates: list[int] = []
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            raw = Path(path).read_text().strip()
            if raw != "max":
                limit = int(raw)
                if limit < (1 << 60):
                    current_path = (
                        "/sys/fs/cgroup/memory.current"
                        if path.endswith("memory.max")
                        else "/sys/fs/cgroup/memory/memory.usage_in_bytes"
                    )
                    used = int(Path(current_path).read_text().strip())
                    candidates.append(max(0, limit - used))
                    break
        except (OSError, ValueError):
            pass
    try:  # MemAvailable counts reclaimable page cache; SC_AVPHYS_PAGES does not
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                candidates.append(int(line.split()[1]) * 1024)
                break
    except (OSError, ValueError, IndexError):
        pass
    if not candidates:
        try:
            candidates.append(int(os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")))
        except (ValueError, OSError, AttributeError):
            return None
    return min(candidates)


def auto_worker_ceiling(cpus: int) -> int:
    """CPU-bound worker count for ``auto``: all CPUs minus ~1/8 left for the OS and co-tenants."""
    return max(1, cpus - cpus // 8)


def default_native_threads() -> int:
    """Threads for one native XGBoost/LightGBM training process (it owns the machine's CPUs)."""
    return auto_worker_ceiling(available_cpu_count())
