"""Copy everything from the local SQLite file into PostgreSQL (users, settings, data, history).

Run once when switching to PostgreSQL:
    DATABASE_URL=postgresql://user:pass@host:5432/dbname python tools/migrate_sqlite_to_postgres.py [instance/pharmacy.db]

The target database must be empty of app data; the script refuses to overwrite existing rows.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Tables in dependency order (parents before children).
TABLES = ["users", "app_settings", "mapping_profiles", "import_runs", "uploads", "items", "snapshots", "item_history"]
SERIAL_TABLES = ["users", "uploads", "items", "snapshots"]
DATA_TABLES = ["uploads", "items"]


def migrate(sqlite_path: Path, database_url: str) -> dict[str, int]:
    os.environ["DATABASE_URL"] = database_url
    import app  # creates the PostgreSQL schema on import

    app.DATABASE_URL = database_url
    app.init_db()
    source = sqlite3.connect(sqlite_path)
    source.row_factory = sqlite3.Row
    copied: dict[str, int] = {}
    with app.get_db() as target:
        for table in DATA_TABLES:
            if target.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]:
                raise SystemExit(f"Target table {table} already has rows; refusing to overwrite.")
        # Starting the app on an empty database creates a default admin, internal flags, and history
        # rebuilt from analysis folders on disk; the source copies replace all of them.
        for table in ("item_history", "snapshots", "users", "app_settings", "mapping_profiles", "import_runs"):
            target.execute(f"DELETE FROM {table}")
        source_tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        for table in TABLES:
            if table not in source_tables:
                continue
            columns = sorted(set(row[1] for row in source.execute(f"PRAGMA table_info({table})")) & target.table_columns(table))
            rows = [tuple(row[column] for column in columns) for row in source.execute(f"SELECT {', '.join(columns)} FROM {table}")]
            if rows:
                target.executemany(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})", rows)
            copied[table] = len(rows)
        for table in SERIAL_TABLES:
            target.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)")
    return copied


def main() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("Set DATABASE_URL to the PostgreSQL database to copy into.")
    sqlite_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "instance" / "pharmacy.db"
    if not sqlite_path.exists():
        raise SystemExit(f"SQLite file not found: {sqlite_path}")
    for table, count in migrate(sqlite_path, database_url).items():
        print(f"{table}: {count} rows")
    print("Done. Keep DATABASE_URL set when starting the app.")


if __name__ == "__main__":
    main()
