"""Incremental catalog discovery must not block research API reads."""

import json
import shutil
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from mike_trade.research.run_catalog import ResearchRunCatalog, ResearchRunQuery


def write_run(root: Path, name: str, equity: str = "10001") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    manifest = root / f"{name}.manifest.json"
    manifest.write_text(json.dumps({
        "run_id": name, "execution_id": f"execution-{name}", "symbols": ["IBM"],
        "starting_capital": "10000", "strategies": [], "period_role": "RESEARCH",
        "window_start": "2026-07-09T00:00:00Z", "window_end": "2026-09-21T04:00:00Z",
    }))
    manifest.with_name(f"{name}.results.json").write_text(json.dumps({
        "cycles": [], "fills": [], "equity_curve": [{"equity": equity}],
    }))
    return manifest


def test_unchanged_sync_reuses_summaries_and_discovers_only_changed_runs(tmp_path):
    root = tmp_path / "research"
    first = write_run(root, "first")
    catalog = ResearchRunCatalog(root, tmp_path / "index.duckdb")
    assert catalog.sync() == 1
    from mike_trade.research.run_catalog import build_trade_records

    with patch("mike_trade.research.run_catalog.build_trade_records", wraps=build_trade_records) as build:
        assert catalog.sync() == 1
        assert build.call_count == 0
        write_run(root, "second")
        assert catalog.sync() == 2
        assert build.call_count == 1
        write_run(root, "first", equity="10009")
        assert catalog.sync() == 2
        assert build.call_count == 2
        assert catalog.get_run("first")["net_pnl"] == "9.000000"
    first.unlink()
    assert catalog.sync() == 1
    assert catalog.get_run("first") is None
    assert catalog.get_run("second") is not None


def test_unchanged_sync_does_not_reparse_json_or_write_index(tmp_path):
    root = tmp_path / "research"
    write_run(root, "first")
    catalog = ResearchRunCatalog(root, tmp_path / "index.duckdb")
    catalog.sync()
    with catalog._lock:
        con = catalog._get_connection()
        before = con.execute("SELECT indexed_at FROM research_runs_index").fetchone()
        catalog._release(con)
    with patch.object(Path, "read_text", side_effect=AssertionError("Unexpected artifact parse")):
        assert catalog.sync() == 1
    with catalog._lock:
        con = catalog._get_connection()
        assert con.execute("SELECT indexed_at FROM research_runs_index").fetchone() == before
        catalog._release(con)


@pytest.mark.parametrize("persistent", [False, True])
def test_initial_population_and_expired_refresh_never_block_reads(tmp_path, persistent):
    root = tmp_path / "research"
    write_run(root, "first")
    catalog = ResearchRunCatalog(root, tmp_path / "index.duckdb" if persistent else None)
    entered, release = threading.Event(), threading.Event()
    from mike_trade.research.run_catalog import build_trade_records

    def slow_records(*args):
        entered.set()
        assert release.wait(5)
        return build_trade_records(*args)

    with patch("mike_trade.research.run_catalog.build_trade_records", side_effect=slow_records):
        try:
            started = time.monotonic()
            assert catalog.query_runs(ResearchRunQuery()).total == 0
            assert time.monotonic() - started < 1
            assert entered.wait(3)
            worker = catalog._refresh_thread
            assert catalog.query_runs(ResearchRunQuery()).total == 0
            assert catalog._refresh_thread is worker  # One population worker only.
        finally:
            release.set()
            catalog._refresh_thread.join(5)
    assert catalog.query_runs(ResearchRunQuery()).total == 1
    write_run(root, "second")
    catalog._last_sync_time = 0
    entered.clear()
    release.clear()
    with patch("mike_trade.research.run_catalog.build_trade_records", side_effect=slow_records):
        try:
            assert catalog.query_runs(ResearchRunQuery()).total == 1
            assert entered.wait(3)
            assert catalog.get_run("first") is not None
        finally:
            release.set()
            catalog._refresh_thread.join(5)
    assert catalog.query_runs(ResearchRunQuery()).total == 2


def test_empty_catalog_refresh_is_throttled_and_persisted_catalog_remains_readable(tmp_path):
    root = tmp_path / "research"
    root.mkdir()
    catalog = ResearchRunCatalog(root, tmp_path / "index.duckdb")
    assert catalog.sync() == 0
    with patch.object(catalog, "sync", side_effect=AssertionError("Unexpected rescan")):
        for _ in range(3):
            assert catalog.query_runs(ResearchRunQuery()).total == 0
    write_run(root, "first")
    catalog.sync()
    reopened = ResearchRunCatalog(root, catalog.catalog_path)
    assert reopened.query_runs(ResearchRunQuery()).total == 1
    reopened._refresh_thread.join(5)


def test_duplicate_artifacts_and_legacy_variant_changes_remain_correct(tmp_path):
    root = tmp_path / "research"
    first = write_run(root / "a", "first")
    data = json.loads(first.read_text())
    data.pop("execution_id")
    first.write_text(json.dumps(data))
    catalog = ResearchRunCatalog(root, tmp_path / "index.duckdb")
    assert catalog.sync() == 1
    duplicate = root / "b"
    duplicate.mkdir()
    for f in first.parent.iterdir():
        shutil.copyfile(f, duplicate / f.name)
    assert catalog.sync() == 1
    assert catalog.get_run("first")["duplicate_artifact_count"] == 2
    (duplicate / "first.results.json").write_text(json.dumps({"equity_curve": [{"equity": "10002"}]}))
    assert catalog.sync() == 2
    assert all(r["selection_id"].startswith("legacy-first-") for r in catalog.query_runs(ResearchRunQuery()).items)
    shutil.rmtree(duplicate)
    assert catalog.sync() == 1
    assert catalog.get_run("first")["duplicate_artifact_count"] == 1
