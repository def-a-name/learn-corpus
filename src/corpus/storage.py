"""提供标准化来源与登记附件共用的文件存储操作。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from src.corpus.paths import REPO_ROOT


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def atomic_copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(source.read_bytes())
    temporary.replace(target)


def registered_assets_are_current(
    records: Any,
    repo_root: Path = REPO_ROOT,
) -> bool:
    if not isinstance(records, list):
        return not records
    for item in records:
        if not isinstance(item, dict):
            return False
        path = repo_root / str(item.get("stored_path") or "")
        if not path.is_file() or sha256_file(path) != item.get("asset_hash"):
            return False
    return True
