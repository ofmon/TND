"""Minimal parser for Paradox (Clausewitz) script files.

Parses into a list of ``Entry(key, op, value)`` where ``value`` is either a
string scalar or a nested ``list`` of entries. Bare values inside a block
(e.g. ``provinces = { 1 2 3 }``) become entries with ``key=None``.

This is a *reader*. Edits to game files are done surgically on the source
text (see ``state_file.py``) so comments and formatting are preserved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Union

Value = Union[str, list["Entry"]]

_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<comment>\#[^\n]*)
  | (?P<string>"(?:[^"\\]|\\.)*")
  | (?P<op><=|>=|!=|\?=|=|<|>)
  | (?P<brace>[{}])
  | (?P<word>[^\s{}=<>!#"]+)
    """,
    re.VERBOSE,
)


class ParseError(ValueError):
    pass


@dataclass
class Entry:
    key: str | None
    op: str | None
    value: Value
    # Character offsets into the source text: the whole entry, and its value.
    # For a block value, ``value_start``/``value_end`` span the braces.
    start: int = 0
    end: int = 0
    value_start: int = 0
    value_end: int = 0

    @property
    def is_block(self) -> bool:
        return isinstance(self.value, list)


@dataclass
class Token:
    kind: str
    text: str
    line: int
    start: int
    end: int


def tokenize(text: str) -> Iterator[Token]:
    pos = 0
    line = 1
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise ParseError(f"line {line}: unexpected character {text[pos]!r}")
        kind = m.lastgroup
        tok = m.group()
        if kind not in ("ws", "comment"):
            yield Token(kind, tok, line, m.start(), m.end())  # type: ignore[arg-type]
        line += tok.count("\n")
        pos = m.end()


def parse(text: str) -> list[Entry]:
    tokens = list(tokenize(text))
    i = 0

    def parse_block(depth: int) -> tuple[list[Entry], int]:
        """Parse entries until the matching '}'; return them and the '}' end offset."""
        nonlocal i
        entries: list[Entry] = []
        while i < len(tokens):
            t = tokens[i]
            if t.text == "}":
                if depth == 0:
                    raise ParseError(f"line {t.line}: unmatched '}}'")
                i += 1
                return entries, t.end
            if t.text == "{":
                # anonymous block, e.g. inside lists of lists
                i += 1
                body, end = parse_block(depth + 1)
                entries.append(Entry(None, None, body, t.start, end, t.start, end))
                continue
            if t.kind == "op":
                raise ParseError(f"line {t.line}: unexpected operator {t.text!r}")
            # t is a key or a bare value
            if i + 1 < len(tokens) and tokens[i + 1].kind == "op":
                op = tokens[i + 1].text
                i += 2
                if i >= len(tokens):
                    raise ParseError(f"line {t.line}: missing value after {t.text}{op}")
                v = tokens[i]
                if v.text == "{":
                    i += 1
                    body, end = parse_block(depth + 1)
                    entries.append(Entry(_unquote(t.text), op, body, t.start, end, v.start, end))
                elif v.text == "}" or v.kind == "op":
                    raise ParseError(f"line {v.line}: missing value after {t.text}{op}")
                else:
                    i += 1
                    entries.append(Entry(_unquote(t.text), op, _unquote(v.text), t.start, v.end, v.start, v.end))
            else:
                i += 1
                entries.append(Entry(None, None, _unquote(t.text), t.start, t.end, t.start, t.end))
        if depth != 0:
            raise ParseError("unexpected end of file: unclosed '{'")
        return entries, len(text)

    return parse_block(0)[0]


def parse_file(path: Path) -> list[Entry]:
    return parse(read_text(path))


def read_text(path: Path) -> str:
    # utf-8-sig strips a BOM if present; history files may be either
    return path.read_text(encoding="utf-8-sig", errors="replace")


def _unquote(tok: str) -> str:
    if len(tok) >= 2 and tok[0] == tok[-1] == '"':
        return tok[1:-1]
    return tok


# --- query helpers -----------------------------------------------------------

def find(entries: list[Entry], key: str) -> list[Entry]:
    return [e for e in entries if e.key == key]


def first(entries: list[Entry], key: str) -> Entry | None:
    for e in entries:
        if e.key == key:
            return e
    return None


def scalar(entries: list[Entry], key: str, default: str | None = None) -> str | None:
    e = first(entries, key)
    if e is None or e.is_block:
        return default
    return e.value  # type: ignore[return-value]


def bare_values(entries: list[Entry]) -> list[str]:
    return [e.value for e in entries if e.key is None and not e.is_block]  # type: ignore[misc]
