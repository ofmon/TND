"""Export every state with its geography to JSON, grouped into world regions.

Output: tools/cache/regions/<region>.json - one file per region, each a list of
states with id, localised name, current owner/cores, manpower, category, and
every province's approximate lat/lon, VP name and neighbouring states. Used as
the research input for tools/data/states/*.csv.

Usage:
    python tools/export_state_table.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import province_geo
import states as st

TOOLS = Path(__file__).resolve().parent
OUT_DIR = TOOLS / "cache" / "regions"

# (region, lat_min, lat_max, lon_min, lon_max) - first match wins
REGIONS = [
    # the Caribbean box must precede north_america, which overlaps it
    ("caribbean_central_america", 7, 30, -95, -55),
    ("north_america", 7, 90, -170, -30),  # incl. Greenland
    ("north_america", 15, 90, -190, -170),  # Aleutians/Midway (map wraps past -180)
    ("north_west_africa", -30, 0, -30, 0),  # Ascension, St Helena
    ("south_america", -60, 7, -95, -30),
    ("western_europe", 35, 72, -32, 15),
    ("northeast_europe", 48.5, 72, 15, 41),  # Poland, Czechia, Baltics, Belarus, N. Ukraine, W. Russia
    ("balkans_danube", 34, 48.5, 15, 41),  # Hungary southwards, S. Ukraine, Turkey (European side)
    ("russia", 41, 90, 41, 180),
    ("russia", 60, 90, -180, -160),
    ("middle_east_central_asia", 12, 45, 25, 75),
    ("south_asia", 5, 37, 60, 98),
    ("east_asia", 18, 55, 73, 150),
    ("southeast_asia_oceania", -60, 30, 90, 180),
    ("southeast_asia_oceania", -60, 30, -180, -100),
    ("north_west_africa", 0, 38, -30, 25),
    ("east_south_africa", -40, 20, 8, 75),
]


def load_state_names(repo_root: Path) -> dict[str, str]:
    names: dict[str, str] = {}
    pat = re.compile(r'^\s*(STATE_\d+)\s*:\s*\d*\s*"(.*)"')
    for path in sorted((repo_root / "localisation").rglob("*state_names*_l_english.yml")):
        for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            m = pat.match(line)
            if m:
                names[m.group(1)] = m.group(2)
    return names


def region_for(lat: float, lon: float) -> str:
    for name, la0, la1, lo0, lo1 in REGIONS:
        if la0 <= lat <= la1 and lo0 <= lon <= lo1:
            return name
    return "unassigned"


def main() -> int:
    geo = province_geo.load()
    all_states = st.load_all()
    loc = load_state_names(st.REPO_ROOT)
    prov_state = {p: s.id for s in all_states for p in s.provinces}

    regions: dict[str, list[dict]] = {}
    for s in all_states:
        provs = []
        neighbour_states: set[int] = set()
        weights = 0.0
        lat_sum = lon_sum = 0.0
        for pid in s.provinces:
            g = geo.get(pid)
            if g is None:
                continue
            nb = sorted({prov_state[n] for n in g["neighbours"] if n in prov_state and prov_state[n] != s.id})
            neighbour_states.update(nb)
            entry = {"id": pid, "lat": g["lat"], "lon": g["lon"]}
            if "name" in g:
                entry["name"] = g["name"]
            if pid in s.victory_points:
                entry["vp"] = s.victory_points[pid]
            if g["type"] != "land":
                entry["type"] = g["type"]
            if nb:
                entry["borders_states"] = nb
            provs.append(entry)
            w = g["pixels"]
            weights += w
            lat_sum += g["lat"] * w
            lon_sum += g["lon"] * w
        lat_c = lat_sum / weights if weights else 0.0
        lon_c = lon_sum / weights if weights else 0.0
        record = {
            "id": s.id,
            "name": loc.get(f"STATE_{s.id}", s.name),
            "file": s.path.name,
            "owner": s.owner,
            "cores": s.cores,
            "claims": s.claims,
            "manpower": s.manpower,
            "category": s.category,
            "centroid": [round(lat_c, 2), round(lon_c, 2)],
            "neighbour_states": sorted(neighbour_states),
            "provinces": provs,
        }
        regions.setdefault(region_for(lat_c, lon_c), []).append(record)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for old in OUT_DIR.glob("*.json"):
        old.unlink()
    for name, recs in sorted(regions.items()):
        (OUT_DIR / f"{name}.json").write_text(json.dumps(recs, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{name}: {len(recs)} states")
    return 0


if __name__ == "__main__":
    sys.exit(main())
