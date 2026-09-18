"""为后续 REST/MCP 提供同一份有界公开 JSON，不承载认证或协议适配。"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from time import monotonic
from typing import Any
from uuid import uuid4

from src.retrieval.bundle import bundle_key, check_deadline, prioritize_members, read_members
from src.retrieval.contracts import ESTIMATOR_VERSION, Item
from src.retrieval.lexical_store import (
    BudgetExceededError,
    IndexUnavailableError,
    InvalidRequestError,
    LexicalStore,
)
from src.retrieval.lexical_query import CompiledQuery
from src.retrieval.text import estimate_evidence_tokens


MAX_RESPONSE_TOKENS = 8000
MAX_RESPONSE_BYTES = 64 * 1024
_REQUEST_ID = re.compile(r"req_[A-Za-z0-9_-]{1,64}")
_ITEM_ID = re.compile(r"itm_[a-z2-7]{32}")
_METADATA = frozenset({
    "item_id", "source_type", "title", "path", "locator", "role", "evidence_role", "turn_index",
})


@dataclass(frozen=True)
class RequestLimits:
    """限制一次 corpus core 操作的总执行时间。"""

    corpus_timeout_ms: int

    def __post_init__(self) -> None:
        if type(self.corpus_timeout_ms) is not int or self.corpus_timeout_ms < 1:
            raise ValueError("corpus timeout must be a positive integer")


@dataclass(frozen=True)
class PublicResponse:
    """保存已经计量和校验的精确 JSON 字节，避免 adapter 意外修改共享结果。"""

    json_bytes: bytes

    @property
    def payload(self) -> dict[str, Any]:
        """返回独立副本，MCP 可将其作为结构化结果使用。"""

        return json.loads(self.json_bytes)


@dataclass(frozen=True)
class _PreparedRequest:
    """仅在同一 core 内复用的不可变校验结果，避免跨操作或跨实例误用。"""

    owner: object
    operation: str
    max_tokens: int
    compiled: tuple[CompiledQuery, ...] = ()
    scopes: tuple[str, ...] | None = None
    limit: int = 8
    seed_item_id: str | None = None
    generation: str | None = None


def _request(request: object, allowed: set[str], required: set[str]) -> dict[str, Any]:
    if type(request) is not dict or not required <= request.keys() or request.keys() - allowed:
        raise InvalidRequestError("request schema is invalid")
    return request


def _tokens(request: dict[str, Any]) -> int:
    value = request.get("max_estimated_tokens", MAX_RESPONSE_TOKENS)
    if type(value) is not int or not 1 <= value <= MAX_RESPONSE_TOKENS:
        raise InvalidRequestError("max_estimated_tokens must be an integer between 1 and 8000")
    return value


def _json(payload: dict[str, Any]) -> bytes:
    try:
        return json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise IndexUnavailableError("public response cannot be serialized") from exc


def _metadata(item: Item) -> dict[str, Any]:
    return {
        "item_id": item.item_id,
        "source_type": item.scope,
        "title": item.title,
        "path": item.source_path,
        "locator": item.locator,
        "role": item.role,
        "evidence_role": item.evidence_role,
        "turn_index": item.turn_index,
    }


def _validate_metadata(item: dict[str, Any]) -> None:
    """只允许公开字段的合法类型和标准化来源相对路径。"""

    scope = item["source_type"]
    path = item["path"]
    if (
        scope not in {"conversation", "note", "article"}
        or not isinstance(path, str)
        or not path.startswith(f"sources/{scope}s/")
        or ".." in PurePosixPath(path).parts or "\\" in path
        or any(ord(char) < 32 for char in path)
        or not isinstance(item["item_id"], str) or _ITEM_ID.fullmatch(item["item_id"]) is None
        or not isinstance(item["locator"], str) or not item["locator"]
        or (item["title"] is not None and not isinstance(item["title"], str))
    ):
        raise IndexUnavailableError("public item metadata is invalid")
    if scope == "conversation":
        if (
            item["role"] not in {"human", "assistant"}
            or item["evidence_role"] != (
                "user_statement" if item["role"] == "human" else "assistant_suggestion"
            )
            or type(item["turn_index"]) is not int or item["turn_index"] < 0
        ):
            raise IndexUnavailableError("public conversation metadata is invalid")
    elif (
        item["role"] is not None or item["turn_index"] is not None
        or (scope == "article" and item["evidence_role"] != "external_source")
        or (scope == "note" and item["evidence_role"] is not None
            and not isinstance(item["evidence_role"], str))
    ):
        raise IndexUnavailableError("public section metadata is invalid")


def _validate_response(payload: dict[str, Any], operation: str) -> None:
    """序列化之前验证公开结果的字段白名单与完整性不变量。"""

    common = {"request_id", "generation", "usage"}
    if operation == "search":
        if set(payload) != common | {"results", "is_truncated"}:
            raise IndexUnavailableError("public search response schema is invalid")
        items = payload["results"]
        extra = {"bundle_key", "rank", "snippet", "truncated_before", "truncated_after"}
        if type(payload["is_truncated"]) is not bool or len(items) > 20:
            raise IndexUnavailableError("public search response is invalid")
    else:
        if set(payload) != common | {
            "seed_item_id", "bundle_key", "bundle_status", "membership_complete",
            "missing_item_ids", "items",
        }:
            raise IndexUnavailableError("public bundle response schema is invalid")
        items = payload["items"]
        extra = {"body", "is_truncated", "relations"}
        missing = payload["missing_item_ids"]
        if type(items) is not list or type(missing) is not list:
            raise IndexUnavailableError("public bundle item arrays are invalid")
        returned_ids = {item["item_id"] for item in items}
        if (
            not isinstance(payload["seed_item_id"], str)
            or _ITEM_ID.fullmatch(payload["seed_item_id"]) is None
            or payload["seed_item_id"] not in returned_ids
            or payload["bundle_status"] not in {"complete", "partial_budget", "partial_error"}
            or re.fullmatch(r"bnd_[0-9a-f]{40}", payload["bundle_key"]) is None
            or type(payload["membership_complete"]) is not bool
            or len(set(missing)) != len(missing)
            or any(not isinstance(ref, str) or _ITEM_ID.fullmatch(ref) is None for ref in missing)
            or set(missing) & returned_ids
            or (payload["bundle_status"] == "complete" and (
                not payload["membership_complete"] or missing or not items
            ))
        ):
            raise IndexUnavailableError("public bundle completeness is invalid")
    if len({item["item_id"] for item in items}) != len(items):
        raise IndexUnavailableError("public response contains duplicate items")
    for rank, item in enumerate(items, start=1):
        if set(item) != _METADATA | extra:
            raise IndexUnavailableError("public item schema is invalid")
        _validate_metadata(item)
        if operation == "search":
            if (
                item["rank"] != rank or not isinstance(item["snippet"], str)
                or type(item["truncated_before"]) is not bool
                or type(item["truncated_after"]) is not bool
                or re.fullmatch(r"bnd_[0-9a-f]{40}", item["bundle_key"]) is None
            ):
                raise IndexUnavailableError("public search item is invalid")
        else:
            relations = item["relations"]
            if (
                not isinstance(item["body"], str) or not item["body"]
                or item["is_truncated"] is not False
                or set(relations) != {"counterpart_item_ids", "previous_part_id", "next_part_id"}
            ):
                raise IndexUnavailableError("public bundle item is invalid")


class RetrievalCore:
    """在原有词法运行时上提供无用户任务状态的公开结果层。"""

    def __init__(self, store: LexicalStore, limits: RequestLimits) -> None:
        self.store = store
        self.limits = limits
        self._request_owner = object()

    def _start(self, request_id: str | None) -> tuple[float, str]:
        deadline = monotonic() + self.limits.corpus_timeout_ms / 1000
        value = "req_" + uuid4().hex if request_id is None else request_id
        if not isinstance(value, str) or _REQUEST_ID.fullmatch(value) is None:
            raise InvalidRequestError("request_id has an invalid format")
        return deadline, value

    def _encode(
        self, payload: dict[str, Any], operation: str, max_tokens: int, deadline: float,
        max_response_bytes: int | None = None,
    ) -> PublicResponse | None:
        if max_response_bytes is None:
            max_response_bytes = MAX_RESPONSE_BYTES
        check_deadline(deadline)
        payload["usage"] = {
            "estimated_evidence_tokens": 0,
            "estimator_version": ESTIMATOR_VERSION,
        }
        _validate_response(payload, operation)
        # usage 自身的数字也占空间；迭代到包含该数字的完整 JSON 估算值不再变化。
        while True:
            check_deadline(deadline)
            encoded = _json(payload)
            measured = estimate_evidence_tokens(encoded.decode("utf-8"))
            check_deadline(deadline)
            if measured == payload["usage"]["estimated_evidence_tokens"]:
                break
            payload["usage"]["estimated_evidence_tokens"] = measured
        if measured > max_tokens or len(encoded) > max_response_bytes:
            return None
        return PublicResponse(encoded)

    def search(
        self, request: object, *, request_id: str | None = None,
        max_response_bytes: int | None = None,
    ) -> PublicResponse:
        deadline, request_id = self._start(request_id)
        values = self.validate_request("search", request)
        max_tokens = values.max_tokens
        result = self.store._search_compiled(
            values.compiled, values.scopes, values.limit, deadline=deadline,
        )
        candidates = []
        for hit in result.results:
            check_deadline(deadline)
            item = self.store.read_canonical_item(hit.item_id, result.generation, deadline=deadline)
            candidates.append({
                **_metadata(item), "bundle_key": bundle_key(item), "rank": hit.rank,
                "snippet": hit.snippet, "truncated_before": hit.truncated_before,
                "truncated_after": hit.truncated_after,
            })
        # 从完整结果向下缩短，始终保留同一排名的完整前缀。
        for count in range(len(candidates), -1, -1):
            response = self._encode({
                "request_id": request_id, "generation": result.generation,
                "is_truncated": count < len(candidates), "results": candidates[:count],
            }, "search", max_tokens, deadline, max_response_bytes)
            if response is not None:
                return response
        raise BudgetExceededError("minimum search response exceeds the response budget")

    def read_bundle(
        self, request: object, *, request_id: str | None = None,
        max_response_bytes: int | None = None,
    ) -> PublicResponse:
        deadline, request_id = self._start(request_id)
        values = self.validate_request("read_bundle", request)
        max_tokens = values.max_tokens
        seed = self.store.read_canonical_item(values.seed_item_id, values.generation, deadline=deadline)
        key = bundle_key(seed)
        members = read_members(self.store, seed, deadline)
        item_payloads = {}
        for item in members:
            check_deadline(deadline)
            item_payloads[item.item_id] = {
                **_metadata(item), "body": item.body, "is_truncated": False,
                "relations": asdict(item.relations),
            }

        def encode(selected: set[str]) -> PublicResponse | None:
            complete = len(selected) == len(members)
            return self._encode({
                "request_id": request_id, "generation": self.store.generation,
                "seed_item_id": seed.item_id, "bundle_key": key,
                "bundle_status": "complete" if complete else "partial_budget",
                "membership_complete": True,
                "missing_item_ids": [
                    item.item_id for item in members if item.item_id not in selected
                ],
                "items": [item_payloads[item.item_id] for item in members if item.item_id in selected],
            }, "bundle", max_tokens, deadline, max_response_bytes)

        priority = prioritize_members(members, seed.item_id)
        selected = {seed.item_id}
        response = encode(selected)
        if response is None:
            raise BudgetExceededError("bundle seed exceeds the response budget")
        for item in priority[1:]:
            check_deadline(deadline)
            candidate = selected | {item.item_id}
            expanded = encode(candidate)
            if expanded is None:
                break
            selected = candidate
            response = expanded
        return response

    def validate_request(self, operation: str, request: object) -> _PreparedRequest:
        """校验并编译一次；入口和后续 core 执行复用不可变结果。"""

        if isinstance(request, _PreparedRequest):
            if request.owner is not self._request_owner or request.operation != operation:
                raise InvalidRequestError("prepared request does not belong to this operation")
            return request

        if operation == "search":
            values = _request(request, {
                "queries", "scopes", "limit", "generation", "max_estimated_tokens",
            }, {"queries"})
            max_tokens = _tokens(values)
            if type(values["queries"]) is not list or (
                "scopes" in values and type(values["scopes"]) is not list
            ):
                raise InvalidRequestError("queries and scopes must be arrays")
            if "generation" in values:
                self.store.validate_generation(values["generation"])
            compiled = self.store._validate_search_request(
                values["queries"], values.get("scopes"), values.get("limit", 8),
            )
            return _PreparedRequest(
                self._request_owner, operation, max_tokens, compiled,
                None if "scopes" not in values else tuple(values["scopes"]),
                values.get("limit", 8), generation=values.get("generation"),
            )
        if operation == "read_bundle":
            values = _request(request, {
                "seed_item_id", "generation", "max_estimated_tokens",
            }, {"seed_item_id", "generation"})
            max_tokens = _tokens(values)
            self.store.validate_generation(values["generation"])
            seed = values["seed_item_id"]
            if not isinstance(seed, str) or _ITEM_ID.fullmatch(seed) is None:
                raise InvalidRequestError("item_id has an invalid format")
            return _PreparedRequest(
                self._request_owner, operation, max_tokens,
                seed_item_id=seed, generation=values["generation"],
            )
        raise InvalidRequestError("operation is invalid")

    def status(self) -> PublicResponse:
        deadline, _ = self._start(None)
        with self.store.request_deadline(deadline):
            payload = asdict(self.store.status())
        payload["capabilities"] = {
            "read_bundle": True,
            "max_estimated_tokens": MAX_RESPONSE_TOKENS,
            "max_response_bytes": MAX_RESPONSE_BYTES,
            "corpus_timeout_ms": self.limits.corpus_timeout_ms,
        }
        encoded = _json(payload)
        if len(encoded) > MAX_RESPONSE_BYTES:
            raise BudgetExceededError("status response exceeds the response byte limit")
        check_deadline(deadline)
        return PublicResponse(encoded)
