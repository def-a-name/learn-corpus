#!/usr/bin/env python3
"""把个人笔记或显式选择的 Claude document 导入只读 note source。"""

from __future__ import annotations

import argparse
from pathlib import Path

from src.ingestion.import_markdown import MarkdownPolicy, import_markdown_sources, print_stats
from src.corpus.paths import MANIFEST_PATH, REPO_ROOT


DEFAULT_INPUT = REPO_ROOT.parent / "notes"
DEFAULT_OUTPUT = REPO_ROOT / "sources" / "notes"
DEFAULT_ASSETS = REPO_ROOT / "sources" / "assets"


def import_notes(
    input_root: Path,
    output_dir: Path = DEFAULT_OUTPUT,
    manifest_path: Path = MANIFEST_PATH,
    *,
    asset_root: Path = DEFAULT_ASSETS,
    origin: str = "personal-notes",
    includes: list[str] | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict:
    policy = MarkdownPolicy("note", origin, output_dir, asset_root)
    return import_markdown_sources(
        input_root,
        policy,
        manifest_path,
        includes=includes,
        limit=limit,
        dry_run=dry_run,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Import personal Markdown notes.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--assets", type=Path, default=DEFAULT_ASSETS)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--origin", choices=("personal-notes", "claude-export"), default="personal-notes")
    parser.add_argument(
        "--include",
        action="append",
        help="import only the specified relative path under input; repeatable",
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    stats = import_notes(
        args.input,
        args.output,
        args.manifest,
        asset_root=args.assets,
        origin=args.origin,
        includes=args.include,
        limit=args.limit,
        dry_run=args.dry_run,
    )
    print_stats("Notes import", stats, args.dry_run)


if __name__ == "__main__":
    main()
