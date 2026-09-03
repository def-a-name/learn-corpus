from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.ingestion.build_source_inventory import build_inventory  # noqa: E402
from src.ingestion.import_web_chat import (  # noqa: E402
    import_web_chats,
    iter_web_chat_units,
    load_review_resolutions,
    unit_skip_reason,
)
from src.operations.check_corpus import check_source_consistency  # noqa: E402
from src.operations.rebuild_review_queue import build_queue  # noqa: E402


DEEPSEEK_REVIEW = """> From: https://chat.deepseek.com/a/chat/s/synthetic-deepseek-session

# you asked

message time: 2026-08-04 22:49:45

虚构星港观测站何时校准？

---

# you asked

message time: 2026-08-04 22:50:09

虚构星港观测站如何校准？

---

# deepseek response

按合成时间表校准虚构观测站[citation:1]。
"""


CHATGPT_REVIEW = """> From: https://chatgpt.com/c/synthetic-chatgpt-session

# you asked

message time: 2026-08-24 17:28:30

为虚构的潮汐传感器生成合成配置。

---

# you asked

message time: 2026-08-24 17:29:40

请基于上一份合成配置生成新版。

![合成仪表图](images/telemetry.png)

---

# chatgpt response

已生成：[下载合成配置](sandbox:/mnt/data/synthetic-config.txt)
"""


CHATGPT_PROJECT = """> From: https://chatgpt.com/g/g-synthetic-project/c/synthetic-project-session

# you asked

message time: 2026-08-28 14:20:23

介绍一下虚构星图索引。

---

# chatgpt response

虚构星图索引会先按星区读取合成记录，再返回结果。
"""


