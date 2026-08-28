#!/usr/bin/env python3
"""导入 Chrome 插件导出的 ChatGPT/DeepSeek Markdown 会话。"""

from __future__ import annotations

import argparse
import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit

from scripts.common.ingest_log import SourceChangeTracker
from scripts.common.wiki_core import (
    MANIFEST_PATH,
    REPO_ROOT,
    WEB_CHAT_ASSISTANT_FINAL_DETECTION,
    WEB_CHAT_IMPORTER_VERSION,
    clean_message,
    format_line_locator,
    is_exact_meaningless_exchange,
    load_manifest,
    redact_secrets,
    relative_to_repo,
    save_manifest,
    sha256_file,
    source_needs_redaction,
    utc_now,
    yaml_document,
)
from scripts.ingest.markdown_sources import MarkdownImportError, scan_markdown


DEFAULT_INPUT = REPO_ROOT.parent / "web-chats"
DEFAULT_OUTPUT = REPO_ROOT / "sources" / "conversations"
DEFAULT_ASSETS = REPO_ROOT / "sources" / "assets"
ORIGIN = "web-chat-export"
SOURCE_FORMAT = "browser-extension-markdown"
REVIEW_SKIP_REASONS = frozenset(
    {
        "web_chat_parse_review",
        "provider_detection_review",
        "provider_role_mismatch_review",
        "message_time_review",
        "message_time_order_review",
        "assistant_branch_ambiguous_review",
        "asset_path_escape_review",
        "local_asset_missing_review",
        "asset_type_unsupported_review",
        "sandbox_asset_missing_review",
        "citation_targets_missing_review",
        "source_id_collision_review",
    }
)

_FROM_LINE = re.compile(r"^> From:\s*(?P<url>https?://\S+)\s*$", re.MULTILINE)
_FENCE_START = re.compile(r"^ {0,3}(?P<marker>`{3,}|~{3,})")
_MESSAGE_TIME = re.compile(r"^message time:\s*(?P<value>.+?)\s*$", re.IGNORECASE)
_CITATION = re.compile(r"\[!?citation:(?P<label>\d+)\]", re.IGNORECASE)
_SANDBOX_LINK = re.compile(r"\]\(\s*(?P<target>sandbox:/[^)\s]+)\s*\)", re.IGNORECASE)
_SUPPORTED_ASSET_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".txt",
    ".csv",
    ".json",
    ".pdf",
}


@dataclass(frozen=True)
class WebChatExchange:
    turn_index: int
    user_text: str
    assistant_text: str
    user_time: str
    user_locator: str
    assistant_locator: str


@dataclass(frozen=True)
class WebChatParse:
    exchanges: tuple[WebChatExchange, ...]
    visible_user_block_count: int
    assistant_block_count: int
    assistant_final_count: int
    omitted_trivial_exchange_count: int
    omitted_unpaired_user_count: int
    invalid_message_time_count: int
    non_monotonic_message_time_count: int
    ambiguous_assistant_block_count: int
    provider_role_mismatch_count: int
    first_message_time: str
    last_message_time: str


@dataclass(frozen=True)
class RawAsset:
    source_path: Path
    source_target: str
    digest: str
    reference_count: int


@dataclass(frozen=True)
class StoredAsset:
    raw: RawAsset
    stored_path: Path
    rendered_target: str


@dataclass(frozen=True)
class WebChatUnit:
    source_id: str
    provider: str
    provider_session_id: str
    provider_share_id: str
    derived_session_key: str
    identity_confidence: str
    source_url: str
    title: str
    created: str
    updated: str
    source_path: Path
    relative_path: str
    locator: str
    content_hash: str
    start_line: int
    end_line: int
    parse: WebChatParse | None
    raw_assets: tuple[RawAsset, ...]
    image_reference_count: int
    missing_local_asset_count: int
    missing_sandbox_asset_count: int
    unsupported_asset_type_count: int
    asset_path_escape_count: int
    citation_marker_count: int
    citation_label_count: int
    parse_error: str


@dataclass(frozen=True)
class _RoleMarker:
    role: str
    provider: str
    line: int
    start: int
    end: int


@dataclass
class _PendingExchange:
    turn_index: int
    user_text: str
    user_time: str
    user_locator: str
    assistant_blocks: list[tuple[str, str, str]]


