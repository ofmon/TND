"""Automated in-game smoke test for TND.

Launches HOI4 with the repo mod plus a tiny logging helper mod, starts the
2000 bookmark through the real UI, and checks the result:

  * no crash dump, game reaches the main menu and loads the mod (Active Mod Count)
  * the game starts on 2000.1.1 and the helper's on_startup logs every country's
    leader and ruling party, which are compared with tools/data/leaders/*.csv
  * every state id in history/states exists in game (the helper logs them)
  * screenshots of the politics screen for sample countries are saved for review

The helper mod is generated in <HOI4 docs>/mod/tnd_smoketest and enabled only
for the test run; dlc_load.json is restored afterwards.

WARNING: drives the mouse and keyboard on the primary monitor while it runs.

Usage:
    python tools/smoke_test.py [--sample LBA USA ...] [--keep-running]
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import json
import os
import re
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

import states as st

HOI4 = Path(os.environ.get("HOI4_PATH", r"E:\SteamLibrary\steamapps\common\Hearts of Iron IV"))
DOCS = next(p for p in (Path.home() / "OneDrive" / "Documents", Path.home() / "Documents")
            if (p / "Paradox Interactive" / "Hearts of Iron IV").is_dir()) / "Paradox Interactive" / "Hearts of Iron IV"
LOGS = DOCS / "logs"
OUT = Path(__file__).resolve().parent / "reports" / "smoke"
HELPER = "tnd_smoketest"
MARK = "TNDTEST"

user32 = ctypes.WinDLL("user32", use_last_error=True)
try:  # real pixels for screenshots and clicks on scaled displays
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except OSError:
    pass

# --- input -------------------------------------------------------------------

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE, MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, MOUSEEVENTF_ABSOLUTE = 0x1, 0x2, 0x4, 0x8000
KEYEVENTF_KEYUP, KEYEVENTF_UNICODE, KEYEVENTF_SCANCODE = 0x2, 0x4, 0x8


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _U(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


def _send(*inputs: INPUT) -> None:
    arr = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def screen_size() -> tuple[int, int]:
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def click(x: float, y: float, pause: float = 0.6) -> None:
    """Left-click at a fraction (0..1) of the primary screen."""
    ax, ay = int(x * 65535), int(y * 65535)
    mv = INPUT(INPUT_MOUSE, _U(mi=MOUSEINPUT(ax, ay, 0, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, 0, 0)))
    _send(mv)
    time.sleep(0.15)
    _send(INPUT(INPUT_MOUSE, _U(mi=MOUSEINPUT(ax, ay, 0, MOUSEEVENTF_LEFTDOWN | MOUSEEVENTF_ABSOLUTE, 0, 0))))
    time.sleep(0.05)
    _send(INPUT(INPUT_MOUSE, _U(mi=MOUSEINPUT(ax, ay, 0, MOUSEEVENTF_LEFTUP | MOUSEEVENTF_ABSOLUTE, 0, 0))))
    time.sleep(pause)


def key(scan: int, pause: float = 0.3) -> None:
    """Press a key by hardware scan code (games read scan codes)."""
    _send(INPUT(INPUT_KEYBOARD, _U(ki=KEYBDINPUT(0, scan, KEYEVENTF_SCANCODE, 0, 0))))
    time.sleep(0.05)
    _send(INPUT(INPUT_KEYBOARD, _U(ki=KEYBDINPUT(0, scan, KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP, 0, 0))))
    time.sleep(pause)


def type_text(text: str) -> None:
    for ch in text:
        code = ord(ch)
        _send(INPUT(INPUT_KEYBOARD, _U(ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE, 0, 0))),
              INPUT(INPUT_KEYBOARD, _U(ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0))))
        time.sleep(0.03)


SC_ESC, SC_ENTER, SC_GRAVE, SC_Q, SC_F1 = 0x01, 0x1C, 0x29, 0x10, 0x3B


def screenshot(name: str) -> Path:
    from PIL import ImageGrab

    OUT.mkdir(parents=True, exist_ok=True)
    w, h = screen_size()
    path = OUT / f"{name}.png"
    ImageGrab.grab(bbox=(0, 0, w, h)).save(path)
    return path


def _grab_centre():
    from PIL import ImageGrab

    w, h = screen_size()
    return ImageGrab.grab(bbox=(int(w * 0.36), int(h * 0.25), int(w * 0.64), int(h * 0.75))).convert("L").resize((200, 200))


def click_until_changed(x: float, y: float, wait: float, tries: int = 4, threshold: float = 2.5) -> bool:
    """Click and confirm the screen reacted (first clicks can be eaten while the window takes focus)."""
    from PIL import ImageChops, ImageStat

    for _ in range(tries):
        focus_game()
        before = _grab_centre()
        click(x, y, 0.3)
        end = time.time() + wait
        while time.time() < end:
            time.sleep(1)
            if ImageStat.Stat(ImageChops.difference(before, _grab_centre())).mean[0] > threshold:
                time.sleep(1.5)  # let the new screen settle
                return True
    return False


def menu_visible() -> bool:
    """The main menu's dark button panel is on screen (the loading screen there is bright)."""
    from PIL import ImageGrab, ImageStat

    w, h = screen_size()
    box = (int(w * 0.445), int(h * 0.35), int(w * 0.555), int(h * 0.62))
    return ImageStat.Stat(ImageGrab.grab(bbox=box).convert("L")).mean[0] < 120


