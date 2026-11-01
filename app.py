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

from flask import Flask, Response, abort, flash, g, redirect, render_template, request, send_file, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import actions as action_plans
import ask as data_questions
import db as database
from insights import analyze_profit_loss
from pos_pipeline import normalize_branch_name
from settings import BUSINESS_PROFILES, SETTING_FIELDS, SETTING_GROUPS, AnalysisSettings
from settings import dumps as dump_settings
from settings import loads as load_settings_json
from smart_import import (
    FIELD_LABELS,
    SUPPORTED_EXTENSIONS,
    TableSpec,
    apply_user_mapping,
    detect_file,
    field_choices,
    load_profiles,
    profile_entry,
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
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip() or None  # PostgreSQL when set, SQLite otherwise
BRANCH_TEMPLATE_PATH = BASE_DIR / "pharmacy_branch_upload_template.xlsx"

MAX_UPLOAD_FILES = 10
DEFAULT_ADMIN_USERNAME = os.environ.get("PHARMACY_ADMIN_USERNAME", "admin")
KNOWN_DEFAULT_PASSWORD = "admin123"  # published in the README: whoever still uses it must change it
DEFAULT_ADMIN_PASSWORD = os.environ.get("PHARMACY_ADMIN_PASSWORD", KNOWN_DEFAULT_PASSWORD)

# Marketing / public site. All optional: the landing page works without them.
WHATSAPP_NUMBER = re.sub(r"\D", "", os.environ.get("PHARMACY_WHATSAPP_NUMBER", ""))  # international, e.g. 201001234567
CONTACT_EMAIL = os.environ.get("PHARMACY_CONTACT_EMAIL", "").strip()
PUBLIC_URL = os.environ.get("PHARMACY_PUBLIC_URL", "").strip().rstrip("/")
DEMO_MODE = os.environ.get("PHARMACY_DEMO_MODE", "").strip().lower() in {"1", "true", "yes"}
DEMO_USERNAME = os.environ.get("PHARMACY_DEMO_USERNAME", "demo")

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
    "base_daily_sales": "REAL NOT NULL DEFAULT 0",
    "unavailable_days": "INTEGER NOT NULL DEFAULT 0",
    "sales_trend": "TEXT NOT NULL DEFAULT ''",
    "trend_ratio": "REAL NOT NULL DEFAULT 1",
    "lead_time_days": "INTEGER NOT NULL DEFAULT 7",
    "abc_class": "TEXT NOT NULL DEFAULT ''",
    "xyz_class": "TEXT NOT NULL DEFAULT ''",
    "demand_cv": "REAL",
}