class TestWebChatImporter:
    def test_chatgpt_project_url_imports_with_provider_session_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            inputs = root / "web-chats"
            inputs.mkdir()
            (inputs / "synthetic-project.md").write_text(CHATGPT_PROJECT, encoding="utf-8")
            output = root / "sources" / "conversations"
            manifest = root / "meta" / "manifest.json"

            unit = next(iter_web_chat_units(inputs))
            first = import_web_chats(inputs, output, manifest)
            second = import_web_chats(inputs, output, manifest)
            document = next(output.rglob("*.md")).read_text(encoding="utf-8")

        assert unit.provider == "chatgpt"
        assert unit.provider_session_id == "synthetic-project-session"
        assert unit.derived_session_key == "synthetic-project-session"
        assert unit.identity_confidence == "provider"
        assert unit_skip_reason(unit) == ""
        assert first["imported"] == 1
        assert second["unchanged"] == 1
        assert "provider_session_id: synthetic-project-session" in document

    def test_current_export_issues_enter_review_without_writing_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            inputs = root / "web-chats"
            deepseek = inputs / "deepseek.md"
            chatgpt_dir = inputs / "chatgpt"
            chatgpt = chatgpt_dir / "chatgpt.md"
            image = chatgpt_dir / "images" / "telemetry.png"
            deepseek.parent.mkdir(parents=True)
            image.parent.mkdir(parents=True)
            deepseek.write_text(DEEPSEEK_REVIEW, encoding="utf-8")
            chatgpt.write_text(CHATGPT_REVIEW, encoding="utf-8")
            image.write_bytes(b"png fixture")

            output = root / "sources" / "conversations"
            assets = root / "sources" / "assets"
            manifest = root / "meta" / "manifest.json"
            stats = import_web_chats(inputs, output, manifest, asset_root=assets)
            units = {unit.provider: unit for unit in iter_web_chat_units(inputs)}

        assert stats["discovered"] == 2
        assert stats["imported"] == 0
        assert stats["skipped"] == 2
        assert stats["skip_reasons"] == \
            {
                "citation_targets_missing_review": 1,
                "sandbox_asset_missing_review": 1,
            }
        assert unit_skip_reason(units["deepseek"]) == "citation_targets_missing_review"
        assert unit_skip_reason(units["chatgpt"]) == "sandbox_asset_missing_review"
        assert units["deepseek"].parse.omitted_unpaired_user_count == 1
        assert units["chatgpt"].parse.omitted_unpaired_user_count == 1
        assert units["chatgpt"].image_reference_count == 1
        assert len(units["chatgpt"].raw_assets) == 1
        assert not output.exists()
        assert not manifest.exists()

    def test_explicit_citation_resolution_is_hash_bound_and_omits_markers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            inputs = root / "web-chats"
            source = inputs / "deepseek.md"
            source.parent.mkdir(parents=True)
            source.write_text(DEEPSEEK_REVIEW, encoding="utf-8")
            unit = next(iter_web_chat_units(inputs))
            resolutions = root / "meta" / "source-review-resolutions.json"
            resolutions.parent.mkdir(parents=True)
            resolutions.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "codex_sessions": {},
                        "web_chat_sessions": {
                            unit.source_id: {
                                "decision": "omit_unresolved_citation_markers",
                                "reviewed_at": "2026-08-28",
                                "reviewed_by": "user",
                                "source_hash": f"sha256:{unit.content_hash}",
                                "source_locator": unit.locator,
                                "citation_marker_count": unit.citation_marker_count,
                                "citation_label_count": unit.citation_label_count,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            output = root / "sources" / "conversations"
            manifest = root / "meta" / "manifest.json"
            loaded_resolution = load_review_resolutions(resolutions)[unit.source_id]
            first = import_web_chats(
                inputs,
                output,
                manifest,
                review_resolutions_path=resolutions,
            )
            second = import_web_chats(
                inputs,
                output,
                manifest,
                review_resolutions_path=resolutions,
            )
            document = next(output.rglob("*.md")).read_text(encoding="utf-8")
            saved = json.loads(manifest.read_text(encoding="utf-8"))["sources"][unit.source_id]
            inventory = build_inventory(
                root / "claude",
                root / "codex",
                root / "notes",
                manifest,
                web_chat_input=inputs,
                codex_review_resolutions=root / "missing-codex-resolutions.json",
                web_chat_review_resolutions=resolutions,
                scanned_at="2026-08-28T12:00:00+08:00",
            )
            inventory_path = root / "inventory.json"
            inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
            queue = build_queue(manifest, root / "queue.md", inventory_path)
            source.write_text(DEEPSEEK_REVIEW + "\n", encoding="utf-8")
            changed_unit = next(iter_web_chat_units(inputs))

        assert unit_skip_reason(unit, loaded_resolution) == ""
        assert first["imported"] == 1
        assert second["unchanged"] == 1
        assert "[citation:1]" not in document
        assert "review_resolution: omit_unresolved_citation_markers" in document
        assert "omitted_unresolved_citation_marker_count: 1" in document
        assert saved["review_resolution"] == "omit_unresolved_citation_markers"
        web_chat = next(item for item in inventory["inputs"] if item["provider"] == "web-chat")
        assert web_chat["retained"] == 1
        assert web_chat["skipped"] == 0
        assert "citation_targets_missing_review" not in queue
        assert unit_skip_reason(changed_unit, loaded_resolution) == \
            "review_resolution_mismatch_review"

    def test_clean_deepseek_and_chatgpt_exports_import_and_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            inputs = root / "web-chats"
            chatgpt_dir = inputs / "chatgpt"
            image = chatgpt_dir / "images" / "telemetry.png"
            image.parent.mkdir(parents=True)
            image.write_bytes(b"png fixture")
            (inputs / "deepseek.md").write_text(
                DEEPSEEK_REVIEW.replace("[citation:1]", ""), encoding="utf-8"
            )
            chatgpt_dir.joinpath("chatgpt.md").write_text(
                CHATGPT_REVIEW.replace(
                    "[下载合成配置](sandbox:/mnt/data/synthetic-config.txt)",
                    "请参考合成仪表图",
                ),
                encoding="utf-8",
            )
            output = root / "sources" / "conversations"
            assets = root / "sources" / "assets"
            manifest = root / "meta" / "manifest.json"

            first = import_web_chats(inputs, output, manifest, asset_root=assets)
            second = import_web_chats(inputs, output, manifest, asset_root=assets)
            documents = list(output.rglob("*.md"))
            chatgpt_document = next(path for path in documents if path.parent.name == "chatgpt")
            chatgpt_text = chatgpt_document.read_text(encoding="utf-8")
            asset_file_count = len(list(assets.rglob("*.png")))
            saved = json.loads(manifest.read_text(encoding="utf-8"))
            ingest_events = [
                json.loads(line)
                for line in (root / "meta" / "ingest.log").read_text(encoding="utf-8").splitlines()
                if line
            ]
            errors, warnings = check_source_consistency(manifest, root / "sources", root)

        assert first["imported"] == 2
        assert second["unchanged"] == 2
        assert len(documents) == 2
        assert len(saved["sources"]) == 2
        assert "source_completeness: summary" in chatgpt_text
        assert "assistant_final_detection: heuristic" in chatgpt_text
        assert "omitted_unpaired_user_count: 1" in chatgpt_text
        assert "../../assets/conversation/chatgpt/" in chatgpt_text
        assert asset_file_count == 1
        assert len(ingest_events) == 1
        assert ingest_events[0]["importer"] == "web-chat"
        assert errors == []
        assert warnings == []

    def test_inventory_and_queue_keep_provider_specific_review_details(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            claude = root / "claude"
            codex = root / "codex"
            notes = root / "notes"
            web_chats = root / "web-chats"
            for path in (claude, codex, notes, web_chats):
                path.mkdir()
            (web_chats / "deepseek.md").write_text(DEEPSEEK_REVIEW, encoding="utf-8")
            chatgpt_dir = web_chats / "chatgpt"
            image = chatgpt_dir / "images" / "telemetry.png"
            image.parent.mkdir(parents=True)
            image.write_bytes(b"png fixture")
            chatgpt_dir.joinpath("chatgpt.md").write_text(CHATGPT_REVIEW, encoding="utf-8")

            inventory = build_inventory(
                claude,
                codex,
                notes,
                root / "missing-manifest.json",
                web_chat_input=web_chats,
                codex_review_resolutions=root / "missing-resolutions.json",
                scanned_at="2026-08-28T12:00:00+08:00",
            )
            inventory_path = root / "inventory.json"
            inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"version": 2, "sources": {}}), encoding="utf-8")
            queue = build_queue(manifest, root / "queue.md", inventory_path)

        web_chat = next(item for item in inventory["inputs"] if item["provider"] == "web-chat")
        assert inventory["version"] == 3
        assert web_chat["discovered"] == 2
        assert web_chat["retained"] == 0
        assert web_chat["skipped"] == 2
        assert web_chat["provider_counts"] == {"chatgpt": 1, "deepseek": 1}
        assert "Provider**：`chatgpt`" in queue
        assert "Provider**：`deepseek`" in queue
        assert "ChatGPT sandbox 资源缺失 1 个" in queue
        assert "引用占位符 1 个" in queue
        assert "--raw-path" in queue
