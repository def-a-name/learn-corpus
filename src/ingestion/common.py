#!/usr/bin/env python3
"""来源导入器共用的文本清洗、过滤和标准化辅助逻辑。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from src.corpus.document import yaml_document


CLAUDE_IMPORTER_VERSION = 9
CLAUDE_ASSISTANT_FINAL_DETECTION = "heuristic"
# 兼容尚未迁移的外部调用；Claude importer 使用专用版本常量。
IMPORTER_VERSION = CLAUDE_IMPORTER_VERSION
CODEX_IMPORTER_VERSION = 10
CODEX_ASSISTANT_FINAL_DETECTION = "explicit"
MARKDOWN_SOURCE_IMPORTER_VERSION = 1
WEB_CHAT_IMPORTER_VERSION = 2
WEB_CHAT_ASSISTANT_FINAL_DETECTION = "heuristic"

_REMOVABLE_BLOCKS = (
    "system-reminder",
    "environment_context",
    "turn_aborted",
    "local-command-caveat",
    "local-command-stdout",
    "local-command-stderr",
    "command-name",
    "command-message",
    "command-args",
    "user_shell_command",
)

_TRIVIAL_GREETING_TEXTS = frozenset({"hi", "hello", "hey", "你好", "您好"})
_TRIVIAL_TEST_TEXTS = frozenset({"test", "测试"})
_TRIVIAL_USER_TEXTS = _TRIVIAL_GREETING_TEXTS | _TRIVIAL_TEST_TEXTS
_TRIVIAL_ASSISTANT_TEXTS = _TRIVIAL_USER_TEXTS | frozenset(
    {
        "你好很高兴见到你",
        "您好很高兴见到您",
        "测试成功",
        "测试正常",
        "测试通过",
        "收到测试消息",
        "testsuccessful",
        "testpassed",
    }
)
_GREETING_ASSISTANCE_MARKERS = (
    "需要我帮",
    "需要我协助",
    "有什么可以帮",
    "有什么我可以帮",
    "有什么需要帮",
    "howcanihelp",
    "whatcanidoforyou",
)
_TRIVIAL_OPERATIONAL_REPLY_PREFIXES = (
    "youvehityourlimit",
    "youraccountdoesnothaveaccesstoclaudecodepleaserunlogin",
)

_SECRET_PATTERNS = [
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|secret)"
        r"(['\"]?)(\s*[:=]\s*)(['\"]?)([^\s'\"`,;}]+)\4"
    ),
    re.compile(
        r"(?i)(密码|口令)(\s*(?::|：|=|是|为)\s*|\s+)"
        r"((?=[^\s'\"`,，。；;\]}]*\d)[^\s'\"`,，。；;\]}]+)"
    ),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
]


def read_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    """读取 JSONL；损坏或活跃写入中的行会被跳过并计数。"""
    records: list[dict[str, Any]] = []
    invalid = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                invalid += 1
                continue
            if isinstance(value, dict):
                records.append(value)
    return records, invalid


def clean_message(text: str) -> str:
    text = text.replace("\r\n", "\n")
    for tag in _REMOVABLE_BLOCKS:
        text = re.sub(fr"<{tag}>.*?</{tag}>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<ide_[^>]+>.*?</ide_[^>]+>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_trivial_text(text: str) -> str:
    return re.sub(r"[^a-z0-9一-鿿]+", "", text.lower())


def is_trivial_user_text(text: str) -> bool:
    return normalize_trivial_text(text) in _TRIVIAL_USER_TEXTS


def is_exact_meaningless_exchange(user_text: str, assistant_text: str) -> bool:
    """只过滤双方都明确无语义内容的纯寒暄/测试 exchange。"""
    normalized_user = normalize_trivial_text(user_text)
    if normalized_user not in _TRIVIAL_USER_TEXTS:
        return False
    normalized_assistant = normalize_trivial_text(assistant_text)
    if normalized_assistant in _TRIVIAL_ASSISTANT_TEXTS:
        return True
    if any(
        normalized_assistant.startswith(prefix) for prefix in _TRIVIAL_OPERATIONAL_REPLY_PREFIXES
    ):
        return True
    if normalized_user in _TRIVIAL_GREETING_TEXTS:
        starts_with_greeting = any(
            normalized_assistant.startswith(greeting) for greeting in _TRIVIAL_GREETING_TEXTS
        )
        if starts_with_greeting and any(
            marker in normalized_assistant for marker in _GREETING_ASSISTANCE_MARKERS
        ):
            return True
        return False
    return False


def is_trivial_session(messages: list[dict[str, str]]) -> bool:
    user_messages = [item["text"] for item in messages if item["role"] == "user"]
    return bool(user_messages) and all(is_trivial_user_text(message) for message in user_messages)


def is_noise_message(text: str) -> bool:
    normalized = text.lower()
    return "juice_schema.xsd" in normalized and "juice number" in normalized


def conversation_skip_reason(messages: list[dict[str, str]]) -> str:
    if is_trivial_session(messages):
        return "trivial_session"
    if not any(item["role"] == "user" for item in messages):
        return "no_user_message"
    if not has_substantive_exchange(messages):
        return "no_substantive_assistant_final"
    return ""


def has_substantive_exchange(messages: list[dict[str, str]]) -> bool:
    assistant_messages = [item["text"].lower() for item in messages if item["role"] == "assistant"]
    unusable_markers = (
        "does not have access to claude code",
        "please run /login",
    )
    return bool(assistant_messages) and any(
        not any(marker in message for marker in unusable_markers) for message in assistant_messages
    )


def redact_secrets(text: str) -> tuple[str, int]:
    count = 0

    def assignment_replacement(match: re.Match[str]) -> str:
        nonlocal count
        value = match.group(5).strip().lower()
        if value in {"[redacted]", "[masked]", "redacted", "masked"}:
            return match.group(0)
        count += 1
        return f"{match.group(1)}{match.group(2)}{match.group(3)}{match.group(4)}[REDACTED]{match.group(4)}"

    text = _SECRET_PATTERNS[0].sub(assignment_replacement, text)

    def chinese_assignment_replacement(match: re.Match[str]) -> str:
        nonlocal count
        value = match.group(3).strip().lower()
        if value in {"[redacted]", "[masked]", "redacted", "masked"}:
            return match.group(0)
        count += 1
        return f"{match.group(1)}{match.group(2)}[REDACTED]"

    text = _SECRET_PATTERNS[1].sub(chinese_assignment_replacement, text)
    for pattern in _SECRET_PATTERNS[2:]:
        text, replacements = pattern.subn("[REDACTED]", text)
        count += replacements
    return text, count


def source_needs_redaction(path: Path) -> bool:
    if not path.is_file():
        return False
    _, count = redact_secrets(path.read_text(encoding="utf-8", errors="replace"))
    return count > 0


def extract_text(content: Any, allowed_types: set[str]) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") not in allowed_types:
            continue
        value = block.get("text", "")
        if isinstance(value, str) and value.strip():
            parts.append(value)
    return "\n\n".join(parts)


def derive_title(origin: str, messages: list[dict[str, str]]) -> str:
    candidates = [item["text"] for item in messages if item["role"] == "user"]
    first_user = next(
        (
            text
            for text in candidates
            if not is_trivial_user_text(text) and not is_noise_message(text)
        ),
        candidates[0] if candidates else "未命名会话",
    )
    first_line = re.sub(r"\s+", " ", first_user).strip()
    first_line = re.sub(r"^#+\s*", "", first_line)
    if len(first_line) > 72:
        first_line = first_line[:69].rstrip() + "..."
    label = "Claude" if origin == "claude" else "Codex"
    return f"{label} 会话：{first_line}"


def project_from_path(path: str | Path) -> str:
    name = Path(path).parent.name
    for prefix in ("-home-fan-ampotech-", "-home-fan-", "-home-fanxiang-"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    return name.strip("-") or "global"


def build_conversation_document(
    *,
    source_id: str,
    origin: str,
    title: str,
    created: str,
    imported: str,
    project: str,
    source_path: Path,
    source_hash: str,
    messages: list[dict[str, str]],
    extra_metadata: dict[str, Any] | None = None,
    initial_redactions: int = 0,
) -> tuple[str, int]:
    redactions = initial_redactions
    rendered_messages: list[str] = []
    role_counts = {"user": 0, "assistant": 0}

    for message in messages:
        role = message["role"]
        text, count = redact_secrets(clean_message(message["text"]))
        redactions += count
        if not text:
            continue
        role_counts[role] += 1
        heading = "用户" if role == "user" else "助手"
        rendered_messages.append(f"## {heading} {role_counts[role]}\n\n{text}")

    metadata: dict[str, Any] = {
        "id": source_id,
        "type": "conversation",
        "origin": origin,
        "title": title,
        "created": created or "unknown",
        "imported": imported,
        "project": project,
        "source_path": str(source_path.resolve()),
        "source_hash": f"sha256:{source_hash}",
        "message_count": len(rendered_messages),
        "redaction_count": redactions,
    }
    if extra_metadata:
        metadata.update({key: value for key, value in extra_metadata.items() if value not in (None, "")})

    body = (
        f"# {title}\n\n"
        "> 本页由导入器生成，是原始会话的标准化副本；形成事实主张时应核对对应 locator 的原始证据。\n\n"
        + "\n\n".join(rendered_messages)
    )
    return yaml_document(metadata, body), redactions


def discover_jsonl(root: Path, *, exclude_subagents: bool = False) -> Iterable[Path]:
    for path in sorted(root.rglob("*.jsonl")):
        if exclude_subagents and "subagents" in path.parts:
            continue
        yield path
