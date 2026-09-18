from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
from time import monotonic, sleep

import pytest

from src.service.mcp_protocol import PROTOCOL_VERSION, tool_definitions
from src.service.stdio.stdio_config import StdioConfig, load_stdio_config
from test_api import HEADERS, client_for, config  # noqa: F401
from test_mcp import call as http_call
from test_public_core import open_core, parts  # noqa: F401


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX pipe transport")


class PipeClient:
    def __init__(self, proc):
        self.proc = proc
        self.buffer = bytearray()
        self.next_id = 1

    def write(self, raw, timeout=5):
        end = monotonic() + timeout
        while raw:
            assert select.select([], [self.proc.stdin], [], max(0, end - monotonic()))[1], "stdin blocked"
            count = os.write(self.proc.stdin.fileno(), raw[:4096])
            raw = raw[count:]

    def send(self, method, params=None, *, rpc_id=None):
        payload = {"jsonrpc": "2.0", "method": method}
        if rpc_id is not None:
            payload["id"] = rpc_id
        if params is not None:
            payload["params"] = params
        self.write(json.dumps(payload, ensure_ascii=False).encode() + b"\n")

    def receive(self, timeout=5):
        end = monotonic() + timeout
        while b"\n" not in self.buffer:
            assert select.select([self.proc.stdout], [], [], max(0, end - monotonic()))[0], "response timed out"
            raw = os.read(self.proc.stdout.fileno(), 65536)
            assert raw, f"stdout closed, exit={self.proc.poll()}"
            self.buffer.extend(raw)
        line, _, rest = self.buffer.partition(b"\n")
        self.buffer = bytearray(rest)
        return json.loads(line)

    def rpc(self, method, params=None):
        rpc_id, self.next_id = self.next_id, self.next_id + 1
        self.send(method, params, rpc_id=rpc_id)
        value = self.receive()
        assert value["id"] == rpc_id, value
        return value

    def initialize(self):
        response = self.rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "synthetic-client", "version": "1"},
        })
        assert response["result"]["protocolVersion"] == PROTOCOL_VERSION
        self.send("notifications/initialized")

    def tool(self, name, arguments=None):
        return self.rpc("tools/call", {"name": name, "arguments": arguments or {}})["result"]


@pytest.fixture
def launch(config, tmp_path):
    counter = 0

    @contextmanager
    def start(*, bootstrap=None, initialize=True, service_config=None, **overrides):
        nonlocal counter
        directory = tmp_path / f"synthetic-process-{counter}"
        counter += 1
        directory.mkdir()
        ledger_directory = directory / "ledger"
        ledger_directory.mkdir(mode=0o700)
        selected = service_config or config
        corpus_path = overrides.pop("corpus_path", str(selected.corpus_path))
        corpus_timeout_ms = overrides.pop("corpus_timeout_ms", selected.corpus_timeout_ms)
        values = {
            "corpus_path": corpus_path,
            "corpus_timeout_ms": corpus_timeout_ms,
            "mcp": {"transport": "stdio",
                    "ledger": {"path": str(ledger_directory / "execution.sqlite3")},
                    "stdio": overrides},
        }
        path = directory / "stdio.json"
        path.write_text(json.dumps(values))
        args = [sys.executable, "-m", "src.service.stdio.stdio_server"] if bootstrap is None else [sys.executable, "-c", bootstrap]
        with (directory / "stderr.log").open("w+b") as diagnostic:
            proc = subprocess.Popen([*args, "--config", str(path)], cwd=ROOT,
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=diagnostic, bufsize=0)
            client = PipeClient(proc)
            try:
                if initialize:
                    client.initialize()
                yield client, directory
            finally:
                if proc.stdin and not proc.stdin.closed:
                    proc.stdin.close()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
                proc.stdout.close()
    return start


def error_code(result):
    assert result["isError"] is True and "structuredContent" not in result
    return json.loads(result["content"][0]["text"])["error"]["code"]


def start_task(client):
    result = client.tool("start_retrieval_task")
    assert result["isError"] is False
    return result["structuredContent"]["task_id"]


