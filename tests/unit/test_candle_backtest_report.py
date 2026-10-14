"""The consolidated comparison derives every figure from the recorded fills."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from mike_trade.modeling.candle_backtest_report import build_comparison, write_comparison

TRADES = [
    # configuration, symbol, entry_time, entry_price, exit_time, exit_price, quantity, gross, costs, pnl, hold, entry, exit_s, reason
    ("C5", "IBM", "2026-08-10 14:00:00+00:00", "100.02", "2026-08-10 15:00:00+00:00", "101.98", "10", "19.6", "0", "19.6", "3600", "x", "", "PROTECTIVE_STOP"),
    ("C5", "IBM", "2026-08-11 14:00:00+00:00", "100.00", "2026-08-11 19:55:00+00:00", "99.00", "10", "-10", "0", "-10", "21300", "x", "", "END_OF_DAY"),
    ("C5", "IBM", "2026-08-12 14:00:00+00:00", "100.00", "2026-08-12 14:30:00+00:00", "99.50", "10", "-5", "0", "-5", "1800", "x", "", "PROTECTIVE_STOP"),
]
COLUMNS = ("configuration,symbol,entry_time,entry_price,exit_time,exit_price,quantity,gross_pnl,"
           "costs,pnl,hold_seconds,entry_strategy,exit_strategy,exit_reason").split(",")


@pytest.fixture()
def campaign(tmp_path: Path) -> Path:
    (tmp_path / "report").mkdir()
    (tmp_path / "campaign.yaml").write_text(
        "campaign_id: demo\nwindow: {start: '2026-08-10T00:00:00Z', end: '2026-09-21T00:00:00Z'}\n"
        "starting_capital: '10000'\nexecution: {slippage_bps: '2'}\n"
        "protective_stop: {type: native_trailing, trail_percent: '0.5'}\n"
        "end_of_day_close: {enabled: true, at: '15:55'}\n"
        "configurations: {C5: {buy: [m], sell: []}}\n"
    )
    with (tmp_path / "report" / "trades.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        writer.writerows(TRADES)
    with (tmp_path / "report" / "per_symbol.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["configuration", "symbol", "max_drawdown", "estimated_slippage", "ending_equity"])
        writer.writerow(["C5", "IBM", "15.00", "3.21", "10004.60"])
    with (tmp_path / "report" / "baseline_buy_and_hold.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["symbol", "first_open", "last_close", "return", "profit_on_capital"])
        writer.writerow(["IBM", "200", "190", "-0.05", "-500.00"])
    (tmp_path / "report" / "report.json").write_text(json.dumps({"failures": []}))
    return tmp_path


def test_statistics_exit_mix_slippage_and_buy_and_hold(campaign):
    (run,) = build_comparison(campaign)["runs"]
    assert run["trades"] == 3 and run["wins"] == 1 and run["losses"] == 2
    assert run["win_rate"] == pytest.approx(1 / 3)
    assert float(run["net_pnl"]) == pytest.approx(4.6)
    assert float(run["average_pnl_per_trade"]) == pytest.approx(4.6 / 3)
    assert float(run["profit_factor"]) == pytest.approx(19.6 / 15)
    assert float(run["average_hold_minutes"]) == pytest.approx((3600 + 21300 + 1800) / 3 / 60)
    assert (run["trailing_stop_exits"], run["end_of_day_exits"], run["other_exits"]) == (2, 1, {})
    assert float(run["max_drawdown"]) == 15.0
    # modeled slippage is added back exactly: fills are touch*(1+2bp) for buys, touch*(1-2bp) for sells
    expected = sum(
        float(q) * (float(e) * 0.0002 / 1.0002 + float(x) * 0.0002 / 0.9998)
        for _c, _s, _t, e, _u, x, q, *_rest in TRADES
    )
    assert float(run["modeled_slippage"]) == pytest.approx(expected)
    assert float(run["net_before_modeled_slippage"]) == pytest.approx(4.6 + expected)
    assert float(run["buy_and_hold_profit"]) == -500.0
    assert float(run["net_minus_buy_and_hold"]) == pytest.approx(504.6)


def test_outputs_include_a_separate_result_per_run(campaign, tmp_path):
    out = tmp_path / "out"
    paths = write_comparison(campaign, out)
    text = paths["markdown"].read_text()
    assert "IBM · C5" in text and "Buy & hold" in text and "JEV review off" in text
    run_dir = out / "runs" / "IBM_C5"
    assert len(list(csv.DictReader((run_dir / "trades.csv").open()))) == 3
    assert json.loads((run_dir / "summary.json").read_text())["trades"] == 3
    assert (out / "comparison.csv").exists() and (out / "comparison.json").exists()
