"""End-to-end web flow: upload -> review -> analyze -> pages, on an isolated data folder."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "sample_data"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SAMPLES))

import generate_samples  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    generate_samples.main()
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
    test_client = appmod.app.test_client()
    test_client.post("/login", data={"username": "admin", "password": "admin123"})
    return test_client


def submit_review(client, review_url: str, overrides: dict[str, str] | None = None):
    html = client.get(review_url).data.decode()
    form = {name: "1" for name in re.findall(r'name="(use-[^"]+)" value="1" checked', html)}
    for name, body in re.findall(r'<select name="(map-[^"]+)">(.*?)</select>', html, flags=re.S):
        selected = re.search(r'<option value="([^"]*)" selected', body)
        form[name] = selected.group(1) if selected else ""
    branch = re.search(r'name="branch"[^>]*value="([^"]*)"', html)
    form["branch"] = branch.group(1) if branch else ""
    form.update(overrides or {})
    return client.post(review_url, data=form)


def upload(client, paths, branch=""):
    data = {"branch": branch, "files": [(path.open("rb"), path.name) for path in paths]}
    response = client.post("/upload", data=data, content_type="multipart/form-data")
    assert response.status_code == 302 and "/upload/review/" in response.headers["Location"]
    return response.headers["Location"]


def test_upload_review_analyze_and_browse(client):
    review = upload(client, [SAMPLES / "B_sales_invoices.csv", SAMPLES / "B_stock_batches.csv"], branch="مصر الجديدة")
    assert "Quantity sold" in client.get(review).data.decode()
    done = submit_review(client, review)
    assert "/imports/" in done.headers["Location"]
    result = client.get(done.headers["Location"]).data.decode()
    assert "مصر الجديدة" in result

    for url in ["/", "/profit-loss", "/reports/expiry", "/reports/reorder", "/reports/loss", "/financial",
                "/weekly-comparison", "/uploads", "/data-management", "/export/expiry"]:
        assert client.get(url).status_code == 200, url
    page = client.get("/profit-loss?branch=مصر الجديدة").data.decode()
    assert "Bebelac" in page  # the item sold below cost is named


def test_user_can_fix_a_misread_column_and_layout_is_remembered(client):
    path = SAMPLES / "D_chain_multi_branch.xlsx"
    review = upload(client, [path])
    # Pretend the store column was wrong: ignore it, everything goes to one branch.
    html = client.get(review).data.decode()
    store_select = re.search(r'<td><strong>Store</strong></td>\s*<td>\s*<select name="(map-[^"]+)"', html).group(1)
    done = submit_review(client, review, {store_select: "_ignore", "branch": "One Branch"})
    assert "/imports/" in done.headers["Location"]
    assert client.get("/profit-loss?branch=One Branch").status_code == 200

    second = client.get(upload(client, [path])).data.decode()
    assert "saved layout" in second or "شكل ملف محفوظ" in second


def test_period_is_asked_when_files_do_not_say(client, tmp_path):
    path = tmp_path / "summary.csv"
    path.write_text("Item,Sold,Stock,Cost,Price\nPanadol,10,5,30,40\n", encoding="utf-8")
    review = upload(client, [path], branch="Test")
    assert "required" in client.get(review).data.decode()
    assert "/upload/review/" in submit_review(client, review).headers["Location"]
    assert "/imports/" in submit_review(client, review, {"period_days": "30"}).headers["Location"]


def test_rejects_unsupported_file(client, tmp_path):
    bad = tmp_path / "notes.pdf"
    bad.write_bytes(b"%PDF")
    response = client.post("/upload", data={"files": [(bad.open("rb"), bad.name)]}, content_type="multipart/form-data")
    assert response.headers["Location"].endswith("/upload")
