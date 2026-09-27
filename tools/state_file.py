"""Surgical, formatting-preserving edits to a single state history file.

Every edit re-parses the text afterwards, so offsets are always fresh. This is
cheap at state-file sizes and keeps the editing logic simple and correct.
"""

from __future__ import annotations

import re
from pathlib import Path

import clausewitz as cw
from states import START_DATE, parse_date

OWNERSHIP_KEYS = {
    "owner",
    "controller",
    "add_core_of",
    "remove_core_of",
    "add_claim_by",
    "remove_claim_by",
    "transfer_state_to",
    "set_province_controller",
    "set_state_controller",
}
# effects that, inside a country scope (`CHI = { ... }`), change who owns/controls land
_SCOPED_OWNERSHIP_EFFECTS = {"transfer_state", "set_state_owner", "set_province_controller", "set_state_controller"}
CONDITIONAL_KEYS = {"if", "else_if", "else"}
_TAG_RE = re.compile(r"^[A-Z][A-Z0-9]{2}$")


def _key(e: cw.Entry) -> str:
    # Paradox script keys are case-insensitive (vanilla uses both `if` and `IF`)
    return (e.key or "").lower()


def _is_ownership_effect(e: cw.Entry) -> bool:
    if _key(e) in OWNERSHIP_KEYS:
        return True
    # e.g. `CHI = { transfer_state = PREV }` or `CHI = { set_province_controller = 1125 }`
    if e.key and _TAG_RE.match(e.key) and e.is_block:
        return any(_key(c) in _SCOPED_OWNERSHIP_EFFECTS for c in e.value)  # type: ignore[union-attr]
    return False


class StateFileError(ValueError):
    pass


