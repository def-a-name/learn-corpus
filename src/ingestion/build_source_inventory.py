#!/usr/bin/env python3
"""盘点当前可访问的会话、notes 和 articles 输入范围。"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from src.ingestion.import_claude import DEFAULT_INPUT as CLAUDE_INPUT
from src.ingestion.import_codex import DEFAULT_INPUT as CODEX_INPUT
from src.ingestion.import_codex import DEFAULT_REVIEW_RESOLUTIONS as CODEX_REVIEW_RESOLUTIONS
from src.ingestion.import_web_chat import DEFAULT_INPUT as WEB_CHAT_INPUT
from src.ingestion.import_web_chat import DEFAULT_REVIEW_RESOLUTIONS as WEB_CHAT_REVIEW_RESOLUTIONS
from src.corpus.manifest import load_manifest
from src.corpus.paths import MANIFEST_PATH, REPO_ROOT


DEFAULT_NOTES_INPUT = REPO_ROOT.parent / "notes"
DEFAULT_ARTICLES_INPUT = REPO_ROOT.parent / "articles"
DEFAULT_OUTPUT = REPO_ROOT / "meta" / "source-inventory.json"
WEB_CHAT_MISSING = [
    "只盘点当前目录中的 Chrome 插件 Markdown 导出，不代表账号全部网页历史",
    "插件未导出的模型、分支、message ID、assistant 时间和隐藏事件无法恢复",
]


def _display_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return Path("..", resolved.relative_to(REPO_ROOT.parent)).as_posix()
    except ValueError:
        pass
    home = Path.home().resolve()
    try:
        return f"~/{resolved.relative_to(home).as_posix()}"
    except ValueError:
        return str(resolved)


def _missing_input(provider: str, input_path: Path, source_format: str, known_missing: list[str]) -> dict[str, Any]:
    return {
        "provider": provider,
        "input_path": _display_path(input_path),
        "format": source_format,
        "available": False,
        "date_range": {"from": None, "to": None},
        "discovered": 0,
        "retained": 0,
        "skipped": 0,
        "known_missing": [f"输入路径当前不可用: {_display_path(input_path)}", *known_missing],
    }


def _analysis_strategy(
    provider: str,
    input_root: Path,
    manifest_path: Path,
    includes: Iterable[str] | None,
    codex_review_resolutions: Path,
    web_chat_review_resolutions: Path,
):
    """为盘点复用正式导入的来源分析策略。"""

    from src.ingestion.import_claude import ClaudeStrategy
    from src.ingestion.import_codex import CodexStrategy
    from src.ingestion.import_web_chat import WebChatStrategy
    from src.ingestion.markdown_sources import MarkdownPolicy, MarkdownStrategy

    source_root = (manifest_path.parent.parent if manifest_path.parent.name == "meta" else manifest_path.parent) / "sources"
    if provider == "claude-export":
        return ClaudeStrategy(input_root, source_root / "conversations" / "claude", "all", includes, None)
    if provider == "codex":
        return CodexStrategy(input_root, source_root / "conversations" / "codex", None, None, includes, codex_review_resolutions)
    if provider == "web-chat":
        return WebChatStrategy(input_root, source_root / "conversations", source_root / "assets", web_chat_review_resolutions, includes)
    if provider == "notes":
        return MarkdownStrategy(input_root, MarkdownPolicy("note", "personal-notes", source_root / "notes", source_root / "assets"), includes, None)
    if provider == "articles":
        return MarkdownStrategy(input_root, MarkdownPolicy("article", "external-articles", source_root / "articles", source_root / "assets", "external_source"), includes, None)
    raise ValueError(f"unsupported scoped inventory provider: {provider}")


def update_inventory(
    existing: dict[str, Any],
    provider: str,
    includes: Iterable[str],
    *,
    claude_input: Path = CLAUDE_INPUT,
    codex_input: Path = CODEX_INPUT,
    notes_input: Path = DEFAULT_NOTES_INPUT,
    articles_input: Path = DEFAULT_ARTICLES_INPUT,
    web_chat_input: Path = WEB_CHAT_INPUT,
    manifest_path: Path = MANIFEST_PATH,
    codex_review_resolutions: Path = CODEX_REVIEW_RESOLUTIONS,
    web_chat_review_resolutions: Path = WEB_CHAT_REVIEW_RESOLUTIONS,
    scanned_at: str | None = None,
) -> dict[str, Any]:
    """用来源策略分析显式输入，并定向合并现有盘点快照。"""

    from src.ingestion.batch import merge_inventory

    values = tuple(includes)
    if not values:
        raise ValueError("at least one included source file is required")
    roots = {
        "claude-export": claude_input,
        "codex": codex_input,
        "web-chat": web_chat_input,
        "notes": notes_input,
        "articles": articles_input,
    }
    if provider not in roots:
        raise ValueError(f"unsupported scoped inventory provider: {provider}")
    manifest = load_manifest(manifest_path)
    plan = _analysis_strategy(
        provider,
        roots[provider],
        manifest_path,
        values,
        codex_review_resolutions,
        web_chat_review_resolutions,
    ).prepare(manifest)
    return merge_inventory(existing, [plan], manifest, scanned_at=scanned_at)


def build_inventory(
    claude_input: Path,
    codex_input: Path,
    notes_input: Path,
    manifest_path: Path = MANIFEST_PATH,
    *,
    articles_input: Path | None = None,
    web_chat_input: Path | None = None,
    codex_review_resolutions: Path = CODEX_REVIEW_RESOLUTIONS,
    web_chat_review_resolutions: Path = WEB_CHAT_REVIEW_RESOLUTIONS,
    scanned_at: str | None = None,
) -> dict[str, Any]:
    """用五类来源策略生成完整但不执行正式导入的覆盖快照。"""

    from src.ingestion.batch import merge_inventory

    articles_input = articles_input or notes_input.parent / "articles"
    web_chat_input = web_chat_input or notes_input.parent / "web-chats"
    timestamp = scanned_at or datetime.now(ZoneInfo("Asia/Hong_Kong")).isoformat(timespec="seconds")
    inventory: dict[str, Any] = {
        "version": 3,
        "scanned_at": timestamp,
        "updated_at": timestamp,
        "coverage_boundary": (
            "只覆盖当前机器上可访问的五类已知输入位置，不代表所有历史会话已提供；"
            "Claude 和 web-chat 事件计数只表示现有有损 Markdown 导出中仍可见的内容。"
        ),
        "known_missing": [
            "未导出到 web-chats/ 的 ChatGPT/DeepSeek 历史尚未提供",
            "Claude Web、其他设备和其他账号的历史尚未提供",
        ],
        "inputs": [],
    }
    manifest = load_manifest(manifest_path)
    sources = (
        ("claude-export", claude_input, "markdown-export", [
            "生成这批 Markdown 的原始 ~/.claude/projects/**/*.jsonl 已不可得；真实 session ID、完整 tool result、system 和 progress 事件无法恢复",
            "其他设备、尚未导出的 Claude Code 及 Claude Web 会话不在本次范围",
        ]),
        ("web-chat", web_chat_input, "browser-extension-markdown", WEB_CHAT_MISSING),
        ("codex", codex_input, "codex-rollout-jsonl", ["其他设备或账号的 Codex rollout 不在本次范围"]),
        ("notes", notes_input, "markdown", ["当前只盘点 Markdown 正文；图片仅计数，V1 不做图像内容理解"]),
        ("articles", articles_input, "markdown", ["远程图片和链接只记录引用，inventory 不下载或联网检查"]),
    )
    for provider, input_root, source_format, known_missing in sources:
        if not input_root.is_dir():
            missing = _missing_input(provider, input_root, source_format, known_missing)
            missing["scanned_at"] = timestamp
            inventory["inputs"].append(missing)
            continue
        plan = _analysis_strategy(
            provider,
            input_root,
            manifest_path,
            None,
            codex_review_resolutions,
            web_chat_review_resolutions,
        ).prepare(manifest)
        inventory = merge_inventory(inventory, [plan], manifest, scanned_at=timestamp)
        current = next(item for item in inventory["inputs"] if item["provider"] == provider)
        current["format"] = source_format
        current["known_missing"] = known_missing
        current["input_path"] = _display_path(input_root)
        if provider in {"notes", "articles"}:
            current["date_range"] = {"from": None, "to": None}
        if provider == "notes":
            current["assets_discovered"] = sum(
                path.is_file() and path.suffix.lower() != ".md"
                and not any(part.startswith(".") for part in path.relative_to(input_root).parts)
                for path in input_root.rglob("*")
            )
        if current["discovered"] != current["retained"] + current["skipped"]:
            raise ValueError(f"{provider} inventory counts are inconsistent")
    return inventory


def save_inventory(inventory: dict[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    temp_path.write_text(json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(output_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the source coverage inventory.")
    parser.add_argument("--claude-input", type=Path, default=CLAUDE_INPUT)
    parser.add_argument("--codex-input", type=Path, default=CODEX_INPUT)
    parser.add_argument("--notes-input", type=Path, default=DEFAULT_NOTES_INPUT)
    parser.add_argument("--articles-input", type=Path, default=DEFAULT_ARTICLES_INPUT)
    parser.add_argument("--web-chat-input", type=Path, default=WEB_CHAT_INPUT)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument(
        "--codex-review-resolutions",
        type=Path,
        default=CODEX_REVIEW_RESOLUTIONS,
    )
    parser.add_argument(
        "--web-chat-review-resolutions",
        type=Path,
        default=WEB_CHAT_REVIEW_RESOLUTIONS,
    )
    parser.add_argument(
        "--provider",
        choices=("claude-export", "web-chat", "codex", "notes", "articles"),
        help="update only one provider in the existing inventory",
    )
    parser.add_argument(
        "--include",
        action="append",
        help="update only the specified relative raw input path; repeatable",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if bool(args.provider) != bool(args.include):
        parser.error("--provider and at least one --include must be used together")
    if args.provider is not None:
        if not args.output.is_file():
            parser.error("scoped inventory update requires an existing output file")
        try:
            existing = json.loads(args.output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            parser.error(f"cannot read existing source inventory: {exc}")
        inventory = update_inventory(
            existing,
            args.provider,
            args.include,
            claude_input=args.claude_input,
            codex_input=args.codex_input,
            notes_input=args.notes_input,
            articles_input=args.articles_input,
            web_chat_input=args.web_chat_input,
            manifest_path=args.manifest,
            codex_review_resolutions=args.codex_review_resolutions,
            web_chat_review_resolutions=args.web_chat_review_resolutions,
        )
    else:
        inventory = build_inventory(
            args.claude_input,
            args.codex_input,
            args.notes_input,
            args.manifest,
            articles_input=args.articles_input,
            web_chat_input=args.web_chat_input,
            codex_review_resolutions=args.codex_review_resolutions,
            web_chat_review_resolutions=args.web_chat_review_resolutions,
        )
    if not args.dry_run:
        save_inventory(inventory, args.output)
    selected = (
        inventory["inputs"]
        if args.provider is None
        else [item for item in inventory["inputs"] if item["provider"] == args.provider]
    )
    summary = ", ".join(
        f"{item['provider']}={item['discovered']}/{item['retained']}/{item['skipped']}" for item in selected
    )
    print(f"Source inventory ({'dry-run' if args.dry_run else 'written'}): {summary}")


if __name__ == "__main__":
    main()
