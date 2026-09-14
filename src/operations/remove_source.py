#!/usr/bin/env python3
"""一次性删除标准化 Markdown source 及 manifest 登记的附件。"""

from __future__ import annotations

import argparse
from pathlib import Path, PurePosixPath
from typing import Any

from src.corpus.core import (
    REPO_ROOT,
    load_manifest,
    parse_frontmatter,
    save_manifest,
    sha256_file,
)
from src.corpus.ingest_log import SourceChangeTracker


_SOURCE_ROOTS = {
    "conversation": PurePosixPath("sources/conversations"),
    "note": PurePosixPath("sources/notes"),
    "article": PurePosixPath("sources/articles"),
}
_ASSET_ROOT = PurePosixPath("sources/assets")
_DEPENDENCY_FIELDS = ("duplicate_of", "fork_parent_source_id")


class SourceRemovalError(ValueError):
    """表示当前 source 不能安全地一次性删除。"""


def _canonical_relative_path(value: Any, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value:
        raise SourceRemovalError(f"{label} must be a canonical repository-relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise SourceRemovalError(f"{label} must be a canonical repository-relative path")
    return path


def _resolve_under(
    repo_root: Path,
    relative_path: PurePosixPath,
    allowed_roots: tuple[PurePosixPath, ...],
    label: str,
) -> Path:
    if not any(relative_path.is_relative_to(root) for root in allowed_roots):
        roots = ", ".join(root.as_posix() for root in allowed_roots)
        raise SourceRemovalError(f"{label} must be under one of: {roots}")
    candidate = repo_root / Path(relative_path)
    current = candidate
    while current != repo_root:
        if current.is_symlink():
            raise SourceRemovalError(f"{label} must not traverse a symbolic link")
        current = current.parent
    resolved = candidate.resolve()
    if not any(resolved.is_relative_to((repo_root / Path(root)).resolve()) for root in allowed_roots):
        raise SourceRemovalError(f"{label} resolves outside its allowed root")
    return resolved


def _registered_assets(record: dict[str, Any], repo_root: Path) -> tuple[Path, ...]:
    raw_assets = record.get("assets", [])
    if raw_assets is None:
        raw_assets = []
    if not isinstance(raw_assets, list):
        raise SourceRemovalError("manifest assets must be a list")
    paths: list[Path] = []
    for index, item in enumerate(raw_assets, start=1):
        if not isinstance(item, dict):
            raise SourceRemovalError(f"manifest asset {index} must be an object")
        relative = _canonical_relative_path(
            item.get("stored_path"), f"manifest asset {index} stored_path"
        )
        path = _resolve_under(
            repo_root, relative, (_ASSET_ROOT,), f"manifest asset {index} stored_path"
        )
        if path in paths:
            raise SourceRemovalError(
                f"manifest contains a duplicate asset path: {relative.as_posix()}"
            )
        paths.append(path)
    return tuple(paths)


def _dependency_conflicts(source_id: str, sources: dict[str, Any]) -> list[str]:
    conflicts: list[str] = []
    for other_id, item in sources.items():
        if other_id == source_id or not isinstance(item, dict):
            continue
        for field in _DEPENDENCY_FIELDS:
            if item.get(field) == source_id:
                conflicts.append(f"{other_id}:{field}")
    return sorted(conflicts)


def _shared_asset_conflicts(
    source_id: str,
    assets: tuple[Path, ...],
    sources: dict[str, Any],
    repo_root: Path,
) -> list[str]:
    targets = set(assets)
    if not targets:
        return []
    conflicts: list[str] = []
    for other_id, item in sources.items():
        if other_id == source_id or not isinstance(item, dict):
            continue
        try:
            other_assets = _registered_assets(item, repo_root)
        except SourceRemovalError as exc:
            raise SourceRemovalError(
                f"cannot validate assets for another source {other_id}: {exc}"
            ) from exc
        for shared in sorted(targets.intersection(other_assets)):
            conflicts.append(f"{other_id}:{shared.relative_to(repo_root).as_posix()}")
    return sorted(conflicts)


def plan_source_removal(
    source_id: str,
    *,
    repo_root: Path = REPO_ROOT,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    manifest_path = manifest_path or repo_root / "meta" / "manifest.json"
    manifest = load_manifest(manifest_path)
    record = manifest["sources"].get(source_id)
    if not isinstance(record, dict):
        raise SourceRemovalError(f"source does not exist: {source_id}")
    if record.get("ingest_status") != "ready":
        raise SourceRemovalError(f"source is not ready: {source_id}")

    relative_output = _canonical_relative_path(record.get("output_path"), "manifest output_path")
    output_path = _resolve_under(
        repo_root,
        relative_output,
        tuple(_SOURCE_ROOTS.values()),
        "manifest output_path",
    )
    if output_path.suffix.lower() != ".md":
        raise SourceRemovalError("manifest output_path must identify a Markdown file")
    if not output_path.is_file():
        raise SourceRemovalError(f"standardized source does not exist: {relative_output.as_posix()}")
    try:
        metadata, _ = parse_frontmatter(output_path)
    except (OSError, UnicodeError, ValueError) as exc:
        raise SourceRemovalError(f"cannot parse standardized source: {relative_output.as_posix()}") from exc
    if metadata.get("id") != source_id:
        raise SourceRemovalError(f"standardized source ID does not match: {relative_output.as_posix()}")
    scope = next(
        name for name, root in _SOURCE_ROOTS.items() if relative_output.is_relative_to(root)
    )
    if metadata.get("type") != scope:
        raise SourceRemovalError(
            f"standardized source type does not match its output root: {relative_output.as_posix()}"
        )
    if scope in {"note", "article"} and (
        metadata.get("layer") != scope or record.get("layer") != scope
    ):
        raise SourceRemovalError(
            f"standardized source layer does not match its output root: {relative_output.as_posix()}"
        )

    dependencies = _dependency_conflicts(source_id, manifest["sources"])
    if dependencies:
        raise SourceRemovalError(f"source has manifest dependents: {', '.join(dependencies)}")

    assets = _registered_assets(record, repo_root)
    asset_records = record.get("assets") or []
    for asset, asset_record in zip(assets, asset_records, strict=True):
        if not asset.is_file():
            raise SourceRemovalError(
                f"registered asset does not exist: {asset.relative_to(repo_root).as_posix()}"
            )
        expected_hash = asset_record.get("asset_hash")
        if not isinstance(expected_hash, str) or sha256_file(asset) != expected_hash:
            raise SourceRemovalError(
                f"registered asset hash does not match: {asset.relative_to(repo_root).as_posix()}"
            )
    shared_assets = _shared_asset_conflicts(source_id, assets, manifest["sources"], repo_root)
    if shared_assets:
        raise SourceRemovalError(f"source has shared assets: {', '.join(shared_assets)}")

    return {
        "source_id": source_id,
        "output_path": relative_output.as_posix(),
        "asset_paths": tuple(path.relative_to(repo_root).as_posix() for path in assets),
        "manifest": manifest,
        "manifest_path": manifest_path,
        "repo_root": repo_root,
        "resolved_output_path": output_path,
        "resolved_asset_paths": assets,
    }


def remove_source(
    source_id: str,
    *,
    repo_root: Path = REPO_ROOT,
    manifest_path: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    plan = plan_source_removal(source_id, repo_root=repo_root, manifest_path=manifest_path)
    result = {
        "source_id": source_id,
        "output_path": plan["output_path"],
        "asset_paths": plan["asset_paths"],
        "assets": len(plan["asset_paths"]),
        "removed": not dry_run,
    }
    if dry_run:
        return result

    output_path = plan["resolved_output_path"]
    asset_paths = plan["resolved_asset_paths"]
    tracker = SourceChangeTracker.for_output(output_path)
    tracker.observe(output_path)
    for asset_path in asset_paths:
        tracker.observe(asset_path)

    for asset_path in asset_paths:
        asset_path.unlink()
    output_path.unlink()
    plan["manifest"]["sources"].pop(source_id)
    save_manifest(plan["manifest"], plan["manifest_path"])
    tracker.append("remove-source")
    return result


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
