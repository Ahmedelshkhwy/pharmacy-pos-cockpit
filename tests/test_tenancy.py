"""Several organizations on one install: sign-up, data isolation, platform console, suspension."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "sample_data"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SAMPLES))

import generate_samples  # noqa: E402
from test_web_flow import submit_review, upload  # noqa: E402

PAGES = ["/", "/profit-loss", "/reports/expiry", "/reports/reorder", "/reports/loss", "/financial", "/trends",
         "/uploads", "/data-management", "/actions", "/abc", "/export/expiry", "/purchase-orders.xlsx",
         "/ask?q=" + "إيه أكتر الأصناف خسارة", "/settings", "/users"]


@pytest.fixture()
def appmod(tmp_path, monkeypatch):
    generate_samples.main()
    import app as module

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
        monkeypatch.setattr(module, name, value)
    module.init_db()
    module.app.config["TESTING"] = True
    return module


def signup(appmod, org_name, username, business_type="pharmacy", password="secret-pass-1", ip="10.0.0.1"):
    client = appmod.app.test_client()
    response = client.post(
        "/signup",
        data={"org_name": org_name, "business_type": business_type, "username": username,
              "password": password, "confirm_password": password},
        environ_base={"REMOTE_ADDR": ip},
    )
    return client, response


def body(client, url):
    response = client.get(url)
    assert response.status_code == 200, url
    return response.data.decode("utf-8", "ignore") if "xlsx" not in url else response.data.hex()


def test_signup_creates_an_organization_and_logs_the_owner_in(appmod):
    client, response = signup(appmod, "سوبر ماركت الخير", "khair", business_type="supermarket")
    assert response.status_code == 302 and response.headers["Location"].endswith("/upload")
    page = body(client, "/")
    assert "سوبر ماركت الخير" in page  # organization name in the menu
    settings_page = body(client, "/settings")
    assert 'value="supermarket" selected' in settings_page or "سوبر ماركت" in settings_page
    with appmod.platform_db() as db:
        org = db.execute("SELECT * FROM organizations WHERE name = ?", ("سوبر ماركت الخير",)).fetchone()
        owner = db.execute("SELECT * FROM users WHERE username = 'khair'").fetchone()
    assert org["business_type"] == "supermarket" and owner["org_id"] == org["id"] and owner["role"] == "owner"
    assert not owner["is_platform_admin"]
    # The new owner can log in again later.
    client.post("/logout")
    again = appmod.app.test_client()
    assert again.post("/login", data={"username": "khair", "password": "secret-pass-1"}).status_code == 302
    assert again.get("/upload").status_code == 200


def test_signup_validation(appmod):
    _, response = signup(appmod, "X", "short-org")
    assert response.status_code == 400
    _, response = signup(appmod, "Pharmacy One", "a b")  # bad username
    assert response.status_code == 400
    _, response = signup(appmod, "Pharmacy One", "admin")  # taken (the platform owner)
    assert response.status_code == 400
    client = appmod.app.test_client()
    mismatch = client.post("/signup", data={"org_name": "Pharmacy One", "business_type": "pharmacy", "username": "one",
                                            "password": "secret-pass-1", "confirm_password": "other-pass-1"})
    assert mismatch.status_code == 400
    bot = client.post("/signup", data={"org_name": "Bot Org", "business_type": "pharmacy", "username": "bot",
                                       "password": "secret-pass-1", "confirm_password": "secret-pass-1", "website": "x"})
    assert bot.status_code == 302
    with appmod.platform_db() as db:
        assert db.execute("SELECT COUNT(*) FROM organizations").fetchone()[0] == 1  # only the main one
        assert not db.execute("SELECT 1 FROM users WHERE username IN ('one', 'bot')").fetchone()


def test_signup_is_rate_limited_per_address(appmod):
    for index in range(appmod.SIGNUPS_PER_IP_PER_DAY):
        assert signup(appmod, f"Shop {index}", f"shop{index}", ip="10.9.9.9")[1].status_code == 302
    assert signup(appmod, "One Too Many", "toomany", ip="10.9.9.9")[1].status_code == 400
    assert signup(appmod, "Other Place", "otherplace", ip="10.9.9.8")[1].status_code == 302


def test_organizations_never_see_each_other(appmod):
    alpha, _ = signup(appmod, "صيدلية ألفا", "alpha_owner")
    beta, _ = signup(appmod, "Beta Market", "beta_owner", business_type="supermarket")
    files = [SAMPLES / "B_sales_invoices.csv", SAMPLES / "B_stock_batches.csv"]
    assert "/imports/" in submit_review(alpha, upload(alpha, files, branch="فرع-ألفا-السري")).headers["Location"]
    assert "/imports/" in submit_review(beta, upload(beta, [SAMPLES / "D_chain_multi_branch.xlsx"])).headers["Location"]

    alpha_text = " ".join(body(alpha, url) for url in PAGES)
    beta_text = " ".join(body(beta, url) for url in PAGES)
    assert "فرع-ألفا-السري" in alpha_text and "فرع-ألفا-السري" not in beta_text
    beta_org = org_id(appmod, "beta_owner")
    with appmod.get_db(beta_org) as db:
        beta_branches = [row[0] for row in db.execute("SELECT DISTINCT branch FROM items")]
    assert beta_branches and all(branch not in alpha_text for branch in beta_branches)
    assert "beta_owner" not in alpha_text and "alpha_owner" not in beta_text  # users pages

    # A branch or import of the other organization is simply not there.
    assert "فرع-ألفا-السري" not in beta.get("/branches/فرع-ألفا-السري").data.decode()
    # "Delete everything" in one organization only wipes that organization (rows and files).
    alpha_org = org_id(appmod, "alpha_owner")
    alpha_files = appmod.DATA_DIR / "orgs" / str(alpha_org)
    files_before = sorted(alpha_files.rglob("*"))
    beta.post("/data-management", data={"mode": "all", "confirm": "DELETE ALL", "delete_files": "1"})
    with appmod.get_db(beta_org) as db:
        assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    with appmod.get_db(alpha_org) as db:
        assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] > 0
    assert files_before and sorted(alpha_files.rglob("*")) == files_before
    assert "فرع-ألفا-السري" in body(alpha, "/profit-loss")

    # The platform's own organization (existing data) stays separate too.
    with appmod.get_db(appmod.PLATFORM_ORG_ID) as db:
        assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    assert beta_org != appmod.PLATFORM_ORG_ID


def test_settings_are_per_organization(appmod):
    alpha, _ = signup(appmod, "Alpha", "alpha")
    beta, _ = signup(appmod, "Beta", "beta", business_type="spare_parts")
    alpha_settings = appmod.load_settings_json
    with appmod.get_db(org_id(appmod, "alpha")) as db:
        a = alpha_settings(db.execute("SELECT value FROM app_settings WHERE key = 'analysis'").fetchone()["value"])
    with appmod.get_db(org_id(appmod, "beta")) as db:
        b = alpha_settings(db.execute("SELECT value FROM app_settings WHERE key = 'analysis'").fetchone()["value"])
    assert a.business_type == "pharmacy" and b.business_type == "spare_parts"


def test_owner_cannot_edit_users_of_another_organization(appmod):
    alpha, _ = signup(appmod, "Alpha", "alpha")
    signup(appmod, "Beta", "beta")
    with appmod.platform_db() as db:
        beta_id = db.execute("SELECT id FROM users WHERE username = 'beta'").fetchone()["id"]
    response = alpha.post("/users", data={"action": "delete", "user_id": beta_id, "role": "viewer"})
    assert response.status_code == 404
    with appmod.platform_db() as db:
        assert db.execute("SELECT 1 FROM users WHERE id = ?", (beta_id,)).fetchone()
    # Users an owner adds belong to the owner's organization.
    alpha.post("/users", data={"action": "create", "username": "alpha_viewer", "password": "viewer-pass-1", "role": "viewer"})
    assert org_id(appmod, "alpha_viewer") == org_id(appmod, "alpha")


def test_platform_console_is_for_the_platform_owner_only(appmod):
    alpha, _ = signup(appmod, "Alpha Pharmacy", "alpha")
    assert alpha.get("/platform").status_code == 403
    assert alpha.get("/leads").status_code == 403
    assert "/platform" not in body(alpha, "/")

    admin = appmod.app.test_client()
    admin.post("/login", data={"username": "admin", "password": "admin123"})
    page = body(admin, "/platform")
    assert "Alpha Pharmacy" in page and "alpha" in page
    assert admin.get("/leads").status_code == 200


def test_suspended_organization_cannot_log_in(appmod):
    alpha, _ = signup(appmod, "Alpha", "alpha")
    admin = appmod.app.test_client()
    admin.post("/login", data={"username": "admin", "password": "admin123"})
    alpha_org = org_id(appmod, "alpha")
    admin.post("/platform", data={"org_id": alpha_org, "status": "suspended"})
    assert alpha.get("/profit-loss").headers["Location"].startswith("/login")  # logged out right away
    fresh = appmod.app.test_client()
    fresh.post("/login", data={"username": "alpha", "password": "secret-pass-1"})
    assert fresh.get("/profit-loss").status_code == 302
    # The main organization cannot be suspended.
    admin.post("/platform", data={"org_id": appmod.PLATFORM_ORG_ID, "status": "suspended"})
    assert admin.get("/platform").status_code == 200

    admin.post("/platform", data={"org_id": alpha_org, "status": "active"})
    fresh.post("/login", data={"username": "alpha", "password": "secret-pass-1"})
    assert fresh.get("/profit-loss").status_code == 200


def test_existing_spaces_are_created_on_restart(appmod):
    signup(appmod, "Alpha", "alpha")
    alpha_org = org_id(appmod, "alpha")
    appmod.init_db()  # app restart: every organization's tables are checked again
    with appmod.get_db(alpha_org) as db:
        assert db.execute("SELECT COUNT(*) FROM uploads").fetchone()[0] == 0


def org_id(appmod, username):
    with appmod.platform_db() as db:
        return int(db.execute("SELECT org_id FROM users WHERE username = ?", (username,)).fetchone()["org_id"])


def test_backup_keeps_every_organization(appmod, tmp_path):
    import sqlite3

    from tools.backup_database import backup, org_backup_path

    alpha, _ = signup(appmod, "Alpha", "alpha")
    submit_review(alpha, upload(alpha, [SAMPLES / "D_chain_multi_branch.xlsx"]))
    alpha_org = org_id(appmod, "alpha")
    target = tmp_path / "backup.db"
    counts = backup(target)
    assert counts["users"] == 2 and counts["organizations"] == 2 and counts.get("items", 0) == 0
    assert counts[f"org{alpha_org}.items"] == 240
    with sqlite3.connect(org_backup_path(target, alpha_org)) as copy:
        assert copy.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 240
