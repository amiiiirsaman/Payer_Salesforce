from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Allow `python main.py ...` without install
sys.path.insert(0, str(Path(__file__).parent / "src"))

from payer_intel.crew import run  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Aarete Salesforce Payer Intelligence")
    p.add_argument("--seed", default="data/seed_payers_smoke.csv", help="CSV: payer_name,domain,payer_type")
    p.add_argument("--out", default="out", help="Output directory")
    p.add_argument("--log-level", default="INFO")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    out = run(Path(args.seed), Path(args.out))
    print(f"\nReport written: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
