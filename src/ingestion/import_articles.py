#!/usr/bin/env python3
"""把外部保存的 Markdown 文章导入只读 article source。"""

from __future__ import annotations

import argparse
from pathlib import Path

from src.ingestion.markdown_sources import MarkdownPolicy, import_markdown_sources, print_stats
from src.shared.corpus_core import MANIFEST_PATH, REPO_ROOT


DEFAULT_INPUT = REPO_ROOT.parent / "articles"
DEFAULT_OUTPUT = REPO_ROOT / "sources" / "articles"
DEFAULT_ASSETS = REPO_ROOT / "sources" / "assets"


def import_articles(
    input_root: Path,
    output_dir: Path = DEFAULT_OUTPUT,
    manifest_path: Path = MANIFEST_PATH,
    *,
    asset_root: Path = DEFAULT_ASSETS,
    includes: list[str] | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict:
    policy = MarkdownPolicy("article", "external-articles", output_dir, asset_root, "external_source")
    return import_markdown_sources(
        input_root,
        policy,
        manifest_path,
        includes=includes,
        limit=limit,
        dry_run=dry_run,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Import external Markdown articles.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--assets", type=Path, default=DEFAULT_ASSETS)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument(
        "--include",
        action="append",
        help="import only the specified relative path under input; repeatable",
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    stats = import_articles(
        args.input,
        args.output,
        args.manifest,
        asset_root=args.assets,
        includes=args.include,
        limit=args.limit,
        dry_run=args.dry_run,
    )
    print_stats("Articles import", stats, args.dry_run)


if __name__ == "__main__":
    main()
