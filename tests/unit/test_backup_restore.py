from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from mike_trade.domain.accounts import AccountEnvironment, AccountScope
from mike_trade.domain.trading import (
    BudgetPolicy,
    OrderIntent,
    OrderPurpose,
    OrderSide,
    OrderType,
    PositionCycle,
    TimeInForce,
)
from mike_trade.persistence import maintenance
from mike_trade.persistence.journal import DomainEventJournal, NewEvent
from mike_trade.persistence.maintenance import (
    BackupVerificationError,
    IntegrityCheckFailedError,
    RestoreRefusedError,
    apply_pending_migrations,
    check_integrity,
    checkpoint,
    create_backup,
    migration_status,
    optimize,
    rehearse_restore,
    restore_backup,
    verify_backup,
)
from mike_trade.persistence.repositories import (
    AccountScopeRepository,
    OrderIntentRepository,
    PositionCycleRepository,
)
from mike_trade.persistence.schema import initialize_database
from mike_trade.persistence.transaction import (
    WriterLock,
    WriterLockHeldError,
    WriterSession,
)

NOW = datetime(2026, 10, 8, 14, 30, 1, 123456, tzinfo=UTC)
SCOPE = AccountScope(AccountEnvironment.PAPER, "ACCT1")
POLICY = BudgetPolicy(
    per_symbol_budget=Decimal("10000"),
    total_application_budget=Decimal("20000"),
    max_open_positions=5,
)


class _Clock:
    def now(self) -> datetime:
        return NOW


def _make_db(path: Path, env: AccountEnvironment | str = AccountEnvironment.PAPER) -> Path:
    initialize_database(path, env, now=NOW).close()
    return path


def _populate(path: Path, scope: AccountScope = SCOPE) -> None:
    session = WriterSession(path)
    try:
        writer = session.bind_scope(scope.scope_id)
        clock = _Clock()
        AccountScopeRepository(writer, clock).register_scope(scope)
        cycles = PositionCycleRepository(writer, clock)
        cycle = cycles.open_cycle(
            PositionCycle(cycle_id="c1", account_scope_id=scope.scope_id, symbol="AAPL")
        )
        intents = OrderIntentRepository(writer, clock, POLICY)
        intents.record_intent(
            OrderIntent(
                intent_id="i1",
                client_order_id="client-i1",
                account_scope_id=scope.scope_id,
                cycle_id=cycle.cycle_id,
                symbol="AAPL",
                side=OrderSide.BUY,
                purpose=OrderPurpose.ENTRY,
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.DAY,
                created_at=NOW,
                quantity=Decimal("10"),
            ),
            Decimal("1000"),
        )
        journal = DomainEventJournal(writer.connection, clock=clock)
        journal.append(
            [
                NewEvent(
                    account_scope_id=scope.scope_id,
                    stream_type="ops",
                    stream_id="s1",
                    event_type="Seeded",
                    payload={"n": 1},
                    occurred_at=NOW,
                )
            ]
        )
    finally:
        session.close()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_integrity_ok_on_fresh_database(tmp_path: Path) -> None:
    path = _make_db(tmp_path / "fresh.db")
    report = check_integrity(path)
    assert report.ok is True
    assert report.integrity_check_ok is True
    assert report.foreign_key_violations == ()
    assert report.journal_mode_ok is True
    assert report.schema_ok is True
    assert report.journal_problems == ()


def test_online_backup_while_writer_holds_lock_verifies(tmp_path: Path) -> None:
    path = _make_db(tmp_path / "ops.db")
    _populate(path)
    session = WriterSession(path)
    try:
        manifest = create_backup(path, tmp_path / "backups", now=NOW)
        verification = verify_backup(manifest.backup_path)
        assert verification.ok is True
        assert verification.sha256_ok is True
        assert verification.integrity_ok is True
    finally:
        session.close()


