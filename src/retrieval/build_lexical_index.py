"""从投影 item 构建并验证一个不可变 SQLite FTS5 数据库。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from src.retrieval.contracts import (
    CHUNK_POLICY_VERSION,
    ESTIMATOR_VERSION,
    LEXICAL_SCHEMA_VERSION,
    PROJECTION_SCHEMA_VERSION,
    QUERY_POLICY_VERSION,
    RANKING_POLICY_VERSION,
    Item,
    ProjectionResult,
)
from src.retrieval.index_artifact import (
    SCOPES,
    SQLITE_USER_VERSION,
    LexicalBuildError,
    index_id_from_digest,
    sha256_file,
    validate_built_at,
    validate_index_artifact,
    validate_immutable_database,
)
from src.retrieval.project_items import project_corpus
from src.retrieval.text import estimate_evidence_tokens, normalize_index_text
from src.corpus.paths import validate_data_path, REPO_ROOT


_ITEM_ID = re.compile(r"^itm_[a-z2-7]{32}$")

_CREATE_ITEMS = """
CREATE TABLE items (
    rowid              INTEGER PRIMARY KEY,
    item_id            TEXT NOT NULL UNIQUE,
    scope              TEXT NOT NULL
                       CHECK (scope IN ('conversation', 'note', 'article')),
    title              TEXT,
    source_title       TEXT,
    source_path        TEXT NOT NULL,
    source_id          TEXT NOT NULL,
    locator            TEXT NOT NULL,
    locator_with_lines TEXT,
    evidence_role      TEXT,
    provider           TEXT,
    session_id         TEXT,
    turn_index         INTEGER,
    role               TEXT,
    heading_path_json  TEXT,
    occurrence         INTEGER,
    part               INTEGER NOT NULL CHECK (part >= 1),
    body               TEXT NOT NULL,
    body_sha256        TEXT NOT NULL,
    token_estimate     INTEGER NOT NULL CHECK (token_estimate >= 1),
    relations_json     TEXT NOT NULL,
    UNIQUE (source_path, locator)
)
"""
_CREATE_FTS = """
CREATE VIRTUAL TABLE items_fts USING fts5(
    title,
    body,
    content='',
    tokenize='porter unicode61 remove_diacritics 2'
)
"""
_INSERT_ITEM = """
INSERT INTO items (
    rowid, item_id, scope, title, source_title, source_path, source_id, locator,
    locator_with_lines, evidence_role, provider, session_id, turn_index,
    role, heading_path_json, occurrence, part, body, body_sha256,
    token_estimate, relations_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


@dataclass(frozen=True)
class DatabaseBuildResult:
    database_path: Path
    database_sha256: str
    item_count: int
    item_counts: dict[str, int]


@dataclass(frozen=True)
class IndexBuildResult:
    index_id: str
    index_path: Path
    database_path: Path
    manifest_path: Path
    database_sha256: str
    source_digest: str
    source_count: int
    item_count: int
    item_counts: dict[str, int]
    built_at: str
    reused: bool


@dataclass(frozen=True)
class PublicationResult:
    current_index_id: str
    previous_index_id: str | None
    changed: bool


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _logical_locator(item: Item) -> str:
    suffix = f"/part:{item.part}"
    if not item.locator.endswith(suffix):
        raise LexicalBuildError(f"item locator/part mismatch: {item.item_id}")
    return item.locator[: -len(suffix)]


def _validate_items(items: Sequence[Item]) -> tuple[Item, ...]:
    if not items:
        raise LexicalBuildError("projection contains no items")
    by_id: dict[str, Item] = {}
    locators: set[tuple[str, str]] = set()
    groups: dict[tuple[str, str], list[Item]] = {}
    for item in items:
        if not _ITEM_ID.fullmatch(item.item_id):
            raise LexicalBuildError(f"invalid item ID: {item.item_id}")
        if item.item_id in by_id:
            raise LexicalBuildError(f"duplicate item ID: {item.item_id}")
        by_id[item.item_id] = item
        if item.scope not in SCOPES:
            raise LexicalBuildError(f"invalid item scope: {item.item_id}")
        locator_key = (item.source_path, item.locator)
        if locator_key in locators:
            raise LexicalBuildError(f"duplicate source locator: {item.item_id}")
        locators.add(locator_key)
        if hashlib.sha256(item.body.encode("utf-8")).hexdigest() != item.body_sha256:
            raise LexicalBuildError(f"item body hash mismatch: {item.item_id}")
        if estimate_evidence_tokens(item.body) != item.token_estimate:
            raise LexicalBuildError(f"item token estimate mismatch: {item.item_id}")
        logical_locator = _logical_locator(item)
        groups.setdefault((item.source_path, logical_locator), []).append(item)
        if item.scope == "conversation":
            if (
                item.role not in {"human", "assistant"}
                or not isinstance(item.source_title, str) or not item.source_title.strip()
                or item.heading_path is not None
            ):
                raise LexicalBuildError(f"conversation item has invalid metadata: {item.item_id}")
        elif (
            item.role is not None or item.relations.counterpart_item_ids
            or not isinstance(item.source_title, str) or not item.source_title.strip()
            or item.heading_path is None
        ):
            raise LexicalBuildError(f"document item has conversation metadata: {item.item_id}")

    for group_items in groups.values():
        ordered = sorted(group_items, key=lambda value: value.part)
        if [item.part for item in ordered] != list(range(1, len(ordered) + 1)):
            raise LexicalBuildError(f"item parts are not continuous: {ordered[0].item_id}")
        for index, item in enumerate(ordered):
            expected_previous = ordered[index - 1].item_id if index else None
            expected_next = ordered[index + 1].item_id if index + 1 < len(ordered) else None
            if item.relations.previous_part_id != expected_previous:
                raise LexicalBuildError(f"invalid previous part relation: {item.item_id}")
            if item.relations.next_part_id != expected_next:
                raise LexicalBuildError(f"invalid next part relation: {item.item_id}")

    for item in items:
        for counterpart_id in item.relations.counterpart_item_ids:
            counterpart = by_id.get(counterpart_id)
            if counterpart is None:
                raise LexicalBuildError(f"missing counterpart item: {item.item_id}")
            if item.item_id not in counterpart.relations.counterpart_item_ids:
                raise LexicalBuildError(f"counterpart relation is not reciprocal: {item.item_id}")
            if counterpart.scope != "conversation" or counterpart.role == item.role:
                raise LexicalBuildError(f"invalid counterpart role: {item.item_id}")
    return tuple(sorted(items, key=lambda value: value.item_id))


def _relations_json(item: Item) -> str:
    return _canonical_json(
        {
            "counterpart_item_ids": list(item.relations.counterpart_item_ids),
            "next_part_id": item.relations.next_part_id,
            "previous_part_id": item.relations.previous_part_id,
        }
    )


def _heading_path_json(item: Item) -> str | None:
    return _canonical_json(list(item.heading_path)) if item.heading_path is not None else None


def _configure_connection(connection: sqlite3.Connection) -> None:
    journal_mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
    if str(journal_mode).lower() != "delete":
        raise LexicalBuildError("SQLite did not enter DELETE journal mode")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA foreign_keys=ON")


def _write_database(path: Path, items: Sequence[Item]) -> None:
    connection = sqlite3.connect(path)
    try:
        _configure_connection(connection)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(f"PRAGMA user_version={SQLITE_USER_VERSION}")
        connection.execute(_CREATE_ITEMS)
        connection.execute(_CREATE_FTS)
        for rowid, item in enumerate(items, start=1):
            connection.execute(
                _INSERT_ITEM,
                (
                    rowid,
                    item.item_id,
                    item.scope,
                    item.title,
                    item.source_title,
                    item.source_path,
                    item.source_id,
                    item.locator,
                    item.locator_with_lines,
                    item.evidence_role,
                    item.provider,
                    item.session_id,
                    item.turn_index,
                    item.role,
                    _heading_path_json(item),
                    item.occurrence,
                    item.part,
                    item.body,
                    item.body_sha256,
                    item.token_estimate,
                    _relations_json(item),
                ),
            )
            connection.execute(
                "INSERT INTO items_fts(rowid, title, body) VALUES (?, ?, ?)",
                (rowid, normalize_index_text(item.title), normalize_index_text(item.body)),
            )
        connection.commit()

        item_count = connection.execute("SELECT count(*) FROM items").fetchone()[0]
        fts_count = connection.execute("SELECT count(*) FROM items_fts").fetchone()[0]
        if item_count != len(items) or fts_count != len(items):
            raise LexicalBuildError("items/FTS row count mismatch")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise LexicalBuildError(f"SQLite integrity_check failed: {integrity}")
        connection.execute("INSERT INTO items_fts(items_fts) VALUES ('integrity-check')")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def build_database(projection: ProjectionResult, database_path: Path) -> DatabaseBuildResult:
    """在原本不存在的路径上原子创建已验证数据库。"""

    database_path = database_path.resolve()
    if database_path.exists():
        raise LexicalBuildError(f"database path already exists: {database_path}")
    if not database_path.parent.is_dir():
        raise LexicalBuildError(f"database parent does not exist: {database_path.parent}")
    items = _validate_items(projection.items)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{database_path.name}.", suffix=".tmp", dir=database_path.parent
    )
    os.close(file_descriptor)
    temporary_path = Path(temporary_name)
    try:
        _write_database(temporary_path, items)
        validate_immutable_database(temporary_path, len(items))
        os.replace(temporary_path, database_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    counts = Counter(item.scope for item in items)
    return DatabaseBuildResult(
        database_path=database_path,
        database_sha256=f"sha256:{sha256_file(database_path)}",
        item_count=len(items),
        item_counts={scope: counts.get(scope, 0) for scope in sorted(SCOPES)},
    )


def _manifest_payload(
    projection: ProjectionResult,
    index_id: str,
    database: DatabaseBuildResult,
    built_at: str,
) -> dict[str, Any]:
    return {
        "index_id": index_id,
        "source_digest": projection.source_digest,
        "database_sha256": database.database_sha256,
        "built_at": built_at,
        "source_count": len(projection.sources),
        "item_count": database.item_count,
        "item_counts": database.item_counts,
        "schema_version": LEXICAL_SCHEMA_VERSION,
        "projection_schema_version": PROJECTION_SCHEMA_VERSION,
        "chunk_policy_version": CHUNK_POLICY_VERSION,
        "query_policy_version": QUERY_POLICY_VERSION,
        "ranking_policy_version": RANKING_POLICY_VERSION,
        "estimator_version": ESTIMATOR_VERSION,
    }


def _result_from_existing(
    projection: ProjectionResult, index_path: Path
) -> IndexBuildResult:
    manifest_path = index_path / "index.json"
    database_path = index_path / "corpus.sqlite"
    manifest = validate_index_artifact(index_path)
    expected_versions = {
        "index_id": index_id_from_digest(projection.source_digest),
        "source_digest": projection.source_digest,
        "source_count": len(projection.sources),
        "item_count": len(projection.items),
        "item_counts": {
            scope: sum(item.scope == scope for item in projection.items)
            for scope in sorted(SCOPES)
        },
        "schema_version": LEXICAL_SCHEMA_VERSION,
        "projection_schema_version": PROJECTION_SCHEMA_VERSION,
        "chunk_policy_version": CHUNK_POLICY_VERSION,
        "query_policy_version": QUERY_POLICY_VERSION,
        "ranking_policy_version": RANKING_POLICY_VERSION,
        "estimator_version": ESTIMATOR_VERSION,
    }
    for key, expected in expected_versions.items():
        if manifest.get(key) != expected:
            raise LexicalBuildError(f"existing index {key} mismatch")
    return IndexBuildResult(
        index_id=manifest["index_id"],
        index_path=index_path,
        database_path=database_path,
        manifest_path=manifest_path,
        database_sha256=manifest["database_sha256"],
        source_digest=projection.source_digest,
        source_count=len(projection.sources),
        item_count=len(projection.items),
        item_counts=manifest["item_counts"],
        built_at=manifest["built_at"],
        reused=True,
    )


def build_index(
    projection: ProjectionResult,
    retrieval_root: Path,
    *,
    built_at: str | None = None,
) -> IndexBuildResult:
    """构建或验证一个按内容寻址的检索索引，不发布。"""

    index_id = index_id_from_digest(projection.source_digest)
    retrieval_root = retrieval_root.resolve()
    retrieval_root.mkdir(parents=True, exist_ok=True)
    index_path = retrieval_root / index_id
    if index_path.exists() or index_path.is_symlink():
        return _result_from_existing(projection, index_path)

    built_at = validate_built_at(built_at or _utc_now())
    staging_path = Path(tempfile.mkdtemp(prefix=f".{index_id}.", dir=retrieval_root))
    try:
        database = build_database(projection, staging_path / "corpus.sqlite")
        manifest = _manifest_payload(projection, index_id, database, built_at)
        (staging_path / "index.json").write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        validate_index_artifact(staging_path, expected_index_id=index_id)
        os.replace(staging_path, index_path)
    except Exception:
        if staging_path.exists():
            shutil.rmtree(staging_path)
        raise
    return replace(_result_from_existing(projection, index_path), reused=False)


def _read_index_link(retrieval_root: Path, name: str) -> str | None:
    path = retrieval_root / name
    if not os.path.lexists(path):
        return None
    if not path.is_symlink():
        raise LexicalBuildError(f"{name} is not a symlink")
    resolved = path.resolve(strict=True)
    try:
        relative = resolved.relative_to(retrieval_root)
    except ValueError as exc:
        raise LexicalBuildError(f"{name} escapes retrieval root") from exc
    if (
        len(relative.parts) != 1
        or re.fullmatch(r"idx_[0-9a-f]{20}", relative.name) is None
        or not resolved.is_dir()
    ):
        raise LexicalBuildError(f"{name} does not target one index")
    return relative.name


def _atomic_index_link(retrieval_root: Path, name: str, index_id: str) -> None:
    file_descriptor, temporary_name = tempfile.mkstemp(prefix=f".{name}.", dir=retrieval_root)
    os.close(file_descriptor)
    temporary_path = Path(temporary_name)
    temporary_path.unlink()
    try:
        temporary_path.symlink_to(Path(index_id))
        os.replace(temporary_path, retrieval_root / name)
    finally:
        if os.path.lexists(temporary_path):
            temporary_path.unlink()


def publish_index(retrieval_root: Path, index_id: str) -> PublicationResult:
    """原子切换 current，并将旧 current 保留为 previous。"""

    retrieval_root = retrieval_root.resolve(strict=True)
    target = retrieval_root / index_id
    manifest = validate_index_artifact(target)
    if manifest["index_id"] != index_id:
        raise LexicalBuildError("publish target index mismatch")
    old_current = _read_index_link(retrieval_root, "current")
    if old_current == index_id:
        return PublicationResult(index_id, _read_index_link(retrieval_root, "previous"), False)
    if os.path.lexists(retrieval_root / "previous") and not (retrieval_root / "previous").is_symlink():
        raise LexicalBuildError("previous is not a symlink")
    if old_current is not None:
        _atomic_index_link(retrieval_root, "previous", old_current)
    _atomic_index_link(retrieval_root, "current", index_id)
    return PublicationResult(index_id, old_current, True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build and optionally publish a lexical index.")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--retrieval-root", type=Path, help="Index directory (default: <repo-root>/meta/corpus)")
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    if getattr(args, "repo_root", None) is not None:
        validate_data_path(args.repo_root)
    repo_root = args.repo_root.resolve()
    retrieval_root = args.retrieval_root or (repo_root / "meta" / "corpus")
    projection = project_corpus(repo_root, args.manifest)
    index_result = build_index(projection, retrieval_root)
    publication = publish_index(retrieval_root, index_result.index_id) if args.publish else None
    print(
        json.dumps(
            {
                "index_id": index_result.index_id,
                "source_digest": index_result.source_digest,
                "database_sha256": index_result.database_sha256,
                "sources": index_result.source_count,
                "items": index_result.item_count,
                "item_counts": index_result.item_counts,
                "built_at": index_result.built_at,
                "reused": index_result.reused,
                "published": publication.changed if publication else False,
                "previous_index_id": (
                    publication.previous_index_id if publication is not None else None
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
