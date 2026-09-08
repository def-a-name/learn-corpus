"""提供静态凭据验证与单进程并发登记，不维护用户 turn 状态。"""

from __future__ import annotations

import hashlib
import hmac
import re
import threading
from dataclasses import dataclass


ERRORS = {
    "invalid_request": (400, "Request validation failed"),
    "request_too_large": (413, "Request exceeds size limits"),
    "unauthorized": (401, "Authentication required"),
    "forbidden": (403, "Request origin or host is not allowed"),
    "not_found": (404, "Endpoint not found"),
    "method_not_allowed": (405, "Method not allowed"),
    "request_timeout": (408, "Request body reception timed out"),
    "unsupported_media_type": (415, "Unsupported media type or content encoding"),
    "rate_limited": (429, "Request limit exceeded"),
    "internal_error": (500, "Internal server error"),
    "item_not_found": (404, "Item not found"),
    "generation_mismatch": (409, "Requested generation does not match"),
    "index_unavailable": (503, "Index unavailable"),
    "budget_exceeded": (422, "Response budget exceeded"),
}


class HTTPFailure(Exception):
    """只携带固定公开错误，不保存请求中的敏感值。"""

    def __init__(self, code: str, *, status: int | None = None, headers=None):
        default_status, message = ERRORS[code]
        super().__init__(message)
        self.code = code
        self.status = default_status if status is None else status
        self.headers = {} if headers is None else headers
        if code == "unauthorized":
            self.headers["WWW-Authenticate"] = "Bearer"


def positive_integer(value: object) -> bool:
    return type(value) is int and value > 0


@dataclass(frozen=True)
class Credential:
    client_id: str
    key_id: str
    secret_sha256: str

    def __post_init__(self):
        if (
            any(not isinstance(v, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", v) is None
                for v in (self.client_id, self.key_id))
            or not isinstance(self.secret_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.secret_sha256) is None
        ):
            raise ValueError("credential configuration is invalid")


class BearerVerifier:
    """验证 key_id.secret；同客户端的新旧 key 共用并发身份。"""

    def __init__(self, credentials: tuple[Credential, ...]):
        if not credentials or len({value.key_id for value in credentials}) != len(credentials):
            raise ValueError("credentials must contain unique keys")
        self._keys = {value.key_id: value for value in credentials}
        self.client_ids = frozenset(value.client_id for value in credentials)

    def authenticate(self, authorization: str | None) -> Credential:
        match = re.fullmatch(
            r"(?i:Bearer) ([A-Za-z0-9_-]{1,64})\.([A-Za-z0-9_-]{43})", authorization or ""
        )
        if match is None:
            raise HTTPFailure("unauthorized")
        credential = self._keys.get(match[1])
        actual = hashlib.sha256(match[2].encode("ascii")).digest()
        expected = bytes.fromhex(credential.secret_sha256) if credential else bytes(32)
        matched = hmac.compare_digest(actual, expected)
        if not matched or credential is None:
            raise HTTPFailure("unauthorized")
        return credential


class Admissions:
    """非阻塞地登记全局及客户端并发，不建立等待队列。"""

    def __init__(self, global_limit: int, client_limit: int):
        if not positive_integer(global_limit) or not positive_integer(client_limit):
            raise ValueError("concurrency limits must be positive integers")
        self.global_limit, self.client_limit = global_limit, client_limit
        self._total = 0
        self._clients: dict[str, int] = {}
        self._lock = threading.Lock()

    def acquire(self, client: str):
        with self._lock:
            count = self._clients.get(client, 0)
            if self._total >= self.global_limit or count >= self.client_limit:
                raise HTTPFailure("rate_limited")
            self._total += 1
            self._clients[client] = count + 1

    def release(self, client: str):
        with self._lock:
            self._total -= 1
            self._clients[client] -= 1
            if not self._clients[client]:
                del self._clients[client]
