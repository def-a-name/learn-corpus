"""验证统一配置的模式隔离、严格字段和启动分派。"""

from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from src.service import server
from src.service.config import load_http_config, load_service_config
from src.service.stdio.stdio_config import StdioConfig, load_stdio_config
from test_api import HEADERS, client_for, config  # noqa: F401
from test_mcp import rpc
from test_mcp_stdio import PipeClient
from test_public_core import open_core  # noqa: F401


@pytest.fixture
def unified(config):
    values = asdict(config)
    values["corpus_path"] = str(config.corpus_path)
    values["credentials_file"] = config.credentials_file.name
    common = {key: values.pop(key) for key in ("corpus_path", "corpus_timeout_ms")}
    return {**common, "http": {**values, "host": "127.0.0.1", "port": 8765},
            "mcp": {"transport": "http", "stdio": {"frame_timeout_ms": 250}}}


def write_config(tmp_path, values):
    path = tmp_path / "synthetic-config.json"
    path.write_text(json.dumps(values))
    return path


def test_deployment_examples_share_loopback_http_baseline():
    root = Path(__file__).resolve().parents[1]
    values = json.loads((root / "config/config.json.example").read_text())
    http = values["http"]
    assert (http["host"], http["port"]) == ("127.0.0.1", 2699)
    assert http["allowed_peers"] == ["127.0.0.1"]

    nginx = (root / "config/nginx.conf.example").read_text()
    assert "proxy_pass http://127.0.0.1:2699;" in nginx
    assert f"server_name {http['allowed_hosts'][0]};" in nginx

    systemd = (root / "config/systemd.service.example").read_text()
    assert "--config /opt/learn-corpus/config/config.json" in systemd
    assert " --host " not in systemd and " --port " not in systemd


def test_unified_http_keeps_auth_and_shared_mcp_core(unified, config, tmp_path):
    path = write_config(tmp_path, unified)
    selected = load_service_config(path)
    assert selected.transport == "http" and (selected.host, selected.port) == ("127.0.0.1", 8765)
    assert selected.runtime == config
    with client_for(load_http_config(path)) as client:
        assert client.get("/v1/status").status_code == 401
        rest = client.get("/v1/status", headers=HEADERS).json()
        response = rpc(client, "tools/call", {"name": "status", "arguments": {}})
        assert response.status_code == 200
        assert response.json()["result"]["structuredContent"] == rest
    with pytest.raises(ValueError, match="cannot load stdio configuration"):
        load_stdio_config(path)


@pytest.mark.parametrize("with_http", [False, True])
def test_unified_stdio_ignores_inactive_http_settings(unified, tmp_path, with_http):
    unified["mcp"]["transport"] = "stdio"
    unified["corpus_path"] = "synthetic-index"
    if with_http:
        unified["http"] = {"credentials_file": "synthetic-missing-credentials",
                           "host": "synthetic-inactive-host", "global_concurrency": 0}
    else:
        del unified["http"]
    path = write_config(tmp_path, unified)
    selected = load_service_config(path)
    assert selected.transport == "stdio" and isinstance(selected.runtime, StdioConfig)
    assert selected.runtime.corpus_path == tmp_path / "synthetic-index"
    assert selected.runtime.frame_timeout_ms == 250
    assert selected.runtime.shutdown_timeout_ms == 6000
    assert load_stdio_config(path) == selected.runtime
    with pytest.raises(ValueError, match="cannot load service configuration"):
        load_http_config(path)


@pytest.mark.parametrize("keys, value", [
    (("mcp", "transport"), "both"),
    (("mcp", "transport"), None),
    (("mcp", "enabled"), True),
    (("mcp", "stdio", "global_concurrency"), 2),
    (("mcp", "stdio", "credentials_file"), "synthetic-secret"),
    (("http", "unknown"), True),
    (("http", "port"), True),
    (("http", "port"), 65536),
    (("http", "host"), "8.8.8.8"),
    (("http", "global_concurrency"), 0),
    (("corpus_timeout_ms",), 0),
    (("max_json_keys",), 32),
    (("max_json_array_items",), 20),
    (("max_response_bytes",), 65536),
    (("response_limits",), {}),
    (("request_timeout_ms",), 5000),
    (("http", "trusted_proxies"), []),
    (("http", "max_header_bytes"), 8192),
    (("http", "max_headers"), 64),
    (("http", "max_authorization_bytes"), 256),
    (("credentials_file",), "synthetic-secret"),
])
def test_unified_invalid_fields_fail_closed(unified, tmp_path, keys, value):
    target = unified
    for key in keys[:-1]:
        target = target[key]
    target[keys[-1]] = value
    with pytest.raises(ValueError, match="cannot load service configuration"):
        load_service_config(write_config(tmp_path, unified))


