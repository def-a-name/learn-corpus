#!/usr/bin/env python3
"""按 Claude raw locator 读取冻结 Markdown 中的精确原文范围。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from scripts.common.wiki_core import parse_line_locator
from scripts.ingest.import_claude import DEFAULT_INPUT, iter_export_units


class LocatorNotFoundError(ValueError):
    pass


def resolve_locator(input_dir: Path, locator: str) -> dict[str, Any]:
    if not input_dir.is_dir():
        raise FileNotFoundError(f"Claude 导出目录不存在: {input_dir}")
    semantic_locator, requested_start, requested_end = parse_line_locator(locator)
    matched_unit = None
    matched_locator = ""
    matched_kind = ""

    for unit in iter_export_units(input_dir):
        unit_semantic, _, _ = parse_line_locator(unit.locator)
        if semantic_locator == unit_semantic:
            matched_unit = unit
            matched_locator = unit.locator
            matched_kind = unit.kind
            break
        if unit.session_parse is None or not semantic_locator.startswith(unit_semantic + "/"):
            continue
        for exchange in unit.session_parse.exchanges:
            for candidate, kind in (
                (exchange.user_locator, "human_user"),
                (exchange.assistant_locator, "assistant_final"),
            ):
                candidate_semantic, _, _ = parse_line_locator(candidate)
                if semantic_locator == candidate_semantic:
                    matched_unit = unit
                    matched_locator = candidate
                    matched_kind = kind
                    break
            if matched_unit is not None:
                break
        if matched_unit is not None:
            break

    if matched_unit is None:
        raise LocatorNotFoundError(f"locator 无法解析: {locator}")

    _, start_line, end_line = parse_line_locator(matched_locator)
    if start_line is None or end_line is None:
        raise LocatorNotFoundError(f"locator 缺少行范围: {matched_locator}")
    raw_path = matched_unit.source_path.resolve()
    raw_lines = raw_path.read_text(encoding="utf-8", errors="replace").splitlines()
    if end_line > len(raw_lines):
        raise LocatorNotFoundError(
            f"locator 行范围超出文件: {matched_locator}，文件共 {len(raw_lines)} 行"
        )
    content = "\n".join(raw_lines[start_line - 1 : end_line])
    requested_range_matches = (
        requested_start is None
        or (requested_start == start_line and requested_end == end_line)
    )
    return {
        "requested_locator": locator,
        "locator": matched_locator,
        "semantic_locator": semantic_locator,
        "kind": matched_kind,
        "source_id": matched_unit.source_id,
        "title": matched_unit.title,
        "path": str(raw_path),
        "start_line": start_line,
        "end_line": end_line,
        "requested_range_matches": requested_range_matches,
        "content": content,
    }


def resolve_raw_path(raw_path: Path, locator: str) -> dict[str, Any]:
    """按 inventory 已冻结的原始路径和行号读取任意 provider 原文。"""
    if not raw_path.is_file():
        raise FileNotFoundError(f"原始来源不存在: {raw_path}")
    semantic_locator, start_line, end_line = parse_line_locator(locator)
    if start_line is None or end_line is None:
        raise LocatorNotFoundError(f"locator 缺少行范围: {locator}")
    raw_lines = raw_path.read_text(encoding="utf-8", errors="replace").splitlines()
    if start_line < 1 or end_line < start_line or end_line > len(raw_lines):
        raise LocatorNotFoundError(
            f"locator 行范围超出文件: {locator}，文件共 {len(raw_lines)} 行"
        )
    return {
        "requested_locator": locator,
        "locator": locator,
        "semantic_locator": semantic_locator,
        "kind": "raw_unit",
        "source_id": None,
        "title": raw_path.stem,
        "path": str(raw_path.resolve()),
        "start_line": start_line,
        "end_line": end_line,
        "requested_range_matches": True,
        "content": "\n".join(raw_lines[start_line - 1 : end_line]),
    }


def render_text(result: dict[str, Any], line_numbers: bool = False) -> str:
    lines = [
        f"Path: {result['path']}",
        f"Locator: {result['locator']}",
        f"Kind: {result['kind']}",
        f"Lines: {result['start_line']}-{result['end_line']}",
    ]
    if not result["requested_range_matches"]:
        lines.append("Warning: 请求中的行范围已过期；以上述当前解析结果为准。")
    lines.extend(("", "---", ""))
    content_lines = str(result["content"]).splitlines()
    if line_numbers:
        width = len(str(result["end_line"]))
        lines.extend(
            f"{line_number:>{width}} | {line}"
            for line_number, line in enumerate(content_lines, start=result["start_line"])
        )
    else:
        lines.extend(content_lines)
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("locator", help="例如 17-vida.md#Session:12@L19566-L19584")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--raw-path", type=Path, help="直接指定 inventory 中的原始文件，适用于非 Claude provider")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--line-numbers", action="store_true")
    args = parser.parse_args()
    try:
        result = (
            resolve_raw_path(args.raw_path, args.locator)
            if args.raw_path is not None
            else resolve_locator(args.input, args.locator)
        )
    except (FileNotFoundError, LocatorNotFoundError) as exc:
        raise SystemExit(str(exc)) from exc
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render_text(result, args.line_numbers), end="")


if __name__ == "__main__":
    main()
