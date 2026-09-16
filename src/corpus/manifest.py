"""读写标准化来源 manifest 并定义状态契约。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.corpus.paths import MANIFEST_PATH


ALLOWED_INGEST_STATUSES = {"ready", "review", "skipped", "excluded"}
ALLOWED_CURATION_STATUSES = {
    "unassessed",
    "candidate",
    "drafted",
    "promoted",
    "store_only",
    "rejected",
    "conflict",
}
# 兼容首次基线中的旧 CLI 名称；新代码应使用上面两个独立状态集合。
ALLOWED_SOURCE_STATUSES = ALLOWED_CURATION_STATUSES


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    if not path.exists():
        return {"version": 2, "sources": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("sources"), dict):
        raise ValueError(f"invalid manifest format: {path}")
    return data


def save_manifest(data: dict[str, Any], path: Path = MANIFEST_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(path)
