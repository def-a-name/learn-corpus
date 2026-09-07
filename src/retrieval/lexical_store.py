"""搜索并读取一个已锁定的不可变词法 generation。"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from time import monotonic
from typing import Any, Sequence

from src.retrieval.contracts import (
    GenerationStatus,
    Item,
    ItemRelations,
    ReadResult,
    SearchResponse,
    SearchResult,
    SUPPORTED_SCOPES,
)
from src.retrieval.generation import (
    LexicalBuildError,
    open_immutable_database,
    validate_generation_artifact,
)
from src.retrieval.lexical_query import (
    CompiledQuery,
    QueryValidationError,
    compile_lexical_query,
)
from src.retrieval.text import estimate_evidence_tokens


CANDIDATE_LIMIT = 20
DEFAULT_RESULT_LIMIT = 8
MAX_RESULT_LIMIT = 20
MAX_QUERIES = 6
RRF_K = 60
SNIPPET_MAX_TOKENS = 160
SNIPPET_MAX_BYTES = 480
DEFAULT_READ_MAX_TOKENS = 800
MAX_READ_TOKENS = 800

_SCOPE_SET = frozenset(SUPPORTED_SCOPES)
_ITEM_ID = re.compile(r"^itm_[a-z2-7]{32}$")
_GENERATION = re.compile(r"^gen_[0-9a-f]{20}$")
_FENCE_START = re.compile(r"^ {0,3}(?P<marker>`{3,}|~{3,})")
_LIST_START = re.compile(r"^\s*(?:[-+*]|\d+[.)])[ \t]+")
_TABLE_DELIMITER = re.compile(
    r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$"
)

_ITEM_SELECT = """
SELECT item_id, scope, title, source_path, locator, evidence_role,
       provider, session_id, turn_index, role, part, body,
       token_estimate, relations_json
