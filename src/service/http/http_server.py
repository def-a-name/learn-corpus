"""启动单进程 Uvicorn HTTP API。"""

from __future__ import annotations

import logging

import uvicorn

from src.service.http.api import create_app
from src.service.http.http_config import HttpConfig


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


def run(config: HttpConfig, host: str, port: int) -> int:
    """使用已经校验的配置启动 HTTP transport。"""

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
    uvicorn.run(create_app(config), host=host, port=port, workers=1,
                proxy_headers=False, access_log=False, server_header=False,
                ws="none", loop="asyncio", lifespan="on", log_config=log_config)
    return 0