def wait_for_menu(timeout: float = 300) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        focus_game()
        if menu_visible():
            time.sleep(3)  # let the fade-in finish
            return True
        if not game_running():
            return False
        time.sleep(3)
    return False


def focus_game() -> None:
    hwnd = user32.FindWindowW(None, "Hearts of Iron IV")
    if hwnd:
        user32.ShowWindow(hwnd, 9)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.5)


def console(command: str) -> None:
    key(SC_GRAVE, 0.6)
    type_text(command)
    key(SC_ENTER, 1.0)
    key(SC_GRAVE, 0.6)


# --- setup -------------------------------------------------------------------

HELPER_ON_ACTIONS = """on_actions = {
	on_startup = {
		effect = {
			log = "%(m)s START [GetDateText]"
			every_country = {
				log = "%(m)s LEADER [This.GetTag]|[This.GetLeader]|[This.GetRulingParty]"
			}
			every_state = {
				log = "%(m)s STATE [This.GetID]|[This.Owner.GetTag]"
			}
			log = "%(m)s END"
		}
	}
}
""" % {"m": MARK}


def install_helper() -> None:
    root = DOCS / "mod" / HELPER
    (root / "common" / "on_actions").mkdir(parents=True, exist_ok=True)
    (root / "common" / "on_actions" / "zz_tnd_smoketest.txt").write_text(HELPER_ON_ACTIONS, encoding="utf-8")
    desc = f'version="1"\nname="TND smoke test helper"\nsupported_version="1.19.*"\n'
    (root / "descriptor.mod").write_text(desc, encoding="utf-8")
    (DOCS / "mod" / f"{HELPER}.mod").write_text(desc + f'path="{root.as_posix()}"\n', encoding="utf-8")


def set_mods(mods: list[str]) -> str:
    path = DOCS / "dlc_load.json"
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(json.dumps({"enabled_mods": mods, "disabled_dlcs": []}), encoding="utf-8")
    return old


def wait_for(pattern: str, file: str = "game.log", timeout: float = 300) -> bool:
    rx = re.compile(pattern)
    end = time.time() + timeout
    while time.time() < end:
        p = LOGS / file
        if p.exists() and rx.search(p.read_text(encoding="utf-8", errors="replace")):
            return True
        if not game_running():
            return False
        time.sleep(2)
    return False


def game_running() -> bool:
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq hoi4.exe", "/NH"], capture_output=True, text=True).stdout
    return "hoi4.exe" in out


def crashes_since(t0: float) -> list[Path]:
    d = DOCS / "crashes"
    return [p for p in d.iterdir() if p.stat().st_mtime > t0] if d.exists() else []


# --- checks ------------------------------------------------------------------

def expected_leaders() -> dict[str, tuple[str, str]]:
    """tag -> (ruling leader display name, slot)."""
    out: dict[str, tuple[str, str]] = {}
    for path in sorted((Path(__file__).resolve().parent / "data" / "leaders").glob("*.csv")):
        with open(path, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                if (r.get("ruling") or "").strip().lower() == "yes":
                    out[r["tag"].strip()] = (r["name"].strip(), r["slot"].strip())
    return out


def _norm(name: str) -> str:
    import unicodedata

    s = unicodedata.normalize("NFKD", name.strip()).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z]+", " ", s).strip()


def check_log(results: list[str]) -> bool:
    text = (LOGS / "game.log").read_text(encoding="utf-8", errors="replace")
    ok = True
    if f"{MARK} START" not in text:
        results.append("FAIL: helper on_startup never ran (game did not start?)")
        return False
    leaders = dict(re.findall(rf"{MARK} LEADER (\w+)\|(.*)", text))
    expected = expected_leaders()
    wrong = []
    for tag, (name, slot) in sorted(expected.items()):
        got = leaders.get(tag)
        if got is None:
            continue  # tag does not exist in game (no states)
        got_name, _, got_party = got.partition("|")
        if _norm(got_name) != _norm(name):
            wrong.append(f"{tag}: leader is {got_name.strip()!r}, expected {name!r} (ruling party {got_party.strip()})")
    results.append(f"{'PASS' if not wrong else 'FAIL'}: ruling leaders match data for "
                   f"{len([t for t in expected if t in leaders]) - len(wrong)}/{len([t for t in expected if t in leaders])} countries")
    results += [f"  {w}" for w in wrong]
    ok &= not wrong

    in_game = {int(i) for i in re.findall(rf"{MARK} STATE (\d+)\|", text)}
    files = {s.id for s in st.load_all()}
    missing = sorted(files - in_game)
    results.append(f"{'PASS' if not missing else 'FAIL'}: {len(in_game)} states in game, {len(files)} in history/states"
                   + (f"; missing {missing[:20]}" if missing else ""))
    ok &= not missing
    return ok


