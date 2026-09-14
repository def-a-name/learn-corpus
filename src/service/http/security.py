"""提供 HTTP Bearer 验证与单进程并发登记。"""

from __future__ import annotations

import base64
import binascii
import hmac
import re
import threading
from dataclasses import dataclass, field

from src.service.errors import HTTPFailure
from src.service.validation import positive_integer


@dataclass(frozen=True)
class Credential:
    cid: str
    key: str
    secret: str = field(repr=False)

    def __post_init__(self):
        if (
            not isinstance(self.cid, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.cid) is None
            or not isinstance(self.key, str)
            or re.fullmatch(re.escape(self.cid) + r"_key_[0-9]{1,10}", self.key) is None
            or not isinstance(self.secret, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{32,64}", self.secret) is None
        ):
            raise ValueError("credential configuration is invalid")


class BearerVerifier:
    """验证 Base64URL(key.secret)；同一 cid 的多个 key 共用并发身份。"""

    def __init__(self, credentials: tuple[Credential, ...]):
        if not credentials or len({value.key for value in credentials}) != len(credentials):
            raise ValueError("credentials must contain unique keys")
        self._keys = {value.key: value for value in credentials}
        self.client_ids = frozenset(value.cid for value in credentials)

    def authenticate(self, authorization: str | None) -> Credential:
        match = re.fullmatch(
            r"(?i:Bearer) ([A-Za-z0-9_-]{1,192}={0,2})", authorization or ""
        )
        if match is None:
            raise HTTPFailure("unauthorized")
        encoded = match[1]
        try:
            raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
            canonical = base64.urlsafe_b64encode(raw).decode("ascii")
            # 接受规范的有填充或无填充编码，拒绝冗余填充与非零尾部位。
            if encoded not in (canonical, canonical.rstrip("=")):
                raise ValueError("invalid token encoding")
            key, secret = raw.decode("ascii").split(".")
            if (re.fullmatch(r"[A-Za-z0-9_-]{1,64}_key_[0-9]{1,10}", key) is None
                    or re.fullmatch(r"[A-Za-z0-9_-]{32,64}", secret) is None):
                raise ValueError("invalid token format")
        except (ValueError, UnicodeError, binascii.Error):
            raise HTTPFailure("unauthorized") from None
        credential = self._keys.get(key)
        expected = credential.secret if credential else "0" * len(secret)
        matched = hmac.compare_digest(secret, expected)
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
