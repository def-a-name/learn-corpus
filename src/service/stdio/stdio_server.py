"""以非阻塞管道和单工作线程提供本机 stdio MCP，不启动 HTTP 或网络监听。"""

from __future__ import annotations

import argparse
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
import os
from pathlib import Path
import selectors
import signal
import sys
from time import monotonic
from uuid import uuid4

from src.retrieval.lexical_store import LexicalStore, LexicalStoreError
from src.retrieval.public_core import MAX_RESPONSE_BYTES, RequestLimits, RetrievalCore
from src.service.execution_ledger_store import ExecutionLedgerStore, LedgerFailure
from src.service.mcp_protocol import (
    RPCFailure, ToolFailure, control_result, parse_envelope, prepare_message, rpc_payload,
    tool_error_result, tool_success_result,
)
from src.service.errors import TOOL_ERRORS, HTTPFailure
from src.service.ledger_config import LedgerConfig, TaskLimitsConfig
from src.service.retrieval_tasks import RetrievalTaskService
from src.service.stdio.stdio_config import StdioConfig


MAX_FRAME_BYTES = 32 * 1024
MAX_PENDING_RESPONSES = 2
POLL_SECONDS = 0.02


def _encode(payload):
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


def _event(operation, code, request_id=None):
    """诊断只含固定类别与服务生成 ID；stderr 堵塞时丢弃，不阻塞协议和退出。"""
    payload = {"transport": "mcp_stdio", "operation": operation, "error_category": code}
    if request_id is not None:
        payload["request_id"] = request_id
    try:
        os.write(2, _encode(payload))
    except OSError:
        pass


@dataclass
class _Output:
    data: bytes
    deadline: float
    releases_tool: bool = False
    offset: int = 0


