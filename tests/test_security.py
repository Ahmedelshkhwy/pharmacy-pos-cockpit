"""Security: CSRF, first password, secret key, cookies, login limits, redirects, error messages."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_web_flow import client  # noqa: E402,F401


def token_from(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def test_forms_without_the_csrf_token_are_rejected(client):
    import app as appmod

    appmod.app.config["CSRF_ENABLED"] = True
    with appmod.get_db() as db:
        before = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    # A forged "delete everything" / "create user" from another site has no token.
    assert client.post("/data-management", data={"mode": "all", "confirm": "DELETE ALL"}).status_code == 400
    assert client.post("/users", data={"action": "create", "username": "attacker", "password": "password123", "role": "owner"}).status_code == 400
    assert client.post("/users", data={"action": "create", "username": "attacker", "password": "password123", "role": "owner", "csrf_token": "guess"}).status_code == 400
    with appmod.get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == before
    # The real form works: the token comes from the page.
    token = token_from(client.get("/users").data.decode())
    response = client.post("/users", data={"action": "create", "username": "staff1", "password": "password123", "role": "viewer", "csrf_token": token})
    assert response.status_code == 302


def test_login_page_carries_a_token_and_login_needs_it(client):
    import app as appmod

    appmod.app.config["CSRF_ENABLED"] = True
    fresh = appmod.app.test_client()
    assert fresh.post("/login", data={"username": "admin", "password": "admin123"}).status_code == 400
    token = token_from(fresh.get("/login").data.decode())
    assert fresh.post("/login", data={"username": "admin", "password": "admin123", "csrf_token": token}).status_code == 302


def test_first_admin_password_is_random_when_not_configured(tmp_path, monkeypatch):
    import app as appmod

    data = tmp_path / "instance"
    monkeypatch.setattr(appmod, "DATA_DIR", data)
    monkeypatch.setattr(appmod, "UPLOAD_DIR", data / "uploads")
    monkeypatch.setattr(appmod, "DB_PATH", data / "pharmacy.db")
    monkeypatch.setattr(appmod, "DEFAULT_ADMIN_PASSWORD", "")
    appmod.init_db()
    password = (data / "initial_admin_password.txt").read_text().strip()
    appmod.app.config["ENFORCE_PASSWORD_CHANGE"] = True
    assert len(password) >= 12 and password != "admin123"
    fresh = appmod.app.test_client()
    assert "/login" in fresh.post("/login", data={"username": "admin", "password": "admin123"}).headers["Location"]
    assert fresh.post("/login", data={"username": "admin", "password": password}).headers["Location"].endswith("/change-password")


def test_secret_key_is_never_a_fixed_value(tmp_path, monkeypatch):
    import app as appmod

    monkeypatch.delenv("PHARMACY_SECRET_KEY", raising=False)
    monkeypatch.setattr(appmod, "DATA_DIR", tmp_path)
    first = appmod.load_secret_key()
    assert len(first) >= 40 and first != "change-this-secret-key"
    assert appmod.load_secret_key() == first  # kept, so restarts and other workers share it
    monkeypatch.setenv("PHARMACY_SECRET_KEY", "from-env")
    assert appmod.load_secret_key() == "from-env"


def test_session_cookie_settings(client):
    import app as appmod

    assert appmod.app.config["SESSION_COOKIE_HTTPONLY"] and appmod.app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert appmod.app.config["PERMANENT_SESSION_LIFETIME"].total_seconds() == 12 * 3600
    cookie = client.get_cookie("session")
    assert cookie is not None and cookie.http_only and cookie.expires is not None  # expires, not a forever session


def test_repeated_wrong_passwords_lock_the_account_for_a_while(client):
    import app as appmod

    attacker = appmod.app.test_client()
    for _ in range(appmod.LOGIN_MAX_FAILURES_PER_USER):
        attacker.post("/login", data={"username": "admin", "password": "wrong-guess"})
    # Even the right password is refused during the lock, so guessing cannot continue.
    response = attacker.post("/login", data={"username": "admin", "password": "admin123"})
    assert "/login" in response.headers["Location"]
    assert "محاولات" in attacker.get("/login").data.decode()
    with appmod.get_db() as db:
        db.execute("DELETE FROM login_failures")
    assert attacker.post("/login", data={"username": "admin", "password": "admin123"}).headers["Location"] == "/"


def test_redirects_stay_on_this_site(client):
    for evil in ["https://evil.example.com/", "//evil.example.com"]:
        response = client.post("/language/ar", headers={"Referer": evil})
        assert response.headers["Location"] == "/"
    response = client.post("/language/ar", headers={"Referer": "http://localhost/reports/dead?branch=x"})
    assert response.headers["Location"] == "/reports/dead?branch=x"


def test_errors_do_not_show_internals(client, tmp_path):
    bad = tmp_path / "broken.xlsx"
    bad.write_bytes(b"not really an excel file")
    response = client.post("/upload", data={"files": [(bad.open("rb"), bad.name)]}, content_type="multipart/form-data")
    page = client.get(response.headers["Location"]).data.decode()
    assert "broken.xlsx" in page
    assert "Traceback" not in page and "Error" not in page.split("broken.xlsx", 1)[1][:200]
    missing = client.get("/no-such-page")
    assert missing.status_code == 404 and "Werkzeug" not in missing.data.decode()


def test_owner_can_recover_access_with_a_server_variable(client, monkeypatch):
    import app as appmod

    with appmod.get_db() as db:
        db.execute("UPDATE users SET role = 'viewer' WHERE username = 'admin'")  # even a demoted account
    for _ in range(appmod.LOGIN_MAX_FAILURES_PER_USER):
        appmod.app.test_client().post("/login", data={"username": "admin", "password": "forgotten"})
    monkeypatch.setenv("PHARMACY_RESET_ADMIN_PASSWORD", "Recovered-2026")
    appmod.apply_password_reset()
    appmod.app.config["ENFORCE_PASSWORD_CHANGE"] = True
    fresh = appmod.app.test_client()
    response = fresh.post("/login", data={"username": "admin", "password": "Recovered-2026"})
    assert response.headers["Location"].endswith("/change-password")  # unlocked, must pick a new one
    with appmod.get_db() as db:
        assert db.execute("SELECT role FROM users WHERE username = 'admin'").fetchone()[0] == "owner"
        db.execute("UPDATE users SET password_hash = ? WHERE username = 'admin'", (appmod.generate_password_hash("Chosen-Later-1"),))
    appmod.apply_password_reset()  # the variable was left on the server: not applied a second time
    assert appmod.app.test_client().post("/login", data={"username": "admin", "password": "Chosen-Later-1"}).status_code == 302
    with appmod.get_db() as db:
        assert appmod.check_password_hash(db.execute("SELECT password_hash FROM users WHERE username = 'admin'").fetchone()[0], "Chosen-Later-1")
    monkeypatch.setenv("PHARMACY_RESET_ADMIN_PASSWORD", "short")
    appmod.apply_password_reset()  # too short: ignored
