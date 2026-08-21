#!/usr/bin/env python3
"""更新来源的整理状态；不会修改来源正文。"""

from __future__ import annotations

import argparse
from pathlib import Path

from wiki_core import ALLOWED_SOURCE_STATUSES, MANIFEST_PATH, load_manifest, save_manifest, utc_now


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_id")
    parser.add_argument("status", choices=sorted(ALLOWED_SOURCE_STATUSES))
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    args = parser.parse_args()
    manifest = load_manifest(args.manifest)
    if args.source_id not in manifest["sources"]:
        raise SystemExit(f"来源不存在: {args.source_id}")
    manifest["sources"][args.source_id]["status"] = args.status
    manifest["sources"][args.source_id]["status_updated_at"] = utc_now()
    save_manifest(manifest, args.manifest)
    print(f"已更新 {args.source_id}: {args.status}")


if __name__ == "__main__":
    main()
