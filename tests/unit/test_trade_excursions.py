"""Targeted MFE/MAE accounting tests on small synthetic trade paths."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from mike_trade.backtest.runner import compute_metrics

T0 = datetime(2025, 1, 6, 15, 0, tzinfo=UTC)
MIN = timedelta(minutes=1)


def _fills(entry, exit_, entry_at=0, exit_at=3, qty="10", fee="1", side="BUY"):
    cover = side == "SELL_SHORT"
    return [
        {"symbol": "AAA", "side": side, "quantity": D(qty), "price": D(entry), "fee": D(fee),
         "time": T0 + entry_at * MIN},
        {"symbol": "AAA", "side": "BUY_TO_COVER" if cover else "SELL", "quantity": D(qty),
         "price": D(exit_), "fee": D(fee), "time": T0 + exit_at * MIN},
    ]


def _bar(minute, high, low, symbol="AAA"):
    return {"symbol": symbol, "time": T0 + minute * MIN, "high": D(high), "low": D(low)}


def _trade(fills, bars):
    return compute_metrics(fills, starting_capital=D("10000"), bars=bars).trades[0]


def test_profitable_long_runs_to_the_high():
    t = _trade(_fills("100", "108"), [_bar(0, "103", "99"), _bar(1, "110", "101"), _bar(2, "109", "104")])
    assert (t["mfe"], t["mae"]) == (D("100"), D("-10"))
    assert (t["mfe_pct"], t["mae_pct"]) == (D("10"), D("-1"))
    assert t["pnl"] == D("78")  # gross 80 less fees 2: fees never enter excursions
    assert t["excursion_source"] == "bars" and t["excursion_bars"] == 3


def test_losing_long_mae_is_the_low_and_mfe_is_zero():
    t = _trade(_fills("100", "92"), [_bar(0, "100", "97"), _bar(1, "99", "90"), _bar(2, "95", "91")])
    assert (t["mfe"], t["mae"]) == (D("0"), D("-100"))
    assert (t["mfe_pct"], t["mae_pct"]) == (D("0"), D("-10"))


def test_favorable_move_then_loss_keeps_both_extremes():
    t = _trade(_fills("100", "95"), [_bar(0, "106", "100"), _bar(1, "104", "96"), _bar(2, "97", "94")])
    assert (t["mfe"], t["mae"]) == (D("60"), D("-60"))
    assert t["pnl"] < 0 and t["mfe"] > 0


def test_adverse_move_then_recovery_is_a_winner_with_real_mae():
    t = _trade(_fills("100", "105"), [_bar(0, "100", "94"), _bar(1, "101", "97"), _bar(2, "106", "100")])
    assert (t["mfe"], t["mae"]) == (D("60"), D("-60"))
    assert t["pnl"] > 0


def test_same_candle_entry_at_open_and_intrabar_exit_uses_that_candle():
    # Entry at the bar open (bar start), stop exit stamped at the bar end.
    t = _trade(_fills("100", "98", entry_at=0, exit_at=1), [_bar(0, "101", "97")])
    assert (t["mfe"], t["mae"]) == (D("10"), D("-30"))
    assert t["excursion_bars"] == 1


def test_candles_straddling_entry_or_exit_are_excluded():
    bars = [
        _bar(0, "130", "70"),  # intrabar entry stamped at bar end -> not in position
        _bar(1, "104", "99"),
        _bar(2, "120", "50"),  # exit fills at the open of this bar -> already flat
    ]
    t = _trade(_fills("100", "102", entry_at=1, exit_at=2), bars)
    assert (t["mfe"], t["mae"]) == (D("40"), D("-10"))
    assert t["excursion_bars"] == 1


def test_bars_for_other_symbols_and_outside_window_are_ignored():
    bars = [_bar(1, "104", "99"), _bar(1, "500", "1", symbol="BBB"), _bar(10, "900", "1")]
    t = _trade(_fills("100", "102", entry_at=1, exit_at=2), bars)
    assert (t["mfe"], t["mae"]) == (D("40"), D("-10"))


def test_missing_observations_fall_back_to_fill_prices_not_entry_default():
    t = _trade(_fills("100", "108"), [])
    assert t["excursion_source"] == "fills" and t["excursion_bars"] == 0
    assert (t["mfe"], t["mae"]) == (D("80"), D("0"))
    t = _trade(_fills("100", "92"), [_bar(20, "500", "1")])
    assert (t["mfe"], t["mae"]) == (D("0"), D("-80"))


def test_exit_fill_slippage_beyond_the_bar_range_is_still_observed():
    t = _trade(_fills("100", "96", exit_at=1), [_bar(0, "101", "97")])
    assert t["mae"] == D("-40")


def test_partial_exits_use_each_lots_own_window_and_quantity():
    fills = [
        {"symbol": "AAA", "side": "BUY", "quantity": D("10"), "price": D("100"), "fee": D("0"), "time": T0},
        {"symbol": "AAA", "side": "SELL", "quantity": D("4"), "price": D("103"), "fee": D("0"), "time": T0 + 2 * MIN},
        {"symbol": "AAA", "side": "SELL", "quantity": D("6"), "price": D("101"), "fee": D("0"), "time": T0 + 4 * MIN},
    ]
    bars = [_bar(0, "102", "99"), _bar(1, "104", "100"), _bar(2, "109", "101"), _bar(3, "103", "98")]
    first, second = compute_metrics(fills, starting_capital=D("10000"), bars=bars).trades
    assert (first["mfe"], first["mae"]) == (D("16"), D("-4"))  # bars 0-1 only, qty 4
    assert (second["mfe"], second["mae"]) == (D("54"), D("-12"))  # bars 0-3, qty 6


def test_short_excursions_flip_sign_convention():
    t = _trade(_fills("100", "96", side="SELL_SHORT"), [_bar(0, "103", "99"), _bar(1, "101", "94"), _bar(2, "98", "95")])
    assert (t["mfe"], t["mae"]) == (D("60"), D("-30"))
    assert (t["mfe_pct"], t["mae_pct"]) == (D("6"), D("-3"))
