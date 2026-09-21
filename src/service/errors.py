"""定义跨 transport 共用的固定公开错误。"""


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
    "retrieval_timeout": (408, "Retrieval processing timed out"),
}


MCP_TASK_ERRORS = {
    "task_not_found": (404, "Retrieval task not found"),
    "task_busy": (409, "Retrieval task has an unresolved call"),
    "task_blocked": (409, "Retrieval task is blocked"),
    "search_already_attempted": (409, "Search query and scope set was already attempted"),
    "seed_not_available": (409, "Read seed is not an available search candidate"),
    "seed_already_attempted": (409, "Read seed was already attempted"),
    "task_call_limit_exceeded": (429, "Retrieval task call limit exceeded"),
    "task_budget_exceeded": (422, "Retrieval task evidence budget exceeded"),
    "ledger_unavailable": (503, "Execution ledger unavailable"),
    "ledger_capacity_exceeded": (503, "Execution ledger capacity exceeded"),
}


TOOL_ERRORS = {**ERRORS, **MCP_TASK_ERRORS}


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
