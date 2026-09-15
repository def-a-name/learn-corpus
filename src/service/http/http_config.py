"""装配 HTTP transport 配置、请求头边界和 Bearer 凭据。"""

from __future__ import annotations

import ipaddress
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from src.service.errors import HTTPFailure
from src.service.http.security import Credential
from src.service.json_boundary import parse_json
from src.service.validation import positive_integer


MAX_HEADER_BYTES = 8192
MAX_HEADERS = 64
MAX_AUTHORIZATION_BYTES = 256
_CREDENTIAL_MAX_JSON_KEYS = 4096
_CREDENTIAL_MAX_JSON_ARRAY_ITEMS = 1024


@dataclass(frozen=True)
class HttpConfig:
    corpus_path: Path
    credentials_file: Path
    allowed_peers: tuple[str, ...]
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...]
    corpus_timeout_ms: int
    global_concurrency: int
    client_concurrency: int
    body_timeout_ms: int

    def __post_init__(self):
        numbers = {
            "corpus_timeout_ms": self.corpus_timeout_ms,
            "global_concurrency": self.global_concurrency,
            "client_concurrency": self.client_concurrency,
            "body_timeout_ms": self.body_timeout_ms,
        }
        for name, value in numbers.items():
            if not positive_integer(value):
                raise ValueError(f"{name} must be a positive integer")
        if not self.allowed_peers or not self.allowed_hosts:
            raise ValueError("peer and host allowlists must not be empty")
        for value in self.allowed_peers:
            if str(ipaddress.ip_address(value)) != value:
                raise ValueError("peer addresses must be canonical IP literals")
        for value in (*self.allowed_hosts, *self.allowed_origins):
            if not isinstance(value, str) or not value or "*" in value or any(ord(c) <= 32 for c in value):
                raise ValueError("host and origin allowlists must contain exact values")


def load_credentials(path: Path) -> tuple[Credential, ...]:
    """启动时读取受限凭据文件；轮换或撤销通过替换配置并重启生效。"""

    try:
        with path.open("rb") as stream:
            mode = os.fstat(stream.fileno()).st_mode
            if not stat.S_ISREG(mode) or mode & 0o077:
                raise ValueError("credential file permissions are invalid")
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("credential file exceeds size limit")
        value = parse_json(
            raw, max_keys=_CREDENTIAL_MAX_JSON_KEYS,
            max_array_items=_CREDENTIAL_MAX_JSON_ARRAY_ITEMS,
        )
        if not isinstance(value, list):
            raise ValueError("credential configuration must be an array")
        return tuple(Credential(**entry) for entry in value)
    except (OSError, ValueError, TypeError, HTTPFailure) as exc:
        raise ValueError("cannot load credential configuration") from exc


def parse_http_config(value: dict, path: Path) -> HttpConfig:
    """装配 HTTP 运行配置，路径均相对配置文件目录解析。"""

    value = dict(value)
    for name in ("corpus_path", "credentials_file"):
        if not isinstance(value[name], str) or not value[name]:
            raise ValueError(f"{name} must be a non-empty path string")
        candidate = Path(value[name])
        value[name] = candidate if candidate.is_absolute() else path.parent / candidate
    for name in ("allowed_peers", "allowed_hosts", "allowed_origins"):
        if not isinstance(value[name], list):
            raise ValueError(f"{name} must be an array")
        value[name] = tuple(value[name])
    return HttpConfig(**value)


def validate_bind(host: object, port: object) -> tuple[str, int]:
    """校验 HTTP 监听地址，拒绝布尔或非整数端口。"""

    if not isinstance(host, str):
        raise ValueError("HTTP host must be an IP address string")
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("HTTP host must be a valid IP address") from exc
    if str(address) != "0.0.0.0" and (address.is_unspecified or address.is_multicast or not address.is_private):
        raise ValueError("HTTP host must be 0.0.0.0 or a private IP address")
    if not positive_integer(port) or port > 65535:
        raise ValueError("HTTP port must be an integer from 1 to 65535")
    return str(address), port