FROM items
WHERE item_id = ?
"""


class LexicalStoreError(RuntimeError):
    """可在传输边界稳定映射的运行时错误。"""

    code = "lexical_store_error"


class InvalidRequestError(LexicalStoreError):
    code = "invalid_request"


class GenerationMismatchError(LexicalStoreError):
    code = "generation_mismatch"


class ItemNotFoundError(LexicalStoreError):
    code = "item_not_found"


class IndexUnavailableError(LexicalStoreError):
    code = "index_unavailable"


class BudgetExceededError(LexicalStoreError):
    code = "budget_exceeded"


@dataclass(frozen=True)
class _Block:
    start: int
    end: int
    kind: str


@dataclass(frozen=True)
class _Snippet:
    text: str
    truncated_before: bool
    truncated_after: bool
    token_estimate: int


def _trim_span(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _fits(text: str, max_tokens: int, max_bytes: int | None = None) -> bool:
    return estimate_evidence_tokens(text) <= max_tokens and (
        max_bytes is None or len(text.encode("utf-8")) <= max_bytes
    )


def _line_spans(text: str, start: int, end: int) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    offset = start
    for line in text[start:end].splitlines(keepends=True):
        line_end = offset + len(line)
        trimmed_start, trimmed_end = _trim_span(text, offset, line_end)
        if trimmed_start < trimmed_end:
            spans.append((trimmed_start, trimmed_end))
        offset = line_end
    if not spans and start < end:
        spans.append(_trim_span(text, start, end))
    return [span for span in spans if span[0] < span[1]]


def _markdown_blocks(text: str) -> list[_Block]:
    line_spans: list[tuple[int, int, str]] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        end = offset + len(line)
        line_spans.append((offset, end, line.rstrip("\r\n")))
        offset = end
    if offset < len(text):
        line_spans.append((offset, len(text), text[offset:]))

    blocks: list[_Block] = []
    index = 0
    while index < len(line_spans):
        if not line_spans[index][2].strip():
            index += 1
            continue
        start_index = index
        line = line_spans[index][2]
        fence = _FENCE_START.match(line)
        if fence:
            kind = "code"
            marker = fence.group("marker")
            index += 1
            while index < len(line_spans):
                closing = _FENCE_START.match(line_spans[index][2])
                index += 1
                if (
                    closing
                    and closing.group("marker")[0] == marker[0]
                    and len(closing.group("marker")) >= len(marker)
                    and not line_spans[index - 1][2][closing.end() :].strip()
                ):
                    break
        elif _LIST_START.match(line):
            kind = "list"
            index += 1
            while index < len(line_spans) and line_spans[index][2].strip():
                index += 1
        elif line.lstrip().startswith(">"):
            kind = "blockquote"
            index += 1
            while index < len(line_spans) and line_spans[index][2].lstrip().startswith(">"):
                index += 1
        elif (
            "|" in line
            and index + 1 < len(line_spans)
            and _TABLE_DELIMITER.match(line_spans[index + 1][2])
        ):
            kind = "table"
            index += 2
            while (
                index < len(line_spans)
                and "|" in line_spans[index][2]
                and line_spans[index][2].strip()
            ):
                index += 1
        else:
            kind = "paragraph"
            index += 1
            while index < len(line_spans) and line_spans[index][2].strip():
                if _FENCE_START.match(line_spans[index][2]):
                    break
                index += 1
        start = line_spans[start_index][0]
        end = line_spans[index - 1][1]
        start, end = _trim_span(text, start, end)
        if start < end:
            blocks.append(_Block(start, end, kind))
    return blocks


def _sentence_spans(text: str, start: int, end: int) -> list[tuple[int, int]]:
    value = text[start:end]
    spans: list[tuple[int, int]] = []
    cursor = 0
    pattern = re.compile(
        r"[\s\S]*?[\u3002\uff01\uff1f.!?]+"
        r"[\"'\u201d\u2019\uff09\u3011\u300b\u300d\u300f]*(?:\s+|$)"
    )
    for match in pattern.finditer(value):
        match_start, match_end = _trim_span(value, cursor, match.end())
        if match_start < match_end:
            spans.append((start + match_start, start + match_end))
        cursor = match.end()
    if cursor < len(value):
        tail_start, tail_end = _trim_span(value, cursor, len(value))
        if tail_start < tail_end:
            spans.append((start + tail_start, start + tail_end))
    return spans


def _block_units(text: str, block: _Block) -> list[tuple[int, int]]:
    if block.kind != "paragraph":
        return _line_spans(text, block.start, block.end)
    sentences = _sentence_spans(text, block.start, block.end)
    if len(sentences) > 1:
        return sentences
    lines = _line_spans(text, block.start, block.end)
    return lines if len(lines) > 1 else [(block.start, block.end)]


def _component_strings(anchor: str) -> tuple[str, ...]:
    components: list[str] = []
    current: list[str] = []
    for value in anchor:
        category = unicodedata.category(value)
        if category[0] in {"L", "N", "M"}:
            current.append(value)
        elif current:
            components.append("".join(current))
            current = []
    if current:
        components.append("".join(current))
    return tuple(components)


def _occurrences(value: str, text: str) -> list[tuple[int, int]]:
    return [
        (match.start(), match.end())
        for match in re.finditer(re.escape(value), text, flags=re.IGNORECASE)
    ]


def _minimum_cover(
    groups: Sequence[Sequence[tuple[int, int]]], start: int, end: int
) -> tuple[int, int] | None:
    events = sorted(
        (span_start, span_end, group_index)
        for group_index, spans in enumerate(groups)
        for span_start, span_end in spans
        if start <= span_start < span_end <= end
    )
    if not groups or {event[2] for event in events} != set(range(len(groups))):
        return None
    best: tuple[int, int, int] | None = None
    for left in range(len(events)):
        seen: set[int] = set()
        maximum_end = events[left][1]
        for right in range(left, len(events)):
            maximum_end = max(maximum_end, events[right][1])
            seen.add(events[right][2])
            if len(seen) == len(groups):
                candidate = (maximum_end - events[left][0], events[left][0], maximum_end)
                if best is None or candidate < best:
                    best = candidate
                break
    return None if best is None else (best[1], best[2])


def _anchor_groups(anchor: str, body: str) -> tuple[tuple[tuple[int, int], ...], ...]:
    exact = _occurrences(anchor, body)
    if exact:
        return (tuple(exact),)
    return tuple(tuple(_occurrences(component, body)) for component in _component_strings(anchor))


def _anchor_ranges(
    groups: Sequence[Sequence[tuple[int, int]]], block: _Block
) -> tuple[tuple[int, int], ...]:
    if len(groups) == 1:
        return tuple(
            (start, end)
            for start, end in groups[0]
            if block.start <= start < end <= block.end
        )
    cover = _minimum_cover(groups, block.start, block.end)
    return () if cover is None else (cover,)


def _select_block(
    body: str, blocks: Sequence[_Block], query: CompiledQuery
) -> tuple[_Block, tuple[int, int] | None]:
    anchor_groups = tuple(_anchor_groups(anchor, body) for anchor in query.anchors)
    best: tuple[int, int, int, _Block, tuple[int, int]] | None = None
    for block in blocks:
        anchor_ranges: list[tuple[tuple[int, int], ...]] = []
        for groups in anchor_groups:
            ranges = _anchor_ranges(groups, block)
            if ranges:
                anchor_ranges.append(ranges)
        if not anchor_ranges:
            continue
        focus = _minimum_cover(anchor_ranges, block.start, block.end)
        if focus is None:
            continue
        candidate = (-len(anchor_ranges), focus[1] - focus[0], block.start, block, focus)
        if best is None or candidate[:3] < best[:3]:
            best = candidate
    if best is not None:
        return best[3], best[4]
    return blocks[0], None


def _scalar_window(
    text: str,
    start: int,
    end: int,
    focus: tuple[int, int] | None,
    max_tokens: int,
    max_bytes: int | None,
) -> tuple[int, int] | None:
    if start >= end:
        return None
    if focus is None:
        window_start = start
        window_end = start
    else:
        window_start = max(start, min(focus[0], end - 1))
        window_end = min(end, max(window_start + 1, focus[1]))
        if not _fits(text[window_start:window_end], max_tokens, max_bytes):
            window_end = window_start
    if window_end == window_start:
        if not _fits(text[window_start : window_start + 1], max_tokens, max_bytes):
            return None
        window_end += 1

    while True:
        changed = False
        for direction in ("after", "after", "before"):
            candidate_start = window_start - 1 if direction == "before" else window_start
            candidate_end = window_end + 1 if direction == "after" else window_end
            if candidate_start < start or candidate_end > end:
                continue
            if _fits(text[candidate_start:candidate_end], max_tokens, max_bytes):
                window_start, window_end = candidate_start, candidate_end
                changed = True
        if not changed:
            break
    return _trim_span(text, window_start, window_end)


def _bounded_block_window(
    body: str,
    block: _Block,
    focus: tuple[int, int] | None,
    max_tokens: int,
    max_bytes: int | None,
) -> tuple[int, int] | None:
    if _fits(body[block.start : block.end], max_tokens, max_bytes):
        return block.start, block.end
    units = _block_units(body, block)
    if focus is None:
        first = last = 0
    else:
        overlapping = [
            index
            for index, (start, end) in enumerate(units)
            if start < focus[1] and focus[0] < end
        ]
        if not overlapping:
            return _scalar_window(
                body, block.start, block.end, focus, max_tokens, max_bytes
            )
        first, last = min(overlapping), max(overlapping)
    candidate = _trim_span(body, units[first][0], units[last][1])
    if not _fits(body[candidate[0] : candidate[1]], max_tokens, max_bytes):
        return _scalar_window(body, block.start, block.end, focus, max_tokens, max_bytes)

    after = last + 1
    before = first - 1
    while True:
        changed = False
        for direction in ("after", "after", "before"):
            if direction == "after" and after < len(units):
                proposed = _trim_span(body, candidate[0], units[after][1])
                if _fits(body[proposed[0] : proposed[1]], max_tokens, max_bytes):
                    candidate = proposed
                    after += 1
                    changed = True
            elif direction == "before" and before >= 0:
                proposed = _trim_span(body, units[before][0], candidate[1])
                if _fits(body[proposed[0] : proposed[1]], max_tokens, max_bytes):
                    candidate = proposed
                    before -= 1
                    changed = True
        if not changed:
            break
    return candidate


def _make_snippet(body: str, query: CompiledQuery) -> _Snippet:
    blocks = _markdown_blocks(body)
    if not blocks:
        raise IndexUnavailableError("indexed item body is unavailable")
    block, focus = _select_block(body, blocks, query)
    span = _bounded_block_window(
        body,
        block,
        focus,
        SNIPPET_MAX_TOKENS,
        SNIPPET_MAX_BYTES,
    )
    if span is None or span[0] >= span[1]:
        raise BudgetExceededError("snippet budget cannot contain one Unicode scalar")
    value = body[span[0] : span[1]]
    return _Snippet(
        text=value,
        truncated_before=span[0] > 0,
        truncated_after=span[1] < len(body),
        token_estimate=estimate_evidence_tokens(value),
    )


def _truncate_read_body(body: str, max_tokens: int) -> tuple[str, bool]:
    if estimate_evidence_tokens(body) <= max_tokens:
        return body, False
    blocks = _markdown_blocks(body)
    safe_end = 0
    for block in blocks:
        if _fits(body[: block.end], max_tokens):
            safe_end = block.end
            continue
        for _, unit_end in _block_units(body, block):
            if _fits(body[:unit_end], max_tokens):
                safe_end = unit_end
            else:
                break
        break
    if safe_end == 0:
        span = _scalar_window(body, 0, len(body), None, max_tokens, None)
        if span is None:
            raise BudgetExceededError("read budget cannot contain one Unicode scalar")
        safe_end = span[1]
    value = body[:safe_end].rstrip()
    if not value:
        raise BudgetExceededError("read budget cannot contain canonical content")
    return value, True


def _relations(value: str) -> ItemRelations:
    try:
        decoded = json.loads(value)
        counterpart = decoded["counterpart_item_ids"]
        previous = decoded["previous_part_id"]
        next_part = decoded["next_part_id"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise IndexUnavailableError("indexed item relations are unavailable") from exc
    if (
        not isinstance(counterpart, list)
        or any(not isinstance(item_id, str) for item_id in counterpart)
        or (previous is not None and not isinstance(previous, str))
        or (next_part is not None and not isinstance(next_part, str))
    ):
        raise IndexUnavailableError("indexed item relations are unavailable")
    return ItemRelations(tuple(counterpart), previous, next_part)


class LexicalStore:
    """对一个启动时锁定的不可变 generation 提供线程安全门面。"""

    def __init__(
        self,
        generation_path: Path,
        manifest: dict[str, Any],
        connection: sqlite3.Connection,
    ) -> None:
        self._generation_path = generation_path
        self._manifest = dict(manifest)
        self._connection: sqlite3.Connection | None = connection
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()

    @classmethod
    def open_current(cls, retrieval_root: Path) -> "LexicalStore":
        """仅解析一次 current，完整验证后锁定其真实目录。"""

        try:
            root = retrieval_root.resolve(strict=True)
            current = root / "current"
            if not os.path.lexists(current) or not current.is_symlink():
                raise LexicalBuildError("current is not a symlink")
            generation_path = current.resolve(strict=True)
            generations_root = (root / "generations").resolve(strict=True)
            relative = generation_path.relative_to(generations_root)
            if len(relative.parts) != 1 or _GENERATION.fullmatch(relative.name) is None:
                raise LexicalBuildError("current does not target one generation")
            manifest = validate_generation_artifact(
                generation_path, expected_generation=relative.name
            )
            connection = open_immutable_database(
                generation_path / "corpus.sqlite", check_same_thread=False
            )
        except (LexicalBuildError, OSError, sqlite3.Error, ValueError) as exc:
            raise IndexUnavailableError("lexical index is unavailable") from exc
        return cls(generation_path, manifest, connection)

    @property
    def generation(self) -> str:
        return str(self._manifest["generation"])

    def __enter__(self) -> "LexicalStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def _database(self) -> sqlite3.Connection:
        if self._connection is None:
            raise IndexUnavailableError("lexical store is closed")
        return self._connection

    def status(self) -> GenerationStatus:
        return GenerationStatus(
            generation=self.generation,
            source_digest=self._manifest["source_digest"],
            built_at=self._manifest["built_at"],
            item_counts=dict(self._manifest["item_counts"]),
            schema_version=self._manifest["schema_version"],
            projection_schema_version=self._manifest["projection_schema_version"],
            chunk_policy_version=self._manifest["chunk_policy_version"],
            query_policy_version=self._manifest["query_policy_version"],
            ranking_policy_version=self._manifest["ranking_policy_version"],
            estimator_version=self._manifest["estimator_version"],
        )

    @contextmanager
    def request_deadline(self, deadline: float):
        """在同一连接锁内约束等待和 SQL 时间，退出时移除请求级回调。"""

        remaining = deadline - monotonic()
        if remaining <= 0 or not self._lock.acquire(timeout=remaining):
            raise BudgetExceededError("request processing deadline exceeded")
        connection = None
        try:
            if monotonic() >= deadline:
                raise BudgetExceededError("request processing deadline exceeded")
            connection = self._database()
            connection.set_progress_handler(lambda: int(monotonic() >= deadline), 100)
            yield
            if monotonic() >= deadline:
                raise BudgetExceededError("request processing deadline exceeded")
        except IndexUnavailableError as exc:
            cause = exc.__cause__
            if (
                isinstance(cause, sqlite3.Error)
                and (
                    getattr(cause, "sqlite_errorcode", None) == getattr(sqlite3, "SQLITE_INTERRUPT", 9)
                    or str(cause) == "interrupted"
                )
                and monotonic() >= deadline
            ):
                raise BudgetExceededError("request processing deadline exceeded") from exc
            raise
        finally:
            if connection is not None:
                connection.set_progress_handler(None, 0)
            self._lock.release()

    def read_canonical_item(self, item_id: str, generation: str) -> Item:
        """内部精确读取完整投影字段，供 bundle 校验使用，不截断正文。"""

        self.validate_generation(generation)
        if not isinstance(item_id, str) or _ITEM_ID.fullmatch(item_id) is None:
            raise InvalidRequestError("item_id has an invalid format")
        with self._lock:
            try:
                row = self._database().execute(
                    "SELECT * FROM items WHERE item_id = ?", (item_id,)
                ).fetchone()
            except sqlite3.Error as exc:
                raise IndexUnavailableError("item read failed") from exc
        if row is None:
            raise ItemNotFoundError("item was not found in the pinned generation")
        try:
            fields = dict(row)
            fields.pop("rowid")
            raw_heading = fields.pop("heading_path_json")
            heading = None if raw_heading is None else json.loads(raw_heading)
            if heading is not None and (
                not isinstance(heading, list) or any(not isinstance(v, str) for v in heading)
            ):
                raise ValueError("invalid heading path")
            fields["heading_path"] = None if heading is None else tuple(heading)
            fields["relations"] = _relations(fields.pop("relations_json"))
            return Item(**fields)
        except (TypeError, ValueError, KeyError) as exc:
            raise IndexUnavailableError("indexed item metadata is unavailable") from exc

    def logical_member_ids(self, seed: Item) -> tuple[str, ...]:
        """独立枚举逻辑单元成员，用于发现断开的关系链，不提供公开浏览能力。"""

        if seed.scope == "conversation":
            predicate = "session_id IS ? AND turn_index = ?"
            parameters = (seed.session_id, seed.turn_index)
        else:
            suffix = f"/part:{seed.part}"
            predicate = "substr(locator, 1, length(locator) - length('/part:' || part)) = ?"
            parameters = (seed.locator.removesuffix(suffix),)
        with self._lock:
            try:
                rows = self._database().execute(
                    "SELECT item_id FROM items WHERE source_path = ? AND scope = ? AND "
                    + predicate + " ORDER BY item_id",
                    (seed.source_path, seed.scope, *parameters),
                ).fetchall()
            except sqlite3.Error as exc:
                raise IndexUnavailableError("bundle membership query failed") from exc
        return tuple(row["item_id"] for row in rows)

    def validate_generation(self, generation: str) -> None:
        """在读取或公开搜索前校验调用方绑定的快照。"""

        if not isinstance(generation, str) or _GENERATION.fullmatch(generation) is None:
            raise InvalidRequestError("generation has an invalid format")
        if generation != self.generation:
            raise GenerationMismatchError("requested generation is not pinned")

    def search_lex(
        self,
        queries: Sequence[str],
        scopes: Sequence[str] | None = None,
        limit: int = DEFAULT_RESULT_LIMIT,
    ) -> SearchResponse:
        compiled = self._validate_search_request(queries, scopes, limit)
        allowed_scopes = SUPPORTED_SCOPES if scopes is None else tuple(scopes)
        placeholders = ", ".join("?" for _ in allowed_scopes)
        sql = (
            "SELECT i.item_id, bm25(items_fts, 4.0, 1.0) AS bm25_score "
            "FROM items_fts JOIN items AS i ON i.rowid=items_fts.rowid "
            f"WHERE items_fts MATCH ? AND i.scope IN ({placeholders}) "
            "ORDER BY bm25_score ASC, i.item_id ASC LIMIT ?"
        )
        with self._lock:
            connection = self._database()
            try:
                rankings: list[dict[str, int]] = []
                for query in compiled:
                    rows = connection.execute(
                        sql,
                        (query.match_expression, *allowed_scopes, CANDIDATE_LIMIT),
                    ).fetchall()
                    rankings.append(
                        {row["item_id"]: rank for rank, row in enumerate(rows, start=1)}
                    )
                fused_ids = self._fuse(rankings, limit)
                rows_by_id: dict[str, sqlite3.Row] = {}
                for item_id in fused_ids:
                    row = connection.execute(_ITEM_SELECT, (item_id,)).fetchone()
                    if row is None:
                        raise IndexUnavailableError("ranked item is unavailable")
                    rows_by_id[item_id] = row
            except sqlite3.Error as exc:
                raise IndexUnavailableError("lexical query failed") from exc

        results: list[SearchResult] = []
        total_tokens = 0
        for rank, item_id in enumerate(fused_ids, start=1):
            query_index = 0 if item_id in rankings[0] else min(
                (index for index in range(1, len(rankings)) if item_id in rankings[index]),
                key=lambda index: (rankings[index][item_id], index),
            )
            row = rows_by_id[item_id]
            snippet = _make_snippet(row["body"], compiled[query_index])
            total_tokens += snippet.token_estimate
            results.append(
                SearchResult(
                    item_id=item_id,
                    rank=rank,
                    title=row["title"],
                    source_type=row["scope"],
                    path=row["source_path"],
                    locator=row["locator"],
                    evidence_role=row["evidence_role"],
                    provider=row["provider"],
                    session_id=row["session_id"],
                    turn_index=row["turn_index"],
                    role=row["role"],
                    part=row["part"],
                    snippet=snippet.text,
                    truncated_before=snippet.truncated_before,
                    truncated_after=snippet.truncated_after,
                )
            )
        return SearchResponse(self.generation, tuple(results), total_tokens)

    @staticmethod
    def _validate_search_request(
        queries: Sequence[str], scopes: Sequence[str] | None, limit: int
    ) -> tuple[CompiledQuery, ...]:
        if isinstance(queries, (str, bytes)) or not isinstance(queries, Sequence):
            raise InvalidRequestError("queries must be an array")
        if not 1 <= len(queries) <= MAX_QUERIES:
            raise InvalidRequestError("queries must contain between 1 and 6 values")
        try:
            compiled = tuple(compile_lexical_query(query) for query in queries)
        except QueryValidationError as exc:
            raise InvalidRequestError(str(exc)) from exc
        dedupe_keys = [query.dedupe_key for query in compiled]
        if len(set(dedupe_keys)) != len(dedupe_keys):
            raise InvalidRequestError("queries must be unique")
        if scopes is not None:
            if isinstance(scopes, (str, bytes)) or not isinstance(scopes, Sequence):
                raise InvalidRequestError("scopes must be an array")
            if not 1 <= len(scopes) <= len(SUPPORTED_SCOPES):
                raise InvalidRequestError("scopes must contain between 1 and 3 values")
            if any(not isinstance(scope, str) or scope not in _SCOPE_SET for scope in scopes):
                raise InvalidRequestError("scope is invalid")
            if len(set(scopes)) != len(scopes):
                raise InvalidRequestError("scopes must be unique")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_RESULT_LIMIT
        ):
            raise InvalidRequestError("limit must be an integer between 1 and 20")
        return compiled

    @staticmethod
    def _fuse(rankings: Sequence[dict[str, int]], limit: int) -> tuple[str, ...]:
        item_ids = {item_id for ranking in rankings for item_id in ranking}

        def key(item_id: str) -> tuple[Fraction, int, int, str]:
            score = sum(
                Fraction(2 if index == 0 else 1, RRF_K + ranking[item_id])
                for index, ranking in enumerate(rankings)
                if item_id in ranking
            )
            primary = rankings[0].get(item_id, CANDIDATE_LIMIT + 1)
            any_rank = min(ranking[item_id] for ranking in rankings if item_id in ranking)
            return -score, primary, any_rank, item_id

        return tuple(sorted(item_ids, key=key)[:limit])

    def read_item(
        self,
        item_id: str,
        generation: str,
        max_estimated_tokens: int = DEFAULT_READ_MAX_TOKENS,
    ) -> ReadResult:
        self.validate_generation(generation)
        if not isinstance(item_id, str) or _ITEM_ID.fullmatch(item_id) is None:
            raise InvalidRequestError("item_id has an invalid format")
        if (
            isinstance(max_estimated_tokens, bool)
            or not isinstance(max_estimated_tokens, int)
            or not 1 <= max_estimated_tokens <= MAX_READ_TOKENS
        ):
            raise InvalidRequestError(
                "max_estimated_tokens must be an integer between 1 and 800"
            )
        with self._lock:
            try:
                row = self._database().execute(_ITEM_SELECT, (item_id,)).fetchone()
            except sqlite3.Error as exc:
                raise IndexUnavailableError("item read failed") from exc
        if row is None:
            raise ItemNotFoundError("item was not found in the pinned generation")
        body, is_truncated = _truncate_read_body(row["body"], max_estimated_tokens)
        return ReadResult(
            generation=self.generation,
            item_id=row["item_id"],
            title=row["title"],
            source_type=row["scope"],
            path=row["source_path"],
            locator=row["locator"],
            evidence_role=row["evidence_role"],
            provider=row["provider"],
            session_id=row["session_id"],
            turn_index=row["turn_index"],
            role=row["role"],
            part=row["part"],
            body=body,
            is_truncated=is_truncated,
            relations=_relations(row["relations_json"]),
            estimated_evidence_tokens=estimate_evidence_tokens(body),
        )
