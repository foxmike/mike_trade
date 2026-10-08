"""Unit tests for mike_trade.backtest.commands (operator backtest CLI layer).

These tests never run a real backtest: ``run`` uses a fake runner that only
records the request, and ``list``/``show``/``export`` read from a fake
results store (fixture manifest + results files under ``tmp_path``) through
the real read-only :class:`BacktestResultsIndex`.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import socket
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from mike_trade.backtest import commands as bt
from mike_trade.backtest.commands import BacktestResultsIndex
from mike_trade.backtest.manifest import PeriodRole, RunManifest, write_manifest

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Fakes and fixtures
# ---------------------------------------------------------------------------


class FakeRunner:
    """Stand-in for the application-layer runner; records requests, runs nothing."""

    def __init__(self, run_id: str = "fake-run-1") -> None:
        self.requests: list = []
        self._run_id = run_id

    def run(self, request):
        self.requests.append(request)
        return SimpleNamespace(run_id=self._run_id)


def _manifest_kwargs(**overrides):
    values = {
        "run_id": "run-001",
        "created_at": datetime(2024, 1, 10, 12, 0, tzinfo=UTC),
        "config_hash": "ab" * 32,
        "symbols": ("AAPL",),
        "window_start": datetime(2024, 1, 2, 14, 30, tzinfo=UTC),
        "window_end": datetime(2024, 1, 3, 21, 0, tzinfo=UTC),
        "period_role": PeriodRole.DEV,
        "code_revision": {"commit": "test", "dirty": False},
        "data_provenance": {"source": "fixture", "feed": "IEX"},
        "data_revision": "r1",
        "data_vintage": "r1",
        "dataset_hash": "cd" * 32,
        "starting_capital": "100000",
        "fee_assumptions": {},
        "slippage_assumptions": {},
        "fill_assumptions": {},
        "ambiguity_assumptions": {},
        "seed": 7,
        "strategies": (),
        "holdout_access": None,
    }
    values.update(overrides)
    return values


def _write_manifest(directory: Path, **overrides) -> str:
    manifest = RunManifest(**_manifest_kwargs(**overrides))
    write_manifest(manifest, directory)
    return manifest.run_id


def _raw_fills():
    return [
        {
            "symbol": "AAPL",
            "side": "BUY",
            "quantity": "10",
            "price": "100",
            "fee": "1",
            "time": "2024-01-02T15:30:00+00:00",
            "high": "101",
            "low": "99",
        },
        {
            "symbol": "AAPL",
            "side": "SELL",
            "quantity": "10",
            "price": "110",
            "fee": "1",
            "time": "2024-01-02T16:30:00+00:00",
        },
    ]


def _write_results(directory: Path, run_id: str, fills=None) -> None:
    payload = {
        "evaluations": [],
        "signals": [],
        "intents": [],
        "fills": _raw_fills() if fills is None else fills,
        "cycles": [],
        "equity_curve": [],
    }
    (directory / f"{run_id}.results.json").write_text(json.dumps(payload))


def _write_store(directory: Path) -> list[str]:
    """Three runs: run-b/run-a share created_at (tie), run-c is newest."""
    created_tie = datetime(2024, 1, 10, 12, 0, tzinfo=UTC)
    created_new = datetime(2024, 1, 11, 12, 0, tzinfo=UTC)
    _write_manifest(directory, run_id="run-b", created_at=created_tie)
    _write_results(directory, "run-b")
    _write_manifest(directory, run_id="run-a", created_at=created_tie)
    _write_results(directory, "run-a")
    _write_manifest(directory, run_id="run-c", created_at=created_new)
    _write_results(directory, "run-c")
    return ["run-b", "run-a", "run-c"]


def _write_config(directory: Path, name: str = "config.json", **extra) -> Path:
    payload = {"strategy_ids": ["strat-x"], "starting_capital": "100000"}
    payload.update(extra)
    path = directory / name
    path.write_text(json.dumps(payload))
    return path


def _write_dataset(directory: Path, name: str = "data.json") -> Path:
    path = directory / name
    path.write_text(json.dumps({"events": []}))
    return path


def _run_args(directory: Path, **overrides):
    values = {
        "symbols": ["AAPL", "MSFT"],
        "start": "2024-01-02T14:30:00Z",
        "end": "2024-01-03T21:00:00Z",
        "dataset": str(_write_dataset(directory)),
        "seed": "7",
        "config": str(_write_config(directory)),
        "role": "dev",
        "holdout_lock": None,
        "results_dir": str(directory / "results"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _call(handler, args, **kwargs):
    stdout, stderr = io.StringIO(), io.StringIO()
    code = handler(args, stdout=stdout, stderr=stderr, **kwargs)
    return code, stdout.getvalue(), stderr.getvalue()


# ---------------------------------------------------------------------------
# run: positive delegation
# ---------------------------------------------------------------------------


def test_run_valid_delegates_exact_parameters(tmp_path):
    runner = FakeRunner()
    args = _run_args(tmp_path)
    code, out, err = _call(bt.cmd_backtest_run, args, runner=runner)
    assert code == 0, err
    assert len(runner.requests) == 1
    request = runner.requests[0]
    assert request.symbols == ("AAPL", "MSFT")
    assert request.window_start == datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    assert request.window_end == datetime(2024, 1, 3, 21, 0, tzinfo=UTC)
    assert request.config["dataset"] == args.dataset
    assert request.config["strategy_ids"] == ["strat-x"]
    assert request.seed == 7
    assert request.period_role is PeriodRole.DEV
    assert request.holdout_lock is None
    assert request.broker is None
    assert "run_id=fake-run-1" in out


def test_run_comma_separated_symbols(tmp_path):
    runner = FakeRunner()
    args = _run_args(tmp_path, symbols=["aapl, msft"])
    code, _, err = _call(bt.cmd_backtest_run, args, runner=runner)
    assert code == 0, err
    assert runner.requests[0].symbols == ("AAPL", "MSFT")


def test_run_holdout_with_lock_delegates(tmp_path):
    lock = {
        "token": "tok-1",
        "research_config_hash": "ab" * 32,
        "window_start": "2024-01-01T00:00:00+00:00",
        "window_end": "2024-02-01T00:00:00+00:00",
    }
    lock_path = tmp_path / "lock.json"
    lock_path.write_text(json.dumps(lock))
    runner = FakeRunner()
    args = _run_args(tmp_path, role="holdout", holdout_lock=str(lock_path))
    code, _, err = _call(bt.cmd_backtest_run, args, runner=runner)
    assert code == 0, err
    assert runner.requests[0].period_role is PeriodRole.HOLDOUT
    assert runner.requests[0].holdout_lock.token == "tok-1"


def test_run_awaitable_runner_is_supported(tmp_path):
    class AsyncRunner(FakeRunner):
        async def run(self, request):  # type: ignore[override]
            self.requests.append(request)
            return SimpleNamespace(run_id="async-run-9")

    runner = AsyncRunner()
    code, out, err = _call(bt.cmd_backtest_run, _run_args(tmp_path), runner=runner)
    assert code == 0, err
    assert "run_id=async-run-9" in out


# ---------------------------------------------------------------------------
# run: negative validation (never calls the runner)
# ---------------------------------------------------------------------------


def test_run_zero_symbols_refused(tmp_path):
    runner = FakeRunner()
    code, _, err = _call(bt.cmd_backtest_run, _run_args(tmp_path, symbols=[]), runner=runner)
    assert code != 0
    assert "--symbols" in err
    assert runner.requests == []


def test_run_too_many_symbols_refused(tmp_path):
    runner = FakeRunner()
    many = [f"S{i}" for i in range(11)]
    code, _, err = _call(bt.cmd_backtest_run, _run_args(tmp_path, symbols=many), runner=runner)
    assert code != 0
    assert "10" in err
    assert runner.requests == []


def test_run_missing_start_refused(tmp_path):
    runner = FakeRunner()
    code, _, err = _call(bt.cmd_backtest_run, _run_args(tmp_path, start=None), runner=runner)
    assert code != 0
    assert "--start" in err
    assert runner.requests == []


def test_run_missing_end_refused(tmp_path):
    runner = FakeRunner()
    code, _, err = _call(bt.cmd_backtest_run, _run_args(tmp_path, end=None), runner=runner)
    assert code != 0
    assert "--end" in err
    assert runner.requests == []


def test_run_end_before_start_refused(tmp_path):
    runner = FakeRunner()
    args = _run_args(
        tmp_path, start="2024-01-03T21:00:00Z", end="2024-01-02T14:30:00Z"
    )
    code, _, err = _call(bt.cmd_backtest_run, args, runner=runner)
    assert code != 0
    assert runner.requests == []


def test_run_window_exceeding_maximum_refused(tmp_path):
    runner = FakeRunner()
    args = _run_args(
        tmp_path, start="2023-01-01T00:00:00Z", end="2024-06-01T00:00:00Z"
    )
    code, _, err = _call(bt.cmd_backtest_run, args, runner=runner)
    assert code != 0
    assert "maximum" in err
    assert runner.requests == []


def test_run_missing_dataset_refused(tmp_path):
    runner = FakeRunner()
    args = _run_args(tmp_path, dataset=str(tmp_path / "nope.json"))
    code, _, err = _call(bt.cmd_backtest_run, args, runner=runner)
    assert code != 0
    assert "--dataset" in err
    assert runner.requests == []


@pytest.mark.parametrize(
    "remote",
    ["http://example.com/data.json", "https://example.com/d.json", "s3://bucket/key", "ftp://h/x"],
)
def test_run_remote_dataset_refused(tmp_path, remote):
    runner = FakeRunner()
    code, _, err = _call(bt.cmd_backtest_run, _run_args(tmp_path, dataset=remote), runner=runner)
    assert code != 0
    assert "never download" in err
    assert runner.requests == []


def test_run_missing_seed_and_config_refused(tmp_path):
    runner = FakeRunner()
    code, _, _ = _call(bt.cmd_backtest_run, _run_args(tmp_path, seed=None), runner=runner)
    assert code != 0
    code, _, _ = _call(
        bt.cmd_backtest_run, _run_args(tmp_path, config=str(tmp_path / "no.json")), runner=runner
    )
    assert code != 0
    assert runner.requests == []


def test_run_bad_role_refused(tmp_path):
    runner = FakeRunner()
    code, _, err = _call(bt.cmd_backtest_run, _run_args(tmp_path, role="prod"), runner=runner)
    assert code != 0
    assert "--role" in err
    assert runner.requests == []


def test_holdout_without_lock_refused(tmp_path):
    runner = FakeRunner()
    code, _, err = _call(
        bt.cmd_backtest_run, _run_args(tmp_path, role="holdout", holdout_lock=None), runner=runner
    )
    assert code != 0
    assert "--holdout-lock" in err
    assert runner.requests == []


def test_run_without_runner_refused(tmp_path):
    code, _, err = _call(bt.cmd_backtest_run, _run_args(tmp_path))
    assert code != 0
    assert "runner" in err


def test_run_never_touches_network(tmp_path, monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    runner = FakeRunner()
    code, _, err = _call(bt.cmd_backtest_run, _run_args(tmp_path), runner=runner)
    assert code == 0, err


# ---------------------------------------------------------------------------
# list: ordering, limit, schema
# ---------------------------------------------------------------------------


def test_list_ordering_and_limit(tmp_path):
    _write_store(tmp_path)
    index = BacktestResultsIndex(tmp_path)
    rows = index.list_runs(limit=20)
    assert [row["run_id"] for row in rows] == ["run-c", "run-a", "run-b"]
    limited = index.list_runs(limit=2)
    assert [row["run_id"] for row in limited] == ["run-c", "run-a"]
    assert len(index.list_runs(limit=100)) == 3


def test_list_never_more_than_limit(tmp_path):
    for i in range(6):
        _write_manifest(tmp_path, run_id=f"run-{i:03d}")
        _write_results(tmp_path, f"run-{i:03d}")
    index = BacktestResultsIndex(tmp_path)
    assert len(index.list_runs(limit=5)) == 5


def test_list_json_schema(tmp_path):
    _write_store(tmp_path)
    args = SimpleNamespace(limit=20, json=True, results_dir=str(tmp_path))
    code, out, err = _call(bt.cmd_backtest_list, args)
    assert code == 0, err
    payload = json.loads(out)
    assert set(payload) == {"runs"}
    assert len(payload["runs"]) == 3
    row = payload["runs"][0]
    assert row["run_id"] == "run-c"
    for key in (
        "run_id",
        "created_at",
        "symbols",
        "window",
        "role",
        "config_hash",
        "seed",
        "trade_count",
        "net_return",
    ):
        assert key in row, key
    assert row["symbols"] == ["AAPL"]
    assert row["window"] == {
        "start": "2024-01-02T14:30:00.000000Z",
        "end": "2024-01-03T21:00:00.000000Z",
    }
    assert row["role"] == "DEV"
    assert row["config_hash"] == "ab" * 32
    assert row["seed"] == 7
    assert row["trade_count"] == 1
    assert row["net_return"] == "0.00098"


def test_list_text_is_deterministic(tmp_path):
    _write_store(tmp_path)
    args = SimpleNamespace(limit=20, json=False, results_dir=str(tmp_path))
    _, first, _ = _call(bt.cmd_backtest_list, args)
    _, second, _ = _call(bt.cmd_backtest_list, args)
    assert first == second
    assert "run-c" in first and "run-a" in first and "run-b" in first
    assert first.index("run-c") < first.index("run-a") < first.index("run-b")


def test_list_bad_limit_refused(tmp_path):
    for bad in (0, -3, "abc"):
        args = SimpleNamespace(limit=bad, json=False, results_dir=str(tmp_path))
        code, _, err = _call(bt.cmd_backtest_list, args)
        assert code != 0, bad
        assert "--limit" in err


def test_list_empty_store(tmp_path):
    args = SimpleNamespace(limit=20, json=True, results_dir=str(tmp_path / "missing"))
    code, out, err = _call(bt.cmd_backtest_list, args)
    assert code == 0, err
    assert json.loads(out) == {"runs": []}


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def test_show_json_schema(tmp_path):
    _write_store(tmp_path)
    args = SimpleNamespace(run_id="run-a", json=True, results_dir=str(tmp_path))
    code, out, err = _call(bt.cmd_backtest_show, args)
    assert code == 0, err
    payload = json.loads(out)
    assert payload["run_id"] == "run-a"
    manifest, summary = payload["manifest"], payload["summary"]
    for key in ("run_id", "symbols", "role", "config_hash", "seed"):
        assert key in manifest or key == "role" or key == "symbols", key
    assert manifest["run_id"] == "run-a"
    assert manifest["symbols"] == ["AAPL"]
    assert manifest["period_role"] == "DEV"
    assert manifest["config_hash"] == "ab" * 32
    assert manifest["seed"] == 7
    assert manifest["window_start"] == "2024-01-02T14:30:00.000000Z"
    assert summary["trade_count"] == 1
    assert summary["net_return"] == "0.00098"


def test_show_text(tmp_path):
    _write_store(tmp_path)
    args = SimpleNamespace(run_id="run-a", json=False, results_dir=str(tmp_path))
    code, out, err = _call(bt.cmd_backtest_show, args)
    assert code == 0, err
    assert "run_id=run-a" in out
    assert "net_return=0.00098" in out


def test_show_unknown_run_refused(tmp_path):
    _write_store(tmp_path)
    args = SimpleNamespace(run_id="run-zzz", json=False, results_dir=str(tmp_path))
    code, _, err = _call(bt.cmd_backtest_show, args)
    assert code != 0
    assert "unknown run_id" in err


def test_show_missing_run_id_refused(tmp_path):
    args = SimpleNamespace(run_id=None, json=False, results_dir=str(tmp_path))
    code, _, err = _call(bt.cmd_backtest_show, args)
    assert code != 0
    assert "run_id" in err


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def test_export_json_contents(tmp_path):
    _write_store(tmp_path)
    dest = tmp_path / "out" / "run-a.json"
    args = SimpleNamespace(
        run_id="run-a", format="json", out=str(dest), results_dir=str(tmp_path)
    )
    code, out, err = _call(bt.cmd_backtest_export, args)
    assert code == 0, err
    assert "exported run_id=run-a format=json" in out
    payload = json.loads(dest.read_text())
    assert payload["run_id"] == "run-a"
    assert payload["metrics"]["trade_count"] == 1
    assert payload["metrics"]["net_return"] == "0.00098"
    assert len(payload["trades"]) == 1
    trade = payload["trades"][0]
    assert trade["symbol"] == "AAPL"
    assert trade["pnl"] == "98"
    assert trade["entry_price"] == "100"
    assert trade["exit_price"] == "110"
    # Matches the fake store contents through the real index.
    record = BacktestResultsIndex(tmp_path).get("run-a")
    assert record is not None
    assert payload["trades"] == record["trades"]
    assert payload["metrics"] == record["summary"]


def test_export_csv_contents(tmp_path):
    _write_store(tmp_path)
    dest = tmp_path / "run-a.csv"
    args = SimpleNamespace(
        run_id="run-a", format="csv", out=str(dest), results_dir=str(tmp_path)
    )
    code, _, err = _call(bt.cmd_backtest_export, args)
    assert code == 0, err
    lines = dest.read_text().splitlines()
    metric_lines = [line for line in lines if line.startswith("# ")]
    assert any(line == "# net_return=0.00098" for line in metric_lines)
    assert any(line == "# trade_count=1" for line in metric_lines)
    body = [line for line in lines if not line.startswith("# ")]
    reader = csv.DictReader(body)
    rows = list(reader)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "AAPL"
    assert rows[0]["pnl"] == "98"
    assert rows[0]["quantity"] == "10"


def test_export_validation(tmp_path):
    _write_store(tmp_path)
    base = {"run_id": "run-a", "format": "json", "out": str(tmp_path / "x.json"),
            "results_dir": str(tmp_path)}
    code, _, err = _call(bt.cmd_backtest_export, SimpleNamespace(**{**base, "format": "xml"}))
    assert code != 0 and "--format" in err
    code, _, err = _call(bt.cmd_backtest_export, SimpleNamespace(**{**base, "out": None}))
    assert code != 0 and "--out" in err
    code, _, err = _call(
        bt.cmd_backtest_export, SimpleNamespace(**{**base, "run_id": "missing"})
    )
    assert code != 0 and "unknown run_id" in err
    assert not (tmp_path / "x.json").exists()


# ---------------------------------------------------------------------------
# Read-only store, no credentials, registration pattern
# ---------------------------------------------------------------------------


def _store_hashes(directory: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.iterdir())
        if path.is_file()
    }


def test_index_never_mutates_store(tmp_path):
    _write_store(tmp_path)
    before = _store_hashes(tmp_path)
    index = BacktestResultsIndex(tmp_path)
    index.list_runs(limit=2)
    index.get("run-a")
    stdout, stderr = io.StringIO(), io.StringIO()
    assert bt.cmd_backtest_list(
        SimpleNamespace(limit=5, json=False, results_dir=str(tmp_path)),
        index=index, stdout=stdout, stderr=stderr,
    ) == 0
    assert bt.cmd_backtest_show(
        SimpleNamespace(run_id="run-a", json=True, results_dir=str(tmp_path)),
        index=index, stdout=stdout, stderr=stderr,
    ) == 0
    assert bt.cmd_backtest_export(
        SimpleNamespace(run_id="run-a", format="json",
                        out=str(tmp_path / "exp.json"), results_dir=str(tmp_path)),
        index=index, stdout=stdout, stderr=stderr,
    ) == 0
    (tmp_path / "exp.json").unlink()
    assert _store_hashes(tmp_path) == before


class _TrackingEnv(dict):
    """Records every key lookup so tests can prove credential keys are untouched."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.accessed: list = []

    def __getitem__(self, key):
        self.accessed.append(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        self.accessed.append(key)
        return super().get(key, default)

    def __contains__(self, key):
        self.accessed.append(key)
        return super().__contains__(key)


def _is_credential_key(key) -> bool:
    text = str(key).upper()
    return "ALPACA" in text or text.startswith("APCA_")


def test_no_credential_access(tmp_path, monkeypatch):
    tracking = _TrackingEnv(os.environ)
    tracking["ALPACA_API_KEY"] = "fake-key"
    tracking["ALPACA_API_SECRET"] = "fake-secret"
    tracking["APCA_API_KEY_ID"] = "fake-id"
    tracking["APCA_API_SECRET_KEY"] = "fake-secret-2"
    monkeypatch.setattr(os, "environ", tracking)

    _write_store(tmp_path)
    runner = FakeRunner()
    assert _call(bt.cmd_backtest_run, _run_args(tmp_path), runner=runner)[0] == 0
    assert _call(
        bt.cmd_backtest_list,
        SimpleNamespace(limit=5, json=True, results_dir=str(tmp_path)),
    )[0] == 0
    assert _call(
        bt.cmd_backtest_show,
        SimpleNamespace(run_id="run-a", json=True, results_dir=str(tmp_path)),
    )[0] == 0
    assert _call(
        bt.cmd_backtest_export,
        SimpleNamespace(run_id="run-a", format="csv",
                        out=str(tmp_path / "e.csv"), results_dir=str(tmp_path)),
    )[0] == 0

    touched = [key for key in tracking.accessed if _is_credential_key(key)]
    assert touched == []

    source = Path(bt.__file__).read_text()
    lowered = source.lower()
    assert "os.environ" not in source
    assert "getenv" not in lowered
    assert "alpaca" not in lowered
    assert "api_key" not in lowered
    assert "api_secret" not in lowered


def test_public_surface(tmp_path):
    expected = {
        "register",
        "cmd_backtest_run",
        "cmd_backtest_list",
        "cmd_backtest_show",
        "cmd_backtest_export",
        "BacktestResultsIndex",
    }
    assert set(bt.__all__) == expected
    for name in expected:
        assert callable(getattr(bt, name)), name


def test_register_wires_backtest_group():
    parser = argparse.ArgumentParser(prog="mike-trade")
    subparsers = parser.add_subparsers(dest="subcommand")
    bt.register(subparsers)

    args = parser.parse_args(
        ["backtest", "run", "--symbols", "AAPL,MSFT", "--start", "2024-01-02T14:30:00Z",
         "--end", "2024-01-03T21:00:00Z", "--dataset", "d.json", "--seed", "7",
         "--config", "c.json", "--role", "dev"]
    )
    assert args.backtest_subcommand == "run"
    assert args.func is bt.cmd_backtest_run
    assert args.symbols == ["AAPL,MSFT"]
    assert args.role == "dev"

    args = parser.parse_args(["backtest", "list", "--limit", "5", "--json"])
    assert args.func is bt.cmd_backtest_list
    assert args.limit == "5"

    args = parser.parse_args(["backtest", "show", "run-1"])
    assert args.func is bt.cmd_backtest_show
    assert args.run_id == "run-1"

    args = parser.parse_args(
        ["backtest", "export", "run-1", "--format", "csv", "--out", "o.csv"]
    )
    assert args.func is bt.cmd_backtest_export
    assert args.format == "csv"
