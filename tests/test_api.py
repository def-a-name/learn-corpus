from __future__ import annotations

import hashlib
import asyncio
import json
import logging
import threading
from dataclasses import replace

import anyio
import pytest
from starlette.testclient import TestClient

from src.retrieval.lexical_store import IndexUnavailableError
from src.retrieval.public_core import RequestLimits
from src.service.config import RestConfig, strict_json
from src.service.api import create_app
from src.service.security import Admissions, BearerVerifier, Credential, HTTPFailure
from test_public_core import exchange, open_core  # noqa: F401


SECRET = "a" * 43
TOKEN = "synthetic-key." + SECRET
HEADERS = {"Authorization": "Bearer " + TOKEN, "X-Forwarded-For": "192.0.2.1"}


@pytest.fixture
def config(open_core, tmp_path):
    core = open_core(exchange())
    credentials = tmp_path / "synthetic-verifiers.json"
    credentials.write_text(json.dumps([{
        "client_id": "synthetic-client", "key_id": "synthetic-key",
        "secret_sha256": hashlib.sha256(SECRET.encode()).hexdigest(),
    }]))
    credentials.chmod(0o600)
    return RestConfig(
        retrieval_root=core.store._generation_path.parent.parent, credentials_file=credentials,
        allowed_peers=("192.0.2.2", "127.0.0.1"), trusted_proxies=("192.0.2.2",),
        allowed_hosts=("service.test",), allowed_origins=("https://client.test",),
        response_limits=RequestLimits(30_000, 5000),
        global_concurrency=2, client_concurrency=1,
        max_header_bytes=4096, max_headers=30, max_authorization_bytes=256,
        max_json_keys=20, max_json_array_items=10, body_timeout_ms=100,
    )


def client_for(config, *, peer="192.0.2.2"):
    return TestClient(create_app(config), base_url="http://service.test", client=(peer, 4000))


def assert_error(response, status, code):
    assert response.status_code == status
    value = response.json()
    assert set(value) == {"error"}
    assert set(value["error"]) == {"code", "message", "request_id"}
    assert value["error"]["code"] == code
    assert value["error"]["request_id"].startswith("req_")
    assert TOKEN not in response.text
    assert "Traceback" not in response.text


def test_search_bundle_status_and_core_json_match(config):
    with client_for(config) as client:
        query = {"queries": ["quasar"]}
        response = client.post("/v1/search", json=query, headers=HEADERS)
        assert response.status_code == 200
        payload = response.json()
        expected = client.app.state.core.search(query, request_id=payload["request_id"])
        assert response.content == expected.json_bytes
        seed = payload["results"][0]
        request = {"seed_item_id": seed["item_id"], "generation": payload["generation"]}
        bundle = client.post("/v1/read-bundle", json=request, headers=HEADERS)
        expected = client.app.state.core.read_bundle(request, request_id=bundle.json()["request_id"])
        assert bundle.content == expected.json_bytes
        assert bundle.json()["bundle_key"] == seed["bundle_key"]
        assert len(bundle.json()["items"]) == 4
        status = client.get("/v1/status", headers=HEADERS)
        assert status.content == client.app.state.core.status().json_bytes
        assert status.json()["semantic_search"] is False
        missing = client.post("/v1/search", json={"queries": ["absentword"]}, headers=HEADERS)
        assert missing.status_code == 200 and missing.json()["results"] == []


def test_health_is_only_auth_exemption_and_still_checks_origin_and_peer(config):
    with client_for(config) as client:
        assert client.get("/healthz", headers={"X-Forwarded-For": "192.0.2.1"}).content == b'{"ok":true}'
        for path in ("/v1/status", "/v1/search", "/v1/read-bundle"):
            response = client.request("GET" if path.endswith("status") else "POST", path,
                                      headers={"X-Forwarded-For": "192.0.2.1"})
            assert_error(response, 401, "unauthorized")
            assert response.headers["www-authenticate"] == "Bearer"
        assert_error(client.get("/healthz", headers={**HEADERS, "Origin": "https://evil.test"}), 403, "forbidden")
        assert_error(client.get("/healthz", headers={**HEADERS, "Host": "evil.test"}), 403, "forbidden")
    with client_for(config, peer="192.0.2.99") as client:
        assert_error(client.get("/healthz", headers=HEADERS), 403, "forbidden")


@pytest.mark.parametrize("header", [None, "Basic synthetic", "Bearer unknown." + SECRET,
                                    "Bearer synthetic-key." + "b" * 43])  # secret-scan: allow - synthetic token fixture
