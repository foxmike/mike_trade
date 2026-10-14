from __future__ import annotations

import pytest

from mike_trade.research.commands import ExperimentSpec, resolve_worker_count


def test_worker_count_is_bounded_by_jobs_and_positive() -> None:
    assert resolve_worker_count(4, 2) == 2
    assert resolve_worker_count(1, 6) == 1
    assert 1 <= resolve_worker_count("auto", 6) <= 6


def test_explicit_worker_count_respects_memory_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    import mike_trade.research.commands as commands

    monkeypatch.setattr(commands, "available_cpu_count", lambda: 8)
    monkeypatch.setattr(commands, "_available_memory_bytes", lambda: 2 * 512 * 1024 * 1024)
    assert resolve_worker_count(6, 10) == 2
    assert resolve_worker_count("auto", 10) == 2


def test_auto_workers_reserve_cpu_headroom_and_explicit_counts_are_honoured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mike_trade.research.commands as commands

    monkeypatch.setattr(commands, "available_cpu_count", lambda: 72)
    monkeypatch.setattr(commands, "_available_memory_bytes", lambda: 256 * 1024**3)
    assert resolve_worker_count("auto", 800) == 63
    assert resolve_worker_count(40, 800) == 40
    assert resolve_worker_count(200, 800) == 72  # never beyond the CPUs
    assert resolve_worker_count("auto", 800, memory_per_worker_bytes=8 * 1024**3) == 32
    assert resolve_worker_count(1, 800) == 1


def test_batch_children_inherit_single_thread_native_pools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    from mike_trade.research.commands import _single_threaded_workers

    monkeypatch.delenv("OMP_NUM_THREADS", raising=False)
    monkeypatch.setenv("OPENBLAS_NUM_THREADS", "4")  # an operator's value is kept
    with _single_threaded_workers():
        assert os.environ["OMP_NUM_THREADS"] == "1"
        assert os.environ["OPENBLAS_NUM_THREADS"] == "4"
    assert "OMP_NUM_THREADS" not in os.environ


@pytest.mark.parametrize(
    "spec",
    [
        ExperimentSpec("NVDA", "vwap_bounce", "2026-07-09T00:00:00Z", "2026-10-09T00:00:00Z"),
        ExperimentSpec(
            "IBM", "vwap_breakdown", "2026-07-09T00:00:00+00:00", "2026-10-09T00:00:00+00:00", 42
        ),
    ],
)
def test_experiment_spec_accepts_reproducible_inputs(spec: ExperimentSpec) -> None:
    spec.validate()


def test_experiment_spec_rejects_unsupported_strategy_and_unbounded_dates() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        ExperimentSpec("NVDA", "unknown", "2026-07-09T00:00:00Z", "2026-10-09T00:00:00Z").validate()
    with pytest.raises(ValueError, match="timezone"):
        ExperimentSpec("NVDA", "vwap_bounce", "2026-07-09", "2026-10-09T00:00:00Z").validate()
