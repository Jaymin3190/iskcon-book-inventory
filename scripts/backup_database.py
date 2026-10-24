import argparse
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


def create_backup(source: Path, backup_dir: Path) -> Path:
    source = source.expanduser().resolve()
    backup_dir = backup_dir.expanduser()
    if not source.is_file():
        raise FileNotFoundError(f"Database not found: {source}")

    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"inventory-{timestamp}.db"
    if backup.exists():
        raise FileExistsError(f"Backup already exists: {backup}")

    source_conn = sqlite3.connect(str(source))
    backup_conn = sqlite3.connect(str(backup))
    try:
        source_conn.backup(backup_conn)
        integrity = backup_conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"Backup integrity check failed: {integrity}")

        foreign_key_errors = backup_conn.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise RuntimeError(
                f"Backup contains {len(foreign_key_errors)} foreign-key violation(s)"
            )

        counts = {}
        for table in ("books", "users", "stock"):
            counts[table] = backup_conn.execute(
                f'SELECT COUNT(*) FROM "{table}"'
            ).fetchone()[0]
    except Exception:
        backup_conn.close()
        source_conn.close()
        backup.unlink(missing_ok=True)
        raise
    else:
        backup_conn.close()
        source_conn.close()

    if os.name == "posix":
        backup_dir.chmod(0o700)
        backup.chmod(0o600)

    print(f"Backup: {backup}")
    print(f"Size: {backup.stat().st_size} bytes")
    print(f"Integrity: {integrity}")
    print(f"Foreign-key violations: {len(foreign_key_errors)}")
    for table, count in counts.items():
        print(f"{table.title()}: {count}")
    return backup


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create and verify a consistent SQLite database backup."
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(os.environ.get("DATABASE_PATH", "data/inventory.db")),
        help="SQLite database file (default: DATABASE_PATH or data/inventory.db)",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=Path.home() / "inventory-backups",
        help="Directory for timestamped backups (default: ~/inventory-backups)",
    )
    args = parser.parse_args()

    try:
        create_backup(args.database, args.backup_dir)
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        print(f"Backup failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
