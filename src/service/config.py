"""读取统一服务配置并互斥选择 HTTP 或 stdio transport。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from src.config_reader import read_config_object
from src.corpus.workspace import workspace_data_root
from src.service.errors import HTTPFailure
from src.service.ledger_config import (
    LedgerConfig, TaskLimitsConfig, parse_ledger_config, parse_task_limits_config,
)

if TYPE_CHECKING:
    from src.service.http.http_config import HttpConfig
    from src.service.stdio.stdio_config import StdioConfig


_HTTP_KEYS = {
    "credentials_file", "allowed_peers", "allowed_hosts", "allowed_origins",
    "global_concurrency", "client_concurrency", "body_timeout_ms", "host", "port",
}


def _require_fields(value: dict, required: set[str], section: str) -> None:
    """在构造运行配置前报告缺失字段，避免暴露配置值。"""

    missing = sorted(required - value.keys())
    if missing:
        raise ValueError(f"{section} configuration is missing required fields: {', '.join(missing)}")


def _reject_unknown_fields(value: dict, allowed: set[str], section: str) -> None:
    """报告未支持的字段名，不回显其值。"""

    unknown = sorted(value.keys() - allowed)
    if unknown:
        raise ValueError(f"{section} configuration contains unknown fields: {', '.join(unknown)}")


@dataclass(frozen=True)
class ServiceConfig:
    """一次启动只选择一种 transport；未选择的部分不生成运行配置。"""

    transport: str
    runtime: HttpConfig | StdioConfig
    ledger: LedgerConfig
    task_limits: TaskLimitsConfig
    host: str | None
    port: int | None


def parse_service_config(value: dict, path: Path) -> ServiceConfig:
    """选择统一配置的活动分支。"""

    path = path.expanduser().resolve()
    common_keys = {"corpus_path", "corpus_timeout_ms"}
    _reject_unknown_fields(value, common_keys | {"workspace", "http", "mcp"}, "service")
    _require_fields(value, {"corpus_timeout_ms", "mcp"}, "service")
    data_root = workspace_data_root(value.get("workspace", {}), path)
    mcp = value["mcp"]
    if not isinstance(mcp, dict):
        raise ValueError("MCP configuration must be an object")
    _reject_unknown_fields(mcp, {"transport", "ledger", "task_limits", "stdio"}, "MCP")
    _require_fields(mcp, {"transport", "ledger"}, "MCP")
    ledger_value = mcp["ledger"]
    if isinstance(ledger_value, dict):
        ledger_value = {"path": str(data_root / "var/execution-ledger.sqlite3"), **ledger_value}
    ledger = parse_ledger_config(ledger_value, path)
    task_limits = parse_task_limits_config(mcp.get("task_limits"))
    transport = mcp["transport"]
    if transport not in ("http", "stdio"):
        raise ValueError("MCP transport must be http or stdio")
    http = value.get("http", {})
    if not isinstance(http, dict):
        raise ValueError("HTTP configuration must be an object")
    _reject_unknown_fields(http, _HTTP_KEYS, "HTTP")
    stdio = mcp.get("stdio", {})
    if not isinstance(stdio, dict):
        raise ValueError("stdio configuration must be an object")
    _reject_unknown_fields(
        stdio,
        {"frame_timeout_ms", "write_timeout_ms", "shutdown_timeout_ms"},
        "stdio",
    )
    common = {"corpus_path": str(data_root / "meta/corpus"),
              **{name: value[name] for name in common_keys if name in value}}
    if transport == "stdio":
        from src.service.stdio.stdio_config import parse_stdio_config

        return ServiceConfig(
            transport, parse_stdio_config({**common, **stdio}, path), ledger,
            task_limits, None, None,
        )
    from src.service.http.http_config import parse_http_config, validate_bind

    _require_fields(http, _HTTP_KEYS, "HTTP")
    host, port = validate_bind(http["host"], http["port"])
    runtime = parse_http_config(
        {**common, **{name: child for name, child in http.items() if name not in {"host", "port"}}}, path,
    )
    return ServiceConfig(transport, runtime, ledger, task_limits, host, port)


def load_service_config(path: Path) -> ServiceConfig:
    """加载互斥 transport 配置，错误不回显配置内容。"""

    try:
        return parse_service_config(read_config_object(path), path)
    except FileNotFoundError as exc:
        raise ValueError("cannot load service configuration: configuration file does not exist") from exc
    except PermissionError as exc:
        raise ValueError("cannot load service configuration: configuration file is not readable") from exc
    except OSError as exc:
        raise ValueError("cannot load service configuration: configuration file cannot be read") from exc
    except HTTPFailure as exc:
        raise ValueError("cannot load service configuration: configuration JSON is invalid") from exc
    except (TypeError, KeyError) as exc:
        raise ValueError("cannot load service configuration: configuration structure is invalid") from exc
    except ValueError as exc:
        raise ValueError(f"cannot load service configuration: {exc}") from exc


def load_http_config(path: Path) -> HttpConfig:
    """加载明确选择了 HTTP 的统一配置。"""

    selected = load_service_config(path)
    if selected.transport != "http":
        raise ValueError("cannot load service configuration")
    return selected.runtime
