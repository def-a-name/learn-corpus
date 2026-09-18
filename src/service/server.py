"""按统一配置互斥分派 HTTP 或本机 stdio transport。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.service.config import load_service_config


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve read-only retrieval over HTTP or MCP stdio")
    parser.add_argument("--config", type=Path, required=True, help="Path to the service JSON configuration")
    arguments = parser.parse_args()
    try:
        selected = load_service_config(arguments.config)
    except ValueError as exc:
        print(f"Service configuration error: {exc}.", file=sys.stderr)
        return 2

    if selected.transport == "stdio":
        from src.service.stdio.stdio_server import main as run_stdio

        return run_stdio(selected.runtime, selected.ledger)
    from src.service.http.http_server import run as run_http

    return run_http(selected.runtime, selected.host, selected.port, selected.ledger)


if __name__ == "__main__":
    raise SystemExit(main())
