"""Generate realistic, deliberately messy pharmacy exports for testing the smart importer.

Each output mimics a different POS style so the importer is exercised on the things
real exports do: intro/title rows, Arabic headers, Arabic-Indic digits, currency text,
totals rows, repeated headers, duplicate rows, batch-level stock, transaction-level
sales with returns, multiple branches in one file, and an extra junk sheet.

Run:  python sample_data/generate_samples.py
"""
from __future__ import annotations

import csv
import math
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from openpyxl import Workbook

OUT_DIR = Path(__file__).resolve().parent
TODAY = date(2026, 10, 31)
PERIOD_DAYS = 90
PERIOD_END = TODAY - timedelta(days=1)
PERIOD_START = PERIOD_END - timedelta(days=PERIOD_DAYS - 1)

# name, category, unit cost (EGP), margin rate
CATALOG = [
    ("Panadol Extra 24 Tab", "Analgesics", 38.0, 0.22),
    ("Panadol Advance 24 Tab", "Analgesics", 34.0, 0.22),
    ("Brufen 400mg 30 Tab", "Analgesics", 41.0, 0.20),
    ("Cataflam 50mg 20 Tab", "Analgesics", 52.0, 0.18),
    ("Voltaren 75mg Amp", "Analgesics", 28.0, 0.25),
    ("Ketofan 50mg Caps", "Analgesics", 33.0, 0.21),
    ("Augmentin 1g 14 Tab", "Antibiotics", 118.0, 0.15),
    ("Augmentin 625mg 14 Tab", "Antibiotics", 92.0, 0.15),
    ("Zithromax 500mg 3 Tab", "Antibiotics", 105.0, 0.17),
    ("Flagyl 500mg 20 Tab", "Antibiotics", 24.0, 0.25),
    ("Ciprobay 500mg 10 Tab", "Antibiotics", 76.0, 0.16),
    ("Klacid 500mg 14 Tab", "Antibiotics", 165.0, 0.14),
    ("Hibiotic 1g 16 Tab", "Antibiotics", 88.0, 0.16),
    ("Concor 5mg 30 Tab", "Cardiovascular", 64.0, 0.18),
    ("Concor 10mg 30 Tab", "Cardiovascular", 79.0, 0.18),
    ("Norvasc 5mg 30 Tab", "Cardiovascular", 97.0, 0.16),
    ("Lipitor 20mg 30 Tab", "Cardiovascular", 185.0, 0.13),
    ("Crestor 10mg 28 Tab", "Cardiovascular", 210.0, 0.12),
    ("Aspocid 75mg 30 Tab", "Cardiovascular", 9.0, 0.30),
    ("Plavix 75mg 28 Tab", "Cardiovascular", 245.0, 0.11),
    ("Glucophage 1000mg 30 Tab", "Diabetes", 46.0, 0.20),
    ("Glucophage XR 750mg", "Diabetes", 58.0, 0.19),
    ("Amaryl 2mg 30 Tab", "Diabetes", 71.0, 0.18),
    ("Januvia 100mg 28 Tab", "Diabetes", 390.0, 0.10),
    ("Galvus Met 50/1000", "Diabetes", 310.0, 0.11),
    ("Lantus SoloStar Pen", "Diabetes", 520.0, 0.08),
    ("Mixtard 30 Penfill", "Diabetes", 165.0, 0.10),
    ("Nexium 40mg 14 Tab", "Gastro", 138.0, 0.16),
    ("Controloc 40mg 14 Tab", "Gastro", 112.0, 0.17),
    ("Antinal 200mg 24 Caps", "Gastro", 31.0, 0.24),
    ("Motilium 10mg 30 Tab", "Gastro", 39.0, 0.22),
    ("Gaviscon Liquid 200ml", "Gastro", 72.0, 0.20),
    ("Buscopan 10mg 20 Tab", "Gastro", 36.0, 0.22),
    ("Colona 30 Tab", "Gastro", 44.0, 0.21),
    ("Claritine 10mg 10 Tab", "Allergy", 49.0, 0.21),
    ("Zyrtec 10mg 20 Tab", "Allergy", 58.0, 0.20),
    ("Telfast 180mg 20 Tab", "Allergy", 86.0, 0.18),
    ("Aerius 5mg 10 Tab", "Allergy", 63.0, 0.19),
    ("Ventolin Inhaler", "Respiratory", 47.0, 0.20),
    ("Symbicort 160 Turbuhaler", "Respiratory", 420.0, 0.09),
    ("Singulair 10mg 14 Tab", "Respiratory", 155.0, 0.14),
    ("Mucosolvan Syrup 100ml", "Respiratory", 29.0, 0.25),
    ("Tussivan-N Syrup", "Respiratory", 26.0, 0.25),
    ("Otrivin Adult Spray", "Respiratory", 35.0, 0.23),
    ("C-Retard 500mg 20 Caps", "Vitamins", 39.0, 0.26),
    ("Centrum Adults 30 Tab", "Vitamins", 240.0, 0.18),
    ("Neurobion 30 Tab", "Vitamins", 48.0, 0.24),
    ("Ossofortin Forte 30", "Vitamins", 95.0, 0.20),
    ("Vidrop 15ml", "Vitamins", 44.0, 0.23),
    ("Feroglobin B12 30 Caps", "Vitamins", 135.0, 0.19),
    ("Omega 3 Plus 30 Caps", "Vitamins", 160.0, 0.21),
    ("Bepanthen Cream 30g", "Dermatology", 68.0, 0.22),
    ("Fucidin Cream 15g", "Dermatology", 59.0, 0.21),
    ("Betaderm Cream", "Dermatology", 22.0, 0.27),
    ("Daktarin Cream 20g", "Dermatology", 33.0, 0.24),
    ("La Roche Effaclar Duo", "Cosmetics", 650.0, 0.18),
    ("Vichy Mineral 89 50ml", "Cosmetics", 980.0, 0.16),
    ("Bioderma Sensibio H2O", "Cosmetics", 540.0, 0.17),
    ("Eucerin Sun SPF50", "Cosmetics", 720.0, 0.17),
    ("Sensodyne Repair 75ml", "Personal Care", 95.0, 0.20),
    ("Listerine 500ml", "Personal Care", 120.0, 0.19),
    ("Pampers Premium Size 4", "Baby Care", 410.0, 0.07),
    ("Bebelac 1 400g", "Baby Care", 215.0, 0.06),
    ("Similac Gold 2 400g", "Baby Care", 260.0, 0.06),
    ("Sudocrem 125g", "Baby Care", 145.0, 0.20),
    ("Strepsils Honey 24", "OTC", 52.0, 0.23),
    ("Congestal 20 Tab", "OTC", 23.0, 0.26),
    ("Comtrex 20 Tab", "OTC", 27.0, 0.25),
    ("Cold Free 20 Tab", "OTC", 21.0, 0.26),
    ("Euthyrox 100mcg 50 Tab", "Hormones", 29.0, 0.24),
    ("Duphaston 10mg 20 Tab", "Hormones", 175.0, 0.14),
    ("Cialis 20mg 4 Tab", "Urology", 360.0, 0.15),
    ("Prostamol Uno 30 Caps", "Urology", 230.0, 0.16),
    ("Xalatan Eye Drops", "Ophthalmic", 310.0, 0.12),
    ("Tobradex Eye Drops", "Ophthalmic", 64.0, 0.20),
    ("Refresh Tears 15ml", "Ophthalmic", 98.0, 0.19),
    ("Accu-Chek Strips 50", "Devices", 380.0, 0.12),
    ("Omron M2 BP Monitor", "Devices", 1650.0, 0.10),
    ("Digital Thermometer", "Devices", 85.0, 0.30),
    ("Face Masks Box 50", "Devices", 60.0, 0.35),
]

