"""Adapters over the existing immutable Parquet/DuckDB research data store."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import duckdb
import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from mike_trade.backtest.manifest import canonical_json
from mike_trade.modeling.contracts import (
    DatasetSelection,
    DatasetSource,
    FeatureSchema,
    FeatureVector,
    ModelBar,
)
from mike_trade.research.datasets import DatasetSnapshot, HistoricalDatasetRepository
from mike_trade.scheduling.calendar import NEW_YORK, is_trading_day, session_for


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_session_id(start: datetime, end: datetime) -> str | None:
    day = start.astimezone(NEW_YORK).date()
    if not is_trading_day(day):
        return None
    session = session_for(day)
    if start < session.open_utc or end > session.close_utc:
        return None
    return session.session_id


class ParquetDatasetProvider:
    """Read-only, bounded adapter over ``HistoricalDatasetRepository`` snapshots.

    The adapter resolves one immutable snapshot per symbol, verifies its checksum before
    every scan, and fetches rows in bounded batches. The initial intraday model inputs are
    regular-session bars; extended-hours rows remain in the authoritative raw snapshot.
    """

    def __init__(self, repository: HistoricalDatasetRepository | str | Path) -> None:
        self.repository = (
            repository
            if isinstance(repository, HistoricalDatasetRepository)
            else HistoricalDatasetRepository(repository)
        )

    def select(
        self,
        symbols: Sequence[str],
        start: datetime,
        end: datetime,
        *,
        feed: str,
        timeframe: str = "1m",
        adjustment: str = "raw",
        universe_id: str,
        dataset_ids: Mapping[str, str] | None = None,
    ) -> DatasetSelection:
        start_utc, end_utc = start.astimezone(UTC), end.astimezone(UTC)
        if start.tzinfo is None or end.tzinfo is None or end_utc <= start_utc:
            raise ValueError("selection requires a bounded timezone-aware range")
        normalized = tuple(dict.fromkeys(item.strip().upper() for item in symbols))
        if not normalized or any(not item for item in normalized):
            raise ValueError("symbols must be non-empty")
        if re.fullmatch(r"[1-9][0-9]*m", timeframe.lower()) is None:
            raise ValueError("the intraday dataset adapter requires a minute timeframe")
        pins = {key.upper(): value for key, value in (dataset_ids or {}).items()}
        if set(pins) - set(normalized):
            raise ValueError("dataset_ids contains a symbol outside the requested selection")
        snapshots = self.repository.snapshots()
        sources: list[DatasetSource] = []
        for item in normalized:
            candidates = [
                snapshot
                for snapshot in snapshots
                if snapshot.symbol == item
                and snapshot.feed == feed.lower()
                and snapshot.timeframe == timeframe.lower()
                and snapshot.adjustment == adjustment.lower()
                and (item not in pins or snapshot.dataset_id == pins[item])
                and datetime.fromisoformat(snapshot.start).astimezone(UTC) <= start_utc
                and datetime.fromisoformat(snapshot.end).astimezone(UTC) >= end_utc
            ]
            if not candidates:
                pin = f" pinned to {pins[item]}" if item in pins else ""
                raise ValueError(f"no immutable dataset covers {item}{pin}")
            chosen = max(candidates, key=lambda value: (value.acquired_at, value.dataset_id))
            sources.append(self._source(chosen))
        return DatasetSelection(universe_id, start_utc, end_utc, tuple(sources))

    @staticmethod
    def _source(snapshot: DatasetSnapshot) -> DatasetSource:
        return DatasetSource(
            snapshot.dataset_id,
            snapshot.sha256,
            snapshot.symbol,
            snapshot.feed,
            snapshot.timeframe,
            snapshot.adjustment,
            datetime.fromisoformat(snapshot.start),
            datetime.fromisoformat(snapshot.end),
        )

    def _snapshot(self, source: DatasetSource) -> DatasetSnapshot:
        matches = [
            snapshot
            for snapshot in self.repository.snapshots()
            if snapshot.dataset_id == source.dataset_id
        ]
        if len(matches) != 1:
            raise ValueError(f"dataset manifest unavailable: {source.dataset_id}")
        snapshot = matches[0]
        path = self.repository.root / snapshot.data_file
        if _file_sha256(path) != source.sha256:
            raise ValueError(f"dataset integrity failure: {source.dataset_id}")
        return snapshot

    def iter_bars(
        self, selection: DatasetSelection, symbol: str, *, batch_size: int = 4096
    ) -> Iterator[ModelBar]:
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise ValueError("batch_size must be a positive int")
        normalized = symbol.strip().upper()
        matches = [source for source in selection.sources if source.symbol == normalized]
        if len(matches) != 1:
            raise ValueError(f"{normalized} is not present in this selection")
        source = matches[0]
        snapshot = self._snapshot(source)
        path = self.repository.root / snapshot.data_file
        connection = duckdb.connect(":memory:")
        try:
            cursor = connection.execute(
                "SELECT timestamp, symbol, open, high, low, close, volume, vwap, trade_count "
                "FROM read_parquet(?) WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp",
                [str(path), selection.start, selection.end],
            )
            while True:
                rows = cursor.fetchmany(batch_size)
                if not rows:
                    break
                for row in rows:
                    start = row[0].astimezone(UTC)
                    end = start + timedelta(minutes=int(source.timeframe[:-1]))
                    session_id = _regular_session_id(start, end)
                    if session_id is None:
                        continue
                    yield ModelBar(
                        symbol=row[1],
                        start=start,
                        end=end,
                        open=Decimal(row[2]),
                        high=Decimal(row[3]),
                        low=Decimal(row[4]),
                        close=Decimal(row[5]),
                        volume=Decimal(row[6]),
                        vwap=None if row[7] is None else Decimal(row[7]),
                        trade_count=None if row[8] is None else int(Decimal(row[8])),
                        feed=source.feed,
                        timeframe=source.timeframe,
                        adjustment=source.adjustment,
                        session_id=session_id,
                    )
        finally:
            connection.close()


@dataclass(frozen=True, slots=True)
class FeatureDatasetManifest:
    """Provenance for one immutable, derived feature partition."""

    feature_dataset_id: str
    feature_id: str
    feature_version: str
    feature_names: tuple[str, ...]
    symbol: str
    start: str
    end: str
    row_count: int
    source_dataset_ids: tuple[str, ...]
    source_hashes: tuple[str, ...]
    feed: str
    timeframe: str
    adjustment: str
    universe_id: str
    data_file: str
    sha256: str
    schema_version: int = 1


class FeatureDatasetRepository:
    """Write-new Parquet storage for derived features, separate from raw observations."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.data_dir = self.root / "feature_datasets"
        self.manifest_dir = self.root / "feature_manifests"

    def persist_partition(
        self,
        rows: Iterable[FeatureVector],
        *,
        selection: DatasetSelection,
        schema: FeatureSchema,
        symbol: str,
    ) -> FeatureDatasetManifest:
        normalized = symbol.strip().upper()
        materialized = list(rows)
        if not materialized:
            raise ValueError("cannot persist an empty feature partition")
        if any(
            row.symbol != normalized
            or row.schema_id != schema.feature_id
            or row.schema_version != schema.version
            or tuple(row.values) != schema.names
            for row in materialized
        ):
            raise ValueError("feature row does not match partition identity")
        data_rows = [
            {
                "symbol": row.symbol,
                "bar_start": row.bar_start,
                "as_of": row.as_of,
                "session_id": row.session_id,
                **dict(row.values),
            }
            for row in materialized
        ]
        identity_rows = [
            {
                key: (format(value, ".17g") if isinstance(value, float) else value)
                for key, value in row.items()
            }
            for row in data_rows
        ]
        identity = {
            "schema": asdict(schema),
            "symbol": normalized,
            "rows": identity_rows,
            "sources": [(s.dataset_id, s.sha256) for s in selection.sources],
            "universe_id": selection.universe_id,
        }
        content_hash = hashlib.sha256(canonical_json(identity).encode()).hexdigest()
        dataset_id = (
            f"features-{schema.feature_id}-{schema.version}-{normalized}-{content_hash[:20]}"
        )
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_dir.mkdir(parents=True, exist_ok=True)
        data_path = self.data_dir / f"{dataset_id}.parquet"
        fd, temp_name = tempfile.mkstemp(dir=self.data_dir, suffix=".parquet")
        os.close(fd)
        temp = Path(temp_name)
        try:
            pq.write_table(pa.Table.from_pylist(data_rows), temp, compression="zstd", version="2.6")
            with temp.open("rb") as stream:
                os.fsync(stream.fileno())
            try:
                os.link(temp, data_path)
            except FileExistsError:
                if _file_sha256(temp) != _file_sha256(data_path):
                    raise ValueError("conflicting immutable feature partition") from None
        finally:
            temp.unlink(missing_ok=True)
        source_ids = tuple(source.dataset_id for source in selection.sources)
        source_hashes = tuple(source.sha256 for source in selection.sources)
        manifest = FeatureDatasetManifest(
            dataset_id,
            schema.feature_id,
            schema.version,
            schema.names,
            normalized,
            materialized[0].bar_start.isoformat(),
            materialized[-1].as_of.isoformat(),
            len(materialized),
            source_ids,
            source_hashes,
            selection.feed,
            selection.timeframe,
            selection.adjustment,
            selection.universe_id,
            str(data_path.relative_to(self.root)),
            _file_sha256(data_path),
        )
        manifest_path = self.manifest_dir / f"{dataset_id}.json"
        payload = json.dumps(asdict(manifest), sort_keys=True, separators=(",", ":")) + "\n"
        _atomic_create(manifest_path, payload.encode())
        recorded_raw = json.loads(manifest_path.read_text())
        for name in ("feature_names", "source_dataset_ids", "source_hashes"):
            recorded_raw[name] = tuple(recorded_raw[name])
        recorded = FeatureDatasetManifest(**recorded_raw)
        if recorded != manifest:
            raise ValueError("conflicting immutable feature manifest")
        return manifest


def _atomic_create(path: Path, payload: bytes) -> None:
    """Commit a new file with fsync + hard-link; an identical file is idempotent."""
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temp, path)
        except FileExistsError:
            if path.read_bytes() != payload:
                raise
    finally:
        temp.unlink(missing_ok=True)
