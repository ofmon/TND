"""Load HOI4 state history files into a simple data model."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import clausewitz as cw

REPO_ROOT = Path(__file__).resolve().parent.parent
MOD_STATES_DIR = REPO_ROOT / "history" / "states"

# Game start date of the mod's default bookmark (common/bookmarks/Dawn.txt).
START_DATE = (2000, 1, 1)

STATE_CATEGORIES = (
    "wasteland",
    "enclave",
    "tiny_island",
    "pastoral",
    "small_island",
    "rural",
    "town",
    "large_town",
    "city",
    "large_city",
    "metropolis",
    "megalopolis",
)

# y.m.d with an optional hour (e.g. 1936.11.9.1); the hour is ignored for ordering
_DATE_RE = re.compile(r"^(\d{1,4})\.(\d{1,2})\.(\d{1,2})(?:\.\d{1,2})?$")


@dataclass
class State:
    id: int
    path: Path
    name: str | None = None
    manpower: int | None = None
    category: str | None = None
    owner: str | None = None
    controller: str | None = None
    cores: list[str] = field(default_factory=list)
    claims: list[str] = field(default_factory=list)
    provinces: list[int] = field(default_factory=list)
    victory_points: dict[int, float] = field(default_factory=dict)
    parse_error: str | None = None


def parse_date(key: str | None) -> tuple[int, int, int] | None:
    if key is None:
        return None
    m = _DATE_RE.match(key)
    return tuple(int(g) for g in m.groups()) if m else None  # type: ignore[return-value]


def _apply_history(state: State, entries: list[cw.Entry]) -> None:
    for e in entries:
        if e.key == "owner":
            state.owner = e.value  # type: ignore[assignment]
        elif e.key == "controller":
            state.controller = e.value  # type: ignore[assignment]
        elif e.key == "add_core_of" and e.value not in state.cores:
            state.cores.append(e.value)  # type: ignore[arg-type]
        elif e.key == "remove_core_of" and e.value in state.cores:
            state.cores.remove(e.value)  # type: ignore[arg-type]
        elif e.key == "add_claim_by" and e.value not in state.claims:
            state.claims.append(e.value)  # type: ignore[arg-type]
        elif e.key == "remove_claim_by" and e.value in state.claims:
            state.claims.remove(e.value)  # type: ignore[arg-type]
        elif e.key == "victory_points" and e.is_block:
            vals = cw.bare_values(e.value)  # type: ignore[arg-type]
            for prov, value in zip(vals[0::2], vals[1::2]):
                try:
                    state.victory_points[int(prov)] = float(value)
                except ValueError:
                    pass


def load_state(path: Path, text: str | None = None) -> State:
    """Load a state from ``path`` (or from ``text``, attributed to ``path``)."""
    m = re.match(r"^(\d+)", path.name)
    state = State(id=int(m.group(1)) if m else -1, path=path)
    try:
        root = cw.parse(text if text is not None else cw.read_text(path))
    except cw.ParseError as exc:
        state.parse_error = str(exc)
        return state

    block = cw.first(root, "state")
    if block is None or not block.is_block:
        state.parse_error = "no top-level 'state = { ... }' block"
        return state
    body: list[cw.Entry] = block.value  # type: ignore[assignment]

    sid = cw.scalar(body, "id")
    if sid is not None and sid.isdigit():
        state.id = int(sid)
    state.name = cw.scalar(body, "name")
    mp = cw.scalar(body, "manpower")
    if mp is not None:
        try:
            state.manpower = int(float(mp))
        except ValueError:
            pass
    state.category = cw.scalar(body, "state_category")

    prov = cw.first(body, "provinces")
    if prov is not None and prov.is_block:
        state.provinces = [int(v) for v in cw.bare_values(prov.value) if v.isdigit()]  # type: ignore[arg-type]

    hist = cw.first(body, "history")
    if hist is not None and hist.is_block:
        hist_body: list[cw.Entry] = hist.value  # type: ignore[assignment]
        _apply_history(state, [e for e in hist_body if parse_date(e.key) is None])
        dated = sorted(
            (parse_date(e.key), e) for e in hist_body if parse_date(e.key) and e.is_block
        )
        for date, e in dated:
            if date <= START_DATE:
                _apply_history(state, e.value)  # type: ignore[arg-type]
    if state.controller is None:
        state.controller = state.owner
    return state


def state_files(directory: Path = MOD_STATES_DIR) -> list[Path]:
    return sorted(p for p in directory.glob("*.txt"))


def load_all(directory: Path = MOD_STATES_DIR) -> list[State]:
    return [load_state(p) for p in state_files(directory)]


def defined_tags(repo_root: Path = REPO_ROOT) -> set[str]:
    tags: set[str] = set()
    for path in (repo_root / "common" / "country_tags").glob("*.txt"):
        for e in cw.parse_file(path):
            if e.key and len(e.key) == 3 and not e.is_block:
                tags.add(e.key)
    return tags