def _provider_identity(source_url: str) -> tuple[str, str, str, str]:
    parsed = urlsplit(source_url)
    host = parsed.netloc.lower().split(":", 1)[0]
    parts = [part for part in parsed.path.split("/") if part]
    if host == "chatgpt.com" and len(parts) >= 2 and parts[0] == "c":
        return "chatgpt", parts[1], "", "provider"
    if host == "chat.deepseek.com" and len(parts) >= 4 and parts[:3] == ["a", "chat", "s"]:
        return "deepseek", "", parts[3], "provider-link"
    return "unknown", "", "", "path-derived"


def _safe_key(value: str) -> str:
    key = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return key[:96]


def _source_identity(
    provider: str,
    provider_session_id: str,
    provider_share_id: str,
    source_url: str,
    relative_path: str,
) -> tuple[str, str]:
    provider_key = provider_session_id or provider_share_id
    safe_provider_key = _safe_key(provider_key)
    if provider != "unknown" and safe_provider_key:
        return f"web-chat-{provider}-{safe_provider_key}", safe_provider_key
    digest = hashlib.sha256(
        "\0".join((provider, source_url, relative_path)).encode("utf-8")
    ).hexdigest()[:20]
    return f"web-chat-{provider}-{digest}", digest


def _role_markers(text: str) -> tuple[_RoleMarker, ...]:
    markers: list[_RoleMarker] = []
    fence_marker = ""
    offset = 0
    for line_number, line in enumerate(text.splitlines(keepends=True), start=1):
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
            offset += len(line)
            continue
        if not fence_marker:
            value = line.rstrip("\r\n")
            if value == "# you asked":
                markers.append(_RoleMarker("user", "", line_number, offset, offset + len(line)))
            elif value in {"# chatgpt response", "# deepseek response"}:
                provider = value.removeprefix("# ").removesuffix(" response")
                markers.append(
                    _RoleMarker("assistant", provider, line_number, offset, offset + len(line))
                )
        offset += len(line)
    return tuple(markers)


def _block_text(raw: str) -> str:
    value = raw.strip()
    value = re.sub(r"(?:^|\n)---\s*$", "", value).strip()
    return clean_message(value)


def _user_block(raw: str) -> tuple[str, str, bool]:
    lines = raw.strip().splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return "", "", True
    match = _MESSAGE_TIME.fullmatch(lines[0].strip())
    if match is None:
        return _block_text("\n".join(lines)), "", True
    value = match.group("value")
    try:
        datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        invalid = True
    else:
        invalid = False
    return _block_text("\n".join(lines[1:])), value, invalid


def parse_web_chat(text: str, provider: str, locator: str) -> WebChatParse:
    markers = _role_markers(text)
    exchanges: list[WebChatExchange] = []
    pending: _PendingExchange | None = None
    visible_users = 0
    assistant_blocks = 0
    omitted_unpaired = 0
    invalid_times = 0
    ambiguous_assistants = 0
    provider_mismatches = 0
    times: list[str] = []

    def finish_pending() -> None:
        nonlocal pending, omitted_unpaired, ambiguous_assistants
        if pending is None:
            return
        candidates = [item for item in pending.assistant_blocks if item[0]]
        if not candidates:
            omitted_unpaired += 1
        elif len(candidates) > 1:
            ambiguous_assistants += len(candidates)
        else:
            assistant_text, assistant_locator, assistant_provider = candidates[0]
            if assistant_provider != provider:
                pass
            exchanges.append(
                WebChatExchange(
                    turn_index=pending.turn_index,
                    user_text=pending.user_text,
                    assistant_text=assistant_text,
                    user_time=pending.user_time,
                    user_locator=pending.user_locator,
                    assistant_locator=assistant_locator,
                )
            )
        pending = None

    for index, marker in enumerate(markers):
        next_marker = markers[index + 1] if index + 1 < len(markers) else None
        block_end = next_marker.start if next_marker is not None else len(text)
        end_line = (next_marker.line - 1) if next_marker is not None else max(1, len(text.splitlines()))
        raw = text[marker.end:block_end]
        semantic = f"{locator}/Turn:{visible_users + 1}/{ 'User' if marker.role == 'user' else 'Assistant' }"
        block_locator = format_line_locator(semantic, marker.line, max(marker.line, end_line))
        if marker.role == "user":
            finish_pending()
            visible_users += 1
            user_text, user_time, invalid_time = _user_block(raw)
            invalid_times += int(invalid_time)
            if user_time:
                times.append(user_time)
            pending = _PendingExchange(
                turn_index=visible_users,
                user_text=user_text,
                user_time=user_time,
                user_locator=block_locator,
                assistant_blocks=[],
            )
            continue

        assistant_blocks += 1
        if marker.provider != provider:
            provider_mismatches += 1
        assistant_text = _block_text(raw)
        if pending is not None:
            assistant_locator = format_line_locator(
                f"{locator}/Turn:{pending.turn_index}/Assistant:{assistant_blocks}",
                marker.line,
                max(marker.line, end_line),
            )
            pending.assistant_blocks.append((assistant_text, assistant_locator, marker.provider))
    finish_pending()

    retained = tuple(
        exchange
        for exchange in exchanges
        if not is_exact_meaningless_exchange(exchange.user_text, exchange.assistant_text)
    )
    non_monotonic = sum(current < previous for previous, current in zip(times, times[1:]))
    return WebChatParse(
        exchanges=retained,
        visible_user_block_count=visible_users,
        assistant_block_count=assistant_blocks,
        assistant_final_count=len(retained),
        omitted_trivial_exchange_count=len(exchanges) - len(retained),
        omitted_unpaired_user_count=omitted_unpaired,
        invalid_message_time_count=invalid_times,
        non_monotonic_message_time_count=non_monotonic,
        ambiguous_assistant_block_count=ambiguous_assistants,
        provider_role_mismatch_count=provider_mismatches,
        first_message_time=times[0] if times else "",
        last_message_time=times[-1] if times else "",
    )