# demand class -> (share of catalogue, daily units range)
DEMAND_CLASSES = [
    ("fast", 0.15, (2.0, 6.0)),
    ("normal", 0.40, (0.4, 2.0)),
    ("slow", 0.25, (0.03, 0.3)),
    ("dead", 0.20, (0.0, 0.0)),
]

ARABIC_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")


SUPPLIER_BY_CATEGORY = {
    "Analgesics": "GSK Distribution", "Antibiotics": "GSK Distribution", "Allergy": "GSK Distribution",
    "Cardiovascular": "Pharma Overseas", "Diabetes": "Pharma Overseas", "Hormones": "Pharma Overseas", "Urology": "Pharma Overseas",
    "Gastro": "Ibnsina Pharma", "Respiratory": "Ibnsina Pharma", "OTC": "Ibnsina Pharma", "Ophthalmic": "Ibnsina Pharma",
    "Vitamins": "United Company", "Dermatology": "United Company", "Personal Care": "United Company",
    "Cosmetics": "Beauty Hub", "Baby Care": "Mother & Baby Co", "Devices": "MedTech Supplies",
}


@dataclass
class Item:
    code: str
    barcode: str
    name: str
    category: str
    cost: float
    price: float
    demand: str
    daily_rate: float
    sold_qty: int = 0
    sales_value: float = 0.0
    batches: list[tuple[date, int]] = field(default_factory=list)
    daily_sales: list[int] = field(default_factory=list)  # units per day of the period
    unavailable_days: int = 0                              # days it could not be sold (stockout)

    @property
    def supplier(self) -> str:
        return SUPPLIER_BY_CATEGORY[self.category]

    @property
    def stock(self) -> int:
        return sum(qty for _expiry, qty in self.batches)


