from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.retrieval.contracts import (  # noqa: E402
    CHUNK_POLICY_VERSION,
    RANKING_POLICY_VERSION,
)
from src.retrieval.project_items import (  # noqa: E402
    HARD_MAX_TOKENS,
    TARGET_MAX_TOKENS,
    ProjectionError,
    chunk_markdown,
    project_corpus,
    stable_item_id,
)
from src.retrieval.text import estimate_evidence_tokens  # noqa: E402
from src.corpus.core import yaml_document  # noqa: E402


class ProjectionFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.records: dict[str, dict[str, object]] = {}

    def add_source(
        self,
        source_id: str,
        scope: str,
        body: str,
        *,
        provider: str | None = None,
        exchange_count: int | None = None,
        evidence_role: str | None = None,
        raw_hash: str | None = None,
        filename: str | None = None,
    ) -> Path:
        raw_hash = raw_hash or (source_id[0] * 64)
        relative = Path("sources") / f"{scope}s" / (filename or f"{source_id}.md")
        metadata: dict[str, object] = {
            "id": source_id,
            "type": scope,
            "title": f"Title {source_id}",
            "source_hash": f"sha256:{raw_hash}",
            "importer_version": 1,
        }
        record: dict[str, object] = {
            "output_path": relative.as_posix(),
            "source_hash": raw_hash,
            "importer_version": 1,
            "ingest_status": "ready",
        }
        if scope == "conversation":
            metadata.update(
                {
                    "provider": provider,
                    "provider_session_id": f"session-{source_id}",
                    "exchange_count": exchange_count,
                }
            )
            record["provider"] = provider
        else:
            metadata["layer"] = scope
            if evidence_role is not None:
                metadata["evidence_role"] = evidence_role
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml_document(metadata, body), encoding="utf-8")
        self.records[source_id] = record
        return path

    def write_manifest(self) -> Path:
        path = self.root / "meta" / "manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"version": 2, "sources": self.records}, ensure_ascii=False),
            encoding="utf-8",
        )
        return path


def conversation_body(provider: str, session: str, human: str, assistant: str) -> str:
    return f"""# Fixture conversation

## Exchange 1

- **Turn**: 1
- **User locator**: `{provider}:{session}:turn:1:user@L10-L12`
- **Assistant locator**: `{provider}:{session}:turn:1:assistant@L13-L18`

### Human user

{human}

### Assistant final

{assistant}
"""


