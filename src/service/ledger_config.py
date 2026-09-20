"""定义 MCP execution ledger 的部署配置。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.service.validation import positive_integer


DEFAULT_BUSY_TIMEOUT_MS = 1000
DEFAULT_MAX_TASKS = 10_000
DEFAULT_MAX_MB = 256
DEFAULT_SEARCH_CALLS = 4
DEFAULT_READ_CALLS = 8
DEFAULT_EVIDENCE_TOKENS = 8000
_SQLITE_INTEGER_MAX = 2**63 - 1


@dataclass(frozen=True)
class TaskLimitsConfig:
    """保存新建 retrieval task 的执行限制快照。"""

    search_calls: int = DEFAULT_SEARCH_CALLS
    read_calls: int = DEFAULT_READ_CALLS
    estimated_evidence_tokens: int = DEFAULT_EVIDENCE_TOKENS

    def __post_init__(self) -> None:
        for name, value in (
            ("task limits search_calls", self.search_calls),
            ("task limits read_calls", self.read_calls),
            ("task limits estimated_evidence_tokens", self.estimated_evidence_tokens),
        ):
            if not positive_integer(value) or value > _SQLITE_INTEGER_MAX:
                raise ValueError(f"{name} must be a positive 64-bit integer")


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


def parse_task_limits_config(value: object) -> TaskLimitsConfig:
    """严格解析新任务的 search/read 次数与累计 evidence 预算。"""

    if value is None:
        return TaskLimitsConfig()
    if not isinstance(value, dict):
        raise ValueError("task limits configuration must be an object")
    allowed = {"search_calls", "read_calls", "estimated_evidence_tokens"}
    unknown = sorted(value.keys() - allowed)
    if unknown:
        raise ValueError(
            f"task limits configuration contains unknown fields: {', '.join(unknown)}"
        )
    return TaskLimitsConfig(
        search_calls=value.get("search_calls", DEFAULT_SEARCH_CALLS),
        read_calls=value.get("read_calls", DEFAULT_READ_CALLS),
        estimated_evidence_tokens=value.get(
            "estimated_evidence_tokens", DEFAULT_EVIDENCE_TOKENS,
        ),
    )
