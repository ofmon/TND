"""Set every state's 2000-era industry and infrastructure from tools/data/industry/. Idempotent.

Data:

  tools/data/industry/national_2000.csv
      tag, country, gdp_ppp_bn, industry_mult, milex_pct, arms_industry, dockyards, notes
      One row per country that owns a state.

  tools/data/industry/overrides.csv
      state, industrial_complex, arms_factory, dockyard, infrastructure, notes
      Pins values for specific states (blank = computed). Pinned factories count
      towards the owner's national totals.

Method (per country):

  * total factories  = round(SCALE * gdp_ppp_bn ** EXPONENT * industry_mult)
    The sublinear exponent keeps the largest economies playable and inside
    their building slots.
  * dockyards        = the national `dockyards` figure (major shipbuilders only)
  * arms factories   = (total - dockyards) * min(0.4, milex_pct * 6 / 100)
                       * (0.25 + 0.75 * arms_industry)
  * civilian         = the rest.

  Factories are spread over the country's states in proportion to
  slots(category) * sqrt(manpower), never exceeding a state's free shared
  building slots minus a reserve left for construction. Dockyards go to coastal
  states only, preferring states that already had shipyards.

  Infrastructure comes from national GDP per capita (PPP), adjusted by the state
  category, clamped to 1..5.

Usage:
    python tools/apply_industry.py [--check] [--report]
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import buildings
import clausewitz as cw
import province_geo
import states as st
from state_file import StateFile

DATA = Path(__file__).resolve().parent / "data" / "industry"
SCALE = 0.155
EXPONENT = 0.8
MANAGED = ("industrial_complex", "arms_factory", "dockyard")

# infrastructure base level by GDP per capita (PPP, int'l $), and per-category adjustment
INFRA_TIERS = ((1500, 1.5), (3000, 2.0), (6000, 2.5), (12000, 3.0), (20000, 3.5), (math.inf, 4.0))
INFRA_ADJUST = {
    "megalopolis": 1.0, "metropolis": 1.0, "large_city": 0.5, "city": 0.5,
    "large_town": 0.0, "town": 0.0, "rural": -0.5, "pastoral": -1.0, "wasteland": -1.5,
}


@dataclass
class Nation:
    tag: str
    gdp: float
    mult: float
    milex: float
    arms: float
    dockyards: int

    def totals(self) -> tuple[int, int, int]:
        total = max(round(SCALE * self.gdp ** EXPONENT * self.mult), 0)
        dock = min(self.dockyards, total) if total else 0
        share = min(0.4, self.milex * 6 / 100) * (0.25 + 0.75 * self.arms)
        mil = round((total - dock) * share)
        return total - dock - mil, mil, dock


def load_nations() -> tuple[dict[str, Nation], list[str]]:
    nations: dict[str, Nation] = {}
    errors: list[str] = []
    with open(DATA / "national_2000.csv", encoding="utf-8-sig", newline="") as f:
        for n, r in enumerate(csv.DictReader(f), start=2):
            try:
                t = r["tag"].strip()
                if t in nations:
                    errors.append(f"national_2000.csv:{n}: duplicate tag {t}")
                nations[t] = Nation(t, float(r["gdp_ppp_bn"]), float(r["industry_mult"]),
                                    float(r["milex_pct"]), float(r["arms_industry"]), int(r["dockyards"]))
            except (KeyError, ValueError) as exc:
                errors.append(f"national_2000.csv:{n}: bad row ({exc!r})")
    return nations, errors


def load_overrides() -> tuple[dict[int, dict[str, int]], list[str]]:
    path = DATA / "overrides.csv"
    out: dict[int, dict[str, int]] = {}
    errors: list[str] = []
    if not path.exists():
        return out, errors
    with open(path, encoding="utf-8-sig", newline="") as f:
        for n, r in enumerate(csv.DictReader(f), start=2):
            try:
                sid = int(r["state"])
                vals = {k: int(r[k]) for k in (*MANAGED, "infrastructure") if (r.get(k) or "").strip()}
            except (KeyError, ValueError) as exc:
                errors.append(f"overrides.csv:{n}: bad row ({exc!r})")
                continue
            if sid in out:
                errors.append(f"overrides.csv:{n}: state {sid} listed twice")
            out[sid] = vals
    return out, errors


def category_slots(hoi4: Path) -> dict[str, int]:
    slots: dict[str, int] = {}
    for path in sorted((hoi4 / "common" / "state_category").glob("*.txt")):
        for e in cw.parse_file(path):
            if e.is_block and e.key == "state_categories":
                for cat in e.value:  # type: ignore[union-attr]
                    v = cw.scalar(cat.value, "local_building_slots") if cat.is_block else None  # type: ignore[arg-type]
                    if v is not None:
                        slots[cat.key] = int(float(v))  # type: ignore[index]
    return slots


def tech_slot_factors(hoi4: Path) -> dict[str, float]:
    """global_building_slots_factor granted by each technology."""
    factors: dict[str, float] = {}

    def scan(entries: list[cw.Entry], tech: str | None) -> None:
        for e in entries:
            if e.key == "global_building_slots_factor" and tech and not e.is_block:
                factors[tech] = factors.get(tech, 0.0) + float(e.value)  # type: ignore[arg-type]
            elif e.is_block and e.key not in ("allow", "allow_branch", "ai_will_do", "path"):
                scan(e.value, e.key if tech is None and e.key != "technologies" else tech)  # type: ignore[arg-type]

    for path in sorted((hoi4 / "common" / "technologies").glob("*.txt")):
        scan(cw.parse_file(path), None)
    return factors


def country_slot_factor(tag: str, factors: dict[str, float]) -> float:
    """Sum of slot factors of every technology the country's history grants (any date, any DLC branch)."""
    total = 0.0
    for path in (st.REPO_ROOT / "history" / "countries").glob(f"{tag} - *.txt"):
        def scan(entries: list[cw.Entry]) -> list[str]:
            techs: list[str] = []
            for e in entries:
                if e.key == "set_technology" and e.is_block:
                    techs += [t.key for t in e.value if t.key and not t.is_block and t.value != "0"]  # type: ignore[union-attr]
                elif e.is_block:
                    techs += scan(e.value)  # type: ignore[arg-type]
            return techs
        try:
            techs = set(scan(cw.parse_file(path)))
        except cw.ParseError as exc:
            print(f"WARNING: {path.name}: {exc}; assuming no slot techs")
            continue
        total += sum(factors.get(t, 0.0) for t in techs)
    return total


