#!/usr/bin/env python3
"""记录各来源导入器共用的 append-only 来源变更事件。"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from src.corpus.manifest import utc_now
from src.corpus.paths import REPO_ROOT
from src.corpus.storage import sha256_file


@dataclass(frozen=True)
class SourceChange:
    action: str
    path: Path
    sha256: str | None


def _file_hash(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_sources_root(path: Path) -> Path | None:
    """返回最近的 sources 祖先目录；输出不在范围内时返回 None。"""
    resolved = path.resolve()
    for candidate in (resolved, *resolved.parents):
        if candidate.name == "sources":
            return candidate
    return None


class SourceChangeTracker:
    """捕获初始状态，并为最终来源变更追加一条事件。"""

    def __init__(self, sources_root: Path | None) -> None:
        self.sources_root = sources_root.resolve() if sources_root is not None else None
        self._before: dict[Path, str | None] = {}

    @classmethod
    def for_output(cls, output_path: Path) -> "SourceChangeTracker":
        return cls(find_sources_root(output_path))

    def observe(self, path: Path) -> None:
        if self.sources_root is None:
            return
        resolved = path.resolve()
        try:
            resolved.relative_to(self.sources_root)
        except ValueError:
            return
        self._before.setdefault(resolved, _file_hash(resolved))

    def changes(self) -> list[SourceChange]:
        changes: list[SourceChange] = []
        for path, before_hash in sorted(self._before.items(), key=lambda item: item[0].as_posix()):
            after_hash = _file_hash(path)
            if before_hash == after_hash:
                continue
            if before_hash is None:
                action = "added"
            elif after_hash is None:
                action = "deleted"
            else:
                action = "updated"
            changes.append(SourceChange(action=action, path=path, sha256=after_hash))
        return changes

    def append(self, importer: str) -> bool:
        changes = self.changes()
        if not changes:
            return False
        if self.sources_root is None:
            raise AssertionError("source changes exist without a sources root")
        repo_root = self.sources_root.parent
        records = []
        for change in changes:
            relative_path = change.path.relative_to(repo_root).as_posix()
            records.append(
                {
                    "action": change.action,
                    "path": relative_path,
                    "sha256": change.sha256,
                }
            )
        event = {
            "version": 1,
            "timestamp": utc_now(),
            "importer": importer,
            "changes": records,
        }
        encoded = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        log_path = repo_root / "meta" / "ingest.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(log_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            remaining = memoryview(encoded)
            while remaining:
                written = os.write(descriptor, remaining)
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return True


def _parse_events(data: bytes, label: str) -> tuple[list[dict[str, Any]], list[str]]:
    events: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return events, [f"{label}: must use UTF-8"]
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"{label}:{line_number}: invalid JSON: {exc.msg}")
            continue
        if not isinstance(event, dict):
            errors.append(f"{label}:{line_number}: event must be a JSON object")
            continue
        if set(event) != {"version", "timestamp", "importer", "changes"}:
            errors.append(f"{label}:{line_number}: event fields do not match schema")
            continue
        if event.get("version") != 1:
            errors.append(f"{label}:{line_number}: unknown version={event.get('version')}")
        try:
            datetime.fromisoformat(str(event.get("timestamp")).replace("Z", "+00:00"))
        except ValueError:
            errors.append(f"{label}:{line_number}: invalid timestamp")
        if event.get("importer") not in {
            "claude",
            "codex",
            "web-chat",
            "notes",
            "articles",
            "remove-source",
        }:
            errors.append(f"{label}:{line_number}: unknown importer")
        changes = event.get("changes")
        if not isinstance(changes, list) or not changes:
            errors.append(f"{label}:{line_number}: changes must be a non-empty list")
            continue
        seen_paths: set[str] = set()
        valid_changes: list[dict[str, Any]] = []
        for index, change in enumerate(changes, start=1):
            prefix = f"{label}:{line_number}:change:{index}"
            if not isinstance(change, dict) or set(change) != {"action", "path", "sha256"}:
                errors.append(f"{prefix}: fields do not match schema")
                continue
            action = change.get("action")
            path = str(change.get("path") or "")
            digest = change.get("sha256")
            pure_path = PurePosixPath(path)
            if (
                pure_path.is_absolute()
                or len(pure_path.parts) < 2
                or pure_path.parts[0] != "sources"
                or any(part in {"", ".", ".."} for part in pure_path.parts)
                or "\\" in path
            ):
                errors.append(f"{prefix}: path must be a canonical relative path under sources/")
            if path in seen_paths:
                errors.append(f"{prefix}: duplicate path in the same event: {path}")
            seen_paths.add(path)
            if action not in {"added", "updated", "deleted"}:
                errors.append(f"{prefix}: unknown action")
            if action == "deleted":
                if digest is not None:
                    errors.append(f"{prefix}: deleted sha256 must be null")
            elif not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                errors.append(f"{prefix}: invalid sha256")
            valid_changes.append(change)
        event["changes"] = valid_changes
        events.append(event)
    return events, errors


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _working_source_changes(repo_root: Path) -> tuple[dict[str, str], list[str]]:
    errors: list[str] = []
    changes: dict[str, str] = {}
    tracked = _git(repo_root, "diff", "--no-renames", "--name-status", "-z", "HEAD", "--", "sources")
    if tracked.returncode != 0:
        return changes, ["ingest log: cannot read Git source changes"]
    tokens = tracked.stdout.split(b"\0")
    if tokens and tokens[-1] == b"":
        tokens.pop()
    if len(tokens) % 2:
        errors.append("ingest log: cannot parse Git source changes")
    else:
        for index in range(0, len(tokens), 2):
            status = tokens[index].decode("ascii", errors="replace")[:1]
            path = tokens[index + 1].decode("utf-8", errors="surrogateescape")
            changes[path] = "added" if status == "A" else "deleted" if status == "D" else "updated"
    untracked = _git(repo_root, "ls-files", "--others", "--exclude-standard", "-z", "--", "sources")
    if untracked.returncode != 0:
        errors.append("ingest log: cannot read untracked source files")
    else:
        for raw_path in untracked.stdout.split(b"\0"):
            if raw_path:
                changes[raw_path.decode("utf-8", errors="surrogateescape")] = "added"
    return changes, errors


def check_ingest_log(repo_root: Path = REPO_ROOT) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    top_level = _git(repo_root, "rev-parse", "--show-toplevel")
    if top_level.returncode != 0:
        return errors, warnings
    try:
        actual_top_level = Path(top_level.stdout.decode("utf-8").strip()).resolve()
    except UnicodeDecodeError:
        return ["ingest log: Git repository path is not UTF-8"], warnings
    if actual_top_level != repo_root.resolve():
        return errors, warnings
    if _git(repo_root, "rev-parse", "--verify", "HEAD").returncode != 0:
        return errors, warnings

    log_path = repo_root / "meta" / "ingest.log"
    if not log_path.is_file():
        return ["meta/ingest.log does not exist"], warnings
    current = log_path.read_bytes()
    committed = _git(repo_root, "show", "HEAD:meta/ingest.log")
    baseline = committed.stdout if committed.returncode == 0 else b""
    _, baseline_errors = _parse_events(baseline, "HEAD:meta/ingest.log")
    errors.extend(baseline_errors)
    if not current.startswith(baseline):
        errors.append(
            "meta/ingest.log is append-only; committed content cannot be changed or deleted"
        )
        return errors, warnings
    new_events, new_errors = _parse_events(current[len(baseline) :], "meta/ingest.log:new")
    errors.extend(new_errors)

    working_changes, git_errors = _working_source_changes(repo_root)
    errors.extend(git_errors)
    logged_changes: dict[str, list[dict[str, Any]]] = {}
    for event in new_events:
        for change in event["changes"]:
            logged_changes.setdefault(str(change["path"]), []).append(change)

    for path in sorted(set(working_changes) - set(logged_changes)):
        errors.append(f"ingest log: source change has no log entry: {path}")
    for path in sorted(set(logged_changes) - set(working_changes)):
        errors.append(f"ingest log: logged path has no corresponding source change: {path}")
    for path in sorted(set(working_changes) & set(logged_changes)):
        expected_action = working_changes[path]
        sequence = logged_changes[path]
        first_action = sequence[0].get("action")
        final = sequence[-1]
        final_action = final.get("action")
        if first_action == "added" and final_action != "deleted":
            logged_action = "added"
        elif final_action == "deleted":
            logged_action = "deleted"
        else:
            logged_action = "updated"
        if logged_action != expected_action:
            errors.append(
                f"ingest log: action differs from Git for {path}: "
                f"logged={logged_action} git={expected_action}"
            )
            continue
        if expected_action != "deleted":
            actual_path = repo_root / path
            if not actual_path.is_file() or sha256_file(actual_path) != final.get("sha256"):
                errors.append(f"ingest log: sha256 differs from current file: {path}")
    return errors, warnings
