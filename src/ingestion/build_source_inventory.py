#!/usr/bin/env python3
"""盘点当前可访问的会话、notes 和 articles 输入范围。"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from src.ingestion.import_claude import DEFAULT_INPUT as CLAUDE_INPUT
from src.ingestion.import_claude import iter_export_units, unit_inventory_record, unit_skip_reason
from src.ingestion.import_codex import DEFAULT_INPUT as CODEX_INPUT
from src.ingestion.import_codex import DEFAULT_REVIEW_RESOLUTIONS as CODEX_REVIEW_RESOLUTIONS
from src.ingestion.import_codex import DEFERRED_SKIP_REASONS as CODEX_DEFERRED_SKIP_REASONS
from src.ingestion.import_codex import REVIEW_SKIP_REASONS as CODEX_REVIEW_SKIP_REASONS
from src.ingestion.import_codex import iter_session_units as iter_codex_units
from src.ingestion.import_codex import load_review_resolutions as load_codex_review_resolutions
from src.ingestion.import_codex import resolve_session_units as resolve_codex_units
from src.ingestion.import_codex import unit_inventory_record as codex_unit_inventory_record
from src.ingestion.import_web_chat import DEFAULT_INPUT as WEB_CHAT_INPUT
from src.ingestion.import_web_chat import DEFAULT_REVIEW_RESOLUTIONS as WEB_CHAT_REVIEW_RESOLUTIONS
from src.ingestion.import_web_chat import REVIEW_SKIP_REASONS as WEB_CHAT_REVIEW_SKIP_REASONS
from src.ingestion.import_web_chat import iter_web_chat_units
from src.ingestion.import_web_chat import load_review_resolutions as load_web_chat_review_resolutions
from src.ingestion.import_web_chat import unit_inventory_record as web_chat_unit_inventory_record
from src.ingestion.import_web_chat import unit_skip_reason as web_chat_unit_skip_reason
from src.ingestion.markdown_sources import discover_markdown
from src.corpus.manifest import load_manifest
from src.corpus.paths import MANIFEST_PATH, REPO_ROOT
from src.corpus.storage import sha256_file


DEFAULT_NOTES_INPUT = REPO_ROOT.parent / "notes"
DEFAULT_ARTICLES_INPUT = REPO_ROOT.parent / "articles"
DEFAULT_OUTPUT = REPO_ROOT / "meta" / "source-inventory.json"
WEB_CHAT_MISSING = [
    "只盘点当前目录中的 Chrome 插件 Markdown 导出，不代表账号全部网页历史",
    "插件未导出的模型、分支、message ID、assistant 时间和隐藏事件无法恢复",
]


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
    return Counter(
        str(item.get("origin", "unknown"))
        for item in manifest["sources"].values()
        if item.get("ingest_status") == "ready" and item.get("output_path")
    )


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


def _markdown_units(
    files: list[Path],
    input_root: Path,
    manifest_sources: dict[str, Any],
    layer: str,
) -> tuple[list[dict[str, Any]], int, Counter[str]]:
    manifest_by_path = {
        Path(str(item.get("source_path") or "")).resolve(): (source_id, item)
        for source_id, item in manifest_sources.items()
        if item.get("layer") == layer
    }
    units: list[dict[str, Any]] = []
    retained = 0
    skip_reasons: Counter[str] = Counter()
    for path in files:
        manifest_record = manifest_by_path.get(path.resolve())
        if manifest_record is None:
            source_id = None
            status = "ready"
            reason = None
        else:
            source_id, item = manifest_record
            status = str(item.get("ingest_status") or "ready")
            reason = item.get("skip_reason") or item.get("ingest_issue")
        if status == "ready":
            retained += 1
        else:
            skip_reasons[str(reason or status)] += 1
        raw = path.read_bytes()
        units.append(
            {
                "source_id": source_id,
                "unit_kind": "document",
                "document_kind": layer,
                "title": path.stem,
                "raw_source_path": str(path.resolve()),
                "raw_source_hash": f"sha256:{sha256_file(path)}",
                "raw_source_locator": (
                    f"{path.relative_to(input_root).as_posix()}@L1-L{max(1, len(raw.splitlines()))}"
                ),
                "parse_status": "imported" if status == "ready" and source_id else status,
                "skip_reason": reason,
            }
        )
    return units, retained, skip_reasons


def _web_chat_records(
    units,
    manifest_sources: dict[str, Any],
    review_resolutions,
) -> list[dict[str, Any]]:
    """把一批已解析的 web-chat 单元投影为 inventory 记录。"""

    records: list[dict[str, Any]] = []
    for unit in units:
        review_resolution = review_resolutions.get(unit.source_id)
        reason = web_chat_unit_skip_reason(unit, review_resolution)
        if reason in WEB_CHAT_REVIEW_SKIP_REASONS:
            status = "review"
        elif reason:
            status = "excluded"
        elif unit.source_id in manifest_sources:
            status = "imported"
        else:
            status = "ready"
        records.append(web_chat_unit_inventory_record(
            unit, status, reason, review_resolution
        ))
    return records


def _normalize_web_chat_collisions(
    records: list[dict[str, Any]],
    manifest_sources: dict[str, Any],
) -> list[dict[str, Any]]:
    """合并局部记录后重新计算跨文件 source ID 冲突。"""

    counts = Counter(str(record.get("source_id") or "") for record in records)
    normalized: list[dict[str, Any]] = []
    for original in records:
        record = deepcopy(original)
        reasons = [
            str(reason)
            for reason in record.get("review_reasons", [])
            if reason != "source_id_collision_review"
        ]
        details = [
            str(detail)
            for detail in record.get("review_details", [])
            if detail != "多个 Markdown 导出解析为同一 provider 会话身份"
        ]
        source_id = str(record.get("source_id") or "")
        original_reason = record.get("skip_reason")
        if counts[source_id] > 1:
            reason = "source_id_collision_review"
            reasons.insert(0, reason)
            details.insert(0, "多个 Markdown 导出解析为同一 provider 会话身份")
        elif original_reason == "source_id_collision_review":
            reason = reasons[0] if reasons else None
        else:
            reason = original_reason
        record["review_reasons"] = reasons
        record["review_details"] = details
        record["skip_reason"] = reason
        record["parse_status"] = (
            "review"
            if reason in WEB_CHAT_REVIEW_SKIP_REASONS
            else "excluded"
            if reason
            else "imported"
            if source_id in manifest_sources
            else "ready"
        )
        normalized.append(record)
    return normalized


def _web_chat_input_record(
    web_chat_input: Path,
    records: list[dict[str, Any]],
    standardized: Counter[str],
    *,
    scanned_at: str | None = None,
) -> dict[str, Any]:
    """从合并后的单元记录重算 web-chat 汇总。"""

    retained = sum(
        record.get("parse_status") in {"ready", "imported"}
        for record in records
    )
    provider_counts = Counter(str(record.get("provider") or "unknown") for record in records)
    skip_reasons = Counter(
        str(record["skip_reason"])
        for record in records
        if record.get("skip_reason")
    )
    result: dict[str, Any] = {
        "provider": "web-chat",
        "input_path": _display_path(web_chat_input),
        "format": "browser-extension-markdown",
        "available": True,
        "date_range": _date_range(str(record.get("created") or "unknown") for record in records),
        "discovered": len(records),
        "retained": retained,
        "skipped": len(records) - retained,
        "provider_counts": dict(sorted(provider_counts.items())),
        "skip_reasons": dict(sorted(skip_reasons.items())),
        "currently_standardized": standardized["web-chat-export"],
        "known_missing": WEB_CHAT_MISSING,
        "units": sorted(
            records,
            key=lambda record: (
                str(record.get("raw_source_path") or ""),
                str(record.get("source_id") or ""),
            ),
        ),
    }
    if scanned_at is not None:
        result["scanned_at"] = scanned_at
    return result


def update_web_chat_inventory(
    existing: dict[str, Any],
    web_chat_input: Path,
    includes: Iterable[str],
    manifest_path: Path = MANIFEST_PATH,
    *,
    review_resolutions: Path = WEB_CHAT_REVIEW_RESOLUTIONS,
    scanned_at: str | None = None,
) -> dict[str, Any]:
    """只重新解析显式 web-chat 文件，并合并回现有全局快照。"""

    if existing.get("version") != 3 or not isinstance(existing.get("inputs"), list):
        raise ValueError("existing source inventory has an unsupported format")
    values = tuple(includes)
    if not values:
        raise ValueError("at least one included web chat file is required")
    inputs = existing["inputs"]
    positions = [
        index for index, item in enumerate(inputs)
        if item.get("provider") == "web-chat"
    ]
    if len(positions) != 1:
        raise ValueError("existing source inventory must contain one web-chat input")
    position = positions[0]
    current = inputs[position]
    if not isinstance(current.get("units"), list):
        raise ValueError("existing web-chat inventory is missing unit records")

    selected_units = list(iter_web_chat_units(web_chat_input, values))
    selected_paths = {str(unit.source_path.resolve()) for unit in selected_units}
    manifest_sources = _manifest_sources(manifest_path)
    resolutions = load_web_chat_review_resolutions(review_resolutions)
    retained_records = [
        deepcopy(record)
        for record in current["units"]
        if str(record.get("raw_source_path") or "") not in selected_paths
    ]
    updated_records = _web_chat_records(
        selected_units, manifest_sources, resolutions
    )
    records = _normalize_web_chat_collisions(
        [*retained_records, *updated_records], manifest_sources
    )
    timestamp = scanned_at or datetime.now(ZoneInfo("Asia/Hong_Kong")).isoformat(timespec="seconds")
    result = deepcopy(existing)
    result["updated_at"] = timestamp
    result["inputs"][position] = _web_chat_input_record(
        web_chat_input,
        records,
        _standardized_counts(manifest_path),
        scanned_at=timestamp,
    )
    return result


def _claude_records_for_update(
    input_root: Path,
    includes: Iterable[str],
    manifest_sources: dict[str, Any],
) -> list[dict[str, Any]]:
    """为显式 Claude Markdown 文件生成 inventory 单元。"""

    records: list[dict[str, Any]] = []
    for unit in iter_export_units(input_root, includes=includes):
        reason = unit_skip_reason(unit)
        manifest_document = manifest_sources.get(unit.source_id, {})
        forced_status = ""
        if manifest_document.get("source_kind") == "document" and manifest_document.get("layer") in {
            "note", "article",
        }:
            ingest_status = str(manifest_document.get("ingest_status") or "")
            if ingest_status == "ready":
                reason = ""
                forced_status = "imported"
            elif ingest_status == "review":
                reason = str(manifest_document.get("ingest_issue") or "document_import_review")
                forced_status = "review"
            elif ingest_status == "skipped":
                reason = str(manifest_document.get("skip_reason") or "document_import_skipped")
                forced_status = "excluded"
        if forced_status:
            status = forced_status
        elif reason == "document_classification_review":
            status = "review"
        elif reason:
            status = "excluded"
        elif unit.source_id in manifest_sources:
            status = "imported"
        elif unit.kind == "document":
            status = "deferred"
        else:
            status = "ready"
        records.append(unit_inventory_record(unit, status, reason))
    return records


def _codex_records_for_update(
    input_root: Path,
    includes: Iterable[str],
    manifest_sources: dict[str, Any],
    review_resolutions: Path,
) -> list[dict[str, Any]]:
    """为显式 Codex rollout 文件生成 inventory 单元。"""

    units = list(iter_codex_units(input_root, includes))
    resolved_units = resolve_codex_units(
        units,
        load_codex_review_resolutions(review_resolutions),
    )
    records: list[dict[str, Any]] = []
    for resolved in resolved_units:
        unit = resolved.unit
        reason = resolved.skip_reason
        status = (
            "review" if reason in CODEX_REVIEW_SKIP_REASONS
            else "deferred" if reason in CODEX_DEFERRED_SKIP_REASONS
            else "excluded" if reason
            else "imported" if unit.source_id in manifest_sources
            else "ready"
        )
        records.append(codex_unit_inventory_record(resolved, status, reason))
    return records


def _updated_input_record(
    current: dict[str, Any],
    input_root: Path,
    records: list[dict[str, Any]],
    standardized_count: int,
    scanned_at: str,
) -> dict[str, Any]:
    """从保留和更新后的单元重算一个来源类别的汇总。"""

    provider = str(current.get("provider") or "")
    records = sorted(
        records,
        key=lambda record: (
            str(record.get("raw_source_path") or ""),
            str(record.get("raw_source_locator") or ""),
            str(record.get("source_id") or ""),
        ),
    )
    retained = sum(not record.get("skip_reason") for record in records)
    skip_reasons = Counter(
        str(record["skip_reason"])
        for record in records
        if record.get("skip_reason")
    )
    result = deepcopy(current)
    result.update({
        "input_path": _display_path(input_root),
        "available": True,
        "discovered": len(records),
        "retained": retained,
        "skipped": len(records) - retained,
        "skip_reasons": dict(sorted(skip_reasons.items())),
        "currently_standardized": standardized_count,
        "units": records,
        "scanned_at": scanned_at,
    })
    if provider not in {"notes", "articles"}:
        result["date_range"] = _date_range(
            str(record.get("created") or "unknown") for record in records
        )
    if provider == "claude-export":
        kinds = Counter(str(record.get("unit_kind") or "") for record in records)
        threads = Counter(
            str(record.get("thread_kind") or "unknown")
            for record in records if record.get("unit_kind") == "session"
        )
        documents = Counter(
            str(record.get("document_kind") or "unknown")
            for record in records if record.get("unit_kind") == "document"
        )
        result["unit_counts"] = {"session": kinds["session"], "document": kinds["document"]}
        result["thread_counts"] = {
            "main": threads["main"], "subagent": threads["subagent"],
            "unknown": threads["unknown"],
        }
        result["document_counts"] = dict(sorted(documents.items()))
    elif provider == "codex":
        threads = Counter(str(record.get("thread_kind") or "unknown") for record in records)
        result["thread_counts"] = {
            "main": threads["main"], "subagent": threads["subagent"],
            "unknown": threads["unknown"],
        }
    return result


def update_inventory(
    existing: dict[str, Any],
    provider: str,
    includes: Iterable[str],
    *,
    claude_input: Path = CLAUDE_INPUT,
    codex_input: Path = CODEX_INPUT,
    notes_input: Path = DEFAULT_NOTES_INPUT,
    articles_input: Path = DEFAULT_ARTICLES_INPUT,
    web_chat_input: Path = WEB_CHAT_INPUT,
    manifest_path: Path = MANIFEST_PATH,
    codex_review_resolutions: Path = CODEX_REVIEW_RESOLUTIONS,
    web_chat_review_resolutions: Path = WEB_CHAT_REVIEW_RESOLUTIONS,
    scanned_at: str | None = None,
) -> dict[str, Any]:
    """按来源类别和显式文件列表更新现有 inventory。"""

    values = tuple(includes)
    if provider == "web-chat":
        return update_web_chat_inventory(
            existing,
            web_chat_input,
            values,
            manifest_path,
            review_resolutions=web_chat_review_resolutions,
            scanned_at=scanned_at,
        )
    if existing.get("version") != 3 or not isinstance(existing.get("inputs"), list):
        raise ValueError("existing source inventory has an unsupported format")
    if not values:
        raise ValueError("at least one included source file is required")
    positions = [
        index for index, item in enumerate(existing["inputs"])
        if item.get("provider") == provider
    ]
    if len(positions) != 1:
        raise ValueError(f"existing source inventory must contain one {provider} input")
    position = positions[0]
    current = existing["inputs"][position]
    if not isinstance(current.get("units"), list):
        raise ValueError(f"existing {provider} inventory is missing unit records")

    roots = {
        "claude-export": (claude_input, ".md", "claude-export"),
        "codex": (codex_input, ".jsonl", "codex"),
        "notes": (notes_input, ".md", "personal-notes"),
        "articles": (articles_input, ".md", "external-articles"),
    }
    if provider not in roots:
        raise ValueError(f"unsupported scoped inventory provider: {provider}")
    input_root, suffix, standardized_origin = roots[provider]
    root = input_root.resolve(strict=True)
    selected_paths: set[str] = set()
    for value in values:
        path = (root / value).resolve(strict=True)
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"included path escapes the input directory: {value}") from exc
        if not path.is_file() or path.suffix.lower() != suffix:
            raise FileNotFoundError(f"included source file does not exist: {value}")
        selected_paths.add(str(path))

    manifest_sources = _manifest_sources(manifest_path)
    if provider == "claude-export":
        updated_records = _claude_records_for_update(
            input_root, values, manifest_sources
        )
    elif provider == "codex":
        updated_records = _codex_records_for_update(
            input_root, values, manifest_sources, codex_review_resolutions
        )
    else:
        updated_records, _, _ = _markdown_units(
            discover_markdown(input_root, values),
            input_root,
            manifest_sources,
            "note" if provider == "notes" else "article",
        )
    retained_records = [
        deepcopy(record)
        for record in current["units"]
        if str(record.get("raw_source_path") or "") not in selected_paths
    ]
    timestamp = scanned_at or datetime.now(ZoneInfo("Asia/Hong_Kong")).isoformat(timespec="seconds")
    result = deepcopy(existing)
    result["updated_at"] = timestamp
    result["inputs"][position] = _updated_input_record(
        current,
        input_root,
        [*retained_records, *updated_records],
        _standardized_counts(manifest_path)[standardized_origin],
        timestamp,
    )
    return result


def build_inventory(
    claude_input: Path,
    codex_input: Path,
    notes_input: Path,
    manifest_path: Path = MANIFEST_PATH,
    *,
    articles_input: Path | None = None,
    web_chat_input: Path | None = None,
    codex_review_resolutions: Path = CODEX_REVIEW_RESOLUTIONS,
    web_chat_review_resolutions: Path = WEB_CHAT_REVIEW_RESOLUTIONS,
    scanned_at: str | None = None,
) -> dict[str, Any]:
    articles_input = articles_input or notes_input.parent / "articles"
    web_chat_input = web_chat_input or notes_input.parent / "web-chats"
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
            manifest_document = manifest_sources.get(unit.source_id, {})
            forced_status = ""
            if manifest_document.get("source_kind") == "document" and manifest_document.get("layer") in {
                "note",
                "article",
            }:
                ingest_status = str(manifest_document.get("ingest_status") or "")
                if ingest_status == "ready":
                    reason = ""
                    forced_status = "imported"
                elif ingest_status == "review":
                    reason = str(manifest_document.get("ingest_issue") or "document_import_review")
                    forced_status = "review"
                elif ingest_status == "skipped":
                    reason = str(manifest_document.get("skip_reason") or "document_import_skipped")
                    forced_status = "excluded"
            if reason:
                skip_reasons[reason] += 1
            else:
                retained += 1
            if forced_status:
                status = forced_status
            elif reason == "document_classification_review":
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

    if not web_chat_input.is_dir():
        inputs.append(
            _missing_input(
                "web-chat",
                web_chat_input,
                "browser-extension-markdown",
                WEB_CHAT_MISSING,
            )
        )
    else:
        web_chat_units = list(iter_web_chat_units(web_chat_input))
        web_chat_resolutions = load_web_chat_review_resolutions(web_chat_review_resolutions)
        inputs.append(_web_chat_input_record(
            web_chat_input,
            _normalize_web_chat_collisions(
                _web_chat_records(
                    web_chat_units, manifest_sources, web_chat_resolutions
                ),
                manifest_sources,
            ),
            standardized,
            scanned_at=scanned_at,
        ))

    codex_missing = ["其他设备或账号的 Codex rollout 不在本次范围"]
    if not codex_input.is_dir():
        inputs.append(_missing_input("codex", codex_input, "codex-rollout-jsonl", codex_missing))
    else:
        codex_units = list(iter_codex_units(codex_input))
        codex_resolved = resolve_codex_units(
            codex_units,
            load_codex_review_resolutions(codex_review_resolutions),
        )
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
        note_units, note_retained, note_skips = _markdown_units(
            note_files, notes_input, manifest_sources, "note"
        )
        inputs.append(
            {
                "provider": "notes",
                "input_path": _display_path(notes_input),
                "format": "markdown",
                "available": True,
                "date_range": {"from": None, "to": None},
                "discovered": len(note_files),
                "retained": note_retained,
                "skipped": len(note_files) - note_retained,
                "skip_reasons": dict(sorted(note_skips.items())),
                "assets_discovered": len(asset_files),
                "currently_standardized": standardized["personal-notes"],
                "known_missing": notes_missing,
                "units": note_units,
            }
        )

    articles_missing = ["远程图片和链接只记录引用，inventory 不下载或联网检查"]
    if not articles_input.is_dir():
        inputs.append(_missing_input("articles", articles_input, "markdown", articles_missing))
    else:
        article_files = [
            path
            for path in sorted(articles_input.rglob("*.md"))
            if not any(part.startswith(".") for part in path.relative_to(articles_input).parts)
        ]
        article_units, retained, article_skips = _markdown_units(
            article_files, articles_input, manifest_sources, "article"
        )
        inputs.append(
            {
                "provider": "articles",
                "input_path": _display_path(articles_input),
                "format": "markdown",
                "available": True,
                "date_range": {"from": None, "to": None},
                "discovered": len(article_files),
                "retained": retained,
                "skipped": len(article_files) - retained,
                "skip_reasons": dict(sorted(article_skips.items())),
                "currently_standardized": standardized["external-articles"],
                "known_missing": articles_missing,
                "units": article_units,
            }
        )

    for item in inputs:
        item.setdefault("scanned_at", scanned_at)
        if item["discovered"] != item["retained"] + item["skipped"]:
            raise ValueError(f"{item['provider']} inventory counts are inconsistent")
    return {
        "version": 3,
        "scanned_at": scanned_at,
        "updated_at": scanned_at,
        "coverage_boundary": (
            "只覆盖当前机器上可访问的五类已知输入位置，不代表所有历史会话已提供；"
            "Claude 和 web-chat 事件计数只表示现有有损 Markdown 导出中仍可见的内容。"
        ),
        "known_missing": [
            "未导出到 web-chats/ 的 ChatGPT/DeepSeek 历史尚未提供",
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
    parser = argparse.ArgumentParser(description="Build the source coverage inventory.")
    parser.add_argument("--claude-input", type=Path, default=CLAUDE_INPUT)
    parser.add_argument("--codex-input", type=Path, default=CODEX_INPUT)
    parser.add_argument("--notes-input", type=Path, default=DEFAULT_NOTES_INPUT)
    parser.add_argument("--articles-input", type=Path, default=DEFAULT_ARTICLES_INPUT)
    parser.add_argument("--web-chat-input", type=Path, default=WEB_CHAT_INPUT)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument(
        "--codex-review-resolutions",
        type=Path,
        default=CODEX_REVIEW_RESOLUTIONS,
    )
    parser.add_argument(
        "--web-chat-review-resolutions",
        type=Path,
        default=WEB_CHAT_REVIEW_RESOLUTIONS,
    )
    parser.add_argument(
        "--provider",
        choices=("claude-export", "web-chat", "codex", "notes", "articles"),
        help="update only one provider in the existing inventory",
    )
    parser.add_argument(
        "--include",
        action="append",
        help="update only the specified relative raw input path; repeatable",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if bool(args.provider) != bool(args.include):
        parser.error("--provider and at least one --include must be used together")
    if args.provider is not None:
        if not args.output.is_file():
            parser.error("scoped inventory update requires an existing output file")
        try:
            existing = json.loads(args.output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            parser.error(f"cannot read existing source inventory: {exc}")
        inventory = update_inventory(
            existing,
            args.provider,
            args.include,
            claude_input=args.claude_input,
            codex_input=args.codex_input,
            notes_input=args.notes_input,
            articles_input=args.articles_input,
            web_chat_input=args.web_chat_input,
            manifest_path=args.manifest,
            codex_review_resolutions=args.codex_review_resolutions,
            web_chat_review_resolutions=args.web_chat_review_resolutions,
        )
    else:
        inventory = build_inventory(
            args.claude_input,
            args.codex_input,
            args.notes_input,
            args.manifest,
            articles_input=args.articles_input,
            web_chat_input=args.web_chat_input,
            codex_review_resolutions=args.codex_review_resolutions,
            web_chat_review_resolutions=args.web_chat_review_resolutions,
        )
    if not args.dry_run:
        save_inventory(inventory, args.output)
    selected = (
        inventory["inputs"]
        if args.provider is None
        else [item for item in inventory["inputs"] if item["provider"] == args.provider]
    )
    summary = ", ".join(
        f"{item['provider']}={item['discovered']}/{item['retained']}/{item['skipped']}" for item in selected
    )
    print(f"Source inventory ({'dry-run' if args.dry_run else 'written'}): {summary}")


if __name__ == "__main__":
    main()
