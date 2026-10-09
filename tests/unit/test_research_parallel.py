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
