"""One-time migration: rebase history/states onto the installed vanilla version.

The mod replaces history/states, so its state files must match the game's map
exactly: every land province in exactly one state. The mod's files were a mix
of an older game version (~1.15, 982 states) and newer vanilla copies, which
left provinces unassigned or double-assigned under 1.19.

For every vanilla state this script:
  1. resolves leftover git conflict markers (see ``resolve_conflicts``);
  2. picks the mod's version of the state, preferring a modded file over a
     verbatim vanilla copy when a state ID is duplicated;
  3. keeps the mod file as-is if its province list matches vanilla; otherwise
     starts from the vanilla file (correct provinces and province-keyed
     buildings) and carries the mod's owner/cores/manpower/category across.

A CSV report of every decision is written to tools/reports/rebase_report.csv;
rows with ``action=rebased`` need their data reviewed (the state's area changed).

Usage:
    python tools/rebase_states.py --hoi4 "E:/.../Hearts of Iron IV" [--dry-run]
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from pathlib import Path

import clausewitz as cw
import states as st
from state_file import StateFile

REPORT_DIR = Path(__file__).resolve().parent / "reports"

_HUNK_RE = re.compile(
    r"^<<<<<<< [^\n]*\n(?P<ours>.*?)^=======\n(?P<theirs>.*?)^>>>>>>> [^\n]*\n",
    re.S | re.M,
)


def resolve_conflicts(text: str) -> str:
    """Resolve merge hunks in favour of the modern-data branch ("theirs").

    In the f3cafaf/9026c29 merge, "ours" (HEAD) is a re-copied vanilla file and
    "theirs" holds J-Skinner588's modern populations/VPs. The only hunks where
    theirs is empty are newer vanilla additions (DLC ``allowed`` blocks), so an
    empty side yields to the other.
    """

    def pick(m: re.Match[str]) -> str:
        theirs = m.group("theirs")
        return theirs if theirs.strip() else m.group("ours")

    return _HUNK_RE.sub(pick, text)


def _normalise(text: str) -> str:
    text = re.sub(r"#[^\n]*", "", text)
    return " ".join(text.split())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hoi4", type=Path, default=os.environ.get("HOI4_PATH"), required=os.environ.get("HOI4_PATH") is None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    vanilla_dir = args.hoi4 / "history" / "states"
    vanilla = {s.id: s for s in st.load_all(vanilla_dir)}

    # 1. resolve conflicts in place
    mod_texts: dict[Path, str] = {}
    for path in st.state_files():
        raw = cw.read_text(path)
        fixed = resolve_conflicts(raw)
        mod_texts[path] = fixed
        if fixed != raw and not args.dry_run:
            path.write_text(fixed, encoding="utf-8", newline="\n")

    mod_by_id: dict[int, list[st.State]] = {}
    for path, text in mod_texts.items():
        s = st.load_state(path, text)
        if s.parse_error:
            print(f"ERROR: cannot parse {path.name} after conflict resolution: {s.parse_error}")
            return 1
        mod_by_id.setdefault(s.id, []).append(s)

    report: list[dict[str, object]] = []
    keep: set[Path] = set()

    for sid in sorted(vanilla):
        v = vanilla[sid]
        v_text = cw.read_text(v.path)
        candidates = mod_by_id.get(sid, [])
        # prefer a file the team actually edited over a verbatim vanilla copy
        modded = [c for c in candidates if _normalise(mod_texts[c.path]) != _normalise(v_text)]
        chosen = (modded or candidates or [None])[0]
        for dropped in candidates:
            if dropped is not chosen:
                report.append({"id": sid, "file": dropped.path.name, "action": "dropped_duplicate", "note": f"kept {chosen.path.name if chosen else ''}"})

        if chosen is None:
            target = st.MOD_STATES_DIR / v.path.name
            if not args.dry_run:
                StateFile(v_text, target).save()
            keep.add(target)
            report.append({"id": sid, "file": target.name, "action": "added_from_vanilla", "note": ""})
            continue

        if set(chosen.provinces) == set(v.provinces):
            keep.add(chosen.path)
            report.append({"id": sid, "file": chosen.path.name, "action": "kept", "note": ""})
            continue

        # province list changed in vanilla: take vanilla's structure, carry mod data over
        sf = StateFile(v_text, chosen.path)
        notes = []
        if chosen.owner:
            sf.set_ownership(chosen.owner, chosen.cores, chosen.controller, chosen.claims)
        if chosen.manpower is not None and chosen.manpower != v.manpower:
            sf.set_scalar("manpower", str(chosen.manpower))
            notes.append(f"manpower {v.manpower}->{chosen.manpower}")
        if chosen.category and chosen.category != v.category:
            sf.set_scalar("state_category", chosen.category)
            notes.append(f"category {v.category}->{chosen.category}")
        # keep the team's victory points for provinces still in this state
        kept_vps = {p: val for p, val in chosen.victory_points.items() if p in v.provinces and v.victory_points.get(p) != val}
        if kept_vps:
            sf.set_victory_points(kept_vps)
            notes.append(f"vps {kept_vps}")
        added = sorted(set(v.provinces) - set(chosen.provinces))
        removed = sorted(set(chosen.provinces) - set(v.provinces))
        notes.append(f"provinces +{added} -{removed}")
        if not args.dry_run:
            sf.save()
        keep.add(chosen.path)
        report.append({"id": sid, "file": chosen.path.name, "action": "rebased", "note": "; ".join(notes)})

    stale = [p for p in mod_texts if p not in keep]
    for p in stale:
        if not any(r["file"] == p.name for r in report):
            report.append({"id": "", "file": p.name, "action": "removed_not_in_vanilla", "note": ""})
        if not args.dry_run:
            p.unlink()

    REPORT_DIR.mkdir(exist_ok=True)
    with open(REPORT_DIR / "rebase_report.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["id", "file", "action", "note"])
        w.writeheader()
        w.writerows(report)

    counts: dict[str, int] = {}
    for r in report:
        counts[str(r["action"])] = counts.get(str(r["action"]), 0) + 1
    print(counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
