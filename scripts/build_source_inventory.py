#!/usr/bin/env python3
"""盘点当前可访问的 Claude、Codex 和 notes 输入范围。"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from import_claude import DEFAULT_INPUT as CLAUDE_INPUT
from import_claude import iter_export_units, unit_inventory_record, unit_skip_reason
from import_codex import DEFAULT_INPUT as CODEX_INPUT
from import_codex import DEFERRED_SKIP_REASONS as CODEX_DEFERRED_SKIP_REASONS
from import_codex import REVIEW_SKIP_REASONS as CODEX_REVIEW_SKIP_REASONS
from import_codex import iter_session_units as iter_codex_units
from import_codex import resolve_session_units as resolve_codex_units
from import_codex import unit_inventory_record as codex_unit_inventory_record
from wiki_core import MANIFEST_PATH, REPO_ROOT, load_manifest


DEFAULT_NOTES_INPUT = REPO_ROOT.parent / "notes"
DEFAULT_OUTPUT = REPO_ROOT / "meta" / "source-inventory.json"


def _display_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return Path("..", resolved.relative_to(REPO_ROOT.parent)).as_posix()
    except ValueError:
        pass
    home = Path.home().resolve()
    try:
        return f"~/{resolved.relative_to(home).as_posix()}"
    except ValueError:
        return str(resolved)


def _date_range(values: Iterable[str]) -> dict[str, str | None]:
    known = sorted(value for value in values if value and value != "unknown")
    return {"from": known[0] if known else None, "to": known[-1] if known else None}


def _standardized_counts(manifest_path: Path) -> Counter[str]:
    if not manifest_path.is_file():
        return Counter()
    manifest = load_manifest(manifest_path)
    return Counter(str(item.get("origin", "unknown")) for item in manifest["sources"].values())


def _manifest_sources(manifest_path: Path) -> dict[str, Any]:
    if not manifest_path.is_file():
        return {}
    return load_manifest(manifest_path)["sources"]


def _missing_input(provider: str, input_path: Path, source_format: str, known_missing: list[str]) -> dict[str, Any]:
    return {
        "provider": provider,
        "input_path": _display_path(input_path),
        "format": source_format,
        "available": False,
        "date_range": {"from": None, "to": None},
        "discovered": 0,
        "retained": 0,
        "skipped": 0,
        "known_missing": [f"输入路径当前不可用: {_display_path(input_path)}", *known_missing],
    }


def build_inventory(
    claude_input: Path,
    codex_input: Path,
    notes_input: Path,
    manifest_path: Path = MANIFEST_PATH,
    *,
    scanned_at: str | None = None,
) -> dict[str, Any]:
    scanned_at = scanned_at or datetime.now(ZoneInfo("Asia/Hong_Kong")).isoformat(timespec="seconds")
    standardized = _standardized_counts(manifest_path)
    manifest_sources = _manifest_sources(manifest_path)
    inputs: list[dict[str, Any]] = []

    claude_missing = ["其他设备、尚未导出的 Claude Code 及 Claude Web 会话不在本次范围"]
    if not claude_input.is_dir():
        inputs.append(_missing_input("claude-export", claude_input, "markdown-export", claude_missing))
    else:
        claude_units = list(iter_export_units(claude_input))
        unit_counts = Counter(unit.kind for unit in claude_units)
        thread_counts = Counter(unit.thread_kind for unit in claude_units if unit.kind == "session")
        document_counts = Counter(unit.document_kind for unit in claude_units if unit.kind == "document")
        skip_reasons: Counter[str] = Counter()
        unit_records: list[dict[str, Any]] = []
        retained = 0
        for unit in claude_units:
            reason = unit_skip_reason(unit)
            if reason:
                skip_reasons[reason] += 1
            else:
                retained += 1
            if reason == "document_classification_review":
                status = "review"
            elif reason:
                status = "excluded"
            elif unit.source_id in manifest_sources:
                status = "imported"
            elif unit.kind == "document":
                status = "deferred"
            else:
                status = "ready"
            unit_records.append(unit_inventory_record(unit, status, reason))
        inputs.append(
            {
                "provider": "claude-export",
                "input_path": _display_path(claude_input),
                "format": "markdown-export",
                "available": True,
                "date_range": _date_range(unit.created for unit in claude_units),
                "discovered": len(claude_units),
                "retained": retained,
                "skipped": len(claude_units) - retained,
                "unit_counts": {"session": unit_counts["session"], "document": unit_counts["document"]},
                "thread_counts": {
                    "main": thread_counts["main"],
                    "subagent": thread_counts["subagent"],
                    "unknown": thread_counts["unknown"],
                },
                "document_counts": dict(sorted(document_counts.items())),
                "skip_reasons": dict(sorted(skip_reasons.items())),
                "currently_standardized": standardized["claude-export"],
                "known_missing": [
                    "生成这批 Markdown 的原始 ~/.claude/projects/**/*.jsonl 已不可得；真实 session ID、完整 tool result、system 和 progress 事件无法恢复",
                    *claude_missing,
                ],
                "units": unit_records,
            }
        )

    codex_missing = ["其他设备或账号的 Codex rollout 不在本次范围"]
    if not codex_input.is_dir():
        inputs.append(_missing_input("codex", codex_input, "codex-rollout-jsonl", codex_missing))
    else:
        codex_units = list(iter_codex_units(codex_input))
        codex_resolved = resolve_codex_units(codex_units)
        thread_counts = Counter(unit.thread_kind for unit in codex_units)
        skip_reasons: Counter[str] = Counter()
        unit_records: list[dict[str, Any]] = []
        retained = 0
        for resolved in codex_resolved:
            unit = resolved.unit
            reason = resolved.skip_reason
            if reason:
                skip_reasons[reason] += 1
            else:
                retained += 1
            if reason in CODEX_REVIEW_SKIP_REASONS:
                status = "review"
            elif reason in CODEX_DEFERRED_SKIP_REASONS:
                status = "deferred"
            elif reason:
                status = "excluded"
            elif unit.source_id in manifest_sources:
                status = "imported"
            else:
                status = "ready"
            unit_records.append(codex_unit_inventory_record(resolved, status, reason))
        inputs.append(
            {
                "provider": "codex",
                "input_path": _display_path(codex_input),
                "format": "codex-rollout-jsonl",
                "available": True,
                "date_range": _date_range(unit.created for unit in codex_units),
                "discovered": len(codex_units),
                "retained": retained,
                "skipped": len(codex_units) - retained,
                "thread_counts": {
                    "main": thread_counts["main"],
                    "subagent": thread_counts["subagent"],
                    "unknown": thread_counts["unknown"],
                },
                "skip_reasons": dict(sorted(skip_reasons.items())),
                "currently_standardized": standardized["codex"],
                "known_missing": codex_missing,
                "units": unit_records,
            }
        )

    notes_missing = ["当前只盘点 Markdown 正文；图片仅计数，V1 不做图像内容理解"]
    if not notes_input.is_dir():
        inputs.append(_missing_input("notes", notes_input, "markdown", notes_missing))
    else:
        note_files = [
            path
            for path in sorted(notes_input.rglob("*.md"))
            if not any(part.startswith(".") for part in path.relative_to(notes_input).parts)
        ]
        asset_files = [
            path
            for path in sorted(notes_input.rglob("*"))
            if path.is_file() and path.suffix.lower() != ".md" and not any(part.startswith(".") for part in path.relative_to(notes_input).parts)
        ]
        inputs.append(
            {
                "provider": "notes",
                "input_path": _display_path(notes_input),
                "format": "markdown",
                "available": True,
                "date_range": {"from": None, "to": None},
                "discovered": len(note_files),
                "retained": len(note_files),
                "skipped": 0,
                "assets_discovered": len(asset_files),
                "currently_standardized": standardized["notes"],
                "known_missing": notes_missing,
            }
        )

    for item in inputs:
        if item["discovered"] != item["retained"] + item["skipped"]:
            raise ValueError(f"{item['provider']} inventory 计数不成立")
    return {
        "version": 2,
        "scanned_at": scanned_at,
        "coverage_boundary": (
            "只覆盖当前机器上可访问的三个已知输入位置，不代表所有历史会话已提供；"
            "Claude 事件计数只表示现有有损 Markdown 导出中仍可见的内容。"
        ),
        "known_missing": [
            "ChatGPT 历史尚未提供",
            "Claude Web、其他设备和其他账号的历史尚未提供",
        ],
        "inputs": inputs,
    }


def save_inventory(inventory: dict[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    temp_path.write_text(json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(output_path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claude-input", type=Path, default=CLAUDE_INPUT)
    parser.add_argument("--codex-input", type=Path, default=CODEX_INPUT)
    parser.add_argument("--notes-input", type=Path, default=DEFAULT_NOTES_INPUT)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    inventory = build_inventory(args.claude_input, args.codex_input, args.notes_input, args.manifest)
    if not args.dry_run:
        save_inventory(inventory, args.output)
    summary = ", ".join(
        f"{item['provider']}={item['discovered']}/{item['retained']}/{item['skipped']}" for item in inventory["inputs"]
    )
    print(f"Source inventory ({'dry-run' if args.dry_run else 'written'}): {summary}")


if __name__ == "__main__":
    main()
