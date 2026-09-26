"""通过临时 Codex 配置验收真实 stdio 五工具，不启动模型 turn。"""

import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
from time import monotonic, sleep

import pytest

from test_api import config  # noqa: F401
from test_public_core import open_core  # noqa: F401


@pytest.mark.skipif(os.environ.get("LEARN_CORPUS_STDIO_CODEX_TEST") != "1",
                    reason="Explicit native Codex stdio integration run")
def test_native_codex_stdio_five_tools(config, tmp_path):
    codex = shutil.which("codex")
    if codex is None:
        pytest.skip("Codex executable is unavailable")
    root = Path(__file__).resolve().parents[1]
    home = tmp_path / "synthetic-client"
    home.mkdir()
    ledger = tmp_path / "synthetic-ledger"
    ledger.mkdir(mode=0o700)
    service = tmp_path / "synthetic-config.json"
    service.write_text(json.dumps({"corpus_path": str(config.corpus_path),
                                   "corpus_timeout_ms": config.corpus_timeout_ms,
                                   "mcp": {"transport": "stdio", "ledger": {
                                       "path": str(ledger / "execution.sqlite3"),
                                   }}}))
    pid_path = tmp_path / "synthetic-server.pid"
    bootstrap = ("import os; from pathlib import Path; from src.service.server import main; "
                 f"Path({str(pid_path)!r}).write_text(str(os.getpid())); raise SystemExit(main())")
    (home / "config.toml").write_text(
        '[mcp_servers.learn_corpus]\ncommand = ' + json.dumps(sys.executable) + '\n'
        'args = ' + json.dumps(["-c", bootstrap, "--config", str(service)]) + '\n'
        'cwd = ' + json.dumps(str(root)) + '\nstartup_timeout_sec = 10\ntool_timeout_sec = 10\n'
    )
    with (tmp_path / "synthetic-host.log").open("wb") as diagnostic:
        proc = subprocess.Popen([codex, "app-server"], cwd=tmp_path,
                                env={**os.environ, "CODEX_HOME": str(home)}, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=diagnostic)
        messages = queue.Queue()

        def collect():
            for line in proc.stdout:
                messages.put(json.loads(line))

        reader = threading.Thread(target=collect, daemon=True)
        reader.start()

        def call(rpc_id, method, params):
            proc.stdin.write(json.dumps({"id": rpc_id, "method": method, "params": params}).encode() + b"\n")
            proc.stdin.flush()
            while True:
                value = messages.get(timeout=20)
                if value.get("id") == rpc_id:
                    assert "error" not in value, value
                    return value["result"]

        try:
            call(1, "initialize", {"clientInfo": {"name": "synthetic-test", "version": "1"},
                                   "capabilities": {"experimentalApi": True}})
            proc.stdin.write(b'{"method":"initialized"}\n')
            proc.stdin.flush()
            thread = call(2, "thread/start", {"cwd": str(tmp_path), "ephemeral": True})["thread"]["id"]
            inventory = call(3, "mcpServerStatus/list", {"threadId": thread})
            entry = next(entry for entry in inventory["data"] if entry["name"] == "learn_corpus")
            assert {tool["name"] for tool in entry["tools"].values()} == {
                "start_retrieval_task", "search_sources", "read_bundle",
                "get_retrieval_task", "status",
            }

            def tool(rpc_id, name, args):
                result = call(rpc_id, "mcpServer/tool/call", {"threadId": thread, "server": "learn_corpus",
                                                             "tool": name, "arguments": args})
                assert not result.get("isError"), result
                return result["structuredContent"]

            status = tool(4, "status", {})
            task_id = tool(5, "start_retrieval_task", {})["task_id"]
            search = tool(6, "search_sources", {
                "task_id": task_id, "queries": ["quasar"], "max_estimated_tokens": 2000,
            })
            assert status["index_id"] == search["index_id"]
            read = tool(7, "read_bundle", {
                "task_id": task_id, "seed_item_id": search["results"][0]["item_id"],
                "max_estimated_tokens": 4000,
            })
            assert read["bundle_status"] == "complete" and len(read["items"]) == 4
            assert read["bundle_key"] == search["results"][0]["bundle_key"]
        finally:
            proc.stdin.close()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
            reader.join(timeout=2)
            proc.stdout.close()
        server_pid = int(pid_path.read_text())
        end = monotonic() + 3
        while True:
            try:
                os.kill(server_pid, 0)
            except ProcessLookupError:
                break
            assert monotonic() < end, "MCP subprocess survived host shutdown"
            sleep(0.02)
