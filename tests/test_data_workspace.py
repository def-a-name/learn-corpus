"""用独立虚构工作区验证代码与来源数据的隔离。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.fixture
def workspace(tmp_path: Path):
    data = tmp_path / "synthetic-private"
    engine = data / "engine"
    engine.mkdir(parents=True)
    shutil.copytree(Path(__file__).resolve().parents[1] / "src", engine / "src", ignore=shutil.ignore_patterns("__pycache__"))
    env = dict(os.environ)
    env["LEARN_CORPUS_DATA_ROOT"] = str(data)
    env.pop("PYTHONPATH", None)

    def run(module: str, *args: str, check: bool = True):
        result = subprocess.run(
            [sys.executable, "-B", "-m", module, *map(str, args)],
            cwd=engine, env=env, capture_output=True, text=True,
        )
        if check:
            assert result.returncode == 0, result.stdout + result.stderr
        return result

    return data, engine, env, run


def test_import_assets_index_git_check_and_removal_use_data_repository(workspace):
    data, engine, env, run = workspace
    raw = data.parent / "synthetic-input"
    raw.mkdir()
    (raw / "note.md").write_text("# Synthetic observatory\n\nquasar atlas\n\n![](chart.png)\n")
    (raw / "chart.png").write_bytes(b"synthetic-image-content")
    run("src.ingestion.import_notes", "--input", raw, "--include", "note.md")
    manifest = json.loads((data / "meta/manifest.json").read_text())
    source_id, record = next(iter(manifest["sources"].items()))
    assert record["output_path"].startswith("sources/notes/")
    assert record["assets"]
    assert (data / record["assets"][0]["stored_path"]).is_file()
    for name in ["source-inventory.json", "source-review-queue.md", "ingest.log"]:
        assert (data / "meta" / name).is_file()
    subprocess.run(["git", "init", "-q", data], check=True)
    subprocess.run(["git", "-C", data, "add", "sources", "meta"], check=True)
    subprocess.run(["git", "-C", data, "-c", "user.name=Synthetic User", "-c", "user.email=synthetic@example.test", "commit", "-qm", "Synthetic baseline"], check=True)
    run("src.maintenance.check_corpus")
    run("src.retrieval.build_lexical_index", "--publish")
    assert (data / "meta/corpus/current").is_symlink()
    run("src.maintenance.remove_source", source_id, "--dry-run")
    run("src.maintenance.remove_source", source_id)
    assert not (data / record["output_path"]).exists()
    assert not (data / record["assets"][0]["stored_path"]).exists()
    run("src.maintenance.check_corpus")
    assert not (engine / "sources").exists()
    assert not (engine / "meta").exists()


def test_mixed_source_batch_defaults_share_one_working_directory(workspace):
    data, engine, env, run = workspace
    raw = data.parent / "synthetic-mixed-input"
    raw.mkdir()
    (raw / "article.md").write_text("# Synthetic article\n\nquasar article\n")
    (raw / "note.md").write_text("# Synthetic note\n\nquasar note\n")
    (raw / "claude.md").write_text(
        "# Project: synthetic-observatory\n\n# Session: synthetic-session\n\n"
        "- **Date**: 2026-01-01 00:00 UTC\n\n## User (Turn 1)\n\n"
        "Synthetic observation request.\n\n## Assistant\n\nSynthetic observation response.\n"
    )
    (raw / "web.md").write_text(
        "> From: https://chatgpt.com/c/synthetic-workspace-session\n\n"
        "# you asked\n\nmessage time: 2026-01-01 12:00:00\n\n"
        "Synthetic web request.\n\n---\n\n# chatgpt response\n\nSynthetic web response.\n"
    )
    rows = [
        {"type": "session_meta", "payload": {"id": "synthetic-workspace-rollout", "timestamp": "2026-01-01T00:00:00Z", "source": "cli", "thread_source": "user"}},
        {"type": "event_msg", "payload": {"type": "task_started"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Synthetic rollout request."}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": "Synthetic rollout response."}]}},
        {"type": "event_msg", "payload": {"type": "task_complete"}},
    ]
    (raw / "codex.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    run("src.ingestion.import_batch", "--notes-input", raw, "--note-include", "note.md", "--articles-input", raw, "--article-include", "article.md", "--claude-input", raw, "--claude-include", "claude.md", "--web-chat-input", raw, "--web-chat-include", "web.md", "--codex-input", raw, "--codex-include", "codex.jsonl")
    records = json.loads((data / "meta/manifest.json").read_text())["sources"]
    assert len(records) == 5
    assert all((data / record["output_path"]).is_file() for record in records.values())
    assert not (engine / "sources").exists()


def test_inventory_only_analyzes_explicit_provider(workspace):
    data, engine, env, run = workspace
    raw = data.parent / "synthetic-inventory-input"
    raw.mkdir()
    (raw / "note.md").write_text("# Synthetic inventory note\n\nquasar inventory\n")
    run("src.ingestion.build_source_inventory", "--notes-input", raw)
    inventory = json.loads((data / "meta/source-inventory.json").read_text())
    assert [item["provider"] for item in inventory["inputs"]] == ["notes"]
    result = run("src.ingestion.import_notes", check=False)
    assert result.returncode == 2 and "--input" in result.stderr
    result = run("src.ingestion.import_batch", "--note-include", "note.md", check=False)
    assert result.returncode == 2 and "--notes-input" in result.stderr


@pytest.mark.parametrize("destination", ["outside", "code", "symlink"])
def test_explicit_workspace_rejects_escaped_outputs(workspace, destination):
    data, engine, env, run = workspace
    raw = data.parent / "synthetic-boundary-input"
    raw.mkdir()
    (raw / "note.md").write_text("# Synthetic boundary note\n\nquasar boundary\n")
    outside = data.parent / "synthetic-outside"
    outside.mkdir()
    if destination == "symlink":
        (data / "sources").symlink_to(outside, target_is_directory=True)
        output = data / "sources/notes"
    else:
        output = (outside if destination == "outside" else engine) / "sources/notes"
    result = run("src.ingestion.import_notes", "--input", raw, "--include", "note.md", "--output", output, check=False)
    assert result.returncode != 0
    assert "data output path" in result.stderr
    assert not (data / "meta").exists()
    assert not list(outside.rglob("*.md"))
    assert not (engine / "sources").exists()


def test_invalid_workspace_fails_before_writing(workspace):
    data, engine, env, run = workspace
    env["LEARN_CORPUS_DATA_ROOT"] = str(data / "synthetic-missing")
    result = run("src.maintenance.find_unprocessed", check=False)
    assert result.returncode != 0 and "existing directory" in result.stderr
    env["LEARN_CORPUS_DATA_ROOT"] = ""
    result = run("src.maintenance.find_unprocessed", check=False)
    assert result.returncode != 0 and "nonempty" in result.stderr
    env["LEARN_CORPUS_DATA_ROOT"] = str(engine / "src")
    result = run("src.maintenance.find_unprocessed", check=False)
    assert result.returncode != 0 and "code directory" in result.stderr
