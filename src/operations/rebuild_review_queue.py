#!/usr/bin/env python3
"""根据 inventory 和 manifest 重建来源导入问题队列。"""

from __future__ import annotations

import argparse
import json
import shlex
from pathlib import Path
from typing import Any

from src.shared.corpus_core import MANIFEST_PATH, REPO_ROOT, load_manifest, parse_line_locator


DEFAULT_INVENTORY = REPO_ROOT / "meta" / "source-inventory.json"
DEFAULT_OUTPUT = REPO_ROOT / "meta" / "source-review-queue.md"


def _review_items(manifest_path: Path, inventory_path: Path) -> list[dict[str, Any]]:
    manifest = load_manifest(manifest_path)
    by_id: dict[str, dict[str, Any]] = {}
    for source_id, value in manifest["sources"].items():
        legacy_review = value.get("status") in {"review", "conflict"}
        if value.get("ingest_status") != "review" and not legacy_review:
            continue
        by_id[source_id] = {
            "source_id": source_id,
            "title": value.get("title") or source_id,
            "provider": value.get("origin", "unknown"),
            "created": value.get("created", "unknown"),
            "output_path": value.get("output_path"),
            "raw_source_path": value.get("source_path"),
            "raw_source_locator": value.get("source_locator"),
            "reason": value.get("ingest_issue") or value.get("status") or "manifest_ingest_review",
        }

    if inventory_path.is_file():
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        for source_input in inventory.get("inputs", []):
            input_provider = source_input.get("provider", "unknown")
            for unit in source_input.get("units", []):
                if unit.get("parse_status") != "review":
                    continue
                source_id = str(unit.get("source_id") or "")
                if not source_id:
                    continue
                by_id[source_id] = {
                    "source_id": source_id,
                    "title": unit.get("title") or source_id,
                    "provider": unit.get("provider") or input_provider,
                    "created": unit.get("created", "unknown"),
                    "output_path": None,
                    "raw_source_path": unit.get("raw_source_path"),
                    "raw_source_locator": unit.get("raw_source_locator"),
                    "reason": unit.get("skip_reason") or "inventory_parse_review",
                    "review_details": unit.get("review_details") or [],
                }
    return sorted(by_id.values(), key=lambda item: (str(item.get("created", "")), item["source_id"]))


def _raw_location_lines(item: dict[str, Any]) -> list[str]:
    raw_path = str(item.get("raw_source_path") or "unknown")
    raw_locator = str(item.get("raw_source_locator") or "unknown")
    _, start_line, end_line = parse_line_locator(raw_locator)
    lines = [
        f"- **Raw path**：`{raw_path}`",
        f"- **Raw locator**：`{raw_locator}`",
    ]
    if raw_path != "unknown" and start_line is not None and end_line is not None:
        quoted_path = shlex.quote(raw_path)
        quoted_locator = shlex.quote(raw_locator)
        lines.extend(
            (
                f"- **Raw lines**：{start_line}–{end_line}",
                f"- **打开原文**：[{Path(raw_path).name}:{start_line}](<{raw_path}:{start_line}>)",
                "- **CLI**：`python3 -m src.ingestion.read_raw_locator "
                f"{quoted_locator} --raw-path {quoted_path} --line-numbers`",
            )
        )
    return lines


def build_queue(
    manifest_path: Path,
    output_path: Path,
    inventory_path: Path = DEFAULT_INVENTORY,
    dry_run: bool = False,
) -> str:
    items = _review_items(manifest_path, inventory_path)
    lines = [
        "# 来源导入审核队列",
        "",
        "<!-- 此文件由 src.operations.rebuild_review_queue 生成。 -->",
        "",
        "这里只列出会阻塞来源正确性的解析、角色、locator、敏感信息或完整性问题。",
        "`ready + unassessed` 的普通来源不会进入本队列。",
        "",
    ]
    if not items:
        lines.append("当前没有来源导入问题。")
    else:
        for item in items:
            lines.extend(
                (
                    f"## {item['title']}",
                    "",
                    f"- [ ] **来源 ID**：`{item['source_id']}`",
                    f"- **Provider**：`{item['provider']}`",
                    f"- **日期**：{item['created']}",
                    f"- **原因**：`{item['reason']}`",
                )
            )
            lines.extend(_raw_location_lines(item))
            details = item.get("review_details") or []
            if details:
                lines.append("- **审核线索**：")
                lines.extend(f"  - {detail}" for detail in details)
            lines.extend(
                (
                    f"- **标准化文件**：`{item['output_path']}`" if item.get("output_path") else "- **标准化文件**：未生成",
                    "- **处理结果**：待人工核对后填写",
                    "",
                )
            )
    content = "\n".join(lines).rstrip() + "\n"
    if not dry_run:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content, encoding="utf-8")
    return content


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild the source review queue.")
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    content = build_queue(args.manifest, args.output, args.inventory, args.dry_run)
    issue_count = content.count("**来源 ID**")
    print(f"Source review queue issues: {issue_count}")


if __name__ == "__main__":
    main()