def test_unified_missing_sections_and_stdio_limits(unified, tmp_path):
    del unified["http"]
    with pytest.raises(ValueError, match="cannot load service configuration"):
        load_service_config(write_config(tmp_path, unified))
    unified["mcp"]["transport"] = "stdio"
    unified["mcp"]["stdio"]["shutdown_timeout_ms"] = 0
    with pytest.raises(ValueError, match="cannot load service configuration"):
        load_service_config(write_config(tmp_path, unified))
    del unified["mcp"]["stdio"]
    assert load_service_config(write_config(tmp_path, unified)).runtime.frame_timeout_ms == 5000
    del unified["mcp"]["transport"]
    with pytest.raises(ValueError, match="cannot load service configuration"):
        load_service_config(write_config(tmp_path, unified))


def test_unified_duplicate_keys_and_size_limit(tmp_path):
    path = tmp_path / "synthetic-config.json"
    for raw in ('{"mcp":{"transport":"http","transport":"stdio"}}', ' ' * 65537):
        path.write_text(raw)
        with pytest.raises(ValueError, match="cannot load service configuration"):
            load_service_config(path)


def test_unified_http_launcher_uses_only_config_bind(unified, tmp_path, monkeypatch):
    from src.service.http import http_server

    path = write_config(tmp_path, unified)
    calls = []
    monkeypatch.setattr(http_server.uvicorn, "run", lambda app, **kwargs: calls.append(kwargs))
    monkeypatch.setattr(sys, "argv", ["server", "--config", str(path)])
    assert server.main() == 0
    assert (calls[0]["host"], calls[0]["port"]) == ("127.0.0.1", 8765)
    assert calls[0]["workers"] == 1 and calls[0]["proxy_headers"] is False


@pytest.mark.parametrize("field", ["host", "port"])
def test_unified_http_launcher_reports_missing_bind_field(unified, tmp_path, monkeypatch, capsys, field):
    del unified["http"][field]
    path = write_config(tmp_path, unified)
    monkeypatch.setattr(sys, "argv", ["server", "--config", str(path)])
    assert server.main() == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert f"HTTP configuration is missing required fields: {field}" in output.err
    assert "Traceback" not in output.err


def test_unified_launcher_reports_invalid_config_reason(unified, tmp_path, monkeypatch, capsys):
    unified["http"]["port"] = 0
    path = write_config(tmp_path, unified)
    monkeypatch.setattr(sys, "argv", ["server", "--config", str(path)])
    assert server.main() == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "HTTP port must be an integer from 1 to 65535" in output.err
    assert "Traceback" not in output.err


def test_stdio_dispatch_never_constructs_http(unified, tmp_path, monkeypatch, capsys):
    from src.service.http import http_server
    from src.service.stdio import stdio_server

    unified["mcp"]["transport"] = "stdio"
    path = write_config(tmp_path, unified)
    calls = []

    def unexpected(*args, **kwargs):
        pytest.fail("HTTP service was started in stdio mode")

    monkeypatch.setattr(http_server, "run", unexpected)
    monkeypatch.setattr(stdio_server, "main", lambda config: calls.append(config) or 3)
    monkeypatch.setattr(sys, "argv", ["server", "--config", str(path)])
    assert server.main() == 3
    assert len(calls) == 1 and isinstance(calls[0], StdioConfig)
    for flags in (["--host", "127.0.0.1"], ["--port", "8765"]):
        monkeypatch.setattr(sys, "argv", ["server", "--config", str(path), *flags])
        with pytest.raises(SystemExit) as exc:
            server.main()
        assert exc.value.code == 2
    assert len(calls) == 1
    output = capsys.readouterr()
    assert output.out == "" and "Traceback" not in output.err


@pytest.mark.skipif(os.name != "posix", reason="POSIX pipe transport")
def test_unified_stdio_real_pipes_without_network(unified, tmp_path):
    unified["mcp"]["transport"] = "stdio"
    unified["http"] = {"credentials_file": "synthetic-missing-credentials"}
    path = write_config(tmp_path, unified)
    bootstrap = '''import socket
import sys
def forbidden(*args, **kwargs):
    raise AssertionError("Network access is forbidden in this test")
socket.socket = forbidden
from src.service.server import main
assert "fastapi" not in sys.modules and "uvicorn" not in sys.modules
raise SystemExit(main())
'''
    root = Path(__file__).resolve().parents[1]
    with (tmp_path / "synthetic-diagnostic.log").open("w+b") as diagnostic:
        proc = subprocess.Popen([sys.executable, "-c", bootstrap, "--config", str(path)],
                                cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=diagnostic, bufsize=0)
        client = PipeClient(proc)
        try:
            client.initialize()
            status = client.tool("status")["structuredContent"]
            search = client.tool("search_sources", {"queries": ["quasar"], "max_estimated_tokens": 2000})["structuredContent"]
            assert search["generation"] == status["generation"]
            read = client.tool("read_bundle", {"generation": search["generation"],
                                                "seed_item_id": search["results"][0]["item_id"],
                                                "max_estimated_tokens": 4000})["structuredContent"]
            assert read["bundle_status"] == "complete"
            assert read["bundle_key"] == search["results"][0]["bundle_key"]
            proc.stdin.close()
            assert proc.wait(timeout=8) == 0
        finally:
            if not proc.stdin.closed:
                proc.stdin.close()
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=3)
            proc.stdout.close()
