"""Read-only audit of the pinned annual SIP snapshots."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import duckdb

from mike_trade.research.datasets import HistoricalDatasetRepository, _sha256
from mike_trade.scheduling.calendar import NEW_YORK, is_trading_day, session_for


ROOT = Path(__file__).resolve().parents[2] / "data"
START = datetime.fromisoformat("2025-10-09T00:00:00+00:00")
END = datetime.fromisoformat("2026-10-09T00:00:00+00:00")


def sessions(first: date, last: date):
    day = first
    while day <= last:
        if is_trading_day(day):
            yield session_for(day)
        day += timedelta(days=1)


def audit() -> dict:
    repo = HistoricalDatasetRepository(ROOT)
    rows = {}
    expected = list(sessions(date(2025, 10, 9), date(2026, 10, 8)))
    for symbol in ("IBM", "NVDA", "TSLA"):
        annual = [s for s in repo.snapshots() if s.symbol == symbol and s.feed == "sip"
                  and s.timeframe == "1m" and s.adjustment == "raw"
                  and datetime.fromisoformat(s.start) <= START
                  and datetime.fromisoformat(s.end) >= END]
        if len(annual) != 1:
            raise ValueError(f"{symbol}: expected one pinned annual SIP snapshot, found {len(annual)}")
        snapshot = annual[0]
        path = ROOT / snapshot.data_file
        if _sha256(path) != snapshot.sha256:
            raise ValueError(f"{symbol}: snapshot hash mismatch")
        con = duckdb.connect(":memory:")
        try:
            candles = con.execute(
                "SELECT timestamp, symbol, open, high, low, close, volume "
                "FROM read_parquet(?) ORDER BY timestamp", [str(path)]
            ).fetchall()
        finally:
            con.close()
        if len(candles) != snapshot.bar_count:
            raise ValueError(f"{symbol}: manifest count mismatch")
        observed = Counter()
        invalid = []
        timestamps = set()
        off_session = 0
        for stamp, bar_symbol, op, hi, lo, close, volume in candles:
            stamp = stamp.astimezone(UTC)
            if stamp in timestamps:
                raise ValueError(f"{symbol}: duplicate timestamp {stamp}")
            timestamps.add(stamp)
            if not START <= stamp < END or bar_symbol != symbol:
                raise ValueError(f"{symbol}: out-of-range or mixed-symbol candle")
            if not (float(lo) <= float(op) <= float(hi)
                    and float(lo) <= float(close) <= float(hi)
                    and float(volume) >= 0):
                invalid.append(stamp.isoformat())
            local_day = stamp.astimezone(NEW_YORK).date()
            if is_trading_day(local_day):
                session = session_for(local_day)
                if session.open_utc <= stamp < session.close_utc:
                    observed[local_day] += 1
                else:
                    off_session += 1
            else:
                off_session += 1
        if invalid:
            raise ValueError(f"{symbol}: {len(invalid)} invalid OHLCV candles")
        session_counts = {s.session_date.isoformat(): observed[s.session_date] for s in expected}
        missing_sessions = [day for day, count in session_counts.items() if count == 0]
        full_sessions = sum(
            observed[s.session_date] == int((s.close_utc - s.open_utc).total_seconds() / 60)
            for s in expected
        )
        missing_minutes = sum(
            int((s.close_utc - s.open_utc).total_seconds() / 60) - observed[s.session_date]
            for s in expected
        )
        daily = [s for s in repo.snapshots() if s.symbol == symbol and s.feed == "sip"
                 and s.timeframe == "1d" and s.adjustment == "raw"]
        if len(daily) != 1 or _sha256(ROOT / daily[0].data_file) != daily[0].sha256:
            raise ValueError(f"{symbol}: daily SIP history missing or corrupt")
        daily_candles = repo.daily_candles(
            symbol, date(2025, 9, 1), date(2026, 10, 8), feed="sip"
        )
        expected_daily = {s.session_date for s in sessions(date(2025, 9, 1), date(2026, 10, 8))}
        observed_daily = {c.start.astimezone(NEW_YORK).date() for c in daily_candles}
        if observed_daily != expected_daily or len(daily_candles) != daily[0].bar_count:
            raise ValueError(f"{symbol}: daily history has missing or duplicate sessions")
        rows[symbol] = {
            "minute_dataset_id": snapshot.dataset_id,
            "minute_bars": len(candles),
            "first_bar": snapshot.first_bar,
            "last_bar": snapshot.last_bar,
            "regular_session_bars": sum(session_counts.values()),
            "extended_hours_bars": off_session,
            "expected_sessions": len(expected),
            "sessions_with_bars": len(expected) - len(missing_sessions),
            "full_regular_sessions": full_sessions,
            "missing_regular_minutes": missing_minutes,
            "missing_sessions": missing_sessions,
            "daily_dataset_id": daily[0].dataset_id,
            "daily_bars": daily[0].bar_count,
            "daily_sessions_complete": len(observed_daily),
            "daily_first_bar": daily[0].first_bar,
            "daily_last_bar": daily[0].last_bar,
            "session_counts": session_counts,
        }
    return {"window_start": START.isoformat(), "window_end_exclusive": END.isoformat(),
            "symbols": rows}


if __name__ == "__main__":
    result = audit()
    target = Path(__file__).with_name("dataset_audit.json")
    target.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    for symbol, row in result["symbols"].items():
        print(symbol, row["minute_bars"], "bars,", row["sessions_with_bars"], "/",
              row["expected_sessions"], "sessions,", row["full_regular_sessions"],
              "full sessions,", row["missing_regular_minutes"], "missing regular minutes")
