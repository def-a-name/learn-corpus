#!/usr/bin/env python3
"""提供标准化来源受控删除的命令行入口。"""

from __future__ import annotations

import argparse
from pathlib import Path

from src.corpus.core import REPO_ROOT
from src.corpus.removal import SourceRemovalError, remove_source


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Remove one standardized Markdown source and its registered assets."
    )
    parser.add_argument("source_id", help="exact manifest source ID to remove")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        result = remove_source(
            args.source_id,
            repo_root=args.repo_root,
            manifest_path=args.manifest,
            dry_run=args.dry_run,
        )
    except SourceRemovalError as exc:
        parser.error(str(exc))
    mode = "dry-run" if args.dry_run else "removed"
    print(
        f"Source removal ({mode}): source_id={result['source_id']}, "
        f"output={result['output_path']}, assets={result['assets']}"
    )


if __name__ == "__main__":
    main()
