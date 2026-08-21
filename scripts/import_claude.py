#!/usr/bin/env python3
"""把 claude-exec-docs 中的 Markdown 导出导入为标准化来源。"""

from __future__ import annotations

import argparse
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from wiki_core import (
    IMPORTER_VERSION,
    MANIFEST_PATH,
    REPO_ROOT,
    clean_message,
    derive_title,
    load_manifest,
    redact_secrets,
    relative_to_repo,
    save_manifest,
    sha256_file,
    source_needs_redaction,
    utc_now,
    yaml_document,
)


DEFAULT_INPUT = REPO_ROOT.parent / "claude-exec-docs"
DEFAULT_OUTPUT = REPO_ROOT / "sources" / "conversations" / "claude"
EXCLUDED_FILES = {"INDEX.md", "prompt-tmp.md"}
SESSION_HEADING = re.compile(r"^# Session:\s*(.+)$", re.MULTILINE)


@dataclass(frozen=True)
class ExportUnit:
    source_id: str
    kind: str
    title: str
    created: str
    project: str
    source_path: Path
    locator: str
    content: str
    content_hash: str
    cwd: str = ""
    claude_version: str = ""
    user_turns: int | None = None


def _field(section: str, name: str) -> str:
    match = re.search(fr"^- \*\*{re.escape(name)}\*\*:\s*(.+)$", section, flags=re.MULTILINE)
    return match.group(1).strip().strip("`") if match else ""


def _first_user_text(section: str) -> str:
    match = re.search(
        r"^## (?:👤 )?User(?: \(Turn \d+\))?\s*$\n(?:\*[^\n]+\*\n)?\s*(.*?)(?=^## |\Z)",
        section,
        flags=re.MULTILINE | re.DOTALL,
    )
    return match.group(1).strip() if match else ""


def _stable_id(relative_path: str, kind: str, locator: str, created: str, cwd: str) -> str:
    identity = "\0".join((relative_path, kind, locator, created, cwd))
    return "claude-export-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def _is_low_information_session(raw_title: str, section: str) -> bool:
    normalized = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", raw_title.lower())
    if normalized in {"hello", "hi", "你好", "test", "测试"}:
        return True
    if normalized.startswith(("localcommandcaveat", "commandmessage", "commandname")):
        return True
    if "does not have access to claude code" in section.lower() and section.count("## 🤖 Assistant") <= 1:
        return True
    has_user = bool(re.search(r"^## (?:👤 )?User", section, flags=re.MULTILINE))
    has_assistant = bool(re.search(r"^## (?:🤖 )?Assistant", section, flags=re.MULTILINE))
    return not (has_user and has_assistant)


