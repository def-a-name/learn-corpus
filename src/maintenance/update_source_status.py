#!/usr/bin/env python3
"""提供来源 ingest 或 curation 状态更新命令。"""

from __future__ import annotations

import argparse
from pathlib import Path

from src.corpus.manifest import (
    ALLOWED_CURATION_STATUSES,
    ALLOWED_INGEST_STATUSES,
    load_manifest,
    save_manifest,
    utc_now,
)
from src.corpus.paths import MANIFEST_PATH


def main() -> None:
    parser = argparse.ArgumentParser(description="Update a source ingest or curation status.")
    parser.add_argument("source_id")
    parser.add_argument("status")
    parser.add_argument("--kind", choices=("ingest", "curation"), default="curation")
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    args = parser.parse_args()
    manifest = load_manifest(args.manifest)
    if args.source_id not in manifest["sources"]:
        raise SystemExit(f"source does not exist: {args.source_id}")
    allowed = ALLOWED_INGEST_STATUSES if args.kind == "ingest" else ALLOWED_CURATION_STATUSES
    if args.status not in allowed:
        raise SystemExit(
            f"unknown {args.kind} status={args.status}; "
            f"allowed values: {', '.join(sorted(allowed))}"
        )
    field = f"{args.kind}_status"
    manifest["sources"][args.source_id][field] = args.status
    manifest["sources"][args.source_id][f"{field}_updated_at"] = utc_now()
    save_manifest(manifest, args.manifest)
    print(f"Updated {args.source_id}: {field}={args.status}")


if __name__ == "__main__":
    main()
