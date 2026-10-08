"""SQLite integrity, migration, backup, restore, and maintenance services.

All routines in this module are application services: they contain the
database business logic so later CLI/Makefile recipes can delegate without
reimplementing it. No function here deletes or prunes ledger or append-only
rows; corruption is reported, never silently ignored.

Design choices (4, the task's decision budget):
1. ``migration_status`` returns a plain ``dict`` (applied/pending/checksum
   lists plus current/latest versions) so no extra public status type is
   needed beyond the required report dataclasses.
2. Backup manifests are JSON sidecars named ``<backup>.manifest.json`` next
   to the backup file; ``verify_backup`` also accepts the legacy
   ``<stem>.manifest.json`` spelling.
3. Pre-restore preservation moves the live database file plus its ``-wal``
   and ``-shm`` sidecars into a timestamped sibling directory named
   ``<target name>.pre-restore-<UTC stamp>``; the writer-lock file itself is
   never moved. The restored file is installed atomically with temp-file plus
   ``os.replace`` in the target directory.
4. ``checkpoint``/``optimize``/``apply_pending_migrations`` prove single
   ownership by acquiring the target's ``WriterLock`` for the duration of the
   operation; when another process holds that lock the acquisition raises and
   the operation is refused. ``verify_backup`` reports failures via the
   returned ``BackupVerification.ok`` flag while ``restore_backup`` and
   ``create_backup`` escalate failures to exceptions.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from mike_trade.domain.errors import PersistenceError
from mike_trade.persistence import schema
from mike_trade.persistence.journal import DomainEventJournal
from mike_trade.persistence.transaction import WriterLock, WriterLockHeldError

__all__ = [
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
]


class BackupVerificationError(PersistenceError):
    """A backup failed verification and must not be restored."""


class RestoreRefusedError(PersistenceError):
    """A restore was refused by an isolation, confirmation, or locking rule."""


class IntegrityCheckFailedError(PersistenceError):
    """A database or backup copy failed its integrity checks."""


@dataclass(frozen=True, slots=True)
class IntegrityReport:
    """Result of :func:`check_integrity` for one database file."""

    db_path: str
    ok: bool
    integrity_check_ok: bool
    integrity_check_messages: tuple[str, ...]
    foreign_key_violations: tuple[str, ...]
    journal_mode: str
    journal_mode_ok: bool
    schema_version: int
    expected_schema_version: int
    schema_ok: bool
    checksum_mismatches: tuple[int, ...]
    journal_problems: tuple[str, ...]
    environment: str | None
    database_id: str | None
    account_scope_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BackupManifest:
    """Manifest written beside every online backup."""

    backup_path: str
    manifest_path: str
    environment: str
    database_id: str
    account_scope_ids: tuple[str, ...]
    schema_version: int
    sha256: str
    size_bytes: int
    created_at: str
    integrity_ok: bool
    integrity_messages: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class BackupVerification:
    """Result of :func:`verify_backup` for one backup file."""

    backup_path: str
    ok: bool
    manifest_present: bool
    sha256_ok: bool
    expected_sha256: str | None
    actual_sha256: str | None
    integrity_ok: bool
    integrity_messages: tuple[str, ...]
    error: str | None = None


_CHECKPOINT_MODES = frozenset({"PASSIVE", "FULL", "RESTART", "TRUNCATE"})


def _compact_stamp(now: datetime) -> str:
    _require_aware(now)
    return now.astimezone(UTC).strftime("%Y%m%dT%H%M%S-%fZ")


def _require_aware(now: datetime) -> datetime:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    return now.astimezone(UTC)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 64), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_candidates(backup_path: Path) -> list[Path]:
    return [
        Path(str(backup_path) + ".manifest.json"),
        backup_path.with_suffix(".manifest.json"),
    ]


def _find_manifest(backup_path: Path) -> Path | None:
    for candidate in _manifest_candidates(backup_path):
        if candidate.is_file():
            return candidate
    return None


def _read_identity(conn: sqlite3.Connection) -> tuple[str | None, str | None]:
    try:
        row = conn.execute(
            "SELECT environment, database_id FROM db_identity WHERE id = 1"
        ).fetchone()
    except sqlite3.Error:
        return None, None
    if row is None:
        return None, None
    return str(row[0]), str(row[1])


def _checksum_mismatches(
    conn: sqlite3.Connection, known_by_version: dict[int, str]
) -> tuple[int, ...]:
    try:
        rows = conn.execute("SELECT version, sha256 FROM schema_migrations").fetchall()
    except sqlite3.Error:
        return ()
    mismatched: list[int] = []
    for version, recorded in rows:
        known = known_by_version.get(int(version))
        if known is not None and str(recorded) != known:
            mismatched.append(int(version))
    return tuple(sorted(mismatched))


class _VerifyClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


def check_integrity(db_path: str | Path) -> IntegrityReport:
    """Run read-only integrity checks over one database file."""
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise PersistenceError(f"database does not exist: {path}")
    conn = schema.open_connection(path, read_only=True)
    try:
        integrity_rows = conn.execute("PRAGMA integrity_check").fetchall()
        messages = tuple(str(row[0]) for row in integrity_rows)
        integrity_ok = len(messages) == 1 and messages[0].lower() == "ok"

        fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
        fk_violations = tuple(
            f"table={row[0]} rowid={row[1]} ref={row[2]} fk={row[3]}" for row in fk_rows
        )

        mode_row = conn.execute("PRAGMA journal_mode").fetchone()
        journal_mode = str(mode_row[0]) if mode_row is not None else ""
        journal_ok = journal_mode.lower() == "wal"

        current_version = schema.current_schema_version(conn)
        migrations = schema.discover_migrations()
        expected_version = migrations[-1].version if migrations else 0
        known_by_version = {migration.version: migration.sha256 for migration in migrations}
        mismatches = _checksum_mismatches(conn, known_by_version)
        schema_ok = (
            current_version == expected_version
            and not mismatches
            and current_version > 0
        )

        try:
            scope_rows = conn.execute(
                "SELECT account_scope_id FROM account_scopes ORDER BY account_scope_id"
            ).fetchall()
            scope_ids = tuple(str(row[0]) for row in scope_rows)
        except sqlite3.Error:
            scope_ids = ()

        journal = DomainEventJournal(conn, clock=_VerifyClock())
        problems: list[str] = []
        for scope_id in scope_ids:
            try:
                scope_problems = journal.verify_chain(scope_id)
            except Exception as exc:  # noqa: BLE001 - evidence, not control flow
                problems.append(f"{scope_id}: verify_chain error: {exc!r}")
                continue
            problems.extend(f"{scope_id}: {item}" for item in scope_problems)
        journal_ok = not problems

        environment, database_id = _read_identity(conn)
        ok = bool(
            integrity_ok
            and not fk_violations
            and journal_ok
            and journal_ok
            and schema_ok
        )
        return IntegrityReport(
            db_path=str(path),
            ok=ok,
            integrity_check_ok=integrity_ok,
            integrity_check_messages=messages,
            foreign_key_violations=fk_violations,
            journal_mode=journal_mode,
            journal_mode_ok=journal_ok,
            schema_version=current_version,
            expected_schema_version=expected_version,
            schema_ok=schema_ok,
            checksum_mismatches=mismatches,
            journal_problems=tuple(problems),
            environment=environment,
            database_id=database_id,
            account_scope_ids=scope_ids,
        )
    finally:
        conn.close()


def migration_status(db_path: str | Path) -> dict[str, object]:
    """Report applied versions, pending versions, and checksum mismatches."""
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise PersistenceError(f"database does not exist: {path}")
    conn = schema.open_connection(path, read_only=True)
    try:
        migrations = schema.discover_migrations()
        known_by_version = {migration.version: migration.sha256 for migration in migrations}
        try:
            rows = conn.execute("SELECT version, sha256 FROM schema_migrations").fetchall()
        except sqlite3.Error:
            rows = []
        applied = sorted(int(version) for version, _ in rows)
        applied_set = set(applied)
        mismatches = sorted(
            int(version)
            for version, recorded in rows
            if known_by_version.get(int(version)) is not None
            and str(recorded) != known_by_version[int(version)]
        )
        pending = [m.version for m in migrations if m.version not in applied_set]
        current = schema.current_schema_version(conn)
        latest = migrations[-1].version if migrations else 0
        return {
            "db_path": str(path),
            "applied_versions": tuple(applied),
            "pending_versions": tuple(pending),
            "checksum_mismatches": tuple(mismatches),
            "current_version": current,
            "latest_version": latest,
        }
    finally:
        conn.close()


def apply_pending_migrations(db_path: str | Path, *, now: datetime) -> dict[str, object]:
    """Apply pending forward migrations while holding the writer lock."""
    _require_aware(now)
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise PersistenceError(f"database does not exist: {path}")
    try:
        lock = WriterLock(path)
    except WriterLockHeldError as exc:
        raise WriterLockHeldError(
            f"cannot migrate {path}: another writer holds the lock"
        ) from exc
    try:
        conn = schema.open_connection(path)
        try:
            schema.apply_migrations(conn, now=now)
        finally:
            conn.close()
    finally:
        lock.close()
    return migration_status(path)


def create_backup(
    db_path: str | Path, backup_dir: str | Path, *, now: datetime
) -> BackupManifest:
    """Take an online backup with a JSON manifest sidecar."""
    _require_aware(now)
    source_path = Path(db_path).expanduser().resolve()
    if not source_path.is_file():
        raise PersistenceError(f"database does not exist: {source_path}")
    dest_dir = Path(backup_dir).expanduser().resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)

    probe = schema.open_connection(source_path, read_only=True)
    try:
        environment, database_id = _read_identity(probe)
        version = schema.current_schema_version(probe)
        try:
            scope_rows = probe.execute(
                "SELECT account_scope_id FROM account_scopes ORDER BY account_scope_id"
            ).fetchall()
            scope_ids = tuple(str(row[0]) for row in scope_rows)
        except sqlite3.Error:
            scope_ids = ()
    finally:
        probe.close()
    if environment is None or database_id is None:
        raise PersistenceError(f"database {source_path} has no identity")

    stamp = _compact_stamp(now)
    backup_name = f"{environment}-{database_id}-{stamp}.sqlite3"
    backup_path = dest_dir / backup_name
    if backup_path.exists():
        raise PersistenceError(f"backup destination already exists: {backup_path}")

    source = schema.open_connection(source_path, read_only=True)
    dest = sqlite3.connect(backup_path, isolation_level=None)
    try:
        try:
            dest.execute("PRAGMA journal_mode = DELETE")
        except sqlite3.Error:
            pass
        source.backup(dest)
    finally:
        try:
            dest.close()
        finally:
            source.close()

    report = check_integrity(backup_path)
    if not report.integrity_check_ok or not report.ok:
        try:
            backup_path.unlink()
        except OSError:
            pass
        raise IntegrityCheckFailedError(
            f"backup copy {backup_path} failed integrity_check: "
            f"{report.integrity_check_messages!r}"
        )

    sha256 = _sha256_file(backup_path)
    size_bytes = backup_path.stat().st_size
    created_at = schema.to_db_timestamp(now.astimezone(UTC))
    manifest_path = Path(str(backup_path) + ".manifest.json")
    payload = {
        "backup_file": backup_path.name,
        "environment": environment,
        "database_id": database_id,
        "account_scope_ids": list(scope_ids),
        "schema_version": version,
        "sha256": sha256,
        "size_bytes": size_bytes,
        "created_at": created_at,
        "integrity_ok": report.ok,
        "integrity_check_ok": report.integrity_check_ok,
        "integrity_check_messages": list(report.integrity_check_messages),
    }
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return BackupManifest(
        backup_path=str(backup_path),
        manifest_path=str(manifest_path),
        environment=environment,
        database_id=database_id,
        account_scope_ids=scope_ids,
        schema_version=version,
        sha256=sha256,
        size_bytes=size_bytes,
        created_at=created_at,
        integrity_ok=report.ok,
        integrity_messages=report.integrity_check_messages,
    )


def verify_backup(backup_path: str | Path) -> BackupVerification:
    """Recompute sha256 against the manifest and run integrity_check."""
    path = Path(backup_path).expanduser().resolve()
    if not path.is_file():
        return BackupVerification(
            backup_path=str(path),
            ok=False,
            manifest_present=False,
            sha256_ok=False,
            expected_sha256=None,
            actual_sha256=None,
            integrity_ok=False,
            integrity_messages=(),
            error=f"backup file does not exist: {path}",
        )
    manifest_path = _find_manifest(path)
    if manifest_path is None:
        try:
            report = check_integrity(path)
            integrity_ok = report.ok
            messages: tuple[str, ...] = report.integrity_check_messages
        except Exception as exc:  # noqa: BLE001 - verification evidence
            integrity_ok = False
            messages = (f"integrity probe failed: {exc!r}",)
        return BackupVerification(
            backup_path=str(path),
            ok=False,
            manifest_present=False,
            sha256_ok=False,
            expected_sha256=None,
            actual_sha256=None,
            integrity_ok=integrity_ok,
            integrity_messages=messages,
            error="missing manifest sidecar",
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return BackupVerification(
            backup_path=str(path),
            ok=False,
            manifest_present=True,
            sha256_ok=False,
            expected_sha256=None,
            actual_sha256=None,
            integrity_ok=False,
            integrity_messages=(),
            error=f"unreadable manifest: {exc!r}",
        )
    expected = manifest.get("sha256")
    actual = _sha256_file(path)
    sha_ok = isinstance(expected, str) and actual == expected
    try:
        report = check_integrity(path)
        integrity_ok = report.ok
        messages = report.integrity_check_messages
    except Exception as exc:  # noqa: BLE001 - verification evidence
        integrity_ok = False
        messages = (f"integrity probe failed: {exc!r}",)
    problems: list[str] = []
    if not sha_ok:
        problems.append("sha256 mismatch")
    if not integrity_ok:
        problems.append("integrity_check failed")
    ok = sha_ok and integrity_ok
    return BackupVerification(
        backup_path=str(path),
        ok=ok,
        manifest_present=True,
        sha256_ok=sha_ok,
        expected_sha256=expected if isinstance(expected, str) else None,
        actual_sha256=actual,
        integrity_ok=integrity_ok,
        integrity_messages=messages,
        error=None if ok else "; ".join(problems),
    )


def rehearse_restore(backup_path: str | Path, scratch_dir: str | Path) -> dict[str, object]:
    """Restore a backup copy to scratch and validate it; never touches live data."""
    source = Path(backup_path).expanduser().resolve()
    scratch = Path(scratch_dir).expanduser().resolve()
    scratch.mkdir(parents=True, exist_ok=True)
    verification = verify_backup(source)
    rehearsed_path = scratch / f"{source.stem}.rehearsal.sqlite3"
    if rehearsed_path.exists():
        rehearsed_path.unlink()
    shutil.copyfile(source, rehearsed_path)
    manifest_sidecar = _find_manifest(source)
    if manifest_sidecar is not None:
        shutil.copyfile(
            manifest_sidecar, Path(str(rehearsed_path) + ".manifest.json")
        )
    integrity = check_integrity(rehearsed_path)
    status = migration_status(rehearsed_path)
    ok = bool(verification.ok and integrity.ok and not status["pending_versions"])
    return {
        "backup_path": str(source),
        "rehearsed_path": str(rehearsed_path),
        "verification": verification,
        "integrity": integrity,
        "migration_status": status,
        "ok": ok,
    }


def restore_backup(
    backup_path: str | Path, target_db_path: str | Path, *, confirm_environment: str
) -> IntegrityReport:
    """Verify, isolate-check, preserve, and atomically install a backup."""
    source = Path(backup_path).expanduser().resolve()
    target = Path(target_db_path).expanduser().resolve()
    verification = verify_backup(source)
    if not verification.ok:
        raise BackupVerificationError(
            f"backup {source} failed verification: {verification.error}"
        )
    manifest_path = _find_manifest(source)
    assert manifest_path is not None
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_env = str(manifest["environment"])
    manifest_id = str(manifest["database_id"])
    if confirm_environment != manifest_env:
        raise RestoreRefusedError(
            f"confirm_environment {confirm_environment!r} does not match "
            f"backup environment {manifest_env!r}"
        )
    try:
        lock = WriterLock(target)
    except WriterLockHeldError as exc:
        raise RestoreRefusedError(
            f"refusing restore: another writer holds {target}"
        ) from exc
    try:
        if target.is_file():
            probe = schema.open_connection(target, read_only=True)
            try:
                target_env, target_id = _read_identity(probe)
            finally:
                probe.close()
            if target_env != manifest_env or target_id != manifest_id:
                raise RestoreRefusedError(
                    f"refusing cross-identity restore: backup "
                    f"{manifest_env}/{manifest_id} onto target "
                    f"{target_env}/{target_id}"
                )
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S-%fZ")
            pre_dir = target.parent / f"{target.name}.pre-restore-{stamp}"
            pre_dir.mkdir(parents=True, exist_ok=False)
            shutil.move(str(target), str(pre_dir / target.name))
            for suffix in ("-wal", "-shm", "-journal"):
                sidecar = Path(str(target) + suffix)
                if sidecar.is_file():
                    shutil.move(str(sidecar), str(pre_dir / (target.name + suffix)))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            for suffix in ("-wal", "-shm", "-journal"):
                sidecar = Path(str(target) + suffix)
                if sidecar.is_file():
                    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S-%fZ")
                    pre_dir = target.parent / f"{target.name}.pre-restore-{stamp}"
                    pre_dir.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(sidecar), str(pre_dir / (target.name + suffix)))

        fd, tmp_name = tempfile.mkstemp(
            dir=str(target.parent), prefix=target.name + ".restore-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                with open(source, "rb") as src:
                    shutil.copyfileobj(src, handle, length=1024 * 64)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, target)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        report = check_integrity(target)
        if not report.ok:
            raise IntegrityCheckFailedError(
                f"restored database {target} failed integrity: "
                f"{report.integrity_check_messages!r} "
                f"violations={report.foreign_key_violations!r} "
                f"journal={report.journal_problems!r}"
            )
        return report
    finally:
        lock.close()


def checkpoint(db_path: str | Path, mode: str = "TRUNCATE") -> tuple[int, int, int]:
    """Run a WAL checkpoint while holding the writer lock."""
    normalized = str(mode).upper()
    if normalized not in _CHECKPOINT_MODES:
        raise ValueError(f"checkpoint mode must be one of {sorted(_CHECKPOINT_MODES)}")
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise PersistenceError(f"database does not exist: {path}")
    lock = WriterLock(path)
    try:
        conn = schema.open_connection(path)
        try:
            row = conn.execute(f"PRAGMA wal_checkpoint({normalized})").fetchone()
            return (int(row[0]), int(row[1]), int(row[2]))
        finally:
            conn.close()
    finally:
        lock.close()


def optimize(db_path: str | Path) -> None:
    """Run PRAGMA optimize and ANALYZE while holding the writer lock."""
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise PersistenceError(f"database does not exist: {path}")
    lock = WriterLock(path)
    try:
        conn = schema.open_connection(path)
        try:
            conn.execute("PRAGMA optimize")
            conn.execute("ANALYZE")
        finally:
            conn.close()
    finally:
        lock.close()
