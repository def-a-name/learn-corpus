from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.corpus.ingest_log import SourceChangeTracker, check_ingest_log, check_committed_ingest_log  # noqa: E402


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


class TestIngestLog:
    def test_committed_checker_matches_push_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "sources" / "notes" / "synthetic-note.md"
            source.parent.mkdir(parents=True)
            git(root, "init", "-q")
            git(root, "config", "user.name", "Synthetic Tester")
            git(root, "config", "user.email", "synthetic@example.test")

            first = SourceChangeTracker.for_output(source.parent)
            first.observe(source)
            source.write_text("Synthetic version one.\n", encoding="utf-8")
            first.append("notes")
            git(root, "add", "sources", "meta/ingest.log")
            git(root, "commit", "-qm", "Add synthetic source")
            baseline = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()

            update = SourceChangeTracker.for_output(source.parent)
            update.observe(source)
            source.write_text("Synthetic version two.\n", encoding="utf-8")
            update.append("notes")
            git(root, "add", "sources", "meta/ingest.log")
            git(root, "commit", "-qm", "Update synthetic source")
            head = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            assert check_committed_ingest_log(root, baseline, head) == []

            source.write_text("Synthetic unlogged version.\n", encoding="utf-8")
            git(root, "add", "sources")
            git(root, "commit", "-qm", "Unlogged synthetic change")
            bad_head = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            assert any("sha256 mismatch" in error for error in check_committed_ingest_log(root, baseline, bad_head))

    def test_tracker_appends_only_actual_source_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "sources" / "notes" / "note.md"
            asset = root / "sources" / "assets" / "note" / "image.png"
            tracker = SourceChangeTracker.for_output(source.parent)
            tracker.observe(source)
            tracker.observe(asset)
            source.parent.mkdir(parents=True)
            asset.parent.mkdir(parents=True)
            source.write_text("# Note\n", encoding="utf-8")
            asset.write_bytes(b"image")
            assert tracker.append("notes")

            update = SourceChangeTracker.for_output(source.parent)
            update.observe(source)
            update.observe(asset)
            source.write_text("# Updated note\n", encoding="utf-8")
            asset.unlink()
            assert update.append("notes")

            unchanged = SourceChangeTracker.for_output(source.parent)
            unchanged.observe(source)
            assert not unchanged.append("notes")
            events = [
                json.loads(line)
                for line in (root / "meta" / "ingest.log").read_text(encoding="utf-8").splitlines()
                if line
            ]

        assert len(events) == 2
        assert events[0]["importer"] == "notes"
        assert [(item["action"], item["path"]) for item in events[0]["changes"]] == \
            [
                ("added", "sources/assets/note/image.png"),
                ("added", "sources/notes/note.md"),
            ]
        assert [(item["action"], item["path"]) for item in events[1]["changes"]] == \
            [
                ("deleted", "sources/assets/note/image.png"),
                ("updated", "sources/notes/note.md"),
            ]

    def test_checker_matches_git_changes_and_rejects_omissions_or_rewrites(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "sources" / "notes" / "note.md"
            asset = root / "sources" / "assets" / "image.png"
            source.parent.mkdir(parents=True)
            git(root, "init", "-q")
            git(root, "config", "user.name", "Test")
            git(root, "config", "user.email", "test@example.invalid")

            initial = SourceChangeTracker.for_output(source.parent)
            initial.observe(source)
            initial.observe(asset)
            source.write_text("version 1\n", encoding="utf-8")
            asset.parent.mkdir(parents=True)
            asset.write_bytes(b"image")
            initial.append("notes")
            git(root, "add", "sources", "meta/ingest.log")
            git(root, "commit", "-q", "-m", "baseline")

            update = SourceChangeTracker.for_output(source.parent)
            update.observe(source)
            update.observe(asset)
            source.write_text("version 2\n", encoding="utf-8")
            asset.unlink()
            update.append("notes")

            added = root / "sources" / "notes" / "added.md"
            addition = SourceChangeTracker.for_output(source.parent)
            addition.observe(added)
            added.write_text("new source\n", encoding="utf-8")
            addition.append("notes")

            revise_addition = SourceChangeTracker.for_output(source.parent)
            revise_addition.observe(added)
            added.write_text("revised new source\n", encoding="utf-8")
            revise_addition.append("notes")

            web_chat = root / "sources" / "conversations" / "chatgpt" / "chat.md"
            web_chat_event = SourceChangeTracker.for_output(web_chat.parent)
            web_chat_event.observe(web_chat)
            web_chat.parent.mkdir(parents=True)
            web_chat.write_text("web chat source\n", encoding="utf-8")
            web_chat_event.append("web-chat")
            errors, warnings = check_ingest_log(root)
            assert errors == []
            assert warnings == []

            unlogged = root / "sources" / "notes" / "unlogged.md"
            unlogged.write_text("missing event\n", encoding="utf-8")
            missing_errors, _ = check_ingest_log(root)
            assert any("has no log entry" in error for error in missing_errors)
            unlogged.unlink()

            (root / "meta" / "ingest.log").write_text("\n", encoding="utf-8")
            rewrite_errors, _ = check_ingest_log(root)
            assert any("append-only" in error for error in rewrite_errors)
