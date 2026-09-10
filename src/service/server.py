"""以单进程启动受限 HTTP API 服务，网络与凭据参数由外部配置提供。"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import sys
from pathlib import Path

import uvicorn

from src.service.config import load_config
from src.service.api import create_app


class UvicornLifecycleFilter(logging.Filter):
    """只放行 Uvicorn 的常规启动和停止日志，排除异常详情。"""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno == logging.INFO and not record.exc_info and not record.stack_info and record.msg in (
            "Started server process [%d]",
            "Waiting for application startup.",
            "Application startup complete.",
            "Uvicorn running on %s://%s:%d (Press CTRL+C to quit)",
            "Uvicorn running on %s://[%s]:%d (Press CTRL+C to quit)",
            "Shutting down",
            "Waiting for connections to close. (CTRL+C to force quit)",
            "Waiting for background tasks to complete. (CTRL+C to force quit)",
            "Waiting for application shutdown.",
            "Application shutdown complete.",
            "Finished server process [%d]",
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the read-only retrieval HTTP API")
    parser.add_argument("--config", type=Path, required=True, help="Path to the service JSON configuration")
    parser.add_argument("--host", default="0.0.0.0",
                        help="IP address to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=2699, help="TCP port to bind (default: 2699)")
    arguments = parser.parse_args()
    try:
        address = ipaddress.ip_address(arguments.host)
        if str(address) != "0.0.0.0" and (address.is_unspecified or address.is_multicast or not address.is_private):
            raise ValueError("bind address is not private")
        if not 1 <= arguments.port <= 65535:
            raise ValueError("port is invalid")
        config = load_config(arguments.config)
    except ValueError as e:
        print(f"Service configuration is invalid: {e}", file=sys.stderr)
        return 2

    # 保留 Uvicorn 正常生命周期日志，关闭默认 access 和异常详情日志。
    log_config = {
        "version": 1, "disable_existing_loggers": False,
        "filters": {"lifecycle": {"()": UvicornLifecycleFilter}},
        "formatters": {
            "safe": {"format": "%(message)s"},
            "uvicorn": {"()": "uvicorn.logging.DefaultFormatter", "fmt": "%(levelprefix)s %(message)s",
                        "use_colors": False},
        },
        "handlers": {
            "safe": {"class": "logging.StreamHandler", "formatter": "safe", "stream": "ext://sys.stdout"},
            "discard": {"class": "logging.NullHandler"},
            "lifecycle": {"class": "logging.StreamHandler", "formatter": "uvicorn",
                          "filters": ["lifecycle"], "stream": "ext://sys.stderr"},
        },
        "loggers": {
            "learn_corpus.service": {"handlers": ["safe"], "level": "INFO", "propagate": False},
            "uvicorn": {"handlers": ["discard"], "propagate": False},
            "uvicorn.error": {"handlers": ["lifecycle"], "level": "INFO", "propagate": False},
            "uvicorn.access": {"handlers": ["discard"], "propagate": False},
        },
    }
    uvicorn.run(create_app(config), host=str(address), port=arguments.port, workers=1,
                proxy_headers=False, access_log=False, server_header=False,
                ws="none", loop="asyncio", lifespan="on", log_config=log_config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
