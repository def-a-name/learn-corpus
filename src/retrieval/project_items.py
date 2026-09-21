#!/usr/bin/env python3
"""将 ready 状态的标准化 Markdown 来源投影为确定性原子 item。"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence
from urllib.parse import quote

from src.retrieval.contracts import (
    CHUNK_POLICY_VERSION,
    ESTIMATOR_VERSION,
    LEXICAL_SCHEMA_VERSION,
    PROJECTION_SCHEMA_VERSION,
    QUERY_POLICY_VERSION,
    RANKING_POLICY_VERSION,
    STABLE_ITEM_ID_VERSION,
    Item,
    ItemRelations,
    ProjectionResult,
    SourceSnapshot,
)
from src.retrieval.text import estimate_evidence_tokens
from src.corpus.document import parse_frontmatter, parse_line_locator
from src.corpus.paths import MANIFEST_PATH, REPO_ROOT
from src.corpus.scopes import SOURCE_ROOTS

TARGET_MIN_TOKENS = 400
TARGET_MAX_TOKENS = 600
HARD_MAX_TOKENS = 800

_ATX_HEADING = re.compile(r"^ {0,3}(?P<level>#{1,6})[ \t]+(?P<title>.*?)[ \t]*#*[ \t]*$")
_FENCE_START = re.compile(r"^ {0,3}(?P<marker>`{3,}|~{3,})")
_LIST_START = re.compile(r"^\s*(?:[-+*]|\d+[.)])[ \t]+")
_LIST_ITEM_START = re.compile(r"^(?P<indent>[ \t]*)(?:[-+*]|\d+[.)])[ \t]+")
_BLANK_BLOCKQUOTE_LINE = re.compile(r"^[ \t]*(?:>[ \t]*)+$")
_TABLE_DELIMITER = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$")
_EXCHANGE = re.compile(r"^## Exchange (?P<number>[1-9]\d*)$")
_TURN = re.compile(r"^- \*\*Turn\*\*: (?P<number>[1-9]\d*)$")
_USER_LOCATOR = re.compile(r"^- \*\*User locator\*\*: `(?P<locator>[^`]+)`$")
_ASSISTANT_LOCATOR = re.compile(r"^- \*\*Assistant locator\*\*: `(?P<locator>[^`]+)`$")


class ProjectionError(ValueError):
    """当标准化来源集无法在不猜测的前提下投影时抛出。"""


@dataclass(frozen=True)
class _LogicalItem:
    scope: str
    title: str | None
    source_title: str | None
    source_path: str
    source_id: str
    identity_locator: str
    locator_with_lines: str | None
    identity_kind: str
    evidence_role: str | None
    provider: str | None
    session_id: str | None
    turn_index: int | None
    role: str | None
    heading_path: tuple[str, ...] | None
    occurrence: int | None
    body: str
    counterpart_group: str | None = None
    logical_group: str = ""


@dataclass(frozen=True)
class _Section:
    kind: str
    title: str | None
    heading_path: tuple[str, ...]
    occurrence: int
    identity_locator: str
    body: str
    start_line: int
    end_line: int


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_item_id(
    source_path: str,
    identity_locator: str,
    identity_kind: str,
    part: int,
) -> str:
    identity = (
        STABLE_ITEM_ID_VERSION,
        source_path,
        identity_locator,
        identity_kind,
        part,
    )
    digest = hashlib.sha256(_canonical_json(identity).encode("utf-8")).digest()[:20]
    encoded = base64.b32encode(digest).decode("ascii").rstrip("=").lower()
    return f"itm_{encoded}"


def _split_scalar(text: str, limit: int = TARGET_MAX_TOKENS) -> list[str]:
    chunks: list[str] = []
    remaining = text
    while remaining:
        if estimate_evidence_tokens(remaining) <= limit:
            chunks.append(remaining)
            break
        low, high = 1, len(remaining)
        while low < high:
            middle = (low + high + 1) // 2
            if estimate_evidence_tokens(remaining[:middle]) <= limit:
                low = middle
            else:
                high = middle - 1
        chunks.append(remaining[:low])
        remaining = remaining[low:]
    return chunks


def _split_lines(text: str) -> list[str]:
    pieces: list[str] = []
    current: list[str] = []
    for line in text.splitlines(keepends=True):
        candidate = "".join((*current, line))
        if current and estimate_evidence_tokens(candidate) > TARGET_MAX_TOKENS:
            pieces.append("".join(current).rstrip("\n"))
            current = []
        if estimate_evidence_tokens(line) > TARGET_MAX_TOKENS:
            if current:
                pieces.append("".join(current).rstrip("\n"))
                current = []
            pieces.extend(_split_scalar(line.rstrip("\n")))
        else:
            current.append(line)
    if current:
        pieces.append("".join(current).rstrip("\n"))
    return [piece for piece in pieces if piece]


def _split_protected_lines(text: str) -> list[str]:
    """在不拆分 hard_max 以内完整行的前提下按目标上限打包。"""

    pieces: list[str] = []
    current: list[str] = []
    for line in text.splitlines(keepends=True):
        candidate = "".join((*current, line))
        if current and estimate_evidence_tokens(candidate) > TARGET_MAX_TOKENS:
            pieces.append("".join(current).rstrip("\r\n"))
            current = []
        line_tokens = estimate_evidence_tokens(line)
        if line_tokens > HARD_MAX_TOKENS:
            if current:
                pieces.append("".join(current).rstrip("\r\n"))
                current = []
            pieces.extend(_split_scalar(line.rstrip("\r\n")))
        elif line_tokens > TARGET_MAX_TOKENS:
            if current:
                pieces.append("".join(current).rstrip("\r\n"))
                current = []
            pieces.append(line.rstrip("\r\n"))
        else:
            current.append(line)
    if current:
        pieces.append("".join(current).rstrip("\r\n"))
    return [piece for piece in pieces if piece]


def _split_sentences(text: str) -> list[str]:
    sentences = [value for value in re.findall(r".*?(?:[。！？.!?]+(?:\s+|$)|$)", text, re.DOTALL) if value]
    pieces: list[str] = []
    current = ""
    for sentence in sentences:
        candidate = current + sentence
        if current and estimate_evidence_tokens(candidate) > TARGET_MAX_TOKENS:
            pieces.append(current.rstrip())
            current = ""
        if estimate_evidence_tokens(sentence) > TARGET_MAX_TOKENS:
            pieces.extend(_split_lines(sentence) if "\n" in sentence else _split_scalar(sentence))
        else:
            current += sentence
    if current.strip():
        pieces.append(current.rstrip())
    return pieces


_PROTECTED_MARKDOWN_KINDS = frozenset({"code", "table", "blockquote", "list"})


def _pack_markdown_units(units: Sequence[str], separator: str = "\n") -> list[str]:
    """打包已安全分隔的语法单元，不强制拆分未超限的单元。"""

    chunks: list[str] = []
    current: list[str] = []
    for unit in (value for value in units if value):
        unit_tokens = estimate_evidence_tokens(unit)
        if unit_tokens > HARD_MAX_TOKENS:
            raise ProjectionError("markdown syntax unit exceeds hard_max")
        if unit_tokens > TARGET_MAX_TOKENS:
            if current:
                chunks.append(separator.join(current))
                current = []
            chunks.append(unit)
            continue
        candidate = separator.join((*current, unit))
        if current and estimate_evidence_tokens(candidate) > TARGET_MAX_TOKENS:
            chunks.append(separator.join(current))
            current = []
        current.append(unit)
    if current:
        chunks.append(separator.join(current))
    return chunks


def _split_structural_units(units: Sequence[str], separator: str = "\n") -> list[str]:
    expanded: list[str] = []
    for unit in units:
        if estimate_evidence_tokens(unit) <= HARD_MAX_TOKENS:
            expanded.append(unit)
        else:
            expanded.extend(_split_lines(unit))
    return _pack_markdown_units(expanded, separator)


def _matching_fence_close(line: str, marker: str) -> bool:
    closing = _FENCE_START.match(line)
    return bool(
        closing
        and closing.group("marker")[0] == marker[0]
        and len(closing.group("marker")) >= len(marker)
        and not line[closing.end() :].strip()
    )


def _split_fenced_code(block: str) -> list[str]:
    lines = block.splitlines()
    opening = _FENCE_START.match(lines[0]) if lines else None
    if opening is None or len(lines) < 2:
        return _split_lines(block)
    marker = opening.group("marker")
    closed = _matching_fence_close(lines[-1], marker)
    content_lines = lines[1:-1] if closed else lines[1:]
    if not content_lines:
        return _split_lines(block)
    pieces = _split_protected_lines("\n".join(content_lines))
    if not pieces:
        return _split_lines(block)
    pieces[0] = f"{lines[0]}\n{pieces[0]}"
    if closed:
        pieces[-1] = f"{pieces[-1]}\n{lines[-1]}"
    if any(estimate_evidence_tokens(piece) > HARD_MAX_TOKENS for piece in pieces):
        return _split_lines(block)
    return pieces


def _split_table(block: str) -> list[str]:
    lines = block.splitlines()
    if len(lines) < 3 or not _TABLE_DELIMITER.match(lines[1]):
        return _split_lines(block)
    header = "\n".join(lines[:2])
    rows = lines[2:]
    units: list[str]
    first = f"{header}\n{rows[0]}"
    if estimate_evidence_tokens(first) <= HARD_MAX_TOKENS:
        units = [first, *rows[1:]]
    else:
        units = [header, *rows]
    return _split_structural_units(units)


def _indent_width(value: str) -> int:
    return len(value.expandtabs(4))


def _split_list(block: str) -> list[str]:
    lines = block.splitlines()
    first = _LIST_ITEM_START.match(lines[0]) if lines else None
    if first is None:
        return _split_lines(block)
    base_indent = _indent_width(first.group("indent"))
    items: list[str] = []
    current: list[str] = []
    for line in lines:
        marker = _LIST_ITEM_START.match(line)
        if (
            current
            and marker is not None
            and _indent_width(marker.group("indent")) == base_indent
        ):
            items.append("\n".join(current))
            current = []
        current.append(line)
    if current:
        items.append("\n".join(current))
    return _split_structural_units(items)


def _split_blockquote(block: str) -> list[str]:
    paragraphs: list[str] = []
    current: list[str] = []
    for line in block.splitlines():
        current.append(line)
        if _BLANK_BLOCKQUOTE_LINE.match(line):
            paragraphs.append("\n".join(current))
            current = []
    if current:
        paragraphs.append("\n".join(current))
    return _split_structural_units(paragraphs)


def _split_markdown_block(kind: str, block: str) -> list[str]:
    tokens = estimate_evidence_tokens(block)
    if tokens <= TARGET_MAX_TOKENS:
        return [block]
    if kind in _PROTECTED_MARKDOWN_KINDS and tokens <= HARD_MAX_TOKENS:
        return [block]
    if kind == "code":
        pieces = _split_fenced_code(block)
    elif kind == "table":
        pieces = _split_table(block)
    elif kind == "list":
        pieces = _split_list(block)
    elif kind == "blockquote":
        pieces = _split_blockquote(block)
    else:
        pieces = _split_sentences(block)
    if not pieces or any(estimate_evidence_tokens(piece) > HARD_MAX_TOKENS for piece in pieces):
        raise ProjectionError(f"{kind} splitter exceeded hard_max")
    return pieces


def _markdown_blocks(text: str) -> list[tuple[str, str]]:
    lines = text.strip("\n").splitlines()
    blocks: list[tuple[str, str]] = []
    index = 0
    while index < len(lines):
        if not lines[index].strip():
            index += 1
            continue
        fence = _FENCE_START.match(lines[index])
        if fence:
            marker = fence.group("marker")
            start = index
            index += 1
            while index < len(lines):
                closing = _FENCE_START.match(lines[index])
                if (
                    closing
                    and closing.group("marker")[0] == marker[0]
                    and len(closing.group("marker")) >= len(marker)
                    and not lines[index][closing.end() :].strip()
                ):
                    index += 1
                    break
                index += 1
            blocks.append(("code", "\n".join(lines[start:index])))
            continue
        start = index
        if _LIST_START.match(lines[index]):
            kind = "list"
            index += 1
            while index < len(lines) and lines[index].strip():
                index += 1
        elif lines[index].lstrip().startswith(">"):
            kind = "blockquote"
            index += 1
            while index < len(lines) and lines[index].lstrip().startswith(">"):
                index += 1
        elif (
            "|" in lines[index]
            and index + 1 < len(lines)
            and _TABLE_DELIMITER.match(lines[index + 1])
        ):
            kind = "table"
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                index += 1
        else:
            kind = "paragraph"
            index += 1
            while index < len(lines) and lines[index].strip():
                if _FENCE_START.match(lines[index]):
                    break
                index += 1
        blocks.append((kind, "\n".join(lines[start:index])))
    return blocks


def chunk_markdown(text: str) -> tuple[str, ...]:
    """在不超过硬上限的前提下确定性拆分一个逻辑正文。"""

    body = text.strip()
    if not body:
        raise ProjectionError("logical item body is empty")
    if estimate_evidence_tokens(body) <= TARGET_MAX_TOKENS:
        return (body,)

    atomic: list[str] = []
    for kind, block in _markdown_blocks(body):
        atomic.extend(_split_markdown_block(kind, block))

    chunks: list[str] = []
    current: list[str] = []
    for block in atomic:
        candidate = "\n\n".join((*current, block))
        if current and estimate_evidence_tokens(candidate) > TARGET_MAX_TOKENS:
            chunks.append("\n\n".join(current))
            current = []
        if estimate_evidence_tokens(block) > HARD_MAX_TOKENS:
            raise ProjectionError("chunk splitter produced a block above hard_max")
        current.append(block)
    if current:
        chunks.append("\n\n".join(current))
    if not chunks or any(not chunk for chunk in chunks):
        raise ProjectionError("chunk splitter produced an empty part")
    if any(estimate_evidence_tokens(chunk) > HARD_MAX_TOKENS for chunk in chunks):
        raise ProjectionError("chunk splitter exceeded hard_max")
    return tuple(chunks)


def _normalize_heading(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value).casefold().strip()
    normalized = re.sub(r"[`*_~]", "", normalized)
    normalized = re.sub(r"[^\w\u3400-\u9fff -]", "", normalized)
    normalized = re.sub(r"[\s-]+", "-", normalized).strip("-")
    return normalized or f"untitled-{_sha256_text(value)[:12]}"


def _trim_line_range(lines: Sequence[str], start_line: int) -> tuple[str, int, int] | None:
    first = 0
    last = len(lines)
    while first < last and not lines[first].strip():
        first += 1
    while last > first and not lines[last - 1].strip():
        last -= 1
    if first == last:
        return None
    return "\n".join(lines[first:last]).rstrip(), start_line + first, start_line + last - 1


def _parse_sections(body: str, body_start_line: int, scope: str, source_id: str) -> list[_Section]:
    lines = body.splitlines()
    sections: list[_Section] = []
    stack: list[tuple[int, str]] = []
    occurrences: Counter[tuple[str, ...]] = Counter()
    current_title: str | None = None
    current_path: tuple[str, ...] = ()
    current_occurrence = 1
    current_heading_line: str | None = None
    current_content: list[str] = []
    current_start = body_start_line
    fence_marker = ""

    def finish() -> None:
        nonlocal current_content
        trimmed = _trim_line_range(current_content, current_start + (1 if current_heading_line else 0))
        if trimmed is None:
            current_content = []
            return
        content, content_start, content_end = trimmed
        if current_heading_line is None:
            rendered = content
            start_line = content_start
            identity = f"{scope}:{source_id}/root"
            kind = "root"
            title = None
            path: tuple[str, ...] = ()
            occurrence = 1
        else:
            rendered = f"{current_heading_line}\n{content}"
            start_line = current_start
            normalized_path = tuple(_normalize_heading(value) for value in current_path)
            encoded_path = "/".join(quote(value, safe="-._~") for value in normalized_path)
            identity = f"{scope}:{source_id}/heading:{encoded_path}"
            if current_occurrence > 1:
                identity += f":occurrence:{current_occurrence}"
            kind = "heading"
            title = current_title
            path = current_path
            occurrence = current_occurrence
        sections.append(
            _Section(kind, title, path, occurrence, identity, rendered, start_line, content_end)
        )
        current_content = []

    for offset, line in enumerate(lines):
        source_line = body_start_line + offset
        fence = _FENCE_START.match(line)
        if fence:
            marker = fence.group("marker")
            if not fence_marker:
                fence_marker = marker
            elif (
                marker[0] == fence_marker[0]
                and len(marker) >= len(fence_marker)
                and not line[fence.end() :].strip()
            ):
                fence_marker = ""
            current_content.append(line)
            continue
        heading = None if fence_marker else _ATX_HEADING.match(line)
        if heading is None:
            current_content.append(line)
            continue
        finish()
        level = len(heading.group("level"))
        title = heading.group("title").strip()
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        current_title = title
        current_path = tuple(value for _, value in stack)
        normalized_key = tuple(_normalize_heading(value) for value in current_path)
        occurrences[normalized_key] += 1
        current_occurrence = occurrences[normalized_key]
        current_heading_line = line
        current_start = source_line
        current_content = []
    finish()
    return sections


def _body_start_line(path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return 1
    end = text.find("\n---\n", 4)
    if end == -1:
        raise ProjectionError(f"front matter is not closed: {path}")
    remainder = text[end + 5 :]
    stripped = remainder.lstrip("\n")
    skipped_newlines = len(remainder) - len(stripped)
    return text[: end + 5].count("\n") + 1 + skipped_newlines


def _find_required_line(
    lines: Sequence[str],
    structural: Sequence[bool],
    pattern: re.Pattern[str],
    start: int,
    end: int,
    label: str,
) -> tuple[int, re.Match[str]]:
    matches = [
        (index, match)
        for index in range(start, end)
        if structural[index] and (match := pattern.match(lines[index]))
    ]
    if len(matches) != 1:
        raise ProjectionError(f"conversation exchange requires exactly one {label}")
    return matches[0]


def _structural_line_mask(lines: Sequence[str]) -> list[bool]:
    structural: list[bool] = []
    fence_marker = ""
    for line in lines:
        fence = _FENCE_START.match(line)
        if fence_marker:
            structural.append(False)
            if fence:
                marker = fence.group("marker")
                if (
                    marker[0] == fence_marker[0]
                    and len(marker) >= len(fence_marker)
                    and not line[fence.end() :].strip()
                ):
                    fence_marker = ""
            continue
        if fence:
            structural.append(False)
            fence_marker = fence.group("marker")
            continue
        structural.append(True)
    return structural


def _parse_conversation(
    body: str, metadata: dict[str, Any], source_path: str, source_id: str
) -> list[_LogicalItem]:
    lines = body.splitlines()
    structural = _structural_line_mask(lines)
    exchange_positions = [
        index
        for index, line in enumerate(lines)
        if structural[index] and _EXCHANGE.match(line)
    ]
    expected_count = metadata.get("exchange_count")
    if not isinstance(expected_count, int) or expected_count < 1:
        raise ProjectionError(f"conversation {source_id} has invalid exchange_count")
    if len(exchange_positions) != expected_count:
        raise ProjectionError(f"conversation {source_id} exchange structure is ambiguous")
    provider = metadata.get("provider")
    if not isinstance(provider, str) or not provider:
        raise ProjectionError(f"conversation {source_id} has no provider")
    session_id = metadata.get("provider_session_id") or metadata.get("derived_session_key")
    if session_id is not None and not isinstance(session_id, str):
        raise ProjectionError(f"conversation {source_id} has invalid session identity")

    logical: list[_LogicalItem] = []
    seen_locators: set[str] = set()
    for ordinal, start in enumerate(exchange_positions, start=1):
        end = exchange_positions[ordinal] if ordinal < len(exchange_positions) else len(lines)
        exchange = _EXCHANGE.match(lines[start])
        if exchange is None or int(exchange.group("number")) != ordinal:
            raise ProjectionError(f"conversation {source_id} exchange numbering is not continuous")
        turn_index_pos, turn_match = _find_required_line(
            lines, structural, _TURN, start + 1, end, "turn"
        )
        user_pos, user_match = _find_required_line(
            lines, structural, _USER_LOCATOR, start + 1, end, "user locator"
        )
        assistant_pos, assistant_match = _find_required_line(
            lines,
            structural,
            _ASSISTANT_LOCATOR,
            start + 1,
            end,
            "assistant locator",
        )
        human_headers = [
            index
            for index in range(start + 1, end)
            if structural[index] and lines[index] == "### Human user"
        ]
        raw_assistant_headers = [
            index for index in range(start + 1, end) if lines[index] == "### Assistant final"
        ]
        assistant_headers = [
            index
            for index in raw_assistant_headers
            if structural[index]
        ]
        if not assistant_headers and len(raw_assistant_headers) == 1:
            assistant_headers = raw_assistant_headers
        if len(human_headers) != 1 or len(assistant_headers) != 1:
            raise ProjectionError(f"conversation {source_id} role structure is ambiguous")
        human_header = human_headers[0]
        assistant_header = assistant_headers[0]
        if not (
            turn_index_pos < human_header
            and user_pos < human_header
            and assistant_pos < human_header
            and human_header < assistant_header
        ):
            raise ProjectionError(f"conversation {source_id} exchange metadata order is invalid")
        human_body = "\n".join(lines[human_header + 1 : assistant_header]).strip()
        assistant_body = "\n".join(lines[assistant_header + 1 : end]).strip()
        if not human_body or not assistant_body:
            raise ProjectionError(f"conversation {source_id} has an empty role body")
        turn_index = int(turn_match.group("number"))
        group = f"{source_id}:turn:{turn_index}"
        role_values = (
            (
                "human",
                "user_statement",
                user_match.group("locator"),
                human_body,
                f"{group}:assistant",
            ),
            (
                "assistant",
                "assistant_suggestion",
                assistant_match.group("locator"),
                assistant_body,
                f"{group}:human",
            ),
        )
        for role, evidence_role, raw_locator, role_body, counterpart_group in role_values:
            identity_locator, _, _ = parse_line_locator(raw_locator)
            if identity_locator in seen_locators:
                raise ProjectionError(f"conversation {source_id} contains a duplicate locator")
            seen_locators.add(identity_locator)
            logical.append(
                _LogicalItem(
                    scope="conversation",
                    title=None,
                    source_title=None,
                    source_path=source_path,
                    source_id=source_id,
                    identity_locator=identity_locator,
                    locator_with_lines=raw_locator,
                    identity_kind=role,
                    evidence_role=evidence_role,
                    provider=provider,
                    session_id=session_id,
                    turn_index=turn_index,
                    role=role,
                    heading_path=None,
                    occurrence=None,
                    body=role_body,
                    counterpart_group=counterpart_group,
                    logical_group=f"{group}:{role}",
                )
            )
    return logical


def _parse_document(
    body: str,
    body_start_line: int,
    metadata: dict[str, Any],
    source_path: str,
    source_id: str,
    scope: str,
) -> list[_LogicalItem]:
    evidence_role = metadata.get("evidence_role")
    if scope == "article":
        if evidence_role != "external_source":
            raise ProjectionError(f"article {source_id} must be external_source")
    elif evidence_role is not None and not isinstance(evidence_role, str):
        raise ProjectionError(f"note {source_id} has invalid evidence_role")
    source_title = metadata.get("title")
    if not isinstance(source_title, str) or not source_title.strip():
        raise ProjectionError(f"document {source_id} has no title")
    sections = _parse_sections(body, body_start_line, scope, source_id)
    logical: list[_LogicalItem] = []
    for section in sections:
        title = source_title if section.kind == "root" else section.title
        with_lines = f"{section.identity_locator}@L{section.start_line}-L{section.end_line}"
        logical.append(
            _LogicalItem(
                scope=scope,
                title=title,
                source_title=source_title,
                source_path=source_path,
                source_id=source_id,
                identity_locator=section.identity_locator,
                locator_with_lines=with_lines,
                identity_kind=section.kind,
                evidence_role=evidence_role,
                provider=None,
                session_id=None,
                turn_index=None,
                role=None,
                heading_path=section.heading_path,
                occurrence=section.occurrence,
                body=section.body,
                logical_group=section.identity_locator,
            )
        )
    return logical


def _source_type(output_path: str) -> str:
    pure = PurePosixPath(output_path)
    for scope, root in SOURCE_ROOTS.items():
        try:
            pure.relative_to(root)
        except ValueError:
            continue
        return scope
    raise ProjectionError(f"manifest output_path is outside allowed source roots: {output_path}")


def _normalized_raw_hash(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.removeprefix("sha256:")


def _validate_source(
    repo_root: Path, source_id: str, record: dict[str, Any]
) -> tuple[Path, str, dict[str, Any], str, int]:
    output_path = record.get("output_path")
    if (
        not isinstance(output_path, str)
        or not output_path
        or "\\" in output_path
        or Path(output_path).is_absolute()
        or any(part in {"", ".", ".."} for part in PurePosixPath(output_path).parts)
    ):
        raise ProjectionError(f"source {source_id} has an invalid output_path")
    scope = _source_type(output_path)
    allowed_root = (repo_root / SOURCE_ROOTS[scope]).resolve()
    path = (repo_root / output_path).resolve()
    try:
        path.relative_to(allowed_root)
    except ValueError as exc:
        raise ProjectionError(f"source {source_id} escapes its allowed root") from exc
    if not path.is_file():
        raise ProjectionError(f"ready source is missing: {output_path}")
    try:
        metadata, body = parse_frontmatter(path)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ProjectionError(f"cannot parse standardized source: {output_path}") from exc
    if metadata.get("id") != source_id:
        raise ProjectionError(f"manifest/source ID mismatch: {output_path}")
    if metadata.get("type") != scope:
        raise ProjectionError(f"manifest/source type mismatch: {output_path}")
    if scope in {"note", "article"} and metadata.get("layer") != scope:
        raise ProjectionError(f"manifest/source layer mismatch: {output_path}")
    if scope == "conversation" and metadata.get("provider") != record.get("provider"):
        raise ProjectionError(f"manifest/source provider mismatch: {output_path}")
    if _normalized_raw_hash(metadata.get("source_hash")) != _normalized_raw_hash(record.get("source_hash")):
        raise ProjectionError(f"manifest/source raw hash mismatch: {output_path}")
    importer_version = record.get("importer_version")
    if not isinstance(importer_version, int) or metadata.get("importer_version") != importer_version:
        raise ProjectionError(f"manifest/source importer version mismatch: {output_path}")
    return path, scope, metadata, body, importer_version


def _finalize_items(logical_items: Iterable[_LogicalItem]) -> tuple[Item, ...]:
    items: list[Item] = []
    groups: dict[str, list[str]] = {}
    counterpart_targets: dict[str, str | None] = {}
    for logical in logical_items:
        if logical.logical_group in groups:
            raise ProjectionError(f"duplicate logical item group: {logical.logical_group}")
        chunks = chunk_markdown(logical.body)
        group_ids: list[str] = []
        raw_base, line_start, line_end = parse_line_locator(logical.locator_with_lines or "")
        for part, body in enumerate(chunks, start=1):
            item_id = stable_item_id(
                logical.source_path,
                logical.identity_locator,
                logical.identity_kind,
                part,
            )
            locator = f"{logical.identity_locator}/part:{part}"
            locator_with_lines = None
            if logical.locator_with_lines:
                locator_with_lines = (
                    f"{raw_base}/part:{part}@L{line_start}-L{line_end}"
                    if line_start is not None and line_end is not None
                    else f"{logical.locator_with_lines}/part:{part}"
                )
            items.append(
                Item(
                    item_id=item_id,
                    scope=logical.scope,
                    title=logical.title,
                    source_title=logical.source_title,
                    source_path=logical.source_path,
                    source_id=logical.source_id,
                    locator=locator,
                    locator_with_lines=locator_with_lines,
                    evidence_role=logical.evidence_role,
                    provider=logical.provider,
                    session_id=logical.session_id,
                    turn_index=logical.turn_index,
                    role=logical.role,
                    heading_path=logical.heading_path,
                    occurrence=logical.occurrence,
                    part=part,
                    body=body,
                    body_sha256=_sha256_text(body),
                    token_estimate=estimate_evidence_tokens(body),
                )
            )
            group_ids.append(item_id)
        groups[logical.logical_group] = group_ids
        counterpart_targets[logical.logical_group] = logical.counterpart_group

    if len({item.item_id for item in items}) != len(items):
        raise ProjectionError("stable item ID collision")
    by_id = {item.item_id: item for item in items}
    for group, group_ids in groups.items():
        counterpart_group = counterpart_targets[group]
        counterparts = tuple(groups.get(counterpart_group, ())) if counterpart_group else ()
        for index, item_id in enumerate(group_ids):
            item = by_id[item_id]
            by_id[item_id] = replace(
                item,
                relations=ItemRelations(
                    counterpart_item_ids=counterparts,
                    previous_part_id=group_ids[index - 1] if index else None,
                    next_part_id=group_ids[index + 1] if index + 1 < len(group_ids) else None,
                ),
            )
    return tuple(by_id[item.item_id] for item in items)


def _source_digest(sources: Sequence[SourceSnapshot]) -> str:
    payload = [
        (
            source.source_path,
            source.standardized_sha256,
            source.source_id,
            source.source_type,
            source.importer_version,
            PROJECTION_SCHEMA_VERSION,
            CHUNK_POLICY_VERSION,
            LEXICAL_SCHEMA_VERSION,
            QUERY_POLICY_VERSION,
            RANKING_POLICY_VERSION,
        )
        for source in sorted(sources, key=lambda value: value.source_path)
    ]
    return f"sha256:{_sha256_text(_canonical_json(payload))}"


def project_corpus(
    repo_root: Path = REPO_ROOT,
    manifest_path: Path | None = None,
) -> ProjectionResult:
    """验证所有 ready 来源并返回内存中的确定性投影。"""

    repo_root = repo_root.resolve()
    manifest_path = manifest_path or (repo_root / MANIFEST_PATH.relative_to(REPO_ROOT))
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProjectionError(f"cannot read manifest: {manifest_path}") from exc
    records = manifest.get("sources") if isinstance(manifest, dict) else None
    if not isinstance(records, dict):
        raise ProjectionError("manifest sources must be an object")
    ready = {
        source_id: record
        for source_id, record in records.items()
        if isinstance(record, dict) and record.get("ingest_status") == "ready"
    }
    if not ready:
        raise ProjectionError("manifest contains no ready sources")

    logical_items: list[_LogicalItem] = []
    snapshots: list[SourceSnapshot] = []
    ready_paths: set[str] = set()
    for source_id, record in sorted(
        ready.items(),
        key=lambda value: (
            value[1].get("output_path")
            if isinstance(value[1].get("output_path"), str)
            else ""
        ),
    ):
        path, scope, metadata, body, importer_version = _validate_source(repo_root, source_id, record)
        output_path = record["output_path"]
        if output_path in ready_paths:
            raise ProjectionError(f"duplicate ready output_path: {output_path}")
        ready_paths.add(output_path)
        standardized_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        snapshots.append(
            SourceSnapshot(output_path, standardized_sha256, source_id, scope, importer_version)
        )
        if scope == "conversation":
            logical_items.extend(_parse_conversation(body, metadata, output_path, source_id))
        else:
            logical_items.extend(
                _parse_document(
                    body,
                    _body_start_line(path),
                    metadata,
                    output_path,
                    source_id,
                    scope,
                )
            )

    discovered = {
        path.relative_to(repo_root).as_posix()
        for root in SOURCE_ROOTS.values()
        for path in (repo_root / root).rglob("*.md")
        if path.is_file()
    }
    if discovered != ready_paths:
        missing = sorted(ready_paths - discovered)
        unregistered = sorted(discovered - ready_paths)
        raise ProjectionError(
            f"ready/source set mismatch: missing={missing!r}, unregistered={unregistered!r}"
        )
    items = _finalize_items(logical_items)
    if not items:
        raise ProjectionError("projection produced no items")
    snapshots_tuple = tuple(sorted(snapshots, key=lambda value: value.source_path))
    return ProjectionResult(items, snapshots_tuple, _source_digest(snapshots_tuple))


def main() -> None:
    parser = argparse.ArgumentParser(description="Project ready sources into retrieval items.")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    result = project_corpus(args.repo_root, args.manifest)
    counts = Counter(item.scope for item in result.items)
    print(
        json.dumps(
            {
                "source_digest": result.source_digest,
                "sources": len(result.sources),
                "items": len(result.items),
                "item_counts": dict(sorted(counts.items())),
                "projection_schema_version": PROJECTION_SCHEMA_VERSION,
                "chunk_policy_version": CHUNK_POLICY_VERSION,
                "estimator_version": ESTIMATOR_VERSION,
                "ranking_policy_version": RANKING_POLICY_VERSION,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
