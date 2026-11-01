"""Demo data: a fictional 4-branch pharmacy chain with three monthly exports, for sales demos.

What it does:
  1. Writes the demo POS exports to sample_data/demo/ (Arabic headers, title rows, a branch
     column, like a real chain export). These are the "fake data files" you can show a prospect
     or upload yourself from the upload page.
  2. Imports them through the same smart pipeline the upload page uses, oldest month first,
     so the dashboard, profit & loss, action center (transfers between branches) and the
     trends page all have data.
  3. Creates a read-only "demo" user that sees the demo branches only.

It only touches the demo branches: real branches in the same database are left alone.
Still, run it on a separate demo deployment, not on a customer's database.

Run:
    python seed_demo.py                 # generate files + import + demo user
    python seed_demo.py --files-only    # only write the files to sample_data/demo/
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from openpyxl import Workbook

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "sample_data"))

import generate_samples  # noqa: E402
from generate_samples import PERIOD_DAYS, build_branch  # noqa: E402

DEMO_DIR = ROOT / "sample_data" / "demo"
CHAIN_NAME = "صيدليات الشفاء (بيانات تجريبية)"
DEMO_BRANCHES = [
    ("فرع المعادي", 51),
    ("فرع مدينة نصر", 52),
    ("فرع الشيخ زايد", 53),
    ("فرع مصر الجديدة", 54),
]
DEMO_USERNAME = os.environ.get("PHARMACY_DEMO_USERNAME", "demo")
DEMO_PASSWORD = os.environ.get("PHARMACY_DEMO_PASSWORD", "demo12345")


def month_ends(today: date | None = None, count: int = 3) -> list[date]:
    """The last day of each of the `count` months before today, oldest first."""
    today = today or date.today()
    ends = []
    cursor = today.replace(day=1) - timedelta(days=1)
    for _ in range(count):
        ends.append(cursor)
        cursor = cursor.replace(day=1) - timedelta(days=1)
    return list(reversed(ends))


def write_month(path: Path, period_end: date, month_index: int) -> None:
    """One chain export covering the 90 days up to period_end, all branches in one sheet."""
    period_start = period_end - timedelta(days=PERIOD_DAYS - 1)
    wb = Workbook()
    ws = wb.active
    ws.title = "حركة الأصناف"
    ws.append([CHAIN_NAME])
    ws.append([f"تقرير حركة الأصناف من {period_start:%d/%m/%Y} إلى {period_end:%d/%m/%Y}"])
    ws.append([])
    ws.append(["كود الصنف", "اسم الصنف", "الفرع", "المجموعة", "الكمية المباعة", "الرصيد الحالي", "سعر الشراء", "سعر البيع", "تاريخ الصلاحية", "المورد"])
    for branch, seed in DEMO_BRANCHES:
        # Same item codes in every branch (so transfers can be matched), different stock and demand.
        for item in build_branch(seed + 100 * month_index, "D"):
            expiry = min((exp for exp, qty in item.batches if qty > 0), default=None)
            ws.append([item.code, item.name, branch, item.category, item.sold_qty, item.stock, item.cost, item.price, expiry, item.supplier])
    wb.save(path)


def write_demo_files(today: date | None = None, out_dir: Path = DEMO_DIR) -> list[Path]:
    # The generator dates expiries from its own TODAY: move it to now so the demo never looks stale.
    generate_samples.TODAY = today or date.today()
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for month_index, period_end in enumerate(month_ends(today)):
        path = out_dir / f"demo_chain_{period_end:%Y-%m}.xlsx"
        write_month(path, period_end, month_index)
        paths.append(path)
    return paths


def import_demo_files(paths: list[Path]) -> int:
    import app as appmod
    from smart_import import detect_file
    from smart_pipeline import run_smart_import

    appmod.init_db()
    for branch, _seed in DEMO_BRANCHES:
        appmod.delete_branch_data(branch)
    settings = appmod.load_analysis_settings()
    imported = 0
    for path in paths:  # oldest first, so the trends page shows the right order
        tables = [(path, spec) for spec in detect_file(path) if spec.usable]
        period_end = date.fromisoformat(path.stem.rsplit("_", 1)[1] + "-01")
        result = run_smart_import(tables, appmod.POS_ANALYSIS_DIR, settings=settings, today=_next_month(period_end))
        for branch_result in result.branches:
            imported += appmod.import_pos_analysis_to_db(branch_result.output_dir, import_token="demo")
    return imported


def _next_month(first_day: date) -> date:
    return (first_day.replace(day=28) + timedelta(days=4)).replace(day=1)


def ensure_demo_user() -> None:
    from werkzeug.security import generate_password_hash

    import app as appmod

    branches = json.dumps([branch for branch, _seed in DEMO_BRANCHES], ensure_ascii=False)
    with appmod.get_db() as db:
        existing = db.execute("SELECT id FROM users WHERE username = ?", (DEMO_USERNAME,)).fetchone()
        if existing:
            db.execute(
                "UPDATE users SET role = 'viewer', branches = ?, password_hash = ?, must_change_password = 0 WHERE id = ?",
                (branches, generate_password_hash(DEMO_PASSWORD), existing["id"]),
            )
        else:
            db.execute(
                "INSERT INTO users (username, password_hash, created_at, role, branches, must_change_password) VALUES (?, ?, ?, 'viewer', ?, 0)",
                (DEMO_USERNAME, generate_password_hash(DEMO_PASSWORD), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), branches),
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--files-only", action="store_true", help="only write the demo files, do not import them")
    args = parser.parse_args()
    paths = write_demo_files()
    print("Demo files:", *[f"  {path.relative_to(ROOT)}" for path in paths], sep="\n")
    if args.files_only:
        return
    rows = import_demo_files(paths)
    ensure_demo_user()
    print(f"Imported {rows} item rows for {len(DEMO_BRANCHES)} demo branches.")
    print(f"Demo login: {DEMO_USERNAME} / {DEMO_PASSWORD} (read-only, demo branches only)")


if __name__ == "__main__":
    main()
