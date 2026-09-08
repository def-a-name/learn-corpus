"""声明显式服务配置及严格 JSON 解析，部署值不写入源码。"""

from __future__ import annotations

import ipaddress
import json
import math
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from src.retrieval.public_core import RequestLimits
from src.service.security import Credential, HTTPFailure, RatePolicy, positive_integer


def strict_json(raw: bytes, *, max_keys: int, max_array_items: int) -> object:
    """先限制容器深度，再解析并拒绝重复 key、非 JSON 数值和非法字符。"""

    try:
        text = raw.decode("utf-8")
        depth, quoted, escaped = 0, False, False
        for char in text:
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char in "[{":
                depth += 1
                if depth > 8:
                    raise ValueError("depth exceeded")
            elif char in "]}":
                depth -= 1
        key_count = 0

        def pairs(values):
            nonlocal key_count
            key_count += len(values)
            result = dict(values)
            if len(result) != len(values) or key_count > max_keys:
                raise ValueError("invalid keys")
            return result

        def reject_constant(_):
            raise ValueError("invalid constant")

        value = json.loads(text, object_pairs_hook=pairs, parse_constant=reject_constant)

        def validate(node):
            if isinstance(node, str):
                node.encode("utf-8")
                if "\0" in node:
                    raise ValueError("invalid character")
            elif isinstance(node, dict):
                for key, child in node.items():
                    validate(key)
                    validate(child)
            elif isinstance(node, list):
                if len(node) > max_array_items:
                    raise ValueError("array exceeded")
                for child in node:
                    validate(child)
            elif isinstance(node, float) and not math.isfinite(node):
                raise ValueError("invalid number")
        validate(value)
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise HTTPFailure("invalid_request") from exc


@dataclass(frozen=True)
class RestConfig:
    retrieval_root: Path
    credentials_file: Path
    allowed_peers: tuple[str, ...]
    trusted_proxies: tuple[str, ...]
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...]
    response_limits: RequestLimits
    ip_rate: RatePolicy
    client_rate: RatePolicy
    global_concurrency: int
    client_concurrency: int
    max_ip_buckets: int
    max_header_bytes: int
    max_headers: int
    max_authorization_bytes: int
    max_json_keys: int
    max_json_array_items: int
    body_timeout_ms: int

    def __post_init__(self):
        numbers = (
            self.global_concurrency, self.client_concurrency, self.max_ip_buckets,
            self.max_header_bytes, self.max_headers, self.max_authorization_bytes,
            self.max_json_keys, self.max_json_array_items, self.body_timeout_ms,
        )
        if not all(positive_integer(value) for value in numbers):
            raise ValueError("transport limits must be positive integers")
        if not self.allowed_peers or not self.allowed_hosts:
            raise ValueError("peer and host allowlists must not be empty")
        for value in (*self.allowed_peers, *self.trusted_proxies):
            if str(ipaddress.ip_address(value)) != value:
                raise ValueError("peer addresses must be canonical IP literals")
        if not set(self.trusted_proxies) <= set(self.allowed_peers):
            raise ValueError("trusted proxies must be allowed peers")
        for value in (*self.allowed_hosts, *self.allowed_origins):
            if not isinstance(value, str) or not value or "*" in value or any(ord(c) <= 32 for c in value):
                raise ValueError("host and origin allowlists must contain exact values")


def load_credentials(path: Path) -> tuple[Credential, ...]:
    """启动时读取受限 verifier 文件；轮换或撤销通过替换配置并重启生效。"""

    try:
        with path.open("rb") as stream:
            mode = os.fstat(stream.fileno()).st_mode
            if not stat.S_ISREG(mode) or mode & 0o077:
                raise ValueError("credential file permissions are invalid")
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("credential file exceeds size limit")
        value = strict_json(raw, max_keys=4096, max_array_items=1024)
        if not isinstance(value, list):
            raise ValueError("credential configuration must be an array")
        return tuple(Credential(**entry) for entry in value)
    except (OSError, ValueError, TypeError, HTTPFailure) as exc:
        raise ValueError("cannot load credential configuration") from exc


def load_config(path: Path) -> RestConfig:
    """从显式 JSON 文件装配配置，相对路径以配置文件目录为基准。"""

    try:
        with path.open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("configuration exceeds size limit")
        value = strict_json(raw, max_keys=4096, max_array_items=1024)
        if not isinstance(value, dict):
            raise ValueError("configuration must be an object")
        for name in ("retrieval_root", "credentials_file"):
            if not isinstance(value[name], str) or not value[name]:
                raise ValueError("configuration path is invalid")
            candidate = Path(value[name])
            value[name] = candidate if candidate.is_absolute() else path.parent / candidate
        for name in ("allowed_peers", "trusted_proxies", "allowed_hosts", "allowed_origins"):
            if not isinstance(value[name], list):
                raise ValueError("allowlist must be an array")
            value[name] = tuple(value[name])
        value["response_limits"] = RequestLimits(**value["response_limits"])
        value["ip_rate"] = RatePolicy(**value["ip_rate"])
        value["client_rate"] = RatePolicy(**value["client_rate"])
        return RestConfig(**value)
    except (OSError, TypeError, ValueError, KeyError, HTTPFailure) as exc:
        raise ValueError("cannot load service configuration") from exc
