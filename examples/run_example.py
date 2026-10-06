"""在独立临时目录演示公开文章的导入、构建与 stdio MCP 证据读取。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
from time import monotonic


CODE_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="learn-corpus-example-") as temporary:
        data = Path(temporary)
        env = dict(os.environ)
        env["LEARN_CORPUS_DATA_ROOT"] = str(data)
        env.pop("PYTHONPATH", None)

        def command(module: str, *args: str) -> None:
            subprocess.run([sys.executable, "-B", "-m", module, *args], cwd=CODE_ROOT, env=env, check=True)

        command("src.ingestion.import_articles", "--input", str(CODE_ROOT / "examples"), "--include", "sqlite-fts5.md")
        manifest = json.loads((data / "meta/manifest.json").read_text())
        for source in manifest["sources"].values():
            command("src.maintenance.scan_secrets", str(data / source["output_path"]))
        command("src.retrieval.build_lexical_index", "--publish")
        ledger = data / "ledger"
        ledger.mkdir(mode=0o700)
        config = json.loads((CODE_ROOT / "config/config.json.example").read_text())
        config["corpus_path"] = str(data / "meta/corpus")
        config["mcp"]["transport"] = "stdio"
        config["mcp"]["ledger"]["path"] = str(ledger / "execution.sqlite3")
        config.pop("http", None)
        config_path = data / "stdio.json"
        config_path.write_text(json.dumps(config))
        process = subprocess.Popen([sys.executable, "-B", "-m", "src.service.server", "--config", str(config_path)], cwd=CODE_ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        buffer = bytearray()
        next_id = 0

        def send(method: str, params: dict, request_id: int | None = None) -> None:
            payload = {"jsonrpc": "2.0", "method": method, "params": params}
            if request_id is not None:
                payload["id"] = request_id
            process.stdin.write(json.dumps(payload).encode() + b"\n")
            process.stdin.flush()

        def rpc(method: str, params: dict) -> dict:
            nonlocal buffer, next_id
            next_id += 1
            send(method, params, next_id)
            deadline = monotonic() + 20
            while b"\n" not in buffer:
                if not select.select([process.stdout], [], [], max(0, deadline - monotonic()))[0]:
                    raise RuntimeError("Example MCP response timed out")
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    raise RuntimeError("Example MCP process closed its output")
                buffer.extend(chunk)
            line, _, remaining = buffer.partition(b"\n")
            buffer = bytearray(remaining)
            response = json.loads(line)
            if response.get("id") != next_id or "error" in response:
                raise RuntimeError("Example MCP request failed")
            return response["result"]

        def tool(name: str, arguments: dict | None = None) -> dict:
            result = rpc("tools/call", {"name": name, "arguments": arguments or {}})
            if result.get("isError") or "structuredContent" not in result:
                raise RuntimeError(f"Example MCP tool failed: {name}")
            return result["structuredContent"]

        try:
            initialized = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "learn-corpus-public-example", "version": "1"}})
            if initialized["protocolVersion"] != "2025-06-18":
                raise RuntimeError("Example MCP protocol version mismatch")
            send("notifications/initialized", {})
            task = tool("start_retrieval_task")
            found = tool("search_sources", {"task_id": task["task_id"], "queries": ["unicode61"], "scopes": ["article"], "limit": 3, "max_estimated_tokens": 1600})
            if not found["results"]:
                raise RuntimeError("Example query did not find a source")
            seed = found["results"][0]
            bundle = tool("read_bundle", {"task_id": task["task_id"], "seed_item_id": seed["item_id"], "max_estimated_tokens": 8000})
            if not bundle["items"] or not any("unicode61" in item["body"].lower() for item in bundle["items"]):
                raise RuntimeError("Example source body was not returned")
            detail = tool("get_retrieval_task", {"task_id": task["task_id"]})
            status = tool("status")
            print(json.dumps({"query": "unicode61", "search_hits": len(found["results"]), "read_items": len(bundle["items"]), "source_title": bundle["items"][0]["source_title"], "index_id": status["index_id"], "task_recorded": detail["task_id"] == task["task_id"]}, ensure_ascii=False))
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            process.stdout.close()
            process.stderr.close()


if __name__ == "__main__":
    main()