def _sandbox_targets(text: str) -> tuple[str, ...]:
    return tuple(match.group("target") for match in _SANDBOX_LINK.finditer(text))


def _sandbox_candidate(path: Path, target: str) -> Path | None:
    filename = Path(urlsplit(target).path).name
    for candidate in (path.parent / filename, path.parent / "files" / filename):
        if candidate.is_file():
            return candidate.resolve()
    return None


def _raw_assets(
    text: str,
    path: Path,
    input_root: Path,
) -> tuple[tuple[RawAsset, ...], int, int, int, int, int]:
    scan = scan_markdown(text, path)
    root = input_root.resolve()
    references: dict[tuple[Path, str], int] = Counter()
    missing_local = 0
    missing_sandbox = 0
    unsupported = 0
    escaped = 0

    for reference in scan.image_references:
        parsed = urlsplit(reference.target)
        if parsed.scheme in {"http", "https"}:
            continue
        if parsed.scheme or reference.target.startswith("/"):
            unsupported += 1
            continue
        source_path = (path.parent / unquote(parsed.path)).resolve()
        try:
            source_path.relative_to(root)
        except ValueError:
            escaped += 1
            continue
        if not source_path.is_file():
            missing_local += 1
            continue
        if source_path.suffix.lower() not in _SUPPORTED_ASSET_SUFFIXES:
            unsupported += 1
            continue
        references[(source_path, reference.target)] += 1

    for target in _sandbox_targets(text):
        source_path = _sandbox_candidate(path, target)
        if source_path is None:
            missing_sandbox += 1
            continue
        try:
            source_path.relative_to(root)
        except ValueError:
            escaped += 1
            continue
        if source_path.suffix.lower() not in _SUPPORTED_ASSET_SUFFIXES:
            unsupported += 1
            continue
        references[(source_path, target)] += 1

    assets = tuple(
        RawAsset(source_path, target, sha256_file(source_path), count)
        for (source_path, target), count in sorted(
            references.items(), key=lambda item: (str(item[0][0]), item[0][1])
        )
    )
    return assets, len(scan.image_references), missing_local, missing_sandbox, unsupported, escaped


