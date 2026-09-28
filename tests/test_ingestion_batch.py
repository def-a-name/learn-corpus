from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.corpus import paths as corpus_paths
from src.ingestion import batch as ingestion_batch
from src.ingestion import import_claude, import_codex, import_web_chat
from src.ingestion.batch import execute_batch
from src.ingestion.import_notes import import_notes
from src.ingestion.markdown_sources import MarkdownPolicy, MarkdownStrategy


@pytest.fixture
def isolated_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """让虚构来源与正式产物都留在临时仓库。"""

    monkeypatch.setattr(corpus_paths, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ingestion_batch, "REPO_ROOT", tmp_path)
    return tmp_path


def _strategy(root: Path, kind: str, filename: str) -> MarkdownStrategy:
    input_root = root / f"raw-{kind}s"
    policy = MarkdownPolicy(
        kind,
        "personal-notes" if kind == "note" else "external-articles",
        root / "sources" / f"{kind}s",
        root / "sources" / "assets",
        "external_source" if kind == "article" else None,
    )
    return MarkdownStrategy(input_root, policy, [filename], None)


def _units(root: Path, provider: str) -> list[dict]:
    inventory = json.loads((root / "meta" / "source-inventory.json").read_text(encoding="utf-8"))
    return next(item["units"] for item in inventory["inputs"] if item["provider"] == provider)


def test_five_source_strategies_commit_one_batch(isolated_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = isolated_repo
    for module in (import_claude, import_codex, import_web_chat):
        monkeypatch.setattr(module, "REPO_ROOT", root)
    claude = root / "raw-claude"
    codex = root / "raw-codex"
    web = root / "raw-web"
    notes = root / "raw-notes"
    articles = root / "raw-articles"
    for input_root in (claude, codex, web, notes, articles):
        input_root.mkdir()
    (claude / "session.md").write_text(
        "# Project: synthetic-observatory\n\n# Session: synthetic-useful\n\n"
        "- **Date**: 2026-08-01 00:00 UTC\n\n## User (Turn 1)\n\n"
        "虚构观测请求。\n\n## Assistant\n\n虚构观测回复。\n",
        encoding="utf-8",
    )
    codex_rows = [
        {"type": "session_meta", "payload": {
            "id": "synthetic-codex-session", "timestamp": "2026-08-02T00:00:00Z",
            "source": "cli", "thread_source": "user",
        }},
        {"type": "event_msg", "payload": {"type": "task_started"}},
        {"type": "response_item", "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "虚构星区请求。"}],
        }},
        {"type": "response_item", "payload": {
            "type": "message", "role": "assistant", "phase": "final_answer",
            "content": [{"type": "output_text", "text": "虚构星区答复。"}],
        }},
        {"type": "event_msg", "payload": {"type": "task_complete"}},
    ]
    (codex / "session.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in codex_rows) + "\n",
        encoding="utf-8",
    )
    (web / "session.md").write_text(
        "> From: https://chatgpt.com/c/synthetic-web-session\n\n"
        "# you asked\n\nmessage time: 2026-08-03 12:00:00\n\n"
        "虚构网页请求。\n\n---\n\n# chatgpt response\n\n虚构网页答复。\n",
        encoding="utf-8",
    )
    (notes / "note.md").write_text("# 虚构笔记\n\n合成观测笔记。\n", encoding="utf-8")
    (articles / "article.md").write_text("# 虚构文章\n\n合成观测文章。\n", encoding="utf-8")
    resolutions = root / "meta" / "source-review-resolutions.json"
    strategies = [
        import_claude.ClaudeStrategy(
            claude, root / "sources" / "conversations" / "claude", "session", ["session.md"], None,
        ),
        import_codex.CodexStrategy(
            codex, root / "sources" / "conversations" / "codex", None, None,
            ["session.jsonl"], resolutions,
        ),
        import_web_chat.WebChatStrategy(
            web, root / "sources" / "conversations", root / "sources" / "assets",
            resolutions, ["session.md"],
        ),
        _strategy(root, "note", "note.md"),
        _strategy(root, "article", "article.md"),
    ]

    plans = execute_batch(strategies, root / "meta" / "manifest.json")

    assert [plan.stats["imported"] for plan in plans] == [1, 1, 1, 1, 1]
    assert all(not plan.blocking_reasons for plan in plans)
    saved = json.loads((root / "meta" / "manifest.json").read_text(encoding="utf-8"))["sources"]
    assert len(saved) == 5
    assert {item["origin"] for item in saved.values()} == {
        "claude-export", "codex", "web-chat-export", "personal-notes", "external-articles",
    }
    assert len((root / "meta" / "ingest.log").read_text(encoding="utf-8").splitlines()) == 5
    assert "当前没有来源导入问题。" in (root / "meta" / "source-review-queue.md").read_text(encoding="utf-8")


