# POS Kick-off Analysis

This project includes a repeatable scaffold for real POS exports from systems such as Julip, Ascon, and Saap.
It reads exported files that share the same branch prefix, detects the actual table header after report intro rows,
joins product activity with stock, and writes analysis files under `instance/pos_analysis`.

## Branch Upload Workflow

Each branch uploads three raw POS exports together from the platform upload page:

- `Product Activity`
- `Stock`
- `Stock Cost`

The platform archives the raw files first, then creates an analysis folder and imports the results.
Uploading a branch replaces only that branch's previous data, so `i-7` does not delete future `i-1` to `i-10` data.
The selected branch in the platform is the source of truth. If the POS export still contains an old internal
label such as `PHARMACY 7`, the platform stores the batch under the branch selected on upload.

Product Activity should be exported from the POS for the latest 90 days. The platform uses 90 days as the
sales velocity baseline, then forecasts stockout probability over 7 / 14 / 30 days. These are different concepts:
90 days measures movement, while 7 / 14 / 30 days are decision horizons.

Raw archive path:

```text
instance/pos_raw/<branch>/<timestamp>/<branch>-PRODUCT ACTIVITY.xlsx
instance/pos_raw/<branch>/<timestamp>/<branch>-STOCK.xlsx
instance/pos_raw/<branch>/<timestamp>/<branch>-STOCK COST.xlsx
```

Analysis path:

```text
instance/pos_analysis/<branch>_<timestamp>/
```

## CLI Expected Files

For branch `i-7`, CLI analysis can still read files beside the `pharmacy_system` folder:

```text
../i-7-PRODUCT ACTIVITY.xlsx
../i-7-STOCK.xlsx
../i-7-STOCK COST.xlsx
```

## Run

From `pharmacy_system`:

```powershell
.\.venv\Scripts\python.exe run_pos_kickoff.py
```

Optional:

```powershell
.\.venv\Scripts\python.exe run_pos_kickoff.py --prefix "i-7-" --source-dir ".." --output-dir ".\instance\pos_analysis"
```

## Output Files

- `README.md`
- `summary.json`
- `merged_items.csv`
- `category_summary.csv`
- `dead_stock.csv`
- `slow_moving.csv`
- `stockout_with_sales.csv`
- `top_stock_value.csv`
- `top_net_sales.csv`

## Current Signals

The latest `i-7` run now includes predictive metrics:

- Total items: 2,943
- POS sales quantity: 8,421.68
- Net sales: 8,169.68
- Stock sales value: 197,173.30
- Estimated stock cost value: 112,301.35
- Estimated gross profit from POS sales: 150,617.51
- Expected lost sales next 30 days: 90,492.94
- Frozen capital value: 74,005.43
- High risk items: 666
- Recommended reorder units: 3,501
- Dead stock: 1,011
- Overstock risk: 348
- Stockout with sales: 665
- Fast moving: 52

## Predictive Fields

- `avg_daily_sales`: sales velocity using the 90-day movement baseline.
- `days_cover`: how many days current stock can cover at the current velocity.
- `stockout_probability_7d`, `14d`, `30d`: probability that demand exceeds current stock.
- `reorder_point`: suggested minimum stock level using lead-time demand plus safety stock.
- `recommended_reorder_qty`: quantity needed to rebuild safe cover.
- `risk_score`: 1-10 score combining stockout risk, stock value, dead stock, overstock, and velocity.
- `decision_label`: plain decision such as buy now, stop buying, clear dead stock, or healthy.
- `lost_sales_value`: estimated value of unmet demand over the next 30 days.
- `frozen_capital_value`: capital locked in dead or overstocked items.

## Dashboard Layers

- General command center: `/` shows all branches together with a branch filter.
- Branch dashboard: `/?branch=i-7` or `/branches/i-7` isolates decisions for one branch.
- Financial panel: `/financial` compares exposure by branch and category.
- Branch financial panel: `/financial?branch=i-7` filters the financial cards and category exposure.
