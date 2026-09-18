"""使用 FastAPI 接入只读检索核心，入口校验先于业务调用。"""

from __future__ import annotations

import ipaddress
import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from functools import partial
from time import monotonic
from uuid import uuid4

import anyio
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.responses import Response

from src.retrieval.lexical_store import LexicalStore, LexicalStoreError
from src.retrieval.public_core import RequestLimits, RetrievalCore
from src.service.execution_ledger_store import ExecutionLedgerStore
from src.service.http.http_config import (
    MAX_AUTHORIZATION_BYTES, MAX_HEADER_BYTES, MAX_HEADERS,
    HttpConfig, load_credentials,
)
from src.service.json_boundary import strict_json
from src.service.http.openapi import build_openapi
from src.service.http import mcp
from src.service.errors import ERRORS, HTTPFailure
from src.service.http.security import Admissions, BearerVerifier
from src.service.ledger_config import LedgerConfig
from src.service.retrieval_tasks import RetrievalTaskService


_LOG = logging.getLogger("learn_corpus.service")
_ROUTES = {
    "/mcp": ("POST", "mcp"),
    "/healthz": ("GET", "health"),
    "/v1/status": ("GET", "status"),
    "/v1/search": ("POST", "search"),
    "/v1/read-bundle": ("POST", "read_bundle"),
    "/docs": ("GET", "docs"),
    "/openapi.json": ("GET", "openapi"),
}


def error_response(error: HTTPFailure, request_id: str) -> Response:
    body = json.dumps({"error": {
        "code": error.code, "message": ERRORS[error.code][1], "request_id": request_id,
    }}, separators=(",", ":")).encode("utf-8")
    return Response(body, status_code=error.status, media_type="application/json", headers=error.headers)


def connection_peer(scope, config: HttpConfig) -> str:
    peer = scope.get("client")
    try:
        source = str(ipaddress.ip_address(peer[0])) if peer else None
    except ValueError as exc:
        raise HTTPFailure("forbidden") from exc
    if source not in config.allowed_peers:
        raise HTTPFailure("forbidden")
    return source


def request_headers(scope, config: HttpConfig):
    """只使用 ASGI 原始连接对端执行来源边界，不解析转发头。"""

    connection_peer(scope, config)
    raw = scope.get("headers", [])
    if len(raw) > MAX_HEADERS or sum(len(k) + len(v) + 4 for k, v in raw) > MAX_HEADER_BYTES:
        raise HTTPFailure("request_too_large", status=431)
    headers = {}
    singletons = {"host", "origin", "authorization", "content-type", "content-length",
                  "content-encoding", "transfer-encoding",
                  "accept", "mcp-protocol-version", "mcp-session-id"}
    try:
        for raw_key, raw_value in raw:
            key = raw_key.decode("ascii").lower()
            if key == "authorization" and len(raw_value) > MAX_AUTHORIZATION_BYTES:
                raise HTTPFailure("request_too_large", status=431)
            if key in singletons:
                if key in headers or any(value < 32 or value == 127 for value in raw_value):
                    raise HTTPFailure("invalid_request")
                headers[key] = raw_value.decode("ascii")
    except UnicodeError as exc:
        raise HTTPFailure("invalid_request") from exc
    return headers


async def receive_body(receive, headers, config: HttpConfig, *, maximum=16 * 1024) -> bytes:
    length = headers.get("content-length")
    if length is not None:
        if not length.isascii() or not length.isdigit() or len(length) > 20:
            raise HTTPFailure("invalid_request")
        if int(length) > maximum:
            raise HTTPFailure("request_too_large")
    if length is not None and "transfer-encoding" in headers:
        raise HTTPFailure("invalid_request")
    body = bytearray()
    try:
        with anyio.fail_after(config.body_timeout_ms / 1000):
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    raise HTTPFailure("invalid_request")
                if message["type"] != "http.request":
                    raise HTTPFailure("invalid_request")
                block = message.get("body", b"")
                if len(body) + len(block) > maximum:
                    raise HTTPFailure("request_too_large")
                body.extend(block)
                if not message.get("more_body", False):
                    break
    except TimeoutError as exc:
        raise HTTPFailure("request_timeout") from exc
    if length is not None and int(length) != len(body):
        raise HTTPFailure("invalid_request")
    return bytes(body)


