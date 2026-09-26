"""Apply the curated state data in tools/data/ to history/states. Idempotent.

Data files (one per world region, so they can be reviewed independently):

  tools/data/states/<region>.csv
      id         existing state id, or ``new:<slug>`` for a state created by a split
      owner      country tag on the start date
      controller blank = owner
      cores      space-separated tags
      claims     space-separated tags
      manpower   total population on the start date (HOI4 convention)
      category   one of states.STATE_CATEGORIES
      name       display name (required for new states, ignored otherwise)
      notes      rationale / source

  tools/data/splits/<region>.csv
      province   province id to move
      to_state   existing state id or ``new:<slug>``
      notes      rationale

New states get sequential IDs after the highest existing ID. Allocations are
recorded in tools/data/new_state_ids.csv and must never be renumbered once
other content can reference them.

Usage:
    python tools/apply_state_data.py [--check]     # --check: validate data only
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import buildings
import province_geo
import states as st
from state_file import StateFile

DATA = Path(__file__).resolve().parent / "data"
IDS_FILE = DATA / "new_state_ids.csv"
NEW_NAMES_LOC = st.REPO_ROOT / "localisation" / "english" / "tnd_new_state_names_l_english.yml"
NEW_RE = re.compile(r"^new:[a-z0-9_]+$")


@dataclass
class StateRow:
    source: str
    id: str
    owner: str
    controller: str
    cores: list[str]
    claims: list[str]
    manpower: int
    category: str
    name: str
    notes: str


@dataclass
class HistoricalRow:
    """A core or claim layered on top of the start-date baseline (data/historical/)."""

    source: str
    state: str
    tag: str
    kind: str  # "core" | "claim"
    rationale: str


@dataclass
class Move:
    source: str
    province: int
    to_state: str
    notes: str


def _split_tags(value: str) -> list[str]:
    return [t for t in value.replace(",", " ").split() if t]


def load_data() -> tuple[list[StateRow], list[Move], list[str]]:
    rows: list[StateRow] = []
    moves: list[Move] = []
    errors: list[str] = []
    for path in sorted((DATA / "states").glob("*.csv")):
        with open(path, encoding="utf-8-sig", newline="") as f:
            for n, r in enumerate(csv.DictReader(f), start=2):
                where = f"{path.name}:{n}"
                try:
                    rows.append(
                        StateRow(
                            source=where,
                            id=r["id"].strip(),
                            owner=r["owner"].strip(),
                            controller=(r.get("controller") or "").strip(),
                            cores=_split_tags(r.get("cores") or ""),
                            claims=_split_tags(r.get("claims") or ""),
                            manpower=int(float(r["manpower"])),
                            category=r["category"].strip(),
                            name=(r.get("name") or "").strip(),
                            notes=(r.get("notes") or "").strip(),
                        )
                    )
                except (KeyError, ValueError, AttributeError) as exc:
                    errors.append(f"{where}: bad row ({exc!r})")
    for path in sorted((DATA / "splits").glob("*.csv")):
        with open(path, encoding="utf-8-sig", newline="") as f:
            for n, r in enumerate(csv.DictReader(f), start=2):
                where = f"{path.name}:{n}"
                try:
                    moves.append(Move(where, int(r["province"]), r["to_state"].strip(), (r.get("notes") or "").strip()))
                except (KeyError, ValueError, AttributeError) as exc:
                    errors.append(f"{where}: bad row ({exc!r})")
    return rows, moves, errors


def load_historical() -> tuple[list[HistoricalRow], list[str]]:
    rows: list[HistoricalRow] = []
    errors: list[str] = []
    for path in sorted((DATA / "historical").glob("*.csv")):
        with open(path, encoding="utf-8-sig", newline="") as f:
            for n, r in enumerate(csv.DictReader(f), start=2):
                where = f"historical/{path.name}:{n}"
                try:
                    rows.append(
                        HistoricalRow(
                            where,
                            r["state"].strip(),
                            r["tag"].strip(),
                            r["type"].strip().lower(),
                            (r.get("rationale") or "").strip(),
                        )
                    )
                except (KeyError, AttributeError) as exc:
                    errors.append(f"{where}: bad row ({exc!r})")
    return rows, errors


def _row_index(rows: list[StateRow], new_ids: dict[str, int]) -> dict[str, StateRow]:
    """Rows by id, with created states reachable by their numeric id as well."""
    index = {r.id: r for r in rows}
    for key, sid in new_ids.items():
        if key in index:
            index[str(sid)] = index[key]
    return index


def check_historical(
    hist: list[HistoricalRow], rows: list[StateRow], tags: set[str], new_ids: dict[str, int]
) -> list[str]:
    errors: list[str] = []
    by_id = _row_index(rows, new_ids)
    seen: dict[tuple[str, str, str], str] = {}
    for h in hist:
        key = (h.state, h.tag, h.kind)
        if key in seen:
            errors.append(f"{h.source}: duplicate of {seen[key]}")
        seen[key] = h.source
        if h.kind not in ("core", "claim"):
            errors.append(f"{h.source}: type must be core or claim, not {h.kind!r}")
        if h.tag not in tags:
            errors.append(f"{h.source}: {h.tag} is not a defined tag")
        if not h.rationale:
            errors.append(f"{h.source}: rationale is required")
        base = by_id.get(h.state)
        if base is None:
            errors.append(f"{h.source}: state {h.state!r} has no row in data/states")
            continue
        if h.kind == "claim" and h.tag == base.owner:
            errors.append(f"{h.source}: {h.tag} owns state {h.state}; an owner can't claim it")
        redundant = h.tag in (base.cores if h.kind == "core" else base.claims)
        if redundant:
            errors.append(f"{h.source}: {h.tag} {h.kind} on {h.state} is already in the baseline")
    # a claim is pointless where the same tag also gets a core
    cores = {(h.state, h.tag) for h in hist if h.kind == "core"}
    cores |= {(sid, t) for sid, r in by_id.items() for t in r.cores}
    for h in hist:
        if h.kind == "claim" and (h.state, h.tag) in cores:
            errors.append(f"{h.source}: {h.tag} both cores and claims {h.state}; keep the core only")
    return errors


def merge_historical(rows: list[StateRow], hist: list[HistoricalRow], new_ids: dict[str, int]) -> None:
    """Add historical cores/claims to the baseline rows (in place)."""
    by_id = _row_index(rows, new_ids)
    for h in hist:
        target = by_id[h.state]
        bucket = target.cores if h.kind == "core" else target.claims
        if h.tag not in bucket:
            bucket.append(h.tag)


def check_data(
    rows: list[StateRow],
    moves: list[Move],
    existing: dict[int, st.State],
    tags: set[str],
    new_ids: dict[str, int],
) -> list[str]:
    errors: list[str] = []
    seen: dict[str, str] = {}
    new_keys = set()
    for r in rows:
        if r.id in seen:
            errors.append(f"{r.source}: state {r.id} also defined at {seen[r.id]}")
        seen[r.id] = r.source
        if NEW_RE.match(r.id):
            new_keys.add(r.id)
            if not r.name:
                errors.append(f"{r.source}: new state {r.id} needs a name")
        elif not r.id.isdigit() or int(r.id) not in existing:
            errors.append(f"{r.source}: unknown state id {r.id!r}")
        for label, tag_list in (("owner", [r.owner]), ("controller", [r.controller] if r.controller else []), ("core", r.cores), ("claim", r.claims)):
            for t in tag_list:
                if t not in tags:
                    errors.append(f"{r.source}: {label} {t} is not a defined tag")
        if r.category not in st.STATE_CATEGORIES:
            errors.append(f"{r.source}: invalid category {r.category!r}")
        if r.manpower <= 0:
            errors.append(f"{r.source}: manpower must be positive")

    province_state = {p: s.id for s in existing.values() for p in s.provinces}
    moved: dict[int, str] = {}
    for m in moves:
        if m.province not in province_state:
            errors.append(f"{m.source}: province {m.province} is not in any state")
        if m.province in moved:
            errors.append(f"{m.source}: province {m.province} already moved at {moved[m.province]}")
        moved[m.province] = m.source
        if NEW_RE.match(m.to_state):
            if m.to_state not in new_keys:
                errors.append(f"{m.source}: {m.to_state} has no row in data/states")
        elif not m.to_state.isdigit() or int(m.to_state) not in existing:
            errors.append(f"{m.source}: unknown target state {m.to_state!r}")
    for key in new_keys:
        if not any(m.to_state == key for m in moves):
            errors.append(f"{seen[key]}: new state {key} receives no provinces")

    # a move must not empty its source state (moves already applied are no-ops)
    remaining = {sid: set(s.provinces) for sid, s in existing.items()}
    for m in moves:
        src = province_state.get(m.province)
        target = new_ids.get(m.to_state) if NEW_RE.match(m.to_state) else int(m.to_state) if m.to_state.isdigit() else None
        if src is not None and src != target:
            remaining[src].discard(m.province)
    for sid, provs in remaining.items():
        if not provs:
            errors.append(f"state {sid} would be left with no provinces")
    return errors


def load_ids() -> dict[str, int]:
    if not IDS_FILE.exists():
        return {}
    with open(IDS_FILE, encoding="utf-8", newline="") as f:
        return {r["key"]: int(r["id"]) for r in csv.DictReader(f)}


def allocate_ids(rows: list[StateRow], existing_ids: set[int]) -> dict[str, int]:
    ids = load_ids()
    next_id = max(existing_ids | set(ids.values())) + 1
    for r in rows:
        if NEW_RE.match(r.id) and r.id not in ids:
            ids[r.id] = next_id
            next_id += 1
    with open(IDS_FILE, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["key", "id"])
        for key, sid in sorted(ids.items(), key=lambda kv: kv[1]):
            w.writerow([key, sid])
    return ids


def _safe_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "", name).strip()


def create_state_file(sid: int, name: str, template: StateFile) -> StateFile:
    """New state shell, inheriting infrastructure and supply settings from its parent."""
    infra = None
    hist = template.history
    if hist is not None:
        for e in hist.value:  # type: ignore[union-attr]
            if e.key == "buildings" and e.is_block:
                infra = st.cw.scalar(e.value, "infrastructure")  # type: ignore[arg-type]
    extra = []
    for key in ("buildings_max_level_factor", "local_supplies", "impassable"):
        v = template.get(key)
        if v is not None:
            extra.append(f"\t{key} = {v}")
    text = (
        "state = {\n"
        f"\tid = {sid}\n"
        f'\tname = "STATE_{sid}" # {name}\n'
        "\tmanpower = 1\n"
        "\tstate_category = rural\n"
        + "".join(f"{line}\n" for line in extra)
        + "\n\thistory = {\n"
        + ("\t\tbuildings = {\n" f"\t\t\tinfrastructure = {infra}\n" "\t\t}\n" if infra else "")
        + "\t}\n\n"
        "\tprovinces = {\n\t}\n"
        "}\n"
    )
    path = st.MOD_STATES_DIR / f"{sid}-{_safe_filename(name)}.txt"
    return StateFile(text, path)


def write_new_state_loc(rows: list[StateRow], ids: dict[str, int]) -> None:
    if not ids:
        NEW_NAMES_LOC.unlink(missing_ok=True)
        return
    lines = ["l_english:"]
    for r in sorted((r for r in rows if r.id in ids), key=lambda r: ids[r.id]):
        name = r.name.replace('"', "'")
        lines.append(f' STATE_{ids[r.id]}:0 "{name}"')
    NEW_NAMES_LOC.parent.mkdir(parents=True, exist_ok=True)
    # HOI4 requires UTF-8 *with* BOM for localisation
    NEW_NAMES_LOC.write_text("\n".join(lines) + "\n", encoding="utf-8-sig", newline="\n")


def relocate_foreign_province_data(files: dict[int, StateFile], province_state: dict[int, int]) -> None:
    """Move building blocks / VPs that reference another state's province to that state.

    Invalid placements like these break the game (see tools/buildings.py). Data the
    owning state already has for the province wins; the stray copy is dropped.
    """
    for sid, sf in files.items():
        own = set(sf.provinces)
        blocks, vps = sf.referenced_provinces()
        for prov in sorted((blocks | vps) - own):
            dest = province_state.get(prov)
            moved_blocks, moved_vps = sf.extract_province_data({prov})
            if dest is None or dest not in files:
                print(f"state {sid}: dropped data for province {prov}, which is in no state")
                continue
            dest_blocks, dest_vps = files[dest].referenced_provinces()
            keep_blocks = [] if prov in dest_blocks else moved_blocks
            keep_vps = [] if prov in dest_vps else moved_vps
            files[dest].add_province_data(keep_blocks, keep_vps)
            print(f"state {sid}: moved province {prov} data to state {dest}"
                  + ("" if (keep_blocks or keep_vps) else " (duplicate, dropped)"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="validate the data files without writing")
    ap.add_argument("--hoi4", type=Path, default=os.environ.get("HOI4_PATH"), help="game install (for map data)")
    args = ap.parse_args()

    rows, moves, errors = load_data()
    existing = {s.id: s for s in st.load_all()}
    parse_errors = [f"{s.path.name}: {s.parse_error}" for s in existing.values() if s.parse_error]
    if parse_errors:
        print("\n".join(f"ERROR: {e}" for e in parse_errors))
        return 1

    tags = st.defined_tags()
    hist, hist_errors = load_historical()
    errors += hist_errors
    new_ids = load_ids()
    errors += check_data(rows, moves, existing, tags, new_ids)
    if not errors:
        errors += check_historical(hist, rows, tags, new_ids)
    if errors:
        print("\n".join(f"ERROR: {e}" for e in errors))
        print(f"\n{len(errors)} data errors; nothing written")
        return 1
    merge_historical(rows, hist, new_ids)
    n_cores = sum(h.kind == "core" for h in hist)
    print(f"data OK: {len(rows)} state rows, {len(moves)} province moves, "
          f"{n_cores} historical cores, {len(hist) - n_cores} historical claims")
    if args.check:
        return 0

    ids = allocate_ids(rows, set(existing))
    files: dict[int, StateFile] = {sid: StateFile.load(s.path) for sid, s in existing.items()}
    province_state = {p: s.id for s in existing.values() for p in s.provinces}

    def resolve(ref: str) -> int:
        return ids[ref] if NEW_RE.match(ref) else int(ref)

    names = {r.id: r.name for r in rows}
    coastal = province_geo.coastal_provinces(args.hoi4) if args.hoi4 else None
    if moves and coastal is None:
        print("ERROR: province moves need --hoi4 / HOI4_PATH for the coastline check")
        return 1
    for m in moves:
        target = resolve(m.to_state)
        src = province_state[m.province]
        if src == target:
            continue  # already applied on a previous run
        if target not in files:
            files[target] = create_state_file(target, names[m.to_state], files[src])
        src_file, dst_file = files[src], files[target]
        moved_buildings, vps = src_file.extract_province_data({m.province})
        src_file.set_provinces([p for p in src_file.provinces if p != m.province])
        dst_file.set_provinces(sorted(dst_file.provinces + [m.province]))
        dst_file.add_province_data(moved_buildings, vps)
        province_state[m.province] = target

    # A dockyard in a state with no coastline hard-crashes the game on load, so when a
    # split takes a state's whole coastline, its dockyards follow the coast. Sources are
    # found from vanilla's layout so this also repairs moves applied on earlier runs.
    coast_receiver: dict[int, int] = {}  # source state -> state that took its coastal province
    if moves:
        vanilla_state = {p: s.id for s in st.load_all(args.hoi4 / "history" / "states") for p in s.provinces}
        for m in moves:
            src, now = vanilla_state.get(m.province), province_state[m.province]
            if m.province in coastal and src is not None and src != now and src in files:  # type: ignore[operator]
                coast_receiver.setdefault(src, now)
    for src, receiver in coast_receiver.items():
        if not any(p in coastal for p in files[src].provinces):  # type: ignore[operator]
            level = files[src].take_state_building("dockyard")
            if level:
                files[receiver].add_state_building("dockyard", level)
                print(f"state {src} is now landlocked: moved dockyard {level} to state {receiver}")

    relocate_foreign_province_data(files, province_state)

    # building levels above the game's cap are rejected on load ("invalid state building")
    if args.hoi4:
        defs = buildings.load_definitions(args.hoi4, st.REPO_ROOT)
        caps = {name: d.max_level for name, d in defs.items() if not d.provincial and d.max_level}
        for sid, sf in files.items():
            for change in sf.clamp_state_buildings(caps):  # type: ignore[arg-type]
                print(f"state {sid}: clamped {change}")
    for r in rows:
        sf = files[resolve(r.id)]
        sf.set_ownership(r.owner, r.cores, r.controller or None, r.claims)
        sf.set_scalar("manpower", str(r.manpower))
        sf.set_scalar("state_category", r.category)

    changed = 0
    for sf in files.values():
        assert sf.path is not None
        if not sf.path.exists() or sf.changed:
            sf.save()
            changed += 1
    write_new_state_loc(rows, ids)
    print(f"wrote {changed} state files ({sum(1 for k in ids if k in names)} new states)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
