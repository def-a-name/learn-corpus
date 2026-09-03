from __future__ import annotations

import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.ingestion import import_claude, import_codex  # noqa: E402
from src.ingestion.read_raw_locator import render_text, resolve_locator  # noqa: E402
from src.operations.check_corpus import check_source_consistency  # noqa: E402


# 所有会话、路径和标识都是为测试构造的虚构数据。
def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in records) + "\n", encoding="utf-8")


class TestImporter:
    def test_import_claude_rejects_missing_input_before_replacing_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps({"version": 1, "sources": {"old": {"origin": "claude"}}}),
                encoding="utf-8",
            )
            with pytest.raises(FileNotFoundError):
                import_claude.import_exports(
                    root / "missing",
                    root / "output",
                    manifest,
                    replace_legacy=True,
                )
            saved = json.loads(manifest.read_text(encoding="utf-8"))
        assert "old" in saved["sources"]

    def test_import_codex_whitelists_final_messages(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "input" / "rollout-test.jsonl"
            output = root / "sources" / "conversations" / "codex"
            manifest = root / "meta" / "manifest.json"
            write_jsonl(
                source,
                [
                    {
                        "timestamp": "2026-07-22T00:00:00Z",
                        "type": "session_meta",
                        "payload": {
                            "id": "session-1",
                            "timestamp": "2026-07-22T00:00:00Z",
                            "cwd": "/workspace/synthetic-observatory",
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
                            "content": [{"type": "input_text", "text": "你好"}],
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "phase": "final_answer",
                            "content": [{"type": "output_text", "text": "你好。有什么可以帮你处理的合成任务？"}],
                        },
                    },
                    {"type": "event_msg", "payload": {"type": "task_complete"}},
                    {"type": "event_msg", "payload": {"type": "task_started"}},
                    {
                        "type": "response_item",
                        "payload": {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "synthetic runtime instructions"}]},
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "<environment_context>internal</environment_context>"}],
                        },
                    },
                    {"type": "turn_context", "payload": {"turn_id": "turn-1"}},
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": '<request xsi:noNamespaceSchemaLocation="juice_schema.xsd">Juice number</request>',
                                }
                            ],
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "phase": "final_answer",
                            "content": [{"type": "output_text", "text": "24"}],
                        },
                    },
                    {"type": "event_msg", "payload": {"type": "task_complete"}},
                    {"type": "event_msg", "payload": {"type": "task_started"}},
                    {"type": "turn_context", "payload": {"turn_id": "turn-2"}},
                    {
                        "type": "response_item",
                        "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "如何处理合成遥测数据？ password=fake-test-password"}]},  # secret-scan: allow
                    },
                    {
                        "type": "response_item",
                        "payload": {"type": "message", "role": "assistant", "phase": "commentary", "content": [{"type": "output_text", "text": "合成过程更新"}]},
                    },
                    {
                        "type": "response_item",
                        "payload": {"type": "reasoning", "encrypted_content": "hidden"},
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "合成的无 phase 消息"}],
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {"type": "message", "role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": "合成遥测数据处理完成。"}]},
                    },
                    {"type": "event_msg", "payload": {"type": "task_complete"}},
                ],
            )
            parsed = import_codex.parse_session(source, source.parent)
            first = import_codex.import_sessions(source.parent, output, manifest)
            second = import_codex.import_sessions(source.parent, output, manifest)
            document = next(output.glob("*.md")).read_text(encoding="utf-8")
            saved = json.loads(manifest.read_text(encoding="utf-8"))
            ingest_events = [
                json.loads(line)
                for line in (root / "meta" / "ingest.log").read_text(encoding="utf-8").splitlines()
                if line
            ]
        assert first["imported"] == 1
        assert second["unchanged"] == 1
        assert parsed.visible_assistant_final_count == 3
        assert parsed.assistant_final_count == 1
        assert parsed.omitted_trivial_exchange_count == 1
        assert first["omitted_trivial_exchanges"] == 1
        assert "合成遥测数据处理完成。" in document
        assert "[REDACTED]" in document
        assert "assistant_final_detection: explicit" in document
        assert "## Exchange 1" in document
        assert "omitted_trivial_exchange_count: 1" in document
        assert re.search(r"User locator\*\*: `rollout-test\.jsonl#Session:session-1/Turn:3/User@L\d+-L\d+`", document)
        assert "有什么可以帮你处理的合成任务" not in document
        assert "合成过程更新" not in document
        assert "synthetic runtime instructions" not in document
        assert "hidden" not in document
        assert "合成的无 phase 消息" not in document
        assert "\n24\n" not in document
        assert next(iter(saved["sources"].values()))["omitted_trivial_exchange_count"] == 1
        assert len(ingest_events) == 1
        assert ingest_events[0]["importer"] == "codex"

    def test_import_codex_treats_pre_context_agents_envelope_as_runtime(self) -> None:
        agents_envelope = (
            "# AGENTS.md instructions for /workspace/synthetic-observatory\n\n"
            "<INSTRUCTIONS>\n"
            "# 合成项目约定\n\n"
            "- 只修改必要文件。\n"
            "</INSTRUCTIONS>\n\n"
            "<environment_context>\n"
            "  <cwd>/workspace/synthetic-observatory</cwd>\n"
            "</environment_context>"
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "rollout-agents.jsonl"
            write_jsonl(
                source,
                [
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "agents-runtime",
                            "timestamp": "2026-08-27T00:00:00Z",
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
                            "content": [{"type": "input_text", "text": agents_envelope}],
                        },
                    },
                    {"type": "turn_context", "payload": {"turn_id": "turn-1"}},
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "请检查虚构观测站"}],
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "phase": "final_answer",
                            "content": [{"type": "output_text", "text": "检查完成"}],
                        },
                    },
                    {"type": "event_msg", "payload": {"type": "task_complete"}},
                ],
            )
            parsed = import_codex.parse_session(source, root)

        assert len(parsed.exchanges) == 1
        assert parsed.human_user_count == 1
        assert parsed.omitted_runtime_user_count == 1
        assert parsed.ambiguous_user_turn_count == 0
        assert import_codex._intrinsic_skip_reason(parsed) == ""
        assert import_codex._classify_user_text(
                agents_envelope,
                before_turn_context=False,
            )[0] == \
            "human"
        assert import_codex._classify_user_text(
                "# AGENTS.md instructions for /workspace/synthetic-observatory\n\n请解释这份合成约定。",
                before_turn_context=True,
            )[0] == \
            "human"

    def test_codex_classifies_subagent_fork_and_active_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subagent = root / "subagent.jsonl"
            fork = root / "fork.jsonl"
            active = root / "active.jsonl"
            substantive_greeting = root / "substantive-greeting.jsonl"
            write_jsonl(
                subagent,
                [
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "subagent",
                            "timestamp": "2026-08-01T00:00:00Z",
                            "source": {"subagent": "worker"},
                            "thread_source": "subagent",
                            "parent_thread_id": "parent",
                        },
                    }
                ],
            )
            write_jsonl(
                fork,
                [
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "child",
                            "timestamp": "2026-08-02T00:00:00Z",
                            "source": "cli",
                            "thread_source": "user",
                            "forked_from_id": "parent",
                        },
                    },
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "parent",
                            "timestamp": "2026-08-01T00:00:00Z",
                            "source": "cli",
                            "thread_source": "user",
                        },
                    },
                ],
            )
            write_jsonl(
                active,
                [
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "active",
                            "timestamp": "2026-08-03T00:00:00Z",
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
                            "content": [{"type": "input_text", "text": "尚未结束"}],
                        },
                    },
                ],
            )
            write_jsonl(
                substantive_greeting,
                [
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "substantive-greeting",
                            "timestamp": "2026-08-04T00:00:00Z",
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
                            "content": [{"type": "input_text", "text": "你好"}],
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "phase": "final_answer",
                            "content": [{"type": "output_text", "text": "虚构遥测探针需要增加校验帧。"}],
                        },
                    },
                    {"type": "event_msg", "payload": {"type": "task_complete"}},
                ],
            )
            subagent_unit = import_codex.parse_session(subagent, root)
            fork_unit = import_codex.parse_session(fork, root)
            active_unit = import_codex.parse_session(active, root)
            substantive_greeting_unit = import_codex.parse_session(substantive_greeting, root)
            resolved = {
                item.unit.provider_session_id: item
                for item in import_codex.resolve_session_units(
                    [subagent_unit, fork_unit, active_unit, substantive_greeting_unit]
                )
            }
        assert subagent_unit.thread_kind == "subagent"
        assert resolved["subagent"].skip_reason == "subagent_excluded"
        assert fork_unit.provider_session_id == "child"
        assert fork_unit.extra_session_meta_count == 1
        assert resolved["child"].skip_reason == "no_human_user"
        assert active_unit.open_turn_count == 1
        assert resolved["active"].skip_reason == "active_session_deferred"
        assert substantive_greeting_unit.omitted_trivial_exchange_count == 0
        assert resolved["substantive-greeting"].skip_reason == ""

    def test_codex_review_resolution_omits_corrupt_turn_and_imports_later_exchanges(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_dir = root / "input"
            source = input_dir / "rollout-reviewed.jsonl"
            output = root / "sources" / "conversations" / "codex"
            manifest = root / "meta" / "manifest.json"
            resolutions = root / "meta" / "source-review-resolutions.json"
            records_before_corruption = [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": "reviewed",
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
                        "content": [{"type": "input_text", "text": "未完成的合成校准请求"}],
                    },
                },
            ]
            records_after_corruption = [
                {"type": "event_msg", "payload": {"type": "task_started"}},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "请执行完整的虚构校准流程"}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "phase": "final_answer",
                        "content": [{"type": "output_text", "text": "虚构校准流程已完成。"}],
                    },
                },
                {"type": "event_msg", "payload": {"type": "task_complete"}},
            ]
            corrupt_line = b"\x00" * 8
            source.parent.mkdir(parents=True)
            source.write_bytes(
                b"\n".join(
                    [
                        *(json.dumps(item, ensure_ascii=False).encode("utf-8") for item in records_before_corruption),
                        corrupt_line,
                        *(json.dumps(item, ensure_ascii=False).encode("utf-8") for item in records_after_corruption),
                    ]
                )
                + b"\n"
            )
            parsed = import_codex.parse_session(source, input_dir)
            unresolved = import_codex.resolve_session_units([parsed])[0]
            resolutions.parent.mkdir(parents=True)
            resolutions.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "codex_sessions": {
                            "reviewed": {
                                "decision": "omit_invalid_jsonl_and_superseded_turns",
                                "reviewed_at": "2026-08-27",
                                "reviewed_by": "user",
                                "invalid_jsonl_lines": [
                                    {
                                        "line": 4,
                                        "sha256": hashlib.sha256(corrupt_line).hexdigest(),
                                        "nul_only": True,
                                    }
                                ],
                                "superseded_incomplete_turns": [
                                    {"start_line": 2, "next_start_line": 5}
                                ],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            first = import_codex.import_sessions(
                input_dir,
                output,
                manifest,
                review_resolutions_path=resolutions,
            )
            second = import_codex.import_sessions(
                input_dir,
                output,
                manifest,
                review_resolutions_path=resolutions,
            )
            document = next(output.glob("*.md")).read_text(encoding="utf-8")
            saved = json.loads(manifest.read_text(encoding="utf-8"))["sources"]["codex-reviewed"]
            errors, warnings = check_source_consistency(
                manifest,
                root / "sources",
                root,
                codex_review_resolutions_path=resolutions,
            )

        assert parsed.invalid_line_count == 1
        assert parsed.active_open_turn_count == 0
        assert parsed.superseded_incomplete_turns == \
            (import_codex.SupersededIncompleteTurn(start_line=2, next_start_line=5),)
        assert unresolved.skip_reason == "invalid_jsonl_review"
        assert first["imported"] == 1
        assert first["omitted_superseded_turns"] == 1
        assert second["unchanged"] == 1
        assert "未完成的合成校准请求" not in document
        assert "请执行完整的虚构校准流程" in document
        assert "虚构校准流程已完成。" in document
        assert "review_resolution: omit_invalid_jsonl_and_superseded_turns" in document
        assert saved["reviewed_invalid_jsonl_lines"] == 1
        assert saved["reviewed_superseded_incomplete_turn_count"] == 1
        assert errors == []
        assert warnings == []

    def test_codex_imports_only_fork_delta_after_exact_exchange_prefix(self) -> None:
        def meta(session_id: str, **extra: str) -> dict:
            payload = {
                "id": session_id,
                "timestamp": "2026-08-01T00:00:00Z",
                "source": "cli",
                "thread_source": "user",
            }
            payload.update(extra)
            return {"type": "session_meta", "payload": payload}

        def turn(user: str, assistant: str | None, *, aborted: bool = False) -> list[dict]:
            records = [
                {"type": "event_msg", "payload": {"type": "task_started"}},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": user}],
                    },
                },
            ]
            if assistant is not None:
                records.append(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "phase": "final_answer",
                            "content": [{"type": "output_text", "text": assistant}],
                        },
                    }
                )
            records.append(
                {
                    "type": "event_msg",
                    "payload": {"type": "turn_aborted" if aborted else "task_complete"},
                }
            )
            return records

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_dir = root / "input"
            output = root / "output"
            manifest = root / "manifest.json"
            parent_records = [meta("parent")]
            parent_records.extend(turn("共享的合成请求", "共享的合成回复"))
            parent_records.extend(turn("重试虚构校准", None, aborted=True))
            child_records = [meta("child", forked_from_id="parent"), meta("parent")]
            child_records.extend(turn("共享的合成请求", "共享的合成回复"))
            child_records.extend(turn("重试虚构校准", "在 fork 中完成合成校准"))
            child_records.extend(turn("新的合成请求", "新的合成回复"))
            write_jsonl(input_dir / "parent.jsonl", parent_records)
            write_jsonl(input_dir / "child.jsonl", child_records)

            units = import_codex.resolve_session_units(import_codex.iter_session_units(input_dir))
            by_id = {item.unit.provider_session_id: item for item in units}
            target_only = import_codex.import_sessions(
                input_dir,
                output,
                manifest,
                dry_run=True,
                session_ids={"child"},
            )
            first = import_codex.import_sessions(input_dir, output, manifest)
            second = import_codex.import_sessions(input_dir, output, manifest)
            saved = json.loads(manifest.read_text(encoding="utf-8"))
            child_entry = saved["sources"]["codex-child"]
            child_document = (root / child_entry["output_path"]).read_text(encoding="utf-8")
            errors, warnings = check_source_consistency(manifest, output, root)

        child = by_id["child"]
        assert child.skip_reason == ""
        assert child.source_scope == "fork_delta"
        assert child.omitted_fork_prefix_exchange_count == 1
        assert [item.user_text for item in child.exchanges] == ["重试虚构校准", "新的合成请求"]
        assert "重试虚构校准" in child.title
        assert target_only["skip_reasons"] == {"fork_parent_not_standardized_review": 1}
        assert first["imported"] == 2
        assert first["omitted_fork_prefix_exchanges"] == 1
        assert second["unchanged"] == 2
        assert "共享的合成请求" not in child_document
        assert "在 fork 中完成合成校准" in child_document
        assert "source_scope: fork_delta" in child_document
        assert "omitted_fork_prefix_exchange_count: 1" in child_document
        assert child_entry["forked_from_id"] == "parent"
        assert child_entry["fork_parent_source_id"] == "codex-parent"
        assert errors == []
        assert warnings == []

    def test_codex_reviews_fork_without_parent_or_exact_prefix(self) -> None:
        def unit(session_id: str, question: str, answer: str, parent: str | None = None):
            with tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / f"{session_id}.jsonl"
                payload = {
                    "id": session_id,
                    "timestamp": "2026-08-01T00:00:00Z",
                    "source": "cli",
                    "thread_source": "user",
                }
                if parent:
                    payload["forked_from_id"] = parent
                write_jsonl(
                    path,
                    [
                        {"type": "session_meta", "payload": payload},
                        {"type": "event_msg", "payload": {"type": "task_started"}},
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": question}],
                            },
                        },
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "assistant",
                                "phase": "final_answer",
                                "content": [{"type": "output_text", "text": answer}],
                            },
                        },
                        {"type": "event_msg", "payload": {"type": "task_complete"}},
                    ],
                )
                return import_codex.parse_session(path, path.parent)

        parent = unit("parent", "A", "A answer")
        mismatched = unit("mismatch", "B", "B answer", "parent")
        missing = unit("missing", "C", "C answer", "absent")
        identical = unit("identical", "A", "A answer", "parent")
        resolved = {
            item.unit.provider_session_id: item
            for item in import_codex.resolve_session_units([parent, mismatched, missing, identical])
        }
        assert resolved["mismatch"].skip_reason == "fork_prefix_mismatch_review"
        assert resolved["missing"].skip_reason == "fork_parent_missing_review"
        assert resolved["identical"].skip_reason == "fork_no_novel_exchange"
        assert resolved["identical"].omitted_fork_prefix_exchange_count == 1

    def test_import_claude_whitelists_main_human_and_final(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_dir = root / "claude-exec-docs"
            source = input_dir / "01-synthetic-observatory.md"
            output = root / "sources" / "conversations" / "claude"
            manifest = root / "meta" / "manifest.json"
            source.parent.mkdir(parents=True)
            source.write_text(
                """# Project: synthetic-observatory

# Session: synthetic-probe-calibration

- **Date**: 2026-07-21 00:00 UTC
- **Working Directory**: `/workspace/synthetic-observatory`
- **Claude Code Version**: 2.0
- **User Turns**: 2

## 👤 User (Turn 1)

你好

## 🤖 Assistant

你好。有什么可以帮你？

## 👤 User (Turn 2)

虚构遥测探针为什么离线？

## 🤖 Assistant

我先检查合成配置。

## 🤖 Assistant

> **Tool: Read** `/workspace/synthetic-observatory/config.py`

## 👤 User (Turn 3)

<task-notification>
<result>synthetic agent output</result>
</task-notification>

## 🤖 Assistant

需要增加校验帧。

## 👤 User (Turn 4)

<command-message>checkpoint</command-message>
<command-name>/checkpoint</command-name>

## 🤖 Assistant

checkpoint created

---

> *This is a sub-agent conversation spawned by the main session.*

# Session: synthetic-subagent-inspection

- **Date**: 2026-07-21 00:30 UTC

## 👤 User (Turn 1)

检查虚构探针的内部代码。

## 🤖 Assistant

sub-agent 的合成局部回复。

# Session: hello

- **Date**: 2026-07-21 01:00 UTC

## 👤 User (Turn 1)

hello

## 🤖 Assistant

你好
""",
                encoding="utf-8",
            )
            note = input_dir / "my-notes" / "synthetic-observatory.md"
            note.parent.mkdir()
            note.write_text("# 虚构观测站记录\n\n合成观测窗口为五分钟。\n", encoding="utf-8")
            stats = import_claude.import_exports(input_dir, output, manifest, kind="session")
            second = import_claude.import_exports(input_dir, output, manifest, kind="session")
            documents = "\n".join(path.read_text(encoding="utf-8") for path in output.glob("*.md"))
            units = list(import_claude.iter_export_units(input_dir))
            session_unit = next(unit for unit in units if unit.kind == "session" and not import_claude.unit_skip_reason(unit))
            session_result = resolve_locator(input_dir, session_unit.locator)
            stale_session_result = resolve_locator(
                input_dir,
                "01-synthetic-observatory.md#Session:1@L1-L1",
            )
            user_result = resolve_locator(input_dir, session_unit.session_parse.exchanges[0].user_locator)
            assistant_result = resolve_locator(input_dir, session_unit.session_parse.exchanges[0].assistant_locator)
            saved = json.loads(manifest.read_text(encoding="utf-8"))
            ingest_events = [
                json.loads(line)
                for line in (root / "meta" / "ingest.log").read_text(encoding="utf-8").splitlines()
                if line
            ]
        assert stats["discovered"] == 3
        assert stats["imported"] == 1
        assert stats["skipped"] == 2
        assert stats["skip_reasons"] == {"subagent_excluded": 1, "trivial_session": 1}
        assert second["unchanged"] == 1
        assert stats["omitted_trivial_exchanges"] == 2
        assert "需要增加校验帧" in documents
        assert "### Human user" in documents
        assert "### Assistant final" in documents
        assert "assistant_final_detection: heuristic" in documents
        assert "omitted_trivial_exchange_count: 1" in documents
        assert "有什么可以帮你" not in documents
        assert "Assistant phase confidence" not in documents
        assert "我先检查合成配置" not in documents
        assert "Tool: Read" not in documents
        assert "synthetic agent output" not in documents
        assert "sub-agent 的合成局部回复" not in documents
        assert "# Session: hello" not in documents
        assert re.search(
            r"01-synthetic-observatory\.md#Session:1@L\d+-L\d+$",
            session_unit.locator,
        )
        assert session_result["kind"] == "session"
        assert "# Session: synthetic-probe-calibration" in session_result["content"]
        assert not stale_session_result["requested_range_matches"]
        assert "Warning:" in render_text(stale_session_result)
        assert user_result["kind"] == "human_user"
        assert "虚构遥测探针为什么离线" in user_result["content"]
        assert assistant_result["kind"] == "assistant_final"
        assert "需要增加校验帧" in assistant_result["content"]
        assert f"{user_result['start_line']} |" in render_text(user_result, line_numbers=True)
        assert {unit.document_kind for unit in units if unit.kind == "document"} == {"note"}
        entry = next(iter(saved["sources"].values()))
        assert entry["ingest_status"] == "ready"
        assert entry["curation_status"] == "unassessed"
        assert entry["omitted_trivial_exchange_count"] == 1
        assert entry["assistant_final_detection"] == "heuristic"
        assert "status" not in entry
        assert len(ingest_events) == 1
        assert ingest_events[0]["importer"] == "claude"

    def test_claude_interruption_does_not_promote_process_text(self) -> None:
        parsed = import_claude.parse_session(
            """# Session: interrupted

## User (Turn 1)

检查虚构观测仪。

## Assistant

我先读取合成配置。

## Assistant

> **Tool: Read** `/tmp/synthetic-fixture`

## User (Turn 2)

[Request interrupted by user for tool use]
""",
            "synthetic-fixture.md#Session:1",
        )
        assert parsed.exchanges == ()
        assert parsed.omitted_interrupted_count == 1
        assert parsed.omitted_unpaired_user_count == 1

    def test_claude_does_not_skip_useful_session_with_trivial_first_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_dir = root / "claude-exec-docs"
            output = root / "output"
            manifest = root / "manifest.json"
            input_dir.mkdir()
            (input_dir / "synthetic-session.md").write_text(
                """# Project: synthetic-observatory

# Session: hello

- **Date**: 2026-07-21 00:00 UTC

## User (Turn 1)

hello

## Assistant

你好。

## User (Turn 2)

虚构遥测探针为什么离线？

## Assistant

需要增加校验帧。
""",
                encoding="utf-8",
            )
            stats = import_claude.import_exports(input_dir, output, manifest)
            document = next(output.glob("*.md")).read_text(encoding="utf-8")
        assert stats["imported"] == 1
        assert "Claude 会话：虚构遥测探针为什么离线" in document
        assert "hello" not in document
        assert "需要增加校验帧" in document
        assert "omitted_trivial_exchange_count: 1" in document

    def test_claude_excludes_expanded_skill_prompt_from_human_messages(self) -> None:
        parsed = import_claude.parse_session(
            """# Session: synthetic-skill-expansion

## User (Turn 1)

<command-name>/review</command-name>

## User (Turn 2)

Base directory for this skill: /tmp/synthetic-review

Synthetic internal skill instructions.

## Assistant

Synthetic skill process output.

## User (Turn 3)

请总结虚构观测结果。

## Assistant

虚构观测结果正常。
""",
            "synthetic-fixture.md#Session:1",
        )
        assert parsed.human_user_count == 1
        assert len(parsed.exchanges) == 1
        assert parsed.exchanges[0].user_text == "请总结虚构观测结果。"
        assert parsed.exchanges[0].assistant_text == "虚构观测结果正常。"

    def test_claude_all_mode_defers_documents_without_orphans(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_dir = root / "claude-exec-docs" / "my-notes"
            output = root / "output"
            manifest = root / "manifest.json"
            input_dir.mkdir(parents=True)
            (input_dir / "note.md").write_text("# 合成笔记\n\n虚构观测内容。\n", encoding="utf-8")
            stats = import_claude.import_exports(root / "claude-exec-docs", output, manifest, kind="all")
            saved = json.loads(manifest.read_text(encoding="utf-8"))
        assert stats["discovered"] == 1
        assert stats["skipped"] == 1
        assert stats["skip_reasons"] == {"document_note_deferred": 1}
        assert saved["sources"] == {}

    def test_claude_limit_skips_unchanged_units(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_dir = root / "claude-exec-docs"
            output = root / "output"
            manifest = root / "manifest.json"
            input_dir.mkdir()

            def write_session(name: str, question: str) -> None:
                (input_dir / name).write_text(
                    f"""# Project: synthetic-observatory

# Session: {name}

- **Date**: 2026-07-21 00:00 UTC

## 👤 User (Turn 1)

{question}

## 🤖 Assistant

合成会话的有效回复。
""",
                    encoding="utf-8",
                )

            write_session("01-first.md", "第一个虚构校准请求")
            first = import_claude.import_exports(input_dir, output, manifest)
            write_session("02-second.md", "第二个虚构校准请求")
            write_session("03-third.md", "第三个虚构校准请求")
            second = import_claude.import_exports(input_dir, output, manifest, limit=1)
        assert first["imported"] == 1
        assert second["discovered"] == 2
        assert second["unchanged"] == 1
        assert second["imported"] == 1
        assert second["discovered"] == second["imported"] + second["unchanged"] + second["skipped"]

    def test_codex_limit_skips_unchanged_and_skipped_units(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_dir = root / "input"
            output = root / "output"
            manifest = root / "manifest.json"

            def write_session(name: str, session_id: str, *, complete: bool = True) -> None:
                records = [
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": session_id,
                            "timestamp": "2026-07-22T00:00:00Z",
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
                            "content": [{"type": "input_text", "text": f"合成请求 {session_id}"}],
                        },
                    },
                ]
                if complete:
                    records.append(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "assistant",
                                "phase": "final_answer",
                                "content": [{"type": "output_text", "text": "合成会话的有效回复"}],
                            },
                        }
                    )
                records.append(
                    {"type": "event_msg", "payload": {"type": "task_complete" if complete else "turn_aborted"}}
                )
                write_jsonl(input_dir / name, records)

            write_session("01-first.jsonl", "first")
            first = import_codex.import_sessions(input_dir, output, manifest)
            write_session("02-skipped.jsonl", "skipped", complete=False)
            write_session("03-third.jsonl", "third")
            second = import_codex.import_sessions(input_dir, output, manifest, limit=1)
        assert first["imported"] == 1
        assert second["discovered"] == 3
        assert second["unchanged"] == 1
        assert second["skipped"] == 1
        assert second["skip_reasons"] == {"no_final_visible": 1}
        assert second["imported"] == 1
        assert second["discovered"] == second["imported"] + second["unchanged"] + second["skipped"]
