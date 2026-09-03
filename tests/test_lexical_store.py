from __future__ import annotations

import hashlib
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.retrieval.build_lexical_index import (  # noqa: E402
    build_generation,
    publish_generation,
)
from src.retrieval.contracts import (  # noqa: E402
    Item,
    ItemRelations,
    ProjectionResult,
    SourceSnapshot,
)
from src.retrieval.lexical_store import (  # noqa: E402
    BudgetExceededError,
    GenerationMismatchError,
    IndexUnavailableError,
    InvalidRequestError,
    ItemNotFoundError,
    LexicalStore,
    SNIPPET_MAX_BYTES,
    SNIPPET_MAX_TOKENS,
)
from src.retrieval.project_items import stable_item_id  # noqa: E402
from src.retrieval.text import estimate_evidence_tokens  # noqa: E402


def make_item(
    name: str,
    scope: str,
    body: str,
    *,
    title: str | None = None,
    role: str | None = None,
    relations: ItemRelations = ItemRelations(),
) -> Item:
    source_id = f"source-{name}"
    source_path = f"sources/{scope}s/{source_id}.md"
    identity_locator = f"{scope}:{source_id}/{name}"
    return Item(
        item_id=stable_item_id(source_path, identity_locator, role or "heading", 1),
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
        relations=relations,
    )


def projection(digest_character: str, *items: Item) -> ProjectionResult:
    sources = tuple(
        SourceSnapshot(item.source_path, "a" * 64, item.source_id, item.scope, 1)
        for item in items
    )
    return ProjectionResult(items, sources, "sha256:" + digest_character * 64)


def published_store(root: Path, digest_character: str, *items: Item) -> LexicalStore:
    generation = build_generation(projection(digest_character, *items), root)
    publish_generation(root, generation.generation)
    return LexicalStore.open_current(root)


