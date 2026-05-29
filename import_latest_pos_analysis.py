from __future__ import annotations

import argparse
from pathlib import Path

from app import import_pos_analysis_to_db


BASE_DIR = Path(__file__).resolve().parent
ANALYSIS_DIR = BASE_DIR / "instance" / "pos_analysis"


def latest_analysis_dir(root: Path = ANALYSIS_DIR) -> Path:
    candidates = [path for path in root.glob("i-*_20*") if path.is_dir() and (path / "merged_items.csv").exists()]
    if not candidates:
        raise FileNotFoundError(f"No analysis folders with merged_items.csv found under {root}")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def main() -> None:
    parser = argparse.ArgumentParser(description="Import POS analysis into the platform.")
    parser.add_argument("--analysis-dir", type=Path, default=None, help="Specific analysis folder to import.")
    parser.add_argument(
        "--replace-all",
        action="store_true",
        help="Clear every branch before importing. Default replaces only the branch in the analysis.",
    )
    args = parser.parse_args()
    path = args.analysis_dir or latest_analysis_dir()
    count = import_pos_analysis_to_db(path, replace_all=args.replace_all)
    print(f"Imported {count} POS rows from {path}")


if __name__ == "__main__":
    main()
