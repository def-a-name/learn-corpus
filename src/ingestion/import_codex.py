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

from src.corpus.document import format_line_locator, yaml_document
from src.corpus.manifest import utc_now
from src.corpus.paths import MANIFEST_PATH, REPO_ROOT, relative_to_repo
from src.corpus.storage import sha256_file
from src.ingestion.batch import InputSnapshot, PlannedFile, PlannedSource, ProviderPlan, execute_batch
from src.ingestion.common import (
    CODEX_ASSISTANT_FINAL_DETECTION,
    CODEX_IMPORTER_VERSION,
    clean_message,
    derive_title,
    extract_text,
    is_exact_meaningless_exchange,
    is_noise_message,
    redact_secrets,
    source_needs_redaction,
)


DEFAULT_INPUT = Path.home() / ".codex" / "sessions"
DEFAULT_OUTPUT = REPO_ROOT / "sources" / "conversations" / "codex"
DEFAULT_REVIEW_RESOLUTIONS = REPO_ROOT / "meta" / "source-review-resolutions.json"
RUNTIME_USER_TAG = re.compile(r"<(?:model_instruction|user_action)(?:>|\s)", re.IGNORECASE)
AGENTS_RUNTIME_USER_ENVELOPE = re.compile(
    r"\A# AGENTS\.md instructions for [^\r\n]+\r?\n\r?\n"
    r"<INSTRUCTIONS>.*?</INSTRUCTIONS>"
    r"(?:\s*<environment_context>.*?</environment_context>)?\s*\Z",
    re.DOTALL,
)
SKILL_RUNTIME_USER_ENVELOPE = re.compile(
    r"\A<skill>\s*"
    r"<name>[^<\r\n]+</name>\s*"
    r"<path>[^<\r\n]+</path>\s*"
    r"---(?:\r?\n).*?</skill>\s*\Z",
    re.DOTALL,
)
QUESTION_REPLY_USER_ENVELOPE = re.compile(
    r"\A<send_user_message_question_reply>\s*(.*?)\s*"
    r"</send_user_message_question_reply>\s*\Z",
    re.DOTALL,
)
REVIEW_SKIP_REASONS = {
    "fork_cycle_review",
    "fork_parent_missing_review",
    "fork_parent_not_standardized_review",
    "fork_parent_unusable_review",
    "fork_prefix_mismatch_review",
    "invalid_jsonl_review",
    "review_resolution_mismatch_review",
    "thread_kind_review",
    "turn_boundary_review",
}

REVIEW_DECISION_OMIT_CORRUPT_TURNS = "omit_invalid_jsonl_and_superseded_turns"
DEFERRED_SKIP_REASONS = {
    "active_session_deferred",
    "fork_parent_deferred",
    "fork_parent_changed_during_import",
    "source_changed_during_import",
}


@dataclass(frozen=True)
class CodexExchange:
    turn_index: int
    user_messages: tuple[str, ...]
    assistant_text: str
    user_locator: str
    user_message_locators: tuple[str, ...]
    assistant_locator: str

    @property
    def user_text(self) -> str:
        """按原始顺序把同一 task 的用户消息渲染为一个 human item。"""

        if len(self.user_messages) == 1:
            return self.user_messages[0]
        return "\n\n".join(
            f"#### User message {index}\n\n{text}"
            for index, text in enumerate(self.user_messages, start=1)
        )


@dataclass(frozen=True)
class InvalidJsonlLine:
    line_number: int
    content_hash: str
    nul_only: bool


@dataclass(frozen=True)
class SupersededIncompleteTurn:
    start_line: int
    next_start_line: int


