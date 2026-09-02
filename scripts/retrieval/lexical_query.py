"""Validate lexical queries and compile them into safe SQLite FTS5 MATCH expressions."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


MAX_QUERY_SCALARS = 256
MAX_QUERY_BYTES = 1024
MIN_ANCHORS = 1
MAX_ANCHORS = 3

_RESERVED_WORDS = frozenset({"near", "not", "or"})
_BIDI_OVERRIDE_OR_ISOLATE = frozenset(
    chr(codepoint)
    for codepoint in (*range(0x202A, 0x202F), *range(0x2066, 0x206A))
)
_RAW_FTS_CHARACTERS = frozenset({'"', "*", "(", ")", "{", "}", "^"})


class QueryValidationError(ValueError):
    """A public query cannot be compiled without exposing FTS5 syntax."""


@dataclass(frozen=True)
class CompiledQuery:
    normalized_query: str
    dedupe_key: str
    anchors: tuple[str, ...]
    match_expression: str
    anchor_count: int
    clause_count: int


def _is_cjk_scalar(value: str) -> bool:
    codepoint = ord(value)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x323AF
        or 0x3040 <= codepoint <= 0x30FF
        or 0x31F0 <= codepoint <= 0x31FF
        or 0xFF66 <= codepoint <= 0xFF9D
        or 0x1100 <= codepoint <= 0x11FF
        or 0x3130 <= codepoint <= 0x318F
        or 0xA960 <= codepoint <= 0xA97F
        or 0xAC00 <= codepoint <= 0xD7FF
    )


def normalize_index_text(text: str | None) -> str:
    """Insert boundaries around CJK scalars for the search-only FTS representation."""

    if text is None:
        return ""
    normalized = unicodedata.normalize("NFC", text)
    output: list[str] = []
    for value in normalized:
        if _is_cjk_scalar(value):
            output.extend((" ", value, " "))
        else:
            output.append(value)
    return " ".join("".join(output).split())


def _validate_raw_query(query: str) -> str:
    if not isinstance(query, str):
        raise QueryValidationError("query must be a string")
    if len(query) > MAX_QUERY_SCALARS:
        raise QueryValidationError("query exceeds Unicode scalar limit")
    try:
        encoded = query.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise QueryValidationError("query contains an invalid Unicode scalar") from exc
    if len(encoded) > MAX_QUERY_BYTES:
        raise QueryValidationError("query exceeds UTF-8 byte limit")
    for value in query:
        codepoint = ord(value)
        if codepoint <= 0x1F or 0x7F <= codepoint <= 0x9F:
            raise QueryValidationError("query contains a control character")
        if value in _BIDI_OVERRIDE_OR_ISOLATE:
            raise QueryValidationError("query contains a bidi override or isolate")
    if any(value in query for value in _RAW_FTS_CHARACTERS):
        raise QueryValidationError("query contains raw FTS5 syntax")
    if ":" in query.replace("::", ""):
        raise QueryValidationError("query contains a column-filter separator")
    normalized = unicodedata.normalize("NFC", query).strip()
    if not normalized:
        raise QueryValidationError("query is empty")
    return normalized


def _component_kind(value: str) -> str | None:
    if _is_cjk_scalar(value):
        return "cjk"
    category = unicodedata.category(value)
    if category[0] in {"L", "N"} or category[0] == "M":
        return "word"
    return None


def _anchor_components(anchor: str) -> tuple[tuple[str, str], ...]:
    components: list[tuple[str, str]] = []
    current: list[str] = []
    current_kind: str | None = None

    def finish() -> None:
        nonlocal current, current_kind
        if current and current_kind:
            components.append((current_kind, "".join(current)))
        current = []
        current_kind = None

    for value in anchor:
        kind = _component_kind(value)
        if kind is None:
            finish()
            continue
        if kind != current_kind:
            finish()
            current_kind = kind
        current.append(value)
    finish()
    return tuple(components)


def _compile_component(kind: str, value: str) -> str:
    if kind == "cjk":
        return f'"{" ".join(value)}"'
    folded = value.casefold()
    if folded in _RESERVED_WORDS:
        raise QueryValidationError(f"query contains reserved FTS5 word: {value}")
    return f"{folded}*"


def query_dedupe_key(query: str) -> str:
    normalized = _validate_raw_query(query)
    return " ".join(normalized.split()).casefold()


def compile_lexical_query(query: str) -> CompiledQuery:
    """Compile one caller-authored 1-3 anchor query without semantic expansion."""

    normalized = _validate_raw_query(query)
    anchors = tuple(re.split(r"\s+", normalized))
    if not MIN_ANCHORS <= len(anchors) <= MAX_ANCHORS:
        raise QueryValidationError(
            f"query must contain between {MIN_ANCHORS} and {MAX_ANCHORS} anchors"
        )

    clauses: list[str] = []
    for anchor in anchors:
        components = _anchor_components(anchor)
        if not components:
            raise QueryValidationError("query anchor contains no searchable term")
        clauses.extend(_compile_component(kind, value) for kind, value in components)
    return CompiledQuery(
        normalized_query=normalized,
        dedupe_key=" ".join(normalized.split()).casefold(),
        anchors=anchors,
        match_expression=" AND ".join(clauses),
        anchor_count=len(anchors),
        clause_count=len(clauses),
    )
