#!/usr/bin/env python3
"""列出 manifest 中尚未完成知识整理的来源。"""

from __future__ import annotations

import argparse
from pathlib import Path

from wiki_core import MANIFEST_PATH, load_manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--status", action="append", choices=("pending", "review", "conflict"))
    args = parser.parse_args()
    statuses = set(args.status or ("pending", "review", "conflict"))
    manifest = load_manifest(args.manifest)
    rows = [
        (source_id, item)
        for source_id, item in manifest["sources"].items()
        if item.get("status", "pending") in statuses
    ]
    for source_id, item in sorted(rows, key=lambda row: (str(row[1].get("created", "")), row[0])):
        print(f"{item.get('status', 'pending'):8} {source_id}  {item.get('output_path', '?')}")
    print(f"共 {len(rows)} 个待处理来源")


if __name__ == "__main__":
    main()