def _build_unit(path: Path, input_root: Path) -> WebChatUnit:
    relative_path = path.resolve().relative_to(input_root.resolve()).as_posix()
    raw = path.read_bytes()
    content_hash = hashlib.sha256(raw).hexdigest()
    line_count = max(1, len(raw.splitlines()))
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        text = raw.decode("utf-8-sig", errors="replace")
        parse_error = f"invalid_utf8:{exc.start}"
    else:
        parse_error = ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    source_match = _FROM_LINE.search(text)
    source_url = source_match.group("url") if source_match else ""
    provider, provider_session_id, provider_share_id, identity_confidence = _provider_identity(source_url)
    source_id, derived_session_key = _source_identity(
        provider,
        provider_session_id,
        provider_share_id,
        source_url,
        relative_path,
    )
    semantic_locator = f"{relative_path}#Conversation:{derived_session_key}"
    locator = format_line_locator(semantic_locator, 1, line_count)
    parsed: WebChatParse | None = None
    raw_assets: tuple[RawAsset, ...] = ()
    image_references = 0
    missing_local = 0
    missing_sandbox = 0
    unsupported = 0
    escaped = 0
    if not parse_error:
        try:
            parsed = parse_web_chat(text, provider, semantic_locator)
            (
                raw_assets,
                image_references,
                missing_local,
                missing_sandbox,
                unsupported,
                escaped,
            ) = _raw_assets(text, path, input_root)
        except (MarkdownImportError, ValueError) as exc:
            parse_error = str(exc)
    citations = list(_CITATION.finditer(text))
    created = parsed.first_message_time[:10] if parsed and parsed.first_message_time else "unknown"
    updated = parsed.last_message_time[:10] if parsed and parsed.last_message_time else created
    label = {"chatgpt": "ChatGPT", "deepseek": "DeepSeek"}.get(provider, "Web Chat")
    return WebChatUnit(
        source_id=source_id,
        provider=provider,
        provider_session_id=provider_session_id,
        provider_share_id=provider_share_id,
        derived_session_key=derived_session_key,
        identity_confidence=identity_confidence,
        source_url=source_url,
        title=f"{label} 会话：{path.stem}",
        created=created,
        updated=updated,
        source_path=path,
        relative_path=relative_path,
        locator=locator,
        content_hash=content_hash,
        start_line=1,
        end_line=line_count,
        parse=parsed,
        raw_assets=raw_assets,
        image_reference_count=image_references,
        missing_local_asset_count=missing_local,
        missing_sandbox_asset_count=missing_sandbox,
        unsupported_asset_type_count=unsupported,
        asset_path_escape_count=escaped,
        citation_marker_count=len(citations),
        citation_label_count=len({match.group("label") for match in citations}),
        parse_error=parse_error,
    )


def iter_web_chat_units(input_dir: Path) -> Iterable[WebChatUnit]:
    if not input_dir.is_dir():
        raise FileNotFoundError(f"Web chat 导出目录不存在: {input_dir}")
    for path in sorted(input_dir.rglob("*.md")):
        if any(part.startswith(".") for part in path.relative_to(input_dir).parts):
            continue
        yield _build_unit(path, input_dir)


def unit_review_reasons(unit: WebChatUnit) -> tuple[str, ...]:
    reasons: list[str] = []
    parsed = unit.parse
    if unit.parse_error:
        reasons.append("web_chat_parse_review")
    if unit.provider == "unknown":
        reasons.append("provider_detection_review")
    if parsed and parsed.provider_role_mismatch_count:
        reasons.append("provider_role_mismatch_review")
    if parsed and parsed.invalid_message_time_count:
        reasons.append("message_time_review")
    if parsed and parsed.non_monotonic_message_time_count:
        reasons.append("message_time_order_review")
    if parsed and parsed.ambiguous_assistant_block_count:
        reasons.append("assistant_branch_ambiguous_review")
    if unit.asset_path_escape_count:
        reasons.append("asset_path_escape_review")
    if unit.missing_local_asset_count:
        reasons.append("local_asset_missing_review")
    if unit.unsupported_asset_type_count:
        reasons.append("asset_type_unsupported_review")
    if unit.missing_sandbox_asset_count:
        reasons.append("sandbox_asset_missing_review")
    if unit.citation_marker_count:
        reasons.append("citation_targets_missing_review")
    return tuple(reasons)


def unit_skip_reason(unit: WebChatUnit) -> str:
    review_reasons = unit_review_reasons(unit)
    if review_reasons:
        return review_reasons[0]
    parsed = unit.parse
    if parsed is None or not parsed.visible_user_block_count:
        return "no_human_user"
    if not parsed.exchanges:
        if parsed.omitted_trivial_exchange_count == parsed.visible_user_block_count:
            return "trivial_session"
        return "no_final_visible"
    return ""