def reserve(slots: int) -> int:
    """Slots kept free for construction."""
    return max(1, round(slots * 0.15)) if slots >= 3 else 0


def allocate(total: int, weights: dict[int, float], caps: dict[int, int]) -> tuple[dict[int, int], int]:
    """Largest-remainder apportionment with per-state caps; returns (allocation, unplaced)."""
    alloc = {k: 0 for k in weights}
    left = total
    while left > 0:
        open_ = {k: w for k, w in weights.items() if w > 0 and alloc[k] < caps.get(k, 0)}
        if not open_:
            break
        wsum = sum(open_.values())
        shares = {k: left * w / wsum for k, w in open_.items()}
        placed = 0
        for k, s in shares.items():
            add = min(int(s), caps[k] - alloc[k])
            alloc[k] += add
            placed += add
        left -= placed
        if placed == 0:
            # hand out single factories by largest remainder
            for k in sorted(open_, key=lambda k: (shares[k] - int(shares[k]), open_[k]), reverse=True):
                if left == 0:
                    break
                if alloc[k] < caps[k]:
                    alloc[k] += 1
                    left -= 1
    return alloc, left


def infra_level(gdp_pc: float, category: str | None) -> int:
    base = next(v for limit, v in INFRA_TIERS if gdp_pc < limit)
    level = math.floor(base + INFRA_ADJUST.get(category or "", 0.0) + 0.5)
    return max(1, min(5, level))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="validate data and print the plan without writing")
    ap.add_argument("--report", action="store_true", help="print national totals")
    ap.add_argument("--hoi4", type=Path, default=os.environ.get("HOI4_PATH"),
                    required=os.environ.get("HOI4_PATH") is None)
    args = ap.parse_args()

    nations, errors = load_nations()
    overrides, oerr = load_overrides()
    errors += oerr
    all_states = {s.id: s for s in st.load_all()}
    for s in all_states.values():
        if s.parse_error:
            errors.append(f"{s.path.name}: {s.parse_error}")
    owners = {s.owner for s in all_states.values() if s.owner}
    errors += [f"national_2000.csv: no row for state owner {t}" for t in sorted(owners - set(nations))]
    errors += [f"overrides.csv: unknown state {sid}" for sid in overrides if sid not in all_states]
    if errors:
        print("\n".join(f"ERROR: {e}" for e in errors))
        return 1

    slots_by_cat = category_slots(args.hoi4)
    defs = buildings.load_definitions(args.hoi4, st.REPO_ROOT)
    shared = {n for n, d in defs.items() if d.shares_slots}
    coastal_provs = province_geo.coastal_provinces(args.hoi4)
    vanilla = {s.id: s for s in st.load_all(args.hoi4 / "history" / "states")}

    tech_factors = tech_slot_factors(args.hoi4)
    slot_factor = {t: country_slot_factor(t, tech_factors) for t in owners}

    files = {sid: StateFile.load(s.path) for sid, s in all_states.items()}
    free: dict[int, int] = {}
    old_dock: dict[int, int] = {}
    for sid, s in all_states.items():
        eff = files[sid].effective_state_buildings()
        other = sum(int(float(e.value)) for k, e in eff.items() if k in shared and k not in MANAGED)  # type: ignore[arg-type]
        slots = math.floor(slots_by_cat.get(s.category or "", 0) * (1 + slot_factor.get(s.owner or "", 0.0)))
        free[sid] = max(0, slots - reserve(slots) - other)
        # historic shipyards come from vanilla, not our own output, so re-runs are stable
        v_eff = StateFile.load(vanilla[sid].path).effective_state_buildings() if sid in vanilla else {}
        old_dock[sid] = int(float(v_eff["dockyard"].value)) if "dockyard" in v_eff else 0  # type: ignore[arg-type]
    coastal = {sid for sid, s in all_states.items() if any(p in coastal_provs for p in s.provinces)}

    by_owner: dict[str, list[int]] = {}
    for sid, s in all_states.items():
        by_owner.setdefault(s.owner or "", []).append(sid)

    result: dict[int, dict[str, int]] = {}
    problems: list[str] = []
    report: list[tuple[str, int, int, int, int]] = []
    for tag, sids in sorted(by_owner.items()):
        nat = nations[tag]
        civ, mil, dock = nat.totals()
        pop = sum(all_states[s].manpower or 0 for s in sids)
        gdp_pc = nat.gdp * 1e9 / pop if pop else 0
        weight = {s: slots_by_cat.get(all_states[s].category or "", 0) * math.sqrt(all_states[s].manpower or 0) for s in sids}
        room = dict(free)
        levels = {s: {k: 0 for k in MANAGED} for s in sids}

        # pinned values first; they count towards the national totals
        pinned = {s: overrides[s] for s in sids if s in overrides}
        for s, vals in pinned.items():
            for k in MANAGED:
                if k in vals:
                    levels[s][k] = vals[k]
                    room[s] = max(0, room[s] - vals[k])
        want = {"dockyard": dock, "arms_factory": mil, "industrial_complex": civ}
        for k in MANAGED:
            want[k] = max(0, want[k] - sum(v.get(k, 0) for v in pinned.values()))
        # short of slots: shrink every factory type alike instead of starving civilian industry
        capacity = sum(room[s] for s in sids if s not in pinned)
        wanted = sum(want.values())
        if wanted > capacity:
            problems.append(f"{tag}: {wanted} factories wanted but only {capacity} free slots; scaled down")
            scaled = {k: math.floor(v * capacity / wanted) for k, v in want.items()}
            spare = capacity - sum(scaled.values())
            for k in sorted(want, key=lambda k: want[k] * capacity / wanted - scaled[k], reverse=True)[:spare]:
                scaled[k] += 1
            want = scaled

        def free_for(kind: str) -> dict[int, int]:
            return {s: (0 if s in pinned and kind in pinned[s] else room[s]) for s in sids}

        dock_w = {s: (old_dock[s] + 0.5) * weight[s] if s in coastal else 0.0 for s in sids}
        for kind, w in (("dockyard", dock_w), ("arms_factory", weight), ("industrial_complex", weight)):
            alloc, left = allocate(want[kind], w, free_for(kind))
            for s, n in alloc.items():
                levels[s][kind] += n
                room[s] -= n
            if left:
                problems.append(f"{tag}: {left} {kind} did not fit in free building slots")
        for s in sids:
            infra = overrides.get(s, {}).get("infrastructure", infra_level(gdp_pc, all_states[s].category))
            result[s] = {**levels[s], "infrastructure": infra}
        report.append((tag, sum(levels[s]["industrial_complex"] for s in sids),
                       sum(levels[s]["arms_factory"] for s in sids), sum(levels[s]["dockyard"] for s in sids),
                       sum(free[s] for s in sids)))

    for p in problems:
        print(f"WARNING: {p}")
    if args.report or args.check:
        report.sort(key=lambda r: -(r[1] + r[2] + r[3]))
        print(f"{'tag':<5}{'civ':>6}{'mil':>6}{'dock':>6}{'free slots':>12}")
        for tag, c, m, d, fr in report:
            print(f"{tag:<5}{c:>6}{m:>6}{d:>6}{fr:>12}")
        print(f"world: {sum(r[1] for r in report)} civilian, {sum(r[2] for r in report)} military, "
              f"{sum(r[3] for r in report)} dockyards")
    if args.check:
        return 0

    caps = {n: d.max_level for n, d in defs.items() if d.max_level}
    changed = 0
    for sid, vals in result.items():
        sf = files[sid]
        if sf.history is None:
            print(f"WARNING: {all_states[sid].path.name} has no history block; skipped")
            continue
        for name, level in vals.items():
            sf.set_state_building(name, min(level, caps.get(name, level)))
        if sf.changed:
            sf.save()
            changed += 1
    print(f"wrote {changed} state files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