def test_mixed_batch_commits_only_selected_sources(isolated_repo: Path) -> None:
    root = isolated_repo
    notes = root / "raw-notes"
    articles = root / "raw-articles"
    notes.mkdir()
    articles.mkdir()
    (notes / "selected.md").write_text("# 虚构笔记\n\n合成观测甲。\n", encoding="utf-8")
    (notes / "unselected.md").write_text("# 未选笔记\n\n![](missing.png)\n", encoding="utf-8")
    (articles / "selected.md").write_text("# 虚构文章\n\n合成观测乙。\n", encoding="utf-8")
    manifest = root / "meta" / "manifest.json"

    plans = execute_batch(
        [_strategy(root, "note", "selected.md"), _strategy(root, "article", "selected.md")],
        manifest,
    )

    assert [(plan.provider, plan.stats["imported"]) for plan in plans] == [
        ("notes", 1), ("articles", 1),
    ]
    saved = json.loads(manifest.read_text(encoding="utf-8"))["sources"]
    assert len(saved) == 2
    assert {item["source_path"] for item in saved.values()} == {
        str((notes / "selected.md").resolve()),
        str((articles / "selected.md").resolve()),
    }
    assert len(_units(root, "notes")) == 1
    assert len(_units(root, "articles")) == 1
    assert "unselected.md" not in (root / "meta" / "source-review-queue.md").read_text(encoding="utf-8")
    assert len(list((root / "sources" / "notes").glob("*.md"))) == 1
    assert len(list((root / "sources" / "articles").glob("*.md"))) == 1
    assert len((root / "meta" / "ingest.log").read_text(encoding="utf-8").splitlines()) == 2


def test_mixed_batch_block_preserves_ready_and_unselected_state(isolated_repo: Path) -> None:
    root = isolated_repo
    notes = root / "raw-notes"
    articles = root / "raw-articles"
    notes.mkdir()
    articles.mkdir()
    ready = notes / "ready.md"
    ready.write_text("# 虚构旧笔记\n\n合成旧观测。\n", encoding="utf-8")
    unselected = notes / "unselected.md"
    unselected.write_text("# 虚构待审核笔记\n\n![](missing.png)\n", encoding="utf-8")
    article = articles / "new.md"
    article.write_text("# 虚构新文章\n\n合成新观测。\n", encoding="utf-8")
    manifest = root / "meta" / "manifest.json"
    import_notes(notes, root / "sources" / "notes", manifest, asset_root=root / "sources" / "assets", includes=[ready.name])
    import_notes(notes, root / "sources" / "notes", manifest, asset_root=root / "sources" / "assets", includes=[unselected.name])
    old_manifest = manifest.read_bytes()
    old_log = (root / "meta" / "ingest.log").read_bytes()
    old_output = next((root / "sources" / "notes").glob("*.md"))
    old_body = old_output.read_bytes()
    unselected_unit = next(item for item in _units(root, "notes") if item["raw_source_path"] == str(unselected.resolve()))
    ready.write_text("# 虚构旧笔记\n\n![](new-missing.png)\n", encoding="utf-8")

    plans = execute_batch(
        [_strategy(root, "note", ready.name), _strategy(root, "article", article.name)],
        manifest,
    )

    assert plans[0].stats["skip_reasons"] == {"asset_missing": 1}
    assert plans[1].stats["imported"] == 0
    assert plans[1].stats["deferred_by_batch"] == 1
    assert manifest.read_bytes() == old_manifest
    assert (root / "meta" / "ingest.log").read_bytes() == old_log
    assert old_output.read_bytes() == old_body
    assert not (root / "sources" / "articles").exists()
    notes_units = _units(root, "notes")
    assert next(item for item in notes_units if item["raw_source_path"] == str(unselected.resolve())) == unselected_unit
    assert next(item for item in notes_units if item["raw_source_path"] == str(ready.resolve()))["parse_status"] == "review"
    assert _units(root, "articles")[0]["parse_status"] == "ready"
    queue = (root / "meta" / "source-review-queue.md").read_text(encoding="utf-8")
    assert "unselected.md" in queue
    assert "ready.md" in queue
    assert "new.md" not in queue


def test_changed_input_rejects_batch_before_any_output(isolated_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = isolated_repo
    notes = root / "raw-notes"
    notes.mkdir()
    raw = notes / "changing.md"
    raw.write_text("# 虚构原文\n\n初始内容。\n", encoding="utf-8")
    strategy = _strategy(root, "note", raw.name)
    original_prepare = strategy.prepare

    def prepare_then_change(manifest: dict) -> ingestion_batch.ProviderPlan:
        plan = original_prepare(manifest)
        raw.write_text("# 虚构原文\n\n准备后变化。\n", encoding="utf-8")
        return plan

    monkeypatch.setattr(strategy, "prepare", prepare_then_change)
    with pytest.raises(ValueError, match="source changed during import preparation"):
        execute_batch([strategy], root / "meta" / "manifest.json")

    assert not (root / "sources").exists()
    assert not (root / "meta").exists()


def test_mid_commit_write_failure_exposes_partial_output(isolated_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = isolated_repo
    notes = root / "raw-notes"
    articles = root / "raw-articles"
    notes.mkdir()
    articles.mkdir()
    (notes / "one.md").write_text("# 虚构笔记\n\n合成内容甲。\n", encoding="utf-8")
    (articles / "two.md").write_text("# 虚构文章\n\n合成内容乙。\n", encoding="utf-8")
    original_write = ingestion_batch.atomic_write_text

    def fail_article_write(path: Path, content: str) -> None:
        if path.parent == root / "sources" / "articles":
            raise OSError("synthetic write failure")
        original_write(path, content)

    monkeypatch.setattr(ingestion_batch, "atomic_write_text", fail_article_write)
    manifest = root / "meta" / "manifest.json"
    with pytest.raises(OSError, match="synthetic write failure"):
        execute_batch(
            [_strategy(root, "note", "one.md"), _strategy(root, "article", "two.md")],
            manifest,
        )

    assert len(list((root / "sources" / "notes").glob("*.md"))) == 1
    assert not manifest.exists()
    assert not (root / "meta" / "ingest.log").exists()
    assert (root / "meta" / "source-inventory.json").exists()
    assert (root / "meta" / "source-review-queue.md").exists()
