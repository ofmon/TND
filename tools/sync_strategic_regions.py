"""Keep every state inside a single strategic region (the game crashes otherwise).

When a split moves a province into a state from another strategic region, the
province is moved into the region that holds most of the state. Only the vanilla
region files that change are written to map/strategicregions/ (overriding
vanilla); files this script wrote earlier but no longer needs are removed.

Usage:
    python tools/sync_strategic_regions.py --hoi4 "E:/.../Hearts of Iron IV"
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

import clausewitz as cw
import states as st

OUT = st.REPO_ROOT / "map" / "strategicregions"
MANIFEST = Path(__file__).resolve().parent / "data" / "generated_strategic_regions.txt"


def load_regions(directory: Path) -> dict[Path, tuple[int, list[int]]]:
    regions = {}
    for f in sorted(directory.glob("*.txt")):
        r = cw.first(cw.parse_file(f), "strategic_region")
        if r is None or not r.is_block:
            continue
        rid = int(cw.scalar(r.value, "id"))  # type: ignore[arg-type]
        provs = cw.first(r.value, "provinces")  # type: ignore[arg-type]
        regions[f] = (rid, [int(p) for p in cw.bare_values(provs.value)] if provs else [])  # type: ignore[union-attr, arg-type]
    return regions


def rewrite_provinces(path: Path, provinces: list[int]) -> str:
    text = cw.read_text(path)
    root = cw.parse(text)
    r = cw.first(root, "strategic_region")
    provs = cw.first(r.value, "provinces")  # type: ignore[union-attr, arg-type]
    body = " ".join(str(p) for p in provinces)
    return text[: provs.value_start] + "{\n\t\t" + body + "\n\t}" + text[provs.value_end :]  # type: ignore[union-attr]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hoi4", type=Path, default=os.environ.get("HOI4_PATH"), required=os.environ.get("HOI4_PATH") is None)
    args = ap.parse_args()

    regions = load_regions(args.hoi4 / "map" / "strategicregions")
    region_of = {p: f for f, (_, provs) in regions.items() for p in provs}
    members = {f: list(provs) for f, (_, provs) in regions.items()}

    changed: set[Path] = set()
    for s in st.load_all():
        counts = Counter(region_of[p] for p in s.provinces if p in region_of)
        if len(counts) <= 1:
            continue
        home = counts.most_common(1)[0][0]
        for p in s.provinces:
            src = region_of.get(p)
            if src is not None and src != home:
                members[src].remove(p)
                members[home].append(p)
                region_of[p] = home
                changed |= {src, home}
                print(f"state {s.id}: province {p} moved from region {regions[src][0]} to {regions[home][0]}")

    OUT.mkdir(parents=True, exist_ok=True)
    previous = set(MANIFEST.read_text(encoding="utf-8").split()) if MANIFEST.exists() else set()
    written = set()
    for f in sorted(changed):
        (OUT / f.name).write_text(rewrite_provinces(f, sorted(members[f])), encoding="utf-8", newline="\n")
        written.add(f.name)
    for stale in previous - written:
        (OUT / stale).unlink(missing_ok=True)
    MANIFEST.write_text("\n".join(sorted(written)) + "\n", encoding="utf-8")
    print(f"{len(written)} strategic region files written to {OUT.relative_to(st.REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
