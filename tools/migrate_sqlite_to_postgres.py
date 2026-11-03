"""Copy everything from the local SQLite file into PostgreSQL (users, settings, data, history).

Run once when switching to PostgreSQL:
    DATABASE_URL=postgresql://user:pass@host:5432/dbname python tools/migrate_sqlite_to_postgres.py [instance/pharmacy.db]

Organizations that signed up are copied too: from instance/orgs/N/pharmacy.db next to the main file,
or from the -orgN.db files a backup wrote next to it (tools/backup_database.py).

The target database must be empty of app data; the script refuses to overwrite existing rows.
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Tables in dependency order (parents before children).
TABLES = ["organizations", "users", "leads", "app_settings", "mapping_profiles", "import_runs", "uploads", "items", "snapshots", "item_history"]
ORG_TABLES = ["app_settings", "mapping_profiles", "import_runs", "uploads", "items", "snapshots", "item_history"]
SERIAL_TABLES = ["organizations", "users", "leads", "uploads", "items", "snapshots"]
ORG_SERIAL_TABLES = ["uploads", "items", "snapshots"]
DATA_TABLES = ["uploads", "items"]
# Columns renamed since older versions: source name -> current name.
COLUMN_RENAMES = {"uploads": {"expiry_count": "alert_count"}}


def organization_files(sqlite_path: Path) -> dict[int, Path]:
    """Each other organization's SQLite file: instance/orgs/N/pharmacy.db, or backup-orgN.db."""
    found: dict[int, Path] = {}
    for path in (sqlite_path.parent / "orgs").glob("*/pharmacy.db"):
        if path.parent.name.isdigit():
            found[int(path.parent.name)] = path
    for path in sqlite_path.parent.glob(f"{sqlite_path.stem}-org*{sqlite_path.suffix}"):
        match = re.fullmatch(rf"{re.escape(sqlite_path.stem)}-org(\d+)", path.stem)
        if match:
            found.setdefault(int(match.group(1)), path)
    return found


def copy_tables(source: sqlite3.Connection, target, tables: list[str]) -> dict[str, int]:
    copied: dict[str, int] = {}
    source_tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    for table in tables:
        if table not in source_tables:
            continue
        renames = COLUMN_RENAMES.get(table, {})
        source_columns = {row[1] for row in source.execute(f"PRAGMA table_info({table})")}
        target_columns = target.table_columns(table)
        pairs = sorted((column, renames.get(column, column)) for column in source_columns if renames.get(column, column) in target_columns)
        rows = [tuple(row[source] for source, _target in pairs) for row in source.execute(f"SELECT {', '.join(source for source, _ in pairs)} FROM {table}")]
        if rows:
            names = [target_name for _source, target_name in pairs]
            target.executemany(f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})", rows)
        copied[table] = len(rows)
    return copied


def reset_sequences(target, tables: list[str]) -> None:
    for table in tables:
        target.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)")


def migrate(sqlite_path: Path, database_url: str) -> dict[str, int]:
    """Copy the install; returns row counts ("items", ...; "org2.items", ... for other organizations)."""
    os.environ["DATABASE_URL"] = database_url
    import app  # creates the PostgreSQL schema on import

    app.DATABASE_URL = database_url
    app.init_db()
    source = sqlite3.connect(sqlite_path)
    source.row_factory = sqlite3.Row
    source_tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    with app.get_db(app.PLATFORM_ORG_ID) as target:
        for table in DATA_TABLES:
            if target.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]:
                raise SystemExit(f"Target table {table} already has rows; refusing to overwrite.")
        # Starting the app on an empty database creates a default admin, internal flags, and history
        # rebuilt from analysis folders on disk; the source copies replace all of them.
        for table in ("item_history", "snapshots", "users", "app_settings", "mapping_profiles", "import_runs"):
            target.execute(f"DELETE FROM {table}")
        if "organizations" in source_tables:  # older installs have none: keep the main organization
            target.execute("DELETE FROM organizations")
        copied = copy_tables(source, target, TABLES)
        reset_sequences(target, SERIAL_TABLES)

    for org_id, path in sorted(organization_files(sqlite_path).items()):
        org_source = sqlite3.connect(path)
        org_source.row_factory = sqlite3.Row
        with app.get_db(org_id) as target:
            app.init_org_schema(target)
            for table in DATA_TABLES:
                if target.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]:
                    raise SystemExit(f"Organization {org_id}: table {table} already has rows; refusing to overwrite.")
            for table in ("item_history", "snapshots", "app_settings", "mapping_profiles", "import_runs"):
                target.execute(f"DELETE FROM {table}")
            copied.update({f"org{org_id}.{table}": count for table, count in copy_tables(org_source, target, ORG_TABLES).items()})
            reset_sequences(target, ORG_SERIAL_TABLES)
        org_source.close()
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