def iter_export_units(input_dir: Path, kind: str = "all") -> Iterable[ExportUnit]:
    for path in sorted(input_dir.rglob("*.md")):
        if path.name in EXCLUDED_FILES or any(part.startswith(".") for part in path.relative_to(input_dir).parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            continue
        relative_path = path.relative_to(input_dir).as_posix()
        matches = list(SESSION_HEADING.finditer(text))

        if matches and kind in {"all", "session"}:
            project_match = re.search(r"^# Project:\s*(.+)$", text[: matches[0].start()], flags=re.MULTILINE)
            project = project_match.group(1).strip() if project_match else path.stem
            occurrences: dict[str, int] = {}
            for index, match in enumerate(matches):
                raw_title = match.group(1).strip()
                end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
                section = text[match.start() : end].strip()
                if _is_low_information_session(raw_title, section):
                    continue
                created = _field(section, "Date")[:10] or "unknown"
                cwd = _field(section, "Working Directory")
                claude_version = _field(section, "Claude Code Version")
                turns_text = _field(section, "User Turns")
                user_turns = int(turns_text) if turns_text.isdigit() else None
                occurrences[raw_title] = occurrences.get(raw_title, 0) + 1
                locator = raw_title if occurrences[raw_title] == 1 else f"{raw_title}#{occurrences[raw_title]}"
                source_id = _stable_id(relative_path, "session", locator, created, cwd)
                first_user = clean_message(_first_user_text(section))
                title = derive_title("claude", [{"role": "user", "text": first_user or raw_title}])
                digest = hashlib.sha256(section.encode("utf-8")).hexdigest()
                yield ExportUnit(
                    source_id=source_id,
                    kind="session",
                    title=title,
                    created=created,
                    project=project,
                    source_path=path,
                    locator=f"{relative_path}#Session:{locator}",
                    content=section,
                    content_hash=digest,
                    cwd=cwd,
                    claude_version=claude_version,
                    user_turns=user_turns,
                )

        if not matches and kind in {"all", "document"}:
            heading = re.search(r"^#\s+(.+)$", text, flags=re.MULTILINE)
            title = heading.group(1).strip() if heading else path.stem
            source_id = _stable_id(relative_path, "document", title, "unknown", "")
            yield ExportUnit(
                source_id=source_id,
                kind="document",
                title=title,
                created="unknown",
                project=path.parent.name if path.parent != input_dir else path.stem,
                source_path=path,
                locator=relative_path,
                content=text.strip(),
                content_hash=sha256_file(path),
            )


def _remove_legacy_sources(manifest: dict, output_dir: Path, dry_run: bool) -> int:
    legacy_ids = [source_id for source_id, item in manifest["sources"].items() if item.get("origin") == "claude"]
    if dry_run:
        return len(legacy_ids)
    for source_id in legacy_ids:
        item = manifest["sources"].pop(source_id)
        output_path = Path(REPO_ROOT / item.get("output_path", ""))
        if output_path.is_file() and output_path.parent.resolve() == output_dir.resolve():
            output_path.unlink()
    return len(legacy_ids)


def import_exports(
    input_dir: Path,
    output_dir: Path,
    manifest_path: Path,
    *,
    kind: str = "all",
    limit: int | None = None,
    dry_run: bool = False,
    replace_legacy: bool = False,
) -> dict[str, int]:
    if not input_dir.is_dir():
        raise FileNotFoundError(f"Claude 导出目录不存在: {input_dir}")

    manifest = load_manifest(manifest_path)
    stats = {
        "discovered": 0,
        "imported": 0,
        "unchanged": 0,
        "skipped": 0,
        "legacy_removed": 0,
        "redactions": 0,
    }
    if replace_legacy:
        stats["legacy_removed"] = _remove_legacy_sources(manifest, output_dir, dry_run)

    units = iter_export_units(input_dir, kind)
    for unit in units:
        current = manifest["sources"].get(unit.source_id, {})
        current_output = str(current.get("output_path") or "")
        current_output_path = Path(REPO_ROOT / current_output) if current_output else Path()
        if (
            current.get("source_hash") == unit.content_hash
            and current.get("importer_version") == IMPORTER_VERSION
            and current_output_path.is_file()
            and not source_needs_redaction(current_output_path)
        ):
            stats["discovered"] += 1
            stats["unchanged"] += 1
            continue
        if limit is not None and stats["imported"] >= limit:
            break

        stats["discovered"] += 1

        title, title_redactions = redact_secrets(unit.title)
        cleaned = clean_message(unit.content)
        cleaned, redactions = redact_secrets(cleaned)
        redactions += title_redactions
        stats["redactions"] += redactions
        metadata = {
            "id": unit.source_id,
            "type": "conversation" if unit.kind == "session" else "document",
            "origin": "claude-export",
            "source_kind": unit.kind,
            "title": title,
            "created": unit.created,
            "imported": utc_now()[:10],
            "project": unit.project,
            "source_path": str(unit.source_path.resolve()),
            "source_locator": unit.locator,
            "source_hash": f"sha256:{unit.content_hash}",
            "redaction_count": redactions,
        }
        if unit.cwd:
            metadata["cwd"] = unit.cwd
        if unit.claude_version:
            metadata["claude_version"] = unit.claude_version
        if unit.user_turns is not None:
            metadata["user_turns"] = unit.user_turns

        date_prefix = unit.created if unit.created != "unknown" else "document"
        output_path = output_dir / f"{date_prefix}-{unit.source_id.removeprefix('claude-export-')}.md"
        body = (
            f"# {title}\n\n"
            "> 本页从 `claude-exec-docs` 导入。知识结论应整合到 `wiki/`。\n\n"
            f"{cleaned}"
        )
        stats["imported"] += 1
        if dry_run:
            continue
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(yaml_document(metadata, body), encoding="utf-8")
        manifest["sources"][unit.source_id] = {
            "origin": "claude-export",
            "source_kind": unit.kind,
            "source_path": str(unit.source_path.resolve()),
            "source_locator": unit.locator,
            "source_hash": unit.content_hash,
            "output_path": relative_to_repo(output_path),
            "status": "pending" if current.get("source_hash") != unit.content_hash else current.get("status", "pending"),
            "title": title,
            "created": unit.created,
            "redaction_count": redactions,
            "imported_at": utc_now(),
            "importer_version": IMPORTER_VERSION,
        }

    if not dry_run:
        save_manifest(manifest, manifest_path)
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--kind", choices=("all", "session", "document"), default="all")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--replace-legacy", action="store_true", help="移除旧 ~/.claude 导入器生成的来源")
    args = parser.parse_args()
    stats = import_exports(
        args.input,
        args.output,
        args.manifest,
        kind=args.kind,
        limit=args.limit,
        dry_run=args.dry_run,
        replace_legacy=args.replace_legacy,
    )
    print("Claude 导出导入结果:", ", ".join(f"{key}={value}" for key, value in stats.items()))


if __name__ == "__main__":
    main()
