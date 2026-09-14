"""解析和生成标准化来源文档的公共结构。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml


_LINE_LOCATOR_SUFFIX = re.compile(r"@L(?P<start>\d+)-L(?P<end>\d+)$")


def format_line_locator(locator: str, start_line: int, end_line: int) -> str:
    if start_line < 1 or end_line < start_line:
        raise ValueError(f"invalid locator line range: {start_line}-{end_line}")
    base, _, _ = parse_line_locator(locator)
    return f"{base}@L{start_line}-L{end_line}"


def parse_line_locator(locator: str) -> tuple[str, int | None, int | None]:
    match = _LINE_LOCATOR_SUFFIX.search(locator)
    if match is None:
        return locator, None, None
    return locator[: match.start()], int(match.group("start")), int(match.group("end"))


def yaml_document(metadata: dict[str, Any], body: str) -> str:
    header = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{header}\n---\n\n{body.rstrip()}\n"


def parse_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        raise ValueError(f"front matter is not closed: {path}")
    metadata = yaml.safe_load(text[4:end]) or {}
    if not isinstance(metadata, dict):
        raise ValueError(f"front matter must be an object: {path}")
    return metadata, text[end + 5 :].lstrip("\n")
