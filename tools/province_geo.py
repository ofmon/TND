"""Province geography: pixel centroids, neighbours and approximate lat/lon.

Reads the vanilla map (provinces.bmp + definition.csv) and caches a table to
tools/cache/provinces.json. Real-world coordinates are estimated by fitting a
locally-weighted affine map from pixel space to lat/lon through the anchor
cities in tools/data/geo_anchors.csv (matched to provinces by their vanilla
victory-point names). Accuracy is roughly province-sized, which is enough to
decide which side of a modern border a province falls on - always sanity-check
borderline provinces by neighbours and names.

Usage:
    python tools/province_geo.py --hoi4 "E:/.../Hearts of Iron IV" [--check]
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from pathlib import Path

import numpy as np
from PIL import Image

TOOLS = Path(__file__).resolve().parent
CACHE = TOOLS / "cache" / "provinces.json"
ANCHORS = TOOLS / "data" / "geo_anchors.csv"
K_NEAREST = 8


def load_definitions(hoi4: Path) -> dict[tuple[int, int, int], dict]:
    defs = {}
    with open(hoi4 / "map" / "definition.csv", encoding="utf-8", errors="replace") as f:
        for row in csv.reader(f, delimiter=";"):
            if len(row) >= 7 and row[0].isdigit() and int(row[0]) > 0:
                rgb = (int(row[1]), int(row[2]), int(row[3]))
                defs[rgb] = {"id": int(row[0]), "type": row[4], "terrain": row[6]}
    return defs


def coastal_provinces(hoi4: Path) -> set[int]:
    """Land provinces flagged coastal in map/definition.csv (column 6)."""
    result = set()
    with open(hoi4 / "map" / "definition.csv", encoding="utf-8", errors="replace") as f:
        for row in csv.reader(f, delimiter=";"):
            if len(row) >= 6 and row[0].isdigit() and row[4] == "land" and row[5].strip().lower() == "true":
                result.add(int(row[0]))
    return result


def vp_names(*paths: Path) -> dict[int, str]:
    names: dict[int, str] = {}
    pat = re.compile(r'^\s*VICTORY_POINTS_(\d+)\s*:\s*\d*\s*"(.*)"')
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            m = pat.match(line)
            if m:
                names[int(m.group(1))] = m.group(2)
    return names


def province_id_map(hoi4: Path, defs: dict | None = None) -> np.ndarray:
    """2-D array of province ids, indexed [row, col] with row 0 at the map's top."""
    img = np.asarray(Image.open(hoi4 / "map" / "provinces.bmp").convert("RGB"), dtype=np.uint32)
    h, w, _ = img.shape
    packed = (img[:, :, 0] << 16) | (img[:, :, 1] << 8) | img[:, :, 2]
    defs = defs or load_definitions(hoi4)
    color_to_id = {(r << 16) | (g << 8) | b: d["id"] for (r, g, b), d in defs.items()}
    uniq, inverse = np.unique(packed, return_inverse=True)
    ids_for_uniq = np.array([color_to_id.get(int(c), 0) for c in uniq], dtype=np.int32)
    return ids_for_uniq[inverse].reshape(h, w)


def compute_raw(hoi4: Path) -> dict[int, dict]:
    defs = load_definitions(hoi4)
    idmap = province_id_map(hoi4, defs)
    h, w = idmap.shape

    ys, xs = np.indices((h, w))
    flat = idmap.ravel()
    n = int(flat.max()) + 1
    count = np.bincount(flat, minlength=n)
    sx = np.bincount(flat, weights=xs.ravel(), minlength=n)
    sy = np.bincount(flat, weights=ys.ravel(), minlength=n)

    # neighbours: horizontally/vertically adjacent pixels with different ids
    pairs = set()
    for a, b in ((idmap[:, :-1], idmap[:, 1:]), (idmap[:-1, :], idmap[1:, :])):
        mask = a != b
        stacked = np.stack([a[mask], b[mask]], axis=1)
        stacked.sort(axis=1)
        for p, q in np.unique(stacked, axis=0):
            pairs.add((int(p), int(q)))
    neighbours: dict[int, set[int]] = {}
    for p, q in pairs:
        neighbours.setdefault(p, set()).add(q)
        neighbours.setdefault(q, set()).add(p)

    by_id = {d["id"]: d for d in defs.values()}
    table = {}
    for pid, d in by_id.items():
        if pid >= n or count[pid] == 0:
            continue
        table[pid] = {
            "id": pid,
            "type": d["type"],
            "terrain": d["terrain"],
            "x": round(sx[pid] / count[pid], 1),
            "y": round(sy[pid] / count[pid], 1),
            "pixels": int(count[pid]),
            "neighbours": sorted(neighbours.get(pid, set()) - {0}),
        }
    return table