@dataclass(frozen=True)
class CodexReviewResolution:
    provider_session_id: str
    decision: str
    reviewed_at: str
    reviewed_by: str
    invalid_lines: tuple[InvalidJsonlLine, ...]
    superseded_turns: tuple[SupersededIncompleteTurn, ...]

    @property
    def content_hash(self) -> str:
        payload = {
            "provider_session_id": self.provider_session_id,
            "decision": self.decision,
            "reviewed_at": self.reviewed_at,
            "reviewed_by": self.reviewed_by,
            "invalid_lines": [
                {
                    "line": item.line_number,
                    "sha256": item.content_hash,
                    "nul_only": item.nul_only,
                }
                for item in self.invalid_lines
            ],
            "superseded_turns": [
                {
                    "start_line": item.start_line,
                    "next_start_line": item.next_start_line,
                }
                for item in self.superseded_turns
            ],
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


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
    invalid_lines: tuple[InvalidJsonlLine, ...]
    active_open_turn_count: int
    superseded_incomplete_turns: tuple[SupersededIncompleteTurn, ...]
    multi_user_exchange_count: int
    ambiguous_user_turn_count: int
    ambiguous_final_turn_count: int
    extra_session_meta_count: int
    parse_notes: tuple[str, ...]

    @property
    def invalid_line_count(self) -> int:
        return len(self.invalid_lines)

    @property
    def open_turn_count(self) -> int:
        """兼容旧调用；只表示文件末尾仍未结束、可能仍在写入的 turn。"""
        return self.active_open_turn_count


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
    review_resolution: CodexReviewResolution | None = None


@dataclass
class _Turn:
    ordinal: int
    start_line: int
    human_users: list[tuple[int, int, str]] = field(default_factory=list)
    human_user_message_count: int = 0
    assistant_finals: list[tuple[int, str]] = field(default_factory=list)
    turn_context_seen: bool = False


def _normalize_question_reply(raw: str) -> str | None:
    """把 Codex 结构化提问回复信封还原为可读的用户内容。"""

    match = QUESTION_REPLY_USER_ENVELOPE.fullmatch(raw)
    if not match:
        return None
    try:
        replies = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(replies, list) or not replies:
        return None

    rendered: list[str] = []
    for reply in replies:
        if not isinstance(reply, dict):
            return None
        question = reply.get("question")
        answer = reply.get("answer")
        if not isinstance(question, str) or not isinstance(answer, str):
            return None
        question = clean_message(question)
        answer = clean_message(answer)
        if not question or not answer:
            return None
        rendered.append(f"助手问题：{question}\n用户回答：{answer}")
    return "\n\n".join(rendered)


def _classify_user_text(
    raw: str,
    *,
    before_turn_context: bool = False,
) -> tuple[str, str]:
    if before_turn_context and AGENTS_RUNTIME_USER_ENVELOPE.fullmatch(raw):
        return "runtime", ""
    if SKILL_RUNTIME_USER_ENVELOPE.fullmatch(raw):
        return "runtime", ""
    if is_noise_message(raw) or RUNTIME_USER_TAG.search(raw):
        return "runtime", ""
    question_reply = _normalize_question_reply(raw)
    if question_reply is not None:
        return "structured_reply", question_reply
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


def load_review_resolutions(
    path: Path = DEFAULT_REVIEW_RESOLUTIONS,
) -> dict[str, CodexReviewResolution]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != 1 or not isinstance(payload.get("codex_sessions"), dict):
        raise ValueError(f"invalid Codex review resolution format: {path}")
    resolutions: dict[str, CodexReviewResolution] = {}
    for provider_session_id, raw_resolution in payload["codex_sessions"].items():
        if not isinstance(raw_resolution, dict):
            raise ValueError(f"invalid Codex review resolution entry: {provider_session_id}")
        decision = str(raw_resolution.get("decision") or "")
        reviewed_at = str(raw_resolution.get("reviewed_at") or "")
        reviewed_by = str(raw_resolution.get("reviewed_by") or "")
        raw_invalid_lines = raw_resolution.get("invalid_jsonl_lines")
        raw_superseded_turns = raw_resolution.get("superseded_incomplete_turns")
        if (
            decision != REVIEW_DECISION_OMIT_CORRUPT_TURNS
            or not reviewed_at
            or not reviewed_by
            or not isinstance(raw_invalid_lines, list)
            or not raw_invalid_lines
            or not isinstance(raw_superseded_turns, list)
            or not raw_superseded_turns
        ):
            raise ValueError(f"invalid Codex review resolution fields: {provider_session_id}")
        if any(
            not isinstance(item, dict) or not isinstance(item.get("nul_only"), bool)
            for item in raw_invalid_lines
        ):
            raise ValueError(f"invalid Codex review resolution line fields: {provider_session_id}")
        try:
            invalid_lines = tuple(
                InvalidJsonlLine(
                    line_number=int(item["line"]),
                    content_hash=str(item["sha256"]),
                    nul_only=item["nul_only"],
                )
                for item in raw_invalid_lines
            )
            superseded_turns = tuple(
                SupersededIncompleteTurn(
                    start_line=int(item["start_line"]),
                    next_start_line=int(item["next_start_line"]),
                )
                for item in raw_superseded_turns
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"invalid Codex review resolution locator fields: {provider_session_id}"
            ) from exc
        if any(
            item.line_number < 1 or not re.fullmatch(r"[0-9a-f]{64}", item.content_hash)
            for item in invalid_lines
        ) or any(
            item.start_line < 1 or item.next_start_line <= item.start_line
            for item in superseded_turns
        ):
            raise ValueError(f"invalid Codex review resolution locator values: {provider_session_id}")
        resolutions[str(provider_session_id)] = CodexReviewResolution(
            provider_session_id=str(provider_session_id),
            decision=decision,
            reviewed_at=reviewed_at,
            reviewed_by=reviewed_by,
            invalid_lines=invalid_lines,
            superseded_turns=superseded_turns,
        )
    return resolutions


def _review_resolution_matches(
    unit: CodexSession,
    resolution: CodexReviewResolution,
) -> bool:
    return (
        resolution.provider_session_id == unit.provider_session_id
        and resolution.decision == REVIEW_DECISION_OMIT_CORRUPT_TURNS
        and resolution.invalid_lines == unit.invalid_lines
        and resolution.superseded_turns == unit.superseded_incomplete_turns
        and all(item.nul_only for item in unit.invalid_lines)
    )


def parse_session(path: Path, input_root: Path | None = None) -> CodexSession:
    raw = path.read_bytes()
    content_hash = hashlib.sha256(raw).hexdigest()
    raw_lines = raw.splitlines()
    lines = [line.decode("utf-8", errors="replace") for line in raw_lines]
    records: list[tuple[int, dict[str, Any]]] = []
    invalid_lines: list[InvalidJsonlLine] = []
    for line_number, (raw_line, line) in enumerate(zip(raw_lines, lines), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            invalid_lines.append(
                InvalidJsonlLine(
                    line_number=line_number,
                    content_hash=hashlib.sha256(raw_line).hexdigest(),
                    nul_only=bool(raw_line) and raw_line.strip(b"\x00") == b"",
                )
            )
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
    active_open_turns = 0
    superseded_incomplete_turns: list[SupersededIncompleteTurn] = []
    ambiguous_users = 0
    ambiguous_finals = 0
    orphan_finals = 0
    multi_user_exchanges = 0
    notes: set[str] = set()

    def finish_turn(terminal: str, next_start_line: int | None = None) -> None:
        nonlocal current, active_open_turns, interruptions, unpaired_users
        nonlocal ambiguous_users, ambiguous_finals, orphan_finals, multi_user_exchanges
        if current is None:
            return
        if terminal == "active":
            active_open_turns += 1
        elif terminal == "superseded":
            if next_start_line is None:
                raise ValueError("superseded turn has no following task start line")
            superseded_incomplete_turns.append(
                SupersededIncompleteTurn(
                    start_line=current.start_line,
                    next_start_line=next_start_line,
                )
            )
        elif terminal == "aborted":
            interruptions += 1
        assistant_line = (
            current.assistant_finals[0][0]
            if len(current.assistant_finals) == 1
            else None
        )
        pairable = bool(
            terminal == "complete"
            and current.human_users
            and assistant_line is not None
            and all(end_line < assistant_line for _, end_line, _ in current.human_users)
        )
        if (
            current.human_users
            and assistant_line is not None
            and not pairable
        ) or (current.human_user_message_count > 1 and not pairable):
            ambiguous_users += 1
        if len(current.assistant_finals) > 1:
            ambiguous_finals += 1
        if pairable:
            if current.human_user_message_count > 1:
                multi_user_exchanges += 1
            user_start_line = current.human_users[0][0]
            user_end_line = current.human_users[-1][1]
            assistant_line, assistant_text = current.assistant_finals[0]
            user_message_locators = tuple(
                format_line_locator(
                    f"{semantic_locator}/Turn:{current.ordinal}/User:{index}",
                    start_line,
                    end_line,
                )
                for index, (start_line, end_line, _) in enumerate(
                    current.human_users,
                    start=1,
                )
            )
            exchanges.append(
                CodexExchange(
                    turn_index=current.ordinal,
                    user_messages=tuple(text for _, _, text in current.human_users),
                    assistant_text=assistant_text,
                    user_locator=format_line_locator(
                        f"{semantic_locator}/Turn:{current.ordinal}/User",
                        user_start_line,
                        user_end_line,
                    ),
                    user_message_locators=user_message_locators,
                    assistant_locator=format_line_locator(
                        f"{semantic_locator}/Turn:{current.ordinal}/AssistantFinal", assistant_line, assistant_line
                    ),
                )
            )
        else:
            if current.human_users:
                unpaired_users += current.human_user_message_count
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
            finish_turn("superseded", line_number)
            turn_ordinal += 1
            current = _Turn(turn_ordinal, line_number)
            continue

        if record_type == "turn_context":
            if current is not None:
                current.turn_context_seen = True
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
                classification, cleaned = _classify_user_text(
                    raw_text,
                    before_turn_context=bool(current and not current.turn_context_seen),
                )
                if classification == "runtime":
                    runtime_users += 1
                elif current is None:
                    notes.add("发现不在 task 生命周期内的 human user message；未写入标准化正文。")
                else:
                    human_users += 1
                    current.human_user_message_count += 1
                    if classification == "structured_reply" and current.human_users:
                        start_line, _, existing = current.human_users[-1]
                        current.human_users[-1] = (
                            start_line,
                            line_number,
                            f"{existing}\n\n{cleaned}",
                        )
                    else:
                        current.human_users.append((line_number, line_number, cleaned))
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

    finish_turn("active")
    if unpaired_users:
        notes.add("存在没有显式 assistant final 的 human user turn；该 turn 未写入标准化正文。")
    if orphan_finals:
        notes.add("存在无法与 human user 配对的 assistant final；未写入标准化正文。")
    if active_open_turns:
        notes.add("文件末尾包含未结束 task，可能仍在写入；本轮延后导入。")
    if superseded_incomplete_turns:
        notes.add("历史 task 未见终止事件且已被后续 task 取代；该 turn 未写入标准化正文。")
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
    title_messages = [
        {"role": "user", "text": exchange.user_messages[0]}
        for exchange in retained_exchanges
    ]
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
        invalid_lines=tuple(invalid_lines),
        active_open_turn_count=active_open_turns,
        superseded_incomplete_turns=tuple(superseded_incomplete_turns),
        multi_user_exchange_count=multi_user_exchanges,
        ambiguous_user_turn_count=ambiguous_users,
        ambiguous_final_turn_count=ambiguous_finals,
        extra_session_meta_count=max(len(session_metas) - 1, 0),
        parse_notes=tuple(sorted(notes)),
    )


def _selected_session_paths(
    input_dir: Path,
    includes: Iterable[str] | None = None,
) -> list[Path]:
    """发现全部 rollout，或严格解析一批显式相对路径。"""

    if not input_dir.is_dir():
        raise FileNotFoundError(f"Codex session directory does not exist: {input_dir}")
    root = input_dir.resolve()
    if includes:
        paths: list[Path] = []
        for value in includes:
            path = (root / value).resolve()
            try:
                path.relative_to(root)
            except ValueError as exc:
                raise ValueError(f"included path escapes the input directory: {value}") from exc
            if not path.is_file() or path.suffix.lower() != ".jsonl":
                raise FileNotFoundError(f"included Codex session file does not exist: {value}")
            paths.append(path)
        return sorted(set(paths), key=lambda item: item.relative_to(root).as_posix())
    return [path.resolve() for path in sorted(input_dir.rglob("*.jsonl"))]


def iter_session_units(
    input_dir: Path,
    includes: Iterable[str] | None = None,
) -> Iterable[CodexSession]:
    root = input_dir.resolve()
    for path in _selected_session_paths(root, includes):
        yield parse_session(path, root)


def _intrinsic_skip_reason(
    unit: CodexSession,
    review_resolution: CodexReviewResolution | None = None,
) -> str:
    if unit.thread_kind == "subagent":
        return "subagent_excluded"
    if unit.thread_kind != "main":
        return "thread_kind_review"
    resolution_matches = bool(
        review_resolution and _review_resolution_matches(unit, review_resolution)
    )
    if review_resolution and not resolution_matches:
        return "review_resolution_mismatch_review"
    if unit.invalid_line_count and not resolution_matches:
        return "invalid_jsonl_review"
    if unit.active_open_turn_count:
        return "active_session_deferred"
    if unit.superseded_incomplete_turns and not resolution_matches:
        return "turn_boundary_review"
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
            parent_exchange.user_messages != child_exchange.user_messages
            or parent_exchange.assistant_text != child_exchange.assistant_text
        ):
            break
        prefix += 1
    return prefix