def _review_details(unit: WebChatUnit) -> list[str]:
    details: list[str] = []
    if unit.citation_marker_count:
        details.append(
            f"引用占位符 {unit.citation_marker_count} 个（{unit.citation_label_count} 个编号），无目标 URL 映射"
        )
    if unit.missing_sandbox_asset_count:
        details.append(f"ChatGPT sandbox 资源缺失 {unit.missing_sandbox_asset_count} 个")
    if unit.missing_local_asset_count:
        details.append(f"本地 Markdown 资源缺失 {unit.missing_local_asset_count} 个")
    if unit.parse and unit.parse.omitted_unpaired_user_count:
        details.append(
            f"导出可见但无 assistant final 的 user turn {unit.parse.omitted_unpaired_user_count} 个"
        )
    if unit.parse_error:
        details.append("原始 Markdown 无法按当前 web-chat 格式完整解析")
    return details


def unit_inventory_record(
    unit: WebChatUnit,
    parse_status: str | None = None,
    skip_reason: str | None = None,
) -> dict[str, Any]:
    reason = unit_skip_reason(unit) if skip_reason is None else skip_reason
    status = parse_status or ("review" if reason in REVIEW_SKIP_REASONS else "excluded" if reason else "ready")
    parsed = unit.parse
    return {
        "source_id": unit.source_id,
        "unit_kind": "session",
        "thread_kind": "main",
        "provider": unit.provider,
        "provider_session_id": unit.provider_session_id or None,
        "provider_share_id": unit.provider_share_id or None,
        "derived_session_key": unit.derived_session_key,
        "identity_confidence": unit.identity_confidence,
        "source_url": unit.source_url or None,
        "title": unit.title,
        "created": unit.created,
        "updated": unit.updated,
        "raw_source_path": str(unit.source_path.resolve()),
        "raw_source_hash": f"sha256:{unit.content_hash}",
        "raw_source_locator": unit.locator,
        "raw_source_start_line": unit.start_line,
        "raw_source_end_line": unit.end_line,
        "source_format": SOURCE_FORMAT,
        "source_completeness": "summary",
        "event_count_scope": "export-visible",
        "assistant_final_detection": WEB_CHAT_ASSISTANT_FINAL_DETECTION,
        "parse_status": status,
        "skip_reason": reason or None,
        "review_reasons": list(unit_review_reasons(unit)),
        "review_details": _review_details(unit),
        "visible_user_block_count": parsed.visible_user_block_count if parsed else 0,
        "human_user_count": parsed.visible_user_block_count if parsed else 0,
        "assistant_block_count": parsed.assistant_block_count if parsed else 0,
        "assistant_final_count": parsed.assistant_final_count if parsed else 0,
        "omitted_trivial_exchange_count": parsed.omitted_trivial_exchange_count if parsed else 0,
        "omitted_unpaired_user_count": parsed.omitted_unpaired_user_count if parsed else 0,
        "image_reference_count": unit.image_reference_count,
        "asset_count": len(unit.raw_assets),
        "missing_local_asset_count": unit.missing_local_asset_count,
        "missing_sandbox_asset_count": unit.missing_sandbox_asset_count,
        "citation_marker_count": unit.citation_marker_count,
        "citation_label_count": unit.citation_label_count,
    }


def _stored_assets(unit: WebChatUnit, asset_root: Path) -> tuple[StoredAsset, ...]:
    stored: list[StoredAsset] = []
    for raw in unit.raw_assets:
        safe_name, redactions = redact_secrets(raw.source_path.name)
        if redactions:
            safe_name = f"asset-{raw.digest[:12]}{raw.source_path.suffix.lower()}"
        stored_path = (
            asset_root
            / "conversation"
            / unit.provider
            / unit.source_id
            / f"{raw.digest[:12]}-{safe_name}"
        )
        rendered_target = Path(
            "..",
            "..",
            "assets",
            "conversation",
            unit.provider,
            unit.source_id,
            stored_path.name,
        ).as_posix()
        stored.append(StoredAsset(raw, stored_path, rendered_target))
    return tuple(stored)


def _rewrite_targets(text: str, assets: tuple[StoredAsset, ...]) -> str:
    rendered = text
    for asset in assets:
        rendered = rendered.replace(
            f"]({asset.raw.source_target})", f"]({asset.rendered_target})"
        )
    return rendered


