"""Back up the app database (SQLite or PostgreSQL) into one portable SQLite file.

    python tools/backup_database.py                 # -> backups/pharmacy-YYYYMMDD-HHMMSS.db
    python tools/backup_database.py my-backup.db
    DATABASE_URL=postgresql://... python tools/backup_database.py

Every organization that signed up gets its own file next to it: pharmacy-...-org2.db, -org3.db, ...
(the main file holds the first organization plus the shared tables: users, organizations, leads).

Restore:
- SQLite install: stop the app, copy the main file over instance/pharmacy.db and each -orgN.db
  file to instance/orgs/N/pharmacy.db.
- PostgreSQL install: on an empty database (the -orgN.db files next to it are picked up too),
  DATABASE_URL=postgresql://... python tools/migrate_sqlite_to_postgres.py backups/pharmacy-....db

The files hold users (password hashes), settings and all business data: keep them private.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def org_backup_path(target: Path, org_id: int) -> Path:
    return target.with_name(f"{target.stem}-org{org_id}{target.suffix or '.db'}")


def sqlite_counts(path: Path) -> dict[str, int]:
    with sqlite3.connect(path) as copy:
        tables = [row[0] for row in copy.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        return {table: copy.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}


def copy_tables(source, target: Path, schema: str) -> dict[str, int]:
    """Copy every table of one PostgreSQL schema into a SQLite file that already has the tables."""
    counts: dict[str, int] = {}
    with sqlite3.connect(target) as copy:
        tables = [row[0] for row in source.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = ?", (schema,))]
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


def backup(target: Path) -> dict[str, int]:
    """Back up everything; returns row counts ("items", ...; "org2.items", ... for other organizations)."""
    import app
    import db as database

    target.parent.mkdir(parents=True, exist_ok=True)
    with app.platform_db() as db:
        org_ids = [int(row["id"]) for row in db.execute("SELECT id FROM organizations WHERE id != ? ORDER BY id", (app.PLATFORM_ORG_ID,))]
    for path in [target, *(org_backup_path(target, org_id) for org_id in org_ids)]:
        if path.exists():
            raise SystemExit(f"{path} already exists; choose another name.")

    counts: dict[str, int] = {}
    if not app.DATABASE_URL:
        # SQLite: the built-in online backup copies a consistent snapshot even while the app runs.
        for org_id, source_path, copy_path in [(app.PLATFORM_ORG_ID, app.DB_PATH, target),
                                               *((org_id, app.org_db_path(org_id), org_backup_path(target, org_id)) for org_id in org_ids)]:
            if not source_path.exists():
                continue
            with sqlite3.connect(source_path) as source, sqlite3.connect(copy_path) as copy:
                source.backup(copy)
            prefix = "" if org_id == app.PLATFORM_ORG_ID else f"org{org_id}."
            counts.update({prefix + table: count for table, count in sqlite_counts(copy_path).items()})
        return counts

    # PostgreSQL: create the same tables in new SQLite files, then copy every table.
    source_url, source_path = app.DATABASE_URL, app.DB_PATH
    app.DATABASE_URL, app.DB_PATH = None, target
    try:
        app.init_db()
    finally:
        app.DATABASE_URL, app.DB_PATH = source_url, source_path
    with database.connect(source_url, target) as source:
        counts.update(copy_tables(source, target, "public"))
    for org_id in org_ids:
        copy_path = org_backup_path(target, org_id)
        with database.connect(None, copy_path) as copy:
            app.init_org_schema(copy)
        with database.connect(source_url, target, schema=f"org_{org_id}") as source:
            counts.update({f"org{org_id}.{table}": count for table, count in copy_tables(source, copy_path, f"org_{org_id}").items()})
    return counts


def main() -> None:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "backups" / f"pharmacy-{stamp}.db"
    for table, count in sorted(backup(target).items()):
        print(f"{table}: {count} rows")
    print(f"Backup written to {target} (other organizations: {target.stem}-orgN{target.suffix} next to it)")


if __name__ == "__main__":
    main()