def load_anchors(names: dict[int, str], table: dict[int, dict]) -> list[tuple[str, float, float, float, float]]:
    by_name: dict[str, int] = {}
    for pid, name in names.items():
        if pid in table:
            by_name.setdefault(name.lower(), pid)
    anchors = []
    with open(ANCHORS, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            pid = by_name.get(row["name"].lower())
            if pid is None:
                continue
            p = table[pid]
            anchors.append((row["name"], p["x"], p["y"], float(row["lat"]), float(row["lon"])))
    return anchors


def fit(anchors, x: float, y: float, exclude: int | None = None) -> tuple[float, float]:
    pts = np.array([(a[1], a[2]) for a in anchors])
    lat = np.array([a[3] for a in anchors])
    lon = np.array([a[4] for a in anchors])
    d = np.hypot(pts[:, 0] - x, pts[:, 1] - y)
    if exclude is not None:
        d[exclude] = np.inf
    idx = np.argsort(d)[:K_NEAREST]
    wts = 1.0 / np.maximum(d[idx], 1.0) ** 2
    A = np.column_stack([np.ones(len(idx)), pts[idx, 0] - x, pts[idx, 1] - y])
    W = np.sqrt(wts)[:, None]
    # regularise the slopes toward zero so sparse anchor sets degrade to IDW
    reg = np.diag([0.0, 1e-3, 1e-3])
    AtA = (A * W).T @ (A * W) + reg
    coef_lat = np.linalg.solve(AtA, (A * W).T @ (lat[idx] * W[:, 0]))
    coef_lon = np.linalg.solve(AtA, (A * W).T @ (lon[idx] * W[:, 0]))
    return float(coef_lat[0]), float(coef_lon[0])


def build(hoi4: Path, repo_root: Path) -> dict[int, dict]:
    table = compute_raw(hoi4)
    names = vp_names(
        hoi4 / "localisation" / "english" / "victory_points_l_english.yml",
        repo_root / "localisation" / "replace" / "victory_points_l_english.yml",
    )
    anchors = load_anchors(names, table)
    for pid, p in table.items():
        lat, lon = fit(anchors, p["x"], p["y"])
        p["lat"], p["lon"] = round(lat, 2), round(lon, 2)
        if pid in names:
            p["name"] = names[pid]
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(table), encoding="utf-8")
    return table


def load(hoi4: Path | None = None) -> dict[int, dict]:
    if not CACHE.exists():
        if hoi4 is None:
            raise FileNotFoundError(f"{CACHE} missing; run province_geo.py --hoi4 ... first")
        build(hoi4, TOOLS.parent)
    return {int(k): v for k, v in json.loads(CACHE.read_text(encoding="utf-8")).items()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hoi4", type=Path, default=os.environ.get("HOI4_PATH"), required=os.environ.get("HOI4_PATH") is None)
    ap.add_argument("--check", action="store_true", help="print leave-one-out error for each anchor")
    args = ap.parse_args()
    table = build(args.hoi4, TOOLS.parent)
    print(f"{len(table)} provinces cached to {CACHE}")
    if args.check:
        names = vp_names(args.hoi4 / "localisation" / "english" / "victory_points_l_english.yml")
        anchors = load_anchors(names, table)
        errs = []
        for i, a in enumerate(anchors):
            lat, lon = fit(anchors, a[1], a[2], exclude=i)
            err_km = np.hypot((lat - a[3]) * 111, (lon - a[4]) * 111 * np.cos(np.radians(a[3])))
            errs.append((err_km, a[0]))
        errs.sort(reverse=True)
        print(f"{len(anchors)} anchors; median leave-one-out error {np.median([e for e, _ in errs]):.0f} km")
        for e, name in errs[:10]:
            print(f"  {name}: {e:.0f} km")
    return 0


if __name__ == "__main__":
    sys.exit(main())