def poisson(rng: random.Random, rate: float) -> int:
    if rate <= 0:
        return 0
    limit, k, p = math.exp(-rate), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def ean13(rng: random.Random) -> str:
    digits = [6, 2, 2] + [rng.randint(0, 9) for _ in range(9)]
    checksum = (10 - sum(d * (3 if i % 2 else 1) for i, d in enumerate(digits)) % 10) % 10
    return "".join(map(str, digits + [checksum]))


def build_branch(seed: int, code_prefix: str = "P") -> list[Item]:
    rng = random.Random(seed)
    classes: list[tuple[str, tuple[float, float]]] = []
    for name, share, rate in DEMAND_CLASSES:
        classes.extend([(name, rate)] * round(share * len(CATALOG)))
    rng.shuffle(classes)
    items: list[Item] = []
    for index, (name, category, cost, margin) in enumerate(CATALOG):
        demand, (low, high) = classes[index % len(classes)]
        cost = round(cost * rng.uniform(0.95, 1.05), 2)
        price = round(cost * (1 + margin), 2)
        item = Item(
            code=f"{code_prefix}{1000 + index}",
            barcode=ean13(random.Random(index * 7919)),
            name=name,
            category=category,
            cost=cost,
            price=price,
            demand=demand,
            daily_rate=rng.uniform(low, high),
        )
        cover_days = {
            "fast": rng.choice([0, 0, 3, 8, 20, 35]),
            "normal": rng.choice([5, 15, 30, 45, 60, 250]),
            "slow": rng.choice([90, 200, 400]),
            "dead": None,
        }[demand]
        if cover_days is None:
            stock = rng.randint(4, 40)
        else:
            stock = round(item.daily_rate * cover_days)
        # Stockouts during the period hide real demand: the item sells nothing on those days.
        available = [True] * PERIOD_DAYS
        if stock == 0 and demand in {"fast", "normal"}:
            run_out = rng.randint(45, 80)  # sold out on this day and still empty
            available[run_out:] = [False] * (PERIOD_DAYS - run_out)
        elif demand == "fast" and rng.random() < 0.4:
            gap_start, gap_length = rng.randint(20, 50), rng.randint(12, 20)
            available[gap_start:gap_start + gap_length] = [False] * gap_length
        item.daily_sales = [poisson(rng, item.daily_rate) if open_day else 0 for open_day in available]
        item.unavailable_days = available.count(False)
        item.sold_qty = sum(item.daily_sales)
        item.sales_value = round(item.sold_qty * item.price, 2)
        # Batches: most items one batch, some two, some near expiry.
        expiry_roll = rng.random()
        if expiry_roll < 0.12:
            first_expiry = TODAY + timedelta(days=rng.randint(10, 75))
        elif expiry_roll < 0.16:
            first_expiry = TODAY - timedelta(days=rng.randint(5, 40))
        else:
            first_expiry = TODAY + timedelta(days=rng.randint(200, 900))
        if stock > 6 and rng.random() < 0.35:
            split = rng.randint(1, stock - 1)
            item.batches = [(first_expiry, split), (first_expiry + timedelta(days=rng.randint(120, 400)), stock - split)]
        elif stock > 0:
            item.batches = [(first_expiry, stock)]
        items.append(item)

    # A few realistic problems: a pricing error on a selling analgesic and on selling baby milk.
    for category, factor in (("Analgesics", 0.92), ("Baby Care", 0.97)):
        target = next(item for item in items if item.category == category and item.sold_qty > 0)
        target.price = round(target.cost * factor, 2)
        target.sales_value = round(target.sold_qty * target.price, 2)
    return items


