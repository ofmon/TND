"""Validate the mod's state history files.

Checks for problems that break or silently corrupt a game load:
  * unresolved git merge-conflict markers
  * files that fail to parse
  * duplicate state IDs, or file names whose ID prefix doesn't match ``id =``
  * land provinces assigned to no state, or to more than one state
  * owners / controllers / cores referencing undefined country tags
  * invalid or missing ``state_category`` / ``manpower``

Usage:
    python tools/validate_states.py [--hoi4 "E:/.../Hearts of Iron IV"]

The HOI4 install is needed for the province check (map/definition.csv is not
overridden by this mod). It defaults to the ``HOI4_PATH`` environment variable.
Exits non-zero if any error is found.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

import buildings
import province_geo
import states as st

CONFLICT_RE = re.compile(r"^(<<<<<<<|=======|>>>>>>>)", re.MULTILINE)


def land_provinces(hoi4: Path) -> set[int]:
    result: set[int] = set()
    with open(hoi4 / "map" / "definition.csv", encoding="utf-8", errors="replace") as f:
        for row in csv.reader(f, delimiter=";"):
            if len(row) >= 5 and row[0].isdigit() and row[4] == "land" and int(row[0]) > 0:
                result.add(int(row[0]))
    return result


def state_name_keys(root: Path = st.REPO_ROOT) -> set[str]:
    keys: set[str] = set()
    pat = re.compile(r"^\s*(STATE_\d+)\s*:")
    for path in (root / "localisation").rglob("*_l_english.yml"):
        for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            m = pat.match(line)
            if m:
                keys.add(m.group(1))
    return keys


def country_capitals() -> dict[str, int]:
    """Capital state per tag from the mod's history/countries (first `capital =`)."""
    caps: dict[str, int] = {}
    for path in (st.REPO_ROOT / "history" / "countries").glob("*.txt"):
        tag = path.name[:3]
        try:
            value = st.cw.scalar(st.cw.parse_file(path), "capital")
        except st.cw.ParseError:
            continue
        if value and value.isdigit():
            caps[tag] = int(value)
    return caps


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hoi4", type=Path, default=os.environ.get("HOI4_PATH"))
    args = ap.parse_args()

    errors: list[str] = []
    warnings: list[str] = []

    for path in st.state_files():
        if CONFLICT_RE.search(st.cw.read_text(path)):
            errors.append(f"{path.name}: unresolved merge-conflict markers")

    all_states = st.load_all()
    tags = st.defined_tags()

    by_id: dict[int, list[st.State]] = defaultdict(list)
    for s in all_states:
        by_id[s.id].append(s)
        if s.parse_error:
            errors.append(f"{s.path.name}: parse error: {s.parse_error}")
            continue
        prefix = re.match(r"^(\d+)", s.path.name)
        if prefix and int(prefix.group(1)) != s.id:
            warnings.append(f"{s.path.name}: file name prefix != id {s.id}")
        if s.category not in st.STATE_CATEGORIES:
            errors.append(f"{s.path.name}: invalid state_category {s.category!r}")
        if s.manpower is None:
            errors.append(f"{s.path.name}: missing manpower")
        if not s.provinces:
            errors.append(f"{s.path.name}: no provinces")
        if s.owner is None:
            warnings.append(f"{s.path.name}: no owner")
        for label, tag_list in (("owner", [s.owner]), ("controller", [s.controller]), ("core", s.cores)):
            for tag in tag_list:
                if tag and tag not in tags:
                    errors.append(f"{s.path.name}: {label} {tag} is not defined in common/country_tags")

    for sid, group in sorted(by_id.items()):
        if len(group) > 1:
            names = ", ".join(s.path.name for s in group)
            errors.append(f"duplicate state id {sid}: {names}")
    # HOI4 requires state ids to run 1..N with no gaps
    gaps = sorted(set(range(1, max(by_id) + 1)) - set(by_id)) if by_id else []
    if gaps:
        errors.append(f"state ids are not contiguous; missing {gaps[:40]}")

    loc_keys = state_name_keys()
    unnamed = sorted(s.id for s in all_states if s.name and s.name.startswith("STATE_") and s.name not in loc_keys)
    if unnamed and args.hoi4:
        vanilla_keys = state_name_keys(args.hoi4)
        # vanilla names only apply if the mod doesn't replace vanilla's file
        if not (st.REPO_ROOT / "localisation" / "replace" / "state_names_l_english.yml").exists():
            unnamed = [i for i in unnamed if f"STATE_{i}" not in vanilla_keys]
    if unnamed:
        errors.append(f"{len(unnamed)} states have no STATE_<id> localisation: {unnamed[:40]}")

    owned = {s.id: s.owner for s in all_states}
    for tag, capital in country_capitals().items():
        if tag in {s.owner for s in all_states} and owned.get(capital) != tag:
            warnings.append(f"{tag}: capital state {capital} is owned by {owned.get(capital)} (fix history/countries)")

    province_owner: dict[int, list[int]] = defaultdict(list)
    for s in all_states:
        for p in s.provinces:
            province_owner[p].append(s.id)
    for p, sids in sorted(province_owner.items()):
        if len(sids) > 1:
            errors.append(f"province {p} is in multiple states: {sorted(sids)}")

    if args.hoi4:
        land = land_provinces(args.hoi4)
        missing = sorted(land - province_owner.keys())
        if missing:
            errors.append(f"{len(missing)} land provinces are in no state: {missing[:40]}{' ...' if len(missing) > 40 else ''}")
        # vanilla history files load alongside the mod's unless a mod file has the same name
        mod_hist = {p.name for p in (st.REPO_ROOT / "history" / "countries").glob("*.txt")}
        mod_tags = {n[:3] for n in mod_hist}
        for p in (args.hoi4 / "history" / "countries").glob("*.txt"):
            if p.name[:3] in mod_tags and p.name not in mod_hist:
                errors.append(f"history/countries: vanilla '{p.name}' also loads for {p.name[:3]}; add an empty file of that name")

        # a state spanning two strategic regions crashes the game on load
        region_files = {f.name: f for f in (args.hoi4 / "map" / "strategicregions").glob("*.txt")}
        region_files.update({f.name: f for f in (st.REPO_ROOT / "map" / "strategicregions").glob("*.txt")})
        region_of: dict[int, str] = {}
        for name, f in region_files.items():
            r = st.cw.first(st.cw.parse_file(f), "strategic_region")
            provs = st.cw.first(r.value, "provinces") if r else None  # type: ignore[arg-type]
            for p in st.cw.bare_values(provs.value) if provs else []:  # type: ignore[arg-type]
                region_of[int(p)] = name
        for s in all_states:
            spans = {region_of.get(p) for p in s.provinces} - {None}
            if len(spans) > 1:
                errors.append(f"{s.path.name}: spans strategic regions {sorted(spans)} (crashes the game; run sync_strategic_regions.py)")

        # invalid building placements can crash the game on load
        coastal = province_geo.coastal_provinces(args.hoi4)
        defs = buildings.load_definitions(args.hoi4, st.REPO_ROOT)
        for s in all_states:
            if s.parse_error:
                continue
            for severity, msg in buildings.sweep_state(s.path, defs, coastal):
                (errors if severity == "error" else warnings).append(f"{s.path.name}: {msg}")
        positions = st.REPO_ROOT / "map" / "buildings.txt"
        if positions.exists():
            for severity, msg in buildings.sweep_positions(args.hoi4, positions, all_states, defs):
                (errors if severity == "error" else warnings).append(msg)

        # vanilla itself puts some lakes in states; only flag ones it doesn't
        vanilla_provs = {p for s in st.load_all(args.hoi4 / "history" / "states") for p in s.provinces}
        not_land = sorted(set(province_owner) - land - vanilla_provs)
        if not_land:
            warnings.append(f"{len(not_land)} state provinces are not land provinces: {not_land[:40]}")
    else:
        warnings.append("HOI4 path not given (--hoi4 / HOI4_PATH); skipped province coverage check")

    for w in warnings:
        print(f"WARNING: {w}")
    for e in errors:
        print(f"ERROR: {e}")
    print(f"\n{len(all_states)} state files, {len(errors)} errors, {len(warnings)} warnings")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
