"""Building definitions (common/buildings) and a validity sweep for state histories.

HOI4 hard-crashes on some invalid building placements at load (e.g. a dockyard
in a state with no coast), so every state's buildings are checked against the
game's own definitions:

  * the building type exists
  * state-level buildings aren't placed on a province and vice versa
  * province-keyed blocks only reference provinces that belong to the state
  * coastal-only buildings (``only_costal``) sit on coastal provinces / states
  * province building levels don't exceed ``province_max`` (warning)
  * victory points only reference the state's own provinces
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import clausewitz as cw
import states as st


@dataclass
class BuildingDef:
    name: str
    provincial: bool
    max_level: int | None
    coastal_only: bool
    shares_slots: bool


def _find_scalar(entries: list[cw.Entry], key: str) -> str | None:
    """Depth-first search for a scalar key anywhere in a block."""
    for e in entries:
        if e.key == key and not e.is_block:
            return e.value  # type: ignore[return-value]
        if e.is_block:
            found = _find_scalar(e.value, key)  # type: ignore[arg-type]
            if found is not None:
                return found
    return None


def load_definitions(*roots: Path) -> dict[str, BuildingDef]:
    """Building definitions from each root's common/buildings; later roots override."""
    defs: dict[str, BuildingDef] = {}
    for root in roots:
        for path in sorted((root / "common" / "buildings").glob("*.txt")):
            top = cw.first(cw.parse_file(path), "buildings")
            if top is None or not top.is_block:
                continue
            for b in top.value:  # type: ignore[union-attr]
                if not b.key or not b.is_block:
                    continue
                body: list[cw.Entry] = b.value  # type: ignore[assignment]
                prov_max = _find_scalar(body, "province_max")
                state_max = _find_scalar(body, "state_max")
                cap = prov_max or state_max
                defs[b.key] = BuildingDef(
                    name=b.key,
                    provincial=prov_max is not None,
                    max_level=int(float(cap)) if cap else None,
                    coastal_only=_find_scalar(body, "only_costal") == "yes",
                    shares_slots=_find_scalar(body, "shares_slots") == "yes",
                )
    return defs


