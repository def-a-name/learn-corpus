"""在 MCP adapter 与无状态检索 core 之间执行任务级账本规则。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from src.retrieval.contracts import SUPPORTED_SCOPES
from src.retrieval.lexical_store import LexicalStoreError
from src.retrieval.public_core import MAX_RESPONSE_BYTES, PublicResponse, RetrievalCore
from src.service.execution_ledger_store import ExecutionLedgerStore, LedgerFailure
from src.service.ledger_config import TaskLimitsConfig


EXECUTION_RESERVE_BYTES = 8 * 1024
MCP_CORE_RESPONSE_BYTES = MAX_RESPONSE_BYTES - EXECUTION_RESERVE_BYTES
_TASK_ID = re.compile(r"tsk_[0-9a-f]{32}")
_ITEM_ID = re.compile(r"itm_[a-z2-7]{32}")


@dataclass(frozen=True)
class PreparedTaskRequest:
    """绑定同一任务服务的已验证工具参数。"""

    owner: object
    operation: str
    task_id: str | None = None
    core_request: object = None
    item_ids: tuple[str, ...] | None = None


class RetrievalTaskService:
    """同步执行 admission、core 调用和保守结算。"""

    def __init__(
        self, core: RetrievalCore, ledger: ExecutionLedgerStore,
        task_limits: TaskLimitsConfig | None = None,
    ):
        self.core = core
        self.ledger = ledger
        self.task_limits = task_limits or TaskLimitsConfig()
        self._request_owner = object()

    def _task_id(self, value: object) -> str:
        if not isinstance(value, str) or _TASK_ID.fullmatch(value) is None:
            raise LedgerFailure("invalid_request")
        return value

    def validate_request(self, operation: str, request: object) -> PreparedTaskRequest:
        """在 admission 前完成结构与 core 查询语法校验。"""

        if isinstance(request, PreparedTaskRequest):
            if request.owner is not self._request_owner or request.operation != operation:
                raise LedgerFailure("invalid_request")
            return request
        if not isinstance(request, dict):
            raise LedgerFailure("invalid_request")
        if operation in {"start_task", "status"}:
            if request:
                raise LedgerFailure("invalid_request")
            return PreparedTaskRequest(self._request_owner, operation)
        if operation == "get_task":
            if set(request) - {"task_id", "item_ids"} or "task_id" not in request:
                raise LedgerFailure("invalid_request")
            item_ids = request.get("item_ids")
            if item_ids is not None and (
                not isinstance(item_ids, list) or not 1 <= len(item_ids) <= 20
                or len(set(item_ids)) != len(item_ids)
                or any(not isinstance(value, str) or _ITEM_ID.fullmatch(value) is None
                       for value in item_ids)
            ):
                raise LedgerFailure("invalid_request")
            return PreparedTaskRequest(
                self._request_owner, operation, self._task_id(request["task_id"]),
                item_ids=None if item_ids is None else tuple(item_ids),
            )
        if operation == "search":
            if set(request) - {
                "task_id", "queries", "scopes", "limit", "max_estimated_tokens",
            } or not {"task_id", "queries", "max_estimated_tokens"} <= request.keys():
                raise LedgerFailure("invalid_request")
            task_id = self._task_id(request["task_id"])
            core_request = self.core.validate_request(
                "search", {key: value for key, value in request.items() if key != "task_id"},
            )
            return PreparedTaskRequest(
                self._request_owner, operation, task_id, core_request,
            )
        if operation == "read_bundle":
            if set(request) != {"task_id", "seed_item_id", "max_estimated_tokens"}:
                raise LedgerFailure("invalid_request")
            task_id = self._task_id(request["task_id"])
            core_request = self.core.validate_request("read_bundle", {
                "seed_item_id": request["seed_item_id"],
                "generation": self.core.store.generation,
                "max_estimated_tokens": request["max_estimated_tokens"],
            })
            return PreparedTaskRequest(
                self._request_owner, operation, task_id, core_request,
            )
        raise LedgerFailure("invalid_request")

    def _prepared(self, operation: str, request: object) -> PreparedTaskRequest:
        return self.validate_request(operation, request)

    def _encode(self, payload: dict) -> PublicResponse:
        encoded = self._json_bytes(payload)
        if len(encoded) > MAX_RESPONSE_BYTES:
            raise LedgerFailure("ledger_unavailable")
        return PublicResponse(encoded)

    def _json_bytes(self, payload: dict) -> bytes:
        """生成确定性紧凑 JSON；大小判断与最终返回使用同一份编码。"""

        try:
            return json.dumps(
                payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError) as exc:
            raise LedgerFailure("ledger_unavailable") from exc

    def start_task(
        self, request: object, *, owner_key: str, request_id: str | None = None,
    ) -> PublicResponse:
        self._prepared("start_task", request)
        return self._encode(self.ledger.create_task(owner_key, self.task_limits))

    def get_task(
        self, request: object, *, owner_key: str, request_id: str | None = None,
    ) -> PublicResponse:
        prepared = self._prepared("get_task", request)
        payload = self.ledger.get_task(
            owner_key, prepared.task_id, item_ids=prepared.item_ids,
        )
        calls = payload.pop("calls")
        items = payload.pop("server_returned_items")
        calls_were_truncated = payload["calls_truncated"]
        items_were_truncated = payload["items_truncated"]
        payload["calls"] = []
        payload["server_returned_items"] = []
        payload["calls_truncated"] = calls_were_truncated or bool(calls)
        payload["items_truncated"] = items_were_truncated or bool(items)

        # 两类明细交替加入，分别保留最近的连续窗口；任一下一条放不下时，
        # 只停止该类，另一类仍可使用剩余响应字节。字段永远整条保留。
        call_candidates = list(reversed(calls))
        item_candidates = items
        call_index = 0
        item_index = 0
        call_blocked = False
        item_blocked = False
        while not (call_blocked and item_blocked):
            if not call_blocked:
                if call_index >= len(call_candidates):
                    call_blocked = True
                else:
                    candidate = call_candidates[call_index]
                    payload["calls"].insert(0, candidate)
                    payload["calls_truncated"] = (
                        calls_were_truncated
                        or len(calls) > len(payload["calls"])
                    )
                    if len(self._json_bytes(payload)) <= MAX_RESPONSE_BYTES:
                        call_index += 1
                    else:
                        payload["calls"].pop(0)
                        payload["calls_truncated"] = True
                        call_blocked = True
            if not item_blocked:
                if item_index >= len(item_candidates):
                    item_blocked = True
                else:
                    candidate = item_candidates[item_index]
                    payload["server_returned_items"].append(candidate)
                    payload["items_truncated"] = (
                        items_were_truncated
                        or len(items) > len(payload["server_returned_items"])
                    )
                    if len(self._json_bytes(payload)) <= MAX_RESPONSE_BYTES:
                        item_index += 1
                    else:
                        payload["server_returned_items"].pop()
                        payload["items_truncated"] = True
                        item_blocked = True
        return self._encode(payload)

    def _fail_after_admission(self, call_id: str, code: str) -> None:
        block = code in {"generation_mismatch", "item_not_found", "index_unavailable", "internal_error"}
        self.ledger.finalize_failure(call_id, code, block=block)

    def search(
        self, request: object, *, owner_key: str, request_id: str,
    ) -> PublicResponse:
        prepared = self._prepared("search", request)
        values = prepared.core_request
        scopes = tuple(sorted(values.scopes or SUPPORTED_SCOPES))
        self.ledger.admit_search(
            owner_key, prepared.task_id, request_id,
            queries=tuple(value.normalized_query for value in values.compiled),
            query_keys=tuple(value.dedupe_key for value in values.compiled),
            scopes=scopes, result_limit=values.limit, cap=values.max_tokens,
            current_generation=self.core.store.generation,
        )
        try:
            response = self.core.search(
                values, request_id=request_id, max_response_bytes=MCP_CORE_RESPONSE_BYTES,
                include_source_location=False,
            )
        except LexicalStoreError as exc:
            self._fail_after_admission(request_id, exc.code)
            raise
        except Exception:
            self._fail_after_admission(request_id, "internal_error")
            raise
        payload = response.payload
        payload["execution"] = self.ledger.finalize_success(
            request_id, payload, source_locations=response.source_locations,
        )
        return self._encode(payload)

    def read_bundle(
        self, request: object, *, owner_key: str, request_id: str,
    ) -> PublicResponse:
        prepared = self._prepared("read_bundle", request)
        values = prepared.core_request
        admission = self.ledger.admit_read(
            owner_key, prepared.task_id, request_id,
            seed_item_id=values.seed_item_id, cap=values.max_tokens,
            current_generation=self.core.store.generation,
        )
        # generation 由任务固定；初版不接受客户端搬运或覆盖。
        values = self.core.validate_request("read_bundle", {
            "seed_item_id": values.seed_item_id,
            "generation": admission["generation"],
            "max_estimated_tokens": values.max_tokens,
        })
        try:
            response = self.core.read_bundle(
                values, request_id=request_id, max_response_bytes=MCP_CORE_RESPONSE_BYTES,
                include_source_location=False,
            )
        except LexicalStoreError as exc:
            self._fail_after_admission(request_id, exc.code)
            raise
        except Exception:
            self._fail_after_admission(request_id, "internal_error")
            raise
        payload = response.payload
        payload["execution"] = self.ledger.finalize_success(
            request_id, payload, source_locations=response.source_locations,
        )
        return self._encode(payload)

    def status(self) -> PublicResponse:
        payload = self.core.status().payload
        try:
            health = self.ledger.health()
        except LedgerFailure:
            health = {
                "status": "unavailable", "task_count": None, "database_bytes": None,
                "max_tasks": self.ledger.config.max_tasks,
                "max_mb": self.ledger.config.max_mb,
            }
        payload["execution_ledger"] = health
        return self._encode(payload)