def test_bad_credentials_share_safe_response(config, header):
    with client_for(config) as client:
        headers = {"X-Forwarded-For": "192.0.2.1"}
        if header is not None:
            headers["Authorization"] = header
        assert_error(client.get("/v1/status", headers=headers), 401, "unauthorized")


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/mcp", "/v1/read", "/v1/search/"])
def test_only_four_exact_routes_are_exposed(config, path):
    with client_for(config) as client:
        assert_error(client.get(path, headers=HEADERS), 404, "not_found")
        response = client.get("/v1/search", headers=HEADERS)
        assert_error(response, 405, "method_not_allowed")
        assert response.headers["allow"] == "POST"


@pytest.mark.parametrize("body", [
    b'{"queries":["quasar"],"queries":["changed"]}', b'{"queries":["quasar"],"limit":true}',
    b'{"queries":["quasar"],"limit":"1"}', br'{"queries":["Stra\u00dfe","STRASSE"]}',
    b'{"queries":["quasar"],"unknown":1}', b'{"queries":["\xff"]}',
    br'{"queries":["\u0000"]}', br'{"queries":["\ud800"]}',
    br'{"queries":["\u202e"]}', b'{"queries":["quasar"],"limit":NaN}',
    b'{"queries":["quasar"],"limit":1e999}', b'{"queries":' + b'[' * 9 + b'0' + b']' * 9 + b'}',
])
def test_strict_json_and_shared_validation(config, body):
    with client_for(config) as client:
        response = client.post("/v1/search", content=body, headers={**HEADERS, "Content-Type": "application/json"})
        assert_error(response, 400, "invalid_request")


def test_http_size_media_and_query_boundaries(config):
    with client_for(config) as client:
        assert_error(client.post("/v1/search", content=b"x" * 16385,
                                headers={**HEADERS, "Content-Type": "application/json"}), 413, "request_too_large")
        assert_error(client.post("/v1/search", content="{}", headers=HEADERS), 415, "unsupported_media_type")
        assert_error(client.post("/v1/search", json={}, headers={**HEADERS, "Content-Encoding": "gzip"}),
                     415, "unsupported_media_type")
        assert_error(client.get("/v1/status?secret=synthetic", headers=HEADERS), 400, "invalid_request")
        assert_error(client.get("/v1/status", headers={**HEADERS, "Authorization": "x" * 257}),
                     431, "request_too_large")
        assert_error(client.get("/v1/status", headers={**HEADERS, "X-Synthetic": "x" * 4096}),
                     431, "request_too_large")


def test_proxy_requires_single_overwritten_ip_and_direct_clients_cannot_spoof(config):
    with client_for(config) as client:
        for value in (None, "192.0.2.1, 192.0.2.3", "not-an-ip"):
            headers = dict(HEADERS)
            if value is None:
                del headers["X-Forwarded-For"]
            else:
                headers["X-Forwarded-For"] = value
            assert_error(client.get("/healthz", headers=headers), 400, "invalid_request")
    with client_for(config, peer="127.0.0.1") as client:
        for value in ("192.0.2.9", "not-an-ip"):
            assert client.get("/healthz", headers={"X-Forwarded-For": value}).status_code == 200



def test_core_errors_and_unexpected_errors_are_sanitized_and_logged(config, monkeypatch, caplog):
    with client_for(config) as client:
        mismatch = client.post("/v1/search", json={"queries": ["quasar"], "generation": "gen_" + "b" * 20}, headers=HEADERS)
        assert_error(mismatch, 409, "generation_mismatch")
        missing = client.post("/v1/read-bundle", json={"seed_item_id": "itm_" + "b" * 32,
                                                     "generation": "gen_" + "a" * 20}, headers=HEADERS)
        assert_error(missing, 404, "item_not_found")
        budget = client.post("/v1/search", json={"queries": ["quasar"], "max_estimated_tokens": 1}, headers=HEADERS)
        assert_error(budget, 422, "budget_exceeded")
        for exception, status, code in ((IndexUnavailableError, 503, "index_unavailable"),
                                        (RuntimeError, 500, "internal_error")):
            def fail(*args, **kwargs):
                raise exception("synthetic-private-content " + TOKEN)
            monkeypatch.setattr(client.app.state.core, "search", fail)
            with caplog.at_level(logging.INFO, logger="learn_corpus.service"):
                response = client.post("/v1/search", json={"queries": ["quasar"]}, headers=HEADERS)
            assert_error(response, status, code)
        assert "quasar" not in caplog.text
        assert "synthetic-private-content" not in caplog.text
        assert TOKEN not in caplog.text
        assert "sources/" not in caplog.text