class RestBoundary:
    """统一入口控制和安全错误；不记录原始 path、请求或异常对象。"""

    def __init__(self, app, *, config, state):
        self.app, self.config, self.state = app, config, state

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            await self.app(scope, receive, send)
            return
        started = monotonic()
        request_id = "req_" + uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        operation, status, code, credential = "unknown", 500, "internal_error", None
        admitted = False
        sent = False
        client_id = None
        admission_key = "health"

        async def tracked_send(message):
            nonlocal status, sent
            if message["type"] == "http.response.start":
                status, sent = message["status"], True
            await send(message)

        try:
            headers = request_headers(scope, self.config)
            if headers.get("host") not in self.config.allowed_hosts or (
                "origin" in headers and headers["origin"] not in self.config.allowed_origins
            ):
                raise HTTPFailure("forbidden")
            route = _ROUTES.get(scope["path"])
            if route is None:
                raise HTTPFailure("not_found")
            method, operation = route
            if scope["method"] != method:
                raise HTTPFailure("method_not_allowed", headers={"Allow": method})
            if operation not in {"health", "docs", "openapi"}:
                credential = self.state.verifier.authenticate(headers.get("authorization"))
                client_id = credential.cid
                admission_key = "client:" + client_id
                scope["state"]["owner_key"] = client_id
            else:
                admission_key = operation
            if scope.get("query_string"):
                raise HTTPFailure("invalid_request")
            if "content-encoding" in headers:
                raise HTTPFailure("unsupported_media_type")
            if method == "POST" and headers.get("content-type", "").lower() != "application/json":
                raise HTTPFailure("unsupported_media_type")
            raw = await receive_body(receive, headers, self.config,
                                     maximum=32 * 1024 if operation == "mcp" else 16 * 1024)
            values = None
            if operation == "mcp":
                message = mcp.prepare(raw, headers, self.state.tasks)
                scope["state"]["mcp_message"] = message
                operation = message[2] or message[0]
                values = message[3]
            elif method == "GET":
                if raw:
                    raise HTTPFailure("invalid_request")
            else:
                values = strict_json(raw)
                values = self.state.core.validate_request(operation, values)
            self.state.admissions.acquire(admission_key)
            admitted = True
            scope["state"]["retrieval_values"] = values
            code = "ok"
            await self.app(scope, receive, tracked_send)
            code = scope["state"].get("error_code", code)
        except mcp.RPCFailure as exc:
            status, code = 400, "invalid_request"
            if not sent:
                await mcp.rpc_response(exc.rpc_id, error={"code": exc.code, "message": exc.message},
                                       status=400)(scope, receive, tracked_send)
        except HTTPFailure as exc:
            status, code = exc.status, exc.code
            if not sent:
                await error_response(exc, request_id)(scope, receive, tracked_send)
        except LexicalStoreError as exc:
            code = exc.code if exc.code in ERRORS else "internal_error"
            status = ERRORS[code][0]
            if not sent:
                await error_response(HTTPFailure(code), request_id)(scope, receive, tracked_send)
        except Exception:
            status, code = 500, "internal_error"
            if not sent:
                await error_response(HTTPFailure(code), request_id)(scope, receive, tracked_send)
        finally:
            if admitted:
                self.state.admissions.release(admission_key)
            event = {
                "timestamp": datetime.now(timezone.utc).isoformat(), "request_id": request_id,
                "transport": "mcp" if scope["path"] == "/mcp" else "rest",
                "operation": operation, "status": status,
                "error_category": code, "duration_ms": round((monotonic() - started) * 1000, 3),
            }
            if credential:
                event["key"] = credential.key
            _LOG.info(json.dumps(event, separators=(",", ":")))