def below_cost_names(items: list[Item]) -> set[str]:
    return {item.name for item in items if item.price < item.cost}


def arabic_number(value: float | int) -> str:
    return f"{value:,}".translate(ARABIC_DIGITS)


def write_arabic_excel(path: Path, seed: int = 11, period_end: date = PERIOD_END) -> None:
    """Style A: single Arabic sheet with title rows, mixed formats, totals and noise."""
    items = build_branch(seed, "A")
    period_start = period_end - timedelta(days=PERIOD_DAYS - 1)
    wb = Workbook()
    ws = wb.active
    ws.title = "تقرير الأصناف"
    ws.append(["صيدلية النور - فرع المعادي"])
    ws.append([f"تقرير حركة الأصناف من {period_start:%d/%m/%Y} إلى {period_end:%d/%m/%Y}"])
    ws.append([])
    header = ["م", "كود الصنف", "اسم الصنف", "المجموعة", "الكمية المباعة", "قيمة المبيعات", "الرصيد الحالي", "سعر الشراء", "سعر البيع", "تاريخ الصلاحية", "الشركة"]
    ws.append(header)
    for index, item in enumerate(items, start=1):
        expiry = min((exp for exp, qty in item.batches if qty > 0), default=None)
        expiry_cell: object = expiry
        if expiry and index % 4 == 0:
            expiry_cell = expiry.strftime("%m/%Y")  # month/year only, common on packs
        elif expiry and index % 5 == 0:
            expiry_cell = expiry.strftime("%d-%m-%Y")
        sold: object = item.sold_qty
        value: object = item.sales_value
        price: object = item.price
        if index % 6 == 0:
            sold = arabic_number(item.sold_qty)
        if index % 7 == 0:
            value = f"{item.sales_value:,.2f} ج.م"
        if index % 9 == 0:
            price = f"{item.price} جنيه"
        ws.append([index, item.code, f"  {item.name} ", item.category, sold, value, item.stock, item.cost, price, expiry_cell, item.supplier])
        if index == 40:
            ws.append([])
            ws.append(header)  # page-break header repeated mid-file
    # duplicate row (same item exported twice)
    dup = items[10]
    ws.append([len(items) + 1, dup.code, dup.name, dup.category, 0, 0, 0, dup.cost, dup.price, None])
    # negative stock (data-entry error)
    ws.append([len(items) + 2, "A9999", "Sample Damaged Item", "OTC", 0, 0, -3, 12, 15, None])
    ws.append([])
    ws.append(["", "", "الإجمالي", "", sum(i.sold_qty for i in items), sum(i.sales_value for i in items), sum(i.stock for i in items)])
    wb.save(path)


def write_transactions_and_batches(sales_path: Path, stock_path: Path) -> None:
    """Style B: invoice-level sales (semicolon CSV, Windows Arabic encoding) + batch-level stock CSV."""
    items = build_branch(23, "B")
    rng = random.Random(99)
    with sales_path.open("w", encoding="cp1256", newline="") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(["التاريخ", "رقم الفاتورة", "الباركود", "الصنف", "الكمية", "سعر الوحدة", "الإجمالي"])
        invoice = 50000
        for item in items:
            for offset, units in enumerate(item.daily_sales):
                day = PERIOD_START + timedelta(days=offset)
                remaining = units
                while remaining > 0:
                    qty = min(remaining, rng.choice([1, 1, 1, 2, 2, 3]))
                    remaining -= qty
                    invoice += 1
                    writer.writerow([day.strftime("%d/%m/%Y"), invoice, item.barcode, item.name, qty, f"{item.price:.2f}", f"{qty * item.price:.2f}"])
            if item.sold_qty > 10 and rng.random() < 0.3:
                invoice += 1
                day = PERIOD_START + timedelta(days=max(i for i, units in enumerate(item.daily_sales) if units))
                writer.writerow([day.strftime("%d/%m/%Y"), f"R{invoice}", item.barcode, item.name, -1, f"{item.price:.2f}", f"{-item.price:.2f}"])
    with stock_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Barcode", "Product", "Batch No", "Expiry Date", "Qty On Hand", "Unit Cost", "Retail Price", "Supplier"])
        for item in items:
            for batch_index, (expiry, qty) in enumerate(item.batches, start=1):
                writer.writerow([item.barcode, item.name.upper(), f"LOT{batch_index:02d}", expiry.isoformat(), qty, f"{item.cost:.2f}", f"{item.price:.2f}", item.supplier])