def resolve_session_units(
    units: Iterable[CodexSession],
    review_resolutions: dict[str, CodexReviewResolution] | None = None,
) -> tuple[CodexResolvedSession, ...]:
    ordered = tuple(units)
    review_resolutions = review_resolutions or {}
    by_provider_id: dict[str, CodexSession] = {}
    for unit in ordered:
        if unit.provider_session_id in by_provider_id:
            raise ValueError(f"duplicate Codex provider session ID: {unit.provider_session_id}")
        by_provider_id[unit.provider_session_id] = unit

    resolved: dict[str, CodexResolvedSession] = {}
    resolving: set[str] = set()

    def resolve(unit: CodexSession) -> CodexResolvedSession:
        cached = resolved.get(unit.provider_session_id)
        if cached is not None:
            return cached
        configured_resolution = review_resolutions.get(unit.provider_session_id)
        applied_resolution = (
            configured_resolution
            if configured_resolution and _review_resolution_matches(unit, configured_resolution)
            else None
        )
        intrinsic_reason = _intrinsic_skip_reason(unit, configured_resolution)
        if intrinsic_reason:
            result = CodexResolvedSession(
                unit=unit,
                exchanges=unit.exchanges,
                title=unit.title,
                source_scope="fork_unresolved" if unit.forked_from_id else "full",
                skip_reason=intrinsic_reason,
                review_resolution=applied_resolution,
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
                review_resolution=applied_resolution,
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
                review_resolution=applied_resolution,
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
                review_resolution=applied_resolution,
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
                review_resolution=applied_resolution,
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
                review_resolution=applied_resolution,
            )
            resolved[unit.provider_session_id] = result
            return result

        selected = unit.exchanges[prefix:]
        title_messages = [
            {"role": "user", "text": exchange.user_messages[0]}
            for exchange in selected
        ]
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
            review_resolution=applied_resolution,
        )
        resolved[unit.provider_session_id] = result
        return result

    return tuple(resolve(unit) for unit in ordered)