class TestLexicalStore:
    def test_single_query_bm25_scope_and_canonical_snippet(self) -> None:
        title_hit = make_item("title", "note", "neutral body text", title="quasar guide")
        body_hit = make_item("body", "note", "quasar body text", title="neutral guide")
        article_hit = make_item("article", "article", "quasar article", title="Other")
        with tempfile.TemporaryDirectory() as temp:
            with published_store(Path(temp), "b", title_hit, body_hit, article_hit) as store:
                response = store.search_lex(["quasar"], scopes=["note"], limit=3)

        assert [result.item_id for result in response.results] == \
            [title_hit.item_id, body_hit.item_id]
        assert [result.rank for result in response.results] == [1, 2]
        assert all(result.source_type == "note" for result in response.results)
        assert response.results[0].snippet in title_hit.body

    def test_weighted_rrf_merges_duplicate_items_and_is_stable(self) -> None:
        first = "itm_" + "a" * 32
        both = "itm_" + "b" * 32
        second = "itm_" + "c" * 32
        rankings = ({first: 1, both: 2}, {both: 1, second: 2})

        assert LexicalStore._fuse(rankings, 3) == (both, first, second)
        assert LexicalStore._fuse(rankings, 3) == LexicalStore._fuse(rankings, 3)

    def test_multi_query_search_returns_each_item_once(self) -> None:
        both = make_item("both", "note", "alpha beta", title="Both")
        alpha = make_item("alpha", "note", "alpha only", title="Alpha")
        beta = make_item("beta", "note", "beta only", title="Beta")
        with tempfile.TemporaryDirectory() as temp:
            with published_store(Path(temp), "c", both, alpha, beta) as store:
                response = store.search_lex(["alpha", "beta"], limit=3)

        ids = [result.item_id for result in response.results]
        assert ids[0] == both.item_id
        assert len(ids) == len(set(ids))

    def test_snippet_selects_matching_block_and_obeys_both_caps(self) -> None:
        body = (
            "preface " * 120
            + "\n\n## Target\nneedle is kept in this canonical block.\n\n"
            + "suffix " * 120
        )
        item = make_item("snippet", "note", body, title="Long note")
        with tempfile.TemporaryDirectory() as temp:
            with published_store(Path(temp), "d", item) as store:
                result = store.search_lex(["needle"], limit=1).results[0]

        assert "needle" in result.snippet
        assert result.snippet in body
        assert estimate_evidence_tokens(result.snippet) <= SNIPPET_MAX_TOKENS
        assert len(result.snippet.encode("utf-8")) <= SNIPPET_MAX_BYTES
        assert result.truncated_before
        assert result.truncated_after

    def test_snippet_uses_minimum_span_when_an_anchor_repeats(self) -> None:
        body = "alpha " + "filler " * 180 + "alpha beta target"
        item = make_item("span", "note", body, title="Repeated anchor")
        with tempfile.TemporaryDirectory() as temp:
            with published_store(Path(temp), "7", item) as store:
                result = store.search_lex(["alpha beta"], limit=1).results[0]

        assert "alpha beta target" in result.snippet
        assert estimate_evidence_tokens(result.snippet) <= SNIPPET_MAX_TOKENS

    def test_read_returns_relations_and_safe_prefix_truncation(self) -> None:
        related = make_item("related", "conversation", "related answer", role="assistant")
        body = "First sentence.\n" + "Second sentence with detail. " * 50
        primary = make_item("primary", "conversation", body, role="human")
        primary = replace(
            primary,
            relations=ItemRelations(counterpart_item_ids=(related.item_id,)),
        )
        related = replace(
            related,
            relations=ItemRelations(counterpart_item_ids=(primary.item_id,)),
        )
        with tempfile.TemporaryDirectory() as temp:
            with published_store(Path(temp), "e", primary, related) as store:
                result = store.read_item(primary.item_id, store.generation, 20)

        assert body.startswith(result.body)
        assert result.estimated_evidence_tokens <= 20
        assert result.is_truncated
        assert result.relations.counterpart_item_ids == (related.item_id,)
        assert result.relations.next_part_id is None

    def test_exact_read_preserves_canonical_body(self) -> None:
        body = "## Heading\n\nCanonical **Markdown** body."
        item = make_item("exact", "article", body, title="Heading")
        with tempfile.TemporaryDirectory() as temp:
            with published_store(Path(temp), "f", item) as store:
                result = store.read_item(item.item_id, store.generation)

        assert result.body == body
        assert not result.is_truncated

    def test_request_validation_and_stable_error_codes(self) -> None:
        item = make_item("validation", "note", "alpha", title="Alpha")
        with tempfile.TemporaryDirectory() as temp:
            with published_store(Path(temp), "1", item) as store:
                invalid_calls = (
                    lambda: store.search_lex([]),
                    lambda: store.search_lex(["alpha", " ALPHA "]),
                    lambda: store.search_lex(["alpha"], scopes=["note", "note"]),
                    lambda: store.search_lex(["alpha"], limit=True),
                )
                for call in invalid_calls:
                    with pytest.raises(InvalidRequestError) as caught:
                        call()
                    assert caught.value.code == "invalid_request"

    def test_generation_mismatch_precedes_lookup_and_missing_item_is_distinct(self) -> None:
        item = make_item("generation", "note", "alpha", title="Alpha")
        missing = stable_item_id("sources/notes/missing.md", "missing", "heading", 1)
        with tempfile.TemporaryDirectory() as temp:
            with published_store(Path(temp), "2", item) as store:
                with pytest.raises(GenerationMismatchError) as mismatch:
                    store.read_item(missing, "gen_" + "0" * 20)
                with pytest.raises(ItemNotFoundError) as not_found:
                    store.read_item(missing, store.generation)

        assert mismatch.value.code == "generation_mismatch"
        assert not_found.value.code == "item_not_found"

    def test_store_pins_generation_when_current_changes(self) -> None:
        old_item = make_item("old", "note", "old alpha", title="Old")
        new_item = make_item("new", "note", "new beta", title="New")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = published_store(root, "3", old_item)
            try:
                old_generation = store.generation
                newer = build_generation(projection("4", new_item), root)
                publish_generation(root, newer.generation)
                result = store.read_item(old_item.item_id, old_generation)
                with pytest.raises(GenerationMismatchError):
                    store.read_item(new_item.item_id, newer.generation)
            finally:
                store.close()

        assert result.body == old_item.body

    def test_invalid_current_and_impossibly_small_budget_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "generations").mkdir()
            (root / "current").symlink_to(Path("generations") / ("gen_" + "0" * 20))
            with pytest.raises(IndexUnavailableError) as unavailable:
                LexicalStore.open_current(root)
        assert unavailable.value.code == "index_unavailable"

        item = make_item("budget", "note", "中", title="Budget")
        with tempfile.TemporaryDirectory() as temp:
            with published_store(Path(temp), "5", item) as store:
                with pytest.raises(BudgetExceededError) as budget:
                    store.read_item(item.item_id, store.generation, 1)
        assert budget.value.code == "budget_exceeded"

    def test_status_exposes_only_versioned_generation_metadata(self) -> None:
        item = make_item("status", "article", "status body", title="Status")
        with tempfile.TemporaryDirectory() as temp:
            with published_store(Path(temp), "6", item) as store:
                status = store.status()

        assert status.generation == "gen_" + "6" * 20
        assert status.item_counts == {"article": 1, "conversation": 0, "note": 0}
        assert not status.semantic_search
        assert status.supported_scopes == ("conversation", "note", "article")
