"""实现冻结为 2025-06-18 的无会话 Streamable HTTP MCP 薄适配。"""

from __future__ import annotations

import json

from starlette.responses import Response

from src.retrieval.lexical_store import LexicalStoreError
from src.service.execution_ledger_store import LedgerFailure
from src.service.mcp_protocol import (
    PROTOCOL_VERSION, RPCFailure, TOOLS, ToolFailure, control_result, parse_envelope,
    prepare_message, rpc_payload, tool_definitions, tool_error_result, tool_success_result,
)
from src.service.errors import TOOL_ERRORS, HTTPFailure


def rpc_response(rpc_id, *, result=None, error=None, status=200):
    return Response(json.dumps(rpc_payload(rpc_id, result=result, error=error),
                               ensure_ascii=False, separators=(",", ":")).encode(),
                    status_code=status, media_type="application/json")


def tool_error(rpc_id, code, request_id, details=None):
    return rpc_response(rpc_id, result=tool_error_result(code, request_id, details))


def prepare(raw, headers, task_service):
    """保留 HTTP header 与封装的检查顺序，不向 stdio 注入伪请求头。"""
    accepts = {part.strip().split(";", 1)[0].lower() for part in headers.get("accept", "").split(",")}
    if not {"application/json", "text/event-stream"} <= accepts:
        raise HTTPFailure("invalid_request")
    value = parse_envelope(raw)
    version = headers.get("mcp-protocol-version")
    if version != PROTOCOL_VERSION and not (value["method"] == "initialize" and version is None):
        raise HTTPFailure("invalid_request")
    if "mcp-session-id" in headers:
        raise HTTPFailure("invalid_request")
    return prepare_message(value, task_service)


async def dispatch(request, run_core):
    method, rpc_id, operation, values = request.state.mcp_message
    if rpc_id is None:
        return Response(status_code=202)
    if operation is None:
        return rpc_response(rpc_id, result=control_result(method))
    try:
        if isinstance(values, ToolFailure):
            request.state.error_code = values.code
            return tool_error(
                rpc_id, values.code, request.state.request_id, values.details,
            )
        result = await run_core(request, operation)
    except (LexicalStoreError, LedgerFailure, HTTPFailure) as exc:
        code = exc.code if exc.code in TOOL_ERRORS else "internal_error"
        request.state.error_code = code
        return tool_error(
            rpc_id, code, request.state.request_id, getattr(exc, "details", None),
        )
    except Exception:
        request.state.error_code = "internal_error"
        return tool_error(rpc_id, "internal_error", request.state.request_id)
    return rpc_response(rpc_id, result=tool_success_result(result))
