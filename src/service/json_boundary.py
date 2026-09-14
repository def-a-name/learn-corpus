"""为 HTTP 与 stdio 提供相同的有界严格 JSON 解析。"""

from __future__ import annotations

import json
import math

from src.service.errors import HTTPFailure


MAX_JSON_KEYS = 32
MAX_JSON_ARRAY_ITEMS = 20


def parse_json(raw: bytes, *, max_keys: int, max_array_items: int) -> object:
    """先限制容器深度，再解析并拒绝重复 key、非 JSON 数值和非法字符。"""

    try:
        text = raw.decode("utf-8")
        depth, quoted, escaped = 0, False, False
        for char in text:
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char in "[{":
                depth += 1
                if depth > 8:
                    raise ValueError("depth exceeded")
            elif char in "]}":
                depth -= 1
        key_count = 0

        def pairs(values):
            nonlocal key_count
            key_count += len(values)
            result = dict(values)
            if len(result) != len(values) or key_count > max_keys:
                raise ValueError("invalid keys")
            return result

        def reject_constant(_):
            raise ValueError("invalid constant")

        value = json.loads(text, object_pairs_hook=pairs, parse_constant=reject_constant)

        def validate(node):
            if isinstance(node, str):
                node.encode("utf-8")
                if "\0" in node:
                    raise ValueError("invalid character")
            elif isinstance(node, dict):
                for key, child in node.items():
                    validate(key)
                    validate(child)
            elif isinstance(node, list):
                if len(node) > max_array_items:
                    raise ValueError("array exceeded")
                for child in node:
                    validate(child)
            elif isinstance(node, float) and not math.isfinite(node):
                raise ValueError("invalid number")
        validate(value)
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise HTTPFailure("invalid_request") from exc


def strict_json(raw: bytes) -> object:
    """按固定的公开请求结构上限解析 JSON。"""

    return parse_json(raw, max_keys=MAX_JSON_KEYS, max_array_items=MAX_JSON_ARRAY_ITEMS)
