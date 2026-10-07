"""读取来源处理和检索服务共用的严格 JSON 配置。"""

from __future__ import annotations

from pathlib import Path

from src.service.json_boundary import parse_json


CONFIG_FIELDS = {"workspace", "corpus_path", "corpus_timeout_ms", "http", "mcp"}


def read_config_object(path: Path) -> dict:
    """限制文件大小、层级和容器数量，并拒绝重复字段。"""

    with path.open("rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("configuration exceeds size limit")
    value = parse_json(raw, max_keys=4096, max_array_items=1024)
    if not isinstance(value, dict):
        raise ValueError("configuration must be an object")
    return value