USER_EXTRA_COLUMNS = {
    "role": "TEXT NOT NULL DEFAULT 'owner'",  # users created before roles existed were full admins
    "branches": "TEXT NOT NULL DEFAULT '[]'",
    "must_change_password": "INTEGER NOT NULL DEFAULT 0",
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
app.config["ENFORCE_PASSWORD_CHANGE"] = True

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
        "trends": "Trends over time",
        "ask": "Ask your data",
        "users": "Users & permissions",
        "actions": "Action center",
        "abc": "ABC analysis",
        "settings": "Analysis settings",
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
        "trends": "التطور مع الوقت",
        "ask": "اسأل بياناتك",
        "users": "المستخدمين والصلاحيات",
        "actions": "مركز الأفعال",
        "abc": "تحليل ABC",
        "settings": "إعدادات التحليل",
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


def get_db() -> database.Connection:
    return database.connect(DATABASE_URL, DB_PATH)


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

            CREATE TABLE IF NOT EXISTS mapping_profiles (
                signature TEXT PRIMARY KEY,
                data TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS import_runs (
                token TEXT PRIMARY KEY,
                finished_at TEXT NOT NULL,
                result TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                branch TEXT NOT NULL,
                period_start TEXT NOT NULL DEFAULT '',
                period_end TEXT NOT NULL,
                period_days INTEGER NOT NULL,
                uploaded_at TEXT NOT NULL,
                source_folder TEXT NOT NULL DEFAULT '',
                item_count INTEGER NOT NULL,
                has_cost INTEGER NOT NULL DEFAULT 0,
                net_sales REAL NOT NULL DEFAULT 0,
                gross_profit REAL NOT NULL DEFAULT 0,
                margin_pct REAL NOT NULL DEFAULT 0,
                stock_value REAL NOT NULL DEFAULT 0,
                total_loss REAL NOT NULL DEFAULT 0,
                total_frozen REAL NOT NULL DEFAULT 0,
                lost_sales_value REAL NOT NULL DEFAULT 0,
                frozen_capital_value REAL NOT NULL DEFAULT 0,
                expiry_risk_value REAL NOT NULL DEFAULT 0,
                high_risk_count INTEGER NOT NULL DEFAULT 0,
                dead_count INTEGER NOT NULL DEFAULT 0,
                stockout_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS item_history (
                snapshot_id INTEGER NOT NULL,
                branch TEXT NOT NULL,
                sku TEXT NOT NULL,
                name TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT '',
                sold REAL NOT NULL DEFAULT 0,
                stock REAL NOT NULL DEFAULT 0,
                net_sales REAL NOT NULL DEFAULT 0,
                gross_profit REAL NOT NULL DEFAULT 0,
                avg_daily_sales REAL NOT NULL DEFAULT 0,
                risk_score INTEGER NOT NULL DEFAULT 1,
                loss_value REAL NOT NULL DEFAULT 0,
                frozen_value REAL NOT NULL DEFAULT 0,
                expiry_risk_value REAL NOT NULL DEFAULT 0,
                tags TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_item_history_sku ON item_history(sku, branch);
            CREATE INDEX IF NOT EXISTS idx_item_history_snapshot ON item_history(snapshot_id);
            CREATE INDEX IF NOT EXISTS idx_snapshots_branch ON snapshots(branch, period_end);

            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                name TEXT NOT NULL,
                pharmacy TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL,
                city TEXT NOT NULL DEFAULT '',
                branches INTEGER NOT NULL DEFAULT 1,
                pos_system TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'new'
            );

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        existing_item_columns = db.table_columns("items")
        for column, definition in ITEM_EXTRA_COLUMNS.items():
            if column not in existing_item_columns:
                db.execute(f"ALTER TABLE items ADD COLUMN {column} {definition}")
        existing_user_columns = db.table_columns("users")
        for column, definition in USER_EXTRA_COLUMNS.items():
            if column not in existing_user_columns:
                db.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")
        existing_upload_columns = db.table_columns("uploads")
        for column, definition in UPLOAD_EXTRA_COLUMNS.items():
            if column not in existing_upload_columns:
                db.execute(f"ALTER TABLE uploads ADD COLUMN {column} {definition}")
        user_count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if user_count == 0:
            db.execute(
                "INSERT INTO users (username, password_hash, created_at, must_change_password) VALUES (?, ?, ?, ?)",
                (
                    DEFAULT_ADMIN_USERNAME,
                    generate_password_hash(DEFAULT_ADMIN_PASSWORD),
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    1,  # the first password is either the published default or one set in the server env
                ),
            )


def business_type() -> str:
    if "business_type" not in g:
        try:
            g.business_type = load_analysis_settings().business_type
        except Exception:  # database not ready yet (first start)
            g.business_type = "pharmacy"
    return g.business_type


def load_analysis_settings() -> AnalysisSettings:
    with get_db() as db:
        row = db.execute("SELECT value FROM app_settings WHERE key = 'analysis'").fetchone()
    return load_settings_json(row["value"] if row else None)


def save_analysis_settings(settings: AnalysisSettings) -> None:
    with get_db() as db:
        db.upsert("app_settings", "key", {"key": "analysis", "value": dump_settings(settings)})


ROLES = {
    "owner": ("المالك: كل الفروع وكل الصلاحيات", "Owner: all branches, full control"),
    "manager": ("مدير فرع: فروعه بس، ويقدر يرفع ملفاتها", "Branch manager: own branches, can upload"),
    "viewer": ("مشاهد: فروعه بس، من غير تعديل", "Viewer: own branches, read only"),
}
PERMISSIONS = {"upload": {"owner", "manager"}, "admin": {"owner"}}
NO_BRANCH = "—"  # selected for a restricted user who has no branches yet: matches no data


def current_user() -> dict[str, Any] | None:
    if "user" not in g:
        g.user = None
        if session.get("user_id"):
            with get_db() as db:
                row = db.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
            g.user = dict(row) if row else None
    return g.user


def user_branches(user: dict[str, Any]) -> list[str]:
    try:
        branches = json.loads(user.get("branches") or "[]")
    except json.JSONDecodeError:
        return []
    return [str(branch) for branch in branches if str(branch).strip()]


def allowed_branches() -> list[str] | None:
    """None means every branch (owner); otherwise the only branches this user may see."""
    user = current_user()
    if user is None or user.get("role", "owner") == "owner":
        return None
    return user_branches(user)


def can(action: str) -> bool:
    user = current_user()
    return bool(user) and user.get("role", "owner") in PERMISSIONS[action]


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", next=request.path))
        if current_user() is None:  # the account was deleted
            session.clear()
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped_view


def permission_required(action: str):
    def decorator(view):
        @wraps(view)
        def wrapped_view(*args, **kwargs):
            if not can(action):
                abort(403)
            return view(*args, **kwargs)

        return wrapped_view

    return decorator


@app.errorhandler(403)
def forbidden(_error: Exception) -> tuple[str, int]:
    return render_template("forbidden.html"), 403


PASSWORD_CHANGE_EXEMPT = {"change_password", "logout", "static", "set_language", "privacy", "free_analysis", "free_analysis_thanks"}


@app.before_request
def require_password_change() -> Response | None:
    """A user on a default or admin-set password must pick their own before using the app."""
    if not app.config.get("ENFORCE_PASSWORD_CHANGE") or not session.get("must_change_password"):
        return None
    if request.endpoint in PASSWORD_CHANGE_EXEMPT:
        return None
    flash("لازم تغير كلمة المرور الأول قبل ما تكمل. / Please choose a new password first.", "error")
    return redirect(url_for("change_password"))


def current_language() -> str:
    return session.get("lang", "en") if session.get("lang") in TRANSLATIONS else "en"


def translate(key: str) -> str:
    lang = current_language()
    if key == "app_description":  # wording follows the business type chosen in settings
        return BUSINESS_PROFILES[business_type()]["description"][0 if lang == "ar" else 1]
    return TRANSLATIONS.get(lang, TRANSLATIONS["en"]).get(key, TRANSLATIONS["en"].get(key, key))


@app.context_processor
def inject_language_helpers() -> dict[str, Any]:
    lang = current_language()
    return {
        "lang": lang,
        "text_dir": "rtl" if lang == "ar" else "ltr",
        "next_lang": "en" if lang == "ar" else "ar",
        "t": translate,
        "can": can,
        "scoped_user": allowed_branches() is not None,
        "user": current_user(),
        "roles": ROLES,
        "is_demo_user": bool(current_user()) and current_user()["username"] == DEMO_USERNAME and DEMO_MODE,
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
    "base_daily_sales": ("base_daily_sales", parse_num),
    "unavailable_days": ("unavailable_days", lambda value: parse_int(value, default=0)),
    "sales_trend": ("sales_trend", str),
    "trend_ratio": ("trend_ratio", lambda value: parse_num(value) or 1.0),
    "lead_time_days": ("lead_time_days", lambda value: parse_int(value, default=7)),
    "abc_class": ("abc_class", str),
    "xyz_class": ("xyz_class", str),
    "demand_cv": ("demand_cv", parse_optional_float),
}


def to_iso_date(value: str) -> str:
    text = str(value or "").strip().split(" ")[0]
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return text


def summary_coverage(summary: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, bool]:
    if summary.get("coverage"):
        return summary["coverage"]
    # imported by the legacy 3-file pipeline
    return {"sales": True, "stock": True, "price": True, "cost": any(float(row.get("cost_price") or 0) > 0 for row in rows)}


def read_analysis_rows(path: Path) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
    summary = load_analysis_summary(path)
    metadata = summary.get("metadata", {})
    branch = summary_branch(metadata, path)
    smart = metadata.get("source") == "smart"
    week_date = (metadata.get("end_date") or date.today().isoformat()).split(" ")[0]

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
    return summary, branch, rows


def item_loss_value(item: dict[str, Any]) -> float:
    lost = item["lost_profit_value"] if item.get("cost_known") else item.get("lost_sales_value", 0)
    return float(item.get("negative_margin_loss") or 0) + float(item.get("expiry_risk_value") or 0) + float(lost or 0)


def record_snapshot(db: database.Connection, summary: dict[str, Any], branch: str, rows: list[dict[str, Any]], folder: str) -> int:
    """Store one upload's totals and per-item numbers so history survives later uploads.

    Re-uploading the same branch and period replaces that snapshot instead of duplicating it.
    """
    metadata = summary.get("metadata", {})
    period_days = parse_int(metadata.get("period_days"), default=90)
    period_end = to_iso_date(metadata.get("end_date") or "") or date.today().isoformat()
    period_start = to_iso_date(metadata.get("start_date") or "")
    items = [db_row_to_item(row) for row in rows]
    settings = AnalysisSettings.from_dict(metadata.get("settings"))
    pl = analyze_profit_loss(items, period_days=period_days, coverage=summary_coverage(summary, rows), settings=settings)
    for old in db.execute("SELECT id FROM snapshots WHERE branch = ? AND period_end = ? AND period_days = ?", (branch, period_end, period_days)).fetchall():
        db.execute("DELETE FROM item_history WHERE snapshot_id = ?", (old["id"],))
        db.execute("DELETE FROM snapshots WHERE id = ?", (old["id"],))
    snapshot_id = db.insert(
        """
        INSERT INTO snapshots
        (branch, period_start, period_end, period_days, uploaded_at, source_folder, item_count, has_cost, net_sales,
         gross_profit, margin_pct, stock_value, total_loss, total_frozen, lost_sales_value, frozen_capital_value,
         expiry_risk_value, high_risk_count, dead_count, stockout_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            branch, period_start, period_end, period_days, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), folder,
            len(rows), int(bool(pl["coverage"].get("cost"))), pl["revenue"], pl["gross_profit"], pl["margin_pct"],
            sum(float(row["stock_value"] or 0) for row in rows), pl["total_loss"], pl["total_frozen"],
            sum(float(row["lost_sales_value"] or 0) for row in rows), sum(float(row["frozen_capital_value"] or 0) for row in rows),
            sum(float(row["expiry_risk_value"] or 0) for row in rows), sum(1 for row in rows if row["risk_score"] >= 8),
            sum("dead_stock" in row["tags"] for row in rows), sum("stockout_with_sales" in row["tags"] for row in rows),
        ),
    )
    db.executemany(
        """
        INSERT INTO item_history
        (snapshot_id, branch, sku, name, category, sold, stock, net_sales, gross_profit, avg_daily_sales, risk_score,
         loss_value, frozen_value, expiry_risk_value, tags)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                snapshot_id, item["branch"], item["sku"], item["name"], item["category"], item["sold"], item["stock"],
                item["net_sales"], item["gross_profit"], item["avg_daily_sales"], item["risk_score"], item_loss_value(item),
                float(item["frozen_capital_value"] or 0), float(item["expiry_risk_value"] or 0), item["tags"],
            )
            for item in items
        ],
    )
    return snapshot_id


def backfill_snapshots() -> int:
    """One-time: turn analysis folders saved before history existed into snapshots."""
    with get_db() as db:
        if db.execute("SELECT value FROM app_settings WHERE key = 'snapshots_backfilled'").fetchone():
            return 0
        db.upsert("app_settings", "key", {"key": "snapshots_backfilled", "value": "1"})
    count = 0
    folders = sorted(POS_ANALYSIS_DIR.glob("*_20*/merged_items.csv"), key=lambda csv_path: csv_path.stat().st_mtime) if POS_ANALYSIS_DIR.exists() else []
    for csv_path in folders:
        try:
            summary, branch, rows = read_analysis_rows(csv_path.parent)
        except (OSError, ValueError, KeyError):
            continue
        with get_db() as db:
            record_snapshot(db, summary, branch, rows, csv_path.parent.name)
        count += 1
    return count


def import_pos_analysis_to_db(path: Path, replace_all: bool = False, import_token: str = "") -> int:
    init_db()
    summary, branch, rows = read_analysis_rows(path)
    metadata = summary.get("metadata", {})
    week_date = (metadata.get("end_date") or date.today().isoformat()).split(" ")[0]
    source_file = ", ".join(source["file"] for source in summary.get("sources", [])) or f"{path.name}/merged_items.csv"

    counts = {
        "dead": sum("dead_stock" in row["tags"] for row in rows),
        "alerts": sum(any(tag in row["tags"] for tag in ["stockout_with_sales", "overstock_risk", "expiry_risk", "expired", "negative_margin"]) for row in rows),
        "slow": sum("slow_moving" in row["tags"] for row in rows),
        "fast": sum("fast_moving" in row["tags"] for row in rows),
    }
    branches = sorted({row["branch"] for row in rows}) or [branch]

    with get_db() as db:
        # The items table holds each branch's current picture; uploads and snapshots keep the history.
        if replace_all:
            db.execute("DELETE FROM items")
        else:
            for item_branch in branches:
                db.execute("DELETE FROM items WHERE branch = ?", (item_branch,))

        upload_id = db.insert(
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
        columns = ["upload_id", "branch", "week_date", "tags", "urgency", "action", "reason", *ITEM_CSV_FIELDS]
        placeholders = ", ".join("?" for _ in columns)
        db.executemany(
            f"INSERT INTO items ({', '.join(columns)}) VALUES ({placeholders})",
            [tuple(upload_id if column == "upload_id" else row[column] for column in columns) for row in rows],
        )
        record_snapshot(db, summary, branch, rows, path.name)
    return len(rows)


def branch_options() -> list[str]:
    with get_db() as db:
        branches = [row["branch"] for row in db.execute("SELECT DISTINCT branch FROM items ORDER BY branch").fetchall()]
    allowed = allowed_branches()
    return branches if allowed is None else [branch for branch in branches if branch in allowed]


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
            db.execute("DELETE FROM item_history WHERE snapshot_id IN (SELECT id FROM snapshots WHERE branch = ?)", (branch,))
            db.execute("DELETE FROM snapshots WHERE branch = ?", (branch,))
        else:
            item_count = db.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            upload_count = db.execute("SELECT COUNT(*) FROM uploads").fetchone()[0]
            db.execute("DELETE FROM items")
            db.execute("DELETE FROM uploads")
            db.execute("DELETE FROM item_history")
            db.execute("DELETE FROM snapshots")

    file_count = remove_branch_files(branch) if delete_files else 0
    return {"items": item_count, "uploads": upload_count, "files": file_count}


def known_branches() -> list[str]:
    with get_db() as db:
        rows = db.execute("SELECT branch FROM items UNION SELECT branch FROM uploads ORDER BY branch").fetchall()
    return [row["branch"] for row in rows]


def selected_branch(value: str | None) -> str | None:
    allowed = allowed_branches()
    if allowed is not None:
        # Restricted users always look at one of their own branches.
        value = clean_branch_label(value or "")
        return value if value in allowed else (allowed[0] if allowed else NO_BRANCH)
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
def index() -> str | Response:
    """Public marketing page for visitors; signed-in users go straight to their dashboard."""
    if session.get("user_id") and current_user() is not None:
        return dashboard_page()
    return render_template(
        "landing.html",
        whatsapp_number=WHATSAPP_NUMBER,
        whatsapp_url=whatsapp_contact_link("أهلاً، عايز تحليل مجاني لصيدليتي"),
        contact_email=CONTACT_EMAIL,
        demo_mode=DEMO_MODE,
    )


def whatsapp_contact_link(text: str) -> str:
    """wa.me link to the company number, or empty when no number is configured."""
    if not WHATSAPP_NUMBER:
        return ""
    from urllib.parse import quote

    return f"https://wa.me/{WHATSAPP_NUMBER}?text={quote(text)}"


PHONE_PATTERN = re.compile(r"^\+?[0-9٠-٩][0-9٠-٩ \-]{6,18}$")


@app.route("/free-analysis", methods=["POST"])
def free_analysis() -> Response:
    form = request.form
    if form.get("website"):  # honeypot: real visitors never see or fill this field
        return redirect(url_for("free_analysis_thanks"))
    name = form.get("name", "").strip()[:80]
    phone = form.get("phone", "").strip()[:25]
    if not name or not PHONE_PATTERN.match(phone):
        flash("اكتب اسمك ورقم موبايل صحيح علشان نقدر نكلمك.", "error")
        return redirect(url_for("index") + "#free-analysis")
    lead = {
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "name": name,
        "pharmacy": form.get("pharmacy", "").strip()[:120],
        "phone": phone,
        "city": form.get("city", "").strip()[:60],
        "branches": max(1, min(parse_int(form.get("branches"), 1), 500)),
        "pos_system": form.get("pos_system", "").strip()[:60],
        "message": form.get("message", "").strip()[:1000],
    }
    with get_db() as db:
        db.execute(
            f"INSERT INTO leads ({', '.join(lead)}) VALUES ({', '.join('?' for _ in lead)})",
            tuple(lead.values()),
        )
    session["lead_name"] = name
    session["lead_pharmacy"] = lead["pharmacy"]
    return redirect(url_for("free_analysis_thanks"))


@app.route("/free-analysis/thanks")
def free_analysis_thanks() -> str:
    name = session.get("lead_name", "")
    pharmacy = session.get("lead_pharmacy", "")
    text = f"أهلاً، أنا {name}" + (f" من {pharmacy}" if pharmacy else "") + ". طلبت تحليل مجاني وهبعتلكم ملفات السيستم."
    return render_template("free_analysis_thanks.html", whatsapp_url=whatsapp_contact_link(text), contact_email=CONTACT_EMAIL)


@app.route("/privacy")
def privacy() -> str:
    return render_template("privacy.html", contact_email=CONTACT_EMAIL, whatsapp_number=WHATSAPP_NUMBER)


@app.route("/demo")
def demo_login() -> Response:
    """One-click read-only demo (only when PHARMACY_DEMO_MODE is on and seed_demo.py created the user)."""
    if not DEMO_MODE:
        abort(404)
    with get_db() as db:
        user = db.execute("SELECT * FROM users WHERE username = ?", (DEMO_USERNAME,)).fetchone()
    if not user or user["role"] != "viewer":
        abort(404)
    session.clear()
    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session["lang"] = "ar"
    return redirect(url_for("index"))


STATUS_LABELS = {"new": "جديد", "contacted": "اتكلمنا", "report_sent": "التقرير اتبعت", "trial": "تجربة", "customer": "عميل", "lost": "مش مهتم"}


@app.route("/leads", methods=["GET", "POST"])
@login_required
@permission_required("admin")
def leads_page() -> str | Response:
    if request.method == "POST":
        status = request.form.get("status", "")
        if status not in STATUS_LABELS:
            abort(400)
        with get_db() as db:
            db.execute("UPDATE leads SET status = ? WHERE id = ?", (status, parse_int(request.form.get("lead_id"), 0)))
        return redirect(url_for("leads_page"))
    with get_db() as db:
        leads = [dict(row) for row in db.execute("SELECT * FROM leads ORDER BY id DESC")]
    for lead in leads:
        number = re.sub(r"\D", "", lead["phone"].translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")))
        if number.startswith("0"):
            number = "2" + number  # Egyptian local format 01xxxxxxxxx -> 201xxxxxxxxx
        lead["wa"] = f"https://wa.me/{number}" if number else ""
    return render_template("leads.html", leads=leads, status_labels=STATUS_LABELS)


@app.route("/free-report")
@login_required
def free_report() -> str:
    """One printable page for the free analysis: the money that leaks, and the items behind it."""
    branch = selected_branch(request.args.get("branch"))
    pl = profit_loss_for(branch)
    top_items = sorted(
        (
            {**item, "driver_ar": driver["title_ar"], "driver_en": driver["title_en"], "kind": driver["kind"]}
            for driver in pl.get("drivers", [])
            if driver["kind"] in {"loss", "frozen"}
            for item in driver["top_items"]
        ),
        key=lambda item: -item["amount"],
    )[:10]
    return render_template(
        "free_report.html",
        pl=pl,
        top_items=top_items,
        plan=action_plan(branch),
        selected_branch=branch,
        branch_options=branch_options(),
        today=date.today().isoformat(),
        contact_email=CONTACT_EMAIL,
        whatsapp_number=WHATSAPP_NUMBER,
        public_url=PUBLIC_URL,
    )


def dashboard_page() -> str:
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


def load_mapping_profiles() -> dict[str, Any]:
    """Confirmed column layouts live in the database; a profiles file from older versions is imported once."""
    with get_db() as db:
        profiles = {row["signature"]: json.loads(row["data"]) for row in db.execute("SELECT signature, data FROM mapping_profiles")}
        if not profiles and MAPPING_PROFILES_PATH.exists():
            profiles = load_profiles(MAPPING_PROFILES_PATH)
            for signature, data in profiles.items():
                db.upsert("mapping_profiles", "signature", {"signature": signature, "data": json.dumps(data, ensure_ascii=False)})
    return profiles


def save_mapping_profile(spec: TableSpec) -> None:
    with get_db() as db:
        db.upsert("mapping_profiles", "signature", {"signature": spec.signature, "data": json.dumps(profile_entry(spec), ensure_ascii=False)})


def save_import_result(token: str, result: dict[str, Any]) -> None:
    with get_db() as db:
        db.upsert("import_runs", "token", {"token": token, "finished_at": result["finished_at"], "result": json.dumps(result, ensure_ascii=False, default=str)})


def load_import_result(token: str) -> dict[str, Any] | None:
    if not TOKEN_PATTERN.fullmatch(token or ""):
        return None
    with get_db() as db:
        row = db.execute("SELECT result FROM import_runs WHERE token = ?", (token,)).fetchone()
    if row:
        return json.loads(row["result"])
    return load_json(IMPORTS_DIR / token / "result.json")  # imports made before results moved to the database


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


@app.route("/upload", methods=["GET", "POST"])
@login_required
@permission_required("upload")
def upload() -> str | Response:
    if request.method == "GET":
        return render_template("upload.html", branch_choices=upload_branch_choices(), extensions=sorted(SUPPORTED_EXTENSIONS))

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
    profiles = load_mapping_profiles()
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


def upload_branch_choices() -> list[str]:
    allowed = allowed_branches()
    return known_branches() if allowed is None else allowed


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
@permission_required("upload")
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
            branch_choices=upload_branch_choices(),
            branch_hint=next((spec["meta"]["branch_hint"] for file in draft["files"] for spec in file["specs"] if spec.get("meta", {}).get("branch_hint")), ""),
        )

    draft["branch"] = clean_branch_label(request.form.get("branch", ""))
    draft["period_days"] = parse_int(request.form.get("period_days"), 0) or None
    allowed = allowed_branches()
    if allowed is not None and not draft["branch"] and len(allowed) == 1:
        draft["branch"] = allowed[0]
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
        result = run_smart_import(
            tables, POS_ANALYSIS_DIR, default_branch=draft["branch"], period_days_override=draft["period_days"], settings=load_analysis_settings()
        )
    except ValueError as exc:
        if str(exc) == "period_required":
            flash("الملفات فيها مبيعات بس مش واضح هي عن كام يوم. اكتب مدة المبيعات بالأيام. / Enter how many days the sales cover.", "error")
        else:
            flash(f"مقدرتش أحلل الملفات: {exc} / Could not analyze the files.", "error")
        return redirect(url_for("upload_review", token=token))

    if allowed is not None:
        outside = sorted({branch_result.branch for branch_result in result.branches} - set(allowed))
        if outside:
            flash(f"مسموح لك ترفع لفروعك بس. الملف فيه: {', '.join(outside)}. / You can only upload your own branches.", "error")
            return redirect(url_for("upload_review", token=token))
    for _path, spec in tables:
        save_mapping_profile(spec)
    for branch_result in result.branches:
        import_pos_analysis_to_db(branch_result.output_dir, import_token=token)

    save_import_result(
        token,
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
    result = load_import_result(token)
    if not result:
        return redirect(url_for("uploads"))
    allowed = allowed_branches()
    if allowed is not None and not {branch["branch"] for branch in result["branches"]} <= set(allowed):
        abort(403)
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
        uploads_rows = db.execute(
            f"SELECT analysis_period_days, coverage FROM uploads WHERE id IN (SELECT DISTINCT upload_id FROM items {where})", params
        ).fetchall()
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
    result = analyze_profit_loss(items, period_days=period, coverage=coverage, settings=load_analysis_settings())
    result["has_data"] = bool(items)
    return result


def action_plan(branch: str | None = None) -> dict[str, Any]:
    """Transfers are planned across all branches; the page then shows the selected branch's part."""
    settings = load_analysis_settings()
    with get_db() as db:
        rows = [dict(row) for row in db.execute("SELECT * FROM items")]
    transfers = action_plans.plan_transfers(rows, settings)
    orders = action_plans.purchase_orders(rows, transfers)
    branch_rows = [row for row in rows if not branch or row["branch"] == branch]
    if branch:
        transfers = [t for t in transfers if branch in (t.from_branch, t.to_branch)]
        orders = [order for order in orders if order.branch == branch]
    moved = {(t.from_branch, t.from_sku) for t in transfers if t.reason == "expiry"}
    expiring = action_plans.expiry_actions(branch_rows, moved)
    fixes = action_plans.price_fixes(branch_rows)
    return {
        "transfers": transfers,
        "orders": orders,
        "expiring": expiring,
        "fixes": fixes,
        "stop_list": action_plans.stop_buying(branch_rows),
        "branch_count": len({row["branch"] for row in rows}),
        "digest": action_plans.digest_text(branch, transfers, orders, expiring, fixes, current_language()) + digest_footer(),
    }


def digest_footer() -> str:
    """A small signature on shared summaries: every forwarded summary tells people where it came from."""
    ar = current_language() == "ar"
    line = "اتعمل بمنصة الصيدليات" if ar else "Made with the Pharmacy Platform"
    return f"\n\n— {line}" + (f" {PUBLIC_URL}" if PUBLIC_URL else "")


@app.route("/actions")
@login_required
def action_center() -> str:
    branch = selected_branch(request.args.get("branch"))
    plan = action_plan(branch)
    return render_template(
        "actions.html",
        **plan,
        whatsapp=action_plans.whatsapp_link(plan["digest"]),
        selected_branch=branch,
        branch_options=branch_options(),
        branch_args=branch_query_args(branch),
    )


@app.route("/purchase-orders.xlsx")
@login_required
def purchase_orders_export() -> Response:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    branch = selected_branch(request.args.get("branch"))
    supplier = request.args.get("supplier")
    orders = [order for order in action_plan(branch)["orders"] if supplier is None or order.supplier == supplier]
    ar = current_language() == "ar"
    workbook = Workbook()
    workbook.remove(workbook.active)
    headers = (["الكود", "الباركود", "الصنف", "الرصيد", "بيع/يوم", "الكمية المطلوبة", "تكلفة الوحدة", "الإجمالي", "الأولوية", "متغطي بتحويل", "ABC"] if ar
               else ["Code", "Barcode", "Item", "Stock", "Units/day", "Order qty", "Unit cost", "Total", "Priority", "Covered by transfer", "ABC"])
    priority_names = {"urgent": "عاجل", "soon": "خلال أسبوعين", "planned": "عادي"} if ar else {"urgent": "Urgent", "soon": "Within 2 weeks", "planned": "Planned"}
    used_titles: set[str] = set()
    for order in orders:
        title = re.sub(r"[\\/*?:\[\]]", " ", f"{order.branch} - {order.supplier or ('بدون مورد' if ar else 'No supplier')}")[:28].strip() or "Order"
        while title in used_titles:
            title = title[:26] + str(len(used_titles))
        used_titles.add(title)
        sheet = workbook.create_sheet(title)
        sheet.sheet_view.rightToLeft = ar
        sheet.append(headers)
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        for line in order.lines:
            sheet.append([line.sku, line.barcode, line.name, line.stock, round(line.daily, 2), line.qty, round(line.unit_cost, 2),
                          round(line.value, 2), priority_names[line.priority], line.covered_by_transfer, line.abc_class])
        sheet.append([])
        sheet.append(["", "", "الإجمالي" if ar else "Total", "", "", sum(line.qty for line in order.lines), "", round(order.value, 2)])
        sheet.column_dimensions["C"].width = 34
    if not orders:
        workbook.create_sheet("Empty").append(["لا توجد أصناف مطلوبة" if ar else "Nothing to order"])
    output = io.BytesIO()
    workbook.save(output)
    output.seek(0)
    name = f"purchase_orders_{branch_slug(branch) if branch else 'all'}_{date.today().isoformat()}.xlsx"
    return send_file(output, as_attachment=True, download_name=name, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/abc")
@login_required
def abc_analysis() -> str:
    branch = selected_branch(request.args.get("branch"))
    where = "WHERE branch = ?" if branch else ""
    with get_db() as db:
        rows = [dict(row) for row in db.execute(f"SELECT * FROM items {where}", [branch] if branch else [])]
    at_risk = sorted(
        (row for row in rows if row["abc_class"] == "A" and ("stockout_with_sales" in row["tags"] or row["stockout_probability_14d"] >= 0.5)),
        key=lambda row: -row["stockout_probability_14d"],
    )[:15]
    frozen_c = sorted((row for row in rows if row["abc_class"] in ("C", "") and row["frozen_capital_value"] > 0), key=lambda row: -row["frozen_capital_value"])[:15]
    return render_template(
        "abc.html",
        matrix=action_plans.abc_matrix(rows),
        at_risk=at_risk,
        frozen_c=frozen_c,
        has_classes=any(row["abc_class"] for row in rows),
        has_xyz=any(row["xyz_class"] for row in rows),
        settings=load_analysis_settings(),
        selected_branch=branch,
        branch_options=branch_options(),
        branch_args=branch_query_args(branch),
    )


def saved_ai_key() -> str:
    with get_db() as db:
        row = db.execute("SELECT value FROM app_settings WHERE key = 'anthropic_api_key'").fetchone()
    return row["value"] if row else ""


def ai_key_source() -> str:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "env"
    return "saved" if saved_ai_key() else ""


def claude_client() -> Any | None:
    """Only when a key is configured; the built-in engine answers otherwise."""
    source = ai_key_source()
    if not source:
        return None
    try:
        import anthropic
    except ImportError:
        return None
    return anthropic.Anthropic(api_key=saved_ai_key() if source == "saved" else None, max_retries=1, timeout=60.0)


def visible_item_rows() -> list[dict[str, Any]]:
    allowed = allowed_branches()
    with get_db() as db:
        rows = [dict(row) for row in db.execute("SELECT * FROM items")]
    return rows if allowed is None else [row for row in rows if row["branch"] in allowed]


@app.route("/ask")
@login_required
def ask_page() -> str:
    question = request.args.get("q", "").strip()
    lang = current_language()
    rows = visible_item_rows()
    answer = data_questions.ask(question, rows, lang, claude_client()) if question else None
    branch = sorted({row["branch"] for row in rows})[0] if rows else ""
    examples = [example.format(branch=branch) for example in (data_questions.EXAMPLES_AR if lang == "ar" else data_questions.EXAMPLES_EN)]
    return render_template("ask.html", question=question, answer=answer, examples=examples, ai_enabled=bool(ai_key_source()))


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
        "all": ("All Items", "كل الأصناف"),
        "dead": ("Dead Stock", "مخزون راكد"),
        "reorder": ("Stockout With Sales", "اشتري أو حوّل دلوقتي"),
        "expiry": ("Expiry Risk", "خطر الصلاحية"),
        "loss": ("Selling Below Cost", "بيع بأقل من التكلفة"),
        "overstock": ("Overstock Risk", "زيادة مخزون"),
        "slow": ("Slow Moving", "أصناف بطيئة"),
        "fast": ("Fast Moving", "أصناف سريعة"),
        "risk": ("High Risk Score", "خطورة عالية"),
    }
    if category not in labels:
        return redirect(url_for("index"))
    branch = selected_branch(request.args.get("branch"))
    label = labels[category][1 if current_language() == "ar" else 0]
    title = label if not branch else f"{label} - {branch}"
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
    # The branch comparison shows every branch to owners, and only the selected branch to restricted users.
    compare_where, compare_params = ("WHERE branch = ?", [branch]) if allowed_branches() is not None else ("", [])
    with get_db() as db:
        branch_rows = db.execute(
            f"""
            SELECT
                branch,
                COUNT(*) AS items,
                SUM(lost_sales_value) AS lost_sales_value,
                SUM(frozen_capital_value) AS frozen_capital_value,
                SUM(CASE WHEN tags LIKE '%overstock_risk%' THEN stock_value ELSE 0 END) AS overstock_value,
                SUM(CASE WHEN tags LIKE '%dead_stock%' THEN stock_value ELSE 0 END) AS dead_value,
                SUM(CASE WHEN risk_score >= 8 THEN 1 ELSE 0 END) AS high_risk_count
            FROM items
            {compare_where}
            GROUP BY branch
            ORDER BY lost_sales_value DESC, frozen_capital_value DESC
            """,
            compare_params,
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


def snapshot_rows(branch: str | None = None) -> list[sqlite3.Row]:
    where = "WHERE branch = ?" if branch else ""
    with get_db() as db:
        return db.execute(f"SELECT * FROM snapshots {where} ORDER BY branch, period_end, id", [branch] if branch else []).fetchall()


def financial_history(branch: str | None = None) -> list[dict[str, Any]]:
    rows = sorted(snapshot_rows(branch), key=lambda row: (row["period_end"], row["id"]))
    return [
        {
            "label": row["period_end"][5:],
            "branch": row["branch"],
            "lostSales": round(float(row["lost_sales_value"] or 0), 2),
            "frozenCapital": round(float(row["frozen_capital_value"] or 0), 2),
            "highRisk": int(row["high_risk_count"] or 0),
            "exposure": round(float(row["lost_sales_value"] or 0) + float(row["frozen_capital_value"] or 0), 2),
        }
        for row in rows
    ][-18:]


TREND_METRICS = [
    # snapshot column, arabic, english, which direction is good
    ("net_sales", "المبيعات", "Sales", "up"),
    ("gross_profit", "مجمل الربح", "Gross profit", "up"),
    ("margin_pct", "الهامش %", "Margin %", "up"),
    ("total_loss", "الخسائر", "Losses", "down"),
    ("total_frozen", "فلوس متجمدة", "Frozen cash", "down"),
    ("expiry_risk_value", "خطر الصلاحية", "Expiry risk", "down"),
    ("stockout_count", "نواقص عليها طلب", "Stockouts with demand", "down"),
    ("high_risk_count", "خطورة عالية", "High-risk items", "down"),
]


def compare_values(current: float, previous: float | None, good: str) -> dict[str, Any]:
    if previous is None:
        return {"current": current, "previous": None, "delta": 0.0, "percent": None, "state": "new"}
    delta = current - previous
    percent = (delta / abs(previous) * 100) if previous else None
    if abs(delta) < 1e-9 or (percent is not None and abs(percent) < 1):
        state = "flat"
    else:
        state = "better" if (delta > 0) == (good == "up") else "worse"
    return {"current": current, "previous": previous, "delta": delta, "percent": percent, "state": state}


def compare_snapshots(current: sqlite3.Row, previous: sqlite3.Row | None) -> dict[str, dict[str, Any]]:
    return {
        key: compare_values(float(current[key] or 0), float(previous[key] or 0) if previous else None, good)
        for key, _ar, _en, good in TREND_METRICS
    }


def trends_overview(branch: str | None = None) -> list[dict[str, Any]]:
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in snapshot_rows(branch):
        grouped.setdefault(row["branch"], []).append(row)
    overview = []
    for name, rows in sorted(grouped.items()):
        current, previous = rows[-1], rows[-2] if len(rows) > 1 else None
        overview.append({"branch": name, "count": len(rows), "current": current, "previous": previous, "metrics": compare_snapshots(current, previous)})
    return overview


def snapshot_movers(branch: str, limit: int = 8) -> dict[str, list[dict[str, Any]]] | None:
    """What changed between a branch's last two uploads, item by item."""
    rows = snapshot_rows(branch)
    if len(rows) < 2:
        return None
    current_id, previous_id = rows[-1]["id"], rows[-2]["id"]
    with get_db() as db:
        current = {row["sku"]: row for row in db.execute("SELECT * FROM item_history WHERE snapshot_id = ?", (current_id,))}
        previous = {row["sku"]: row for row in db.execute("SELECT * FROM item_history WHERE snapshot_id = ?", (previous_id,))}
    changes = []
    for sku, now in current.items():
        before = previous.get(sku)
        if before is None:
            continue
        rate_now, rate_before = float(now["avg_daily_sales"] or 0), float(before["avg_daily_sales"] or 0)
        changes.append(
            {
                "sku": sku,
                "name": now["name"],
                "branch": now["branch"],
                "loss_now": float(now["loss_value"] or 0),
                "loss_before": float(before["loss_value"] or 0),
                "loss_delta": float(now["loss_value"] or 0) - float(before["loss_value"] or 0),
                "rate_now": rate_now,
                "rate_before": rate_before,
                "rate_change": (rate_now - rate_before) / rate_before * 100 if rate_before > 0 else None,
                "stockout_now": "stockout_with_sales" in now["tags"],
                "stockout_before": "stockout_with_sales" in before["tags"],
                "dead_now": "dead_stock" in now["tags"],
                "dead_before": "dead_stock" in before["tags"],
            }
        )
    busy = [change for change in changes if max(change["rate_now"], change["rate_before"]) >= 0.2]
    return {
        "worse": sorted((c for c in changes if c["loss_delta"] > 0), key=lambda c: -c["loss_delta"])[:limit],
        "better": sorted((c for c in changes if c["loss_delta"] < 0), key=lambda c: c["loss_delta"])[:limit],
        "new_stockouts": [c for c in changes if c["stockout_now"] and not c["stockout_before"]][:limit],
        "recovered": [c for c in changes if c["stockout_before"] and not c["stockout_now"]][:limit],
        "new_dead": [c for c in changes if c["dead_now"] and not c["dead_before"]][:limit],
        "sales_up": sorted((c for c in busy if (c["rate_change"] or 0) >= 20), key=lambda c: -(c["rate_change"] or 0))[:limit],
        "sales_down": sorted((c for c in busy if c["rate_change"] is not None and c["rate_change"] <= -20 and not c["dead_now"]), key=lambda c: c["rate_change"])[:limit],
        "new_items": len(set(current) - set(previous)),
        "gone_items": len(set(previous) - set(current)),
    }


def product_history(sku: str) -> list[sqlite3.Row]:
    with get_db() as db:
        return db.execute(
            """
            SELECT h.*, s.period_start, s.period_end, s.period_days
            FROM item_history h JOIN snapshots s ON s.id = h.snapshot_id
            WHERE h.sku = ?
            ORDER BY s.period_end, s.id
            """,
            (sku,),
        ).fetchall()


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
    allowed = allowed_branches()
    if allowed is not None:
        comparison_rows = [row for row in comparison_rows if row["branch"] in allowed]
        history = [row for row in history if row["branch"] in allowed]
    product_name = ""
    if current_rows:
        product_name = current_rows[0]["name"]
    elif history:
        product_name = history[-1]["name"]
    else:
        product_name = sku

    max_history_sales = max(max([row["avg_daily_sales"] for row in history] or [1]), 0.0001)
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
def weekly_comparison() -> Response:
    return redirect(url_for("trends", **request.args))


@app.route("/trends")
@login_required
def trends() -> str:
    branch = selected_branch(request.args.get("branch"))
    overview = trends_overview(branch)
    timeline = []
    if branch:
        rows = snapshot_rows(branch)
        timeline = [
            {"row": row, "metrics": compare_snapshots(row, rows[index - 1] if index else None)}
            for index, row in enumerate(rows)
        ][::-1]
    return render_template(
        "trends.html",
        overview=overview,
        timeline=timeline,
        movers=snapshot_movers(branch) if branch else None,
        metrics=TREND_METRICS,
        max_sales=max([float(item["row"]["net_sales"] or 0) for item in timeline] or [1]) or 1,
        selected_branch=branch,
        branch_options=branch_options(),
        branch_args=branch_query_args(branch),
    )


def settings_from_form(form: Any) -> AnalysisSettings:
    # A checked box sends both the hidden "0" and "1"; the last value wins.
    data: dict[str, Any] = {spec.key: (form.getlist(spec.key) or [""])[-1] for spec in SETTING_FIELDS}
    data = {key: value for key, value in data.items() if value != ""}
    data["business_type"] = form.get("business_type", "pharmacy")
    data["lead_time_overrides"] = [
        {"kind": kind, "name": name, "days": days}
        for kind, name, days in zip(form.getlist("override_kind"), form.getlist("override_name"), form.getlist("override_days"))
    ]
    return AnalysisSettings.from_dict(data)


def latest_import_tokens() -> list[str]:
    """Imports behind each branch's current data (legacy imports have no token and cannot be re-run)."""
    with get_db() as db:
        rows = db.execute("SELECT DISTINCT u.import_token FROM uploads u JOIN items i ON i.upload_id = u.id WHERE u.import_token != ''").fetchall()
    return [row["import_token"] for row in rows]


def reanalyze_import(token: str, settings: AnalysisSettings) -> int:
    """Re-run a past import from its archived files and reviewed column mapping."""
    folder = import_dir(token)
    draft = load_json(folder / "draft.json" if folder else None)
    result = load_import_result(token)
    if not folder or not draft or not result:
        return 0
    used = {(table["file"], table["sheet"]) for table in result.get("tables", [])}
    tables = [
        (folder / "files" / file["stored"], spec)
        for file in draft["files"]
        for spec in (TableSpec.from_dict(data) for data in file["specs"])
        if (spec.file_name, spec.sheet) in used and spec.usable
    ]
    if not tables:
        return 0
    rerun = run_smart_import(tables, POS_ANALYSIS_DIR, default_branch=draft["branch"], period_days_override=draft["period_days"], settings=settings)
    for branch_result in rerun.branches:
        import_pos_analysis_to_db(branch_result.output_dir, import_token=token)
    return len(rerun.branches)


@app.route("/settings", methods=["GET", "POST"])
@login_required
@permission_required("admin")
def analysis_settings() -> str | Response:
    if request.method == "POST":
        action = request.form.get("action", "save")
        if action == "save_ai_key":
            key = request.form.get("anthropic_api_key", "").strip()
            if key:
                with get_db() as db:
                    db.upsert("app_settings", "key", {"key": "anthropic_api_key", "value": key})
                flash("اتحفظ مفتاح Claude. / Claude key saved.", "success")
        elif action == "remove_ai_key":
            with get_db() as db:
                db.execute("DELETE FROM app_settings WHERE key = 'anthropic_api_key'")
            flash("اتشال مفتاح Claude. / Claude key removed.", "success")
        elif action == "reset":
            save_analysis_settings(AnalysisSettings().with_profile(load_analysis_settings().business_type))
            flash("رجعت القيم المقترحة لنوع النشاط. / Suggested values restored.", "success")
        elif action == "apply_profile":
            chosen = request.form.get("business_type", "pharmacy")
            save_analysis_settings(load_analysis_settings().with_profile(chosen))
            flash("اتطبقت القيم المقترحة لنوع النشاط. اضغط أعد التحليل علشان تتطبق على البيانات الحالية. / Profile applied; re-analyze to update current data.", "success")
        elif action == "reanalyze":
            settings = load_analysis_settings()
            count = sum(reanalyze_import(token, settings) for token in latest_import_tokens())
            flash(f"اتعاد تحليل {count} فرع بالإعدادات الحالية. / Re-analyzed {count} branch(es).", "success")
        else:
            save_analysis_settings(settings_from_form(request.form))
            flash("اتحفظت الإعدادات وهتتطبق على أي رفع جديد. / Settings saved for new uploads.", "success")
        return redirect(url_for("analysis_settings"))

    with get_db() as db:
        suppliers = [row[0] for row in db.execute("SELECT DISTINCT supplier FROM items WHERE supplier != '' ORDER BY supplier LIMIT 300")]
        categories = [row[0] for row in db.execute("SELECT DISTINCT category FROM items ORDER BY category LIMIT 300")]
    settings = load_analysis_settings()
    return render_template(
        "settings.html",
        settings=settings,
        defaults=AnalysisSettings(),
        fields=SETTING_FIELDS,
        groups=SETTING_GROUPS,
        suppliers=suppliers,
        categories=categories,
        reanalyze_count=len(latest_import_tokens()),
        ai_key_source=ai_key_source(),
        profiles=BUSINESS_PROFILES,
        database_label=database.describe(DATABASE_URL),
    )


def owner_count(db: database.Connection) -> int:
    return db.execute("SELECT COUNT(*) FROM users WHERE role = 'owner'").fetchone()[0]


@app.route("/users", methods=["GET", "POST"])
@login_required
@permission_required("admin")
def users_page() -> str | Response:
    if request.method == "POST":
        action = request.form.get("action", "")
        role = request.form.get("role", "viewer")
        branches = json.dumps(sorted({clean_branch_label(branch) for branch in request.form.getlist("branches") if clean_branch_label(branch)}), ensure_ascii=False)
        if role not in ROLES:
            abort(400)
        with get_db() as db:
            if action == "create":
                username = request.form.get("username", "").strip()
                password = request.form.get("password", "")
                if not re.fullmatch(r"[\w.@-]{3,40}", username) or len(password) < 8:
                    flash("اسم المستخدم 3 حروف على الأقل (من غير مسافات)، وكلمة المرور 8 على الأقل. / Username 3+ chars, password 8+.", "error")
                elif db.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
                    flash("اسم المستخدم ده موجود. / Username already exists.", "error")
                else:
                    db.execute(
                        "INSERT INTO users (username, password_hash, created_at, role, branches, must_change_password) VALUES (?, ?, ?, ?, ?, 1)",
                        (username, generate_password_hash(password), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), role, branches),
                    )
                    flash(f"اتعمل حساب {username}. / Created {username}.", "success")
                return redirect(url_for("users_page"))
            user_id = parse_int(request.form.get("user_id"), 0)
            target = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if not target:
                abort(404)
            losing_owner = target["role"] == "owner" and (action == "delete" or role != "owner")
            if losing_owner and owner_count(db) <= 1:
                flash("لازم يفضل مالك واحد على الأقل. / At least one owner must remain.", "error")
            elif action == "update":
                db.execute("UPDATE users SET role = ?, branches = ? WHERE id = ?", (role, branches, user_id))
                flash(f"اتحدثت صلاحيات {target['username']}. / Updated {target['username']}.", "success")
            elif action == "reset_password":
                password = request.form.get("password", "")
                if len(password) < 8:
                    flash("كلمة المرور 8 حروف على الأقل. / Password must be 8+ characters.", "error")
                else:
                    db.execute("UPDATE users SET password_hash = ?, must_change_password = 1 WHERE id = ?", (generate_password_hash(password), user_id))
                    flash(f"اتغيرت كلمة مرور {target['username']}. / Password reset.", "success")
            elif action == "delete":
                if target["id"] == session.get("user_id"):
                    flash("مينفعش تمسح حسابك. / You cannot delete yourself.", "error")
                else:
                    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
                    flash(f"اتمسح {target['username']}. / Deleted {target['username']}.", "success")
        return redirect(url_for("users_page"))

    with get_db() as db:
        users = [dict(row) for row in db.execute("SELECT id, username, role, branches, created_at FROM users ORDER BY id")]
    for entry in users:
        entry["branch_list"] = user_branches(entry)
    return render_template("users.html", users=users, branch_choices=known_branches())


@app.route("/uploads")
@login_required
def uploads() -> str:
    with get_db() as db:
        rows = db.execute("SELECT * FROM uploads ORDER BY id DESC").fetchall()
    allowed = allowed_branches()
    if allowed is not None:
        rows = [row for row in rows if row["branch"] in allowed]
    return render_template("uploads.html", rows=rows)


@app.route("/data-management", methods=["GET", "POST"])
@login_required
@permission_required("admin")
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
    if app.config.get("ENFORCE_PASSWORD_CHANGE") and (user["must_change_password"] or check_password_hash(user["password_hash"], KNOWN_DEFAULT_PASSWORD)):
        session["must_change_password"] = True
        flash("أول مرة تدخل، أو كلمة المرور افتراضية: اختار كلمة مرور جديدة علشان تكمل. / Choose a new password to continue.", "error")
        return redirect(url_for("change_password"))
    return redirect(safe_next(request.args.get("next")) or url_for("index"))


def safe_next(target: str | None) -> str | None:
    """Only follow local paths after login (no open redirect to another site)."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return None


@app.route("/logout", methods=["POST"])
def logout() -> Response:
    lang = current_language()
    session.clear()
    session["lang"] = lang
    if request.form.get("next") == "free-analysis":  # leaving the demo to request a real analysis
        return redirect(url_for("index") + "#free-analysis")
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
    if new_password in {KNOWN_DEFAULT_PASSWORD, current_password}:
        flash("اختار كلمة مرور جديدة مختلفة عن الحالية وعن الافتراضية.", "error")
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
            "UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?",
            (generate_password_hash(new_password), session["user_id"]),
        )
    session.pop("must_change_password", None)

    flash("تم تغيير كلمة المرور بنجاح.", "success")
    return redirect(url_for("index"))


init_db()
backfill_snapshots()


if __name__ == "__main__":
    app.run(debug=False, use_reloader=False)