def sweep_positions(
    hoi4: Path,
    positions_file: Path,
    all_states: list[st.State],
    defs: dict[str, BuildingDef],
) -> list[tuple[str, str]]:
    """Check map/buildings.txt 3D positions against the current states.

    Vanilla's own file has some positions just outside their state (mostly naval
    spots over sea pixels), so a land position is only flagged when it lies in a
    different state now but was consistent in vanilla.
    """
    import province_geo  # needs numpy/PIL; only imported when positions are checked

    idmap = province_geo.province_id_map(hoi4)
    height, width = idmap.shape
    current = {p: s.id for s in all_states for p in s.provinces}
    vanilla = {p: s.id for s in st.load_all(hoi4 / "history" / "states") for p in s.provinces}
    state_ids = {s.id for s in all_states}
    # position-only pseudo types (e.g. naval_base_spawn) are valid if vanilla uses them
    vanilla_types = {
        line.split(";")[1]
        for line in (hoi4 / "map" / "buildings.txt").read_text(encoding="utf-8", errors="replace").splitlines()
        if line.count(";") >= 4
    }
    problems: list[tuple[str, str]] = []
    unknown_types: set[str] = set()
    kinds_by_state: dict[int, set[str]] = {}
    for n, line in enumerate(positions_file.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        parts = line.split(";")
        if len(parts) < 5 or not parts[0].isdigit():
            continue
        sid, kind = int(parts[0]), parts[1]
        if sid not in state_ids:
            problems.append(("error", f"map/buildings.txt:{n}: position for state {sid}, which doesn't exist"))
            continue
        if kind not in defs and kind not in vanilla_types and kind not in unknown_types:
            unknown_types.add(kind)
            problems.append(("warning", f"map/buildings.txt:{n}: unknown building type {kind!r}"))
        kinds_by_state.setdefault(sid, set()).add(kind)
        col, row = int(float(parts[2])), height - 1 - int(float(parts[4]))
        if 0 <= col < width and 0 <= row < height:
            pid = int(idmap[row, col])
            if pid in current and current[pid] != sid and vanilla.get(pid) == sid:
                problems.append(("error", f"map/buildings.txt:{n}: {kind} position for state {sid} lies in province {pid}, now in state {current[pid]}"))
    # every vanilla state has these; a state without them logs MAP_ERRORs and crashes
    # the strategic air system (fixed by sync_building_positions.py)
    for sid in sorted(state_ids):
        missing = sorted(REQUIRED_EVERY_STATE - kinds_by_state.get(sid, set()))
        if missing:
            problems.append(("error", f"map/buildings.txt: state {sid} has no position for {', '.join(missing)} (run sync_building_positions.py)"))
    return problems


REQUIRED_EVERY_STATE = {
    "arms_factory", "industrial_complex", "anti_air_building", "air_base", "synthetic_refinery",
    "nuclear_reactor_spawn", "rocket_site_spawn", "radar_station", "fuel_silo", "stronghold_network",
}


def _level(value: cw.Value) -> int | None:
    """Level of a building entry: `x = 3` or the landmark form `x = { level = 1 ... }`."""
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return None
    lvl = cw.scalar(value, "level")
    return int(float(lvl)) if lvl is not None else None


def sweep_state(
    path: Path,
    defs: dict[str, BuildingDef],
    coastal: set[int],
) -> list[tuple[str, str]]:
    """Return (severity, message) problems with the buildings in one state file."""
    state = st.load_state(path)
    if state.parse_error:
        return []
    root = cw.first(cw.parse_file(path), "state")
    hist = cw.first(root.value, "history") if root else None  # type: ignore[arg-type]
    if hist is None:
        return []
    problems: list[tuple[str, str]] = []
    provinces = set(state.provinces)
    state_levels: dict[str, int] = {}
    seen_blocks: set[tuple[str, int]] = set()

    def check(name: str, level: int | None, where: str, province: int | None) -> None:
        d = defs.get(name)
        if d is None:
            problems.append(("error", f"unknown building {name!r} {where}"))
            return
        if province is None and d.provincial:
            problems.append(("error", f"provincial building {name} placed at state level {where}"))
        if province is not None and not d.provincial:
            problems.append(("error", f"state building {name} placed on province {province} {where}"))
        if d.coastal_only and province is not None and province not in coastal:
            problems.append(("error", f"coastal-only {name} on inland province {province} {where}"))
        if level is not None and d.max_level is not None and level > d.max_level and province is not None:
            problems.append(("warning", f"{name} level {level} exceeds max {d.max_level} on province {province} {where}"))

    def scan(entries: list[cw.Entry], where: str) -> None:
        for e in entries:
            if e.key == "buildings" and e.is_block:
                for b in e.value:  # type: ignore[union-attr]
                    if not b.key:
                        continue
                    if b.key.isdigit() and b.is_block:
                        prov = int(b.key)
                        if (where, prov) in seen_blocks:
                            problems.append(("error", f"duplicate buildings block for province {prov} {where}"))
                        seen_blocks.add((where, prov))
                        if prov not in provinces:
                            problems.append(("error", f"buildings for province {prov}, which is not in this state {where}"))
                        for pb in b.value:  # type: ignore[union-attr]
                            if pb.key:
                                check(pb.key, _level(pb.value), where, prov)
                    else:
                        lvl = _level(b.value)
                        check(b.key, lvl, where, None)
                        d = defs.get(b.key)
                        if d and not d.provincial and d.max_level and lvl is not None and lvl > d.max_level:
                            problems.append(("error", f"{b.key} = {lvl} exceeds the game's max {d.max_level} {where} (rejected on load)"))
                        if lvl is not None and not b.is_block:
                            state_levels[b.key] = state_levels.get(b.key, 0) + lvl
            elif st.parse_date(e.key) and e.is_block and st.parse_date(e.key) <= st.START_DATE:
                scan(e.value, f"(in {e.key})")  # type: ignore[arg-type]

    scan(hist.value, "")  # type: ignore[arg-type]

    # State-level totals over state_max or the category's slots are not checked: the
    # game clamps them, and vanilla itself trips such checks because dated history
    # blocks can set rather than add levels.
    for name in state_levels:
        d = defs.get(name)
        if d and d.coastal_only and not provinces & coastal:
            problems.append(("error", f"coastal-only {name} in a state with no coastal province (crashes the game)"))
    for prov in state.victory_points:
        if prov not in provinces:
            problems.append(("error", f"victory points on province {prov}, which is not in this state"))
    return problems
