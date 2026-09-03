"""提供词法索引构建器和运行时共用的 generation 验证能力。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.retrieval.contracts import (
    CHUNK_POLICY_VERSION,
    ESTIMATOR_VERSION,
    LEXICAL_SCHEMA_VERSION,
    PROJECTION_SCHEMA_VERSION,
    QUERY_POLICY_VERSION,
    RANKING_POLICY_VERSION,
    SUPPORTED_SCOPES,
)


SQLITE_USER_VERSION = 1
SOURCE_DIGEST_PATTERN = re.compile(r"^sha256:(?P<hex>[0-9a-f]{64})$")
SCOPES = frozenset(SUPPORTED_SCOPES)
GENERATION_MANIFEST_KEYS = frozenset(
    {
        "generation",
        "source_digest",
        "database_sha256",
        "built_at",
        "source_count",
        "item_count",
        "item_counts",
        "schema_version",
        "projection_schema_version",
        "chunk_policy_version",
        "query_policy_version",
        "ranking_policy_version",
        "estimator_version",
    }
)


class LexicalBuildError(ValueError):
    """当投影 item 无法组成内部一致的数据库时抛出。"""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def generation_id(source_digest: str) -> str:
    match = SOURCE_DIGEST_PATTERN.fullmatch(source_digest)
    if match is None:
        raise LexicalBuildError("source digest has an invalid format")
    return f"gen_{match.group('hex')[:20]}"


def open_immutable_database(
    path: Path, *, check_same_thread: bool = True
) -> sqlite3.Connection:
    resolved = path.resolve(strict=True)
    uri = f"{resolved.as_uri()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True, check_same_thread=check_same_thread)
    connection.execute("PRAGMA query_only=ON")
    return connection


def validate_immutable_database(path: Path, expected_item_count: int) -> None:
    connection = open_immutable_database(path)
    try:
        if connection.execute("PRAGMA user_version").fetchone()[0] != SQLITE_USER_VERSION:
            raise LexicalBuildError("SQLite user_version mismatch")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise LexicalBuildError("immutable SQLite integrity_check failed")
        if connection.execute("SELECT count(*) FROM items").fetchone()[0] != expected_item_count:
            raise LexicalBuildError("immutable item count mismatch")
        if connection.execute("SELECT count(*) FROM items_fts").fetchone()[0] != expected_item_count:
            raise LexicalBuildError("immutable FTS count mismatch")
        row = connection.execute(
            "SELECT i.item_id FROM items_fts JOIN items AS i ON i.rowid=items_fts.rowid LIMIT 1"
        ).fetchone()
        if expected_item_count and row is None:
            raise LexicalBuildError("immutable query/read smoke failed")
    finally:
        connection.close()


def _database_scope_counts(path: Path) -> dict[str, int]:
    connection = open_immutable_database(path)
    try:
        counts = dict(connection.execute("SELECT scope, count(*) FROM items GROUP BY scope"))
    finally:
        connection.close()
    return {scope: int(counts.get(scope, 0)) for scope in sorted(SCOPES)}


def validate_built_at(value: Any) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise LexicalBuildError("generation built_at is invalid")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise LexicalBuildError("generation built_at is invalid") from exc
    if parsed.tzinfo != timezone.utc:
        raise LexicalBuildError("generation built_at is not UTC")
    return value


def _load_generation_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LexicalBuildError(f"cannot read generation manifest: {path}") from exc
    if not isinstance(value, dict) or set(value) != GENERATION_MANIFEST_KEYS:
        raise LexicalBuildError("generation manifest schema mismatch")
    return value


def validate_generation_artifact(
    generation_path: Path, *, expected_generation: str | None = None
) -> dict[str, Any]:
    """在不可变运行时打开 generation 前验证已封闭的索引产物。"""

    if generation_path.is_symlink() or not generation_path.is_dir():
        raise LexicalBuildError("generation path is not a real directory")
    entries = {path.name for path in generation_path.iterdir()}
    if entries != {"corpus.sqlite", "generation.json"}:
        raise LexicalBuildError("generation directory contains unexpected files")
    manifest = _load_generation_manifest(generation_path / "generation.json")
    source_digest = manifest.get("source_digest")
    digest_generation = generation_id(source_digest) if isinstance(source_digest, str) else None
    path_generation = expected_generation or generation_path.name
    if digest_generation != path_generation:
        raise LexicalBuildError("generation/source digest identity mismatch")
    fixed_versions = {
        "schema_version": LEXICAL_SCHEMA_VERSION,
        "projection_schema_version": PROJECTION_SCHEMA_VERSION,
        "chunk_policy_version": CHUNK_POLICY_VERSION,
        "query_policy_version": QUERY_POLICY_VERSION,
        "ranking_policy_version": RANKING_POLICY_VERSION,
        "estimator_version": ESTIMATOR_VERSION,
    }
    for key, expected in fixed_versions.items():
        if manifest.get(key) != expected:
            raise LexicalBuildError(f"generation {key} mismatch")
    if manifest.get("generation") != path_generation:
        raise LexicalBuildError("generation manifest identity mismatch")
    validate_built_at(manifest.get("built_at"))
    source_count = manifest.get("source_count")
    item_count = manifest.get("item_count")
    item_counts = manifest.get("item_counts")
    if not isinstance(source_count, int) or source_count < 1:
        raise LexicalBuildError("generation source count is invalid")
    if not isinstance(item_count, int) or item_count < 1:
        raise LexicalBuildError("generation item count is invalid")
    if (
        not isinstance(item_counts, dict)
        or set(item_counts) != SCOPES
        or any(not isinstance(value, int) or value < 0 for value in item_counts.values())
        or sum(item_counts.values()) != item_count
    ):
        raise LexicalBuildError("generation item counts are invalid")
    database_sha256 = manifest.get("database_sha256")
    if (
        not isinstance(database_sha256, str)
        or not SOURCE_DIGEST_PATTERN.fullmatch(database_sha256)
    ):
        raise LexicalBuildError("generation database hash is invalid")
    database_path = generation_path / "corpus.sqlite"
    if f"sha256:{sha256_file(database_path)}" != database_sha256:
        raise LexicalBuildError("generation database hash mismatch")
    validate_immutable_database(database_path, item_count)
    if _database_scope_counts(database_path) != item_counts:
        raise LexicalBuildError("generation database scope counts mismatch")
    return manifest
