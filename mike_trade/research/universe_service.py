"""Universe discovery and historical dataset status service."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mike_trade.research.datasets import DatasetSnapshot, HistoricalDatasetRepository


@dataclass(frozen=True, slots=True)
class UniverseMemberStatus:
    symbol: str
    asset_class: str
    group: str
    status: str  # "DATASET_AND_BACKTEST", "DATASET_ONLY", "MISSING_DATASET"
    dataset_available: bool
    has_backtest_results: bool
    dataset_id: str | None = None
    feed: str | None = None
    timeframe: str | None = None
    adjustment: str | None = None
    start: str | None = None
    end: str | None = None
    bar_count: int | None = None
    sha256: str | None = None
    backtest_run_count: int = 0
    latest_backtest_run_id: str | None = None


@dataclass(frozen=True, slots=True)
class UniverseInventory:
    universe_id: str
    selected_at: str | None
    total_universe_symbols: int
    available_dataset_symbols: int
    missing_dataset_symbols: int
    backtested_symbols: int
    symbols: list[UniverseMemberStatus]
    snapshots: list[dict[str, Any]]
    duckdb_catalog_available: bool


class UniverseService:
    """Manages the 100-stock research universe and cross-references snapshots and backtests."""

    def __init__(
        self,
        dataset_root: str | Path,
        universe_config_path: str | Path | None = None,
    ) -> None:
        self.dataset_root = Path(dataset_root).resolve()
        self.repo = HistoricalDatasetRepository(self.dataset_root)

        if universe_config_path is not None:
            self.config_path = Path(universe_config_path).resolve()
        else:
            # Look for config/research_universe_100_v1.json in repo root
            candidates = [
                Path("config/research_universe_100_v1.json"),
                Path(__file__).resolve().parents[2] / "config" / "research_universe_100_v1.json",
                self.dataset_root.parent.parent / "config" / "research_universe_100_v1.json",
            ]
            self.config_path = next((p for p in candidates if p.is_file()), candidates[0])

    def get_inventory(
        self,
        backtested_counts: Mapping[str, int] | None = None,
        latest_run_ids: Mapping[str, str] | None = None,
    ) -> UniverseInventory:
        """Build a comprehensive dataset inventory for all universe members."""
        counts = backtested_counts or {}
        latest_runs = latest_run_ids or {}

        # 1. Load universe members
        universe_symbols: list[dict[str, str]] = []
        universe_id = "us-liquid-research-100-v1"
        selected_at = None

        if self.config_path.is_file():
            try:
                cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
                universe_id = str(cfg.get("universe_id", universe_id))
                selected_at = cfg.get("selected_at")
                universe_symbols = list(cfg.get("symbols", []))
            except (OSError, ValueError):
                pass

        # 2. Load dataset snapshots
        raw_snapshots: list[DatasetSnapshot] = self.repo.snapshots()
        snapshots_by_symbol: dict[str, DatasetSnapshot] = {}
        for snap in raw_snapshots:
            # Pick latest snapshot if multiple exist
            existing = snapshots_by_symbol.get(snap.symbol.upper())
            if existing is None or snap.acquired_at > existing.acquired_at:
                snapshots_by_symbol[snap.symbol.upper()] = snap

        # 3. Cross-reference
        members: list[UniverseMemberStatus] = []
        available_count = 0
        backtested_count = 0

        # Track seen symbols to include snapshots outside universe if any
        seen_symbols: set[str] = set()

        for u in universe_symbols:
            sym = str(u.get("symbol", "")).upper()
            if not sym:
                continue
            seen_symbols.add(sym)
            snap = snapshots_by_symbol.get(sym)
            b_count = counts.get(sym, 0)
            last_run = latest_runs.get(sym)

            dataset_avail = snap is not None
            has_backtests = b_count > 0

            if dataset_avail:
                available_count += 1
            if has_backtests:
                backtested_count += 1

            if dataset_avail and has_backtests:
                status = "DATASET_AND_BACKTEST"
            elif dataset_avail:
                status = "DATASET_ONLY"
            else:
                status = "MISSING_DATASET"

            members.append(
                UniverseMemberStatus(
                    symbol=sym,
                    asset_class=str(u.get("asset_class", "us_equity")),
                    group=str(u.get("group", "general")),
                    status=status,
                    dataset_available=dataset_avail,
                    has_backtest_results=has_backtests,
                    dataset_id=snap.dataset_id if snap else None,
                    feed=snap.feed if snap else None,
                    timeframe=snap.timeframe if snap else None,
                    adjustment=snap.adjustment if snap else None,
                    start=snap.start if snap else None,
                    end=snap.end if snap else None,
                    bar_count=snap.bar_count if snap else None,
                    sha256=snap.sha256 if snap else None,
                    backtest_run_count=b_count,
                    latest_backtest_run_id=last_run,
                )
            )

        # Also add any snapshots not in universe config
        for sym, snap in snapshots_by_symbol.items():
            if sym not in seen_symbols:
                b_count = counts.get(sym, 0)
                last_run = latest_runs.get(sym)
                available_count += 1
                if b_count > 0:
                    backtested_count += 1
                status = "DATASET_AND_BACKTEST" if b_count > 0 else "DATASET_ONLY"
                members.append(
                    UniverseMemberStatus(
                        symbol=sym,
                        asset_class=snap.asset_class,
                        group="external",
                        status=status,
                        dataset_available=True,
                        has_backtest_results=b_count > 0,
                        dataset_id=snap.dataset_id,
                        feed=snap.feed,
                        timeframe=snap.timeframe,
                        adjustment=snap.adjustment,
                        start=snap.start,
                        end=snap.end,
                        bar_count=snap.bar_count,
                        sha256=snap.sha256,
                        backtest_run_count=b_count,
                        latest_backtest_run_id=last_run,
                    )
                )

        catalog_db = self.dataset_root / "catalog.duckdb"

        serialized_snapshots = [
            {
                "dataset_id": s.dataset_id,
                "provider": s.provider,
                "feed": s.feed,
                "symbol": s.symbol,
                "asset_class": s.asset_class,
                "timeframe": s.timeframe,
                "adjustment": s.adjustment,
                "acquired_at": s.acquired_at,
                "start": s.start,
                "end": s.end,
                "bar_count": s.bar_count,
                "data_file": s.data_file,
                "sha256": s.sha256,
            }
            for s in raw_snapshots
        ]

        total_universe = len(universe_symbols)
        missing_count = total_universe - available_count

        return UniverseInventory(
            universe_id=universe_id,
            selected_at=selected_at,
            total_universe_symbols=total_universe,
            available_dataset_symbols=available_count,
            missing_dataset_symbols=max(0, missing_count),
            backtested_symbols=backtested_count,
            symbols=members,
            snapshots=serialized_snapshots,
            duckdb_catalog_available=catalog_db.is_file(),
        )
