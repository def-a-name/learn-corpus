"""验证词法查询并将其编译为安全的 SQLite FTS5 MATCH 表达式。"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from src.retrieval.text import is_cjk_scalar


MAX_QUERY_SCALARS = 256
MAX_QUERY_BYTES = 1024
MIN_ANCHORS = 1
MAX_ANCHORS = 6

_RESERVED_WORDS = frozenset({"near", "not", "or"})
_BIDI_OVERRIDE_OR_ISOLATE = frozenset(
    chr(codepoint)
    for codepoint in (*range(0x202A, 0x202F), *range(0x2066, 0x206A))
)
_RAW_FTS_CHARACTERS = frozenset({'"', "*", "(", ")", "{", "}", "^"})


class QueryValidationError(ValueError):
    """当公开查询无法在隐藏 FTS5 语法的前提下编译时抛出。"""

    def __init__(self, message: str, *, details: dict[str, object] | None = None):
        super().__init__(message)
        self.details = details


@dataclass(frozen=True)
class CompiledQuery:
    normalized_query: str
    dedupe_key: str
    anchors: tuple[str, ...]
    match_expression: str
    anchor_count: int
    clause_count: int


def _validate_raw_query(query: str) -> str:
    if not isinstance(query, str):
        raise QueryValidationError(
            "query must be a string", details={"reason": "wrong_type", "expected": "string"},
        )
    if len(query) > MAX_QUERY_SCALARS:
        raise QueryValidationError(
            "query exceeds Unicode scalar limit",
            details={
                "reason": "unicode_scalar_limit", "maximum": MAX_QUERY_SCALARS,
                "actual": len(query),
            },
        )
    try:
        encoded = query.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise QueryValidationError(
            "query contains an invalid Unicode scalar",
            details={"reason": "invalid_unicode_scalar"},
        ) from exc
    if len(encoded) > MAX_QUERY_BYTES:
        raise QueryValidationError(
            "query exceeds UTF-8 byte limit",
            details={
                "reason": "utf8_byte_limit", "maximum": MAX_QUERY_BYTES,
                "actual": len(encoded),
            },
        )
    for value in query:
        codepoint = ord(value)
        if codepoint <= 0x1F or 0x7F <= codepoint <= 0x9F:
            raise QueryValidationError(
                "query contains a control character",
                details={"reason": "control_character"},
            )
        if value in _BIDI_OVERRIDE_OR_ISOLATE:
            raise QueryValidationError(
                "query contains a bidi override or isolate",
                details={"reason": "bidi_control"},
            )
    if any(value in query for value in _RAW_FTS_CHARACTERS):
        raise QueryValidationError(
            "query contains raw FTS5 syntax", details={"reason": "raw_fts_syntax"},
        )
    if ":" in query.replace("::", ""):
        raise QueryValidationError(
            "query contains a column-filter separator",
            details={"reason": "column_filter_separator"},
        )
    normalized = unicodedata.normalize("NFC", query).strip()
    if not normalized:
        raise QueryValidationError("query is empty", details={"reason": "empty_query"})
    return normalized


def _component_kind(value: str) -> str | None:
    if is_cjk_scalar(value):
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
        raise QueryValidationError(
            f"query contains reserved FTS5 word: {value}",
            details={"reason": "reserved_fts_word"},
        )
    return f"{folded}*"


def query_dedupe_key(query: str) -> str:
    normalized = _validate_raw_query(query)
    return " ".join(normalized.split()).casefold()


def compile_lexical_query(query: str) -> CompiledQuery:
    """编译一个由调用方给出的 1～6 anchor 查询，不做语义扩展。"""

    normalized = _validate_raw_query(query)
    anchors = tuple(re.split(r"\s+", normalized))
    if not MIN_ANCHORS <= len(anchors) <= MAX_ANCHORS:
        raise QueryValidationError(
            f"query must contain between {MIN_ANCHORS} and {MAX_ANCHORS} anchors",
            details={
                "reason": "anchor_count", "minimum": MIN_ANCHORS,
                "maximum": MAX_ANCHORS, "actual": len(anchors),
            },
        )

    clauses: list[str] = []
    for index, anchor in enumerate(anchors):
        components = _anchor_components(anchor)
        if not components:
            raise QueryValidationError(
                "query anchor contains no searchable term",
                details={"reason": "anchor_not_searchable", "anchor_index": index},
            )
        clauses.extend(_compile_component(kind, value) for kind, value in components)
    return CompiledQuery(
        normalized_query=normalized,
        dedupe_key=" ".join(normalized.split()).casefold(),
        anchors=anchors,
        match_expression=" AND ".join(clauses),
        anchor_count=len(anchors),
        clause_count=len(clauses),
    )
