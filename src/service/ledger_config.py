"""定义 MCP execution ledger 的部署配置。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.service.validation import positive_integer


DEFAULT_BUSY_TIMEOUT_MS = 1000
DEFAULT_MAX_TASKS = 10_000
DEFAULT_MAX_MB = 256


@dataclass(frozen=True)
class LedgerConfig:
    """保存独立账本数据库路径与容量边界。"""

    path: Path
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS
    max_tasks: int = DEFAULT_MAX_TASKS
    max_mb: int = DEFAULT_MAX_MB

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path):
            raise ValueError("ledger path must be a path")
        for name, value in (
            ("ledger busy_timeout_ms", self.busy_timeout_ms),
            ("ledger max_tasks", self.max_tasks),
            ("ledger max_mb", self.max_mb),
        ):
            if not positive_integer(value):
                raise ValueError(f"{name} must be a positive integer")

    @property
    def max_bytes(self) -> int:
        """将面向配置的 MiB 门限换算成精确字节数。"""

        return self.max_mb * 1024 * 1024


def parse_ledger_config(value: object, config_path: Path) -> LedgerConfig:
    """严格解析 MCP 账本配置，路径相对统一配置文件解析。"""

    if not isinstance(value, dict):
        raise ValueError("ledger configuration must be an object")
    allowed = {"path", "busy_timeout_ms", "max_tasks", "max_mb"}
    unknown = sorted(value.keys() - allowed)
    if unknown:
        raise ValueError(
            f"ledger configuration contains unknown fields: {', '.join(unknown)}"
        )
    if "path" not in value:
        raise ValueError("ledger configuration is missing required fields: path")
    candidate = value["path"]
    if not isinstance(candidate, str) or not candidate:
        raise ValueError("ledger path must be a non-empty path string")
    path = Path(candidate)
    return LedgerConfig(
        path=path if path.is_absolute() else config_path.parent / path,
        busy_timeout_ms=value.get("busy_timeout_ms", DEFAULT_BUSY_TIMEOUT_MS),
        max_tasks=value.get("max_tasks", DEFAULT_MAX_TASKS),
        max_mb=value.get("max_mb", DEFAULT_MAX_MB),
    )