def check_errors(results: list[str]) -> None:
    text = (LOGS / "error.log").read_text(encoding="utf-8", errors="replace")
    patterns = {
        "portrait/texture": r"(?i)(texture|portrait|\.dds)",
        "state/province": r"(?i)(history/states|province \d+|strategic region)",
        "characters": r"(?i)(common/characters|recruit_character)",
        "localisation": r"(?i)localisation",
    }
    lines = text.splitlines()
    results.append(f"INFO: error.log has {len(lines)} lines")
    for label, rx in patterns.items():
        hits = [l for l in lines if re.search(rx, l)]
        results.append(f"INFO: {len(hits)} {label} errors" + (f" e.g. {hits[0][hits[0].find(']: ') + 3:][:160]}" if hits else ""))


# --- flow --------------------------------------------------------------------

def start_bookmark(tag: str) -> None:
    """Main menu -> Single Player -> New Game -> 2000 scenario -> first country -> START.

    Coordinates are fractions of the screen, calibrated on the vanilla 1.19 UI at 16:9.
    The country actually played is switched afterwards with the `tag` console command.
    """
    focus_game()
    screenshot("01_menu")
    steps = [
        ("Single Player", 0.5, 0.368, 8),
        ("New Game", 0.5, 0.416, 15),
        ("1 January 2000 scenario", 0.652, 0.417, 6),
        ("Select Scenario", 0.558, 0.65, 25),
        ("Select Country", 0.533, 0.732, 40),
    ]
    for i, (label, x, y, wait) in enumerate(steps, start=2):
        if not click_until_changed(x, y, wait):
            screenshot(f"99_click_{label.replace(' ', '_')}")
            raise RuntimeError(f"UI did not react to '{label}'")
        screenshot(f"{i:02d}_after_{label.replace(' ', '_')}")
    click(0.944, 0.969)          # START (the screen then loads; checked via the helper's log)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sample", nargs="*", default=["LBA", "USA", "SOV", "PRC", "RAJ", "BRA"])
    ap.add_argument("--keep-running", action="store_true")
    ap.add_argument("--menu-only", action="store_true", help="stop after the main menu (no UI driving)")
    args = ap.parse_args()

    install_helper()
    old_mods = set_mods(["mod/tnd_dev.mod", f"mod/{HELPER}.mod"])
    t0 = time.time()
    results: list[str] = []
    try:
        # the previous run's logs would satisfy the waits before the new game rewrites them
        for name in ("game.log", "error.log", "system.log", "setup.log"):
            try:
                (LOGS / name).write_text("", encoding="utf-8")
            except OSError:
                pass
        subprocess.Popen([str(HOI4 / "hoi4.exe")], cwd=str(HOI4))
        if not wait_for(r"Active Mod Count: [1-9]", "system.log", 180):
            results.append("FAIL: mod not active (system.log shows no active mods)")
        if not wait_for(r"Executing History from 2\.1\.1\.1 to\s+1936", timeout=300):
            results.append("FAIL: game did not reach the main menu")
            return report(results, t0)
        if not wait_for_menu():
            screenshot("99_no_menu")
            results.append("FAIL: main menu never appeared (see tools/reports/smoke/99_no_menu.png)")
            return report(results, t0)
        results.append("PASS: reached main menu")
        if args.menu_only:
            return report(results, t0)
        try:
            start_bookmark(args.sample[0])
        except RuntimeError as exc:
            results.append(f"FAIL: {exc} (see tools/reports/smoke/99_click_*.png)")
            return report(results, t0)
        if not wait_for(rf"{MARK} END", timeout=300):
            screenshot("99_stuck")
            results.append("FAIL: game did not start (see tools/reports/smoke/99_stuck.png)")
            return report(results, t0)
        time.sleep(8)
        screenshot("09_ingame")  # leaders/states are verified from the helper's log, not the UI
        check_log(results)
        check_errors(results)
    finally:
        if not args.keep_running and game_running():
            subprocess.run(["taskkill", "/IM", "hoi4.exe", "/F"], capture_output=True)
        (DOCS / "dlc_load.json").write_text(old_mods or json.dumps({"enabled_mods": ["mod/tnd_dev.mod"], "disabled_dlcs": []}), encoding="utf-8")
    return report(results, t0)


def report(results: list[str], t0: float) -> int:
    crashes = crashes_since(t0)
    if crashes:
        results.append(f"FAIL: crash dump(s): {[c.name for c in crashes]}")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "report.txt").write_text("\n".join(results) + "\n", encoding="utf-8")
    print("\n".join(results))
    return 1 if any(r.startswith("FAIL") for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
