from __future__ import annotations

import csv
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
from datetime import date, datetime
from functools import wraps
from pathlib import Path
from typing import Any

from flask import Flask, Response, flash, redirect, render_template, request, send_file, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from insights import analyze_profit_loss
from pos_pipeline import normalize_branch_name
from smart_import import (
    FIELD_LABELS,
    SUPPORTED_EXTENSIONS,
    TableSpec,
    apply_user_mapping,
    detect_file,
    field_choices,
    load_profiles,
    save_profile,
)
from smart_pipeline import branch_slug, clean_branch_label, run_smart_import


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "instance"
UPLOAD_DIR = DATA_DIR / "uploads"
POS_RAW_DIR = DATA_DIR / "pos_raw"
POS_ANALYSIS_DIR = DATA_DIR / "pos_analysis"
IMPORTS_DIR = DATA_DIR / "imports"
MAPPING_PROFILES_PATH = DATA_DIR / "mapping_profiles.json"
DB_PATH = DATA_DIR / "pharmacy.db"
BRANCH_TEMPLATE_PATH = BASE_DIR / "pharmacy_branch_upload_template.xlsx"

MAX_UPLOAD_FILES = 10
DEFAULT_ADMIN_USERNAME = os.environ.get("PHARMACY_ADMIN_USERNAME", "admin")
DEFAULT_ADMIN_PASSWORD = os.environ.get("PHARMACY_ADMIN_PASSWORD", "admin123")

ITEM_EXTRA_COLUMNS = {
    "category": "TEXT NOT NULL DEFAULT 'UNCATEGORIZED'",
    "cost_price": "REAL NOT NULL DEFAULT 0",
    "cost_value": "REAL NOT NULL DEFAULT 0",
    "avg_daily_sales": "REAL NOT NULL DEFAULT 0",
    "days_cover_forecast": "REAL",
    "stockout_probability_7d": "REAL NOT NULL DEFAULT 0",
    "stockout_probability_14d": "REAL NOT NULL DEFAULT 0",
    "stockout_probability_30d": "REAL NOT NULL DEFAULT 0",
    "reorder_point": "REAL NOT NULL DEFAULT 0",
    "recommended_reorder_qty": "REAL NOT NULL DEFAULT 0",
    "risk_score": "INTEGER NOT NULL DEFAULT 1",
    "decision_label": "TEXT NOT NULL DEFAULT 'Healthy'",
    "lost_sales_value": "REAL NOT NULL DEFAULT 0",
    "frozen_capital_value": "REAL NOT NULL DEFAULT 0",
    "unit_profit": "REAL NOT NULL DEFAULT 0",
    "gross_margin_percent": "REAL NOT NULL DEFAULT 0",
    "break_even_price": "REAL NOT NULL DEFAULT 0",
    "max_safe_discount_percent": "REAL NOT NULL DEFAULT 0",
    "suggested_offer_price": "REAL NOT NULL DEFAULT 0",
    "offer_discount_percent": "REAL NOT NULL DEFAULT 0",
    "offer_profit_per_unit": "REAL NOT NULL DEFAULT 0",
    "offer_profit_if_sold": "REAL NOT NULL DEFAULT 0",
    "signal_color": "TEXT NOT NULL DEFAULT 'green'",
    "barcode": "TEXT NOT NULL DEFAULT ''",
    "supplier": "TEXT NOT NULL DEFAULT ''",
    "net_sales": "REAL NOT NULL DEFAULT 0",
    "returns_qty": "REAL NOT NULL DEFAULT 0",
    "cost_known": "INTEGER NOT NULL DEFAULT 0",
    "gross_profit": "REAL NOT NULL DEFAULT 0",
    "lost_profit_value": "REAL NOT NULL DEFAULT 0",
    "excess_qty": "REAL NOT NULL DEFAULT 0",
    "excess_value": "REAL NOT NULL DEFAULT 0",
    "negative_margin_loss": "REAL NOT NULL DEFAULT 0",
    "expired_qty": "REAL NOT NULL DEFAULT 0",
    "expiry_risk_qty": "REAL NOT NULL DEFAULT 0",
    "expiry_risk_value": "REAL NOT NULL DEFAULT 0",
}

UPLOAD_EXTRA_COLUMNS = {
    "analysis_period_days": "INTEGER NOT NULL DEFAULT 90",
    "coverage": "TEXT NOT NULL DEFAULT ''",
    "quality_score": "INTEGER",
    "import_token": "TEXT NOT NULL DEFAULT ''",
}


app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("PHARMACY_SECRET_KEY", "change-this-secret-key")
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
app.jinja_env.auto_reload = True

WEEKLY_COMPARISON_METRICS = [
    ("sales", "Sales", "total_net_sales"),
    ("stock", "Stock value", "total_stock_value"),
    ("dead", "Dead stock", "dead_stock"),
    ("risk", "High risk", "high_risk_count"),
    ("frozen", "Frozen capital", "frozen_capital_value"),
    ("lost", "Lost sales", "lost_sales_value"),
]

TRANSLATIONS = {
    "en": {
        "app_name": "Predictive POS Cockpit",
        "app_description": "A decision cockpit for pharmacy stock, sales risk, and branch performance.",
        "language_toggle": "عربي",
        "dashboard": "Dashboard",
        "reorder_now": "Reorder Now",
        "dead_stock": "Dead Stock",
        "overstock_risk": "Overstock Risk",
        "fast_moving": "Fast Moving",
        "slow_moving": "Slow Moving",
        "financial_panel": "Financial Panel",
        "weekly_comparison": "Weekly Comparison",
        "branch_upload": "Branch Upload",
        "smart_upload": "Upload files",
        "profit_loss": "Profit & Loss Drivers",
        "expiry_risk": "Expiry Risk",
        "selling_below_cost": "Selling Below Cost",
        "imports": "Imports",
        "data_management": "Data Management",
        "password": "Password",
        "logout": "Logout",
        "decision_queue": "Decision Queue",
        "analysis": "Analysis",
        "operations": "Operations",
        "admin": "Admin",
        "view_all": "View all",
        "history": "History",
        "command_center": "General command center",
        "branch_dashboard": "branch dashboard",
        "priority_actions": "Priority actions",
        "highest_risk": "Highest risk score",
        "urgent_reorder": "Urgent reorder candidates",
        "highest_frozen": "Highest frozen capital",
        "fast_winners": "Fast moving winners",
        "latest_import": "Latest import",
        "login": "Login",
        "username": "Username",
        "current_password": "Current password",
        "new_password": "New password",
        "confirm_new_password": "Confirm new password",
        "save_password": "Save password",
        "account": "Account",
        "change_password": "Change password",
        "branch_filter": "Branch filter",
        "all_branches": "All branches",
        "reset": "Reset",
        "upload_excel": "Upload Excel",
        "back_to_dashboard": "Back to dashboard",
        "back_to_reports": "Back to reports",
        "export_csv": "Export CSV",
        "select_branch": "Select branch",
        "risk": "Risk",
        "branch": "Branch",
        "item": "Item",
        "ref": "Ref",
        "sales": "Sales",
        "stock": "Stock",
        "decision": "Decision",
        "stock_value": "Stock value",
        "lost_sales": "Lost sales",
        "frozen_capital": "Frozen capital",
        "high_risk": "High risk",
        "current_week": "Current week",
        "previous_week": "Previous week",
        "uploads": "Uploads",
        "no_data": "No data.",
        "no_rows_report": "No rows in this report.",
    },
    "ar": {
        "app_name": "Predictive POS Cockpit",
        "app_description": "كابينة قرارات لمخزون الصيدليات، مخاطر المبيعات، وأداء الفروع.",
        "language_toggle": "English",
        "dashboard": "لوحة المتابعة",
        "reorder_now": "اشتري أو حوّل دلوقتي",
        "dead_stock": "مخزون راكد",
        "overstock_risk": "زيادة مخزون",
        "fast_moving": "أصناف سريعة",
        "slow_moving": "أصناف بطيئة",
        "financial_panel": "الماليات",
        "weekly_comparison": "مقارنة أسبوعية",
        "branch_upload": "رفع ملفات فرع",
        "smart_upload": "ارفع ملفات",
        "profit_loss": "سر المكسب والخسارة",
        "expiry_risk": "خطر الصلاحية",
        "selling_below_cost": "بيع بأقل من التكلفة",
        "imports": "سجل الرفع",
        "data_management": "إدارة البيانات",
        "password": "كلمة المرور",
        "logout": "خروج",
        "decision_queue": "قرارات مهمة",
        "analysis": "تحليلات",
        "operations": "تشغيل",
        "admin": "إدارة",
        "view_all": "شوف الكل",
        "history": "السجل",
        "command_center": "مركز المتابعة العام",
        "branch_dashboard": "لوحة الفرع",
        "priority_actions": "قرارات محتاجة حركة",
        "highest_risk": "أعلى درجات خطورة",
        "urgent_reorder": "أصناف محتاجة شراء أو تحويل",
        "highest_frozen": "أكبر فلوس متجمّدة",
        "fast_winners": "أصناف سريعة ومهمة",
        "latest_import": "آخر رفع",
        "login": "تسجيل الدخول",
        "username": "اسم المستخدم",
        "current_password": "كلمة المرور الحالية",
        "new_password": "كلمة المرور الجديدة",
        "confirm_new_password": "تأكيد كلمة المرور الجديدة",
        "save_password": "حفظ كلمة المرور",
        "account": "الحساب",
        "change_password": "تغيير كلمة المرور",
        "branch_filter": "فلتر الفرع",
        "all_branches": "كل الفروع",
        "reset": "رجّع الكل",
        "upload_excel": "ارفع إكسل",
        "back_to_dashboard": "ارجع للوحة",
        "back_to_reports": "ارجع للتقارير",
        "export_csv": "صدّر CSV",
        "select_branch": "اختار الفرع",
        "risk": "الخطورة",
        "branch": "الفرع",
        "item": "الصنف",
        "ref": "الكود",
        "sales": "المبيعات",
        "stock": "المخزون",
        "decision": "القرار",
        "stock_value": "قيمة المخزون",
        "lost_sales": "مبيعات ضايعة",
        "frozen_capital": "رأس مال متجمّد",
        "high_risk": "خطورة عالية",
        "current_week": "الأسبوع الحالي",
        "previous_week": "الأسبوع اللي فات",
        "uploads": "مرات الرفع",
        "no_data": "مفيش بيانات.",
        "no_rows_report": "مفيش صفوف في التقرير ده.",
    },
}


