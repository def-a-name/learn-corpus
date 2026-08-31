#!/usr/bin/env python3
"""检查来源与派生状态完整性。"""

from __future__ import annotations

import argparse
import re
from collections import Counter
from pathlib import Path
from typing import Any

from scripts.common.ingest_log import check_ingest_log
from scripts.common.corpus_core import (
    ALLOWED_CURATION_STATUSES,
    ALLOWED_INGEST_STATUSES,
    CLAUDE_ASSISTANT_FINAL_DETECTION,
    CODEX_ASSISTANT_FINAL_DETECTION,
    MANIFEST_PATH,
    REPO_ROOT,
    WEB_CHAT_ASSISTANT_FINAL_DETECTION,
    load_manifest,
    parse_frontmatter,
    redact_secrets,
    sha256_file,
)


def markdown_files(root: Path) -> list[Path]:
    return [
        path
        for path in sorted(root.rglob("*.md"))
        if not any(part.startswith(".") for part in path.relative_to(root).parts)
    ]


def _normalized_hash(value: Any) -> str:
    return str(value or "").removeprefix("sha256:")


def _resolve_repo_path(value: Any, repo_root: Path) -> Path:
    path = Path(str(value or ""))
    return path if path.is_absolute() else repo_root / path


def _claude_session_units(manifest: dict[str, Any]) -> dict[str, Any]:
    from scripts.ingest.import_claude import iter_export_units

    roots: set[Path] = set()
    for item in manifest["sources"].values():
        if item.get("origin") != "claude-export" or item.get("source_kind") != "session":
            continue
        locator = str(item.get("source_locator", ""))
        relative_path, marker, _ = locator.partition("#Session:")
        raw_path = Path(str(item.get("source_path", "")))
        if not marker or not raw_path.is_file():
            continue
        root = raw_path
        for _ in Path(relative_path).parts:
            root = root.parent
        roots.add(root)

    units: dict[str, Any] = {}
    for root in roots:
        if root.is_dir():
            units.update({unit.source_id: unit for unit in iter_export_units(root, kind="session")})
    return units


def _codex_session_units(
    manifest: dict[str, Any],
    review_resolutions_path: Path | None = None,
) -> dict[str, Any]:
    from scripts.ingest.import_codex import DEFAULT_REVIEW_RESOLUTIONS, iter_resolved_session_units

    review_resolutions_path = review_resolutions_path or DEFAULT_REVIEW_RESOLUTIONS

    roots: set[Path] = set()
    for item in manifest["sources"].values():
        if item.get("origin") != "codex" or item.get("source_kind") != "session":
            continue
        locator = str(item.get("source_locator", ""))
        relative_path, marker, _ = locator.partition("#Session:")
        raw_path = Path(str(item.get("source_path", "")))
        if not marker or not raw_path.is_file():
            continue
        root = raw_path
        for _ in Path(relative_path).parts:
            root = root.parent
        roots.add(root)

    units: dict[str, Any] = {}
    for root in roots:
        if root.is_dir():
            units.update(
                {
                    resolved.unit.source_id: resolved
                    for resolved in iter_resolved_session_units(root, review_resolutions_path)
                }
            )
    return units


def _web_chat_units(manifest: dict[str, Any]) -> dict[str, Any]:
    from scripts.ingest.import_web_chat import iter_web_chat_units

    roots: set[Path] = set()
    for item in manifest["sources"].values():
        if item.get("origin") != "web-chat-export" or item.get("source_kind") != "session":
            continue
        locator = str(item.get("source_locator", ""))
        relative_path, marker, _ = locator.partition("#Conversation:")
        raw_path = Path(str(item.get("source_path", "")))
        if not marker or not raw_path.is_file():
            continue
        root = raw_path
        for _ in Path(relative_path).parts:
            root = root.parent
        roots.add(root)

    units: dict[str, Any] = {}
    for root in roots:
        if root.is_dir():
            units.update({unit.source_id: unit for unit in iter_web_chat_units(root)})
    return units


