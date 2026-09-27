"""Audit the leader data for gaps: missing countries and slots, generic portraits,
placeholder party names, weak matches and expired characters.

Usage:
    python tools/audit_leaders.py            # summary plus every finding
    python tools/audit_leaders.py --csv out  # also write findings to a CSV
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

import build_leaders as bl
import clausewitz as cw
import states as st

# party values that describe an office or situation rather than naming a real party
PLACEHOLDER = re.compile(
    r"(?i)\b(presiden|prime minister|independent|non-?partisan|no party|none|government|"
    r"administration|office|crown|monarch|royal|dynasty|house of|closest|sidelined|exile|"
    r"former|faction|military|armed forces|junta|opposition|movement leader|activist|tbd|unknown)\b|\?")
WEAK = re.compile(r"(?i)closest|weak|not a |isn't|not actually|stand-?in|no real|nominal")


def character_expiry(chars: dict[str, tuple[Path, bool]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for cid, (path, is_leader) in chars.items():
        if not is_leader:
            continue
        for top in cw.parse_file(path):
            if top.key == "characters" and top.is_block:
                for c in top.value:  # type: ignore[union-attr]
                    if c.key == cid and c.is_block:
                        role = cw.first(c.value, "country_leader")  # type: ignore[arg-type]
                        if role is not None and role.is_block:
                            out[cid] = cw.scalar(role.value, "expire") or ""  # type: ignore[arg-type]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", type=Path)
    args = ap.parse_args()

    rows, errors = bl.load_rows()
    if errors:
        print("\n".join(errors))
        return 1
    chars = bl.existing_characters()
    sprites = bl.working_sprites()
    expiry = character_expiry(chars)
    notes: dict[str, str] = {}
    for path in sorted(bl.DATA.glob("*.csv")):
        with open(path, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                notes[r["character_id"].strip()] = (r.get("notes") or "")

    findings: list[tuple[str, str, str, str]] = []  # tag, slot, kind, detail
    by_tag: dict[str, dict[str, bl.Leader]] = defaultdict(dict)
    for r in rows:
        by_tag[r.tag][r.slot] = r

    owners = sorted({s.owner for s in st.load_all() if s.owner})
    for tag in owners:
        if tag not in by_tag:
            findings.append((tag, "-", "no_leaders", "country owns states but has no leader rows"))
            continue
        for slot in bl.SLOTS:
            r = by_tag[tag].get(slot)
            if r is None:
                findings.append((tag, slot, "missing_slot", "no leader for this ideology"))
                continue
            if r.id in chars:
                ok = r.dds.exists() or any(s in sprites for s in (f"GFX_portrait_{r.id}_large",))
                # reused characters: portrait resolved by build_leaders (tnd sprite) or their own sprite
                text = cw.read_text(chars[r.id][0])
                m = re.search(rf"{re.escape(r.id)}\s*=\s*\{{.*?large\s*=\s*(\S+)", text, re.S)
                ok = r.dds.exists() or (m is not None and m.group(1) in sprites)
                exp = expiry.get(r.id, "")
                if exp and st.parse_date(exp.strip('"')) and st.parse_date(exp.strip('"')) < st.START_DATE:
                    findings.append((tag, slot, "expired", f"{r.id} expires {exp}"))
            else:
                ok = r.dds.exists()
            if not ok:
                findings.append((tag, slot, "no_portrait", f"{r.name} ({r.id}) shows a generic portrait"))
            if not r.party:
                findings.append((tag, slot, "no_party", f"{r.name}: party is blank (1936 party name shows)"))
            elif PLACEHOLDER.search(r.party):
                findings.append((tag, slot, "placeholder_party", f"{r.name}: party '{r.party}'"))
            if WEAK.search(notes.get(r.id, "")):
                findings.append((tag, slot, "weak_match", f"{r.name}: {notes[r.id][:90]}"))

    kinds = defaultdict(int)
    for f in findings:
        kinds[f[2]] += 1
    print(f"{len(owners)} countries, {len(rows)} leaders")
    for k, n in sorted(kinds.items(), key=lambda kv: -kv[1]):
        print(f"  {k}: {n}")
    for f in findings:
        print(f"{f[0]} {f[1]:<11} {f[2]:<18} {f[3]}")
    if args.csv:
        with open(args.csv, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["tag", "slot", "kind", "detail"])
            w.writerows(findings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
