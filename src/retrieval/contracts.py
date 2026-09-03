"""定义检索投影、索引和读取边界共用的版本常量与数据结构。"""

from __future__ import annotations

from dataclasses import dataclass


PROJECTION_SCHEMA_VERSION = "item-projection-v1"
CHUNK_POLICY_VERSION = "evidence-chunk-v2"
LEXICAL_SCHEMA_VERSION = "lexical-schema-v1"
QUERY_POLICY_VERSION = "qmd-style-v1"
RANKING_POLICY_VERSION = "bm25-rrf-v1"
ESTIMATOR_VERSION = "evidence-estimator-v1"

SUPPORTED_SCOPES = ("conversation", "note", "article")


@dataclass(frozen=True)
class ItemRelations:
    counterpart_item_ids: tuple[str, ...] = ()
    previous_part_id: str | None = None
    next_part_id: str | None = None


@dataclass(frozen=True)
class Item:
    item_id: str
    scope: str
    title: str | None
    source_path: str
    source_id: str
    locator: str
    locator_with_lines: str | None
    evidence_role: str | None
    provider: str | None
    session_id: str | None
    turn_index: int | None
    role: str | None
    heading_path: tuple[str, ...] | None
    occurrence: int | None
    part: int
    body: str
    body_sha256: str
    token_estimate: int
    relations: ItemRelations = ItemRelations()


@dataclass(frozen=True)
class SourceSnapshot:
    source_path: str
    standardized_sha256: str
    source_id: str
    source_type: str
    importer_version: int


@dataclass(frozen=True)
class ProjectionResult:
    items: tuple[Item, ...]
    sources: tuple[SourceSnapshot, ...]
    source_digest: str


@dataclass(frozen=True)
class SearchResult:
    item_id: str
    rank: int
    title: str | None
    source_type: str
    path: str
    locator: str
    evidence_role: str | None
    provider: str | None
    session_id: str | None
    turn_index: int | None
    role: str | None
    part: int
    snippet: str
    truncated_before: bool
    truncated_after: bool


@dataclass(frozen=True)
class SearchResponse:
    generation: str
    results: tuple[SearchResult, ...]
    estimated_evidence_tokens: int
    estimator_version: str = ESTIMATOR_VERSION


@dataclass(frozen=True)
class ReadResult:
    generation: str
    item_id: str
    title: str | None
    source_type: str
    path: str
    locator: str
    evidence_role: str | None
    provider: str | None
    session_id: str | None
    turn_index: int | None
    role: str | None
    part: int
    body: str
    is_truncated: bool
    relations: ItemRelations
    estimated_evidence_tokens: int
    estimator_version: str = ESTIMATOR_VERSION


@dataclass(frozen=True)
class GenerationStatus:
    generation: str
    source_digest: str
    built_at: str
    item_counts: dict[str, int]
    schema_version: str
    projection_schema_version: str
    chunk_policy_version: str
    query_policy_version: str
    ranking_policy_version: str
    estimator_version: str
    semantic_search: bool = False
    supported_scopes: tuple[str, ...] = SUPPORTED_SCOPES
