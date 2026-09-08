"""以单进程启动受限 HTTP API 服务，网络与凭据参数由外部配置提供。"""

from __future__ import annotations

import argparse
import ipaddress
import sys
from pathlib import Path

import uvicorn

from src.service.config import load_config
from src.service.api import create_app


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the read-only retrieval HTTP API")
    parser.add_argument("--config", type=Path, required=True, help="Path to the service JSON configuration")
    parser.add_argument("--host", required=True, help="Exact LAN or loopback address to bind")
    parser.add_argument("--port", type=int, required=True, help="TCP port to bind")
    arguments = parser.parse_args()
    try:
        address = ipaddress.ip_address(arguments.host)
        if address.is_unspecified or address.is_multicast or not address.is_private:
            raise ValueError("bind address is not private")
        if not 1 <= arguments.port <= 65535:
            raise ValueError("port is invalid")
        config = load_config(arguments.config)
    except ValueError:
        print("Service configuration is invalid", file=sys.stderr)
        return 2

    # 仅输出应用的字段白名单日志，关闭默认 access 和 traceback 日志。
    log_config = {
        "version": 1, "disable_existing_loggers": False,
        "formatters": {"safe": {"format": "%(message)s"}},
        "handlers": {
            "safe": {"class": "logging.StreamHandler", "formatter": "safe", "stream": "ext://sys.stdout"},
            "discard": {"class": "logging.NullHandler"},
        },
        "loggers": {
            "learn_corpus.service": {"handlers": ["safe"], "level": "INFO", "propagate": False},
            "uvicorn": {"handlers": ["discard"], "propagate": False},
            "uvicorn.error": {"handlers": ["discard"], "propagate": False},
            "uvicorn.access": {"handlers": ["discard"], "propagate": False},
        },
    }
    uvicorn.run(create_app(config), host=str(address), port=arguments.port, workers=1,
                proxy_headers=False, access_log=False, server_header=False,
                ws="none", loop="asyncio", lifespan="on", log_config=log_config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
