"""PostgreSQL-only checks. Run with DATABASE_URL pointing at a throwaway database."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs DATABASE_URL (PostgreSQL)")

from db import translate_sql  # noqa: E402


def test_sql_translation():
    assert translate_sql("SELECT * FROM t WHERE a = ? AND tags LIKE '%x%'") == "SELECT * FROM t WHERE a = %s AND tags LIKE '%%x%%'"
    assert "SERIAL PRIMARY KEY" in translate_sql("id INTEGER PRIMARY KEY AUTOINCREMENT")
    assert translate_sql("price REAL NOT NULL") == "price DOUBLE PRECISION NOT NULL"


def test_migrate_sqlite_install_into_postgres(tmp_path, monkeypatch):
    import app as appmod
    from test_web_flow import submit_review
    from tools.migrate_sqlite_to_postgres import migrate

    url = os.environ["DATABASE_URL"]
    data = tmp_path / "instance"
    for name, value in {"DATA_DIR": data, "UPLOAD_DIR": data / "uploads", "POS_RAW_DIR": data / "pos_raw", "POS_ANALYSIS_DIR": data / "pos_analysis",
                        "IMPORTS_DIR": data / "imports", "MAPPING_PROFILES_PATH": data / "mapping_profiles.json", "DB_PATH": data / "pharmacy.db"}.items():
        monkeypatch.setattr(appmod, name, value)
    monkeypatch.setattr(appmod, "DATABASE_URL", None)  # build the source install on SQLite
    appmod.init_db()
    client = appmod.app.test_client()
    client.post("/login", data={"username": "admin", "password": "admin123"})
    path = ROOT / "sample_data" / "D_chain_multi_branch.xlsx"
    response = client.post("/upload", data={"files": [(path.open("rb"), path.name)]}, content_type="multipart/form-data")
    submit_review(client, response.headers["Location"])

    copied = migrate(data / "pharmacy.db", url)
    assert copied["items"] == 240 and copied["snapshots"] == 3 and copied["users"] == 1
    assert appmod.DATABASE_URL == url
    with appmod.get_db() as db:
        assert db.is_postgres
        assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 240
    # The migrated install keeps working: same login, and new rows get fresh ids.
    client = appmod.app.test_client()
    assert client.post("/login", data={"username": "admin", "password": "admin123"}).headers["Location"] == "/"
    response = client.post("/upload", data={"files": [(path.open("rb"), path.name)]}, content_type="multipart/form-data")
    assert "/imports/" in submit_review(client, response.headers["Location"]).headers["Location"]
