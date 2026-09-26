"""One-time migration: remove vanilla 1936-scenario effects from state histories.

Vanilla state files carry effects that only make sense for the 1936 start
(e.g. Samoa's demilitarised zone, Dutch-era compliance in Indonesia, Ethiopia's
feudal-development modifiers). They are wrong for the mod's 2000 start.

Only the explicitly listed effects are removed, so anything the team adds to a
state history later is untouched.

Usage:
    python tools/strip_1936_effects.py [--dry-run]
"""

from __future__ import annotations

import argparse
import sys

import clausewitz as cw
import states as st
from state_file import StateFile

# 1936 dynamic modifiers from vanilla focus trees/decisions
SCENARIO_MODIFIERS = {
    "ETH_state_development_dynamic_modifier",
    "ETH_state_decentralization_dynamic_modifier",
    "COG_state_loyal_to_belgium_modifier",
}


def is_scenario_effect(e: cw.Entry) -> bool:
    if e.key == "set_compliance":
        return True  # occupation mechanic; no 2000 state starts occupied
    if e.key == "set_demilitarized_zone" and e.value == "yes":
        return True
    if e.key == "add_dynamic_modifier" and e.is_block:
        return cw.scalar(e.value, "modifier") in SCENARIO_MODIFIERS  # type: ignore[arg-type]
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    for path in st.state_files():
        sf = StateFile.load(path)
        hist = sf.history
        if hist is None:
            continue
        doomed = []
        for e in hist.value:  # type: ignore[union-attr]
            if is_scenario_effect(e):
                doomed.append(e)
            elif st.parse_date(e.key) and e.is_block and st.parse_date(e.key) <= st.START_DATE:
                # dated blocks only ever *undo* DMZs etc.; drop matching effects there too
                doomed.extend(c for c in e.value if is_scenario_effect(c) or c.key == "set_demilitarized_zone")  # type: ignore[union-attr]
        if not doomed:
            continue
        print(f"{path.name}: removing {', '.join(sorted({d.key for d in doomed if d.key}))}")
        if not args.dry_run:
            sf.remove(doomed)
            sf.save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