def write_legacy_pos(folder: Path, branch: str = "i-3") -> None:
    """Style C: the original 3-file POS export with report intro rows (Start Date / End Date / Branch)."""
    items = build_branch(37, "")
    folder.mkdir(parents=True, exist_ok=True)

    activity = Workbook()
    ws = activity.active
    ws.append(["Product Activity Report"])
    ws.append([f"Report Time : {TODAY:%d-%m-%Y} 09:15:00"])
    ws.append(["Start Date", "End Date", "Branch"])
    ws.append([f"{PERIOD_START:%d-%m-%Y} 00:00:00", f"{PERIOD_END:%d-%m-%Y} 23:59:59", "PHARMACY 3"])
    ws.append([])
    ws.append(["Ref", "Barcode", "Name", "Category", "Purchases", "SupplierReturns", "Adjustments", "PosSales", "CustomerReturns", "OnHand", "Cost", "Sales_Price", "NetSales"])
    for item in items:
        ws.append([item.code, item.barcode, item.name, item.category, item.sold_qty + item.stock, 0, 0, item.sold_qty, 0, item.stock, item.cost, item.price, item.sales_value])
    activity.save(folder / f"{branch}-PRODUCT ACTIVITY.xlsx")

    stock = Workbook()
    ws = stock.active
    ws.append(["Stock Report"])
    ws.append(["Branch"])
    ws.append(["PHARMACY 3"])
    ws.append(["Ref", "Name", "QOH", "Sales Price", "Total_Sales_Price"])
    for item in items:
        ws.append([item.code, item.name, item.stock, item.price, round(item.stock * item.price, 2)])
    stock.save(folder / f"{branch}-STOCK.xlsx")

    cost = Workbook()
    ws = cost.active
    ws.append(["Stock Cost Report"])
    ws.append(["Ref", "Name", "Qty", "Cost", "Cost Price Balance", "Sale Price Balance"])
    for item in items:
        ws.append([item.code, item.name, item.stock, item.cost, round(item.stock * item.cost, 2), round(item.stock * item.price, 2)])
    cost.save(folder / f"{branch}-STOCK COST.xlsx")


def write_multi_branch(path: Path) -> None:
    """Style D: chain export, one data sheet holding three branches plus a junk summary sheet."""
    wb = Workbook()
    summary = wb.active
    summary.title = "Summary"
    summary.append(["Chain inventory snapshot"])
    summary.append(["Generated", TODAY.isoformat()])
    summary.append(["Branches", 3])
    data = wb.create_sheet("Data")
    data.append(["SKU", "Item Description", "Store", "Units Sold (90d)", "On Hand", "Avg Cost", "Selling Price", "Exp.", "Supplier"])
    for seed, store in [(41, "Downtown"), (42, "Nasr City"), (43, "Zayed")]:
        for item in build_branch(seed, "C"):
            expiry = min((exp for exp, qty in item.batches if qty > 0), default=None)
            data.append([item.code, item.name, store, item.sold_qty, item.stock, item.cost, item.price, expiry, item.supplier])
    wb.save(path)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    write_arabic_excel(OUT_DIR / "A_arabic_pharmacy_report.xlsx")
    write_transactions_and_batches(OUT_DIR / "B_sales_invoices.csv", OUT_DIR / "B_stock_batches.csv")
    write_legacy_pos(OUT_DIR / "C_legacy_pos")
    write_multi_branch(OUT_DIR / "D_chain_multi_branch.xlsx")
    # Style E: the same branch exported every month (rolling 90 days) to try the trends page.
    monthly = OUT_DIR / "E_monthly_uploads"
    monthly.mkdir(exist_ok=True)
    for month_index, period_end in enumerate([date(2026, 8, 31), date(2026, 9, 30), PERIOD_END]):
        write_arabic_excel(monthly / f"{period_end:%Y-%m}_maadi_report.xlsx", seed=11 + 100 * (2 - month_index), period_end=period_end)
    print(f"Samples written to {OUT_DIR}")


if __name__ == "__main__":
    main()
