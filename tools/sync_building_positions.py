"""Regenerate map/buildings.txt (3D building positions) for the current states.

Each line of buildings.txt is ``state_id;building;x;y;z;rotation;adjacent_sea``.

1. Start from the installed vanilla file; re-key every position that lies in a
   province our splits moved to another state.
2. Fill gaps. Vanilla gives every state the same set of positions (see
   REQUIRED_* below). A state missing one - typically a new state, or one whose
   positions all sat in a province it lost - makes the game log MAP_ERRORs and
   crash in the strategic air system, so missing positions are synthesised on a
   pixel inside the right province, at the heightmap's height.

Run after apply_state_data.py.

Usage:
    python tools/sync_building_positions.py --hoi4 "E:/.../Hearts of Iron IV"
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image

import province_geo
import states as st

OUT = st.REPO_ROOT / "map" / "buildings.txt"

# positions every state has in vanilla (type -> count per state)
REQUIRED_PER_STATE = {
    "arms_factory": 6,
    "industrial_complex": 6,
    "anti_air_building": 3,
    "air_base": 1,
    "synthetic_refinery": 1,
    "nuclear_reactor_spawn": 1,
    "rocket_site_spawn": 1,
    "radar_station": 1,
    "fuel_silo": 1,
    "stronghold_network": 1,
}
# one position per land province
REQUIRED_PER_PROVINCE = ("bunker", "supply_node", "special_project_facility_spawn")
# one position per coastal province, facing an adjacent sea province
REQUIRED_PER_COASTAL_PROVINCE = ("naval_base_spawn", "coastal_bunker", "naval_supply_hub", "naval_headquarters", "floating_harbor")
# positioned over the sea rather than inside the coastal province
OFFSHORE_KINDS = {"floating_harbor"}
# one per coastal state
REQUIRED_PER_COASTAL_STATE = {"dockyard": 1}

# y = HEIGHT_SCALE * heightmap + HEIGHT_OFFSET (fitted on vanilla positions, residual ~0.2)
HEIGHT_SCALE, HEIGHT_OFFSET = 0.0997, 0.018


class PositionMaker:
    def __init__(self, hoi4: Path, idmap: np.ndarray):
        self.idmap = idmap
        self.h, self.w = idmap.shape
        self.heightmap = np.asarray(Image.open(hoi4 / "map" / "heightmap.bmp").convert("L"), dtype=float)
        self._pixels: dict[int, np.ndarray] = {}

    def pixels(self, pid: int) -> np.ndarray:
        """(row, col) pixels of a province, sorted so picks are deterministic."""
        if pid not in self._pixels:
            rows, cols = np.nonzero(self.idmap == pid)
            self._pixels[pid] = np.stack([rows, cols], axis=1)
        return self._pixels[pid]

    def line(self, sid: int, kind: int | str, row: int, col: int, sea: int = 0) -> str:
        z = self.h - 1 - row  # bitmap is top-down, map z bottom-up
        y = HEIGHT_SCALE * self.heightmap[row, col] + HEIGHT_OFFSET
        return f"{sid};{kind};{col + 0.5:.2f};{y:.2f};{z + 0.5:.2f};0.00;{sea}"

    def inner_pixel(self, pid: int, toward: tuple[float, float] | None = None) -> tuple[int, int]:
        px = self.pixels(pid)
        target = np.array(toward) if toward is not None else px.mean(axis=0)
        i = int(np.argmin(((px - target) ** 2).sum(axis=1)))
        return int(px[i][0]), int(px[i][1])

    def spread(self, provinces: list[int], n: int) -> list[tuple[int, int]]:
        """n pixels spread evenly over the given provinces."""
        px = np.concatenate([self.pixels(p) for p in provinces])
        idx = np.linspace(0, len(px) - 1, n + 2)[1:-1].astype(int)
        return [(int(px[i][0]), int(px[i][1])) for i in idx]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hoi4", type=Path, default=os.environ.get("HOI4_PATH"), required=os.environ.get("HOI4_PATH") is None)
    args = ap.parse_args()

    idmap = province_geo.province_id_map(args.hoi4)
    height, width = idmap.shape
    geo = province_geo.load(args.hoi4)
    coastal = province_geo.coastal_provinces(args.hoi4)
    all_states = st.load_all()
    vanilla_state = {p: s.id for s in st.load_all(args.hoi4 / "history" / "states") for p in s.provinces}
    current_state = {p: s.id for s in all_states for p in s.provinces}

    def province_at(x: float, z: float) -> int | None:
        col, row = int(x), height - 1 - int(z)
        return int(idmap[row, col]) if 0 <= col < width and 0 <= row < height else None

    # 1. vanilla positions, re-keyed where a split moved their province
    out_lines: list[str] = []
    rekeyed = 0
    have_state: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    have_prov: dict[tuple[int, str], set[int]] = defaultdict(set)
    for line in (args.hoi4 / "map" / "buildings.txt").read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split(";")
        if len(parts) >= 5 and parts[0].isdigit():
            pid = province_at(float(parts[2]), float(parts[4]))
            sid = int(parts[0])
            if pid is not None and vanilla_state.get(pid) == sid and current_state.get(pid, sid) != sid:
                parts[0] = str(current_state[pid])
                line = ";".join(parts)
                rekeyed += 1
            sid = int(parts[0])
            have_state[sid][parts[1]] += 1
            if pid is not None:
                have_prov[(sid, parts[1])].add(pid)
        out_lines.append(line)

    # 2. fill positions each state is missing
    maker = PositionMaker(args.hoi4, idmap)
    added = 0
    for s in sorted(all_states, key=lambda s: s.id):
        land = [p for p in s.provinces if geo.get(p, {}).get("type") == "land" and len(maker.pixels(p))]
        if not land:
            continue
        for kind, need in REQUIRED_PER_STATE.items():
            missing = need - have_state[s.id][kind]
            if missing > 0:
                for row, col in maker.spread(land, missing):
                    out_lines.append(maker.line(s.id, kind, row, col))
                    added += 1
        for kind in REQUIRED_PER_PROVINCE:
            for p in land:
                if p not in have_prov[(s.id, kind)]:
                    out_lines.append(maker.line(s.id, kind, *maker.inner_pixel(p)))
                    added += 1
        coast = [p for p in land if p in coastal]
        for p in coast:
            seas = [n for n in geo[p]["neighbours"] if geo.get(n, {}).get("type") == "sea"]
            if not seas:
                continue
            sea = seas[0]
            toward = (geo[sea]["y"], geo[sea]["x"])  # cache holds image-space (row=y, col=x) centroids
            for kind in REQUIRED_PER_COASTAL_PROVINCE:
                # vanilla puts floating harbours out at sea, so they never lie inside the
                # land province; only synthesise them for a coastal state that has none
                if kind in OFFSHORE_KINDS and have_state[s.id][kind]:
                    continue
                if p not in have_prov[(s.id, kind)]:
                    out_lines.append(maker.line(s.id, kind, *maker.inner_pixel(p, toward), sea=sea))
                    added += 1
        if coast:
            for kind, need in REQUIRED_PER_COASTAL_STATE.items():
                if have_state[s.id][kind] < need:
                    out_lines.append(maker.line(s.id, kind, *maker.inner_pixel(coast[0])))
                    added += 1

    OUT.write_text("\n".join(out_lines) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(st.REPO_ROOT)}: {len(out_lines)} lines, "
          f"{rekeyed} positions re-keyed, {added} missing positions synthesised")
    return 0


if __name__ == "__main__":
    sys.exit(main())
