#!/usr/bin/env python3
"""根据 Wiki 页面 front matter 重建完整索引。"""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
from typing import Any

from scripts.common.wiki_core import REPO_ROOT, parse_frontmatter


WIKI_ROOT = REPO_ROOT / "wiki"
DEFAULT_OUTPUT = WIKI_ROOT / "index.md"
SPECIAL_TYPES = {"overview", "index", "questions"}
TYPE_LABELS = {"project": "项目", "concept": "概念", "lesson": "经验", "decision": "决策"}


def collect_pages(wiki_root: Path, output_path: Path) -> list[tuple[Path, dict[str, Any]]]:
    pages: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(wiki_root.rglob("*.md")):
        if path == output_path or any(part.startswith(".") for part in path.relative_to(wiki_root).parts):
            continue
        metadata, _ = parse_frontmatter(path)
        if metadata.get("type") in SPECIAL_TYPES:
            continue
        pages.append((path, metadata))
    return pages


def build_index(wiki_root: Path, output_path: Path, dry_run: bool = False) -> str:
    groups: dict[str, list[tuple[Path, dict[str, Any]]]] = defaultdict(list)
    for path, metadata in collect_pages(wiki_root, output_path):
        groups[str(metadata.get("type", "other"))].append((path, metadata))
    lines = [
        "---",
        "title: Wiki 索引",
        "type: index",
        "status: verified",
        "---",
        "",
        "# Wiki 索引",
        "",
        "<!-- 此文件由 scripts.curation.rebuild_index 生成，请勿手工编辑列表部分。 -->",
        "",
    ]
    if not groups:
        lines.append("当前还没有已整理的知识页面。")
    else:
        order = ("project", "concept", "lesson", "decision")
        group_keys = [key for key in order if key in groups] + sorted(set(groups) - set(order))
        for group in group_keys:
            lines.extend([f"## {TYPE_LABELS.get(group, group)}", ""])
            entries = sorted(groups[group], key=lambda item: str(item[1].get("title", item[0].stem)))
            for path, metadata in entries:
                try:
                    vault_path = path.relative_to(REPO_ROOT).with_suffix("").as_posix()
                except ValueError:
                    vault_path = path.relative_to(wiki_root).with_suffix("").as_posix()
                title = str(metadata.get("title") or path.stem)
                description = str(metadata.get("description") or "暂无说明")
                status = str(metadata.get("status") or "unknown")
                tags = metadata.get("tags") or []
                tag_text = "、".join(str(tag) for tag in tags) if isinstance(tags, list) else str(tags)
                suffix = f"；标签：{tag_text}" if tag_text else ""
                lines.append(f"- [[{vault_path}|{title}]]：{description}（`{status}`{suffix}）")
            lines.append("")
    content = "\n".join(lines).rstrip() + "\n"
    if not dry_run:
        output_path.write_text(content, encoding="utf-8")
    return content


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wiki", type=Path, default=WIKI_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    content = build_index(args.wiki, args.output, args.dry_run)
    print(f"索引包含 {content.count('- [[')} 个知识页面")


if __name__ == "__main__":
    main()