def create_app(config: HttpConfig, ledger_config: LedgerConfig) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        # 启动失败不保留半初始化 store；凭据文件必须限制读取权限。
        store = None
        ledger = None
        try:
            app.state.verifier = BearerVerifier(load_credentials(config.credentials_file))
            store = await anyio.to_thread.run_sync(LexicalStore.open_current, config.corpus_path)
            app.state.core = RetrievalCore(store, RequestLimits(config.corpus_timeout_ms))
            ledger = await anyio.to_thread.run_sync(ExecutionLedgerStore.open, ledger_config)
            app.state.tasks = RetrievalTaskService(app.state.core, ledger)
            app.state.admissions = Admissions(config.global_concurrency, config.client_concurrency)
            app.state.workers = anyio.CapacityLimiter(config.global_concurrency)
            yield
        except Exception:
            _LOG.error(json.dumps({"operation": "lifecycle", "status": "failed",
                                   "error_category": "service_unavailable"}))
            raise
        finally:
            if store is not None:
                await anyio.to_thread.run_sync(store.close)
            if ledger is not None:
                await anyio.to_thread.run_sync(ledger.close)

    app = FastAPI(title="Learn Corpus HTTP API", version="1.0.0",
                  lifespan=lifespan, docs_url="/docs", redoc_url=None, openapi_url="/openapi.json",
                  swagger_ui_oauth2_redirect_url=None,
                  swagger_ui_parameters={"validatorUrl": None, "persistAuthorization": False},
                  redirect_slashes=False)
    app.add_middleware(RestBoundary, config=config, state=app.state)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, _):
        request.state.error_code = "invalid_request"
        return error_response(HTTPFailure("invalid_request"), request.state.request_id)

    @app.exception_handler(HTTPException)
    async def framework_error(request, exc):
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "internal_error")
        request.state.error_code = code
        return error_response(HTTPFailure(code), request.state.request_id)

    async def run_core(request, operation):
        function = getattr(app.state.core, operation)
        args = () if operation == "status" else (request.state.retrieval_values,)
        kwargs = {} if operation == "status" else {"request_id": request.state.request_id}
        worker = asyncio.create_task(anyio.to_thread.run_sync(
            partial(function, *args, **kwargs), abandon_on_cancel=False, limiter=app.state.workers,
        ))
        try:
            result = await asyncio.shield(worker)
        except asyncio.CancelledError:
            # 原生 Task.cancel 不受 AnyIO 的线程屏蔽保证；等实际工作结束再释放并发位。
            with anyio.CancelScope(shield=True):
                while not worker.done():
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not worker.cancelled():
                    worker.exception()
            raise
        return result

    async def run_task(request, operation):
        function = getattr(app.state.tasks, operation)
        args = () if operation == "status" else (request.state.retrieval_values,)
        kwargs = {} if operation == "status" else {
            "owner_key": request.state.owner_key,
            "request_id": request.state.request_id,
        }
        worker = asyncio.create_task(anyio.to_thread.run_sync(
            partial(function, *args, **kwargs), abandon_on_cancel=False,
            limiter=app.state.workers,
        ))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            with anyio.CancelScope(shield=True):
                while not worker.done():
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not worker.cancelled():
                    worker.exception()
            raise

    async def execute(request, operation):
        result = await run_core(request, operation)
        return Response(result.json_bytes, media_type="application/json")

    @app.post("/mcp", include_in_schema=False)
    async def mcp_endpoint(request: Request):
        return await mcp.dispatch(request, run_task)

    @app.get("/healthz")
    async def health():
        return Response(b'{"ok":true}', media_type="application/json")

    @app.get("/v1/status")
    async def status(request: Request):
        return await execute(request, "status")

    @app.post("/v1/search")
    async def search(request: Request):
        return await execute(request, "search")

    @app.post("/v1/read-bundle")
    async def read_bundle(request: Request):
        return await execute(request, "read_bundle")

    # 显式提供 schema，避免 FastAPI 根据 Request/Response 签名生成空的契约。
    # 文档元数据不参与运行期校验，成功响应继续使用 core 的原始 JSON 字节。
    schema = build_openapi()
    app.openapi = lambda: schema
    return app