def check_source_consistency(
    manifest_path: Path = MANIFEST_PATH,
    sources_root: Path = REPO_ROOT / "sources",
    repo_root: Path = REPO_ROOT,
    codex_review_resolutions_path: Path | None = None,
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        manifest = load_manifest(manifest_path)
    except (OSError, ValueError) as exc:
        return [str(exc)], warnings

    source_files: dict[str, tuple[Path, dict[str, Any], str]] = {}
    for path in markdown_files(sources_root):
        try:
            metadata, body = parse_frontmatter(path)
        except (OSError, ValueError) as exc:
            errors.append(str(exc))
            continue
        source_id = str(metadata.get("id") or "")
        if not source_id:
            errors.append(f"{path}: 标准化来源缺少 id")
            continue
        if source_id in source_files:
            errors.append(f"重复来源 ID: {source_id}")
            continue
        source_files[source_id] = (path, metadata, body)

    manifest_sources = manifest["sources"]
    manifest_version = int(manifest.get("version", 1))
    for source_id, (path, _, _) in source_files.items():
        if source_id not in manifest_sources:
            errors.append(f"{path}: orphan source，manifest 中不存在 {source_id}")

    output_paths: list[str] = []
    claude_units = _claude_session_units(manifest)
    codex_units = _codex_session_units(manifest, codex_review_resolutions_path)
    web_chat_units = _web_chat_units(manifest)
    sources_root_resolved = sources_root.resolve()
    for source_id, item in manifest_sources.items():
        if manifest_version >= 2:
            ingest_status = str(item.get("ingest_status") or "")
            curation_status = str(item.get("curation_status") or "")
            if ingest_status not in ALLOWED_INGEST_STATUSES:
                errors.append(f"manifest {source_id}: 缺少或未知 ingest_status={ingest_status or 'missing'}")
            if curation_status not in ALLOWED_CURATION_STATUSES:
                errors.append(f"manifest {source_id}: 缺少或未知 curation_status={curation_status or 'missing'}")
        output_value = str(item.get("output_path") or "")
        if not output_value:
            if ingest_status == "ready":
                errors.append(f"manifest {source_id}: ready 来源缺少 output_path")
            elif ingest_status == "skipped":
                if item.get("skip_reason") == "exact_duplicate":
                    duplicate_id = str(item.get("duplicate_of") or "")
                    duplicate = manifest_sources.get(duplicate_id, {})
                    if not duplicate_id or duplicate.get("ingest_status") != "ready":
                        errors.append(f"manifest {source_id}: exact duplicate 缺少 ready duplicate_of")
                    if item.get("layer") != duplicate.get("layer") or item.get("content_fingerprint") != duplicate.get(
                        "content_fingerprint"
                    ):
                        errors.append(f"manifest {source_id}: exact duplicate 的 layer/fingerprint 不一致")
                elif not item.get("skip_reason"):
                    errors.append(f"manifest {source_id}: skipped 来源缺少 skip_reason")
            elif ingest_status == "review" and not item.get("ingest_issue"):
                errors.append(f"manifest {source_id}: review 来源缺少 ingest_issue")
            continue
        output_path = _resolve_repo_path(output_value, repo_root)
        output_paths.append(str(output_path.resolve()))
        if not output_path.is_file():
            errors.append(f"manifest {source_id}: 标准化来源不存在 {output_value}")
            continue
        try:
            output_path.resolve().relative_to(sources_root_resolved)
        except ValueError:
            errors.append(f"manifest {source_id}: output_path 不在 sources/ 内 {output_value}")
        source_record = source_files.get(source_id)
        if source_record is None:
            errors.append(f"manifest {source_id}: output_path 中没有匹配的来源 ID")
            continue
        actual_path, metadata, body = source_record
        if actual_path.resolve() != output_path.resolve():
            errors.append(f"manifest {source_id}: output_path 指向错误 {output_value}")
        for field in (
            "origin",
            "layer",
            "title",
            "source_path",
            "source_locator",
            "assistant_final_detection",
            "source_scope",
            "omitted_trivial_exchange_count",
            "omitted_unpaired_user_count",
            "omitted_fork_prefix_exchange_count",
            "invalid_jsonl_lines",
            "active_open_turn_count",
            "superseded_incomplete_turn_count",
            "review_resolution",
            "reviewed_at",
            "reviewed_by",
            "reviewed_invalid_jsonl_lines",
            "reviewed_superseded_incomplete_turn_count",
        ):
            manifest_value = item.get(field)
            metadata_value = metadata.get(field)
            normalized_metadata = "" if metadata_value in (None, "") else str(metadata_value)
            if manifest_value not in (None, "") and str(manifest_value) != normalized_metadata:
                errors.append(f"manifest {source_id}: {field} 与来源 front matter 不一致")
        manifest_hash = _normalized_hash(item.get("source_hash"))
        metadata_hash = _normalized_hash(metadata.get("source_hash"))
        if not manifest_hash or manifest_hash != metadata_hash:
            errors.append(f"manifest {source_id}: source_hash 与来源 front matter 不一致")
            continue
        manifest_fingerprint = _normalized_hash(item.get("content_fingerprint"))
        metadata_fingerprint = _normalized_hash(metadata.get("content_fingerprint"))
        if manifest_fingerprint and manifest_fingerprint != metadata_fingerprint:
            errors.append(f"manifest {source_id}: content_fingerprint 与来源 front matter 不一致")
        manifest_resolution_hash = _normalized_hash(item.get("review_resolution_hash"))
        metadata_resolution_hash = _normalized_hash(metadata.get("review_resolution_hash"))
        if manifest_resolution_hash != metadata_resolution_hash:
            errors.append(f"manifest {source_id}: review_resolution_hash 与来源 front matter 不一致")
        for asset in item.get("assets") or []:
            asset_path = _resolve_repo_path(str(asset.get("stored_path") or ""), repo_root)
            if not asset_path.is_file():
                errors.append(f"manifest {source_id}: 本地资源不存在 {asset.get('stored_path')}" )
            elif sha256_file(asset_path) != str(asset.get("asset_hash") or ""):
                errors.append(f"manifest {source_id}: 本地资源 hash 已过期 {asset.get('stored_path')}" )

        if item.get("origin") == "claude-export" and item.get("source_kind") == "session":
            if item.get("assistant_final_detection") != CLAUDE_ASSISTANT_FINAL_DETECTION:
                errors.append(f"manifest {source_id}: manifest 缺少或未知 assistant_final_detection")
            if metadata.get("assistant_final_detection") != CLAUDE_ASSISTANT_FINAL_DETECTION:
                errors.append(f"manifest {source_id}: source 缺少或未知 assistant_final_detection")
            if "Assistant phase confidence" in body:
                errors.append(f"manifest {source_id}: exchange 正文仍包含旧 Assistant phase confidence")
            unit = claude_units.get(source_id)
            parsed = unit.session_parse if unit is not None else None
            if unit is None or parsed is None:
                errors.append(f"manifest {source_id}: Claude session locator 无法在原始导出中解析")
                continue
            if str(item.get("source_locator") or "") != unit.locator:
                errors.append(f"manifest {source_id}: Claude session locator 与原始导出行范围不一致")
            actual_user_locators = re.findall(r"^- \*\*User locator\*\*: `([^`]+)`$", body, flags=re.MULTILINE)
            actual_assistant_locators = re.findall(
                r"^- \*\*Assistant locator\*\*: `([^`]+)`$", body, flags=re.MULTILINE
            )
            if actual_user_locators != [exchange.user_locator for exchange in parsed.exchanges]:
                errors.append(f"manifest {source_id}: Claude user turn locator 与原始导出不一致")
            if actual_assistant_locators != [exchange.assistant_locator for exchange in parsed.exchanges]:
                errors.append(f"manifest {source_id}: Claude assistant final locator 与原始导出不一致")
            if int(metadata.get("exchange_count") or 0) != body.count("\n## Exchange "):
                errors.append(f"manifest {source_id}: exchange_count 与标准化正文不一致")
            if int(metadata.get("omitted_trivial_exchange_count") or 0) != parsed.omitted_trivial_exchange_count:
                errors.append(f"manifest {source_id}: 纯寒暄/测试 exchange 省略计数与原始导出不一致")

        if item.get("origin") == "codex" and item.get("source_kind") == "session":
            if item.get("assistant_final_detection") != CODEX_ASSISTANT_FINAL_DETECTION:
                errors.append(f"manifest {source_id}: manifest 缺少或未知 Codex assistant_final_detection")
            if metadata.get("assistant_final_detection") != CODEX_ASSISTANT_FINAL_DETECTION:
                errors.append(f"manifest {source_id}: source 缺少或未知 Codex assistant_final_detection")
            resolved = codex_units.get(source_id)
            if resolved is None:
                errors.append(f"manifest {source_id}: Codex session locator 无法在原始 rollout 中解析")
                continue
            unit = resolved.unit
            expected_resolution_hash = (
                resolved.review_resolution.content_hash if resolved.review_resolution else ""
            )
            if manifest_resolution_hash != expected_resolution_hash:
                errors.append(f"manifest {source_id}: Codex review resolution 与当前处置记录不一致")
            if str(item.get("provider_session_id") or "") != unit.provider_session_id:
                errors.append(f"manifest {source_id}: Codex provider session ID 与原始 rollout 不一致")
            if str(item.get("source_locator") or "") != unit.locator:
                errors.append(f"manifest {source_id}: Codex session locator 与原始 rollout 行范围不一致")
            actual_user_locators = re.findall(r"^- \*\*User locator\*\*: `([^`]+)`$", body, flags=re.MULTILINE)
            actual_assistant_locators = re.findall(
                r"^- \*\*Assistant locator\*\*: `([^`]+)`$", body, flags=re.MULTILINE
            )
            if actual_user_locators != [exchange.user_locator for exchange in resolved.exchanges]:
                errors.append(f"manifest {source_id}: Codex user turn locator 与原始 rollout 不一致")
            if actual_assistant_locators != [exchange.assistant_locator for exchange in resolved.exchanges]:
                errors.append(f"manifest {source_id}: Codex assistant final locator 与原始 rollout 不一致")
            if int(metadata.get("exchange_count") or 0) != body.count("\n## Exchange "):
                errors.append(f"manifest {source_id}: Codex exchange_count 与标准化正文不一致")
            if int(metadata.get("omitted_trivial_exchange_count") or 0) != unit.omitted_trivial_exchange_count:
                errors.append(f"manifest {source_id}: Codex 纯寒暄/测试 exchange 省略计数与 raw rollout 不一致")
            if str(metadata.get("source_scope") or "") != resolved.source_scope:
                errors.append(f"manifest {source_id}: Codex source_scope 与 fork 解析结果不一致")
            expected_title, _ = redact_secrets(resolved.title)
            if str(metadata.get("title") or "") != expected_title:
                errors.append(f"manifest {source_id}: Codex 标题与实际导入范围不一致")
            if (
                int(metadata.get("omitted_fork_prefix_exchange_count") or 0)
                != resolved.omitted_fork_prefix_exchange_count
            ):
                errors.append(f"manifest {source_id}: Codex fork 共同前缀省略计数不一致")
            if unit.forked_from_id:
                if str(metadata.get("forked_from_id") or "") != unit.forked_from_id:
                    errors.append(f"manifest {source_id}: Codex fork 父会话 ID 不一致")
                if str(metadata.get("fork_parent_source_id") or "") != str(
                    resolved.fork_parent_source_id or ""
                ):
                    errors.append(f"manifest {source_id}: Codex fork 父 source ID 不一致")
                if _normalized_hash(metadata.get("fork_parent_hash")) != resolved.fork_parent_hash:
                    errors.append(f"manifest {source_id}: Codex fork 父会话 hash 不一致")
                if str(item.get("forked_from_id") or "") != unit.forked_from_id:
                    errors.append(f"manifest {source_id}: manifest fork 父会话 ID 不一致")
                if str(item.get("fork_parent_source_id") or "") != str(
                    resolved.fork_parent_source_id or ""
                ):
                    errors.append(f"manifest {source_id}: manifest fork 父 source ID 不一致")
                if _normalized_hash(item.get("fork_parent_hash")) != resolved.fork_parent_hash:
                    errors.append(f"manifest {source_id}: manifest fork 父会话 hash 不一致")

        if item.get("origin") == "web-chat-export" and item.get("source_kind") == "session":
            if item.get("assistant_final_detection") != WEB_CHAT_ASSISTANT_FINAL_DETECTION:
                errors.append(f"manifest {source_id}: manifest 缺少或未知 web-chat assistant_final_detection")
            if metadata.get("assistant_final_detection") != WEB_CHAT_ASSISTANT_FINAL_DETECTION:
                errors.append(f"manifest {source_id}: source 缺少或未知 web-chat assistant_final_detection")
            unit = web_chat_units.get(source_id)
            parsed = unit.parse if unit is not None else None
            if unit is None or parsed is None:
                errors.append(f"manifest {source_id}: web-chat session locator 无法在原始导出中解析")
                continue
            if str(item.get("provider") or "") != unit.provider:
                errors.append(f"manifest {source_id}: web-chat provider 与原始导出不一致")
            if str(item.get("provider_session_id") or "") != unit.provider_session_id:
                errors.append(f"manifest {source_id}: web-chat provider session ID 与原始导出不一致")
            if str(item.get("provider_share_id") or "") != unit.provider_share_id:
                errors.append(f"manifest {source_id}: web-chat provider share ID 与原始导出不一致")
            if str(item.get("source_locator") or "") != unit.locator:
                errors.append(f"manifest {source_id}: web-chat session locator 与原始导出行范围不一致")
            actual_user_locators = re.findall(
                r"^- \*\*User locator\*\*: `([^`]+)`$", body, flags=re.MULTILINE
            )
            actual_assistant_locators = re.findall(
                r"^- \*\*Assistant locator\*\*: `([^`]+)`$", body, flags=re.MULTILINE
            )
            if actual_user_locators != [exchange.user_locator for exchange in parsed.exchanges]:
                errors.append(f"manifest {source_id}: web-chat user turn locator 与原始导出不一致")
            if actual_assistant_locators != [
                exchange.assistant_locator for exchange in parsed.exchanges
            ]:
                errors.append(f"manifest {source_id}: web-chat assistant final locator 与原始导出不一致")
            if int(metadata.get("exchange_count") or 0) != body.count("\n## Exchange "):
                errors.append(f"manifest {source_id}: web-chat exchange_count 与标准化正文不一致")
            if (
                int(metadata.get("omitted_unpaired_user_count") or 0)
                != parsed.omitted_unpaired_user_count
            ):
                errors.append(f"manifest {source_id}: web-chat 未配对 user 省略计数不一致")

        raw_path = Path(str(item.get("source_path") or ""))
        if not raw_path.is_file():
            errors.append(f"manifest {source_id}: 原始来源不存在 {raw_path}")
            continue
        if raw_path.resolve() == output_path.resolve():
            errors.append(f"manifest {source_id}: 原始来源不能与标准化输出为同一文件")
            continue
        elif item.get("origin") == "claude-export" and item.get("source_kind") == "session":
            current_hash = claude_units[source_id].content_hash
        else:
            current_hash = sha256_file(raw_path)
        if current_hash != manifest_hash:
            errors.append(f"manifest {source_id}: 原始来源 hash 已过期")

    for output_path, count in Counter(output_paths).items():
        if count > 1:
            errors.append(f"manifest 重复 output_path: {output_path}")
    return errors, warnings


def check(
    manifest_path: Path = MANIFEST_PATH,
    sources_root: Path = REPO_ROOT / "sources",
) -> tuple[list[str], list[str]]:
    errors, warnings = check_source_consistency(manifest_path, sources_root, REPO_ROOT)
    ingest_errors, ingest_warnings = check_ingest_log(REPO_ROOT)
    errors.extend(ingest_errors)
    warnings.extend(ingest_warnings)
    for vault_config in REPO_ROOT.rglob(".obsidian"):
        if vault_config != REPO_ROOT / ".obsidian":
            warnings.append(f"发现嵌套 Obsidian Vault 配置: {vault_config.relative_to(REPO_ROOT)}")
    return errors, warnings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, default=REPO_ROOT / "sources")
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    args = parser.parse_args()
    errors, warnings = check(args.manifest, args.sources)
    for warning in warnings:
        print(f"WARNING: {warning}")
    for error in errors:
        print(f"ERROR: {error}")
    print(f"检查完成: errors={len(errors)}, warnings={len(warnings)}")
    raise SystemExit(1 if errors else 0)


if __name__ == "__main__":
    main()
