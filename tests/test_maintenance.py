from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.ingestion.build_source_inventory import build_inventory  # noqa: E402
from src.ingestion.import_notes import import_notes  # noqa: E402
from src.maintenance.check_corpus import check_source_consistency  # noqa: E402
from src.maintenance.rebuild_review_queue import build_queue  # noqa: E402
from src.corpus.removal import SourceRemovalError, remove_source  # noqa: E402
from src.maintenance.scan_secrets import scan_paths  # noqa: E402
from src.corpus.document import yaml_document  # noqa: E402
from src.corpus.storage import sha256_file  # noqa: E402
from src.corpus import paths as corpus_paths  # noqa: E402
from src.ingestion import batch as ingestion_batch  # noqa: E402


# 所有来源文本、路径和凭据样例都是专用的虚构测试数据。
def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in records) + "\n", encoding="utf-8")


class TestMaintenance:
    def test_remove_source_supports_all_markdown_scopes_and_registered_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw_root = root / "raw"
            raw_root.mkdir()
            manifest_path = root / "meta" / "manifest.json"
            manifest_path.parent.mkdir()
            sources: dict[str, dict] = {}
            expected_paths: list[Path] = []
            raw_paths: list[Path] = []
            cases = (
                ("conversation", Path("sources/conversations/synthetic/session.md")),
                ("note", Path("sources/notes/synthetic-note.md")),
                ("article", Path("sources/articles/synthetic-article.md")),
            )
            for index, (scope, relative_output) in enumerate(cases, start=1):
                source_id = f"synthetic-{scope}"
                raw_path = raw_root / f"input-{index}.md"
                raw_path.write_text(f"# Synthetic raw {index}\n", encoding="utf-8")
                output_path = root / relative_output
                output_path.parent.mkdir(parents=True, exist_ok=True)
                metadata = {
                    "id": source_id,
                    "type": scope,
                    "origin": "synthetic",
                    "source_path": str(raw_path),
                    "source_hash": "sha256:" + "0" * 64,
                }
                if scope in {"note", "article"}:
                    metadata["layer"] = scope
                output_path.write_text(yaml_document(metadata, f"# Synthetic {scope}"), encoding="utf-8")
                asset_path = root / "sources" / "assets" / scope / source_id / f"asset-{index}.bin"
                asset_path.parent.mkdir(parents=True, exist_ok=True)
                asset_path.write_bytes(f"synthetic-asset-{index}".encode())
                sources[source_id] = {
                    "origin": "synthetic",
                    "source_kind": "session" if scope == "conversation" else "document",
                    "layer": scope if scope != "conversation" else None,
                    "source_path": str(raw_path),
                    "source_hash": "0" * 64,
                    "output_path": relative_output.as_posix(),
                    "assets": [
                        {
                            "stored_path": asset_path.relative_to(root).as_posix(),
                            "asset_hash": sha256_file(asset_path),
                        }
                    ],
                    "ingest_status": "ready",
                    "curation_status": "unassessed",
                }
                expected_paths.extend((output_path, asset_path))
                raw_paths.append(raw_path)
            manifest_path.write_text(
                json.dumps({"version": 2, "sources": sources}), encoding="utf-8"
            )

            results = [
                remove_source(source_id, repo_root=root)
                for source_id in ("synthetic-conversation", "synthetic-note", "synthetic-article")
            ]
            saved = json.loads(manifest_path.read_text(encoding="utf-8"))
            events = [
                json.loads(line)
                for line in (root / "meta" / "ingest.log").read_text(encoding="utf-8").splitlines()
            ]
            consistency_errors, _ = check_source_consistency(
                manifest_path, root / "sources", root
            )
            raw_files_remain = all(path.is_file() for path in raw_paths)
            generated_files_removed = all(not path.exists() for path in expected_paths)

        assert all(result["removed"] for result in results)
        assert all(result["assets"] == 1 for result in results)
        assert generated_files_removed
        assert raw_files_remain
        assert saved["sources"] == {}
        assert len(events) == 3
        assert all(event["importer"] == "remove-source" for event in events)
        assert all({change["action"] for change in event["changes"]} == {"deleted"} for event in events)
        assert consistency_errors == []

    def test_remove_source_dry_run_does_not_change_files_or_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "sources" / "notes" / "synthetic-note.md"
            output.parent.mkdir(parents=True)
            output.write_text(
                yaml_document(
                    {"id": "synthetic-note", "type": "note", "layer": "note"},
                    "# Synthetic",
                ),
                encoding="utf-8",
            )
            manifest = root / "meta" / "manifest.json"
            manifest.parent.mkdir()
            manifest.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "sources": {
                            "synthetic-note": {
                                "ingest_status": "ready",
                                "layer": "note",
                                "output_path": "sources/notes/synthetic-note.md",
                                "assets": [],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            before = manifest.read_bytes()

            result = remove_source("synthetic-note", repo_root=root, dry_run=True)

            assert result["removed"] is False
            assert output.is_file()
            assert manifest.read_bytes() == before
            assert not (root / "meta" / "ingest.log").exists()

    def test_removed_note_can_be_imported_again_from_unchanged_raw_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            monkeypatch.setattr(corpus_paths, "REPO_ROOT", root)
            monkeypatch.setattr(ingestion_batch, "REPO_ROOT", root)
            input_root = root / "raw-notes"
            input_root.mkdir()
            raw = input_root / "synthetic-observation.md"
            raw.write_text("# 虚构观测记录\n\n合成校准结果。\n", encoding="utf-8")
            output = root / "sources" / "notes"
            manifest = root / "meta" / "manifest.json"
            assets = root / "sources" / "assets"

            first = import_notes(input_root, output, manifest, asset_root=assets, includes=[raw.name])
            source_id = next(iter(json.loads(manifest.read_text(encoding="utf-8"))["sources"]))
            preview = remove_source(source_id, repo_root=root, dry_run=True)
            removed = remove_source(source_id, repo_root=root)
            second = import_notes(input_root, output, manifest, asset_root=assets, includes=[raw.name])
            saved = json.loads(manifest.read_text(encoding="utf-8"))["sources"]
            events = [
                json.loads(line)
                for line in (root / "meta" / "ingest.log").read_text(encoding="utf-8").splitlines()
            ]

            assert first["imported"] == 1
            assert preview["removed"] is False
            assert removed["removed"] is True
            assert second["imported"] == 1
            assert list(saved) == [source_id]
            assert saved[source_id]["ingest_status"] == "ready"
            assert raw.is_file()
            assert len(list(output.glob("*.md"))) == 1
            assert [event["changes"][0]["action"] for event in events] == ["added", "deleted", "added"]

    def test_remove_source_rejects_manifest_dependents_and_shared_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "sources" / "articles" / "canonical.md"
            output.parent.mkdir(parents=True)
            output.write_text(
                yaml_document(
                    {"id": "canonical", "type": "article", "layer": "article"},
                    "# Synthetic",
                ),
                encoding="utf-8",
            )
            asset = root / "sources" / "assets" / "article" / "canonical" / "shared.bin"
            asset.parent.mkdir(parents=True)
            asset.write_bytes(b"synthetic-shared-asset")
            manifest = root / "meta" / "manifest.json"
            manifest.parent.mkdir()
            manifest.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "sources": {
                            "canonical": {
                                "ingest_status": "ready",
                                "layer": "article",
                                "output_path": "sources/articles/canonical.md",
                                "assets": [
                                    {
                                        "stored_path": "sources/assets/article/canonical/shared.bin",
                                        "asset_hash": sha256_file(asset),
                                    }
                                ],
                            },
                            "dependent": {
                                "ingest_status": "skipped",
                                "duplicate_of": "canonical",
                                "assets": [],
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            with pytest.raises(SourceRemovalError, match="manifest dependents"):
                remove_source("canonical", repo_root=root)

            assert output.is_file()
            assert asset.is_file()

            saved = json.loads(manifest.read_text(encoding="utf-8"))
            saved["sources"].pop("dependent")
            saved["sources"]["shared-owner"] = {
                "ingest_status": "ready",
                "assets": [
                    {
                        "stored_path": "sources/assets/article/canonical/shared.bin",
                        "asset_hash": sha256_file(asset),
                    }
                ],
            }
            manifest.write_text(json.dumps(saved), encoding="utf-8")

            with pytest.raises(SourceRemovalError, match="shared assets"):
                remove_source("canonical", repo_root=root)

            assert output.is_file()
            assert asset.is_file()

    def test_review_queue_includes_clickable_lines_and_locator_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "manifest.json"
            inventory = root / "inventory.json"
            output = root / "queue.md"
            raw = root / "raw.md"
            raw.write_text("# Session: synthetic-review\n", encoding="utf-8")
            manifest.write_text(json.dumps({"version": 2, "sources": {}}), encoding="utf-8")
            inventory.write_text(
                json.dumps(
                    {
                        "inputs": [
                            {
                                "provider": "claude-export",
                                "units": [
                                    {
                                        "source_id": "claude-export-review",
                                        "title": "Claude 会话：虚构审核会话",
                                        "created": "2026-08-25",
                                        "parse_status": "review",
                                        "skip_reason": "document_classification_review",
                                        "raw_source_path": str(raw),
                                        "raw_source_locator": "raw.md#Session:1@L1-L1",
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            content = build_queue(manifest, output, inventory)
        assert "## Claude 会话：虚构审核会话" in content
        assert "raw.md:1" in content
        assert "src.ingestion.read_raw_locator 'raw.md#Session:1@L1-L1'" in content

    def test_codex_invalid_jsonl_precedes_historical_incomplete_turn_in_review_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            claude = root / "claude"
            codex = root / "codex"
            notes = root / "notes"
            claude.mkdir()
            codex.mkdir()
            notes.mkdir()
            source = codex / "rollout-invalid.jsonl"
            corrupt_line = b"\x00" * 8
            records_before = [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": "invalid-with-history",
                        "timestamp": "2026-08-21T00:00:00Z",
                        "source": "cli",
                        "thread_source": "user",
                    },
                },
                {"type": "event_msg", "payload": {"type": "task_started"}},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "未完成的合成请求"}],
                    },
                },
            ]
            records_after = [
                {"type": "event_msg", "payload": {"type": "task_started"}},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "完整的合成请求"}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "phase": "final_answer",
                        "content": [{"type": "output_text", "text": "完整的合成回复"}],
                    },
                },
                {"type": "event_msg", "payload": {"type": "task_complete"}},
            ]
            source.write_bytes(
                b"\n".join(
                    [
                        *(json.dumps(item, ensure_ascii=False).encode("utf-8") for item in records_before),
                        corrupt_line,
                        *(json.dumps(item, ensure_ascii=False).encode("utf-8") for item in records_after),
                    ]
                )
                + b"\n"
            )
            inventory = build_inventory(
                claude,
                codex,
                notes,
                root / "missing-manifest.json",
                codex_review_resolutions=root / "missing-resolutions.json",
                scanned_at="2026-08-27T12:00:00+08:00",
            )
            inventory_path = root / "inventory.json"
            inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"version": 2, "sources": {}}), encoding="utf-8")
            queue = build_queue(manifest, root / "queue.md", inventory_path)

        codex_input = next(item for item in inventory["inputs"] if item["provider"] == "codex")
        unit = codex_input["units"][0]
        assert codex_input["skip_reasons"] == {"invalid_jsonl_review": 1}
        assert unit["parse_status"] == "review"
        assert unit["skip_reason"] == "invalid_jsonl_review"
        assert unit["active_open_turn_count"] == 0
        assert unit["superseded_incomplete_turn_count"] == 1
        assert unit["invalid_jsonl_line_numbers"] == [4]
        assert "invalid_jsonl_review" in queue
        assert "codex-invalid-with-history" in queue

    def test_source_inventory_counts_inputs_and_skip_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            claude = root / "claude"
            codex = root / "codex"
            notes = root / "notes"
            claude.mkdir()
            notes.mkdir()
            (claude / "sessions.md").write_text(
                """# Project: synthetic-observatory

# Session: synthetic-useful

- **Date**: 2026-08-01 00:00 UTC

## User (Turn 1)

虚构观测请求

## Assistant

虚构观测回复

# Session: no final visible

- **Date**: 2026-08-01 01:00 UTC

## User (Turn 1)

只有合成请求
""",
                encoding="utf-8",
            )
            note_export = claude / "my-notes" / "synthetic-document.md"
            note_export.parent.mkdir()
            note_export.write_text("# 合成资料\n\n虚构观测内容\n", encoding="utf-8")
            complete = [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": "ok",
                        "timestamp": "2026-08-02T00:00:00Z",
                        "source": "cli",
                        "thread_source": "user",
                    },
                },
                {"type": "event_msg", "payload": {"type": "task_started"}},
                {
                    "type": "response_item",
                    "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "合成观测请求"}]},
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "phase": "final_answer",
                        "content": [{"type": "output_text", "text": "合成观测回复"}],
                    },
                },
                {"type": "event_msg", "payload": {"type": "task_complete"}},
            ]
            incomplete = [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": "skip",
                        "timestamp": "2026-08-03T00:00:00Z",
                        "source": "cli",
                        "thread_source": "user",
                    },
                },
                {"type": "event_msg", "payload": {"type": "task_started"}},
                {
                    "type": "response_item",
                    "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "只有合成请求"}]},
                },
                {"type": "event_msg", "payload": {"type": "turn_aborted"}},
            ]
            write_jsonl(codex / "01.jsonl", complete)
            write_jsonl(codex / "02.jsonl", incomplete)
            (notes / "note.md").write_text("# 合成笔记\n", encoding="utf-8")
            (notes / "image.png").write_bytes(b"fixture")
            inventory = build_inventory(
                claude,
                codex,
                notes,
                root / "missing-manifest.json",
                scanned_at="2026-08-21T12:00:00+08:00",
            )
        by_provider = {item["provider"]: item for item in inventory["inputs"]}
        assert inventory["version"] == 3
        assert by_provider["claude-export"]["discovered"] == 3
        assert by_provider["claude-export"]["retained"] == 2
        assert by_provider["claude-export"]["unit_counts"] == {"session": 2, "document": 1}
        assert by_provider["claude-export"]["thread_counts"]["main"] == 2
        assert len(by_provider["claude-export"]["units"]) == 3
        assert by_provider["claude-export"]["skip_reasons"] == {"no_final_visible": 1}
        no_final_unit = next(
            unit
            for unit in by_provider["claude-export"]["units"]
            if unit["skip_reason"] == "no_final_visible"
        )
        assert no_final_unit["parse_status"] == "excluded"
        assert by_provider["codex"]["discovered"] == 2
        assert by_provider["codex"]["retained"] == 1
        assert by_provider["codex"]["skip_reasons"] == {"no_final_visible": 1}
        assert by_provider["codex"]["thread_counts"] == {"main": 2, "subagent": 0, "unknown": 0}
        assert len(by_provider["codex"]["units"]) == 2
        assert by_provider["notes"]["assets_discovered"] == 1

    def test_source_consistency_detects_stale_hash_and_orphan(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            sources = repo / "sources"
            source = sources / "example.md"
            raw = repo / "raw.jsonl"
            manifest = repo / "meta" / "manifest.json"
            sources.mkdir()
            manifest.parent.mkdir()
            raw.write_text('{"source": true}\n', encoding="utf-8")
            digest = sha256_file(raw)
            source.write_text(
                yaml_document(
                    {
                        "id": "codex-example",
                        "type": "conversation",
                        "origin": "codex",
                        "title": "虚构观测来源",
                        "source_path": str(raw),
                        "source_hash": f"sha256:{digest}",
                        "omitted_trivial_exchange_count": 0,
                    },
                    "# 虚构观测来源",
                ),
                encoding="utf-8",
            )
            manifest.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "sources": {
                            "codex-example": {
                                "origin": "codex",
                                "title": "虚构观测来源",
                                "source_path": str(raw),
                                "source_hash": digest,
                                "omitted_trivial_exchange_count": 0,
                                "output_path": "sources/example.md",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            errors, warnings = check_source_consistency(manifest, sources, repo)
            raw.write_text('{"source": false}\n', encoding="utf-8")
            stale_errors, stale_warnings = check_source_consistency(manifest, sources, repo)
            orphan = sources / "orphan.md"
            orphan.write_text(yaml_document({"id": "orphan"}, "# Orphan"), encoding="utf-8")
            orphan_errors, _ = check_source_consistency(manifest, sources, repo)
        assert errors == []
        assert warnings == []
        assert stale_errors == []
        assert any("raw source differs from accepted version" in warning for warning in stale_warnings)
        assert any("orphan source" in error for error in orphan_errors)

    def test_secret_scan_reports_location_without_value(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            unsafe = root / "unsafe.env"
            allowed = root / "fixture.py"
            unsafe.write_text("password=fake-test-unapproved-value\n", encoding="utf-8")  # secret-scan: allow
            allowed.write_text("password=test-value  # secret-scan: allow\n", encoding="utf-8")
            findings = scan_paths([unsafe, allowed], root)
        assert len(findings) == 1
        assert findings[0].path == Path("unsafe.env")
        assert findings[0].line == 1
        assert findings[0].kind == "credential_assignment"
        assert "fake-test-unapproved-value" not in repr(findings[0])

    def test_secret_scan_ignores_escaped_quote_expression(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "expression.py"
            source.write_text(  # secret-scan: allow
                "password = '\\\"' + user_password[1:10] + '\\\"}'\n",  # secret-scan: allow
                encoding="utf-8",
            )
            findings = scan_paths([source], root)
        assert findings == []

    def test_secret_scan_reports_chinese_credential_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            unsafe = root / "unsafe.md"
            unsafe.write_text("密码 SyntheticSecret123!\n", encoding="utf-8")  # secret-scan: allow
            findings = scan_paths([unsafe], root)
        assert len(findings) == 1
        assert findings[0].kind == "chinese_credential_assignment"
