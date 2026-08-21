#!/usr/bin/env python3
"""根据 manifest 重建待整理来源列表。"""

from __future__ import annotations

import argparse
from pathlib import Path

from wiki_core import MANIFEST_PATH, REPO_ROOT, load_manifest


DEFAULT_OUTPUT = REPO_ROOT / "meta" / "review-queue.md"


def build_queue(manifest_path: Path, output_path: Path, dry_run: bool = False) -> str:
    manifest = load_manifest(manifest_path)
    items = [
        (source_id, value)
        for source_id, value in manifest["sources"].items()
        if value.get("status", "pending") in {"pending", "review", "conflict"}
    ]
    items.sort(key=lambda row: (str(row[1].get("created", "")), row[0]))
    lines = [
        "# 待整理来源",
        "",
        "<!-- 此文件由 scripts/rebuild_review_queue.py 生成。 -->",
        "",
        "处理前先按 `AGENTS.md` 搜索现有 Wiki，并在修改后更新来源状态。",
        "",
    ]
    if not items:
        lines.append("当前没有待整理来源。")
    else:
        for source_id, item in items:
            output = str(item.get("output_path", ""))
            link = f"../{output}" if output else ""
            title = str(item.get("title") or source_id)
            status = str(item.get("status", "pending"))
            origin = str(item.get("origin", "unknown"))
            created = str(item.get("created", "unknown"))
            lines.extend(
                [
                    f"## {title}",
                    "",
                    f"- [ ] **来源 ID**：`{source_id}`",
                    f"- **状态**：`{status}`",
                    f"- **类型**：`{origin}`",
                    f"- **日期**：{created}",
                    f"- **文件**：[{output}]({link})" if output else "- **文件**：缺失",
                    "- **整理决定**：待填写 `skip/create/update/conflict/review`",
                    "",
                ]
            )
    content = "\n".join(lines).rstrip() + "\n"
    if not dry_run:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content, encoding="utf-8")
    return content


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    content = build_queue(args.manifest, args.output, args.dry_run)
    print(f"审核队列包含 {content.count('**来源 ID**')} 个来源")


if __name__ == "__main__":
    main()
