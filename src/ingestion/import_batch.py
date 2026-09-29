"""按显式文件集合运行跨来源的共同准备与提交流程。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.corpus.paths import MANIFEST_PATH
from src.ingestion.batch import SourceStrategy, execute_batch
from src.ingestion.import_articles import DEFAULT_ASSETS as ARTICLE_ASSETS
from src.ingestion.import_articles import DEFAULT_INPUT as ARTICLE_INPUT
from src.ingestion.import_articles import DEFAULT_OUTPUT as ARTICLE_OUTPUT
from src.ingestion.import_claude import ClaudeStrategy, DEFAULT_INPUT as CLAUDE_INPUT
from src.ingestion.import_claude import DEFAULT_OUTPUT as CLAUDE_OUTPUT
from src.ingestion.import_codex import CodexStrategy, DEFAULT_INPUT as CODEX_INPUT
from src.ingestion.import_codex import DEFAULT_OUTPUT as CODEX_OUTPUT
from src.ingestion.import_notes import DEFAULT_ASSETS as NOTE_ASSETS
from src.ingestion.import_notes import DEFAULT_INPUT as NOTE_INPUT
from src.ingestion.import_notes import DEFAULT_OUTPUT as NOTE_OUTPUT
from src.ingestion.import_web_chat import DEFAULT_ASSETS as WEB_CHAT_ASSETS
from src.ingestion.import_web_chat import DEFAULT_INPUT as WEB_CHAT_INPUT
from src.ingestion.import_web_chat import DEFAULT_OUTPUT as WEB_CHAT_OUTPUT
from src.ingestion.import_web_chat import WebChatStrategy
from src.ingestion.import_markdown import MarkdownPolicy, MarkdownStrategy
from src.ingestion.import_codex import DEFAULT_REVIEW_RESOLUTIONS


def main() -> None:
    parser = argparse.ArgumentParser(description="Import an explicit batch of source files.")
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--claude-input", type=Path, default=CLAUDE_INPUT)
    parser.add_argument("--codex-input", type=Path, default=CODEX_INPUT)
    parser.add_argument("--web-chat-input", type=Path, default=WEB_CHAT_INPUT)
    parser.add_argument("--notes-input", type=Path, default=NOTE_INPUT)
    parser.add_argument("--articles-input", type=Path, default=ARTICLE_INPUT)
    parser.add_argument("--review-resolutions", type=Path, default=DEFAULT_REVIEW_RESOLUTIONS)
    parser.add_argument("--claude-include", action="append")
    parser.add_argument("--claude-document-include", action="append")
    parser.add_argument("--codex-include", action="append")
    parser.add_argument("--codex-session-id", action="append")
    parser.add_argument("--web-chat-include", action="append")
    parser.add_argument("--note-include", action="append")
    parser.add_argument("--article-include", action="append")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not any((
        args.claude_include,
        args.claude_document_include,
        args.codex_include,
        args.web_chat_include,
        args.note_include,
        args.article_include,
    )):
        parser.error("at least one included source file is required")
    if args.codex_session_id and not args.codex_include:
        parser.error("--codex-session-id requires --codex-include")

    strategies: list[SourceStrategy] = []
    if args.claude_include:
        strategies.append(ClaudeStrategy(
            args.claude_input, CLAUDE_OUTPUT, "session", args.claude_include, None
        ))
    if args.claude_document_include:
        strategies.append(MarkdownStrategy(
            args.claude_input,
            MarkdownPolicy("note", "claude-export", NOTE_OUTPUT, NOTE_ASSETS),
            args.claude_document_include,
            None,
        ))
    if args.codex_include:
        strategies.append(CodexStrategy(
            args.codex_input,
            CODEX_OUTPUT,
            None,
            set(args.codex_session_id or ()),
            args.codex_include,
            args.review_resolutions,
        ))
    if args.web_chat_include:
        strategies.append(WebChatStrategy(
            args.web_chat_input,
            WEB_CHAT_OUTPUT,
            WEB_CHAT_ASSETS,
            args.review_resolutions,
            args.web_chat_include,
        ))
    if args.note_include:
        strategies.append(MarkdownStrategy(
            args.notes_input,
            MarkdownPolicy("note", "personal-notes", NOTE_OUTPUT, NOTE_ASSETS),
            args.note_include,
            None,
        ))
    if args.article_include:
        strategies.append(MarkdownStrategy(
            args.articles_input,
            MarkdownPolicy("article", "external-articles", ARTICLE_OUTPUT, ARTICLE_ASSETS, "external_source"),
            args.article_include,
            None,
        ))
    plans = execute_batch(strategies, args.manifest, dry_run=args.dry_run)
    print(json.dumps({
        "blocked": any(plan.blocking_reasons for plan in plans),
        "dry_run": args.dry_run,
        "providers": [
            {"provider": plan.provider, "stats": plan.stats} for plan in plans
        ],
    }, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
