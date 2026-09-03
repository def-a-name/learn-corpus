from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.retrieval.build_lexical_index import (  # noqa: E402
    build_database,
    build_generation,
    publish_generation,
)
from src.retrieval.contracts import (  # noqa: E402
    CHUNK_POLICY_VERSION,
    Item,
    ItemRelations,
    ProjectionResult,
    SourceSnapshot,
)
from src.retrieval.generation import (  # noqa: E402
    LexicalBuildError,
    generation_id,
    open_immutable_database,
)
from src.retrieval.lexical_query import compile_lexical_query  # noqa: E402
from src.retrieval.project_items import (  # noqa: E402
    stable_item_id,
)
from src.retrieval.text import estimate_evidence_tokens  # noqa: E402


def make_item(
    name: str,
    scope: str,
    body: str,
    *,
    title: str | None = None,
    role: str | None = None,
    source_id: str | None = None,
) -> Item:
    source_id = source_id or f"source-{scope}"
    source_path = f"sources/{scope}s/{source_id}.md"
    identity_locator = f"{scope}:{source_id}/{name}"
    item_id = stable_item_id(source_path, identity_locator, role or "heading", 1)
    return Item(
        item_id=item_id,
        scope=scope,
        title=title,
        source_path=source_path,
        source_id=source_id,
        locator=f"{identity_locator}/part:1",
        locator_with_lines=f"{identity_locator}/part:1@L1-L2",
        evidence_role="user_statement" if role == "human" else None,
        provider="codex" if scope == "conversation" else None,
        session_id="session-1" if scope == "conversation" else None,
        turn_index=1 if scope == "conversation" else None,
        role=role,
        heading_path=(title,) if title is not None else None,
        occurrence=1 if title is not None else None,
        part=1,
        body=body,
        body_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        token_estimate=estimate_evidence_tokens(body),
    )


def fixture_projection(*items: Item) -> ProjectionResult:
    sources = tuple(
        SourceSnapshot(item.source_path, "a" * 64, item.source_id, item.scope, 1)
        for item in items
    )
    return ProjectionResult(tuple(items), sources, "sha256:" + "b" * 64)