def test_concurrency_rejects_without_queue_and_releases_after_failure(config, monkeypatch):
    with client_for(config) as client:
        entered, release = threading.Event(), threading.Event()
        original = client.app.state.core.status
        responses = []
        def block():
            entered.set()
            assert release.wait(2)
            return original()
        monkeypatch.setattr(client.app.state.core, "status", block)
        thread = threading.Thread(target=lambda: responses.append(client.get("/v1/status", headers=HEADERS)))
        thread.start()
        try:
            assert entered.wait(1)
            response = client.get("/v1/status", headers=HEADERS)
            assert_error(response, 429, "rate_limited")
            assert "retry-after" not in response.headers
        finally:
            release.set()
            thread.join(2)
        assert responses[0].status_code == 200
        assert client.get("/v1/status", headers=HEADERS).status_code == 200


def test_lifespan_closes_store_and_rejects_bad_credentials_or_index(config):
    with client_for(config) as client:
        store = client.app.state.core.store
    with pytest.raises(IndexUnavailableError):
        store.read_item(exchange()[0].item_id, store.generation)
    config.credentials_file.chmod(0o644)
    with pytest.raises(ValueError, match="cannot load credential"):
        with client_for(config):
            pass
    config.credentials_file.chmod(0o600)
    with pytest.raises(IndexUnavailableError):
        with client_for(replace(config, retrieval_root=config.retrieval_root / "missing")):
            pass


def test_rotation_keys_share_client_and_revocation_uses_new_verifier():
    old = Credential("synthetic-client", "old-key", hashlib.sha256(SECRET.encode()).hexdigest())
    new_secret = "b" * 43
    new = Credential("synthetic-client", "new-key", hashlib.sha256(new_secret.encode()).hexdigest())
    both = BearerVerifier((old, new))
    assert both.authenticate("Bearer old-key." + SECRET).client_id == both.authenticate("Bearer new-key." + new_secret).client_id
    with pytest.raises(HTTPFailure):
        BearerVerifier((new,)).authenticate("Bearer old-key." + SECRET)


def test_global_and_client_admission_limits_are_distinct():
    admissions = Admissions(2, 1)
    admissions.acquire("a")
    with pytest.raises(HTTPFailure):
        admissions.acquire("a")
    admissions.acquire("b")
    with pytest.raises(HTTPFailure):
        admissions.acquire("c")
    admissions.release("a")
    admissions.acquire("c")


def test_parser_limits_total_keys_and_arrays():
    for raw, keys, arrays in ((b'{"a":{"b":1}}', 1, 5), (b'[1,2,3]', 10, 2)):
        with pytest.raises(HTTPFailure):
            strict_json(raw, max_keys=keys, max_array_items=arrays)


def test_slow_body_and_stream_without_content_length(config):
    from src.service.api import receive_body
    async def exercise():
        async def slow():
            await anyio.sleep(1)
        with pytest.raises(HTTPFailure) as timeout:
            await receive_body(slow, {}, replace(config, body_timeout_ms=5))
        assert timeout.value.code == "request_timeout"
        async def oversized():
            return {"type": "http.request", "body": b"x" * 16385}
        with pytest.raises(HTTPFailure) as size:
            await receive_body(oversized, {}, config)
        assert size.value.status == 413
    anyio.run(exercise)


def raw_scope(path="/v1/search", *, extra_headers=()):
    return {
        "type": "http", "http_version": "1.1", "asgi": {"version": "3.0"},
        "method": "POST", "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": b"", "root_path": "", "client": ("192.0.2.2", 4000),
        "server": ("service.test", 80), "headers": [
            (b"host", b"service.test"), (b"x-forwarded-for", b"192.0.2.1"),
            (b"authorization", ("Bearer " + TOKEN).encode()),
            (b"content-type", b"application/json"), *extra_headers,
        ],
    }


def test_raw_asgi_body_timeout_and_duplicate_headers_return_error_json(config):
    async def exercise():
        app = create_app(replace(config, body_timeout_ms=5))
        async with app.router.lifespan_context(app):
            for scope, slow, expected in ((raw_scope(), True, 408),
                                          (raw_scope(extra_headers=((b"authorization", b"duplicate"),)), False, 400)):
                messages = []
                async def receive():
                    if slow:
                        await anyio.sleep(1)
                    return {"type": "http.request", "body": b'{"queries":["quasar"]}'}
                async def send(message):
                    messages.append(message)
                await app(scope, receive, send)
                assert messages[0]["status"] == expected
                assert set(json.loads(messages[1]["body"])) == {"error"}
    anyio.run(exercise)