class StdioServer:
    """连接状态只由主循环修改；仅一个 core 调用或生命周期操作可以在线程中执行。"""

    def __init__(
        self, config: StdioConfig, ledger_config: LedgerConfig,
        task_limits: TaskLimitsConfig | None = None,
    ):
        self.config = config
        self.ledger_config = ledger_config
        self.task_limits = task_limits or TaskLimitsConfig()
        self.core = None
        self.ledger = None
        self.tasks = None
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="corpus-stdio")
        self.future = None
        self.phase = "opening"
        self.handshake = "new"
        self.active_id = None
        self.request_id = None
        self.operation = None
        self.buffer = bytearray()
        self.frame_started = None
        self.outputs = deque()
        self.closing = False
        self.close_deadline = None
        self.exit_code = 0
        self.output_broken = False
        self.output_limit = max(
            MAX_RESPONSE_BYTES + 4096,
            len(_encode(rpc_payload("x" * 128, result=control_result("tools/list")))) + 1024,
        )

    def close(self, code=0):
        """只启动关闭宽限期，不取消工作线程或提前关闭数据库。"""
        if code:
            self.exit_code = code
        if not self.closing:
            self.closing = True
            self.close_deadline = monotonic() + self.config.shutdown_timeout_ms / 1000
            self.buffer.clear()
            self.frame_started = None

    def _open(self):
        store = LexicalStore.open_current(self.config.corpus_path)
        try:
            core = RetrievalCore(store, RequestLimits(self.config.corpus_timeout_ms))
            ledger = ExecutionLedgerStore.open(self.ledger_config)
            return core, ledger, RetrievalTaskService(core, ledger, self.task_limits)
        except BaseException:
            store.close()
            raise

    def _call(self, operation, values, request_id):
        try:
            function = getattr(self.tasks, operation)
            result = function() if operation == "status" else function(
                values, owner_key="stdio_local", request_id=request_id,
            )
            return tool_success_result(result), "ok"
        except (LexicalStoreError, LedgerFailure, HTTPFailure) as exc:
            code = exc.code if exc.code in TOOL_ERRORS else "internal_error"
            details = getattr(exc, "details", None)
        except Exception:
            code = "internal_error"
            details = None
        return tool_error_result(code, request_id, details), code

    def _queue(self, payload, *, releases_tool=False):
        data = _encode(payload)
        if len(data) > self.output_limit or len(self.outputs) >= MAX_PENDING_RESPONSES:
            raise RuntimeError("stdio output limit exceeded")
        self.outputs.append(_Output(data, monotonic() + self.config.write_timeout_ms / 1000,
                                    releases_tool))

    def _protocol_error(self, exc):
        if self.active_id is not None and exc.rpc_id == self.active_id:
            _event("protocol", "duplicate_request_id")
            self.close(2)
            return
        self._queue(rpc_payload(exc.rpc_id, error={"code": exc.code, "message": exc.message}))
        _event("protocol", "invalid_request")

    def _message(self, raw):
        value = None
        try:
            value = parse_envelope(raw)
            if "id" in value and value["id"] == self.active_id:
                # 参数校验之前拒绝重复 ID，避免错误结果被误认为原请求的结果。
                _event("protocol", "duplicate_request_id")
                self.close(2)
                return
            method, rpc_id, operation, values = prepare_message(value, self.tasks)
            if rpc_id is None:
                if method == "notifications/initialized" and self.handshake == "initializing":
                    self.handshake = "ready"
                # 取消及其他合法通知不发送响应，也不主动中止工作线程。
                return
            if method == "initialize":
                if self.handshake != "new":
                    raise RPCFailure(-32600, "Invalid Request", rpc_id)
                self.handshake = "initializing"
            elif method != "ping" and self.handshake != "ready":
                raise RPCFailure(-32600, "Server is not initialized", rpc_id)
            if operation is None:
                self._queue(rpc_payload(rpc_id, result=control_result(method)))
                return
            request_id = "req_" + uuid4().hex
            if isinstance(values, ToolFailure) or self.active_id is not None:
                code = values.code if isinstance(values, ToolFailure) else "rate_limited"
                details = values.details if isinstance(values, ToolFailure) else None
                self._queue(rpc_payload(
                    rpc_id, result=tool_error_result(code, request_id, details),
                ))
                _event(operation, code, request_id)
                return
            self.active_id, self.request_id, self.operation = rpc_id, request_id, operation
            self.future = self.pool.submit(self._call, operation, values, request_id)
            self.phase = "calling"
        except (RPCFailure, HTTPFailure) as exc:
            # 有效 notification 即使参数不合法也不生成 JSON-RPC 响应。
            if value is not None and "id" not in value:
                _event("protocol", "invalid_request")
                return
            if isinstance(exc, HTTPFailure):
                exc = RPCFailure(-32600, "Invalid Request", value.get("id") if value else None)
            self._protocol_error(exc)

    def _completed(self):
        if self.future is None or not self.future.done():
            return
        if self.phase == "calling" and not self.output_broken and len(self.outputs) >= MAX_PENDING_RESPONSES:
            return
        future, self.future = self.future, None
        try:
            result = future.result()
            if self.phase == "opening":
                self.core, self.ledger, self.tasks = result
                self.phase = "idle"
                _event("lifecycle", "ready")
            elif self.phase == "calling":
                payload, code = result
                _event(self.operation, code, self.request_id)
                self.phase = "idle"
                if not self.output_broken:
                    self._queue(rpc_payload(self.active_id, result=payload), releases_tool=True)
                else:
                    self.active_id = None
            elif self.phase == "closing":
                self.phase = "closed"
        except Exception:
            _event("lifecycle", "service_unavailable")
            self.phase = "closed" if self.phase in {"opening", "closing"} else "idle"
            self.close(2)

    def _read(self):
        try:
            data = os.read(0, min(4096, MAX_FRAME_BYTES + 1 - len(self.buffer)))
        except BlockingIOError:
            return
        if not data:
            incomplete = bool(self.buffer)
            if incomplete:
                _event("protocol", "incomplete_message")
            self.close(2 if incomplete else 0)
            return
        if not self.buffer:
            self.frame_started = monotonic()
        self.buffer.extend(data)

    def _write(self):
        output = self.outputs[0]
        try:
            count = os.write(1, output.data[output.offset:])
        except BlockingIOError:
            return
        except OSError:
            self.output_broken = True
            self.outputs.clear()
            self.close(2)
            return
        output.offset += count
        if output.offset == len(output.data):
            self.outputs.popleft()
            if output.releases_tool:
                self.active_id = None

    def _step(self):
        now = monotonic()
        if self.closing and now >= self.close_deadline:
            _event("lifecycle", "shutdown_timeout")
            os._exit(3)
        self._completed()
        if self.outputs and now >= self.outputs[0].deadline:
            _event("transport", "write_timeout")
            self.outputs.clear()
            self.output_broken = True
            self.close(2)
        if not self.closing and b"\n" not in self.buffer:
            if len(self.buffer) > MAX_FRAME_BYTES:
                _event("protocol", "request_too_large")
                self.close(2)
            elif self.frame_started is not None and now >= self.frame_started + self.config.frame_timeout_ms / 1000:
                _event("transport", "request_timeout")
                self.close(2)
        if self.closing and self.future is None and not self.outputs:
            if self.core is not None and self.phase != "closed":
                self.phase = "closing"
                def close_stores():
                    self.core.store.close()
                    self.ledger.close()
                self.future = self.pool.submit(close_stores)
            else:
                return False
        if (not self.closing and self.core is not None and b"\n" in self.buffer
                and len(self.outputs) < MAX_PENDING_RESPONSES):
            raw, _, rest = self.buffer.partition(b"\n")
            self.buffer = bytearray(rest)
            self.frame_started = now if self.buffer else None
            self._message(bytes(raw))
            return True
        with selectors.DefaultSelector() as selector:
            if not self.closing and len(self.buffer) <= MAX_FRAME_BYTES and len(self.outputs) < MAX_PENDING_RESPONSES:
                selector.register(0, selectors.EVENT_READ)
            if self.outputs:
                selector.register(1, selectors.EVENT_WRITE)
            for key, _ in selector.select(POLL_SECONDS):
                if key.fd == 0:
                    self._read()
                else:
                    self._write()
        return True

    def run(self):
        self.future = self.pool.submit(self._open)
        while True:
            try:
                if not self._step():
                    break
            except Exception:
                # 意外异常也保留工作线程，停止协议读写并进入同一关闭宽限期。
                _event("lifecycle", "internal_error")
                self.output_broken = True
                self.outputs.clear()
                self.close(2)
        self.pool.shutdown(wait=True)
        _event("lifecycle", "closed")
        return self.exit_code


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "Invalid command arguments.\n")