def iter_resolved_session_units(
    input_dir: Path,
    review_resolutions_path: Path = DEFAULT_REVIEW_RESOLUTIONS,
    includes: Iterable[str] | None = None,
) -> Iterable[CodexResolvedSession]:
    yield from resolve_session_units(
        iter_session_units(input_dir, includes),
        load_review_resolutions(review_resolutions_path),
    )


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
    review_resolution_hash = (
        resolved.review_resolution.content_hash if resolved.review_resolution else None
    )
    return bool(
        not resolved.skip_reason
        and current.get("source_hash") == resolved.unit.content_hash
        and current.get("fork_parent_hash") == resolved.fork_parent_hash
        and current.get("review_resolution_hash") == review_resolution_hash
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
    record = {
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
        "invalid_jsonl_line_numbers": [item.line_number for item in unit.invalid_lines],
        "active_open_turn_count": unit.active_open_turn_count,
        "superseded_incomplete_turn_count": len(unit.superseded_incomplete_turns),
        "superseded_incomplete_turns": [
            {
                "start_line": item.start_line,
                "next_start_line": item.next_start_line,
            }
            for item in unit.superseded_incomplete_turns
        ],
        "multi_user_exchange_count": unit.multi_user_exchange_count,
        "ambiguous_user_turn_count": unit.ambiguous_user_turn_count,
        "ambiguous_final_turn_count": unit.ambiguous_final_turn_count,
        "extra_session_meta_count": unit.extra_session_meta_count,
        "parse_notes": list(unit.parse_notes),
    }
    if resolved.review_resolution:
        resolution = resolved.review_resolution
        record.update(
            {
                "review_resolution": resolution.decision,
                "reviewed_at": resolution.reviewed_at,
                "reviewed_by": resolution.reviewed_by,
                "review_resolution_hash": resolution.content_hash,
                "reviewed_invalid_jsonl_lines": len(resolution.invalid_lines),
                "reviewed_superseded_incomplete_turn_count": len(resolution.superseded_turns),
            }
        )
    return record


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
        user_message_metadata = ""
        if len(exchange.user_message_locators) > 1:
            user_message_metadata = (
                f"- **User message count**: {len(exchange.user_message_locators)}\n"
                + "".join(
                    f"- **User message {index} locator**: `{locator}`\n"
                    for index, locator in enumerate(
                        exchange.user_message_locators,
                        start=1,
                    )
                )
            )
        rendered.append(
            f"## Exchange {ordinal}\n\n"
            f"- **Turn**: {exchange.turn_index}\n"
            f"- **User locator**: `{exchange.user_locator}`\n"
            f"{user_message_metadata}"
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
        "multi_user_exchange_count": unit.multi_user_exchange_count,
        "invalid_jsonl_lines": unit.invalid_line_count,
        "active_open_turn_count": unit.active_open_turn_count,
        "superseded_incomplete_turn_count": len(unit.superseded_incomplete_turns),
        "redaction_count": redactions,
        "importer_version": CODEX_IMPORTER_VERSION,
    }
    if resolved.review_resolution:
        resolution = resolved.review_resolution
        metadata.update(
            {
                "review_resolution": resolution.decision,
                "reviewed_at": resolution.reviewed_at,
                "reviewed_by": resolution.reviewed_by,
                "review_resolution_hash": f"sha256:{resolution.content_hash}",
                "reviewed_invalid_jsonl_lines": len(resolution.invalid_lines),
                "reviewed_superseded_incomplete_turn_count": len(resolution.superseded_turns),
            }
        )
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
        + (
            "> 本页包含人工审核后的受控恢复：损坏 JSONL 行及其所在的历史残缺 turn 已省略，"
            "其余具有明确 human user、assistant final 和 task 边界的 exchange 正常保留。\n\n"
            if resolved.review_resolution
            else ""
        )
        + "\n\n".join(rendered)
    )
    return yaml_document(metadata, body), redactions, title