class TestItemProjector:
    def test_estimator_and_stable_id_contract(self) -> None:
        assert CHUNK_POLICY_VERSION == "evidence-chunk-v2"
        assert RANKING_POLICY_VERSION == "bm25-rrf-v2"
        assert estimate_evidence_tokens("abcdef") == 2
        assert estimate_evidence_tokens("中文") == 4
        assert estimate_evidence_tokens("🙂") == 2
        first = stable_item_id("sources/notes/a.md", "note:a/root", "root", 1)
        second = stable_item_id("sources/notes/a.md", "note:a/root", "root", 1)
        changed = stable_item_id("sources/notes/a.md", "note:a/root", "root", 2)
        assert first == second
        assert first != changed
        assert re.search(r"^itm_[a-z2-7]{32}$", first)

    def test_conversation_roles_and_counterparts_for_all_providers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = ProjectionFixture(Path(temp))
            for provider in ("claude", "codex", "chatgpt", "deepseek"):
                source_id = f"{provider}-1"
                fixture.add_source(
                    source_id,
                    "conversation",
                    conversation_body(provider, source_id, f"{provider} user", f"{provider} answer"),
                    provider=provider,
                    exchange_count=1,
                )
            result = project_corpus(fixture.root, fixture.write_manifest())

        assert len(result.sources) == 4
        assert len(result.items) == 8
        for provider in ("claude", "codex", "chatgpt", "deepseek"):
            items = [item for item in result.items if item.provider == provider]
            assert {item.role for item in items} == {"human", "assistant"}
            human = next(item for item in items if item.role == "human")
            assistant = next(item for item in items if item.role == "assistant")
            assert human.evidence_role == "user_statement"
            assert assistant.evidence_role == "assistant_suggestion"
            assert human.turn_index == 1
            assert human.relations.counterpart_item_ids == (assistant.item_id,)
            assert assistant.relations.counterpart_item_ids == (human.item_id,)
            assert "/part:1@L10-L12" in (human.locator_with_lines or "")

    def test_long_conversation_messages_have_part_and_counterpart_relations(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = ProjectionFixture(Path(temp))
            fixture.add_source(
                "codex-long",
                "conversation",
                conversation_body("codex", "long", "人" * 1000, "答" * 1000),
                provider="codex",
                exchange_count=1,
            )
            result = project_corpus(fixture.root, fixture.write_manifest())

        human = [item for item in result.items if item.role == "human"]
        assistant = [item for item in result.items if item.role == "assistant"]
        assert len(human) > 1
        assert len(assistant) > 1
        assistant_ids = tuple(item.item_id for item in assistant)
        assert all(item.relations.counterpart_item_ids == assistant_ids for item in human)
        for index, item in enumerate(human):
            assert item.token_estimate <= TARGET_MAX_TOKENS
            assert item.token_estimate <= HARD_MAX_TOKENS
            expected_previous = human[index - 1].item_id if index else None
            expected_next = human[index + 1].item_id if index + 1 < len(human) else None
            assert item.relations.previous_part_id == expected_previous
            assert item.relations.next_part_id == expected_next

    def test_conversation_structure_markers_inside_code_are_content(self) -> None:
        assistant = """Keep this example:

```markdown
## Exchange 2
- **Turn**: 2
### Human user
### Assistant final
```
"""
        with tempfile.TemporaryDirectory() as temp:
            fixture = ProjectionFixture(Path(temp))
            fixture.add_source(
                "codex-code",
                "conversation",
                conversation_body("codex", "code", "Show the example.", assistant),
                provider="codex",
                exchange_count=1,
            )
            result = project_corpus(fixture.root, fixture.write_manifest())

        assert len(result.items) == 2
        projected = next(item for item in result.items if item.role == "assistant")
        assert "## Exchange 2" in projected.body
        assert "### Assistant final" in projected.body

    def test_unique_assistant_boundary_recovers_after_unclosed_user_fence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = ProjectionFixture(Path(temp))
            fixture.add_source(
                "claude-unclosed",
                "conversation",
                conversation_body(
                    "claude",
                    "unclosed",
                    "Synthetic output:\n\n~~~text\nvalue",
                    "The fence was left open.",
                ),
                provider="claude",
                exchange_count=1,
            )
            result = project_corpus(fixture.root, fixture.write_manifest())

        assert len(result.items) == 2
        human = next(item for item in result.items if item.role == "human")
        assert "~~~text" in human.body

    def test_long_unclosed_fenced_code_is_a_valid_block_to_end(self) -> None:
        body = "```text\n" + "\n".join("x" * 180 for _ in range(20))
        parts = chunk_markdown(body)
        assert len(parts) > 1
        assert parts[0].startswith("```text\n")
        assert all(estimate_evidence_tokens(part) <= HARD_MAX_TOKENS for part in parts)

    def test_long_fenced_code_splits_only_at_line_or_scalar_boundaries(self) -> None:
        body = "```text\n" + "\n".join(f"line-{index}-" + "x" * 180 for index in range(20)) + "\n```"
        parts = chunk_markdown(body)
        assert len(parts) > 1
        assert all(part for part in parts)
        assert parts[0].startswith("```text\n")
        assert parts[-1].endswith("\n```")
        assert all("```text" not in part for part in parts[1:])
        assert parts[-1].strip() != "```"
        assert all(estimate_evidence_tokens(part) <= HARD_MAX_TOKENS for part in parts)

    def test_oversized_code_preserves_a_complete_line_up_to_hard_max(self) -> None:
        long_line = "LONG-" + "x" * 1900
        assert estimate_evidence_tokens(long_line) > TARGET_MAX_TOKENS
        assert estimate_evidence_tokens(long_line) <= HARD_MAX_TOKENS
        body = "```text\n" + long_line + "\n" + "\n".join("tail-" + "y" * 180 for _ in range(8)) + "\n```"
        parts = chunk_markdown(body)
        assert len(parts) > 1
        assert sum(long_line in part for part in parts) == 1
        assert all(estimate_evidence_tokens(part) <= HARD_MAX_TOKENS for part in parts)

    def test_protected_markdown_blocks_between_target_and_hard_stay_whole(self) -> None:
        blocks = {
            "code": "```text\n" + "\n".join("x" * 120 for _ in range(15)) + "\n```",
            "table": "\n".join(
                (
                    "| name | value |",
                    "| --- | --- |",
                    *(f"| row-{index} | {'x' * 180} |" for index in range(10)),
                )
            ),
            "list": "\n".join(f"- item-{index} {'x' * 180}" for index in range(10)),
            "blockquote": "\n".join(f"> quote-{index} {'x' * 180}" for index in range(10)),
        }
        for kind, block in blocks.items():
            tokens = estimate_evidence_tokens(block)
            assert tokens > TARGET_MAX_TOKENS
            assert tokens <= HARD_MAX_TOKENS
            assert chunk_markdown(block) == (block,)

    def test_protected_block_above_target_is_not_packed_with_neighbors(self) -> None:
        table = "\n".join(
            (
                "| name | value |",
                "| --- | --- |",
                *(f"| row-{index} | {'x' * 180} |" for index in range(10)),
            )
        )
        body = f"Before.\n\n{table}\n\nAfter."
        parts = chunk_markdown(body)
        assert parts == ("Before.", table, "After.")

    def test_oversized_table_splits_only_between_complete_rows(self) -> None:
        header = "| name | value |"
        delimiter = "| --- | --- |"
        rows = [f"| row-{index} | {'x' * 600} |" for index in range(9)]
        parts = chunk_markdown("\n".join((header, delimiter, *rows)))
        assert len(parts) > 1
        assert f"{header}\n{delimiter}" in parts[0]
        assert all(estimate_evidence_tokens(part) <= HARD_MAX_TOKENS for part in parts)
        for row in rows:
            assert sum(row in part for part in parts) == 1

    def test_oversized_list_splits_at_top_level_items(self) -> None:
        items = [
            f"- parent-{index} {'x' * 350}\n  - child-{index} {'y' * 120}"
            for index in range(10)
        ]
        parts = chunk_markdown("\n".join(items))
        assert len(parts) > 1
        assert all(estimate_evidence_tokens(part) <= HARD_MAX_TOKENS for part in parts)
        for index in range(10):
            parent_part = next(part for part in parts if f"parent-{index}" in part)
            assert f"child-{index}" in parent_part

    def test_oversized_blockquote_splits_at_quote_paragraphs(self) -> None:
        paragraphs = [f"> paragraph-{index} {'x' * 700}\n>" for index in range(6)]
        parts = chunk_markdown("\n".join(paragraphs))
        assert len(parts) > 1
        assert all(estimate_evidence_tokens(part) <= HARD_MAX_TOKENS for part in parts)
        for index in range(6):
            assert sum(f"paragraph-{index}" in part for part in parts) == 1

    def test_note_and_article_use_direct_nonoverlapping_sections(self) -> None:
        note_body = """Root [link](https://example.test/path).

# Parent

Parent direct text.

### Child

Child text with ![diagram](../assets/diagram.png).

```text
# This is code, not a heading
```

### Child

Second child text.
"""
        article_body = """# Article heading

External article body.
"""
        with tempfile.TemporaryDirectory() as temp:
            fixture = ProjectionFixture(Path(temp))
            fixture.add_source("note-a", "note", note_body)
            fixture.add_source(
                "article-a",
                "article",
                article_body,
                evidence_role="external_source",
            )
            result = project_corpus(fixture.root, fixture.write_manifest())

        note_items = [item for item in result.items if item.scope == "note"]
        article_items = [item for item in result.items if item.scope == "article"]
        assert len(note_items) == 4
        root = next(item for item in note_items if "/root/" in item.locator)
        parent = next(item for item in note_items if item.heading_path == ("Parent",))
        children = [item for item in note_items if item.heading_path == ("Parent", "Child")]
        assert root.title == "Title note-a"
        assert "https://example.test/path" in root.body
        assert "Parent direct text" in parent.body
        assert "Child text" not in parent.body
        assert [item.occurrence for item in children] == [1, 2]
        assert "# This is code, not a heading" in children[0].body
        assert "diagram.png" in children[0].body
        assert ":occurrence:2/part:1" in children[1].locator
        assert len(article_items) == 1
        assert article_items[0].evidence_role == "external_source"

    def test_empty_parent_heading_does_not_create_an_item(self) -> None:
        body = """# Empty parent
## Child
Child body.
"""
        with tempfile.TemporaryDirectory() as temp:
            fixture = ProjectionFixture(Path(temp))
            fixture.add_source("note-empty", "note", body)
            result = project_corpus(fixture.root, fixture.write_manifest())

        assert len(result.items) == 1
        assert result.items[0].heading_path == ("Empty parent", "Child")

    def test_digest_changes_with_standardized_source_but_item_identity_stays(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = ProjectionFixture(Path(temp))
            path = fixture.add_source("note-digest", "note", "Stable root body.")
            manifest = fixture.write_manifest()
            before = project_corpus(fixture.root, manifest)
            path.write_text(
                yaml_document(
                    {
                        "id": "note-digest",
                        "type": "note",
                        "layer": "note",
                        "title": "Title note-digest",
                        "source_hash": f"sha256:{'n' * 64}",
                        "importer_version": 1,
                    },
                    "Changed root body.",
                ),
                encoding="utf-8",
            )
            after = project_corpus(fixture.root, manifest)

        assert before.source_digest != after.source_digest
        assert before.items[0].item_id == after.items[0].item_id
        assert before.items[0].body_sha256 != after.items[0].body_sha256

    def test_ready_source_set_mismatch_fails_the_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = ProjectionFixture(Path(temp))
            fixture.add_source("note-ready", "note", "Ready body.")
            extra = fixture.root / "sources" / "notes" / "unregistered.md"
            extra.write_text("Unregistered body.\n", encoding="utf-8")
            with pytest.raises(ProjectionError, match="ready/source set mismatch"):
                project_corpus(fixture.root, fixture.write_manifest())

    def test_manifest_and_frontmatter_hash_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = ProjectionFixture(Path(temp))
            fixture.add_source("note-hash", "note", "Hash body.")
            fixture.records["note-hash"]["source_hash"] = "different"
            with pytest.raises(ProjectionError, match="raw hash mismatch"):
                project_corpus(fixture.root, fixture.write_manifest())
