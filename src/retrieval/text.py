"""提供检索投影、查询编译和读取共用的 Unicode 文本处理。"""

from __future__ import annotations

import math
import unicodedata


def is_cjk_scalar(value: str) -> bool:
    """判断单个 Unicode 标量是否属于当前检索策略支持的 CJK 范围。"""

    codepoint = ord(value)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x323AF
        or 0x3040 <= codepoint <= 0x30FF
        or 0x31F0 <= codepoint <= 0x31FF
        or 0xFF66 <= codepoint <= 0xFF9D
        or 0x1100 <= codepoint <= 0x11FF
        or 0x3130 <= codepoint <= 0x318F
        or 0xA960 <= codepoint <= 0xA97F
        or 0xAC00 <= codepoint <= 0xD7FF
    )


def _is_wide_scalar(value: str) -> bool:
    return is_cjk_scalar(value) or unicodedata.category(value).startswith("S")


def estimate_evidence_tokens(text: str) -> int:
    """对一段 Unicode 文本应用 evidence-estimator-v1。"""

    wide = sum(_is_wide_scalar(value) for value in text)
    other = len(text) - wide
    return max(math.ceil(len(text.encode("utf-8")) / 3), 2 * wide + math.ceil(other / 3))


def normalize_index_text(text: str | None) -> str:
    """在仅用于搜索的 FTS 文本中为 CJK 字符插入边界。"""

    if text is None:
        return ""
    normalized = unicodedata.normalize("NFC", text)
    output: list[str] = []
    for value in normalized:
        if is_cjk_scalar(value):
            output.extend((" ", value, " "))
        else:
            output.append(value)
    return " ".join("".join(output).split())
