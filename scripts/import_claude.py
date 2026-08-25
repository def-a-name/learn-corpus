#!/usr/bin/env python3
"""把 Claude Markdown 导出白名单化为主会话显式问答来源。"""

from __future__ import annotations

import argparse
import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from wiki_core import (
    CLAUDE_ASSISTANT_FINAL_DETECTION,
    CLAUDE_IMPORTER_VERSION,
    MANIFEST_PATH,
    REPO_ROOT,
    clean_message,
    derive_title,
    format_line_locator,
    load_manifest,
    redact_secrets,
    relative_to_repo,
    save_manifest,
    sha256_file,
    source_needs_redaction,
    utc_now,
    yaml_document,
)


DEFAULT_INPUT = REPO_ROOT.parent / "claude-exec-docs"
DEFAULT_OUTPUT = REPO_ROOT / "sources" / "conversations" / "claude"
EXCLUDED_FILES = {"INDEX.md", "prompt-tmp.md"}
SUBAGENT_MARKER = "> *This is a sub-agent conversation spawned by the main session.*"
SESSION_START = re.compile(
    rf"^(?:(?P<subagent>{re.escape(SUBAGENT_MARKER)})\n\n)?# Session:\s*(?P<title>.+)$",
    re.MULTILINE,
)
MESSAGE_HEADING = re.compile(
    r"^## (?:(?:👤 )?User(?: \(Turn (?P<turn>\d+)\))?|(?:🤖 )?Assistant)\s*$",
    re.MULTILINE,
)
USER_HEADING = re.compile(r"^## (?:👤 )?User(?: \(Turn (?P<turn>\d+)\))?\s*$")
TIMESTAMP_LINE = re.compile(r"^\*[^\n]*UTC\*\s*\n?", re.IGNORECASE)
THINKING_BLOCK = re.compile(
    r"<details><summary>💭 Thinking</summary>.*?</details>",
    re.DOTALL | re.IGNORECASE,
)
TOOL_MARKER = re.compile(r"^> \*\*Tool:\s*[^\n]+", re.MULTILINE)
COMPACTION_PREFIX = "This session is being continued from a previous conversation that ran out of context."
INTERRUPTION = re.compile(r"^\[Request interrupted by user(?: for tool use)?\]$", re.IGNORECASE)
RUNTIME_TAG = re.compile(
    r"<(?:local-command-[^>]+|command-(?:message|name|args)|task-notification)(?:>|\s)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ClaudeExchange:
    turn_index: int
    user_text: str
    assistant_text: str
    user_locator: str
    assistant_locator: str


@dataclass(frozen=True)
class SessionParse:
    exchanges: tuple[ClaudeExchange, ...]
    visible_user_block_count: int
    human_user_count: int
    assistant_final_count: int
    omitted_process_count: int
    omitted_tool_call_count: int
    omitted_tool_result_count: int | None
    omitted_instruction_count: int
    omitted_reasoning_count: int
    omitted_compaction_count: int
    omitted_interrupted_count: int
    omitted_unpaired_user_count: int
    assistant_block_count: int
    parse_notes: tuple[str, ...]


@dataclass(frozen=True)
class ExportUnit:
    source_id: str
    kind: str
    title: str
    created: str
    project: str
    source_path: Path
    locator: str
    content: str
    content_hash: str
    start_line: int = 1
    end_line: int = 1
    cwd: str = ""
    claude_version: str = ""
    user_turns: int | None = None
    thread_kind: str = "unknown"
    document_kind: str = ""
    derived_session_key: str = ""
    prefilter_skip_reason: str = ""
    session_parse: SessionParse | None = None


@dataclass
class _PendingExchange:
    turn_index: int
    user_text: str
    user_locator: str
    assistant_blocks: list[tuple[int, str, bool, int, int]]
    interrupted: bool = False


def _field(section: str, name: str) -> str:
    match = re.search(fr"^- \*\*{re.escape(name)}\*\*:\s*(.+)$", section, flags=re.MULTILINE)
    return match.group(1).strip().strip("`") if match else ""


def _stable_id(relative_path: str, kind: str, identity: str, created: str, cwd: str) -> str:
    value = "\0".join((relative_path, kind, identity, created, cwd))
    return "claude-export-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def _prefilter_skip_reason(raw_title: str, section: str) -> str:
    normalized = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", raw_title.lower())
    if normalized in {"hello", "hi", "你好", "test", "测试"}:
        return "trivial_session"
    if normalized.startswith(("localcommandcaveat", "commandmessage", "commandname")):
        return "runtime_only_session"
    if "does not have access to claude code" in section.lower() and section.count("## 🤖 Assistant") <= 1:
        return "unavailable_assistant"
    has_user = bool(re.search(r"^## (?:👤 )?User", section, flags=re.MULTILINE))
    has_assistant = bool(re.search(r"^## (?:🤖 )?Assistant", section, flags=re.MULTILINE))
    return "" if has_user and has_assistant else "incomplete_session"


def _document_kind(relative_path: str, text: str) -> str:
    if relative_path == "01-prompt-history.md":
        return "prompt-history"
    if relative_path.startswith("my-notes/"):
        return "note"
    if relative_path in {"00-global.md", "02-skills.md"}:
        return "instruction"
    if re.match(r"^# Project:", text) and "## Project Memory:" in text:
        return "instruction"
    return "review"


def _strip_timestamp(text: str) -> str:
    return TIMESTAMP_LINE.sub("", text.lstrip(), count=1).strip()


def _classify_user_block(raw: str) -> tuple[str, str]:
    text = _strip_timestamp(raw)
    if INTERRUPTION.fullmatch(text):
        return "interruption", ""
    if text.startswith(COMPACTION_PREFIX):
        return "compaction", ""
    if re.search(r"<task-notification(?:>|\s)", text, flags=re.IGNORECASE):
        return "notification", ""
    if RUNTIME_TAG.search(text):
        return "runtime", ""
    cleaned = clean_message(text)
    if not cleaned:
        return "runtime", ""
    if cleaned.startswith(
        (
            "Base directory for this skill:",
            "**Note**: Placeholders like `{RUNTIME_PATH}`",
            "Unknown skill:",
        )
    ):
        return "runtime", ""
    if re.fullmatch(r"/[A-Za-z][\w-]*(?:\s+[^\n]*)?", cleaned):
        return "runtime", ""
    return "human", cleaned


def _strip_tool_quotes(text: str) -> str:
    lines = text.splitlines()
    kept: list[str] = []
    dropping_tool_quote = False
    for line in lines:
        if re.match(r"^> \*\*Tool:", line):
            dropping_tool_quote = True
            continue
        if dropping_tool_quote and (line.startswith(">") or not line.strip()):
            continue
        dropping_tool_quote = False
        kept.append(line)
    return "\n".join(kept)


def _parse_assistant_block(raw: str) -> tuple[str, int, int, int]:
    reasoning_count = len(THINKING_BLOCK.findall(raw))
    without_reasoning = THINKING_BLOCK.sub("", raw)
    tool_count = len(TOOL_MARKER.findall(without_reasoning))
    without_tools = _strip_tool_quotes(without_reasoning)
    tool_result_count = 0
    if re.search(r"^> \*\*⚠ Error:\*\*", without_tools, flags=re.MULTILINE):
        tool_result_count = 1
        without_tools = re.sub(r"^>.*(?:\n|$)", "", without_tools, flags=re.MULTILINE)
    return clean_message(without_tools), tool_count, tool_result_count, reasoning_count


def parse_session(section: str, locator: str, start_line: int = 1) -> SessionParse:
    matches = list(MESSAGE_HEADING.finditer(section))
    exchanges: list[ClaudeExchange] = []
    pending: _PendingExchange | None = None
    visible_users = 0
    human_users = 0
    assistant_blocks = 0
    omitted_process = 0
    omitted_tools = 0
    visible_tool_results = 0
    omitted_instructions = 0
    omitted_reasoning = 0
    omitted_compactions = 0
    omitted_interruptions = 0
    omitted_unpaired = 0
    notes: set[str] = set()

    def finish_pending() -> None:
        nonlocal pending, omitted_process, omitted_unpaired
        if pending is None:
            return
        selected: tuple[int, str, bool, int, int] | None = None
        last_tool_block = max(
            (index for index, _, has_tool, _, _ in pending.assistant_blocks if has_tool),
            default=-1,
        )
        if not pending.interrupted:
            candidates = [
                item for item in pending.assistant_blocks if item[1] and not item[2] and item[0] > last_tool_block
            ]
            if candidates:
                selected = candidates[-1]
        for item in pending.assistant_blocks:
            if item[1] and item is not selected:
                omitted_process += 1
        if selected is None:
            omitted_unpaired += 1
        else:
            assistant_index, assistant_text, _, assistant_start, assistant_end = selected
            exchanges.append(
                ClaudeExchange(
                    turn_index=pending.turn_index,
                    user_text=pending.user_text,
                    assistant_text=assistant_text,
                    user_locator=pending.user_locator,
                    assistant_locator=format_line_locator(
                        f"{locator}/Assistant:{assistant_index}", assistant_start, assistant_end
                    ),
                )
            )
        pending = None

    for index, match in enumerate(matches, start=1):
        end = matches[index].start() if index < len(matches) else len(section)
        block_start_line = start_line + section.count("\n", 0, match.start())
        block_end_line = start_line + section.count("\n", 0, end) - (1 if end < len(section) else 0)
        raw = section[match.end() : end].strip()
        user_match = USER_HEADING.fullmatch(match.group(0))
        if user_match:
            visible_users += 1
            turn_text = user_match.group("turn")
            turn_index = int(turn_text) if turn_text else visible_users
            classification, text = _classify_user_block(raw)
            if classification == "human":
                finish_pending()
                human_users += 1
                pending = _PendingExchange(
                    turn_index=turn_index,
                    user_text=text,
                    user_locator=format_line_locator(
                        f"{locator}/Turn:{turn_index}/User", block_start_line, block_end_line
                    ),
                    assistant_blocks=[],
                )
            elif classification == "interruption":
                omitted_interruptions += 1
                if pending is not None:
                    pending.interrupted = True
            elif classification == "compaction":
                omitted_compactions += 1
            elif classification == "notification":
                omitted_instructions += 1
            else:
                finish_pending()
                omitted_instructions += 1
            continue

        assistant_blocks += 1
        text, tool_count, tool_result_count, reasoning_count = _parse_assistant_block(raw)
        omitted_tools += tool_count
        visible_tool_results += tool_result_count
        omitted_reasoning += reasoning_count
        if pending is None:
            if text:
                omitted_process += 1
            continue
        pending.assistant_blocks.append(
            (assistant_blocks, text, tool_count > 0, block_start_line, block_end_line)
        )

    finish_pending()
    if omitted_unpaired:
        notes.add("存在没有可确认 assistant final 的 human user turn；该 turn 未写入标准化正文。")
    if visible_tool_results:
        notes.add("只计数导出中仍可见的 tool result；上游已省略的结果无法恢复。")
    notes.add("provider session ID 与上游已省略事件不可从 Markdown 导出恢复。")
    return SessionParse(
        exchanges=tuple(exchanges),
        visible_user_block_count=visible_users,
        human_user_count=human_users,
        assistant_final_count=len(exchanges),
        omitted_process_count=omitted_process,
        omitted_tool_call_count=omitted_tools,
        omitted_tool_result_count=visible_tool_results if visible_tool_results else None,
        omitted_instruction_count=omitted_instructions,
        omitted_reasoning_count=omitted_reasoning,
        omitted_compaction_count=omitted_compactions,
        omitted_interrupted_count=omitted_interruptions,
        omitted_unpaired_user_count=omitted_unpaired,
        assistant_block_count=assistant_blocks,
        parse_notes=tuple(sorted(notes)),
    )


def iter_export_units(input_dir: Path, kind: str = "all") -> Iterable[ExportUnit]:
    for path in sorted(input_dir.rglob("*.md")):
        if path.name in EXCLUDED_FILES or any(part.startswith(".") for part in path.relative_to(input_dir).parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            continue
        relative_path = path.relative_to(input_dir).as_posix()
        matches = list(SESSION_START.finditer(text))

        if matches and kind in {"all", "session"}:
            project_match = re.search(r"^# Project:\s*(.+)$", text[: matches[0].start()], flags=re.MULTILINE)
            project = project_match.group(1).strip() if project_match else path.stem
            occurrences: Counter[str] = Counter()
            for index, match in enumerate(matches, start=1):
                raw_title = match.group("title").strip()
                end = matches[index].start() if index < len(matches) else len(text)
                heading_start = text.find("# Session:", match.start(), match.end())
                section = text[heading_start:end].strip()
                section = re.sub(r"\n\n---\s*$", "", section).strip()
                section_start_line = text.count("\n", 0, heading_start) + 1
                section_end_line = section_start_line + section.count("\n")
                created = _field(section, "Date")[:10] or "unknown"
                cwd = _field(section, "Working Directory")
                claude_version = _field(section, "Claude Code Version")
                turns_text = _field(section, "User Turns")
                user_turns = int(turns_text) if turns_text.isdigit() else None
                occurrences[raw_title] += 1
                identity = raw_title if occurrences[raw_title] == 1 else f"{raw_title}#{occurrences[raw_title]}"
                source_id = _stable_id(relative_path, "session", identity, created, cwd)
                semantic_locator = f"{relative_path}#Session:{index}"
                locator = format_line_locator(semantic_locator, section_start_line, section_end_line)
                parsed = parse_session(section, semantic_locator, section_start_line)
                title_messages = [
                    {"role": "user", "text": exchange.user_text} for exchange in parsed.exchanges
                ] or [{"role": "user", "text": raw_title}]
                title = derive_title("claude", title_messages)
                yield ExportUnit(
                    source_id=source_id,
                    kind="session",
                    title=title,
                    created=created,
                    project=project,
                    source_path=path,
                    locator=locator,
                    content=section,
                    content_hash=hashlib.sha256(section.encode("utf-8")).hexdigest(),
                    start_line=section_start_line,
                    end_line=section_end_line,
                    cwd=cwd,
                    claude_version=claude_version,
                    user_turns=user_turns,
                    thread_kind="subagent" if match.group("subagent") else "main",
                    derived_session_key=source_id.removeprefix("claude-export-"),
                    prefilter_skip_reason=_prefilter_skip_reason(raw_title, section),
                    session_parse=parsed,
                )

        if not matches and kind in {"all", "document"}:
            heading = re.search(r"^#\s+(.+)$", text, flags=re.MULTILINE)
            title = heading.group(1).strip() if heading else path.stem
            document_kind = _document_kind(relative_path, text)
            source_id = _stable_id(relative_path, "document", title, "unknown", "")
            document = text.strip()
            document_offset = text.find(document)
            document_start_line = text.count("\n", 0, document_offset) + 1
            document_end_line = document_start_line + document.count("\n")
            yield ExportUnit(
                source_id=source_id,
                kind="document",
                title=title,
                created="unknown",
                project=path.parent.name if path.parent != input_dir else path.stem,
                source_path=path,
                locator=format_line_locator(relative_path, document_start_line, document_end_line),
                content=document,
                content_hash=sha256_file(path),
                start_line=document_start_line,
                end_line=document_end_line,
                document_kind=document_kind,
            )


def unit_skip_reason(unit: ExportUnit) -> str:
    if unit.kind == "document":
        if unit.document_kind == "instruction":
            return "document_instruction_excluded"
        if unit.document_kind == "prompt-history":
            return "prompt_history_deferred"
        if unit.document_kind == "review":
            return "document_classification_review"
        return ""
    if unit.thread_kind == "subagent":
        return "subagent_excluded"
    parsed = unit.session_parse
    if parsed is None or not parsed.human_user_count:
        return "runtime_only_session" if unit.prefilter_skip_reason == "runtime_only_session" else "no_human_user"
    if not parsed.exchanges:
        return "no_confirmed_exchange"
    trivial = {"hi", "hello", "hey", "你好", "您好", "test", "测试"}
    normalized_users = {
        re.sub(r"[\s.!！?？,，]+", "", exchange.user_text).lower() for exchange in parsed.exchanges
    }
    if normalized_users and normalized_users <= trivial:
        return "trivial_session"
    return ""


def unit_inventory_record(unit: ExportUnit, parse_status: str | None = None, skip_reason: str | None = None) -> dict[str, Any]:
    reason = unit_skip_reason(unit) if skip_reason is None else skip_reason
    status = parse_status or ("excluded" if reason else "ready")
    record: dict[str, Any] = {
        "source_id": unit.source_id,
        "unit_kind": unit.kind,
        "document_kind": unit.document_kind or None,
        "thread_kind": unit.thread_kind if unit.kind == "session" else None,
        "provider_session_id": None,
        "derived_session_key": unit.derived_session_key or None,
        "identity_confidence": "derived" if unit.kind == "session" else "path-derived",
        "project": unit.project,
        "title": unit.title,
        "created": unit.created,
        "raw_source_path": str(unit.source_path.resolve()),
        "raw_source_hash": f"sha256:{unit.content_hash}",
        "raw_source_locator": unit.locator,
        "raw_source_start_line": unit.start_line,
        "raw_source_end_line": unit.end_line,
        "source_completeness": "summary" if unit.kind == "session" else "full",
        "event_count_scope": "export-visible" if unit.kind == "session" else "document",
        "assistant_final_detection": (
            CLAUDE_ASSISTANT_FINAL_DETECTION
            if unit.kind == "session" and unit.thread_kind == "main"
            else None
        ),
        "parse_status": status,
        "skip_reason": reason or None,
        "legacy_prefilter_reason": unit.prefilter_skip_reason or None,
    }
    if unit.session_parse:
        parsed = unit.session_parse
        record.update(
            {
                "visible_user_block_count": parsed.visible_user_block_count,
                "human_user_count": parsed.human_user_count if unit.thread_kind == "main" else 0,
                "assistant_block_count": parsed.assistant_block_count,
                "assistant_final_count": parsed.assistant_final_count if unit.thread_kind == "main" else 0,
                "omitted_process_count": parsed.omitted_process_count,
                "omitted_agent_count": (
                    parsed.visible_user_block_count + parsed.assistant_block_count
                    if unit.thread_kind == "subagent"
                    else 0
                ),
                "omitted_tool_call_count": parsed.omitted_tool_call_count,
                "omitted_tool_result_count": parsed.omitted_tool_result_count,
                "omitted_instruction_count": parsed.omitted_instruction_count,
                "omitted_reasoning_count": parsed.omitted_reasoning_count,
                "omitted_compaction_count": parsed.omitted_compaction_count,
                "omitted_interrupted_count": parsed.omitted_interrupted_count,
                "omitted_unpaired_user_count": parsed.omitted_unpaired_user_count,
                "parse_notes": list(parsed.parse_notes),
            }
        )
    return record


def _render_session(unit: ExportUnit, imported: str) -> tuple[str, int, str]:
    if unit.session_parse is None:
        raise ValueError(f"Claude session 缺少解析结果: {unit.locator}")
    title, redactions = redact_secrets(unit.title)
    rendered: list[str] = []
    for ordinal, exchange in enumerate(unit.session_parse.exchanges, start=1):
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
    parsed = unit.session_parse
    metadata = {
        "id": unit.source_id,
        "type": "conversation",
        "origin": "claude-export",
        "source_kind": "session",
        "provider": "claude",
        "thread_kind": "main",
        "provider_session_id": None,
        "derived_session_key": unit.derived_session_key,
        "identity_confidence": "derived",
        "source_format": "claude-markdown-export",
        "source_completeness": "summary",
        "event_count_scope": "export-visible",
        "assistant_final_detection": CLAUDE_ASSISTANT_FINAL_DETECTION,
        "title": title,
        "created": unit.created,
        "imported": imported,
        "project": unit.project,
        "source_path": str(unit.source_path.resolve()),
        "source_locator": unit.locator,
        "source_hash": f"sha256:{unit.content_hash}",
        "exchange_count": len(rendered),
        "human_user_count": parsed.human_user_count,
        "assistant_final_count": parsed.assistant_final_count,
        "omitted_process_count": parsed.omitted_process_count,
        "omitted_tool_call_count": parsed.omitted_tool_call_count,
        "omitted_tool_result_count": parsed.omitted_tool_result_count,
        "omitted_instruction_count": parsed.omitted_instruction_count,
        "omitted_reasoning_count": parsed.omitted_reasoning_count,
        "omitted_compaction_count": parsed.omitted_compaction_count,
        "omitted_interrupted_count": parsed.omitted_interrupted_count,
        "omitted_unpaired_user_count": parsed.omitted_unpaired_user_count,
        "redaction_count": redactions,
        "importer_version": CLAUDE_IMPORTER_VERSION,
    }
    if unit.cwd:
        metadata["cwd"] = unit.cwd
    if unit.claude_version:
        metadata["claude_version"] = unit.claude_version
    body = (
        f"# {title}\n\n"
        "> 本页从有损 Claude Markdown 导出生成，只保留主会话 human user 与结构启发式确认的 assistant final。"
        "完整 tool/system/progress 事件已无法从现有输入恢复。\n\n"
        + "\n\n".join(rendered)
    )
    return yaml_document(metadata, body), redactions, title


def _remove_legacy_sources(manifest: dict, output_dir: Path, dry_run: bool) -> int:
    legacy_ids = [source_id for source_id, item in manifest["sources"].items() if item.get("origin") == "claude"]
    if dry_run:
        return len(legacy_ids)
    for source_id in legacy_ids:
        item = manifest["sources"].pop(source_id)
        output_path = Path(REPO_ROOT / item.get("output_path", ""))
        if output_path.is_file() and output_path.parent.resolve() == output_dir.resolve():
            output_path.unlink()
    return len(legacy_ids)


def import_exports(
    input_dir: Path,
    output_dir: Path,
    manifest_path: Path,
    *,
    kind: str = "session",
    limit: int | None = None,
    dry_run: bool = False,
    replace_legacy: bool = False,
) -> dict[str, Any]:
    if not input_dir.is_dir():
        raise FileNotFoundError(f"Claude 导出目录不存在: {input_dir}")

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
        "omitted_process": 0,
        "omitted_agent_messages": 0,
        "omitted_tool_calls": 0,
        "omitted_instructions": 0,
        "omitted_reasoning": 0,
        "omitted_compactions": 0,
        "omitted_interruptions": 0,
        "omitted_unpaired_users": 0,
        "legacy_removed": 0,
        "redactions": 0,
    }
    if replace_legacy:
        stats["legacy_removed"] = _remove_legacy_sources(manifest, output_dir, dry_run)

    for unit in iter_export_units(input_dir, kind):
        skip_reason = unit_skip_reason(unit)
        if unit.kind == "document" and not skip_reason:
            skip_reason = "document_note_deferred"
        current = manifest["sources"].get(unit.source_id, {})
        current_output = str(current.get("output_path") or "")
        current_output_path = Path(REPO_ROOT / current_output) if current_output else Path()
        is_unchanged = bool(
            not skip_reason
            and current.get("source_hash") == unit.content_hash
            and current.get("importer_version") == CLAUDE_IMPORTER_VERSION
            and current_output_path.is_file()
            and not source_needs_redaction(current_output_path)
        )
        if not skip_reason and not is_unchanged and limit is not None and stats["imported"] >= limit:
            break

        stats["discovered"] += 1
        if unit.kind == "session" and unit.session_parse:
            thread_kinds = stats["thread_kinds"]
            thread_kinds[unit.thread_kind] = thread_kinds.get(unit.thread_kind, 0) + 1
            parsed = unit.session_parse
            if unit.thread_kind == "main":
                stats["human_user_messages"] += parsed.human_user_count
                stats["assistant_finals"] += parsed.assistant_final_count
                stats["omitted_process"] += parsed.omitted_process_count
                stats["omitted_tool_calls"] += parsed.omitted_tool_call_count
                stats["omitted_instructions"] += parsed.omitted_instruction_count
                stats["omitted_reasoning"] += parsed.omitted_reasoning_count
                stats["omitted_compactions"] += parsed.omitted_compaction_count
                stats["omitted_interruptions"] += parsed.omitted_interrupted_count
                stats["omitted_unpaired_users"] += parsed.omitted_unpaired_user_count
            else:
                stats["omitted_agent_messages"] += parsed.visible_user_block_count + parsed.assistant_block_count

        if skip_reason:
            stats["skipped"] += 1
            reasons = stats["skip_reasons"]
            reasons[skip_reason] = reasons.get(skip_reason, 0) + 1
            if not dry_run and current:
                old_output = Path(REPO_ROOT / str(current.get("output_path", "")))
                if old_output.is_file() and old_output.parent.resolve() == output_dir.resolve():
                    old_output.unlink()
                manifest["sources"].pop(unit.source_id, None)
            continue
        if is_unchanged:
            stats["unchanged"] += 1
            continue

        if unit.kind != "session":
            raise AssertionError(f"未结算的 Claude document: {unit.locator}")
        document, redactions, title = _render_session(unit, utc_now()[:10])
        date = unit.created if unit.created != "unknown" else "undated"
        output_path = output_dir / f"{date}-{unit.derived_session_key}.md"
        stats["imported"] += 1
        stats["redactions"] += redactions
        if dry_run:
            continue
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(document, encoding="utf-8")
        manifest["version"] = max(int(manifest.get("version", 1)), 2)
        manifest["sources"][unit.source_id] = {
            "origin": "claude-export",
            "source_kind": "session",
            "provider": "claude",
            "thread_kind": "main",
            "provider_session_id": None,
            "derived_session_key": unit.derived_session_key,
            "identity_confidence": "derived",
            "assistant_final_detection": CLAUDE_ASSISTANT_FINAL_DETECTION,
            "source_path": str(unit.source_path.resolve()),
            "source_locator": unit.locator,
            "source_hash": unit.content_hash,
            "output_path": relative_to_repo(output_path),
            "ingest_status": "ready",
            "curation_status": current.get("curation_status", "unassessed"),
            "title": title,
            "created": unit.created,
            "redaction_count": redactions,
            "imported_at": utc_now(),
            "importer_version": CLAUDE_IMPORTER_VERSION,
        }

    if not dry_run:
        save_manifest(manifest, manifest_path)
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--kind", choices=("all", "session", "document"), default="session")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--replace-legacy", action="store_true", help="移除旧 ~/.claude 导入器生成的来源")
    args = parser.parse_args()
    stats = import_exports(
        args.input,
        args.output,
        args.manifest,
        kind=args.kind,
        limit=args.limit,
        dry_run=args.dry_run,
        replace_legacy=args.replace_legacy,
    )
    print("Claude 导出导入结果:", ", ".join(f"{key}={value}" for key, value in stats.items()))


if __name__ == "__main__":
    main()
