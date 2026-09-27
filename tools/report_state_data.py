"""Sanity-check the curated state data against reference national populations.

Sums ``manpower`` by owner across every tools/data/states/*.csv and compares it
with tools/data/reference_population_2000.csv. Countries that span several
region files are where mistakes hide, so this is the cross-region check.

Usage:
    python tools/report_state_data.py [--tolerance 0.03]
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

from apply_state_data import load_data

REFERENCE = Path(__file__).resolve().parent / "data" / "reference_population_2000.csv"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tolerance", type=float, default=0.03)
    args = ap.parse_args()

    rows, _, errors = load_data()
    if errors:
        print("\n".join(errors))
        return 1
    totals: Counter[str] = Counter()
    for r in rows:
        totals[r.owner] += r.manpower

    with open(REFERENCE, encoding="utf-8") as f:
        reference = {r["tag"]: int(r["population"]) for r in csv.DictReader(f)}

    off = []
    for tag, ref in sorted(reference.items()):
        got = totals.get(tag, 0)
        diff = (got - ref) / ref
        if abs(diff) > args.tolerance:
            off.append((tag, got, ref, diff))
    world = sum(totals.values())
    print(f"world total: {world / 1e9:.3f}B (UN 1 Jan 2000: ~6.08B)")
    if off:
        print(f"\n{len(off)} countries outside ±{args.tolerance:.0%}:")
        for tag, got, ref, diff in sorted(off, key=lambda t: -abs(t[3])):
            print(f"  {tag}: {got / 1e6:9.2f}M vs {ref / 1e6:9.2f}M ({diff:+.1%})")
    else:
        print(f"all {len(reference)} reference countries within ±{args.tolerance:.0%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
