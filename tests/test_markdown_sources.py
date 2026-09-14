from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.ingestion import import_claude  # noqa: E402
from src.ingestion.build_source_inventory import build_inventory  # noqa: E402
from src.ingestion.import_articles import import_articles  # noqa: E402
from src.ingestion.import_notes import import_notes  # noqa: E402
from src.ingestion.markdown_sources import content_fingerprint  # noqa: E402
from src.operations.check_corpus import check_source_consistency  # noqa: E402
from src.corpus.core import parse_frontmatter  # noqa: E402


# 本模块只使用 example.test 和虚构观测内容构造来源样例。
class TestMarkdownSourceImporter:
    def test_fingerprint_excludes_frontmatter_and_only_normalizes_file_newlines(self) -> None:
        first = b"\xef\xbb\xbf---\r\ntitle: A\r\n---\r\n# Body\r\n\r\ntext  \r\n\r\n"
        second = b"---\ntitle: B\n---\n# Body\n\ntext  \n"
        changed = b"---\ntitle: B\n---\n# Body\n\ntext \n"
        assert content_fingerprint(first) == content_fingerprint(second)
        assert content_fingerprint(second) != content_fingerprint(changed)

    def test_note_import_is_read_only_copies_assets_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            inputs = root / "notes"
            output = root / "sources" / "notes"
            assets = root / "sources" / "assets"
            manifest = root / "meta" / "manifest.json"
            inputs.mkdir()
            (inputs / "diagram.png").write_bytes(b"image-fixture")
            source = inputs / "synthetic-observatory.md"
            source.write_text(
                """# 虚构观测记录

password=fake-test-password  # secret-scan: allow

![合成星图](diagram.png)

[虚构指南](https://example.test/guide)

```bash
curl https://example.test/not-a-link-item
```
""",  # secret-scan: allow
                encoding="utf-8",
            )
            original = source.read_bytes()

            first = import_notes(inputs, output, manifest, asset_root=assets)
            second = import_notes(inputs, output, manifest, asset_root=assets)
            generated = next(output.glob("*.md"))
            metadata, body = parse_frontmatter(generated)
            saved = json.loads(manifest.read_text(encoding="utf-8"))
            ingest_events = [
                json.loads(line)
                for line in (root / "meta" / "ingest.log").read_text(encoding="utf-8").splitlines()
                if line
            ]
            source_after = source.read_bytes()
            stored_asset_exists = Path(
                next(iter(saved["sources"].values()))["assets"][0]["stored_path"]
            ).is_file()
            consistency_errors, _ = check_source_consistency(manifest, root / "sources", root)

        assert first["discovered"] == 1
        assert first["imported"] == 1
        assert first["assets"] == 1
        assert first["links"] == 1
        assert second["unchanged"] == 1
        assert source_after == original
        assert "password=fake-test-password" not in body  # secret-scan: allow
        assert "[REDACTED]" in body
        assert "../assets/note/" in body
        assert metadata["layer"] == "note"
        record = next(iter(saved["sources"].values()))
        assert record["ingest_status"] == "ready"
        assert len(record["assets"]) == 1
        assert stored_asset_exists
        assert consistency_errors == []
        assert len(ingest_events) == 1
        assert ingest_events[0]["importer"] == "notes"
        assert len(ingest_events[0]["changes"]) == 2

    def test_article_maps_publication_metadata_without_filling_missing_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            inputs = root / "articles"
            output = root / "sources" / "articles"
            manifest = root / "meta" / "manifest.json"
            inputs.mkdir()
            (inputs / "article.md").write_text(
                """---
title: 虚构星港报告
source: https://example.test/original
author:
published:
created: 2026-08-20
description: Synthetic clipping
tags: [synthetic]
---
## 合成正文

![](https://cdn.example.test/image.png)
""",
                encoding="utf-8",
            )
            stats = import_articles(inputs, output, manifest, asset_root=root / "sources" / "assets")
            metadata, _ = parse_frontmatter(next(output.glob("*.md")))
            ingest_event = json.loads((root / "meta" / "ingest.log").read_text(encoding="utf-8"))

        assert stats["imported"] == 1
        assert metadata["evidence_role"] == "external_source"
        assert metadata["source_url"] == "https://example.test/original"
        assert metadata["publication_source_status"] == "known"
        assert metadata["author"] == "unknown"
        assert metadata["published"] == "unknown"
        assert metadata["captured"] == "2026-08-20"
        assert metadata["remote_image_count"] == 1
        assert ingest_event["importer"] == "articles"

    def test_article_include_imports_only_the_explicit_markdown_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            inputs = root / "articles"
            output = root / "sources" / "articles"
            manifest = root / "meta" / "manifest.json"
            inputs.mkdir()
            selected = inputs / "selected.md"
            selected.write_text("# 虚构指定文章\n\n合成正文。\n", encoding="utf-8")
            (inputs / "not-selected.md").write_text(
                "# 虚构未指定文章\n\n![](missing.png)\n", encoding="utf-8"
            )

            stats = import_articles(
                inputs,
                output,
                manifest,
                asset_root=root / "sources" / "assets",
                includes=["selected.md"],
            )
            saved = json.loads(manifest.read_text(encoding="utf-8"))
            generated = list(output.glob("*.md"))

        assert stats["discovered"] == 1
        assert stats["imported"] == 1
        assert stats["skipped"] == 0
        assert len(generated) == 1
        assert len(saved["sources"]) == 1
        assert next(iter(saved["sources"].values()))["source_path"] == str(selected.resolve())

    def test_exact_duplicate_is_skipped_only_within_same_layer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            notes = root / "notes"
            articles = root / "articles"
            note_output = root / "sources" / "notes"
            article_output = root / "sources" / "articles"
            assets = root / "sources" / "assets"
            manifest = root / "meta" / "manifest.json"
            notes.mkdir()
            articles.mkdir()
            (notes / "a.md").write_text("---\ntitle: 合成 A\n---\n# 相同的虚构正文\n", encoding="utf-8")
            (notes / "b.md").write_text("---\ntitle: 合成 B\n---\n# 相同的虚构正文\n", encoding="utf-8")
            (articles / "same.md").write_text("# 相同的虚构正文\n", encoding="utf-8")

            note_stats = import_notes(notes, note_output, manifest, asset_root=assets)
            article_stats = import_articles(articles, article_output, manifest, asset_root=assets)
            saved = json.loads(manifest.read_text(encoding="utf-8"))
            duplicate = next(item for item in saved["sources"].values() if item["ingest_status"] == "skipped")
            note_output_count = len(list(note_output.glob("*.md")))
            article_output_count = len(list(article_output.glob("*.md")))

        assert note_stats["imported"] == 1
        assert note_stats["skip_reasons"] == {"exact_duplicate": 1}
        assert article_stats["imported"] == 1
        assert note_output_count == 1
        assert article_output_count == 1
        assert duplicate["duplicate_of"] in saved["sources"]

    def test_missing_local_image_enters_review_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            inputs = root / "notes"
            sources = root / "sources"
            output = sources / "notes"
            manifest = root / "meta" / "manifest.json"
            inputs.mkdir()
            (inputs / "broken.md").write_text("# 合成缺失资源\n\n![](missing.png)\n", encoding="utf-8")

            stats = import_notes(inputs, output, manifest, asset_root=sources / "assets")
            saved = json.loads(manifest.read_text(encoding="utf-8"))
            errors, _ = check_source_consistency(manifest, sources, root)

        assert stats["skip_reasons"] == {"asset_missing": 1}
        assert next(iter(saved["sources"].values()))["ingest_status"] == "review"
        assert not output.exists()
        assert errors == []

    def test_explicit_claude_instruction_document_is_imported_as_note_in_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            claude = root / "claude-exec-docs"
            notes = root / "notes"
            articles = root / "articles"
            output = root / "sources" / "notes"
            manifest = root / "meta" / "manifest.json"
            claude.mkdir()
            notes.mkdir()
            articles.mkdir()
            (claude / "00-global.md").write_text("# 虚构全局指令\n\n合成观测约定。\n", encoding="utf-8")

            import_notes(
                claude,
                output,
                manifest,
                asset_root=root / "sources" / "assets",
                origin="claude-export",
                includes=["00-global.md"],
            )
            import_claude.import_exports(
                claude,
                root / "sources" / "conversations" / "claude",
                manifest,
                kind="all",
            )
            inventory = build_inventory(
                claude,
                root / "missing-codex",
                notes,
                manifest,
                articles_input=articles,
                scanned_at="2026-08-27T00:00:00+08:00",
            )
            claude_input = next(item for item in inventory["inputs"] if item["provider"] == "claude-export")
            saved = json.loads(manifest.read_text(encoding="utf-8"))

        assert claude_input["retained"] == 1
        assert claude_input["skipped"] == 0
        assert claude_input["units"][0]["document_kind"] == "instruction"
        assert claude_input["units"][0]["parse_status"] == "imported"
        assert len(saved["sources"]) == 1
