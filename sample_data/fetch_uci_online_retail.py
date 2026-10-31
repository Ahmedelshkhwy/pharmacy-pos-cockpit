"""Download a real public retail dataset to stress-test the importer.

UCI "Online Retail II" (Chen, 2019, CC BY 4.0): ~1.07M real invoice lines from a UK online
store, 2009-12 to 2011-12, in one 45 MB workbook with two overlapping sheets. It contains
returns (negative quantities), cancelled invoices, exact duplicate lines, and non-product codes,
which makes it a good test of transaction aggregation, de-duplication, and speed.
It has no stock or cost columns, so the analysis shows sales concentration only.

Run:  python sample_data/fetch_uci_online_retail.py
Then upload sample_data/uci/online_retail_II.xlsx on the upload page.
"""
from __future__ import annotations

import io
import urllib.request
import zipfile
from pathlib import Path

URL = "https://archive.ics.uci.edu/static/public/502/online+retail+ii.zip"
OUT_DIR = Path(__file__).resolve().parent / "uci"


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    print(f"Downloading {URL} ...")
    with urllib.request.urlopen(URL, timeout=120) as response:
        archive = zipfile.ZipFile(io.BytesIO(response.read()))
    archive.extractall(OUT_DIR)
    print(f"Saved to {OUT_DIR}: {', '.join(archive.namelist())}")


if __name__ == "__main__":
    main()
