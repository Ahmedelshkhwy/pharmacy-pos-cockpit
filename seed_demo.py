from __future__ import annotations

from datetime import date, timedelta

from app import ParsedRow, classify_rows, get_db, save_upload


BRANCHES = [
    "فرع النزهة",
    "فرع العليا",
    "فرع الروضة",
    "فرع الحمراء",
    "فرع المروج",
]

PRODUCTS = [
    ("Panadol Extra 24 Tabs", "RX-1001", 0, 85, 12.5, 18),
    ("Augmentin 1g Tablets", "RX-1002", 6, 44, 72.0, 36),
    ("Vitamin D3 50000 IU", "RX-1003", 2, 120, 38.0, 80),
    ("Cough Syrup 120ml", "RX-1004", 0, 63, 21.0, 12),
    ("Cetirizine 10mg", "RX-1005", 76, 38, 9.5, 210),
    ("Omeprazole 20mg", "RX-1006", 54, 31, 18.0, 180),
    ("Baby Saline Drops", "RX-1007", 1, 42, 14.0, 24),
    ("Insulin Pen Needles", "RX-1008", 118, 62, 45.0, 270),
    ("Antacid Suspension", "RX-1009", 4, 95, 16.5, 60),
    ("Skin Repair Cream", "RX-1010", 0, 33, 29.0, 8),
    ("Omega 3 Capsules", "RX-1011", 3, 140, 64.0, 130),
    ("Digital Thermometer", "RX-1012", 28, 19, 55.0, 365),
]


def shifted_rows(branch_index: int) -> list[ParsedRow]:
    today = date.today()
    rows: list[ParsedRow] = []
    for item_index, (name, sku, sold, stock, price, days) in enumerate(PRODUCTS):
        sold_adjustment = (branch_index + item_index) % 4
        stock_adjustment = branch_index * 7 + item_index * 2
        rows.append(
            ParsedRow(
                name=name,
                sku=f"{sku}-{branch_index + 1}",
                sold=max(0, sold + sold_adjustment - (2 if item_index in {0, 3, 9} else 0)),
                stock=stock + stock_adjustment,
                price=price,
                expiry=today + timedelta(days=max(5, days - branch_index * 6)),
            )
        )
    return rows


def reset_demo_data() -> None:
    with get_db() as db:
        db.execute("DELETE FROM items")
        db.execute("DELETE FROM uploads")
        db.execute("DELETE FROM sqlite_sequence WHERE name IN ('items', 'uploads')")


def seed_demo() -> None:
    reset_demo_data()
    week_date = date.today().isoformat()
    for branch_index, branch in enumerate(BRANCHES):
        rows = classify_rows(shifted_rows(branch_index))
        save_upload(
            filename=f"{branch.replace(' ', '_')}_{week_date}.xlsx",
            branch=branch,
            week_date=week_date,
            rows=rows,
        )


if __name__ == "__main__":
    seed_demo()
    with get_db() as db:
        uploads = db.execute("SELECT COUNT(*) FROM uploads").fetchone()[0]
        items = db.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    print(f"Demo data ready: {uploads} uploads, {items} items.")
