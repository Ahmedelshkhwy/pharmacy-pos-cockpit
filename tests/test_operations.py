"""Production behaviour: health check, indexes, cleanup of abandoned uploads, backups, config."""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "sample_data"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_web_flow import client, submit_review, upload  # noqa: E402,F401


def test_health_check_needs_no_login(client):
    import app as appmod

    anonymous = appmod.app.test_client()
    response = anonymous.get("/healthz")
    assert response.status_code == 200 and response.data == b"ok"


def test_dev_only_settings_are_off_by_default():
    import app as appmod

    assert not appmod.DEV_MODE
    assert appmod.app.config.get("SEND_FILE_MAX_AGE_DEFAULT") != 0
    assert not appmod.app.config.get("TEMPLATES_AUTO_RELOAD")


def test_lookup_indexes_exist(client):
    import app as appmod

    with appmod.get_db() as db:
        if db.is_postgres:
            names = {row[0] for row in db.execute("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")}
        else:
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {"idx_items_branch", "idx_items_sku", "idx_items_upload", "idx_uploads_branch"} <= names


def test_abandoned_uploads_are_removed_but_finished_ones_kept(client):
    import app as appmod

    done = upload(client, [SAMPLES / "A_arabic_pharmacy_report.xlsx"], branch="Maadi")
    submit_review(client, done)
    abandoned = upload(client, [SAMPLES / "A_arabic_pharmacy_report.xlsx"], branch="Maadi")
    done_dir = appmod.IMPORTS_DIR / done.rsplit("/", 1)[-1]
    abandoned_dir = appmod.IMPORTS_DIR / abandoned.rsplit("/", 1)[-1]
    old = time.time() - (appmod.ABANDONED_IMPORT_HOURS + 1) * 3600
    for folder in (done_dir, abandoned_dir):
        os.utime(folder, (old, old))
    assert appmod.purge_abandoned_imports() == 1
    assert done_dir.exists() and not abandoned_dir.exists()


def test_rejected_upload_leaves_no_analysis_behind(client):
    import app as appmod

    client.post("/users", data={"action": "create", "username": "zayed_mgr", "password": "password123", "role": "manager", "branches": ["Zayed"]})
    manager = appmod.app.test_client()
    manager.post("/login", data={"username": "zayed_mgr", "password": "password123"})
    before = set(appmod.POS_ANALYSIS_DIR.glob("*")) if appmod.POS_ANALYSIS_DIR.exists() else set()
    review = upload(manager, [SAMPLES / "D_chain_multi_branch.xlsx"])
    assert "/upload/review/" in submit_review(manager, review).headers["Location"]  # rejected: other branches
    after = set(appmod.POS_ANALYSIS_DIR.glob("*")) if appmod.POS_ANALYSIS_DIR.exists() else set()
    assert after == before


def test_reanalysis_only_offers_imports_whose_files_still_exist(client):
    import shutil

    import app as appmod

    review = upload(client, [SAMPLES / "A_arabic_pharmacy_report.xlsx"], branch="Maadi")
    submit_review(client, review)
    assert len(appmod.latest_import_tokens()) == 1
    shutil.rmtree(appmod.IMPORTS_DIR / review.rsplit("/", 1)[-1])  # what a redeploy without a disk does
    assert appmod.latest_import_tokens() == []
    assert client.get("/settings").status_code == 200


def test_backup_copies_every_table_and_can_be_opened(client, tmp_path):
    import app as appmod
    from tools.backup_database import backup

    submit_review(client, upload(client, [SAMPLES / "D_chain_multi_branch.xlsx"]))
    target = tmp_path / "backup.db"
    counts = backup(target)
    assert counts["items"] == 240 and counts["users"] == 1 and counts["snapshots"] == 3
    with sqlite3.connect(target) as copy:
        assert copy.execute("SELECT COUNT(*) FROM items WHERE branch = 'Zayed'").fetchone()[0] == 80
    with appmod.get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 240  # the source is untouched
