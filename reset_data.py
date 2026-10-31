import argparse
import os
import sqlite3
import sys
from pathlib import Path

from scripts.backup_database import create_backup


PRESERVED_TABLES = {"users", "locations"}


def clear_application_data(database: Path) -> tuple[int, int, list[str]]:
    conn = sqlite3.connect(str(database))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("PRAGMA defer_foreign_keys = ON")

        tables = [
            row[0]
            for row in conn.execute(
                """SELECT name FROM sqlite_master
                   WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                   ORDER BY name"""
            )
        ]
        missing = PRESERVED_TABLES.difference(tables)
        if missing:
            raise RuntimeError(
                "Database is missing required table(s): "
                + ", ".join(sorted(missing))
            )

        user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        location_count = conn.execute("SELECT COUNT(*) FROM locations").fetchone()[0]
        cleared = [name for name in tables if name not in PRESERVED_TABLES]
        for name in cleared:
            quoted_name = '"' + name.replace('"', '""') + '"'
            conn.execute(f"DELETE FROM {quoted_name}")

        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sqlite_sequence'"
        ).fetchone():
            conn.execute(
                "DELETE FROM sqlite_sequence WHERE name NOT IN ('users', 'locations')"
            )

        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(
                f"Reset would leave {len(violations)} foreign-key violation(s)"
            )

        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"Database integrity check failed: {integrity}")

        conn.commit()
        return user_count, location_count, cleared
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Delete all application data except users and locations."
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
        help="Directory for a verified pre-reset backup",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Confirm deletion of all records except users and locations",
    )
    args = parser.parse_args()

    if not args.confirm:
        parser.error("this operation requires --confirm")

    database = args.database.expanduser().resolve()
    if not database.is_file():
        print(f"Database not found: {database}", file=sys.stderr)
        return 1

    try:
        backup = create_backup(database, args.backup_dir)
        users, locations, cleared = clear_application_data(database)
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        print(f"Reset failed: {exc}", file=sys.stderr)
        return 1

    print(f"Preserved users: {users}")
    print(f"Preserved locations: {locations}")
    print(f"Cleared tables: {', '.join(cleared) if cleared else '(none)'}")
    print(f"Verified backup: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
