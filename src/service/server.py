"""按统一配置互斥分派 HTTP 或本机 stdio transport。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.service.config import load_service_config


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve read-only retrieval over HTTP or MCP stdio")
    parser.add_argument("--config", type=Path, required=True, help="Path to the service JSON configuration")
    parser.add_argument("--host", help="Override HTTP bind address (configuration default: 0.0.0.0)")
    parser.add_argument("--port", type=int, help="Override HTTP TCP port (configuration default: 2699)")
    arguments = parser.parse_args()
    try:
        selected = load_service_config(arguments.config)
        if selected.transport == "stdio":
            if arguments.host is not None or arguments.port is not None:
                raise ValueError("HTTP bind overrides cannot be used with stdio")
        else:
            from src.service.http.http_config import validate_bind

            host, port = validate_bind(
                selected.host if arguments.host is None else arguments.host,
                selected.port if arguments.port is None else arguments.port,
            )
    except ValueError:
        print("Service configuration is invalid.", file=sys.stderr)
        return 2

    if selected.transport == "stdio":
        from src.service.stdio.stdio_server import main as run_stdio

        return run_stdio(selected.runtime)
    from src.service.http.http_server import run as run_http

    return run_http(selected.runtime, host, port)


if __name__ == "__main__":
    raise SystemExit(main())
