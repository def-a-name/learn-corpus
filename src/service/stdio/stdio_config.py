"""从统一配置装配 stdio transport 运行参数。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.service.config import parse_service_config, read_config_object
from src.service.errors import HTTPFailure
from src.service.validation import positive_integer


@dataclass(frozen=True)
class StdioConfig:
    corpus_path: Path
    corpus_timeout_ms: int
    frame_timeout_ms: int = 5000
    write_timeout_ms: int = 5000
    shutdown_timeout_ms: int | None = None

    def __post_init__(self):
        if self.shutdown_timeout_ms is None:
            object.__setattr__(self, "shutdown_timeout_ms", self.corpus_timeout_ms + 1000)
        values = (self.corpus_timeout_ms, self.frame_timeout_ms,
                  self.write_timeout_ms, self.shutdown_timeout_ms)
        if not all(positive_integer(value) for value in values):
            raise ValueError("stdio limits must be positive integers")


def parse_stdio_config(values: dict, path: Path) -> StdioConfig:
    """只装配 stdio 有效字段，不接收 HTTP 认证、网络或并发参数。"""

    values = dict(values)
    candidate = values["corpus_path"]
    if not isinstance(candidate, str) or not candidate:
        raise ValueError("configuration path is invalid")
    candidate = Path(candidate)
    values["corpus_path"] = candidate if candidate.is_absolute() else path.parent / candidate
    return StdioConfig(**values)


def load_stdio_config(path: Path) -> StdioConfig:
    """读取统一配置，并拒绝选择了 HTTP 的配置。"""
    try:
        values = read_config_object(path)
        selected = parse_service_config(values, path)
        if selected.transport != "stdio":
            raise ValueError("stdio transport is not selected")
        return selected.runtime
    except (OSError, TypeError, ValueError, KeyError, HTTPFailure) as exc:
        raise ValueError("cannot load stdio configuration") from exc
