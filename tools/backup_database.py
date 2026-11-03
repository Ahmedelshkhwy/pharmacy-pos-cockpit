"""Back up the app database (SQLite or PostgreSQL) into one portable SQLite file.

    python tools/backup_database.py                 # -> backups/pharmacy-YYYYMMDD-HHMMSS.db
    python tools/backup_database.py my-backup.db
    DATABASE_URL=postgresql://... python tools/backup_database.py

Restore:
- SQLite install: stop the app and copy the backup over instance/pharmacy.db.
- PostgreSQL install: on an empty database,
  DATABASE_URL=postgresql://... python tools/migrate_sqlite_to_postgres.py backups/pharmacy-....db

The file holds users (password hashes), settings and all business data: keep it private.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def backup(target: Path) -> dict[str, int]:
    import app
    import db as database

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise SystemExit(f"{target} already exists; choose another name.")
    if not app.DATABASE_URL:
        # SQLite: the built-in online backup copies a consistent snapshot even while the app runs.
        with sqlite3.connect(app.DB_PATH) as source, sqlite3.connect(target) as copy:
            source.backup(copy)
        with sqlite3.connect(target) as copy:
            tables = [row[0] for row in copy.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
            return {table: copy.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}

    # PostgreSQL: create the same schema in a new SQLite file, then copy every table.
    source_url, source_path = app.DATABASE_URL, app.DB_PATH
    app.DATABASE_URL, app.DB_PATH = None, target
    try:
        app.init_db()
    finally:
        app.DATABASE_URL, app.DB_PATH = source_url, source_path
    counts: dict[str, int] = {}
    with database.connect(source_url, target) as source, sqlite3.connect(target) as copy:
        tables = [row[0] for row in source.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")]
        for table in tables:
            target_columns = {row[1] for row in copy.execute(f"PRAGMA table_info({table})")}
            if not target_columns:
                continue
            columns = sorted(source.table_columns(table) & target_columns)
            rows = [tuple(row[column] for column in columns) for row in source.execute(f"SELECT {', '.join(columns)} FROM {table}")]
            copy.execute(f"DELETE FROM {table}")  # e.g. the default admin init_db just created
            copy.executemany(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})", rows)
            counts[table] = len(rows)
    return counts


def main() -> None:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "backups" / f"pharmacy-{stamp}.db"
    for table, count in sorted(backup(target).items()):
        print(f"{table}: {count} rows")
    print(f"Backup written to {target}")


if __name__ == "__main__":
    main()
