"""Public site (landing, privacy, free-analysis leads), forced password change, demo data and one-page report."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def appmod(tmp_path, monkeypatch):
    import app as appmod

    data = tmp_path / "instance"
    for name, value in {
        "DATA_DIR": data,
        "UPLOAD_DIR": data / "uploads",
        "POS_RAW_DIR": data / "pos_raw",
        "POS_ANALYSIS_DIR": data / "pos_analysis",
        "IMPORTS_DIR": data / "imports",
        "MAPPING_PROFILES_PATH": data / "mapping_profiles.json",
        "DB_PATH": data / "pharmacy.db",
    }.items():
        monkeypatch.setattr(appmod, name, value)
    appmod.init_db()
    appmod.app.config["TESTING"] = True
    return appmod


def login(client, username="admin", password="admin123"):
    return client.post("/login", data={"username": username, "password": password})


def test_visitors_see_landing_and_users_see_dashboard(appmod):
    client = appmod.app.test_client()
    page = client.get("/").data.decode()
    assert "اطلب تحليلك المجاني" in page and 'action="/free-analysis"' in page
    assert "/demo" not in page  # demo button only when demo mode is on
    assert client.get("/privacy").status_code == 200
    login(client)
    assert "اطلب تحليلك المجاني" not in client.get("/").data.decode()


def test_free_analysis_lead_is_saved_and_listed(appmod, monkeypatch):
    monkeypatch.setattr(appmod, "WHATSAPP_NUMBER", "201001234567")
    client = appmod.app.test_client()
    response = client.post("/free-analysis", data={"name": "د. سارة", "phone": "01001234567", "pharmacy": "صيدلية النور", "branches": "4", "city": "القاهرة"})
    assert response.headers["Location"].endswith("/free-analysis/thanks")
    thanks = client.get("/free-analysis/thanks").data.decode()
    assert "wa.me/201001234567" in thanks
    assert client.post("/free-analysis", data={"name": "x", "phone": "abc"}).headers["Location"].endswith("#free-analysis")
    client.post("/free-analysis", data={"name": "bot", "phone": "01001234567", "website": "spam"})  # honeypot
    with appmod.get_db() as db:
        leads = [dict(row) for row in db.execute("SELECT * FROM leads")]
    assert [lead["name"] for lead in leads] == ["د. سارة"] and leads[0]["branches"] == 4

    login(client)
    page = client.get("/leads").data.decode()
    assert "صيدلية النور" in page and "wa.me/201001234567" in page
    client.post("/leads", data={"lead_id": leads[0]["id"], "status": "contacted"})
    with appmod.get_db() as db:
        assert db.execute("SELECT status FROM leads").fetchone()[0] == "contacted"


def test_default_password_must_be_changed(appmod):
    appmod.app.config["ENFORCE_PASSWORD_CHANGE"] = True
    client = appmod.app.test_client()
    assert login(client).headers["Location"].endswith("/change-password")
    assert client.get("/actions").headers["Location"].endswith("/change-password")
    rejected = client.post("/change-password", data={"current_password": "admin123", "new_password": "admin123", "confirm_password": "admin123"})
    assert rejected.headers["Location"].endswith("/change-password")
    client.post("/change-password", data={"current_password": "admin123", "new_password": "N3w-secret!", "confirm_password": "N3w-secret!"})
    assert client.get("/actions").status_code == 200

    # A user created by the owner also picks their own password on first login.
    client.post("/users", data={"action": "create", "username": "maadi_mgr", "password": "temp12345", "role": "viewer"})
    other = appmod.app.test_client()
    assert login(other, "maadi_mgr", "temp12345").headers["Location"].endswith("/change-password")


def test_login_ignores_external_next(appmod):
    client = appmod.app.test_client()
    response = client.post("/login?next=//evil.example.com", data={"username": "admin", "password": "admin123"})
    assert response.headers["Location"] == "/"


def test_demo_data_and_one_click_demo(appmod, monkeypatch, tmp_path):
    import seed_demo

    assert appmod.app.test_client().get("/demo").status_code == 404  # off by default
    paths = seed_demo.write_demo_files(out_dir=tmp_path / "demo")
    assert len(paths) == 3
    assert seed_demo.import_demo_files(paths) > 0
    seed_demo.ensure_demo_user()
    with appmod.get_db() as db:
        branches = {row[0] for row in db.execute("SELECT DISTINCT branch FROM items")}
        snapshots = db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    assert branches == {branch for branch, _seed in seed_demo.DEMO_BRANCHES}
    assert snapshots == 3 * len(seed_demo.DEMO_BRANCHES)  # three months per branch for the trends page

    monkeypatch.setattr(appmod, "DEMO_MODE", True)
    client = appmod.app.test_client()
    assert "/demo" in client.get("/").data.decode()
    assert client.get("/demo").headers["Location"] == "/"
    dashboard = client.get("/").data.decode()
    assert "نسخة تجريبية" in dashboard
    assert client.get("/upload").status_code == 403  # the demo is read-only
    report = client.get("/free-report").data.decode()
    assert "فلوس بتضيع أو متجمدة" in report and "أهم 10 أصناف" in report
    actions = client.get("/actions").data.decode()
    assert "اتعمل بمنصة الصيدليات" in actions  # shared summaries carry the signature


def test_admin_password_reset_from_env(appmod, monkeypatch):
    with appmod.get_db() as db:
        db.execute("UPDATE users SET password_hash = 'x', role = 'viewer'")
    monkeypatch.setattr(appmod, "RESET_ADMIN_PASSWORD", "Rescue-2026")
    appmod.init_db()
    client = appmod.app.test_client()
    assert login(client, "admin", "Rescue-2026").headers["Location"] == "/"
    with appmod.get_db() as db:
        row = db.execute("SELECT role, must_change_password FROM users WHERE username = 'admin'").fetchone()
    assert (row[0], row[1]) == ("owner", 1)
