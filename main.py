from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from pathlib import Path

import requests

# Allow `python main.py ...` without install
sys.path.insert(0, str(Path(__file__).parent / "src"))

from payer_intel.crew import run  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Aarete Salesforce Payer Intelligence")
    p.add_argument("--seed", default="data/seed_payers_smoke.csv", help="CSV: payer_name,domain,payer_type")
    p.add_argument("--out", default="out", help="Output directory")
    p.add_argument("--log-level", default="INFO")
    return p.parse_args()


def check_searchapi_quota(min_required: int) -> None:
    """Abort early if SearchApi quota is insufficient for the run."""
    key = os.environ.get("SEARCHAPI_API_KEY", "")
    if not key:
        print("SEARCHAPI_API_KEY not set - skipping quota check")
        return
    try:
        r = requests.get(
            "https://www.searchapi.io/api/v1/account",
            headers={"Authorization": f"Bearer {key}"},
            timeout=10,
        )
        if r.status_code != 200:
            print(f"Could not check SearchApi quota: HTTP {r.status_code} - proceeding anyway")
            return
        data = r.json()
        remaining = data.get("searches_remaining", data.get("remaining_searches"))
        if remaining is None:
            print("Could not read SearchApi quota from response - proceeding anyway")
            return
        if remaining < min_required:
            print(f"Insufficient SearchApi quota: need {min_required}, have {remaining}. Aborting.")
            sys.exit(1)
        print(f"SearchApi quota OK ({remaining} remaining, need {min_required})")
    except Exception as e:  # noqa: BLE001 — best-effort precheck
        print(f"Could not check SearchApi quota: {e} - proceeding anyway")


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    with open(args.seed, encoding="utf-8-sig") as f:
        seed_count = sum(1 for _ in csv.DictReader(f))
    check_searchapi_quota(min_required=seed_count * 7)
    out = run(Path(args.seed), Path(args.out))
    print(f"\nReport written: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