def get_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with get_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS uploads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                branch TEXT NOT NULL,
                week_date TEXT NOT NULL,
                filename TEXT NOT NULL,
                uploaded_at TEXT NOT NULL,
                analysis_period_days INTEGER NOT NULL DEFAULT 90,
                total_items INTEGER NOT NULL,
                dead_count INTEGER NOT NULL,
                expiry_count INTEGER NOT NULL,
                slow_count INTEGER NOT NULL,
                fast_count INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                upload_id INTEGER NOT NULL,
                branch TEXT NOT NULL,
                week_date TEXT NOT NULL,
                name TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'UNCATEGORIZED',
                sku TEXT,
                sold REAL NOT NULL,
                stock REAL NOT NULL,
                price REAL NOT NULL,
                cost_price REAL NOT NULL DEFAULT 0,
                stock_value REAL NOT NULL,
                cost_value REAL NOT NULL DEFAULT 0,
                turnover_ratio REAL NOT NULL,
                expiry TEXT,
                days_left INTEGER,
                tags TEXT NOT NULL,
                urgency TEXT,
                action TEXT,
                reason TEXT,
                avg_daily_sales REAL NOT NULL DEFAULT 0,
                days_cover_forecast REAL,
                stockout_probability_7d REAL NOT NULL DEFAULT 0,
                stockout_probability_14d REAL NOT NULL DEFAULT 0,
                stockout_probability_30d REAL NOT NULL DEFAULT 0,
                reorder_point REAL NOT NULL DEFAULT 0,
                recommended_reorder_qty REAL NOT NULL DEFAULT 0,
                risk_score INTEGER NOT NULL DEFAULT 1,
                decision_label TEXT NOT NULL DEFAULT 'Healthy',
                lost_sales_value REAL NOT NULL DEFAULT 0,
                frozen_capital_value REAL NOT NULL DEFAULT 0,
                unit_profit REAL NOT NULL DEFAULT 0,
                gross_margin_percent REAL NOT NULL DEFAULT 0,
                break_even_price REAL NOT NULL DEFAULT 0,
                max_safe_discount_percent REAL NOT NULL DEFAULT 0,
                suggested_offer_price REAL NOT NULL DEFAULT 0,
                offer_discount_percent REAL NOT NULL DEFAULT 0,
                offer_profit_per_unit REAL NOT NULL DEFAULT 0,
                offer_profit_if_sold REAL NOT NULL DEFAULT 0,
                signal_color TEXT NOT NULL DEFAULT 'green',
                FOREIGN KEY(upload_id) REFERENCES uploads(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        existing_item_columns = {row["name"] for row in db.execute("PRAGMA table_info(items)").fetchall()}
        for column, definition in ITEM_EXTRA_COLUMNS.items():
            if column not in existing_item_columns:
                db.execute(f"ALTER TABLE items ADD COLUMN {column} {definition}")
        existing_upload_columns = {row["name"] for row in db.execute("PRAGMA table_info(uploads)").fetchall()}
        for column, definition in UPLOAD_EXTRA_COLUMNS.items():
            if column not in existing_upload_columns:
                db.execute(f"ALTER TABLE uploads ADD COLUMN {column} {definition}")
        user_count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if user_count == 0:
            db.execute(
                "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                (
                    DEFAULT_ADMIN_USERNAME,
                    generate_password_hash(DEFAULT_ADMIN_PASSWORD),
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped_view


def current_language() -> str:
    return session.get("lang", "en") if session.get("lang") in TRANSLATIONS else "en"


def translate(key: str) -> str:
    lang = current_language()
    return TRANSLATIONS.get(lang, TRANSLATIONS["en"]).get(key, TRANSLATIONS["en"].get(key, key))


@app.context_processor
def inject_language_helpers() -> dict[str, Any]:
    lang = current_language()
    return {
        "lang": lang,
        "text_dir": "rtl" if lang == "ar" else "ltr",
        "next_lang": "en" if lang == "ar" else "ar",
        "t": translate,
    }


@app.route("/language/<lang_code>", methods=["POST"])
def set_language(lang_code: str) -> Response:
    session["lang"] = "ar" if lang_code == "ar" else "en"
    return redirect(request.referrer or url_for("index"))


def parse_num(value: Any) -> float:
    if value is None or value == "":
        return 0
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[^0-9.\-]", "", str(value))
    try:
        return float(cleaned) if cleaned else 0
    except ValueError:
        return 0


def parse_optional_float(value: str | None) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_int(value: str | None, default: int = 0) -> int:
    try:
        return int(float(value or default))
    except ValueError:
        return default


def load_analysis_summary(path: Path) -> dict[str, Any]:
    summary_path = path / "summary.json"
    if not summary_path.exists():
        return {}
    return json.loads(summary_path.read_text(encoding="utf-8"))


TAG_ACTIONS = {
    "dead_stock": "Decision: discount, bundle, transfer, or return to supplier.",
    "stockout_with_sales": "Decision: urgent reorder or transfer stock from another branch.",
    "overstock_risk": "Decision: stop reordering and push with offer/transfer.",
    "fast_moving": "Decision: protect availability and raise reorder point.",
    "slow_moving": "Decision: review shelf placement and promo.",
    "expired": "Decision: remove expired units and claim from supplier where possible.",
    "expiry_risk": "Decision: promote, transfer, or return before expiry.",
    "negative_margin": "Decision: fix the selling price or recorded cost.",
}


def action_for_tags(tags: str) -> str:
    actions = [action for tag, action in TAG_ACTIONS.items() if tag in tags]
    return " ".join(actions)


def summary_branch(metadata: dict[str, Any], folder: Path) -> str:
    """Smart imports keep the branch name exactly as entered; legacy POS folders use i-N names."""
    if metadata.get("source") == "smart" and metadata.get("branch"):
        return str(metadata["branch"])
    return normalize_branch_name(metadata.get("branch") or folder.name.split("_")[0])


ITEM_CSV_FIELDS = {
    # db column: (csv column, parser)
    "name": ("name", str),
    "category": ("category", str),
    "sku": ("ref", str),
    "barcode": ("barcode", str),
    "supplier": ("supplier", str),
    "sold": ("pos_sales", parse_num),
    "stock": ("qoh", parse_num),
    "price": ("sales_price", parse_num),
    "cost_price": ("cost", parse_num),
    "stock_value": ("stock_value", parse_num),
    "cost_value": ("cost_value", parse_num),
    "turnover_ratio": ("turnover_ratio", parse_num),
    "avg_daily_sales": ("avg_daily_sales", parse_num),
    "days_cover_forecast": ("days_cover", parse_optional_float),
    "stockout_probability_7d": ("stockout_probability_7d", parse_num),
    "stockout_probability_14d": ("stockout_probability_14d", parse_num),
    "stockout_probability_30d": ("stockout_probability_30d", parse_num),
    "reorder_point": ("reorder_point", parse_num),
    "recommended_reorder_qty": ("recommended_reorder_qty", parse_num),
    "risk_score": ("risk_score", lambda value: parse_int(value, default=1)),
    "decision_label": ("decision_label", str),
    "lost_sales_value": ("lost_sales_value", parse_num),
    "frozen_capital_value": ("frozen_capital_value", parse_num),
    "unit_profit": ("unit_profit", parse_num),
    "gross_margin_percent": ("gross_margin_percent", parse_num),
    "break_even_price": ("break_even_price", parse_num),
    "max_safe_discount_percent": ("max_safe_discount_percent", parse_num),
    "suggested_offer_price": ("suggested_offer_price", parse_num),
    "offer_discount_percent": ("offer_discount_percent", parse_num),
    "offer_profit_per_unit": ("offer_profit_per_unit", parse_num),
    "offer_profit_if_sold": ("offer_profit_if_sold", parse_num),
    "signal_color": ("signal_color", str),
    "net_sales": ("net_sales", parse_num),
    "returns_qty": ("returns_qty", parse_num),
    "cost_known": ("cost_known", lambda value: parse_int(value, default=0)),
    "gross_profit": ("gross_profit", parse_num),
    "lost_profit_value": ("lost_profit_value", parse_num),
    "excess_qty": ("excess_qty", parse_num),
    "excess_value": ("excess_value", parse_num),
    "negative_margin_loss": ("negative_margin_loss", parse_num),
    "expired_qty": ("expired_qty", parse_num),
    "expiry_risk_qty": ("expiry_risk_qty", parse_num),
    "expiry_risk_value": ("expiry_risk_value", parse_num),
    "expiry": ("expiry", str),
    "days_left": ("days_left", parse_optional_float),
}


def import_pos_analysis_to_db(path: Path, replace_all: bool = False, import_token: str = "") -> int:
    init_db()
    summary = load_analysis_summary(path)
    metadata = summary.get("metadata", {})
    branch = summary_branch(metadata, path)
    smart = metadata.get("source") == "smart"
    week_date = (metadata.get("end_date") or date.today().isoformat()).split(" ")[0]
    source_file = ", ".join(source["file"] for source in summary.get("sources", [])) or f"{path.name}/merged_items.csv"

    rows: list[dict[str, Any]] = []
    with (path / "merged_items.csv").open(encoding="utf-8-sig", newline="") as handle:
        for csv_row in csv.DictReader(handle):
            row: dict[str, Any] = {}
            for column, (csv_column, parser) in ITEM_CSV_FIELDS.items():
                raw = csv_row.get(csv_column)
                row[column] = parser(raw) if parser is not str else (raw or "")
            row["category"] = row["category"] or "UNCATEGORIZED"
            row["decision_label"] = row["decision_label"] or "Healthy"
            row["signal_color"] = row["signal_color"] or "green"
            row["days_left"] = int(row["days_left"]) if row["days_left"] is not None else None
            if not smart:
                row_branch = normalize_branch_name(csv_row.get("branch") or branch)
                row["branch"] = branch if row_branch == "i-unknown" else row_branch
            else:
                row["branch"] = csv_row.get("branch") or branch
            if not smart and row["net_sales"] == 0:
                row["net_sales"] = row["sold"] * row["price"]
            tags = csv_row.get("tags") or "normal"
            row.update(
                {
                    "week_date": week_date,
                    "tags": tags,
                    "urgency": "High" if any(tag in tags for tag in ["stockout_with_sales", "dead_stock", "expired", "negative_margin"]) else "",
                    "action": action_for_tags(tags),
                    "reason": "",
                }
            )
            rows.append(row)

    counts = {
        "dead": sum("dead_stock" in row["tags"] for row in rows),
        "alerts": sum(any(tag in row["tags"] for tag in ["stockout_with_sales", "overstock_risk", "expiry_risk", "expired", "negative_margin"]) for row in rows),
        "slow": sum("slow_moving" in row["tags"] for row in rows),
        "fast": sum("fast_moving" in row["tags"] for row in rows),
    }
    branches = sorted({row["branch"] for row in rows}) or [branch]

    with get_db() as db:
        if replace_all:
            db.execute("DELETE FROM items")
            db.execute("DELETE FROM uploads")
        else:
            for item_branch in branches:
                db.execute("DELETE FROM items WHERE branch = ?", (item_branch,))
                db.execute("DELETE FROM uploads WHERE branch = ?", (item_branch,))

        cursor = db.execute(
            """
            INSERT INTO uploads
            (branch, week_date, filename, uploaded_at, analysis_period_days, total_items, dead_count, expiry_count,
             slow_count, fast_count, coverage, quality_score, import_token)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                branch,
                week_date,
                source_file,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                parse_int(metadata.get("period_days"), default=90),
                len(rows),
                counts["dead"],
                counts["alerts"],
                counts["slow"],
                counts["fast"],
                json.dumps(summary.get("coverage") or {}),
                (summary.get("quality") or {}).get("score"),
                import_token,
            ),
        )
        upload_id = cursor.lastrowid
        columns = ["upload_id", "branch", "week_date", "tags", "urgency", "action", "reason", *ITEM_CSV_FIELDS]
        placeholders = ", ".join("?" for _ in columns)
        db.executemany(
            f"INSERT INTO items ({', '.join(columns)}) VALUES ({placeholders})",
            [tuple(upload_id if column == "upload_id" else row[column] for column in columns) for row in rows],
        )
    return len(rows)


def branch_options() -> list[str]:
    with get_db() as db:
        return [row["branch"] for row in db.execute("SELECT DISTINCT branch FROM items ORDER BY branch").fetchall()]


def branch_data_summary() -> list[dict[str, Any]]:
    with get_db() as db:
        rows = db.execute(
            """
            SELECT
                branch,
                COUNT(*) AS item_count,
                COALESCE(SUM(stock_value), 0) AS stock_value,
                COALESCE(SUM(lost_sales_value), 0) AS lost_sales_value,
                MAX(week_date) AS latest_week
            FROM items
            GROUP BY branch
            ORDER BY branch
            """
        ).fetchall()
        upload_counts = {
            row["branch"]: row["upload_count"]
            for row in db.execute(
                """
                SELECT branch, COUNT(*) AS upload_count
                FROM uploads
                GROUP BY branch
                """
            ).fetchall()
        }

    summaries = []
    seen = set()
    for row in rows:
        branch = row["branch"]
        seen.add(branch)
        summaries.append(
            {
                "branch": branch,
                "item_count": row["item_count"],
                "upload_count": upload_counts.get(branch, 0),
                "stock_value": row["stock_value"],
                "lost_sales_value": row["lost_sales_value"],
                "latest_week": row["latest_week"] or "-",
            }
        )

    for branch, upload_count in sorted(upload_counts.items()):
        if branch not in seen:
            summaries.append(
                {
                    "branch": branch,
                    "item_count": 0,
                    "upload_count": upload_count,
                    "stock_value": 0,
                    "lost_sales_value": 0,
                    "latest_week": "-",
                }
            )

    return summaries


def remove_branch_files(branch: str | None = None) -> int:
    removed = 0
    targets: list[Path] = []
    if branch:
        targets.append(POS_RAW_DIR / branch_slug(branch))
        if POS_ANALYSIS_DIR.exists():
            for folder in POS_ANALYSIS_DIR.glob(f"{branch_slug(branch)}_*"):
                if summary_branch(load_analysis_summary(folder).get("metadata", {}), folder) == branch:
                    targets.append(folder)
    else:
        targets.extend([POS_RAW_DIR, POS_ANALYSIS_DIR, UPLOAD_DIR, IMPORTS_DIR])

    for target in targets:
        try:
            resolved = target.resolve()
        except FileNotFoundError:
            continue
        if not resolved.exists() or DATA_DIR.resolve() not in resolved.parents:
            continue
        if resolved.is_dir():
            shutil.rmtree(resolved)
            removed += 1
        else:
            resolved.unlink()
            removed += 1

    for directory in (UPLOAD_DIR, POS_RAW_DIR, POS_ANALYSIS_DIR, IMPORTS_DIR):
        directory.mkdir(parents=True, exist_ok=True)

    return removed


def delete_branch_data(branch: str | None = None, delete_files: bool = True) -> dict[str, int]:
    with get_db() as db:
        if branch:
            item_count = db.execute("SELECT COUNT(*) FROM items WHERE branch = ?", (branch,)).fetchone()[0]
            upload_count = db.execute("SELECT COUNT(*) FROM uploads WHERE branch = ?", (branch,)).fetchone()[0]
            db.execute("DELETE FROM items WHERE branch = ?", (branch,))
            db.execute("DELETE FROM uploads WHERE branch = ?", (branch,))
        else:
            item_count = db.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            upload_count = db.execute("SELECT COUNT(*) FROM uploads").fetchone()[0]
            db.execute("DELETE FROM items")
            db.execute("DELETE FROM uploads")

    file_count = remove_branch_files(branch) if delete_files else 0
    return {"items": item_count, "uploads": upload_count, "files": file_count}


def known_branches() -> list[str]:
    with get_db() as db:
        rows = db.execute("SELECT branch FROM items UNION SELECT branch FROM uploads ORDER BY branch").fetchall()
    return [row["branch"] for row in rows]


def selected_branch(value: str | None) -> str | None:
    if not value or value == "all":
        return None
    value = clean_branch_label(value)
    return value if value in known_branches() else None


def branch_query_args(branch: str | None) -> dict[str, str]:
    return {"branch": branch} if branch else {}


def fetch_items(category: str = "all", limit: int | None = None, branch: str | None = None) -> list[sqlite3.Row]:
    conditions: list[str] = []
    order = "id DESC"
    params: list[Any] = []
    if category == "dead":
        conditions.append("tags LIKE ?")
        params.append("%dead_stock%")
        order = "frozen_capital_value DESC, stock_value DESC"
    elif category == "reorder":
        conditions.append("tags LIKE ?")
        params.append("%stockout_with_sales%")
        order = "risk_score DESC, lost_sales_value DESC, sold DESC"
    elif category == "expiry":
        conditions.append("(tags LIKE ? OR tags LIKE ? OR tags LIKE ?)")
        params.extend(["%expired%", "%expiry_risk%", "%near_expiry%"])
        order = "expiry_risk_value DESC, days_left ASC"
    elif category == "loss":
        conditions.append("negative_margin_loss > 0")
        order = "negative_margin_loss DESC"
    elif category == "overstock":
        conditions.append("tags LIKE ?")
        params.append("%overstock_risk%")
        order = "risk_score DESC, frozen_capital_value DESC, stock_value DESC"
    elif category == "slow":
        conditions.append("tags LIKE ?")
        params.append("%slow_moving%")
        order = "cost_value DESC, days_cover_forecast DESC, stock_value DESC"
    elif category == "fast":
        conditions.append("tags LIKE ?")
        params.append("%fast_moving%")
        order = "sold DESC"
    elif category == "risk":
        conditions.append("risk_score >= ?")
        params.append(8)
        order = "risk_score DESC, lost_sales_value DESC, frozen_capital_value DESC"

    if branch:
        conditions.append("branch = ?")
        params.append(branch)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    sql = f"SELECT * FROM items {where} ORDER BY {order}"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    with get_db() as db:
        return db.execute(sql, params).fetchall()


def dashboard_stats(branch: str | None = None) -> dict[str, Any]:
    where = "WHERE branch = ?" if branch else ""
    params = [branch] if branch else []
    with get_db() as db:
        stats = db.execute(
            f"""
            SELECT
                COUNT(*) AS total_items,
                SUM(CASE WHEN tags LIKE '%dead_stock%' THEN 1 ELSE 0 END) AS dead_count,
                SUM(CASE WHEN tags LIKE '%stockout_with_sales%' THEN 1 ELSE 0 END) AS reorder_count,
                SUM(CASE WHEN tags LIKE '%expired%' OR tags LIKE '%expiry_risk%' THEN 1 ELSE 0 END) AS expiry_count,
                SUM(expiry_risk_value) AS expiry_risk_value,
                SUM(gross_profit) AS gross_profit,
                SUM(net_sales) AS net_sales,
                SUM(negative_margin_loss) AS negative_margin_loss,
                SUM(lost_profit_value) AS lost_profit_value,
                SUM(CASE WHEN tags LIKE '%slow_moving%' THEN 1 ELSE 0 END) AS slow_count,
                SUM(CASE WHEN tags LIKE '%fast_moving%' THEN 1 ELSE 0 END) AS fast_count,
                SUM(CASE WHEN tags LIKE '%overstock_risk%' THEN 1 ELSE 0 END) AS overstock_count,
                SUM(CASE WHEN tags LIKE '%dead_stock%' THEN stock_value ELSE 0 END) AS dead_value,
                SUM(CASE WHEN tags LIKE '%overstock_risk%' THEN stock_value ELSE 0 END) AS overstock_value,
                SUM(lost_sales_value) AS lost_sales_value,
                SUM(frozen_capital_value) AS frozen_capital_value,
                SUM(recommended_reorder_qty) AS reorder_qty_total,
                SUM(CASE WHEN risk_score >= 8 THEN 1 ELSE 0 END) AS high_risk_count,
                AVG(risk_score) AS avg_risk_score,
                COUNT(DISTINCT branch) AS branch_count
            FROM items
            {where}
            """,
            params,
        ).fetchone()
        if branch:
            uploads = db.execute("SELECT * FROM uploads WHERE branch = ? ORDER BY id DESC LIMIT 8", (branch,)).fetchall()
        else:
            uploads = db.execute("SELECT * FROM uploads ORDER BY id DESC LIMIT 8").fetchall()
    return {"stats": stats, "uploads": uploads}


@app.route("/")
@login_required
def index() -> str:
    branch = selected_branch(request.args.get("branch"))
    data = dashboard_stats(branch)
    return render_template(
        "dashboard.html",
        **data,
        selected_branch=branch,
        branch_options=branch_options(),
        branch_args=branch_query_args(branch),
        top_dead=fetch_items("dead", 5, branch),
        top_reorder=fetch_items("reorder", 5, branch),
        profit_loss=profit_loss_for(branch),
        top_overstock=fetch_items("overstock", 5, branch),
        top_fast=fetch_items("fast", 5, branch),
        top_risk=fetch_items("risk", 5, branch),
    )


TOKEN_PATTERN = re.compile(r"[0-9a-f]{16}")

QUALITY_ISSUES = {
    # key: (arabic, english, severity)
    "bad_number": ("خلايا أرقام مش مفهومة واتسابت فاضية", "Number cells that could not be read (left empty)", "warn"),
    "bad_date": ("تواريخ مش مفهومة واتسابت فاضية", "Dates that could not be read (left empty)", "warn"),
    "negative_stock": ("أرصدة بالسالب (اتحسبت صفر)", "Negative stock balances (treated as zero)", "warn"),
    "price_below_cost": ("أصناف سعر بيعها أقل من تكلفتها", "Items priced below cost", "danger"),
    "zero_price": ("أصناف عليها مبيعات وسعرها صفر", "Items with sales but zero price", "warn"),
    "missing_cost": ("أصناف ليها رصيد ومن غير تكلفة", "Items in stock without a cost", "warn"),
    "duplicate_transaction": ("سطور فواتير متكررة اتشالت", "Duplicate invoice lines removed", "info"),
    "duplicate_row": ("صفوف متكررة بالظبط اتشالت", "Exact duplicate rows removed", "info"),
    "duplicate_item": ("صنف متكرر في نفس الملف (اتجمع)", "Item repeated in the same file (merged)", "info"),
    "totals_row": ("صفوف إجمالي اتشالت", "Totals rows removed", "info"),
    "repeated_header": ("عناوين متكررة في نص الملف اتشالت", "Repeated header rows removed", "info"),
    "no_identity": ("صفوف من غير كود أو اسم اتشالت", "Rows without code or name removed", "info"),
    "barcode_scientific": ("باركود مكتوب بصيغة علمية (اتجاهل)", "Barcodes in scientific notation (ignored)", "warn"),
    "return_lines": ("سطور مرتجعات (اتخصمت من المبيعات)", "Return lines (subtracted from sales)", "info"),
}

TABLE_ROLES = {
    "transactions": ("فواتير بيع", "Sales invoices"),
    "sales": ("ملخص مبيعات", "Sales summary"),
    "stock": ("رصيد", "Stock"),
    "expiry": ("صلاحية", "Expiry"),
    "cost": ("تكلفة", "Cost"),
    "price": ("أسعار", "Prices"),
}

PERIOD_SOURCES = {
    "transactions": ("من تواريخ الفواتير", "from invoice dates"),
    "report_header": ("من عنوان التقرير", "from the report header"),
    "column_header": ("من اسم العمود", "from the column name"),
    "user": ("المدة اللي كتبتها", "the period you entered"),
}


def import_dir(token: str) -> Path | None:
    return IMPORTS_DIR / token if TOKEN_PATTERN.fullmatch(token or "") else None


def load_json(path: Path | None) -> dict[str, Any] | None:
    if not path or not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


@app.route("/upload", methods=["GET", "POST"])
@login_required
def upload() -> str | Response:
    if request.method == "GET":
        return render_template("upload.html", branch_choices=known_branches(), extensions=sorted(SUPPORTED_EXTENSIONS))

    files = [file for file in request.files.getlist("files") if file and file.filename]
    if not files:
        flash("اختار ملف واحد على الأقل. / Choose at least one file.", "error")
        return redirect(url_for("upload"))
    if len(files) > MAX_UPLOAD_FILES:
        flash(f"أقصى عدد {MAX_UPLOAD_FILES} ملفات في المرة. / Up to {MAX_UPLOAD_FILES} files at once.", "error")
        return redirect(url_for("upload"))

    token = secrets.token_hex(8)
    folder = IMPORTS_DIR / token / "files"
    folder.mkdir(parents=True)
    profiles = load_profiles(MAPPING_PROFILES_PATH)
    stored = []
    for index, file in enumerate(files):
        original = Path(file.filename).name
        extension = Path(original).suffix.lower()
        if extension not in SUPPORTED_EXTENSIONS:
            shutil.rmtree(folder.parent, ignore_errors=True)
            flash(f"نوع الملف {original} مش مدعوم. / {original}: unsupported file type.", "error")
            return redirect(url_for("upload"))
        target = folder / f"{index:02d}{extension}"
        file.save(target)
        try:
            specs = detect_file(target, display_name=original, profiles_store=profiles)
        except Exception as exc:  # unreadable or corrupt file
            shutil.rmtree(folder.parent, ignore_errors=True)
            flash(f"مقدرتش أقرا {original}: {exc} / Could not read {original}.", "error")
            return redirect(url_for("upload"))
        stored.append({"stored": target.name, "original": original, "specs": [spec.to_dict() for spec in specs]})

    save_json(
        IMPORTS_DIR / token / "draft.json",
        {
            "branch": clean_branch_label(request.form.get("branch", "")),
            "period_days": parse_int(request.form.get("period_days"), 0) or None,
            "files": stored,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        },
    )
    return redirect(url_for("upload_review", token=token))


def draft_needs_period(draft: dict[str, Any]) -> bool:
    """True when sales data exists but nothing in the files tells us the period it covers."""
    has_sales = False
    for file in draft["files"]:
        for spec in file["specs"]:
            if not spec.get("usable"):
                continue
            role = set(spec.get("role") or [])
            if "transactions" in role:
                return False
            if "sales" in role:
                has_sales = True
                meta = spec.get("meta") or {}
                if (meta.get("start_date") and meta.get("end_date")) or meta.get("period_days"):
                    return False
    return has_sales


@app.route("/upload/review/<token>", methods=["GET", "POST"])
@login_required
def upload_review(token: str) -> str | Response:
    folder = import_dir(token)
    draft = load_json(folder / "draft.json" if folder else None)
    if not folder or not draft:
        flash("الرفع ده مش موجود. / This upload no longer exists.", "error")
        return redirect(url_for("upload"))

    if request.method == "GET":
        return render_template(
            "upload_review.html",
            token=token,
            draft=draft,
            field_choices=field_choices(current_language()),
            field_labels=FIELD_LABELS,
            table_roles=TABLE_ROLES,
            needs_period=draft_needs_period(draft),
            branch_choices=known_branches(),
            branch_hint=next((spec["meta"]["branch_hint"] for file in draft["files"] for spec in file["specs"] if spec.get("meta", {}).get("branch_hint")), ""),
        )

    draft["branch"] = clean_branch_label(request.form.get("branch", ""))
    draft["period_days"] = parse_int(request.form.get("period_days"), 0) or None
    tables: list[tuple[Path, TableSpec]] = []
    for file_index, file in enumerate(draft["files"]):
        for table_index, spec_data in enumerate(file["specs"]):
            spec = TableSpec.from_dict(spec_data)
            overrides = {}
            for column in spec.columns:
                chosen = request.form.get(f"map-{file_index}-{table_index}-{column.index}", column.field)
                overrides[column.index] = chosen if chosen in FIELD_LABELS else ""
            apply_user_mapping(spec, overrides)
            file["specs"][table_index] = spec.to_dict()
            if request.form.get(f"use-{file_index}-{table_index}") == "1" and spec.usable:
                tables.append((folder / "files" / file["stored"], spec))
    save_json(folder / "draft.json", draft)
    if not tables:
        flash("مفيش جدول صالح للتحليل. لازم يكون فيه عمود كود أو اسم صنف، وعمود كمية أو رصيد. / No usable table selected.", "error")
        return redirect(url_for("upload_review", token=token))

    try:
        result = run_smart_import(tables, POS_ANALYSIS_DIR, default_branch=draft["branch"], period_days_override=draft["period_days"])
    except ValueError as exc:
        if str(exc) == "period_required":
            flash("الملفات فيها مبيعات بس مش واضح هي عن كام يوم. اكتب مدة المبيعات بالأيام. / Enter how many days the sales cover.", "error")
        else:
            flash(f"مقدرتش أحلل الملفات: {exc} / Could not analyze the files.", "error")
        return redirect(url_for("upload_review", token=token))

    for _path, spec in tables:
        save_profile(MAPPING_PROFILES_PATH, spec)
    for branch_result in result.branches:
        import_pos_analysis_to_db(branch_result.output_dir, import_token=token)

    save_json(
        folder / "result.json",
        {
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "quality": result.quality,
            "branches": [
                {
                    "branch": branch_result.branch,
                    "folder": branch_result.output_dir.name,
                    "item_count": branch_result.item_count,
                    "period_days": branch_result.summary["metadata"]["period_days"],
                    "period_source": branch_result.summary["metadata"]["period_source"],
                    "period_warning": branch_result.summary["metadata"].get("period_warning", ""),
                    "coverage": branch_result.summary["coverage"],
                    "profit_loss": {
                        key: branch_result.summary["profit_loss"][key]
                        for key in ("gross_profit", "margin_pct", "revenue", "total_loss", "total_frozen", "headline_ar", "headline_en", "warnings")
                    },
                }
                for branch_result in result.branches
            ],
            "tables": [{"file": spec.file_name, "sheet": spec.sheet, "role": spec.role} for _path, spec in tables],
        },
    )
    return redirect(url_for("import_result", token=token))


@app.route("/imports/<token>")
@login_required
def import_result(token: str) -> str | Response:
    folder = import_dir(token)
    result = load_json(folder / "result.json" if folder else None)
    if not result:
        return redirect(url_for("uploads"))
    counters = result["quality"].get("counters", {})
    issues = [
        {"key": key, "count": counters[key], "text": QUALITY_ISSUES[key], "samples": result["quality"].get("samples", {}).get(key, [])}
        for key in QUALITY_ISSUES
        if counters.get(key)
    ]
    issues.sort(key=lambda issue: {"danger": 0, "warn": 1, "info": 2}[issue["text"][2]])
    return render_template(
        "import_result.html",
        token=token,
        result=result,
        issues=issues,
        table_roles=TABLE_ROLES,
        period_sources=PERIOD_SOURCES,
    )


def db_row_to_item(row: sqlite3.Row) -> dict[str, Any]:
    """Adapt a DB item row to the analysis schema used by insights.analyze_profit_loss."""
    item = dict(row)
    sold, price, cost = float(item["sold"] or 0), float(item["price"] or 0), float(item["cost_price"] or 0)
    net_sales = float(item["net_sales"] or 0)  # legacy rows get sold * price at import time
    cost_known = bool(item["cost_known"]) or cost > 0
    gross_profit = float(item["gross_profit"] or 0)
    if not gross_profit and cost_known and sold > 0:
        gross_profit = net_sales - sold * cost
    return {
        **item,
        "ref": item["sku"],
        "pos_sales": sold,
        "qoh": item["stock"],
        "sales_price": price,
        "cost": cost,
        "cost_known": int(cost_known),
        "net_sales": net_sales,
        "gross_profit": gross_profit,
        "days_cover": item["days_cover_forecast"],
    }


def profit_loss_for(branch: str | None = None) -> dict[str, Any]:
    where = "WHERE branch = ?" if branch else ""
    params = [branch] if branch else []
    with get_db() as db:
        rows = db.execute(f"SELECT * FROM items {where}", params).fetchall()
        uploads_rows = db.execute(f"SELECT analysis_period_days, coverage FROM uploads {where}", params).fetchall()
    items = [db_row_to_item(row) for row in rows]
    coverage: dict[str, bool] = {}
    for upload_row in uploads_rows:
        try:
            upload_coverage = json.loads(upload_row["coverage"]) if upload_row["coverage"] else {}
        except json.JSONDecodeError:
            upload_coverage = {}
        if not upload_coverage:  # imported by the legacy 3-file pipeline
            upload_coverage = {"sales": True, "stock": True, "price": True, "cost": any(item["cost_known"] for item in items)}
        for key, value in upload_coverage.items():
            coverage[key] = coverage.get(key, False) or bool(value)
    periods = {int(upload_row["analysis_period_days"] or 0) for upload_row in uploads_rows}
    period = periods.pop() if len(periods) == 1 else 0
    result = analyze_profit_loss(items, period_days=period, coverage=coverage)
    result["has_data"] = bool(items)
    return result


@app.route("/profit-loss")
@login_required
def profit_loss() -> str:
    branch = selected_branch(request.args.get("branch"))
    return render_template(
        "profit_loss.html",
        pl=profit_loss_for(branch),
        selected_branch=branch,
        branch_options=branch_options(),
        branch_args=branch_query_args(branch),
    )


@app.route("/download-template")
@login_required
def download_template() -> Response:
    if not BRANCH_TEMPLATE_PATH.exists():
        from create_branch_template import build_workbook

        build_workbook().save(BRANCH_TEMPLATE_PATH)
    return send_file(BRANCH_TEMPLATE_PATH, as_attachment=True, download_name="pharmacy_branch_upload_template.xlsx")


@app.route("/reports/<category>")
@login_required
def report(category: str) -> str:
    labels = {
        "all": "All Items",
        "dead": "Dead Stock",
        "reorder": "Stockout With Sales",
        "expiry": "Expiry Risk",
        "loss": "Selling Below Cost",
        "overstock": "Overstock Risk",
        "slow": "Slow Moving",
        "fast": "Fast Moving",
        "risk": "High Risk Score",
    }
    if category not in labels:
        return redirect(url_for("index"))
    branch = selected_branch(request.args.get("branch"))
    title = labels[category] if not branch else f"{labels[category]} - {branch}"
    return render_template(
        "report.html",
        title=title,
        category=category,
        rows=fetch_items(category, branch=branch),
        selected_branch=branch,
        branch_options=branch_options(),
        branch_args=branch_query_args(branch),
    )


@app.route("/branches/<branch>")
@login_required
def branch_dashboard(branch: str) -> Response:
    normalized = selected_branch(branch)
    if not normalized:
        return redirect(url_for("index"))
    return redirect(url_for("index", branch=normalized))


def financial_rows(branch: str | None = None) -> dict[str, Any]:
    item_where = "WHERE branch = ?" if branch else ""
    item_params = [branch] if branch else []
    with get_db() as db:
        branch_rows = db.execute(
            """
            SELECT
                branch,
                COUNT(*) AS items,
                SUM(lost_sales_value) AS lost_sales_value,
                SUM(frozen_capital_value) AS frozen_capital_value,
                SUM(CASE WHEN tags LIKE '%overstock_risk%' THEN stock_value ELSE 0 END) AS overstock_value,
                SUM(CASE WHEN tags LIKE '%dead_stock%' THEN stock_value ELSE 0 END) AS dead_value,
                SUM(CASE WHEN risk_score >= 8 THEN 1 ELSE 0 END) AS high_risk_count
            FROM items
            GROUP BY branch
            ORDER BY lost_sales_value DESC, frozen_capital_value DESC
            """
        ).fetchall()
        category_rows = db.execute(
            f"""
            SELECT
                category,
                COUNT(*) AS items,
                SUM(lost_sales_value) AS lost_sales_value,
                SUM(frozen_capital_value) AS frozen_capital_value,
                SUM(stock_value) AS stock_value,
                SUM(CASE WHEN risk_score >= 8 THEN 1 ELSE 0 END) AS high_risk_count
            FROM items
            {item_where}
            GROUP BY category
            ORDER BY lost_sales_value DESC, frozen_capital_value DESC
            LIMIT 12
            """,
            item_params,
        ).fetchall()
        totals = db.execute(
            f"""
            SELECT
                SUM(lost_sales_value) AS lost_sales_value,
                SUM(frozen_capital_value) AS frozen_capital_value,
                SUM(CASE WHEN tags LIKE '%overstock_risk%' THEN stock_value ELSE 0 END) AS overstock_value,
                SUM(CASE WHEN tags LIKE '%dead_stock%' THEN stock_value ELSE 0 END) AS dead_value,
                SUM(CASE WHEN risk_score >= 8 THEN 1 ELSE 0 END) AS high_risk_count
            FROM items
            {item_where}
            """,
            item_params,
        ).fetchone()

    branch_chart = [
        {
            "branch": row["branch"],
            "lostSales": round(float(row["lost_sales_value"] or 0), 2),
            "frozenCapital": round(float(row["frozen_capital_value"] or 0), 2),
            "overstock": round(float(row["overstock_value"] or 0), 2),
            "deadCapital": round(float(row["dead_value"] or 0), 2),
            "highRisk": int(row["high_risk_count"] or 0),
            "exposure": round(float((row["lost_sales_value"] or 0) + (row["frozen_capital_value"] or 0)), 2),
        }
        for row in branch_rows
    ]
    category_chart = [
        {
            "category": row["category"],
            "lostSales": round(float(row["lost_sales_value"] or 0), 2),
            "frozenCapital": round(float(row["frozen_capital_value"] or 0), 2),
            "stockValue": round(float(row["stock_value"] or 0), 2),
            "highRisk": int(row["high_risk_count"] or 0),
            "exposure": round(float((row["lost_sales_value"] or 0) + (row["frozen_capital_value"] or 0)), 2),
        }
        for row in category_rows
    ]
    trend_chart = financial_history(branch)
    max_branch_exposure = max([row["exposure"] for row in branch_chart] or [1])
    max_category_exposure = max([row["exposure"] for row in category_chart] or [1])
    return {
        "branch_rows": branch_rows,
        "category_rows": category_rows,
        "totals": totals,
        "max_branch_exposure": max_branch_exposure,
        "max_category_exposure": max_category_exposure,
        "branch_chart": branch_chart,
        "category_chart": category_chart,
        "trend_chart": trend_chart,
    }


def financial_history(branch: str | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not POS_ANALYSIS_DIR.exists():
        return rows

    for summary_path in sorted(POS_ANALYSIS_DIR.glob("*_20*/summary.json"), key=lambda path: path.stat().st_mtime):
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        metadata = summary.get("metadata", {})
        item_branch = summary_branch(metadata, summary_path.parent)
        if branch and item_branch != branch:
            continue
        timestamp = summary_path.parent.name.split("_", 1)[-1]
        rows.append(
            {
                "label": timestamp[-6:-4] + ":" + timestamp[-4:-2] if len(timestamp) >= 6 else summary_path.parent.name,
                "branch": item_branch,
                "lostSales": round(float(summary.get("lost_sales_value") or 0), 2),
                "frozenCapital": round(float(summary.get("frozen_capital_value") or 0), 2),
                "highRisk": int(summary.get("high_risk_count") or 0),
                "reorderQty": round(float(summary.get("reorder_qty_total") or 0), 2),
                "exposure": round(float(summary.get("lost_sales_value") or 0) + float(summary.get("frozen_capital_value") or 0), 2),
            }
        )
    return rows[-18:]


def summary_sort_value(summary_path: Path, metadata: dict[str, Any]) -> str:
    end_date = str(metadata.get("end_date") or "").strip()
    if end_date:
        return end_date
    timestamp = summary_path.parent.name.split("_", 1)[-1]
    return timestamp or datetime.fromtimestamp(summary_path.stat().st_mtime).isoformat()


def summary_label(summary_path: Path, metadata: dict[str, Any]) -> str:
    start_date = str(metadata.get("start_date") or "").strip()
    end_date = str(metadata.get("end_date") or "").strip()
    if start_date and end_date:
        return f"{start_date} to {end_date}"
    if end_date:
        return end_date
    return summary_path.parent.name.split("_", 1)[-1]


def summary_metric(summary: dict[str, Any], metric_key: str) -> float:
    if metric_key == "dead_stock":
        return float((summary.get("tag_counts") or {}).get("dead_stock") or 0)
    return float(summary.get(metric_key) or 0)


def change_percent(current: float, previous: float) -> float | None:
    if previous == 0:
        return 0 if current == 0 else None
    return ((current - previous) / abs(previous)) * 100


def change_class(delta: float) -> str:
    if delta > 0:
        return "up"
    if delta < 0:
        return "down"
    return "flat"


def comparison_metric(current: float, previous: float | None) -> dict[str, Any]:
    previous_value = previous if previous is not None else 0
    delta = current - previous_value if previous is not None else 0
    percent = change_percent(current, previous_value) if previous is not None else None
    return {
        "current": round(current, 2),
        "previous": round(previous_value, 2),
        "delta": round(delta, 2),
        "percent": None if percent is None else round(percent, 1),
        "class": change_class(delta) if previous is not None else "flat",
    }


def weekly_summary_records(branch: str | None = None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not POS_ANALYSIS_DIR.exists():
        return records

    for summary_path in POS_ANALYSIS_DIR.glob("*_20*/summary.json"):
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        metadata = summary.get("metadata", {})
        item_branch = summary_branch(metadata, summary_path.parent)
        if branch and item_branch != branch:
            continue
        records.append(
            {
                "branch": item_branch,
                "folder": summary_path.parent.name,
                "label": summary_label(summary_path, metadata),
                "sort_value": summary_sort_value(summary_path, metadata),
                "summary": summary,
            }
        )
    return sorted(records, key=lambda row: (row["branch"], row["sort_value"], row["folder"]))


def branch_weekly_comparison(branch: str | None = None) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in weekly_summary_records(branch):
        grouped.setdefault(record["branch"], []).append(record)

    rows: list[dict[str, Any]] = []
    for item_branch, records in sorted(grouped.items()):
        current = records[-1]
        previous = records[-2] if len(records) > 1 else None
        metrics = {}
        for key, _label, summary_key in WEEKLY_COMPARISON_METRICS:
            current_value = summary_metric(current["summary"], summary_key)
            previous_value = summary_metric(previous["summary"], summary_key) if previous else None
            metrics[key] = comparison_metric(current_value, previous_value)
        rows.append(
            {
                "branch": item_branch,
                "current_label": current["label"],
                "current_folder": current["folder"],
                "previous_label": previous["label"] if previous else "No previous upload",
                "previous_folder": previous["folder"] if previous else "",
                "upload_count": len(records),
                "has_previous": previous is not None,
                "metrics": metrics,
            }
        )

    totals = {}
    for key, _label, _summary_key in WEEKLY_COMPARISON_METRICS:
        current_total = sum(row["metrics"][key]["current"] for row in rows)
        comparable = [row for row in rows if row["has_previous"]]
        previous_total = sum(row["metrics"][key]["previous"] for row in comparable)
        totals[key] = comparison_metric(current_total, previous_total if comparable else None)

    return {"rows": rows, "totals": totals}


def product_history(sku: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not POS_ANALYSIS_DIR.exists():
        return rows

    for csv_path in sorted(POS_ANALYSIS_DIR.glob("*_20*/merged_items.csv"), key=lambda path: path.stat().st_mtime):
        summary = load_analysis_summary(csv_path.parent)
        metadata = summary.get("metadata", {})
        folder_branch = summary_branch(metadata, csv_path.parent)
        smart = metadata.get("source") == "smart"
        label = metadata.get("end_date") or csv_path.parent.name.split("_", 1)[-1]
        try:
            with csv_path.open(encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    if str(row.get("ref") or "").strip() != sku:
                        continue
                    item_branch = (row.get("branch") or folder_branch) if smart else normalize_branch_name(row.get("branch") or folder_branch)
                    if item_branch == "i-unknown":
                        item_branch = folder_branch
                    rows.append(
                        {
                            "label": label,
                            "folder": csv_path.parent.name,
                            "branch": item_branch,
                            "name": row.get("name") or "",
                            "category": row.get("category") or "UNCATEGORIZED",
                            "sold": parse_num(row.get("pos_sales")),
                            "stock": parse_num(row.get("qoh")),
                            "sales_price": parse_num(row.get("sales_price")),
                            "cost_price": parse_num(row.get("cost")),
                            "stock_value": parse_num(row.get("stock_value")),
                            "cost_value": parse_num(row.get("cost_value")),
                            "risk_score": parse_int(row.get("risk_score"), 1),
                            "decision_label": row.get("decision_label") or "",
                            "tags": row.get("tags") or "",
                            "lost_sales_value": parse_num(row.get("lost_sales_value")),
                            "frozen_capital_value": parse_num(row.get("frozen_capital_value")),
                            "stockout_probability_30d": parse_num(row.get("stockout_probability_30d")),
                        }
                    )
        except OSError:
            continue
    return rows


@app.route("/products/<path:sku>")
@login_required
def product_detail(sku: str) -> str:
    branch = selected_branch(request.args.get("branch"))
    params: list[Any] = [sku]
    branch_clause = ""
    if branch:
        branch_clause = "AND branch = ?"
        params.append(branch)
    with get_db() as db:
        current_rows = db.execute(
            f"""
            SELECT * FROM items
            WHERE sku = ? {branch_clause}
            ORDER BY risk_score DESC, lost_sales_value DESC, frozen_capital_value DESC
            """,
            params,
        ).fetchall()
        comparison_rows = db.execute(
            """
            SELECT * FROM items
            WHERE sku = ?
            ORDER BY branch
            """,
            (sku,),
        ).fetchall()

    history = product_history(sku)
    product_name = ""
    if current_rows:
        product_name = current_rows[0]["name"]
    elif history:
        product_name = history[-1]["name"]
    else:
        product_name = sku

    max_history_sales = max(max([row["sold"] for row in history] or [1]), 1)
    max_branch_stock = max(max([row["stock"] for row in comparison_rows] or [1]), 1)
    return render_template(
        "product.html",
        sku=sku,
        product_name=product_name,
        selected_branch=branch,
        branch_options=branch_options(),
        current_rows=current_rows,
        comparison_rows=comparison_rows,
        history_rows=history,
        max_history_sales=max_history_sales,
        max_branch_stock=max_branch_stock,
    )


@app.route("/financial")
@login_required
def financial() -> str:
    branch = selected_branch(request.args.get("branch"))
    return render_template(
        "financial.html",
        **financial_rows(branch),
        selected_branch=branch,
        branch_options=branch_options(),
        branch_args=branch_query_args(branch),
    )


@app.route("/weekly-comparison")
@login_required
def weekly_comparison() -> str:
    branch = selected_branch(request.args.get("branch"))
    comparison = branch_weekly_comparison(branch)
    available_branches = sorted({row["branch"] for row in branch_weekly_comparison()["rows"]})
    return render_template(
        "weekly_comparison.html",
        rows=comparison["rows"],
        totals=comparison["totals"],
        metrics=WEEKLY_COMPARISON_METRICS,
        selected_branch=branch,
        branch_options=available_branches,
        branch_args=branch_query_args(branch),
    )


@app.route("/uploads")
@login_required
def uploads() -> str:
    with get_db() as db:
        rows = db.execute("SELECT * FROM uploads ORDER BY id DESC").fetchall()
    return render_template("uploads.html", rows=rows)


@app.route("/data-management", methods=["GET", "POST"])
@login_required
def data_management() -> str | Response:
    if request.method == "POST":
        mode = request.form.get("mode", "")
        confirm = request.form.get("confirm", "").strip()
        delete_files = request.form.get("delete_files") == "1"

        if mode == "all":
            if confirm != "DELETE ALL":
                flash("Type DELETE ALL to confirm formatting all branch data.", "error")
                return redirect(url_for("data_management"))
            result = delete_branch_data(None, delete_files)
            flash(
                f"Formatted all branches: deleted {result['items']} items, {result['uploads']} imports, and {result['files']} file folders.",
                "success",
            )
            return redirect(url_for("data_management"))

        if mode == "branch":
            branch = selected_branch(request.form.get("branch"))
            if not branch:
                flash("Choose a branch that has data.", "error")
                return redirect(url_for("data_management"))
            if confirm != branch:
                flash(f"Type {branch} to confirm deleting this branch.", "error")
                return redirect(url_for("data_management"))
            result = delete_branch_data(branch, delete_files)
            flash(
                f"Deleted {branch}: removed {result['items']} items, {result['uploads']} imports, and {result['files']} file folders.",
                "success",
            )
            return redirect(url_for("data_management"))

        flash("Choose what you want to delete first.", "error")
        return redirect(url_for("data_management"))

    summaries = branch_data_summary()
    totals = {
        "items": sum(row["item_count"] for row in summaries),
        "uploads": sum(row["upload_count"] for row in summaries),
        "stock_value": sum(float(row["stock_value"] or 0) for row in summaries),
        "lost_sales_value": sum(float(row["lost_sales_value"] or 0) for row in summaries),
    }
    return render_template(
        "data_management.html",
        summaries=summaries,
        totals=totals,
        branch_choices=known_branches(),
    )


@app.route("/export/<category>")
@login_required
def export(category: str) -> Response:
    branch = selected_branch(request.args.get("branch"))
    rows = fetch_items(category, branch=branch)
    show_offer_columns = category in {"slow", "dead"}
    output = io.StringIO()
    writer = csv.writer(output)
    headers = [
        "Period",
        "Branch",
        "Item",
        "Ref",
        "Sales",
        "Stock",
        "Price",
    ]
    if show_offer_columns:
        headers.extend(
            [
                "Cost Price",
                "Unit Profit",
                "Gross Margin %",
                "Break Even Price",
                "Max Safe Discount %",
                "Suggested Offer Price",
                "Offer Discount %",
                "Offer Profit Per Unit",
                "Offer Profit If Sold",
            ]
        )
    headers.extend([
        "Stock Value",
        "Cost Value",
        "Avg Daily Sales",
        "Days Cover",
        "Stockout 7d",
        "Stockout 14d",
        "Stockout 30d",
        "Reorder Point",
        "Recommended Reorder Qty",
        "Risk Score",
        "Lost Sales Value",
        "Frozen Capital",
        "Signal",
        "Decision",
    ])
    writer.writerow(headers)
    for row in rows:
        values = [
            row["week_date"],
            row["branch"],
            row["name"],
            row["sku"],
            row["sold"],
            row["stock"],
            row["price"],
        ]
        if show_offer_columns:
            values.extend(
                [
                    row["cost_price"],
                    row["unit_profit"],
                    row["gross_margin_percent"],
                    row["break_even_price"],
                    row["max_safe_discount_percent"],
                    row["suggested_offer_price"],
                    row["offer_discount_percent"],
                    row["offer_profit_per_unit"],
                    row["offer_profit_if_sold"],
                ]
            )
        values.extend([
            row["stock_value"],
            row["cost_value"],
            row["avg_daily_sales"],
            row["days_cover_forecast"],
            row["stockout_probability_7d"],
            row["stockout_probability_14d"],
            row["stockout_probability_30d"],
            row["reorder_point"],
            row["recommended_reorder_qty"],
            row["risk_score"],
            row["lost_sales_value"],
            row["frozen_capital_value"],
            row["tags"],
            row["decision_label"] or row["action"],
        ])
        writer.writerow(values)
    return Response(
        "\ufeff" + output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=pharmacy_{category}.csv"},
    )


@app.route("/login", methods=["GET", "POST"])
def login() -> str | Response:
    if session.get("user_id"):
        return redirect(url_for("index"))

    if request.method == "GET":
        return render_template("login.html")

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    with get_db() as db:
        user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()

    if not user or not check_password_hash(user["password_hash"], password):
        flash("اسم المستخدم أو كلمة المرور غير صحيحة.", "error")
        return redirect(url_for("login"))

    lang = current_language()
    session.clear()
    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session["lang"] = lang
    return redirect(request.args.get("next") or url_for("index"))


@app.route("/logout", methods=["POST"])
def logout() -> Response:
    lang = current_language()
    session.clear()
    session["lang"] = lang
    flash("تم تسجيل الخروج بنجاح.", "success")
    return redirect(url_for("login"))


@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password() -> str | Response:
    if request.method == "GET":
        return render_template("change_password.html")

    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    if len(new_password) < 8:
        flash("كلمة المرور الجديدة يجب أن تكون 8 أحرف على الأقل.", "error")
        return redirect(url_for("change_password"))
    if new_password != confirm_password:
        flash("تأكيد كلمة المرور غير مطابق.", "error")
        return redirect(url_for("change_password"))

    with get_db() as db:
        user = db.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
        if not user or not check_password_hash(user["password_hash"], current_password):
            flash("كلمة المرور الحالية غير صحيحة.", "error")
            return redirect(url_for("change_password"))
        db.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (generate_password_hash(new_password), session["user_id"]),
        )

    flash("تم تغيير كلمة المرور بنجاح.", "success")
    return redirect(url_for("index"))


init_db()


if __name__ == "__main__":
    app.run(debug=False, use_reloader=False)