class StateFile:
    def __init__(self, text: str, path: Path | None = None):
        self.path = path
        self.text = text.replace("\r\n", "\n")
        self._original = self.text
        self._reparse()

    @property
    def changed(self) -> bool:
        """True if saving would alter the file (no-op edits don't count)."""
        return self.text != self._original and self.rendered() != self._original

    @classmethod
    def load(cls, path: Path) -> "StateFile":
        return cls(cw.read_text(path), path)

    def save(self, path: Path | None = None) -> None:
        target = path or self.path
        if target is None:
            raise StateFileError("no path to save to")
        # HOI4 reads history files as UTF-8; write without BOM, LF line endings
        target.write_text(self.rendered(), encoding="utf-8", newline="\n")

    def rendered(self) -> str:
        """The text as it will be saved: blank-line runs left by deletions collapsed."""
        return re.sub(r"\n(?:[ \t]*\n){2,}", "\n\n", self.text)

    # --- structure -----------------------------------------------------------

    def _reparse(self) -> None:
        self.root = cw.parse(self.text)
        state = cw.first(self.root, "state")
        if state is None or not state.is_block:
            raise StateFileError(f"{self.path}: no 'state = {{ ... }}' block")
        self.state = state

    @property
    def body(self) -> list[cw.Entry]:
        return self.state.value  # type: ignore[return-value]

    @property
    def history(self) -> cw.Entry | None:
        return cw.first(self.body, "history")

    def get(self, key: str) -> str | None:
        return cw.scalar(self.body, key)

    @property
    def provinces(self) -> list[int]:
        prov = cw.first(self.body, "provinces")
        if prov is None or not prov.is_block:
            return []
        return [int(v) for v in cw.bare_values(prov.value) if v.isdigit()]  # type: ignore[arg-type]

    # --- low-level text ops --------------------------------------------------

    def _replace(self, start: int, end: int, new: str) -> None:
        self.text = self.text[:start] + new + self.text[end:]
        self._reparse()

    def _line_span(self, start: int, end: int) -> tuple[int, int]:
        """Widen a span to whole lines if nothing else (but comments) shares them."""
        line_start = self.text.rfind("\n", 0, start) + 1
        line_end = self.text.find("\n", end)
        line_end = len(self.text) if line_end == -1 else line_end
        before = self.text[line_start:start]
        after = self.text[end:line_end]
        if before.strip() == "" and (after.strip() == "" or after.lstrip().startswith("#")):
            return line_start, min(line_end + 1, len(self.text))
        return start, end

    def _delete_entries(self, entries: list[cw.Entry]) -> None:
        spans = sorted({self._line_span(e.start, e.end) for e in entries}, reverse=True)
        text = self.text
        for s, e in spans:
            text = text[:s] + text[e:]
        self.text = text
        self._reparse()

    def remove(self, entries: list[cw.Entry]) -> None:
        """Delete entries (from this file's current parse), then drop emptied wrappers."""
        self._delete_entries(entries)
        self._remove_empty_conditionals()
        self._remove_empty_dated_blocks()

    def _indent_of(self, entry: cw.Entry) -> str:
        line_start = self.text.rfind("\n", 0, entry.start) + 1
        prefix = self.text[line_start:entry.start]
        return prefix if prefix.strip() == "" else ""

    def _child_indent(self, block: cw.Entry) -> str:
        children = block.value
        if isinstance(children, list) and children:
            ind = self._indent_of(children[0])
            if ind:
                return ind
        return self._indent_of(block) + "\t"

    def _insert_into_block(self, block: cw.Entry, lines: list[str], at_start: bool = True) -> None:
        indent = self._child_indent(block)
        chunk = "".join(f"{indent}{line}\n" for line in lines)
        if at_start:
            pos = block.value_start + 1  # just after '{'
            nl = self.text.find("\n", pos)
            # insert on the line after the opening brace when the brace ends its line
            if nl != -1 and self.text[pos:nl].strip() == "":
                self._replace(nl + 1, nl + 1, chunk)
            else:
                self._replace(pos, pos, "\n" + chunk)
        else:
            close = block.value_end - 1  # the '}'
            line_start = self.text.rfind("\n", 0, close) + 1
            if self.text[line_start:close].strip() == "":
                self._replace(line_start, line_start, chunk)
            else:
                self._replace(close, close, "\n" + chunk)

    # --- high-level edits ----------------------------------------------------

    def set_scalar(self, key: str, value: str, comment: str | None = None) -> None:
        """Set a top-level scalar in the state block (manpower, state_category, ...)."""
        rendered = f"{key} = {value}" + (f" # {comment}" if comment else "")
        existing = cw.find(self.body, key)
        if existing:
            e = existing[0]
            s, end = e.start, e.end
            # drop an old trailing comment so it can't contradict the new value
            line_end = self.text.find("\n", end)
            line_end = len(self.text) if line_end == -1 else line_end
            if self.text[end:line_end].lstrip().startswith("#"):
                end = line_end
            self._replace(s, end, rendered)
            if len(existing) > 1:
                self._delete_entries(cw.find(self.body, key)[1:])
            return
        anchor = cw.first(self.body, "name") or cw.first(self.body, "id")
        if anchor is None:
            self._insert_into_block(self.state, [rendered])
            return
        indent = self._indent_of(anchor) or "\t"
        line_end = self.text.find("\n", anchor.end)
        pos = len(self.text) if line_end == -1 else line_end + 1
        self._replace(pos, pos, f"{indent}{rendered}\n")

    def set_ownership(
        self,
        owner: str,
        cores: list[str],
        controller: str | None = None,
        claims: list[str] | None = None,
    ) -> None:
        """Replace all ownership as of START_DATE with the given values.

        Removes owner/controller/core/claim effects from the undated history and
        from dated history blocks up to START_DATE (later dated blocks are kept,
        since they are deliberate scripted changes), then writes a canonical set.
        """
        hist = self.history
        if hist is None:
            self._insert_into_block(self.state, ["history = {", "}"], at_start=False)
            hist = self.history
        assert hist is not None

        doomed: list[cw.Entry] = []

        def collect(entries: list[cw.Entry], in_conditional: bool = False) -> None:
            for e in entries:
                if _is_ownership_effect(e):
                    doomed.append(e)
                elif _key(e) in CONDITIONAL_KEYS and e.is_block:
                    collect(e.value, True)  # type: ignore[arg-type]
                elif not in_conditional:
                    d = parse_date(e.key)
                    if d and d <= START_DATE and e.is_block:
                        collect(e.value)  # type: ignore[arg-type]

        collect(hist.value)  # type: ignore[arg-type]
        if doomed:
            self._delete_entries(doomed)
        self._remove_empty_conditionals()

        lines = [f"owner = {owner}"]
        if controller and controller != owner:
            lines.append(f"controller = {controller}")
        lines += [f"add_core_of = {t}" for t in dict.fromkeys(cores)]
        lines += [f"add_claim_by = {t}" for t in dict.fromkeys(claims or [])]
        hist = self.history
        assert hist is not None
        self._insert_into_block(hist, lines)
        self._remove_empty_dated_blocks()

    def _remove_empty_conditionals(self) -> None:
        """Drop if/else blocks left holding nothing but their `limit`."""
        while True:
            hist = self.history
            if hist is None:
                return
            empty: list[cw.Entry] = []

            def scan(entries: list[cw.Entry]) -> None:
                for e in entries:
                    if not e.is_block:
                        continue
                    if _key(e) in CONDITIONAL_KEYS and all(_key(c) == "limit" for c in e.value):  # type: ignore[union-attr]
                        empty.append(e)
                    else:
                        scan(e.value)  # type: ignore[arg-type]

            scan(hist.value)  # type: ignore[arg-type]
            if not empty:
                return
            # removing an inner block can empty its parent, hence the loop
            self._delete_entries(empty)

    def _remove_empty_dated_blocks(self) -> None:
        hist = self.history
        if hist is None:
            return
        empty = [e for e in hist.value if parse_date(e.key) and e.is_block and not e.value]  # type: ignore[union-attr]
        if empty:
            self._delete_entries(empty)

    def set_provinces(self, provinces: list[int]) -> None:
        prov = cw.first(self.body, "provinces")
        body = " ".join(str(p) for p in provinces)
        if prov is None:
            self._insert_into_block(self.state, ["provinces = {", f"\t{body}", "}"], at_start=False)
            return
        indent = self._indent_of(prov) or "\t"
        self._replace(prov.value_start, prov.value_end, f"{{\n{indent}\t{body}\n{indent}}}")

    def take_state_building(self, name: str) -> int:
        """Remove a state-level building everywhere it's set up to START_DATE; return its level.

        Dated blocks after the undated history add to the level, so the total is
        the undated value plus every dated value up to the start date.
        """
        hist = self.history
        if hist is None:
            return 0
        found: list[cw.Entry] = []

        def scan(entries: list[cw.Entry]) -> None:
            for e in entries:
                if _key(e) == "buildings" and e.is_block:
                    found.extend(b for b in e.value if _key(b) == name and not b.is_block)  # type: ignore[union-attr]
                elif parse_date(e.key) and e.is_block and parse_date(e.key) <= START_DATE:
                    scan(e.value)  # type: ignore[arg-type]

        scan(hist.value)  # type: ignore[arg-type]
        total = sum(int(float(e.value)) for e in found)  # type: ignore[arg-type]
        if found:
            self.remove(found)
        return total

    def clamp_state_buildings(self, max_levels: dict[str, int]) -> list[str]:
        """Lower any state-level building entry above its maximum; return what changed."""
        changes: list[str] = []
        while True:
            hist = self.history
            if hist is None:
                return changes
            over = None

            def scan(entries: list[cw.Entry]) -> None:
                nonlocal over
                for e in entries:
                    if over is not None:
                        return
                    if _key(e) == "buildings" and e.is_block:
                        for b in e.value:  # type: ignore[union-attr]
                            cap = max_levels.get(b.key or "")
                            if cap is not None and not b.is_block and int(float(b.value)) > cap:  # type: ignore[arg-type]
                                over = (b, cap)
                                return
                    elif parse_date(e.key) and e.is_block and parse_date(e.key) <= START_DATE:
                        scan(e.value)  # type: ignore[arg-type]

            scan(hist.value)  # type: ignore[arg-type]
            if over is None:
                return changes
            entry, cap = over
            changes.append(f"{entry.key} {entry.value} -> {cap}")
            self._replace(entry.value_start, entry.value_end, str(cap))

    def effective_state_buildings(self) -> dict[str, cw.Entry]:
        """The entry that sets each state-level building on START_DATE.

        A dated history block's `buildings = { x = N }` sets the level (it does not
        add), so the last dated entry up to START_DATE wins over the undated one.
        """
        hist = self.history
        if hist is None:
            return {}
        effective: dict[str, cw.Entry] = {}

        def take(block: cw.Entry) -> None:
            for b in block.value:  # type: ignore[union-attr]
                if b.key and not b.key.isdigit() and not b.is_block:
                    effective[b.key] = b

        for e in hist.value:  # type: ignore[union-attr]
            if _key(e) == "buildings" and e.is_block:
                take(e)
        dated = sorted((parse_date(e.key), i, e) for i, e in enumerate(hist.value)  # type: ignore[union-attr, arg-type]
                       if parse_date(e.key) and e.is_block and parse_date(e.key) <= START_DATE)
        for _, _, e in dated:
            for sub in e.value:  # type: ignore[union-attr]
                if _key(sub) == "buildings" and sub.is_block:
                    take(sub)
        return effective

    def fit_shared_slots(self, shared: set[str], slots: int) -> list[str]:
        """Trim slot-sharing buildings (largest first) until they fit in `slots`.

        Real slot counts include country tech bonuses, so pass the budget the game
        reports ("<state> has too many buildings : -N" in error.log), not the
        category's base slots.
        """
        changes: list[str] = []
        while True:
            eff = {k: e for k, e in self.effective_state_buildings().items() if k in shared}
            levels = {k: int(float(e.value)) for k, e in eff.items()}  # type: ignore[arg-type]
            if sum(levels.values()) <= slots:
                return changes
            name = max(levels, key=lambda k: (levels[k], k))
            entry = eff[name]
            self._replace(entry.value_start, entry.value_end, str(levels[name] - 1))
            changes.append(f"{name} {levels[name]} -> {levels[name] - 1}")

    def set_state_building(self, name: str, level: int) -> None:
        """Make a state-level building's START_DATE level exactly ``level``.

        Edits the value in place when it's set once in the undated buildings block,
        so re-running with the same level leaves the file untouched.
        """
        hist = self.history
        if hist is None:
            raise StateFileError(f"{self.path}: no history block")
        blk = cw.first(hist.value, "buildings")  # type: ignore[arg-type]
        undated = [b for b in blk.value if _key(b) == name and not b.is_block] if blk is not None else []  # type: ignore[union-attr]
        # no dated block overrides it when the undated entry is the effective one
        if len(undated) == 1 and self.effective_state_buildings().get(name) is undated[0]:
            if level > 0:
                if int(float(undated[0].value)) != level:  # type: ignore[arg-type]
                    self._replace(undated[0].value_start, undated[0].value_end, str(level))
            else:
                self.remove(undated)
            return
        self.take_state_building(name)
        self.add_state_building(name, level)

    def add_state_building(self, name: str, level: int) -> None:
        """Add ``level`` to a state-level building in the undated history buildings block."""
        if level <= 0:
            return
        hist = self.history
        assert hist is not None
        blk = cw.first(hist.value, "buildings")  # type: ignore[arg-type]
        if blk is None:
            self._insert_into_block(hist, ["buildings = {", "}"], at_start=False)
            blk = cw.first(self.history.value, "buildings")  # type: ignore[union-attr, arg-type]
        assert blk is not None
        existing = cw.first(blk.value, name)  # type: ignore[arg-type]
        if existing is not None and not existing.is_block:
            new = int(float(existing.value)) + level  # type: ignore[arg-type]
            self._replace(existing.value_start, existing.value_end, str(new))
        else:
            self._insert_into_block(blk, [f"{name} = {level}"])

    def set_victory_points(self, vps: dict[int, float], names: dict[int, str] | None = None) -> None:
        """Replace the victory points of the given provinces with ``vps``."""
        self.extract_province_data(set(vps), include_buildings=False)
        names = names or {}
        lines = []
        for prov, value in vps.items():
            v = int(value) if float(value).is_integer() else value
            comment = f" # {names[prov]}" if prov in names else ""
            lines.append(f"victory_points = {{ {prov} {v} }}{comment}")
        self.add_province_data([], lines)

    def referenced_provinces(self) -> tuple[set[int], set[int]]:
        """Provinces referenced by (province building blocks, victory points) in the history."""
        blocks: set[int] = set()
        vps: set[int] = set()
        hist = self.history
        if hist is None:
            return blocks, vps

        def scan(entries: list[cw.Entry]) -> None:
            for e in entries:
                if _key(e) == "buildings" and e.is_block:
                    blocks.update(int(b.key) for b in e.value if b.key and b.key.isdigit() and b.is_block)  # type: ignore[union-attr]
                elif _key(e) == "victory_points" and e.is_block:
                    vals = cw.bare_values(e.value)  # type: ignore[arg-type]
                    vps.update(int(v) for v in vals[0::2] if v.isdigit())

        scan(hist.value)  # type: ignore[arg-type]
        return blocks, vps

    def extract_province_data(
        self, provinces: set[int], include_buildings: bool = True
    ) -> tuple[list[str], list[str]]:
        """Remove and return province-keyed building blocks and victory points.

        Returns (building_block_texts, victory_point_lines) for the given
        provinces so they can be re-inserted into another state.
        """
        buildings: list[str] = []
        vps: list[str] = []
        doomed: list[cw.Entry] = []
        hist = self.history
        if hist is None:
            return buildings, vps

        def scan(entries: list[cw.Entry]) -> None:
            for e in entries:
                if e.key == "buildings" and e.is_block and include_buildings:
                    for b in e.value:  # type: ignore[union-attr]
                        if b.key and b.key.isdigit() and int(b.key) in provinces and b.is_block:
                            buildings.append(self.text[b.start:b.end])
                            doomed.append(b)
                elif e.key == "victory_points" and e.is_block:
                    vals = cw.bare_values(e.value)  # type: ignore[arg-type]
                    if vals and vals[0].isdigit() and int(vals[0]) in provinces:
                        vps.append(self.text[e.start:e.end])
                        doomed.append(e)

        scan(hist.value)  # type: ignore[arg-type]
        if doomed:
            self._delete_entries(doomed)
        return buildings, vps

    def add_province_data(self, buildings: list[str], vps: list[str]) -> None:
        hist = self.history
        assert hist is not None
        if vps:
            self._insert_into_block(hist, vps, at_start=False)
        if buildings:
            hist = self.history
            assert hist is not None
            blk = cw.first(hist.value, "buildings")  # type: ignore[arg-type]
            if blk is None:
                self._insert_into_block(hist, ["buildings = {", "}"], at_start=False)
                hist = self.history
                assert hist is not None
                blk = cw.first(hist.value, "buildings")  # type: ignore[arg-type]
            assert blk is not None
            # normalise the moved blocks' indentation to a single line each
            flat = [" ".join(b.split()) for b in buildings]
            self._insert_into_block(blk, flat, at_start=False)
