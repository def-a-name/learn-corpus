#!/usr/bin/env python3
"""把 Codex rollout JSONL 白名单化为主会话显式问答来源。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from wiki_core import (
    CODEX_ASSISTANT_FINAL_DETECTION,
    CODEX_IMPORTER_VERSION,
    MANIFEST_PATH,
    REPO_ROOT,
    clean_message,
    derive_title,
    extract_text,
    format_line_locator,
    is_exact_meaningless_exchange,
    is_noise_message,
    load_manifest,
    redact_secrets,
    relative_to_repo,
    save_manifest,
    sha256_file,
    source_needs_redaction,
    utc_now,
    yaml_document,
)


DEFAULT_INPUT = Path.home() / ".codex" / "sessions"
DEFAULT_OUTPUT = REPO_ROOT / "sources" / "conversations" / "codex"
RUNTIME_USER_TAG = re.compile(r"<(?:model_instruction|user_action)(?:>|\s)", re.IGNORECASE)
REVIEW_SKIP_REASONS = {
    "fork_cycle_review",
    "fork_parent_missing_review",
    "fork_parent_not_standardized_review",
    "fork_parent_unusable_review",
    "fork_prefix_mismatch_review",
    "invalid_jsonl_review",
    "thread_kind_review",
    "turn_boundary_review",
}
DEFERRED_SKIP_REASONS = {
    "active_session_deferred",
    "fork_parent_deferred",
    "fork_parent_changed_during_import",
    "source_changed_during_import",
}


@dataclass(frozen=True)
class CodexExchange:
    turn_index: int
    user_text: str
    assistant_text: str
    user_locator: str
    assistant_locator: str


@dataclass(frozen=True)
class CodexSession:
    source_id: str
    provider_session_id: str
    identity_confidence: str
    created: str
    project: str
    cwd: str
    cli_version: str
    thread_kind: str
    parent_thread_id: str | None
    forked_from_id: str | None
    source_path: Path
    content_hash: str
    locator: str
    line_count: int
    title: str
    exchanges: tuple[CodexExchange, ...]
    visible_user_count: int
    human_user_count: int
    visible_assistant_final_count: int
    assistant_final_count: int
    omitted_trivial_exchange_count: int
    omitted_runtime_user_count: int
    omitted_developer_count: int
    omitted_commentary_count: int
    omitted_unphased_assistant_count: int
    omitted_tool_call_count: int
    omitted_tool_result_count: int
    omitted_reasoning_count: int
    omitted_compaction_count: int
    omitted_interrupted_count: int
    omitted_unpaired_user_count: int
    event_user_message_count: int
    invalid_line_count: int
    open_turn_count: int
    ambiguous_user_turn_count: int
    ambiguous_final_turn_count: int
    extra_session_meta_count: int
    parse_notes: tuple[str, ...]


@dataclass(frozen=True)
class CodexResolvedSession:
    unit: CodexSession
    exchanges: tuple[CodexExchange, ...]
    title: str
    source_scope: str
    skip_reason: str
    fork_parent_source_id: str | None = None
    fork_parent_hash: str | None = None
    omitted_fork_prefix_exchange_count: int = 0


@dataclass
class _Turn:
    ordinal: int
    human_users: list[tuple[int, str]] = field(default_factory=list)
    assistant_finals: list[tuple[int, str]] = field(default_factory=list)


def _classify_user_text(raw: str) -> tuple[str, str]:
    if is_noise_message(raw) or RUNTIME_USER_TAG.search(raw):
        return "runtime", ""
    cleaned = clean_message(raw)
    if not cleaned:
        return "runtime", ""
    if re.fullmatch(r"/[A-Za-z][\w-]*(?:\s+[^\n]*)?", cleaned):
        return "runtime", ""
    return "human", cleaned


def _thread_kind(metadata: dict[str, Any]) -> str:
    thread_source = metadata.get("thread_source")
    source = metadata.get("source")
    if thread_source == "subagent" or (isinstance(source, dict) and "subagent" in source):
        return "subagent"
    if thread_source == "user" or source == "cli":
        return "main"
    return "unknown"


def _filename_session_id(path: Path) -> str:
    match = re.search(r"([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})$", path.stem)
    return match.group(1) if match else path.stem.removeprefix("rollout-")


def parse_session(path: Path, input_root: Path | None = None) -> CodexSession:
    raw = path.read_bytes()
    content_hash = hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    records: list[tuple[int, dict[str, Any]]] = []
    invalid_lines = 0
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            invalid_lines += 1
            continue
        if isinstance(value, dict):
            records.append((line_number, value))

    session_metas = [
        (line_number, record["payload"])
        for line_number, record in records
        if record.get("type") == "session_meta" and isinstance(record.get("payload"), dict)
    ]
    first_meta = session_metas[0][1] if session_metas else {}
    provider_session_id = str(first_meta.get("id") or _filename_session_id(path))
    identity_confidence = "provider" if first_meta.get("id") else "path-derived"
    created = str(first_meta.get("timestamp") or next((record.get("timestamp") for _, record in records), ""))[:10]
    cwd = str(first_meta.get("cwd") or "")
    project = Path(cwd).name if cwd else "global"
    try:
        relative_path = path.relative_to(input_root or DEFAULT_INPUT).as_posix()
    except ValueError:
        relative_path = path.name
    semantic_locator = f"{relative_path}#Session:{provider_session_id}"
    session_locator = format_line_locator(semantic_locator, 1, max(len(lines), 1))

    exchanges: list[CodexExchange] = []
    current: _Turn | None = None
    turn_ordinal = 0
    visible_users = 0
    human_users = 0
    visible_finals = 0
    runtime_users = 0
    developer_messages = 0
    commentary_messages = 0
    unphased_assistant_messages = 0
    tool_calls = 0
    tool_results = 0
    reasoning_items = 0
    compactions = 0
    interruptions = 0
    unpaired_users = 0
    event_users = 0
    open_turns = 0
    ambiguous_users = 0
    ambiguous_finals = 0
    orphan_finals = 0
    notes: set[str] = set()

    def finish_turn(terminal: str) -> None:
        nonlocal current, open_turns, interruptions, unpaired_users
        nonlocal ambiguous_users, ambiguous_finals, orphan_finals
        if current is None:
            return
        if terminal == "open":
            open_turns += 1
        elif terminal == "aborted":
            interruptions += 1
        if len(current.human_users) > 1:
            ambiguous_users += 1
        if len(current.assistant_finals) > 1:
            ambiguous_finals += 1
        if terminal == "complete" and len(current.human_users) == 1 and len(current.assistant_finals) == 1:
            user_line, user_text = current.human_users[0]
            assistant_line, assistant_text = current.assistant_finals[0]
            exchanges.append(
                CodexExchange(
                    turn_index=current.ordinal,
                    user_text=user_text,
                    assistant_text=assistant_text,
                    user_locator=format_line_locator(
                        f"{semantic_locator}/Turn:{current.ordinal}/User", user_line, user_line
                    ),
                    assistant_locator=format_line_locator(
                        f"{semantic_locator}/Turn:{current.ordinal}/AssistantFinal", assistant_line, assistant_line
                    ),
                )
            )
        else:
            if current.human_users:
                unpaired_users += len(current.human_users)
            elif current.assistant_finals:
                orphan_finals += len(current.assistant_finals)
        current = None

    for line_number, record in records:
        record_type = record.get("type")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        payload_type = payload.get("type")

        if record_type == "event_msg" and payload_type == "task_started":
            finish_turn("open")
            turn_ordinal += 1
            current = _Turn(turn_ordinal)
            continue

        if record_type == "event_msg" and payload_type == "user_message":
            event_users += 1
        elif record_type == "response_item" and payload_type == "message":
            role = payload.get("role")
            if role == "developer":
                developer_messages += 1
                continue
            if role == "user":
                visible_users += 1
                raw_text = extract_text(payload.get("content"), {"input_text"})
                classification, cleaned = _classify_user_text(raw_text)
                if classification == "runtime":
                    runtime_users += 1
                elif current is None:
                    notes.add("发现不在 task 生命周期内的 human user message；未写入标准化正文。")
                else:
                    human_users += 1
                    current.human_users.append((line_number, cleaned))
                continue
            if role == "assistant":
                assistant_text = clean_message(extract_text(payload.get("content"), {"output_text"}))
                phase = payload.get("phase")
                if phase == "final_answer":
                    visible_finals += 1
                    if assistant_text and current is not None:
                        current.assistant_finals.append((line_number, assistant_text))
                    elif assistant_text:
                        orphan_finals += 1
                elif phase == "commentary":
                    commentary_messages += 1
                else:
                    unphased_assistant_messages += 1
                continue
        elif record_type == "response_item" and payload_type in {
            "custom_tool_call",
            "function_call",
            "web_search_call",
            "tool_search_call",
        }:
            tool_calls += 1
        elif record_type == "response_item" and payload_type in {
            "custom_tool_call_output",
            "function_call_output",
            "tool_search_output",
        }:
            tool_results += 1
        elif record_type == "response_item" and payload_type == "reasoning":
            reasoning_items += 1
        elif record_type == "compacted":
            compactions += 1

        if record_type == "event_msg" and payload_type in {"task_complete", "turn_aborted"}:
            finish_turn("complete" if payload_type == "task_complete" else "aborted")

    finish_turn("open")
    if unpaired_users:
        notes.add("存在没有显式 assistant final 的 human user turn；该 turn 未写入标准化正文。")
    if orphan_finals:
        notes.add("存在无法与 human user 配对的 assistant final；未写入标准化正文。")
    if open_turns:
        notes.add("文件包含未结束 task，可能仍在写入；本轮不导入。")
    if invalid_lines:
        notes.add("文件包含无法解析的 JSONL 行。")
    if len(session_metas) > 1:
        notes.add("文件包含额外 session_meta；身份只取文件首个 metadata。")

    retained_exchanges = tuple(
        exchange
        for exchange in exchanges
        if not is_exact_meaningless_exchange(exchange.user_text, exchange.assistant_text)
    )
    omitted_trivial_exchanges = len(exchanges) - len(retained_exchanges)
    title_messages = [{"role": "user", "text": exchange.user_text} for exchange in retained_exchanges]
    title = derive_title("codex", title_messages) if title_messages else "Codex 会话：未形成完整问答"
    return CodexSession(
        source_id=f"codex-{provider_session_id}",
        provider_session_id=provider_session_id,
        identity_confidence=identity_confidence,
        created=created or "unknown",
        project=project,
        cwd=cwd,
        cli_version=str(first_meta.get("cli_version") or ""),
        thread_kind=_thread_kind(first_meta),
        parent_thread_id=str(first_meta.get("parent_thread_id")) if first_meta.get("parent_thread_id") else None,
        forked_from_id=str(first_meta.get("forked_from_id")) if first_meta.get("forked_from_id") else None,
        source_path=path,
        content_hash=content_hash,
        locator=session_locator,
        line_count=max(len(lines), 1),
        title=title,
        exchanges=retained_exchanges,
        visible_user_count=visible_users,
        human_user_count=human_users,
        visible_assistant_final_count=visible_finals,
        assistant_final_count=len(retained_exchanges),
        omitted_trivial_exchange_count=omitted_trivial_exchanges,
        omitted_runtime_user_count=runtime_users,
        omitted_developer_count=developer_messages,
        omitted_commentary_count=commentary_messages,
        omitted_unphased_assistant_count=unphased_assistant_messages,
        omitted_tool_call_count=tool_calls,
        omitted_tool_result_count=tool_results,
        omitted_reasoning_count=reasoning_items,
        omitted_compaction_count=compactions,
        omitted_interrupted_count=interruptions,
        omitted_unpaired_user_count=unpaired_users,
        event_user_message_count=event_users,
        invalid_line_count=invalid_lines,
        open_turn_count=open_turns,
        ambiguous_user_turn_count=ambiguous_users,
        ambiguous_final_turn_count=ambiguous_finals,
        extra_session_meta_count=max(len(session_metas) - 1, 0),
        parse_notes=tuple(sorted(notes)),
    )


def iter_session_units(input_dir: Path) -> Iterable[CodexSession]:
    for path in sorted(input_dir.rglob("*.jsonl")):
        yield parse_session(path, input_dir)


def _intrinsic_skip_reason(unit: CodexSession) -> str:
    if unit.thread_kind == "subagent":
        return "subagent_excluded"
    if unit.thread_kind != "main":
        return "thread_kind_review"
    if unit.open_turn_count:
        return "active_session_deferred"
    if unit.invalid_line_count:
        return "invalid_jsonl_review"
    if unit.ambiguous_user_turn_count or unit.ambiguous_final_turn_count:
        return "turn_boundary_review"
    if not unit.human_user_count:
        return "no_human_user"
    if not unit.exchanges:
        if unit.omitted_trivial_exchange_count == unit.human_user_count:
            return "trivial_session"
        return "no_final_visible"
    return ""


def _common_exchange_prefix(parent: CodexSession, child: CodexSession) -> int:
    prefix = 0
    for parent_exchange, child_exchange in zip(parent.exchanges, child.exchanges):
        if (
            parent_exchange.user_text != child_exchange.user_text
            or parent_exchange.assistant_text != child_exchange.assistant_text
        ):
            break
        prefix += 1
    return prefix


def resolve_session_units(units: Iterable[CodexSession]) -> tuple[CodexResolvedSession, ...]:
    ordered = tuple(units)
    by_provider_id: dict[str, CodexSession] = {}
    for unit in ordered:
        if unit.provider_session_id in by_provider_id:
            raise ValueError(f"重复 Codex provider session ID: {unit.provider_session_id}")
        by_provider_id[unit.provider_session_id] = unit

    resolved: dict[str, CodexResolvedSession] = {}
    resolving: set[str] = set()

    def resolve(unit: CodexSession) -> CodexResolvedSession:
        cached = resolved.get(unit.provider_session_id)
        if cached is not None:
            return cached
        intrinsic_reason = _intrinsic_skip_reason(unit)
        if intrinsic_reason:
            result = CodexResolvedSession(
                unit=unit,
                exchanges=unit.exchanges,
                title=unit.title,
                source_scope="fork_unresolved" if unit.forked_from_id else "full",
                skip_reason=intrinsic_reason,
            )
            resolved[unit.provider_session_id] = result
            return result
        if not unit.forked_from_id:
            result = CodexResolvedSession(
                unit=unit,
                exchanges=unit.exchanges,
                title=unit.title,
                source_scope="full",
                skip_reason="",
            )
            resolved[unit.provider_session_id] = result
            return result
        if unit.provider_session_id in resolving or unit.forked_from_id in resolving:
            result = CodexResolvedSession(
                unit=unit,
                exchanges=unit.exchanges,
                title=unit.title,
                source_scope="fork_unresolved",
                skip_reason="fork_cycle_review",
            )
            resolved[unit.provider_session_id] = result
            return result

        parent = by_provider_id.get(unit.forked_from_id)
        if parent is None:
            result = CodexResolvedSession(
                unit=unit,
                exchanges=unit.exchanges,
                title=unit.title,
                source_scope="fork_unresolved",
                skip_reason="fork_parent_missing_review",
            )
            resolved[unit.provider_session_id] = result
            return result

        resolving.add(unit.provider_session_id)
        parent_result = resolve(parent)
        resolving.discard(unit.provider_session_id)
        if parent_result.skip_reason:
            reason = (
                "fork_parent_deferred"
                if parent_result.skip_reason in DEFERRED_SKIP_REASONS
                else "fork_parent_unusable_review"
            )
            result = CodexResolvedSession(
                unit=unit,
                exchanges=unit.exchanges,
                title=unit.title,
                source_scope="fork_unresolved",
                skip_reason=reason,
                fork_parent_source_id=parent.source_id,
                fork_parent_hash=parent.content_hash,
            )
            resolved[unit.provider_session_id] = result
            return result

        prefix = _common_exchange_prefix(parent, unit)
        if prefix == 0:
            result = CodexResolvedSession(
                unit=unit,
                exchanges=unit.exchanges,
                title=unit.title,
                source_scope="fork_unresolved",
                skip_reason="fork_prefix_mismatch_review",
                fork_parent_source_id=parent.source_id,
                fork_parent_hash=parent.content_hash,
            )
            resolved[unit.provider_session_id] = result
            return result

        selected = unit.exchanges[prefix:]
        title_messages = [{"role": "user", "text": exchange.user_text} for exchange in selected]
        title = derive_title("codex", title_messages) if title_messages else unit.title
        result = CodexResolvedSession(
            unit=unit,
            exchanges=selected,
            title=title,
            source_scope="fork_delta",
            skip_reason="" if selected else "fork_no_novel_exchange",
            fork_parent_source_id=parent.source_id,
            fork_parent_hash=parent.content_hash,
            omitted_fork_prefix_exchange_count=prefix,
        )
        resolved[unit.provider_session_id] = result
        return result

    return tuple(resolve(unit) for unit in ordered)


def iter_resolved_session_units(input_dir: Path) -> Iterable[CodexResolvedSession]:
    yield from resolve_session_units(iter_session_units(input_dir))


def _dependency_order(
    units: Iterable[CodexResolvedSession],
) -> tuple[CodexResolvedSession, ...]:
    ordered = tuple(units)
    by_source_id = {item.unit.source_id: item for item in ordered}
    result: list[CodexResolvedSession] = []
    visited: set[str] = set()
    visiting: set[str] = set()

    def visit(item: CodexResolvedSession) -> None:
        source_id = item.unit.source_id
        if source_id in visited or source_id in visiting:
            return
        visiting.add(source_id)
        if item.fork_parent_source_id:
            parent = by_source_id.get(item.fork_parent_source_id)
            if parent is not None:
                visit(parent)
        visiting.discard(source_id)
        visited.add(source_id)
        result.append(item)

    for item in ordered:
        visit(item)
    return tuple(result)


def _manifest_source_is_current(
    resolved: CodexResolvedSession,
    manifest: dict[str, Any],
) -> bool:
    current = manifest["sources"].get(resolved.unit.source_id, {})
    output_value = str(current.get("output_path") or "")
    output_path = REPO_ROOT / output_value if output_value else Path()
    return bool(
        not resolved.skip_reason
        and current.get("source_hash") == resolved.unit.content_hash
        and current.get("fork_parent_hash") == resolved.fork_parent_hash
        and current.get("importer_version") == CODEX_IMPORTER_VERSION
        and output_path.is_file()
        and not source_needs_redaction(output_path)
    )


def unit_inventory_record(
    resolved: CodexResolvedSession,
    parse_status: str | None = None,
    skip_reason: str | None = None,
) -> dict[str, Any]:
    unit = resolved.unit
    reason = resolved.skip_reason if skip_reason is None else skip_reason
    status = parse_status or ("excluded" if reason else "ready")
    return {
        "source_id": unit.source_id,
        "unit_kind": "session",
        "provider_session_id": unit.provider_session_id,
        "derived_session_key": None,
        "identity_confidence": unit.identity_confidence,
        "thread_kind": unit.thread_kind,
        "parent_thread_id": unit.parent_thread_id,
        "forked_from_id": unit.forked_from_id,
        "project": unit.project,
        "title": resolved.title,
        "created": unit.created,
        "raw_source_path": str(unit.source_path.resolve()),
        "raw_source_hash": unit.content_hash,
        "raw_source_locator": unit.locator,
        "source_format": "codex-rollout-jsonl",
        "source_completeness": "raw",
        "event_count_scope": "raw",
        "assistant_final_detection": CODEX_ASSISTANT_FINAL_DETECTION,
        "source_scope": resolved.source_scope,
        "fork_parent_source_id": resolved.fork_parent_source_id,
        "fork_parent_hash": resolved.fork_parent_hash,
        "selected_exchange_count": len(resolved.exchanges),
        "omitted_fork_prefix_exchange_count": resolved.omitted_fork_prefix_exchange_count,
        "parse_status": status,
        "skip_reason": reason or None,
        "visible_user_count": unit.visible_user_count,
        "human_user_count": unit.human_user_count,
        "visible_assistant_final_count": unit.visible_assistant_final_count,
        "assistant_final_count": unit.assistant_final_count,
        "omitted_trivial_exchange_count": unit.omitted_trivial_exchange_count,
        "omitted_runtime_user_count": unit.omitted_runtime_user_count,
        "omitted_developer_count": unit.omitted_developer_count,
        "omitted_commentary_count": unit.omitted_commentary_count,
        "omitted_unphased_assistant_count": unit.omitted_unphased_assistant_count,
        "omitted_tool_call_count": unit.omitted_tool_call_count,
        "omitted_tool_result_count": unit.omitted_tool_result_count,
        "omitted_reasoning_count": unit.omitted_reasoning_count,
        "omitted_compaction_count": unit.omitted_compaction_count,
        "omitted_interrupted_count": unit.omitted_interrupted_count,
        "omitted_unpaired_user_count": unit.omitted_unpaired_user_count,
        "event_user_message_count": unit.event_user_message_count,
        "invalid_jsonl_lines": unit.invalid_line_count,
        "open_turn_count": unit.open_turn_count,
        "ambiguous_user_turn_count": unit.ambiguous_user_turn_count,
        "ambiguous_final_turn_count": unit.ambiguous_final_turn_count,
        "extra_session_meta_count": unit.extra_session_meta_count,
        "parse_notes": list(unit.parse_notes),
    }


def _render_session(resolved: CodexResolvedSession, imported: str) -> tuple[str, int, str]:
    unit = resolved.unit
    title, redactions = redact_secrets(resolved.title)
    rendered: list[str] = []
    for ordinal, exchange in enumerate(resolved.exchanges, start=1):
        user_text, count = redact_secrets(clean_message(exchange.user_text))
        redactions += count
        assistant_text, count = redact_secrets(clean_message(exchange.assistant_text))
        redactions += count
        if not user_text or not assistant_text:
            continue
        rendered.append(
            f"## Exchange {ordinal}\n\n"
            f"- **Turn**: {exchange.turn_index}\n"
            f"- **User locator**: `{exchange.user_locator}`\n"
            f"- **Assistant locator**: `{exchange.assistant_locator}`\n\n"
            f"### Human user\n\n{user_text}\n\n"
            f"### Assistant final\n\n{assistant_text}"
        )
    metadata: dict[str, Any] = {
        "id": unit.source_id,
        "type": "conversation",
        "origin": "codex",
        "source_kind": "session",
        "provider": "codex",
        "thread_kind": "main",
        "provider_session_id": unit.provider_session_id,
        "derived_session_key": None,
        "identity_confidence": unit.identity_confidence,
        "source_format": "codex-rollout-jsonl",
        "source_completeness": "raw",
        "event_count_scope": "raw",
        "assistant_final_detection": CODEX_ASSISTANT_FINAL_DETECTION,
        "source_scope": resolved.source_scope,
        "title": title,
        "created": unit.created,
        "imported": imported,
        "project": unit.project,
        "source_path": str(unit.source_path.resolve()),
        "source_locator": unit.locator,
        "source_hash": f"sha256:{unit.content_hash}",
        "exchange_count": len(rendered),
        "human_user_count": unit.human_user_count,
        "assistant_final_count": unit.assistant_final_count,
        "omitted_fork_prefix_exchange_count": resolved.omitted_fork_prefix_exchange_count,
        "omitted_trivial_exchange_count": unit.omitted_trivial_exchange_count,
        "omitted_runtime_user_count": unit.omitted_runtime_user_count,
        "omitted_developer_count": unit.omitted_developer_count,
        "omitted_commentary_count": unit.omitted_commentary_count,
        "omitted_unphased_assistant_count": unit.omitted_unphased_assistant_count,
        "omitted_tool_call_count": unit.omitted_tool_call_count,
        "omitted_tool_result_count": unit.omitted_tool_result_count,
        "omitted_reasoning_count": unit.omitted_reasoning_count,
        "omitted_compaction_count": unit.omitted_compaction_count,
        "omitted_interrupted_count": unit.omitted_interrupted_count,
        "omitted_unpaired_user_count": unit.omitted_unpaired_user_count,
        "invalid_jsonl_lines": unit.invalid_line_count,
        "redaction_count": redactions,
        "importer_version": CODEX_IMPORTER_VERSION,
    }
    if unit.cwd:
        metadata["cwd"] = unit.cwd
    if unit.cli_version:
        metadata["codex_version"] = unit.cli_version
    if unit.forked_from_id:
        metadata["forked_from_id"] = unit.forked_from_id
        metadata["fork_parent_source_id"] = resolved.fork_parent_source_id
        metadata["fork_parent_hash"] = f"sha256:{resolved.fork_parent_hash}"
    body = (
        f"# {title}\n\n"
        "> 本页从 Codex rollout JSONL 生成，只保留主线程 human user 与显式 `phase=final_answer`。"
        "developer、commentary、tool、reasoning、compaction 和中断事件只计数，不复制正文。\n\n"
        + (
            f"> 该会话从 `{unit.forked_from_id}` fork；已省略与父会话完全相同的"
            f" {resolved.omitted_fork_prefix_exchange_count} 个 exchange，本页只保留分叉后的新内容。\n\n"
            if unit.forked_from_id
            else ""
        )
        + "\n\n".join(rendered)
    )
    return yaml_document(metadata, body), redactions, title


def import_sessions(
    input_dir: Path,
    output_dir: Path,
    manifest_path: Path,
    *,
    limit: int | None = None,
    dry_run: bool = False,
    session_ids: set[str] | None = None,
) -> dict[str, Any]:
    if not input_dir.is_dir():
        raise FileNotFoundError(f"Codex session 目录不存在: {input_dir}")
    manifest = load_manifest(manifest_path)
    stats: dict[str, Any] = {
        "discovered": 0,
        "imported": 0,
        "unchanged": 0,
        "skipped": 0,
        "skip_reasons": {},
        "thread_kinds": {},
        "human_user_messages": 0,
        "assistant_finals": 0,
        "omitted_trivial_exchanges": 0,
        "omitted_fork_prefix_exchanges": 0,
        "omitted_commentary": 0,
        "omitted_tool_calls": 0,
        "omitted_tool_results": 0,
        "omitted_reasoning": 0,
        "omitted_interruptions": 0,
        "omitted_unpaired_users": 0,
        "invalid_lines": 0,
        "redactions": 0,
    }
    requested = set(session_ids or ())
    seen_requested: set[str] = set()
    resolved_units = _dependency_order(iter_resolved_session_units(input_dir))
    resolved_by_source_id = {item.unit.source_id: item for item in resolved_units}
    available_source_ids = {
        item.unit.source_id for item in resolved_units if _manifest_source_is_current(item, manifest)
    }
    for resolved in resolved_units:
        unit = resolved.unit
        if requested and unit.provider_session_id not in requested:
            continue
        seen_requested.add(unit.provider_session_id)
        skip_reason = resolved.skip_reason
        if (
            not skip_reason
            and resolved.fork_parent_source_id
            and resolved.fork_parent_source_id not in available_source_ids
        ):
            skip_reason = "fork_parent_not_standardized_review"
        if not skip_reason and sha256_file(unit.source_path) != unit.content_hash:
            skip_reason = "source_changed_during_import"
        if (
            not skip_reason
            and resolved.fork_parent_source_id
            and resolved.fork_parent_hash
        ):
            parent_result = resolved_by_source_id.get(resolved.fork_parent_source_id)
            parent_source_path = parent_result.unit.source_path if parent_result else None
            if parent_source_path is None or sha256_file(parent_source_path) != resolved.fork_parent_hash:
                skip_reason = "fork_parent_changed_during_import"
        current = manifest["sources"].get(unit.source_id, {})
        is_unchanged = not skip_reason and _manifest_source_is_current(resolved, manifest)
        if not skip_reason and not is_unchanged and limit is not None and stats["imported"] >= limit:
            break

        stats["discovered"] += 1
        thread_kinds = stats["thread_kinds"]
        thread_kinds[unit.thread_kind] = thread_kinds.get(unit.thread_kind, 0) + 1
        stats["human_user_messages"] += unit.human_user_count if unit.thread_kind == "main" else 0
        stats["assistant_finals"] += len(resolved.exchanges) if unit.thread_kind == "main" else 0
        stats["omitted_trivial_exchanges"] += unit.omitted_trivial_exchange_count
        stats["omitted_fork_prefix_exchanges"] += resolved.omitted_fork_prefix_exchange_count
        stats["omitted_commentary"] += unit.omitted_commentary_count
        stats["omitted_tool_calls"] += unit.omitted_tool_call_count
        stats["omitted_tool_results"] += unit.omitted_tool_result_count
        stats["omitted_reasoning"] += unit.omitted_reasoning_count
        stats["omitted_interruptions"] += unit.omitted_interrupted_count
        stats["omitted_unpaired_users"] += unit.omitted_unpaired_user_count
        stats["invalid_lines"] += unit.invalid_line_count

        if skip_reason:
            stats["skipped"] += 1
            reasons = stats["skip_reasons"]
            reasons[skip_reason] = reasons.get(skip_reason, 0) + 1
            if not dry_run and current and skip_reason not in DEFERRED_SKIP_REASONS | REVIEW_SKIP_REASONS:
                old_output = REPO_ROOT / str(current.get("output_path", ""))
                if old_output.is_file() and old_output.parent.resolve() == output_dir.resolve():
                    old_output.unlink()
                manifest["sources"].pop(unit.source_id, None)
            continue
        if is_unchanged:
            stats["unchanged"] += 1
            available_source_ids.add(unit.source_id)
            continue

        document, redactions, title = _render_session(resolved, utc_now()[:10])
        date = unit.created if unit.created != "unknown" else "undated"
        output_path = output_dir / f"{date}-{unit.provider_session_id}.md"
        stats["imported"] += 1
        stats["redactions"] += redactions
        if dry_run:
            available_source_ids.add(unit.source_id)
            continue
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(document, encoding="utf-8")
        manifest["version"] = max(int(manifest.get("version", 1)), 2)
        manifest["sources"][unit.source_id] = {
            "origin": "codex",
            "source_kind": "session",
            "provider": "codex",
            "thread_kind": "main",
            "provider_session_id": unit.provider_session_id,
            "derived_session_key": None,
            "identity_confidence": unit.identity_confidence,
            "assistant_final_detection": CODEX_ASSISTANT_FINAL_DETECTION,
            "source_scope": resolved.source_scope,
            "forked_from_id": unit.forked_from_id,
            "fork_parent_source_id": resolved.fork_parent_source_id,
            "fork_parent_hash": resolved.fork_parent_hash,
            "source_path": str(unit.source_path.resolve()),
            "source_locator": unit.locator,
            "source_hash": unit.content_hash,
            "output_path": relative_to_repo(output_path),
            "ingest_status": "ready",
            "curation_status": current.get("curation_status", "unassessed"),
            "title": title,
            "created": unit.created,
            "omitted_trivial_exchange_count": unit.omitted_trivial_exchange_count,
            "omitted_fork_prefix_exchange_count": resolved.omitted_fork_prefix_exchange_count,
            "redaction_count": redactions,
            "imported_at": utc_now(),
            "importer_version": CODEX_IMPORTER_VERSION,
        }
        available_source_ids.add(unit.source_id)

    missing = requested - seen_requested
    if missing:
        raise ValueError(f"未找到 Codex session ID: {', '.join(sorted(missing))}")
    if not dry_run:
        save_manifest(manifest, manifest_path)
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--session-id", action="append", dest="session_ids")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    stats = import_sessions(
        args.input,
        args.output,
        args.manifest,
        limit=args.limit,
        dry_run=args.dry_run,
        session_ids=set(args.session_ids or ()),
    )
    print("Codex 导入结果:", ", ".join(f"{key}={value}" for key, value in stats.items()))


if __name__ == "__main__":
    main()