def main(
    config: StdioConfig | None = None, ledger_config: LedgerConfig | None = None,
    task_limits: TaskLimitsConfig | None = None,
):
    """统一入口可传入已选配置；也可用本模块读取统一配置。"""
    if os.name != "posix":
        print("Stdio transport requires POSIX pipes.", file=sys.stderr)
        return 2
    # 原生 host 提供管道；日志通道非阻塞，避免 host 不读取 stderr 时卡住退出。
    for fd in (0, 1, 2):
        os.set_blocking(fd, False)
    try:
        if config is None:
            parser = _Parser(description="Serve the read-only retrieval MCP over stdio")
            parser.add_argument("--config", type=Path, required=True, help="Path to the service JSON configuration")
            arguments = parser.parse_args()
            from src.service.config import load_service_config

            selected = load_service_config(arguments.config)
            if selected.transport != "stdio":
                raise ValueError("stdio transport is not selected")
            config, ledger_config = selected.runtime, selected.ledger
            task_limits = selected.task_limits
        if ledger_config is None:
            raise ValueError("ledger configuration is required")
        server = StdioServer(config, ledger_config, task_limits)
    except (ValueError, OSError):
        _event("lifecycle", "invalid_configuration")
        return 2
    previous = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.signal(signum, lambda *_: server.close())
    try:
        return server.run()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
