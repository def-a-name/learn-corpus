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
from src.retrieval.public_core import RetrievalCore
from src.service.config import RestConfig, load_credentials, strict_json
from src.service.security import (
    Admissions, BearerVerifier, ERRORS, HTTPFailure, TokenBuckets,
)


_LOG = logging.getLogger("learn_corpus.service")
_ROUTES = {
    "/healthz": ("GET", "health"),
    "/v1/status": ("GET", "status"),
    "/v1/search": ("POST", "search"),
    "/v1/read-bundle": ("POST", "read_bundle"),
}


def error_response(error: HTTPFailure, request_id: str) -> Response:
    body = json.dumps({"error": {
        "code": error.code, "message": ERRORS[error.code][1], "request_id": request_id,
    }}, separators=(",", ":")).encode("utf-8")
    return Response(body, status_code=error.status, media_type="application/json", headers=error.headers)


def connection_peer(scope, config: RestConfig) -> str:
    peer = scope.get("client")
    try:
        source = str(ipaddress.ip_address(peer[0])) if peer else None
    except ValueError as exc:
        raise HTTPFailure("forbidden") from exc
    if source not in config.allowed_peers:
        raise HTTPFailure("forbidden")
    return source


def headers_and_source(scope, config: RestConfig):
    """使用 ASGI 原始连接对端判断代理信任，不接受任意转发链。"""

    source = connection_peer(scope, config)
    raw = scope.get("headers", [])
    if len(raw) > config.max_headers or sum(len(k) + len(v) + 4 for k, v in raw) > config.max_header_bytes:
        raise HTTPFailure("request_too_large", status=431)
    headers = {}
    singletons = {"host", "origin", "authorization", "content-type", "content-length",
                  "content-encoding", "transfer-encoding", "x-forwarded-for"}
    try:
        for raw_key, raw_value in raw:
            key = raw_key.decode("ascii").lower()
            if key == "authorization" and len(raw_value) > config.max_authorization_bytes:
                raise HTTPFailure("request_too_large", status=431)
            if key in singletons:
                if key in headers or any(value < 32 or value == 127 for value in raw_value):
                    raise HTTPFailure("invalid_request")
                headers[key] = raw_value.decode("ascii")
    except UnicodeError as exc:
        raise HTTPFailure("invalid_request") from exc
    if source in config.trusted_proxies:
        # Nginx 必须覆盖为一个 IP；缺失或逗号链不降级为客户端可控身份。
        try:
            source = str(ipaddress.ip_address(headers.get("x-forwarded-for", "")))
        except ValueError as exc:
            raise HTTPFailure("invalid_request") from exc
    return headers, source


async def receive_body(receive, headers, config: RestConfig) -> bytes:
    maximum = 16 * 1024
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
            peer = connection_peer(scope, self.config)
            try:
                headers, source = headers_and_source(scope, self.config)
            except HTTPFailure:
                # 头部损坏时无法采信转发地址，仍按已确认的连接对端计入入口额度。
                self.state.ip_buckets.charge(peer, 1)
                raise
            self.state.ip_buckets.charge(source, 1)
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
            if operation != "health":
                credential = self.state.verifier.authenticate(headers.get("authorization"))
                client_id = credential.client_id
                admission_key = "client:" + client_id
            if scope.get("query_string"):
                raise HTTPFailure("invalid_request")
            if "content-encoding" in headers:
                raise HTTPFailure("unsupported_media_type")
            if method == "POST" and headers.get("content-type", "").lower() != "application/json":
                raise HTTPFailure("unsupported_media_type")
            raw = await receive_body(receive, headers, self.config)
            values = None
            if method == "GET":
                if raw:
                    raise HTTPFailure("invalid_request")
            else:
                values = strict_json(raw, max_keys=self.config.max_json_keys,
                                     max_array_items=self.config.max_json_array_items)
                values = self.state.core.validate_request(operation, values)
            if credential:
                cost = 1 if operation == "status" else values.cost
                self.state.client_buckets.charge(client_id, cost)
            self.state.admissions.acquire(admission_key)
            admitted = True
            scope["state"]["retrieval_values"] = values
            code = "ok"
            await self.app(scope, receive, tracked_send)
            code = scope["state"].get("error_code", code)
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
                "transport": "rest", "operation": operation, "status": status,
                "error_category": code, "duration_ms": round((monotonic() - started) * 1000, 3),
            }
            if credential:
                event["key_id"] = credential.key_id
            _LOG.info(json.dumps(event, separators=(",", ":")))


def create_app(config: RestConfig) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        # 启动失败不保留半初始化 store；配置文件中仅保存随机凭据的 verifier。
        store = None
        try:
            app.state.verifier = BearerVerifier(load_credentials(config.credentials_file))
            store = await anyio.to_thread.run_sync(LexicalStore.open_current, config.retrieval_root)
            app.state.core = RetrievalCore(store, config.response_limits)
            app.state.ip_buckets = TokenBuckets(config.ip_rate, config.max_ip_buckets)
            app.state.client_buckets = TokenBuckets(config.client_rate, len(app.state.verifier.client_ids))
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

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None,
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

    async def execute(request, operation):
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
        return Response(result.json_bytes, media_type="application/json")

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

    return app
