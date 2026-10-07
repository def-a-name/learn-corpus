"""按统一配置互斥分派 HTTP 或本机 stdio transport。"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from src.service.config import load_service_config
from src.corpus.workspace import CODE_ROOT, find_config_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve read-only retrieval over HTTP or MCP stdio")
    parser.add_argument("--config", type=Path, help="Path to the unified JSON configuration (defaults to the workspace configuration)")
    arguments = parser.parse_args()
    try:
        config_path = arguments.config or find_config_path(CODE_ROOT, os.environ)
        if config_path is None:
            raise ValueError("no configuration file was found; use --config or create config/config.json")
        selected = load_service_config(config_path)
    except ValueError as exc:
        print(f"Service configuration error: {exc}.", file=sys.stderr)
        return 2

    if selected.transport == "stdio":
        from src.service.stdio.stdio_server import main as run_stdio

        return run_stdio(selected.runtime, selected.ledger, selected.task_limits)
    from src.service.http.http_server import run as run_http

    return run_http(
        selected.runtime, selected.host, selected.port,
        selected.ledger, selected.task_limits,
    )


if __name__ == "__main__":
    raise SystemExit(main())