def _asset_records(assets: tuple[StoredAsset, ...]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for asset in assets:
        source_target, _ = redact_secrets(asset.raw.source_target)
        records.append(
            {
                "source_path": str(asset.raw.source_path.resolve()),
                "source_target": source_target,
                "stored_path": relative_to_repo(asset.stored_path),
                "asset_hash": asset.raw.digest,
                "reference_count": asset.raw.reference_count,
            }
        )
    return records


def _render_session(
    unit: WebChatUnit,
    assets: tuple[StoredAsset, ...],
    imported: str,
) -> tuple[str, int, str]:
    if unit.parse is None:
        raise ValueError(f"Web chat 会话缺少解析结果: {unit.locator}")
    title, redactions = redact_secrets(unit.title)
    rendered: list[str] = []
    for ordinal, exchange in enumerate(unit.parse.exchanges, start=1):
        user_text, count = redact_secrets(_rewrite_targets(exchange.user_text, assets))
        redactions += count
        assistant_text, count = redact_secrets(_rewrite_targets(exchange.assistant_text, assets))
        redactions += count
        if not user_text or not assistant_text:
            continue
        rendered.append(
            f"## Exchange {ordinal}\n\n"
            f"- **Turn**: {exchange.turn_index}\n"
            f"- **Message time**: {exchange.user_time or 'unknown'}\n"
            f"- **User locator**: `{exchange.user_locator}`\n"
            f"- **Assistant locator**: `{exchange.assistant_locator}`\n\n"
            f"### Human user\n\n{user_text}\n\n"
            f"### Assistant final\n\n{assistant_text}"
        )
    asset_records = _asset_records(assets)
    metadata: dict[str, Any] = {
        "id": unit.source_id,
        "type": "conversation",
        "origin": ORIGIN,
        "source_kind": "session",
        "provider": unit.provider,
        "thread_kind": "main",
        "provider_session_id": unit.provider_session_id or None,
        "provider_share_id": unit.provider_share_id or None,
        "derived_session_key": unit.derived_session_key,
        "identity_confidence": unit.identity_confidence,
        "source_url": unit.source_url,
        "source_format": SOURCE_FORMAT,
        "source_completeness": "summary",
        "event_count_scope": "export-visible",
        "assistant_final_detection": WEB_CHAT_ASSISTANT_FINAL_DETECTION,
        "title": title,
        "created": unit.created,
        "updated": unit.updated,
        "imported": imported,
        "project": "web-chats",
        "source_path": str(unit.source_path.resolve()),
        "source_locator": unit.locator,
        "source_hash": f"sha256:{unit.content_hash}",
        "exchange_count": len(rendered),
        "human_user_count": unit.parse.visible_user_block_count,
        "assistant_final_count": unit.parse.assistant_final_count,
        "omitted_trivial_exchange_count": unit.parse.omitted_trivial_exchange_count,
        "omitted_unpaired_user_count": unit.parse.omitted_unpaired_user_count,
        "image_reference_count": unit.image_reference_count,
        "asset_count": len(assets),
        "citation_marker_count": unit.citation_marker_count,
        "redaction_count": redactions,
        "importer_version": WEB_CHAT_IMPORTER_VERSION,
    }
    if asset_records:
        metadata["assets"] = asset_records
    body = (
        f"# {title}\n\n"
        "> 本页从有损的浏览器插件 Markdown 导出生成，只保留导出中可见的 human user 与"
        "结构启发式确认的 assistant final。上游未导出的模型、分支、message ID 和隐藏事件无法恢复。\n\n"
        + "\n\n".join(rendered)
    )
    return yaml_document(metadata, body), redactions, title


def _assets_are_current(records: Any) -> bool:
    if not isinstance(records, list):
        return not records
    for item in records:
        if not isinstance(item, dict):
            return False
        path = REPO_ROOT / str(item.get("stored_path") or "")
        if not path.is_file() or sha256_file(path) != item.get("asset_hash"):
            return False
    return True


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(source.read_bytes())
    temporary.replace(target)


def import_web_chats(
    input_dir: Path,
    output_root: Path,
    manifest_path: Path,
    *,
    asset_root: Path = DEFAULT_ASSETS,
    dry_run: bool = False,
) -> dict[str, Any]:
    units = list(iter_web_chat_units(input_dir))
    source_id_counts = Counter(unit.source_id for unit in units)
    manifest = load_manifest(manifest_path)
    change_tracker = SourceChangeTracker.for_output(output_root)
    stats: dict[str, Any] = {
        "discovered": 0,
        "imported": 0,
        "unchanged": 0,
        "skipped": 0,
        "skip_reasons": {},
        "providers": {},
        "human_user_messages": 0,
        "assistant_finals": 0,
        "omitted_trivial_exchanges": 0,
        "omitted_unpaired_users": 0,
        "citations_missing": 0,
        "assets": 0,
        "redactions": 0,
    }
    changed = False
    for unit in units:
        stats["discovered"] += 1
        providers = stats["providers"]
        providers[unit.provider] = providers.get(unit.provider, 0) + 1
        if unit.parse:
            stats["human_user_messages"] += unit.parse.visible_user_block_count
            stats["assistant_finals"] += unit.parse.assistant_final_count
            stats["omitted_trivial_exchanges"] += unit.parse.omitted_trivial_exchange_count
            stats["omitted_unpaired_users"] += unit.parse.omitted_unpaired_user_count
        stats["citations_missing"] += unit.citation_marker_count
        stats["assets"] += len(unit.raw_assets)

        skip_reason = (
            "source_id_collision_review"
            if source_id_counts[unit.source_id] > 1
            else unit_skip_reason(unit)
        )
        current = manifest["sources"].get(unit.source_id, {})
        assets = _stored_assets(unit, asset_root)
        output_path = output_root / unit.provider / f"{unit.created}-{unit.derived_session_key}.md"
        is_unchanged = bool(
            not skip_reason
            and current.get("source_hash") == unit.content_hash
            and current.get("importer_version") == WEB_CHAT_IMPORTER_VERSION
            and Path(REPO_ROOT / str(current.get("output_path") or "")).is_file()
            and _assets_are_current(current.get("assets", []))
            and not source_needs_redaction(Path(REPO_ROOT / str(current.get("output_path") or "")))
        )
        if skip_reason:
            stats["skipped"] += 1
            reasons = stats["skip_reasons"]
            reasons[skip_reason] = reasons.get(skip_reason, 0) + 1
            continue
        if is_unchanged:
            stats["unchanged"] += 1
            continue

        document, redactions, title = _render_session(unit, assets, utc_now()[:10])
        stats["imported"] += 1
        stats["redactions"] += redactions
        if dry_run:
            continue
        change_tracker.observe(output_path)
        for asset in assets:
            change_tracker.observe(asset.stored_path)
            _atomic_copy(asset.raw.source_path, asset.stored_path)
        _atomic_write(output_path, document)
        manifest["version"] = max(int(manifest.get("version", 1)), 2)
        manifest["sources"][unit.source_id] = {
            "origin": ORIGIN,
            "source_kind": "session",
            "provider": unit.provider,
            "thread_kind": "main",
            "provider_session_id": unit.provider_session_id or None,
            "provider_share_id": unit.provider_share_id or None,
            "derived_session_key": unit.derived_session_key,
            "identity_confidence": unit.identity_confidence,
            "assistant_final_detection": WEB_CHAT_ASSISTANT_FINAL_DETECTION,
            "source_path": str(unit.source_path.resolve()),
            "source_locator": unit.locator,
            "source_hash": unit.content_hash,
            "output_path": relative_to_repo(output_path),
            "ingest_status": "ready",
            "curation_status": current.get("curation_status", "unassessed"),
            "title": title,
            "created": unit.created,
            "updated": unit.updated,
            "omitted_trivial_exchange_count": unit.parse.omitted_trivial_exchange_count if unit.parse else 0,
            "omitted_unpaired_user_count": unit.parse.omitted_unpaired_user_count if unit.parse else 0,
            "redaction_count": redactions,
            "assets": _asset_records(assets),
            "imported_at": utc_now(),
            "importer_version": WEB_CHAT_IMPORTER_VERSION,
        }
        changed = True

    if not dry_run and changed:
        save_manifest(manifest, manifest_path)
        change_tracker.append("web-chat")
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--assets", type=Path, default=DEFAULT_ASSETS)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    stats = import_web_chats(
        args.input,
        args.output,
        args.manifest,
        asset_root=args.assets,
        dry_run=args.dry_run,
    )
    print("Web chat 导入结果:", ", ".join(f"{key}={value}" for key, value in stats.items()))


if __name__ == "__main__":
    main()