def test_handshake_controls_and_stdout(launch):
    with launch(initialize=False) as (client, directory):
        assert client.rpc("tools/list")["error"]["code"] == -32600
        assert client.rpc("ping")["result"] == {}
        client.initialize()
        assert client.rpc("tools/list")["result"]["tools"] == tool_definitions()
        client.send("notifications/cancelled", {"requestId": 999})
        client.send("notifications/cancelled", {"requestId": False})
        client.send("notifications/unknown")
        assert client.rpc("ping")["result"] == {}
        assert client.tool("status")["isError"] is False
        assert client.rpc("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                          "clientInfo": {"name": "synthetic", "version": "1"}})["error"]["code"] == -32600
    logs = (directory / "stderr.log").read_text()
    assert '"transport":"mcp_stdio"' in logs
    assert "Traceback" not in logs and "synthetic-process" not in logs


@pytest.mark.parametrize("query", [
    {"queries": ["quasar"], "max_estimated_tokens": 2000},
    {"queries": ["absentword"], "max_estimated_tokens": 2000},
    {"queries": ["quasar"], "scopes": ["article"], "max_estimated_tokens": 2000},
])
def test_rest_http_stdio_parity(launch, config, query):
    with launch() as (pipe, _), client_for(config) as http:
        stdio_task = start_task(pipe)
        http_task = http_call(http, "start_retrieval_task").json()["result"]["structuredContent"]["task_id"]
        stdio = pipe.tool("search_sources", {"task_id": stdio_task, **query})
        mcp = http_call(http, "search_sources", {"task_id": http_task, **query}).json()["result"]
        rest = http.post("/v1/search", json=query, headers=HEADERS).json()
        payload = stdio["structuredContent"]
        http_payload = mcp["structuredContent"]
        stdio_execution = payload.pop("execution")
        http_execution = http_payload.pop("execution")
        http_payload["request_id"] = rest["request_id"] = payload["request_id"]
        assert payload == http_payload == rest
        assert stdio_execution["task_id"] == stdio_task
        assert http_execution["task_id"] == http_task
        status = pipe.tool("status")["structuredContent"]
        status.pop("execution_ledger")
        assert status == http.get("/v1/status", headers=HEADERS).json()
        if not payload["results"]:
            return
        rest_args = {"generation": payload["generation"],
                     "seed_item_id": payload["results"][0]["item_id"],
                     "max_estimated_tokens": 450}
        stdio = pipe.tool("read_bundle", {"task_id": stdio_task,
                                           "seed_item_id": rest_args["seed_item_id"],
                                           "max_estimated_tokens": 450})
        mcp = http_call(http, "read_bundle", {"task_id": http_task,
                                               "seed_item_id": rest_args["seed_item_id"],
                                               "max_estimated_tokens": 450}).json()["result"]
        rest = http.post("/v1/read-bundle", json=rest_args, headers=HEADERS).json()
        if stdio["isError"]:
            assert error_code(stdio) == error_code(mcp) == rest["error"]["code"]
        else:
            stdio_payload = stdio["structuredContent"]
            http_payload = mcp["structuredContent"]
            stdio_payload.pop("execution")
            http_payload.pop("execution")
            http_payload["request_id"] = rest["request_id"] = stdio_payload["request_id"]
            assert stdio_payload == http_payload == rest


@pytest.mark.parametrize("scope", ["note", "article"])
def test_unicode_sections_and_byte_budget(launch, config, open_core, scope):
    core = open_core(parts(scope, "Synthetic", ('# Synthetic\nquasar 虚构正文 "quoted"', 'quasar 中文续篇')))
    selected = replace(config, corpus_path=core.store._generation_path.parent.parent)
    with launch(service_config=selected) as (pipe, _), client_for(selected) as http:
        task_id = start_task(pipe)
        query = {"task_id": task_id, "queries": ["虚构正文"], "scopes": [scope],
                 "max_estimated_tokens": 2000}
        payload = pipe.tool("search_sources", query)["structuredContent"]
        args = {"task_id": task_id, "seed_item_id": payload["results"][0]["item_id"],
                "max_estimated_tokens": 4000}
        result = pipe.tool("read_bundle", args)["structuredContent"]
        rest = http.post("/v1/read-bundle", json={
            "generation": payload["generation"], "seed_item_id": args["seed_item_id"],
            "max_estimated_tokens": args["max_estimated_tokens"],
        }, headers=HEADERS).json()
        rest["request_id"] = result["request_id"]
        result.pop("execution")
        assert rest == result and len(result["items"]) == 2


@pytest.mark.parametrize("raw,code", [
    (b'{', -32700), (b'[]', -32600), (b'\xff', -32700),
    (b'{"jsonrpc":"2.0","jsonrpc":"2.0"}', -32700),
    (b'{"jsonrpc":"2.0","id":true,"method":"ping"}', -32600),
    (b'[[[[[[[[[0]]]]]]]]]', -32600), (br'"\ud800"', -32700),
])
def test_malformed_frames_are_bounded_and_connection_recovers(launch, raw, code):
    with launch() as (client, _):
        client.write(raw + b"\n")
        assert client.receive()["error"]["code"] == code
        assert client.rpc("ping")["result"] == {}


def test_fragmented_and_consecutive_frames(launch):
    with launch() as (client, _):
        raw = b'{"jsonrpc":"2.0","id":100,"method":"ping"}'
        client.write(raw[:12])
        assert not select.select([client.proc.stdout], [], [], 0.03)[0]
        client.write(raw[12:] + b'\n{"jsonrpc":"2.0","id":101,"method":"ping"}\r\n')
        assert [client.receive()["id"], client.receive()["id"]] == [100, 101]
        client.write(raw + b" " * (32768 - len(raw)) + b"\n")
        assert client.receive()["result"] == {}


@pytest.mark.parametrize("ending", [b"", b"\n"])
def test_oversize_frame_closes_without_reflecting_content(launch, ending):
    with launch() as (client, directory):
        client.write(b"x" * 32769 + ending)
        assert client.proc.wait(timeout=3) == 2
        assert os.read(client.proc.stdout.fileno(), 4096) == b""
    assert "request_too_large" in (directory / "stderr.log").read_text()


def test_partial_frame_timeout_does_not_apply_to_idle_connection(launch):
    with launch(frame_timeout_ms=80) as (client, directory):
        sleep(0.16)
        assert client.rpc("ping")["result"] == {}
        client.write(b'{"jsonrpc":')
        assert client.proc.wait(timeout=3) == 2
    assert "request_timeout" in (directory / "stderr.log").read_text()


def _blocked_search(marker, release):
    # 只在测试子进程中模拟受阻计算，不给生产入口增加故障注入参数。
    return f'''from pathlib import Path
from time import sleep
from src.retrieval.public_core import RetrievalCore
from src.service.stdio.stdio_server import main
original = RetrievalCore.search
def paused(self, *args, **kwargs):
    Path({str(marker)!r}).touch()
    while not Path({str(release)!r}).exists():
        sleep(0.01)
    return original(self, *args, **kwargs)
RetrievalCore.search = paused
raise SystemExit(main())
'''


def _wait_for(path):
    end = monotonic() + 3
    while not path.exists():
        assert monotonic() < end, "worker did not start"
        sleep(0.005)


def test_busy_cancel_and_independent_processes(launch, tmp_path):
    marker, release = tmp_path / "synthetic-entered", tmp_path / "synthetic-release"
    with launch(bootstrap=_blocked_search(marker, release)) as (first, _), launch() as (second, _):
        task_id = start_task(first)
        first.send("tools/call", {"name": "search_sources", "arguments": {
            "task_id": task_id, "queries": ["quasar"], "max_estimated_tokens": 1000,
        }}, rpc_id=100)
        _wait_for(marker)
        try:
            first.send("notifications/cancelled", {"requestId": 100})
            for name in ("status", "search_sources"):
                arguments = ({"task_id": task_id, "queries": ["other"],
                              "max_estimated_tokens": 1000} if name == "search_sources" else {})
                assert error_code(first.tool(name, arguments)) == "rate_limited"
            assert first.rpc("ping")["result"] == {}
            assert second.tool("status")["isError"] is False
        finally:
            release.touch()
        assert first.receive()["id"] == 100
        assert first.tool("status")["isError"] is False


@pytest.mark.parametrize("close_kind", ["eof", "signal", "broken_output"])
def test_shutdown_grace_forces_stuck_worker_to_exit(launch, tmp_path, close_kind):
    marker, release = tmp_path / "synthetic-entered", tmp_path / "synthetic-release"
    with launch(bootstrap=_blocked_search(marker, release), shutdown_timeout_ms=120) as (client, directory):
        task_id = start_task(client)
        client.send("tools/call", {"name": "search_sources", "arguments": {
            "task_id": task_id, "queries": ["quasar"], "max_estimated_tokens": 1000,
        }}, rpc_id=100)
        _wait_for(marker)
        if close_kind == "eof":
            client.proc.stdin.close()
        elif close_kind == "signal":
            client.proc.send_signal(signal.SIGTERM)
        else:
            client.proc.stdout.close()
            client.send("ping", rpc_id=101)
        assert client.proc.wait(timeout=3) == 3
    assert "shutdown_timeout" in (directory / "stderr.log").read_text()


def test_graceful_close_waits_for_worker_then_exits(launch, tmp_path):
    marker, release = tmp_path / "synthetic-entered", tmp_path / "synthetic-release"
    with launch(bootstrap=_blocked_search(marker, release), shutdown_timeout_ms=1000) as (client, _):
        task_id = start_task(client)
        client.send("tools/call", {"name": "search_sources", "arguments": {
            "task_id": task_id, "queries": ["quasar"], "max_estimated_tokens": 1000,
        }}, rpc_id=100)
        _wait_for(marker)
        client.proc.stdin.close()
        sleep(0.05)
        assert client.proc.poll() is None
        release.touch()
        assert client.receive()["id"] == 100
        assert client.proc.wait(timeout=3) == 0


def test_core_deadline_error_does_not_poison_stdio(launch):
    script = '''from time import sleep
from src.retrieval.public_core import RetrievalCore
from src.service.stdio.stdio_server import main
original = RetrievalCore._encode
def slow(self, *args, **kwargs):
    sleep(0.12)
    return original(self, *args, **kwargs)
RetrievalCore._encode = slow
raise SystemExit(main())
'''
    with launch(bootstrap=script, corpus_timeout_ms=50) as (client, _):
        task_id = start_task(client)
        assert error_code(client.tool("search_sources", {
            "task_id": task_id, "queries": ["quasar"], "max_estimated_tokens": 1000,
        })) == "budget_exceeded"
        assert client.tool("status")["isError"] is False


def test_output_backpressure_has_bounded_exit(launch):
    with launch(write_timeout_ms=100, shutdown_timeout_ms=150) as (client, directory):
        import fcntl
        if not hasattr(fcntl, "F_SETPIPE_SZ"):
            pytest.skip("Pipe capacity control requires Linux")
        # 明确缩小管道，避免依赖机器默认容量；host 故意不读取响应。
        fcntl.fcntl(client.proc.stdout.fileno(), fcntl.F_SETPIPE_SZ, 4096)
        for rpc_id in range(100, 106):
            client.send("tools/list", rpc_id=rpc_id)
        assert client.proc.wait(timeout=3) == 2
    assert "write_timeout" in (directory / "stderr.log").read_text()


@pytest.mark.parametrize("overrides", [
    {"corpus_path": "synthetic-missing-index"}, {"corpus_timeout_ms": 0},
    {"global_concurrency": 1}, {"client_concurrency": 1}, {"credentials_file": "synthetic-secret"},
    {"frame_timeout_ms": True}, {"shutdown_timeout_ms": 0},
])
def test_startup_errors_are_safe(launch, overrides):
    with launch(initialize=False, **overrides) as (client, directory):
        assert client.proc.wait(timeout=3) == 2
        assert os.read(client.proc.stdout.fileno(), 4096) == b""
    diagnostic = (directory / "stderr.log").read_text()
    assert "synthetic-secret" not in diagnostic and "synthetic-missing" not in diagnostic
    assert "Traceback" not in diagnostic


def test_stdio_config_relative_paths_and_shutdown_default(config, tmp_path):
    path = tmp_path / "synthetic-config.json"
    path.write_text(json.dumps({
        "corpus_path": "index", "corpus_timeout_ms": config.corpus_timeout_ms,
        "mcp": {"transport": "stdio", "ledger": {"path": "ledger/execution.sqlite3"}},
    }))
    loaded = load_stdio_config(path)
    assert loaded.corpus_path == tmp_path / "index"
    assert loaded.shutdown_timeout_ms == config.corpus_timeout_ms + 1000
    path.write_text('{"corpus_path":"index","corpus_path":"duplicate"}')
    with pytest.raises(ValueError, match="cannot load stdio configuration"):
        load_stdio_config(path)


def test_generation_is_pinned_until_process_restart(launch, config):
    from src.retrieval.build_lexical_index import build_generation, publish_generation
    from src.retrieval.contracts import ProjectionResult, SourceSnapshot

    items = parts("note", "Synthetic replacement", ("quasar newer synthetic body",))
    item = items[0]
    projection = ProjectionResult(items, (SourceSnapshot(item.source_path, "b" * 64, item.source_id, item.scope, 1),),
                                  "sha256:" + "b" * 64)
    with launch() as (old, _):
        first = old.tool("status")["structuredContent"]["generation"]
        old_task = start_task(old)
        old_search = old.tool("search_sources", {
            "task_id": old_task, "queries": ["quasar"], "max_estimated_tokens": 1000,
        })["structuredContent"]
        assert old_search["generation"] == first
        newer = build_generation(projection, config.corpus_path)
        publish_generation(config.corpus_path, newer.generation)
        assert old.tool("status")["structuredContent"]["generation"] == first
        with launch() as (new, _):
            assert new.tool("status")["structuredContent"]["generation"] == newer.generation
            new_task = start_task(new)
            result = new.tool("search_sources", {
                "task_id": new_task, "queries": ["quasar"], "max_estimated_tokens": 1000,
            })["structuredContent"]
            assert result["generation"] == newer.generation


@pytest.mark.parametrize("method", ["resources/list", "prompts/list", "admin/rebuild"])
def test_no_extra_capabilities(launch, method):
    with launch() as (client, _):
        assert client.rpc(method)["error"]["code"] == -32601


def test_unexpected_tool_error_is_safe_and_does_not_close_connection(launch):
    script = '''from src.retrieval.public_core import RetrievalCore
from src.service.stdio.stdio_server import main
def fail(self, *args, **kwargs):
    raise RuntimeError("synthetic-private-query-path-token")
RetrievalCore.search = fail
raise SystemExit(main())
'''
    with launch(bootstrap=script) as (client, directory):
        task_id = start_task(client)
        result = client.tool("search_sources", {
            "task_id": task_id, "queries": ["quasar"], "max_estimated_tokens": 1000,
        })
        assert error_code(result) == "internal_error"
        assert "synthetic-private" not in json.dumps(result)
        assert client.tool("status")["isError"] is False
    assert "synthetic-private" not in (directory / "stderr.log").read_text()


@pytest.mark.parametrize("params", [{}, {"extra": True}, []])
def test_duplicate_active_id_closes_without_false_response(launch, tmp_path, params):
    marker, release = tmp_path / "synthetic-entered", tmp_path / "synthetic-release"
    with launch(bootstrap=_blocked_search(marker, release), shutdown_timeout_ms=150) as (client, directory):
        task_id = start_task(client)
        client.send("tools/call", {"name": "search_sources", "arguments": {
            "task_id": task_id, "queries": ["quasar"], "max_estimated_tokens": 1000,
        }}, rpc_id=100)
        _wait_for(marker)
        client.send("ping", params, rpc_id=100)
        assert client.proc.wait(timeout=3) == 3
        assert os.read(client.proc.stdout.fileno(), 4096) == b""
    assert "duplicate_request_id" in (directory / "stderr.log").read_text()


def test_shutdown_also_bounds_stuck_startup(launch):
    script = '''from threading import Event
from src.service.stdio.stdio_server import StdioServer, main
StdioServer._open = lambda self: Event().wait()
raise SystemExit(main())
'''
    with launch(bootstrap=script, initialize=False, shutdown_timeout_ms=100) as (client, _):
        client.proc.stdin.close()
        assert client.proc.wait(timeout=3) == 3


def test_blocked_stderr_does_not_block_protocol_or_exit(config, tmp_path):
    read_fd, write_fd = os.pipe()
    os.set_blocking(write_fd, False)
    try:
        while True:
            try:
                os.write(write_fd, b"x" * 4096)
            except BlockingIOError:
                break
        path = tmp_path / "synthetic-stdio.json"
        path.write_text(json.dumps({
            "corpus_path": str(config.corpus_path),
            "corpus_timeout_ms": config.corpus_timeout_ms,
            "mcp": {"transport": "stdio", "ledger": {"path": "ledger/execution.sqlite3"}},
        }))
        (tmp_path / "ledger").mkdir(mode=0o700, exist_ok=True)
        proc = subprocess.Popen([sys.executable, "-m", "src.service.stdio.stdio_server", "--config", str(path)],
                                cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=write_fd)
        try:
            client = PipeClient(proc)
            client.initialize()
            assert client.tool("status")["isError"] is False
            proc.stdin.close()
            assert proc.wait(timeout=3) == 0
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=3)
            proc.stdout.close()
    finally:
        os.close(read_fd)
        os.close(write_fd)