class CodexStrategy:
    """按父子依赖顺序分析 rollout，并生成正式提交候选。"""

    def __init__(
        self,
        input_dir: Path,
        output_dir: Path,
        limit: int | None,
        session_ids: set[str] | None,
        includes: Iterable[str] | None,
        review_resolutions_path: Path,
    ) -> None:
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.limit = limit
        self.session_ids = set(session_ids or ())
        self.includes = tuple(includes) if includes is not None else None
        self.review_resolutions_path = review_resolutions_path

    def prepare(self, manifest: dict[str, Any]) -> ProviderPlan:
        paths = _selected_session_paths(self.input_dir.resolve(), self.includes)
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
        "omitted_superseded_turns": 0,
        "omitted_unpaired_users": 0,
        "multi_user_exchanges": 0,
        "invalid_lines": 0,
        "redactions": 0,
        }
        plan = ProviderPlan(
            provider="codex",
            importer="codex",
            input_root=self.input_dir,
            output_root=self.output_dir,
            stats=stats,
            selected_paths=paths,
            snapshots=[InputSnapshot(path, sha256_file(path)) for path in paths],
        )
        if self.review_resolutions_path.is_file():
            plan.snapshots.append(InputSnapshot(
                self.review_resolutions_path,
                sha256_file(self.review_resolutions_path),
            ))
        requested = self.session_ids
        seen_requested: set[str] = set()
        resolved_units = _dependency_order(
            iter_resolved_session_units(self.input_dir, self.review_resolutions_path, self.includes)
        )
        resolved_by_source_id = {item.unit.source_id: item for item in resolved_units}
        available_source_ids = {
            item.unit.source_id for item in resolved_units if _manifest_source_is_current(item, manifest)
        }
        for resolved in resolved_units:
            unit = resolved.unit
            if requested and unit.provider_session_id not in requested:
                reason = resolved.skip_reason
                status = (
                    "review" if reason in REVIEW_SKIP_REASONS
                    else "deferred" if reason in DEFERRED_SKIP_REASONS
                    else "excluded" if reason
                    else "imported" if _manifest_source_is_current(resolved, manifest)
                    else "ready"
                )
                if status == "review":
                    plan.blocking_reasons.append(reason)
                plan.inventory_records.append(unit_inventory_record(resolved, status, reason))
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
                skip_reason = "source_changed_during_analysis_review"
            if (
                not skip_reason
                and resolved.fork_parent_source_id
                and resolved.fork_parent_hash
            ):
                parent_result = resolved_by_source_id.get(resolved.fork_parent_source_id)
                parent_source_path = parent_result.unit.source_path if parent_result else None
                if parent_source_path is None or sha256_file(parent_source_path) != resolved.fork_parent_hash:
                    skip_reason = "fork_parent_changed_during_analysis_review"
            current = manifest["sources"].get(unit.source_id, {})
            if current and current.get("ingest_status") == "ready" and current.get("source_hash") != unit.content_hash and skip_reason not in REVIEW_SKIP_REASONS:
                skip_reason = "existing_source_changed_review"
            if current and current.get("ingest_status") == "ready" and current.get("source_path") != str(unit.source_path.resolve()):
                skip_reason = "source_id_collision_review"
            is_unchanged = not skip_reason and _manifest_source_is_current(resolved, manifest)
            if not skip_reason and not is_unchanged and self.limit is not None and stats["imported"] >= self.limit:
                skip_reason = "limit_reached"

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
            stats["omitted_superseded_turns"] += len(unit.superseded_incomplete_turns)
            stats["omitted_unpaired_users"] += unit.omitted_unpaired_user_count
            stats["multi_user_exchanges"] += unit.multi_user_exchange_count
            stats["invalid_lines"] += unit.invalid_line_count

            if skip_reason:
                stats["skipped"] += 1
                reasons = stats["skip_reasons"]
                reasons[skip_reason] = reasons.get(skip_reason, 0) + 1
                status = (
                    "review" if skip_reason in REVIEW_SKIP_REASONS or skip_reason.endswith("_review")
                    else "deferred" if skip_reason in DEFERRED_SKIP_REASONS
                    else "excluded"
                )
                if status == "review":
                    plan.blocking_reasons.append(skip_reason)
                plan.inventory_records.append(unit_inventory_record(resolved, status, skip_reason))
                continue
            if is_unchanged:
                stats["unchanged"] += 1
                available_source_ids.add(unit.source_id)
                plan.inventory_records.append(unit_inventory_record(resolved, "imported", ""))
                continue

            document, redactions, title = _render_session(resolved, utc_now()[:10])
            date = unit.created if unit.created != "unknown" else "undated"
            output_path = self.output_dir / f"{date}-{unit.provider_session_id}.md"
            stats["imported"] += 1
            stats["redactions"] += redactions
            plan.inventory_records.append(unit_inventory_record(resolved, "ready", ""))
            record = {
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
                "invalid_jsonl_lines": unit.invalid_line_count,
                "active_open_turn_count": unit.active_open_turn_count,
                "superseded_incomplete_turn_count": len(unit.superseded_incomplete_turns),
                "multi_user_exchange_count": unit.multi_user_exchange_count,
                "redaction_count": redactions,
                "imported_at": utc_now(),
                "importer_version": CODEX_IMPORTER_VERSION,
            }
            if resolved.review_resolution:
                resolution = resolved.review_resolution
                record.update(
                    {
                        "review_resolution": resolution.decision,
                        "reviewed_at": resolution.reviewed_at,
                        "reviewed_by": resolution.reviewed_by,
                        "review_resolution_hash": resolution.content_hash,
                        "reviewed_invalid_jsonl_lines": len(resolution.invalid_lines),
                        "reviewed_superseded_incomplete_turn_count": len(resolution.superseded_turns),
                    }
                )
            plan.sources.append(PlannedSource(
                unit.source_id, record, (PlannedFile(output_path, text=document),)
            ))
            available_source_ids.add(unit.source_id)

        selected = {path.resolve() for path in paths}
        resolved_source_ids = {item.unit.source_id for item in resolved_units}
        for source_id, item in manifest["sources"].items():
            if (
                item.get("origin") != "codex"
                or item.get("ingest_status") != "ready"
                or Path(str(item.get("source_path") or "")).resolve() not in selected
                or source_id in resolved_source_ids
            ):
                continue
            raw_path = Path(str(item["source_path"]))
            provider_session_id = str(item.get("provider_session_id") or "")
            if requested and provider_session_id not in requested:
                continue
            reason = "session_identity_missing_review"
            plan.blocking_reasons.append(reason)
            stats["discovered"] += 1
            stats["skipped"] += 1
            reasons = stats["skip_reasons"]
            reasons[reason] = reasons.get(reason, 0) + 1
            plan.inventory_records.append({
                "source_id": source_id,
                "unit_kind": "session",
                "provider_session_id": provider_session_id,
                "title": item.get("title") or provider_session_id,
                "raw_source_path": str(raw_path.resolve()),
                "raw_source_hash": sha256_file(raw_path),
                "raw_source_locator": item.get("source_locator") or f"{raw_path.name}@L1-L1",
                "parse_status": "review",
                "skip_reason": reason,
            })
            seen_requested.add(provider_session_id)
        missing = requested - seen_requested
        if missing:
            raise ValueError(f"Codex session IDs were not found: {', '.join(sorted(missing))}")
        stats["blocked"] = bool(plan.blocking_reasons)
        return plan


def import_sessions(
    input_dir: Path,
    output_dir: Path,
    manifest_path: Path,
    *,
    limit: int | None = None,
    dry_run: bool = False,
    session_ids: set[str] | None = None,
    includes: Iterable[str] | None = None,
    review_resolutions_path: Path = DEFAULT_REVIEW_RESOLUTIONS,
) -> dict[str, Any]:
    return execute_batch(
        [CodexStrategy(input_dir, output_dir, limit, session_ids, includes, review_resolutions_path)],
        manifest_path,
        dry_run=dry_run,
    )[0].stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Import Codex rollout sessions.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--session-id", action="append", dest="session_ids")
    parser.add_argument(
        "--include",
        action="append",
        help="import only the specified relative path under input; repeatable",
    )
    parser.add_argument("--review-resolutions", type=Path, default=DEFAULT_REVIEW_RESOLUTIONS)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    stats = import_sessions(
        args.input,
        args.output,
        args.manifest,
        limit=args.limit,
        dry_run=args.dry_run,
        session_ids=set(args.session_ids or ()),
        includes=args.include,
        review_resolutions_path=args.review_resolutions,
    )
    print("Codex import result:", ", ".join(f"{key}={value}" for key, value in stats.items()))


if __name__ == "__main__":
    main()