class TestLexicalIndexBuilder:
    def test_builds_canonical_items_and_search_only_fts(self) -> None:
        items = (
            make_item("star-map", "note", "虚构星图传感器的校准周期是七天。", title="合成星图记录"),
            make_item("ranking", "article", "Synthetic retrieval ranking uses BM25.", title="Synthetic ranking"),
            make_item("human", "conversation", "orion-7 虚构探针出现校准异常。", role="human"),
        )
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "corpus.sqlite"
            result = build_database(fixture_projection(*items), database)
            connection = open_immutable_database(database)
            try:
                canonical = connection.execute(
                    "SELECT title, body FROM items WHERE item_id=?", (items[0].item_id,)
                ).fetchone()
                fts_content = connection.execute(
                    "SELECT title, body FROM items_fts WHERE rowid=(SELECT rowid FROM items WHERE item_id=?)",
                    (items[0].item_id,),
                ).fetchone()
                cjk_match = connection.execute(
                    "SELECT i.item_id FROM items_fts JOIN items i ON i.rowid=items_fts.rowid "
                    "WHERE items_fts MATCH ?",
                    (compile_lexical_query("星图记录").match_expression,),
                ).fetchall()
                identifier_match = connection.execute(
                    "SELECT i.item_id FROM items_fts JOIN items i ON i.rowid=items_fts.rowid "
                    "WHERE items_fts MATCH ?",
                    (compile_lexical_query("orion-7 异常").match_expression,),
                ).fetchall()
            finally:
                connection.close()

        assert canonical == ("合成星图记录", "虚构星图传感器的校准周期是七天。")
        assert fts_content == (None, None)
        assert cjk_match == [(items[0].item_id,)]
        assert identifier_match == [(items[2].item_id,)]
        assert result.item_count == 3
        assert result.item_counts == {"article": 1, "conversation": 1, "note": 1}
        assert re.search(r"^sha256:[0-9a-f]{64}$", result.database_sha256)

    def test_scope_filter_is_applied_before_limit(self) -> None:
        conversations = tuple(
            make_item(
                f"conversation-{index}",
                "conversation",
                "星图 星图 星图 校准",
                role="human",
                source_id=f"conversation-{index}",
            )
            for index in range(25)
        )
        note = make_item("note", "note", "星图 校准", title="Synthetic calibration", source_id="note-one")
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "corpus.sqlite"
            build_database(fixture_projection(*conversations, note), database)
            connection = open_immutable_database(database)
            try:
                rows = connection.execute(
                    "SELECT i.item_id FROM items_fts JOIN items i ON i.rowid=items_fts.rowid "
                    "WHERE items_fts MATCH ? AND i.scope IN (?) "
                    "ORDER BY bm25(items_fts, 4.0, 1.0), i.item_id LIMIT 20",
                    (compile_lexical_query("星图 校准").match_expression, "note"),
                ).fetchall()
            finally:
                connection.close()
        assert rows == [(note.item_id,)]

    def test_relations_are_serialized_canonically(self) -> None:
        human = make_item("human", "conversation", "question", role="human")
        assistant = make_item("assistant", "conversation", "answer", role="assistant")
        human = replace(
            human,
            relations=ItemRelations(counterpart_item_ids=(assistant.item_id,)),
        )
        assistant = replace(
            assistant,
            relations=ItemRelations(counterpart_item_ids=(human.item_id,)),
        )
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "corpus.sqlite"
            build_database(fixture_projection(human, assistant), database)
            connection = open_immutable_database(database)
            try:
                encoded = connection.execute(
                    "SELECT relations_json FROM items WHERE item_id=?", (human.item_id,)
                ).fetchone()[0]
            finally:
                connection.close()
        assert json.loads(encoded) == \
            {
                "counterpart_item_ids": [assistant.item_id],
                "next_part_id": None,
                "previous_part_id": None,
            }

    def test_invalid_hash_relation_and_existing_target_fail_closed(self) -> None:
        item = make_item("note", "note", "body", title="Title")
        bad_hash = replace(item, body_sha256="0" * 64)
        missing_relation = replace(
            item,
            relations=ItemRelations(previous_part_id="itm_" + "a" * 32),
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name, projection, message in (
                ("hash.sqlite", fixture_projection(bad_hash), "body hash mismatch"),
                ("relation.sqlite", fixture_projection(missing_relation), "previous part relation"),
            ):
                with pytest.raises(LexicalBuildError, match=message):
                    build_database(projection, root / name)
                assert not (root / name).exists()
            existing = root / "existing.sqlite"
            existing.write_bytes(b"keep")
            with pytest.raises(LexicalBuildError, match="already exists"):
                build_database(fixture_projection(item), existing)
            assert existing.read_bytes() == b"keep"

    def test_immutable_connection_rejects_writes(self) -> None:
        item = make_item("note", "note", "body", title="Title")
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "corpus.sqlite"
            build_database(fixture_projection(item), database)
            connection = open_immutable_database(database)
            try:
                with pytest.raises(sqlite3.OperationalError):
                    connection.execute("DELETE FROM items")
            finally:
                connection.close()

    def test_content_addressed_generation_is_idempotently_reused(self) -> None:
        item = make_item("note", "note", "body", title="Title")
        projection = fixture_projection(item)
        expected_generation = "gen_" + "b" * 20
        with tempfile.TemporaryDirectory() as temp:
            retrieval_root = Path(temp) / "retrieval"
            first = build_generation(
                projection,
                retrieval_root,
                built_at="2026-09-02T01:02:03Z",
            )
            manifest_before = first.manifest_path.read_bytes()
            second = build_generation(
                projection,
                retrieval_root,
                built_at="2026-09-02T09:09:09Z",
            )
            manifest_after = second.manifest_path.read_bytes()
            manifest = json.loads(manifest_after.decode("utf-8"))

        assert generation_id(projection.source_digest) == expected_generation
        assert first.generation == expected_generation
        assert not first.reused
        assert second.reused
        assert second.built_at == "2026-09-02T01:02:03Z"
        assert manifest_before == manifest_after
        assert manifest["chunk_policy_version"] == CHUNK_POLICY_VERSION
        assert manifest["ranking_policy_version"] == "bm25-rrf-v1"
        assert manifest["source_digest"] == projection.source_digest
        assert set(manifest) == \
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

    def test_existing_generation_with_changed_database_fails_closed(self) -> None:
        item = make_item("note", "note", "body", title="Title")
        projection = fixture_projection(item)
        with tempfile.TemporaryDirectory() as temp:
            retrieval_root = Path(temp) / "retrieval"
            result = build_generation(projection, retrieval_root)
            with result.database_path.open("ab") as handle:
                handle.write(b"changed")
            with pytest.raises(LexicalBuildError, match="database hash mismatch"):
                build_generation(projection, retrieval_root)

    def test_publish_preserves_previous_and_same_generation_is_noop(self) -> None:
        first_projection = fixture_projection(
            make_item("one", "note", "first body", title="First", source_id="first")
        )
        second_projection = replace(
            fixture_projection(
                make_item("two", "note", "second body", title="Second", source_id="second")
            ),
            source_digest="sha256:" + "c" * 64,
        )
        with tempfile.TemporaryDirectory() as temp:
            retrieval_root = Path(temp) / "retrieval"
            first = build_generation(first_projection, retrieval_root)
            second = build_generation(second_projection, retrieval_root)
            initial = publish_generation(retrieval_root, first.generation)
            switched = publish_generation(retrieval_root, second.generation)
            repeated = publish_generation(retrieval_root, second.generation)
            current_target = (retrieval_root / "current").readlink()
            previous_target = (retrieval_root / "previous").readlink()

        assert initial.changed
        assert initial.previous_generation is None
        assert switched.changed
        assert switched.previous_generation == first.generation
        assert not repeated.changed
        assert current_target == Path("generations") / second.generation
        assert previous_target == Path("generations") / first.generation