def test_manifest_contents(tmp_path: Path) -> None:
    path = _make_db(tmp_path / "ops.db")
    _populate(path)
    manifest = create_backup(path, tmp_path / "backups", now=NOW)
    sidecar = Path(manifest.manifest_path)
    assert sidecar.is_file()
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["environment"] == "PAPER"
    assert payload["account_scope_ids"] == [SCOPE.scope_id]
    assert manifest.environment == "PAPER"
    assert manifest.account_scope_ids == (SCOPE.scope_id,)
    assert manifest.sha256 == _sha256(Path(manifest.backup_path))
    assert payload["sha256"] == manifest.sha256
    assert manifest.size_bytes == Path(manifest.backup_path).stat().st_size
    assert manifest.schema_version == 1


def test_byte_flip_detected_by_verify(tmp_path: Path) -> None:
    path = _make_db(tmp_path / "ops.db")
    _populate(path)
    manifest = create_backup(path, tmp_path / "backups", now=NOW)
    original = Path(manifest.backup_path)
    corrupted = tmp_path / "corrupted.sqlite3"
    shutil.copyfile(original, corrupted)
    shutil.copyfile(
        manifest.manifest_path, Path(str(corrupted) + ".manifest.json")
    )
    data = bytearray(corrupted.read_bytes())
    data[len(data) // 2] ^= 0xFF
    corrupted.write_bytes(bytes(data))
    verification = verify_backup(corrupted)
    assert verification.ok is False
    assert verification.sha256_ok is False or verification.integrity_ok is False


def test_missing_manifest_fails(tmp_path: Path) -> None:
    path = _make_db(tmp_path / "ops.db")
    _populate(path)
    manifest = create_backup(path, tmp_path / "backups", now=NOW)
    Path(manifest.manifest_path).unlink()
    verification = verify_backup(manifest.backup_path)
    assert verification.ok is False
    assert verification.manifest_present is False


def test_rehearse_leaves_original_byte_identical(tmp_path: Path) -> None:
    path = _make_db(tmp_path / "ops.db")
    _populate(path)
    manifest = create_backup(path, tmp_path / "backups", now=NOW)
    before = path.read_bytes()
    report = rehearse_restore(manifest.backup_path, tmp_path / "scratch")
    assert report["ok"] is True
    assert path.read_bytes() == before
    integrity = report["integrity"]
    assert integrity.ok is True


def test_cross_environment_restore_refused(tmp_path: Path) -> None:
    paper = _make_db(tmp_path / "paper.db", AccountEnvironment.PAPER)
    _populate(paper)
    live = _make_db(tmp_path / "live.db", AccountEnvironment.LIVE)
    manifest = create_backup(paper, tmp_path / "backups", now=NOW)
    with pytest.raises(RestoreRefusedError):
        restore_backup(manifest.backup_path, live, confirm_environment="PAPER")


def test_wrong_confirm_environment_refused(tmp_path: Path) -> None:
    path = _make_db(tmp_path / "ops.db")
    _populate(path)
    manifest = create_backup(path, tmp_path / "backups", now=NOW)
    with pytest.raises(RestoreRefusedError):
        restore_backup(manifest.backup_path, path, confirm_environment="LIVE")


def test_restore_while_writer_lock_held_refused(tmp_path: Path) -> None:
    path = _make_db(tmp_path / "ops.db")
    _populate(path)
    manifest = create_backup(path, tmp_path / "backups", now=NOW)
    session = WriterSession(path)
    try:
        with pytest.raises(RestoreRefusedError):
            restore_backup(manifest.backup_path, path, confirm_environment="PAPER")
    finally:
        session.close()


def test_successful_restore_preserves_pre_restore_and_passes_integrity(
    tmp_path: Path,
) -> None:
    path = _make_db(tmp_path / "ops.db")
    _populate(path)
    manifest = create_backup(path, tmp_path / "backups", now=NOW)
    # Diverge the live database after the backup (same identity, extra scope).
    session = WriterSession(path)
    try:
        writer2 = session.bind_scope("paper:ACCT2")
        AccountScopeRepository(writer2, _Clock()).register_scope(
            AccountScope(AccountEnvironment.PAPER, "ACCT2")
        )
    finally:
        session.close()
    assert len(check_integrity(path).account_scope_ids) == 2
    report = restore_backup(manifest.backup_path, path, confirm_environment="PAPER")
    assert report.ok is True
    pre = list(path.parent.glob("ops.db.pre-restore-*"))
    assert len(pre) == 1 and pre[0].is_dir()
    assert (pre[0] / "ops.db").is_file()
    assert len(check_integrity(path).account_scope_ids) == 1


def test_migration_status_no_pending_after_initialize(tmp_path: Path) -> None:
    path = _make_db(tmp_path / "ops.db")
    status = migration_status(path)
    assert status["pending_versions"] == ()
    assert status["checksum_mismatches"] == ()
    assert status["current_version"] == status["latest_version"]


def test_apply_pending_migrations_noop(tmp_path: Path) -> None:
    path = _make_db(tmp_path / "ops.db")
    status = apply_pending_migrations(path, now=NOW)
    assert status["pending_versions"] == ()


def test_checkpoint_refused_when_writer_holds_lock(tmp_path: Path) -> None:
    path = _make_db(tmp_path / "ops.db")
    _populate(path)
    session = WriterSession(path)
    try:
        with pytest.raises((WriterLockHeldError, RestoreRefusedError)):
            checkpoint(path)
    finally:
        session.close()


def test_checkpoint_and_optimize_succeed_without_competing_writer(
    tmp_path: Path,
) -> None:
    path = _make_db(tmp_path / "ops.db")
    _populate(path)
    assert len(checkpoint(path)) == 3
    optimize(path)
    assert check_integrity(path).ok is True


def test_journal_tampering_surfaces_in_check_integrity(tmp_path: Path) -> None:
    path = _make_db(tmp_path / "ops.db")
    _populate(path)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("DROP TRIGGER IF EXISTS domain_events_no_update")
        conn.execute("UPDATE domain_events SET payload_json = '{}' WHERE event_seq = 1")
        conn.commit()
    finally:
        conn.close()
    report = check_integrity(path)
    assert report.ok is False
    assert len(report.journal_problems) >= 1


def test_foreign_key_violation_surfaces(tmp_path: Path) -> None:
    path = _make_db(tmp_path / "ops.db")
    _populate(path)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "INSERT INTO fills(fill_id, account_scope_id, external_activity_id, "
            "broker_order_id, symbol, side, quantity, price, fee, filled_at, recorded_at) "
            "VALUES (?, ?, ?, ?, 'AAPL', 'BUY', '1', '10', '0', ?, ?)",
            (
                "paper:ACCT1:orphan",
                SCOPE.scope_id,
                "orphan",
                "no-such-order",
                "2026-10-08T14:30:01.123456Z",
                "2026-10-08T14:30:01.123456Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    report = check_integrity(path)
    assert report.ok is False
    assert len(report.foreign_key_violations) >= 1


def test_schema_version_mismatch_surfaces(tmp_path: Path) -> None:
    path = _make_db(tmp_path / "ops.db")
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("UPDATE schema_migrations SET version = 999 WHERE version = 1")
        conn.commit()
    finally:
        conn.close()
    report = check_integrity(path)
    assert report.ok is False
    assert report.schema_ok is False


def test_rejected_backup_restore_raises_verification_error(tmp_path: Path) -> None:
    path = _make_db(tmp_path / "ops.db")
    _populate(path)
    manifest = create_backup(path, tmp_path / "backups", now=NOW)
    Path(manifest.manifest_path).unlink()
    with pytest.raises(BackupVerificationError):
        restore_backup(manifest.backup_path, path, confirm_environment="PAPER")


def test_public_api_surface() -> None:
    for name in (
        "check_integrity",
        "migration_status",
        "apply_pending_migrations",
        "create_backup",
        "verify_backup",
        "rehearse_restore",
        "restore_backup",
        "checkpoint",
        "optimize",
        "IntegrityReport",
        "BackupManifest",
        "BackupVerification",
        "BackupVerificationError",
        "RestoreRefusedError",
        "IntegrityCheckFailedError",
    ):
        assert hasattr(maintenance, name), name
