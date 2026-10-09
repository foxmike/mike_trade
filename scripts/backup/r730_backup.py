#!/usr/bin/env python3
"""Cron entrypoint for consistent R730 operational and research backups."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path

from mike_trade.persistence.maintenance import create_backup, verify_backup

ROOT = Path(__file__).resolve().parents[2]
DRIVE = Path("/run/media/mike/SAS_DRIVE_1")
BACKUP_ROOT = DRIVE / "mike_trade"
STATUS_DIR = Path.home() / ".local/state/mike_trade"
DRIVE_UUID = "b4883d76-a34f-4976-a5d1-ba973de4a837"


def _atomic_json(path: Path, data: dict[str, object]) -> None:
    temporary = path.with_name(path.name + ".pending")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_drive() -> None:
    if not DRIVE.is_mount():
        raise RuntimeError(f"backup drive is not mounted at {DRIVE}")
    actual = subprocess.check_output(
        ["findmnt", "-n", "-o", "UUID", "--target", str(DRIVE)], text=True
    ).strip()
    if actual != DRIVE_UUID:
        raise RuntimeError(f"unexpected backup drive UUID at {DRIVE}: {actual}")


def _copy_recovery_config(target: Path) -> None:
    target.mkdir(mode=0o700, parents=True)
    for relative in (".env", "config/mike_trade.yaml", "compose.yaml", "compose.r730.yaml"):
        source = ROOT / relative
        if not source.is_file():
            raise FileNotFoundError(f"required recovery configuration missing: {relative}")
        destination = target / relative
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        destination.chmod(0o600)
    shutil.copy2("/etc/fstab", target / "fstab")
    (target / "fstab").chmod(0o600)
    crontab = subprocess.check_output(["crontab", "-l"], text=True)
    (target / "crontab.txt").write_text(crontab)
    (target / "crontab.txt").chmod(0o600)


def _prune(directory: Path, *, keep: int) -> None:
    """Remove only complete backup generations after a new one succeeds."""
    generations = sorted(path for path in directory.iterdir() if path.is_dir()
                         and not path.name.endswith(".pending"))
    for old in generations[:-keep]:
        shutil.rmtree(old)


def _operational(now: datetime) -> dict[str, object]:
    destination = BACKUP_ROOT / "operational"
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = ROOT / "state/r730/shadow.sqlite3"
    manifest = create_backup(db, destination, now=now)
    verification = verify_backup(manifest.backup_path)
    if not verification.ok:
        raise RuntimeError(f"operational backup failed verification: {verification.error}")
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    config = BACKUP_ROOT / "configuration" / stamp
    _copy_recovery_config(config)
    backups = sorted(destination.glob("*.sqlite3.manifest.json"))
    for old_manifest in backups[:-14]:
        old_database = old_manifest.with_name(old_manifest.name.removesuffix(".manifest.json"))
        old_manifest.unlink()
        old_database.unlink(missing_ok=True)
    _prune(config.parent, keep=14)
    return {"backup_file": Path(manifest.backup_path).name,
            "database_id": manifest.database_id, "sha256": manifest.sha256,
            "verified": True, "size_bytes": manifest.size_bytes}


def _research(now: datetime) -> dict[str, object]:
    import duckdb

    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    destination = BACKUP_ROOT / "research"
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    pending = destination / (stamp + ".pending")
    completed = destination / stamp
    if pending.exists() or completed.exists():
        raise FileExistsError(f"backup generation already exists: {stamp}")
    pending.mkdir(mode=0o700)
    # Catalog writes are serialized by the existing research writer lock.
    lock_path = ROOT / "research/data/.catalog-write.lock"
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        catalog = duckdb.connect(str(ROOT / "research/data/catalog.duckdb"), read_only=True)
        try:
            export = pending / "catalog-export"
            catalog.execute("EXPORT DATABASE '" + str(export).replace("'", "''") + "'")
            catalog_count = catalog.execute("SELECT count(*) FROM datasets").fetchone()[0]
        finally:
            catalog.close()
    subprocess.run([
        "rsync", "-a", "--exclude=/data/catalog.duckdb*",
        "--exclude=*.lock", "--exclude=*.tmp", str(ROOT / "research") + "/",
        str(pending / "research") + "/",
    ], check=True)
    # BookieMonster also keeps weekly code snapshots. Exclude large/generated data.
    subprocess.run([
        "rsync", "-a", "--exclude=/.git/", "--exclude=/.venv/",
        "--exclude=/research/", "--exclude=/state/", "--exclude=/node_modules/",
        "--exclude=/.env", "--exclude=/config/mike_trade.yaml",
        "--exclude=__pycache__/", "--exclude=*.pyc", str(ROOT) + "/",
        str(pending / "code") + "/",
    ], check=True)
    checksums: dict[str, str] = {}
    for manifest_path in sorted((pending / "research/data/manifests").glob("*.json")):
        data = json.loads(manifest_path.read_text())
        snapshot = pending / "research/data" / data["data_file"]
        actual = _sha256(snapshot)
        if actual != data["sha256"]:
            raise RuntimeError(f"research snapshot hash mismatch: {data['dataset_id']}")
        checksums[data["dataset_id"]] = actual
    exported = duckdb.connect(":memory:")
    try:
        exported.execute("IMPORT DATABASE '" + str(pending / "catalog-export").replace("'", "''") + "'")
        if exported.execute("SELECT count(*) FROM datasets").fetchone()[0] != catalog_count:
            raise RuntimeError("research catalog export row count mismatch")
    finally:
        exported.close()
    _atomic_json(pending / "manifest.json", {
        "created_at": now.isoformat(), "catalog_dataset_count": catalog_count,
        "snapshot_sha256": checksums, "research_source": str(ROOT / "research"),
    })
    os.replace(pending, completed)
    _prune(destination, keep=8)
    return {"generation": completed.name, "catalog_dataset_count": catalog_count,
            "verified_snapshots": len(checksums), "verified": True}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("operational", "research"))
    args = parser.parse_args()
    os.umask(0o077)
    STATUS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (STATUS_DIR / "backup.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        status_path = STATUS_DIR / "backup-status.json"
        try:
            status = json.loads(status_path.read_text())
        except (OSError, ValueError):
            status = {}
        now = datetime.now(UTC)
        try:
            _require_drive()
            result = _operational(now) if args.mode == "operational" else _research(now)
        except Exception as exc:
            status[args.mode] = {"last_status": "failed", "attempted_at": now.isoformat(),
                                 "error": f"{type(exc).__name__}: {exc}"}
            _atomic_json(status_path, status)
            traceback.print_exc()
            return 1
        status[args.mode] = {"last_status": "ok", "attempted_at": now.isoformat(),
                             "last_success_at": now.isoformat(), **result}
        _atomic_json(status_path, status)
        print(json.dumps({"mode": args.mode, **status[args.mode]}, sort_keys=True))
        return 0


if __name__ == "__main__":
    sys.exit(main())
