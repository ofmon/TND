"""Install a snapshot of this repo as a separate "TND (test)" mod in the HOI4 launcher.

Copies the mod content (not tools/, .git, caches) into
<Documents>/Paradox Interactive/Hearts of Iron IV/mod/tnd_test/ and writes a
matching tnd_test.mod descriptor, so the test build can be enabled in the
launcher independently of the working repo. Re-running replaces the snapshot.

Usage:
    python tools/install_test_mod.py [--mod-dir "<...>/Hearts of Iron IV/mod"]
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

import states as st

NAME = "tnd_test"
DISPLAY_NAME = "TND (test build)"
CONTENT = ["common", "events", "gfx", "history", "interface", "localisation", "map"]
IGNORE = shutil.ignore_patterns("desktop.ini", ".vs", "*.pyc", "__pycache__")


def default_mod_dir() -> Path:
    candidates = [
        Path.home() / "OneDrive" / "Documents",
        Path.home() / "Documents",
    ]
    for docs in candidates:
        mod = docs / "Paradox Interactive" / "Hearts of Iron IV" / "mod"
        if mod.is_dir():
            return mod
    raise FileNotFoundError("could not find the HOI4 mod folder; pass --mod-dir")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mod-dir", type=Path)
    args = ap.parse_args()
    mod_dir = args.mod_dir or default_mod_dir()

    target = mod_dir / NAME
    if target.exists():
        # only ever replace a folder this script created
        if not (target / "descriptor.mod").read_text(encoding="utf-8").count(DISPLAY_NAME):
            print(f"refusing to overwrite {target}: not a {DISPLAY_NAME} install")
            return 1
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for folder in CONTENT:
        src = st.REPO_ROOT / folder
        if src.is_dir():
            shutil.copytree(src, target / folder, ignore=IGNORE)
    for f in ("thumbnail.png",):
        if (st.REPO_ROOT / f).exists():
            shutil.copy2(st.REPO_ROOT / f, target / f)

    descriptor = (st.REPO_ROOT / "descriptor.mod").read_text(encoding="utf-8")
    descriptor = re.sub(r'^name\s*=\s*".*"', f'name="{DISPLAY_NAME}"', descriptor, flags=re.M)
    descriptor = re.sub(r"^(path|remote_file_id)\s*=.*\n?", "", descriptor, flags=re.M)
    (target / "descriptor.mod").write_text(descriptor, encoding="utf-8")
    launcher_path = target.as_posix()
    (mod_dir / f"{NAME}.mod").write_text(descriptor.rstrip("\n") + f'\npath="{launcher_path}"\n', encoding="utf-8")
    print(f"installed {DISPLAY_NAME} to {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