def test_request_cancellation_keeps_admission_until_worker_finishes(config, monkeypatch):
    async def exercise():
        app = create_app(config)
        async with app.router.lifespan_context(app):
            entered, release = threading.Event(), threading.Event()
            original = app.state.core.search
            def block(*args, **kwargs):
                entered.set()
                assert release.wait(2)
                return original(*args, **kwargs)
            monkeypatch.setattr(app.state.core, "search", block)
            async def receive():
                return {"type": "http.request", "body": b'{"queries":["quasar"]}'}
            async def send(_):
                pass
            task = asyncio.create_task(app(raw_scope(), receive, send))
            try:
                with anyio.fail_after(1):
                    while not entered.is_set():
                        await anyio.sleep(0.001)
                task.cancel()
                await anyio.sleep(0.01)
                assert app.state.admissions._total == 1
            finally:
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert app.state.admissions._total == 0
    anyio.run(exercise)


def test_config_loader_and_launcher_preserve_trust_boundary(config, tmp_path, monkeypatch):
    from dataclasses import asdict
    from src.service.config import load_config
    from src.service import server

    values = asdict(config)
    values["retrieval_root"] = str(config.retrieval_root)
    values["credentials_file"] = config.credentials_file.name
    path = tmp_path / "synthetic-service.json"
    path.write_text(json.dumps(values))
    assert load_config(path).credentials_file == config.credentials_file
    calls = []
    monkeypatch.setattr(server.uvicorn, "run", lambda app, **kwargs: calls.append(kwargs))
    monkeypatch.setattr("sys.argv", ["server", "--config", str(path), "--host", "127.0.0.1", "--port", "8765"])
    assert server.main() == 0
    assert calls[0]["workers"] == 1 and calls[0]["proxy_headers"] is False
    assert calls[0]["access_log"] is False and calls[0]["server_header"] is False
    log_config = calls[0]["log_config"]
    assert log_config["loggers"]["uvicorn.error"]["handlers"] == ["discard"]
    assert log_config["handlers"]["discard"]["class"] == "logging.NullHandler"
    values["unknown"] = True
    path.write_text(json.dumps(values))
    with pytest.raises(ValueError, match="cannot load service configuration"):
        load_config(path)


def test_search_compiles_each_query_once_across_entry_and_core(config, monkeypatch):
    from src.retrieval import lexical_store

    calls = []
    original = lexical_store.compile_lexical_query

    def counted(query):
        calls.append(query)
        return original(query)

    monkeypatch.setattr(lexical_store, "compile_lexical_query", counted)
    with client_for(config) as client:
        response = client.post("/v1/search", json={"queries": ["quasar", "clarification"]}, headers=HEADERS)
        assert response.status_code == 200
    assert calls == ["quasar", "clarification"]


def test_sequential_requests_have_no_rate_budget(config):
    with client_for(config) as client:
        seed = exchange()[0].item_id
        generation = client.app.state.core.store.generation
        for _ in range(3):
            assert_error(client.get("/v1/status", headers={"X-Forwarded-For": "192.0.2.1"}),
                         401, "unauthorized")
            assert client.get("/healthz", headers=HEADERS).status_code == 200
            response = client.post("/v1/read-bundle", headers=HEADERS,
                                   json={"seed_item_id": seed, "generation": generation})
            assert response.status_code == 200
        assert client.app.state.admissions._total == 0


@pytest.mark.parametrize("field", ["ip_rate", "client_rate", "max_ip_buckets"])
def test_removed_rate_configuration_is_rejected(config, tmp_path, field):
    from dataclasses import asdict
    from src.service.config import load_config

    values = asdict(config)
    values["retrieval_root"] = str(config.retrieval_root)
    values["credentials_file"] = str(config.credentials_file)
    values[field] = 1 if field == "max_ip_buckets" else {"refill_per_second": 2, "capacity": 10}
    path = tmp_path / "synthetic-old-config.json"
    path.write_text(json.dumps(values))
    with pytest.raises(ValueError, match="cannot load service configuration"):
        load_config(path)


def test_failed_core_call_releases_concurrency(config, monkeypatch):
    with client_for(config) as client:
        original = client.app.state.core.status
        def fail():
            raise RuntimeError("synthetic failure")
        monkeypatch.setattr(client.app.state.core, "status", fail)
        assert_error(client.get("/v1/status", headers=HEADERS), 500, "internal_error")
        assert client.app.state.admissions._total == 0
        monkeypatch.setattr(client.app.state.core, "status", original)
        assert client.get("/v1/status", headers=HEADERS).status_code == 200
