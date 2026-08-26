#!/usr/bin/env python3
"""更新来源的 ingest 或 curation 状态；不会修改来源正文。"""

from __future__ import annotations

import argparse
from pathlib import Path

from scripts.common.wiki_core import (
    ALLOWED_CURATION_STATUSES,
    ALLOWED_INGEST_STATUSES,
    MANIFEST_PATH,
    load_manifest,
    save_manifest,
    utc_now,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_id")
    parser.add_argument("status")
    parser.add_argument("--kind", choices=("ingest", "curation"), default="curation")
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    args = parser.parse_args()
    manifest = load_manifest(args.manifest)
    if args.source_id not in manifest["sources"]:
        raise SystemExit(f"来源不存在: {args.source_id}")
    allowed = ALLOWED_INGEST_STATUSES if args.kind == "ingest" else ALLOWED_CURATION_STATUSES
    if args.status not in allowed:
        raise SystemExit(f"未知 {args.kind} status={args.status}；允许值: {', '.join(sorted(allowed))}")
    field = f"{args.kind}_status"
    manifest["sources"][args.source_id][field] = args.status
    manifest["sources"][args.source_id][f"{field}_updated_at"] = utc_now()
    save_manifest(manifest, args.manifest)
    print(f"已更新 {args.source_id}: {field}={args.status}")


if __name__ == "__main__":
    main()
