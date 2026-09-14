#!/usr/bin/env python3
"""提供未完成 curation 来源的查询命令。"""

from __future__ import annotations

import argparse
from pathlib import Path

from src.corpus.core import MANIFEST_PATH, load_manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="List sources with unresolved curation states.")
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument(
        "--status",
        action="append",
        choices=("candidate", "drafted", "conflict"),
    )
    args = parser.parse_args()
    statuses = set(args.status or ("candidate", "drafted", "conflict"))
    manifest = load_manifest(args.manifest)
    rows = [
        (source_id, item)
        for source_id, item in manifest["sources"].items()
        if item.get("curation_status", "unassessed") in statuses
    ]
    for source_id, item in sorted(rows, key=lambda row: (str(row[1].get("created", "")), row[0])):
        print(f"{item.get('curation_status', 'unassessed'):10} {source_id}  {item.get('output_path', '?')}")
    print(f"Unprocessed sources: {len(rows)}")


if __name__ == "__main__":
    main()
