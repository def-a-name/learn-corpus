from __future__ import annotations

import asyncio
import base64
import errno
import json
import logging
import sqlite3
import threading
from dataclasses import replace

import anyio
import pytest
from starlette.testclient import TestClient

from src.retrieval.lexical_store import IndexUnavailableError
from src.service.http.api import create_app
from src.service.http.http_config import HttpConfig
from src.service.json_boundary import strict_json
from src.service.errors import HTTPFailure
from src.service.http.security import Admissions, BearerVerifier, Credential
from src.service.ledger_config import LedgerConfig
from test_public_core import exchange, open_core  # noqa: F401


def encode_token(key, secret):
    return base64.urlsafe_b64encode(f"{key}.{secret}".encode("ascii")).decode("ascii").rstrip("=")


SECRET = "a" * 43
KEY = "synthetic-client_key_1"
TOKEN = encode_token(KEY, SECRET)
HEADERS = {"Authorization": "Bearer " + TOKEN}


@pytest.fixture
def config(open_core, tmp_path):
    core = open_core(exchange())
    credentials = tmp_path / "synthetic-credentials.json"
    credentials.write_text(json.dumps([{
        "cid": "synthetic-client", "key": KEY, "secret": SECRET,
    }]))
    credentials.chmod(0o600)
    return HttpConfig(
        corpus_path=core.store._generation_path.parent.parent, credentials_file=credentials,
        allowed_peers=("192.0.2.2", "127.0.0.1"),
        allowed_hosts=("service.test",), allowed_origins=("https://client.test",),
        corpus_timeout_ms=5000,
        global_concurrency=2, client_concurrency=1,
        body_timeout_ms=100,
    )


def ledger_for(config):
    return LedgerConfig(config.credentials_file.parent / "synthetic-ledger.sqlite3")


def client_for(config, *, peer="192.0.2.2", task_limits=None):
    return TestClient(
        create_app(config, ledger_for(config), task_limits),
        base_url="http://service.test", client=(peer, 4000),
    )


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


def test_health_auth_exemption_still_checks_origin_and_peer(config):
    with client_for(config) as client:
        assert client.get("/healthz").content == b'{"ok":true}'
        for path in ("/v1/status", "/v1/search", "/v1/read-bundle"):
            response = client.request("GET" if path.endswith("status") else "POST", path,
                                      headers={})
            assert_error(response, 401, "unauthorized")
            assert response.headers["www-authenticate"] == "Bearer"
        assert_error(client.get("/healthz", headers={**HEADERS, "Origin": "https://evil.test"}), 403, "forbidden")
        assert_error(client.get("/healthz", headers={**HEADERS, "Host": "evil.test"}), 403, "forbidden")
    with client_for(config, peer="192.0.2.99") as client:
        assert_error(client.get("/healthz", headers=HEADERS), 403, "forbidden")


@pytest.mark.parametrize("header", [None, "Basic synthetic", "Bearer " + encode_token("unknown_key_1", SECRET),
                                    "Bearer " + encode_token(KEY, "b" * 43)])
def test_bad_credentials_share_safe_response(config, header):
    with client_for(config) as client:
        headers = {}
        if header is not None:
            headers["Authorization"] = header
        assert_error(client.get("/v1/status", headers=headers), 401, "unauthorized")


@pytest.mark.parametrize("path", ["/redoc", "/docs/oauth2-redirect", "/docs/", "/v1/read", "/v1/search/"])
def test_only_declared_exact_routes_are_exposed(config, path):
    with client_for(config) as client:
        assert_error(client.get(path, headers=HEADERS), 404, "not_found")
        response = client.get("/v1/search", headers=HEADERS)
        assert_error(response, 405, "method_not_allowed")
        assert response.headers["allow"] == "POST"


def test_swagger_schema_and_authenticated_browser_flow(config):
    from fastapi.openapi.models import OpenAPI

    config = replace(config, allowed_origins=("http://service.test",))
    browser_headers = {"Origin": "http://service.test"}
    with client_for(config) as client:
        page = client.get("/docs", headers=browser_headers)
        assert page.status_code == 200
        assert "text/html" in page.headers["content-type"]
        assert "SwaggerUIBundle" in page.text and "/openapi.json" in page.text
        assert '"validatorUrl": null' in page.text
        assert "oauth2RedirectUrl" not in page.text
        response = client.get("/openapi.json", headers=browser_headers)
        assert response.status_code == 200
        schema = response.json()
        OpenAPI.model_validate(schema)
        assert set(schema["paths"]) == {"/healthz", "/v1/status", "/v1/search", "/v1/read-bundle"}
        assert schema["components"]["securitySchemes"]["BearerAuth"]["scheme"] == "bearer"
        assert schema["paths"]["/healthz"]["get"]["security"] == []
        for path, method in (("/v1/status", "get"), ("/v1/search", "post"), ("/v1/read-bundle", "post")):
            operation = schema["paths"][path][method]
            assert operation["security"] == [{"BearerAuth": []}]
            assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
            assert operation["responses"]["401"]["content"]["application/json"]["schema"]["$ref"].endswith("/ErrorResponse")
        schemas = schema["components"]["schemas"]
        assert schemas["SearchRequest"]["required"] == ["queries"]
        assert schemas["SearchRequest"]["additionalProperties"] is False
        assert schemas["SearchRequest"]["properties"]["queries"]["maxItems"] == 6
        assert schemas["ReadBundleRequest"]["required"] == ["seed_item_id", "generation"]
        assert TOKEN not in response.text and str(config.corpus_path) not in response.text
        assert str(config.credentials_file) not in response.text

        # 按 Swagger 描述的请求字段和认证方式完成一次合成检索与证据读取。
        query = {"queries": ["quasar"]}
        assert_error(client.post("/v1/search", json=query, headers=browser_headers), 401, "unauthorized")
        authorized = {**browser_headers, "Authorization": "Bearer " + TOKEN}
        search = client.post("/v1/search", json=query, headers=authorized)
        assert search.status_code == 200
        assert set(search.json()) == set(schemas["SearchResponse"]["required"])
        seed = search.json()["results"][0]
        assert set(seed) == set(schemas["SearchResult"]["required"])
        bundle = client.post("/v1/read-bundle", headers=authorized, json={
            "seed_item_id": seed["item_id"], "generation": search.json()["generation"],
        })
        assert bundle.status_code == 200
        assert set(bundle.json()) == set(schemas["ReadBundleResponse"]["required"])
        assert bundle.json()["seed_item_id"] == seed["item_id"]
        assert set(bundle.json()["items"][0]) == set(schemas["BundleItem"]["required"])
        status = client.get("/v1/status", headers=authorized)
        assert status.status_code == 200
        assert set(status.json()) == set(schemas["StatusResponse"]["required"])


@pytest.mark.parametrize("path", ["/docs", "/openapi.json"])
def test_documentation_preserves_boundary_and_concurrency(config, path):
    headers = {}
    with client_for(config) as client:
        assert client.get(path, headers=headers).status_code == 200
        assert_error(client.get(path, headers={**headers, "Host": "evil.test"}), 403, "forbidden")
        assert_error(client.get(path, headers={**headers, "Origin": "https://evil.test"}), 403, "forbidden")
        assert_error(client.get(path + "?token=synthetic", headers=headers), 400, "invalid_request")
        assert_error(client.post(path, headers=headers), 405, "method_not_allowed")
        assert_error(client.request("GET", path, headers=headers, content=b"{}"), 400, "invalid_request")
        admissions = client.app.state.admissions
        for key in ("synthetic-one", "synthetic-two"):
            admissions.acquire(key)
        try:
            assert_error(client.get(path, headers=headers), 429, "rate_limited")
        finally:
            for key in ("synthetic-one", "synthetic-two"):
                admissions.release(key)
        assert client.get(path, headers=headers).status_code == 200
        assert admissions._total == 0
    with client_for(config, peer="192.0.2.99") as client:
        assert_error(client.get(path, headers=headers), 403, "forbidden")


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
        assert_error(client.get("/v1/status", headers={**HEADERS, "X-Synthetic": "x" * 8192}),
                     431, "request_too_large")


def test_forwarded_for_is_ignored_and_connection_peer_controls_access(config):
    with client_for(config) as client:
        for value in (None, "192.0.2.1, 192.0.2.3", "not-an-ip"):
            headers = dict(HEADERS)
            if value is not None:
                headers["X-Forwarded-For"] = value
            assert client.get("/healthz", headers=headers).status_code == 200
    with client_for(config, peer="192.0.2.99") as client:
        assert_error(client.get("/healthz", headers={"X-Forwarded-For": "192.0.2.2"}),
                     403, "forbidden")



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
        assert SECRET not in caplog.text
        assert "sources/" not in caplog.text


def test_concurrency_rejects_without_queue_and_releases_after_failure(config, monkeypatch):
    credentials = json.loads(config.credentials_file.read_text())
    second_key = "synthetic-client_key_2"
    credentials.append({"cid": "synthetic-client", "key": second_key, "secret": "b" * 43})
    config.credentials_file.write_text(json.dumps(credentials))
    second_headers = {**HEADERS, "Authorization": "Bearer " + encode_token(second_key, "b" * 43)}
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
            response = client.get("/v1/status", headers=second_headers)
            assert_error(response, 429, "rate_limited")
            assert "retry-after" not in response.headers
        finally:
            release.set()
            thread.join(2)
        assert responses[0].status_code == 200
        assert client.get("/v1/status", headers=second_headers).status_code == 200


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
        with client_for(replace(config, corpus_path=config.corpus_path / "missing")):
            pass


@pytest.mark.parametrize(("target", "cause", "component", "category", "detail"), [
    (
        "src.service.http.api.load_credentials",
        PermissionError(errno.EACCES, "synthetic private detail", "/synthetic/private/credentials"),
        "credentials", "permission_denied", ("os_error", "EACCES"),
    ),
    (
        "src.service.http.api.LexicalStore.open_current",
        FileNotFoundError(errno.ENOENT, "synthetic private detail", "/synthetic/private/index"),
        "index", "not_found", ("os_error", "ENOENT"),
    ),
    (
        "src.service.http.api.ExecutionLedgerStore.open",
        sqlite3.OperationalError("synthetic private ledger detail"),
        "ledger", "read_only_filesystem", ("sqlite_error", "SQLITE_READONLY"),
    ),
])
def test_startup_failure_logs_safe_component_and_stable_error(
    config, monkeypatch, caplog, target, cause, component, category, detail,
):
    if isinstance(cause, sqlite3.Error):
        cause.sqlite_errorname = detail[1]

    def fail(*_args, **_kwargs):
        raise ValueError("synthetic private wrapper") from cause

    monkeypatch.setattr(target, fail)
    with caplog.at_level(logging.ERROR, logger="learn_corpus.service"):
        with pytest.raises(ValueError, match="synthetic private wrapper"):
            with client_for(config):
                pass

    events = [
        json.loads(record.message) for record in caplog.records
        if record.name == "learn_corpus.service"
        and '"operation":"lifecycle"' in record.message
    ]
    assert events == [{
        "operation": "lifecycle", "phase": "startup", "component": component,
        "status": "failed", "error_category": category, detail[0]: detail[1],
    }]
    assert "synthetic private" not in caplog.text
    assert "/synthetic/private" not in caplog.text
    assert "Traceback" not in caplog.text


def test_rotation_keys_share_client_and_revocation_uses_new_verifier():
    old = Credential("synthetic-client", KEY, SECRET)
    new_secret = "b" * 43
    new = Credential("synthetic-client", "synthetic-client_key_2", new_secret)
    both = BearerVerifier((old, new))
    assert both.authenticate("Bearer " + TOKEN).cid == both.authenticate("Bearer " + encode_token(new.key, new_secret)).cid
    with pytest.raises(HTTPFailure):
        BearerVerifier((new,)).authenticate("Bearer " + TOKEN)
    assert both.client_ids == frozenset({"synthetic-client"})
    with pytest.raises(ValueError, match="unique keys"):
        BearerVerifier((old, old))
    assert SECRET not in repr(old)


@pytest.mark.parametrize("secret", ["a" * 32, "b" * 43, "c" * 64])
def test_base64url_token_accepts_secret_lengths_and_padding(secret):
    credential = Credential("synthetic", "synthetic_key_01", secret)
    verifier = BearerVerifier((credential,))
    padded = base64.urlsafe_b64encode(f"{credential.key}.{secret}".encode()).decode()
    for token in (padded, padded.rstrip("=")):
        assert verifier.authenticate("Bearer " + token) == credential


def test_token_maximum_size_fits_header_limit_and_rejects_noncanonical_encoding():
    credential = Credential("c" * 64, "c" * 64 + "_key_1234567890", "s" * 64)
    token = encode_token(credential.key, credential.secret)
    assert len("Bearer " + token) <= 256
    assert BearerVerifier((credential,)).authenticate("Bearer " + token) == credential

    credential = Credential("synthetic", "synthetic_key_1", SECRET)
    token = encode_token(credential.key, credential.secret)
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    assert len(token) % 4 != 0
    altered = token[:-1] + alphabet[alphabet.index(token[-1]) | 1]
    with pytest.raises(HTTPFailure):
        BearerVerifier((credential,)).authenticate("Bearer " + altered)


@pytest.mark.parametrize("token", [
    "", "A", "%%%", "abc+def/", "a" * 193,
    KEY + "." + SECRET,
    encode_token(KEY, "short"), encode_token(KEY, "a" * 65),
    encode_token("wrong_key", SECRET), encode_token(KEY, SECRET + ".extra"),
    base64.urlsafe_b64encode(b"\xff\xfe").decode(),
    TOKEN + "===", TOKEN + "\n",
])
def test_malformed_encoded_tokens_are_rejected(token):
    verifier = BearerVerifier((Credential("synthetic-client", KEY, SECRET),))
    with pytest.raises(HTTPFailure) as caught:
        verifier.authenticate("Bearer " + token)
    assert caught.value.status == 401
    assert caught.value.headers == {"WWW-Authenticate": "Bearer"}


@pytest.mark.parametrize("changes", [
    {"cid": "another-client"}, {"key": "synthetic-client_key_x"},
    {"key": "synthetic-client_key_"}, {"key": "synthetic-client_key_12345678901"},
    {"secret": "short"}, {"secret": "x" * 65}, {"secret": "a" * 31 + "."},
    {"cid": None}, {"key": None}, {"secret": None},
])
def test_credential_file_rejects_invalid_fields(tmp_path, changes):
    from src.service.http.http_config import load_credentials

    path = tmp_path / "synthetic-credentials.json"
    path.write_text(json.dumps([{"cid": "synthetic-client", "key": KEY, "secret": SECRET, **changes}]))
    path.chmod(0o600)
    with pytest.raises(ValueError, match="cannot load credential configuration"):
        load_credentials(path)


def test_old_credential_schema_is_rejected(tmp_path):
    from src.service.http.http_config import load_credentials

    path = tmp_path / "synthetic-old-credentials.json"
    path.write_text(json.dumps([{"client_id": "synthetic-client", "key_id": "synthetic-key",
                                 "secret_sha256": "0" * 64}]))
    path.chmod(0o600)
    with pytest.raises(ValueError, match="cannot load credential configuration"):
        load_credentials(path)


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
    for raw in (
        json.dumps({str(index): index for index in range(33)}).encode(),
        json.dumps(list(range(21))).encode(),
    ):
        with pytest.raises(HTTPFailure):
            strict_json(raw)


def test_slow_body_and_stream_without_content_length(config):
    from src.service.http.api import receive_body
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
            (b"host", b"service.test"),
            (b"authorization", ("Bearer " + TOKEN).encode()),
            (b"content-type", b"application/json"), *extra_headers,
        ],
    }


def test_raw_asgi_body_timeout_and_duplicate_headers_return_error_json(config):
    async def exercise():
        selected = replace(config, body_timeout_ms=5)
        app = create_app(selected, ledger_for(selected))
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


@pytest.mark.parametrize("transport", ["rest", "mcp"])
def test_request_cancellation_keeps_admission_until_worker_finishes(config, monkeypatch, transport):
    async def exercise():
        app = create_app(config, ledger_for(config))
        async with app.router.lifespan_context(app):
            entered, release = threading.Event(), threading.Event()
            original = app.state.core.search
            def block(*args, **kwargs):
                entered.set()
                assert release.wait(2)
                return original(*args, **kwargs)
            monkeypatch.setattr(app.state.core, "search", block)
            scope = raw_scope()
            body = b'{"queries":["quasar"]}'
            if transport == "mcp":
                task_id = app.state.tasks.start_task(
                    {}, owner_key="synthetic-client", request_id="req_synthetic_start",
                ).payload["task_id"]
                scope = raw_scope("/mcp", extra_headers=(
                    (b"accept", b"application/json, text/event-stream"),
                    (b"mcp-protocol-version", b"2025-06-18"),
                ))
                body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": "search_sources", "arguments": {
                                       "task_id": task_id, "queries": ["quasar"],
                                       "max_estimated_tokens": 1000,
                                   }}}).encode()
            async def receive():
                return {"type": "http.request", "body": body}
            async def send(_):
                pass
            task = asyncio.create_task(app(scope, receive, send))
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
    from src.service.config import load_http_config
    from src.service import server
    from src.service.http import http_server

    values = {
        "corpus_path": "synthetic-retrieval",
        "corpus_timeout_ms": config.corpus_timeout_ms,
        "http": {
            "host": "127.0.0.1",
            "port": 8765,
            "credentials_file": config.credentials_file.name,
            "allowed_peers": list(config.allowed_peers),
            "allowed_hosts": list(config.allowed_hosts),
            "allowed_origins": list(config.allowed_origins),
            "global_concurrency": config.global_concurrency,
            "client_concurrency": config.client_concurrency,
            "body_timeout_ms": config.body_timeout_ms,
        },
        "mcp": {"transport": "http", "ledger": {
            "path": "synthetic-ledger/execution.sqlite3",
        }},
    }
    path = tmp_path / "synthetic-service.json"
    path.write_text(json.dumps(values))
    assert load_http_config(path).credentials_file == config.credentials_file
    assert load_http_config(path).corpus_path == tmp_path / "synthetic-retrieval"
    calls = []
    monkeypatch.setattr(http_server.uvicorn, "run", lambda app, **kwargs: calls.append(kwargs))
    monkeypatch.setattr("sys.argv", ["server", "--config", str(path)])
    assert server.main() == 0
    assert calls[0]["host"] == "127.0.0.1"
    assert calls[0]["port"] == 8765
    assert calls[0]["workers"] == 1 and calls[0]["proxy_headers"] is False
    assert calls[0]["access_log"] is False and calls[0]["server_header"] is False
    log_config = calls[0]["log_config"]
    assert log_config["loggers"]["uvicorn.error"]["handlers"] == ["lifecycle"]
    assert log_config["handlers"]["lifecycle"]["filters"] == ["lifecycle"]
    assert log_config["filters"]["lifecycle"]["()"] is http_server.UvicornLifecycleFilter
    assert log_config["handlers"]["discard"]["class"] == "logging.NullHandler"
    values["unknown"] = True
    path.write_text(json.dumps(values))
    with pytest.raises(ValueError, match="cannot load service configuration"):
        load_http_config(path)
    del values["unknown"]
    values["retrieval_root"] = values.pop("corpus_path")
    path.write_text(json.dumps(values))
    with pytest.raises(ValueError, match="cannot load service configuration"):
        load_http_config(path)


@pytest.mark.parametrize("message, args, level, exc_info, expected", [
    ("Application startup complete.", (), logging.INFO, None, True),
    ("Uvicorn running on %s://%s:%d (Press CTRL+C to quit)",
     ("http", "127.0.0.1", 8765), logging.INFO, None, True),
    ("Finished server process [%d]", (123,), logging.INFO, None, True),
    ("Synthetic private exception", (), logging.ERROR, None, False),
    ("Synthetic request content", (), logging.INFO, None, False),
    ("Application startup complete.", (), logging.INFO,
     (RuntimeError, RuntimeError("synthetic private detail"), None), False),
])
def test_uvicorn_lifecycle_filter(message, args, level, exc_info, expected):
    import io
    from src.service.http.http_server import UvicornLifecycleFilter

    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.addFilter(UvicornLifecycleFilter())
    record = logging.LogRecord("uvicorn.error", level, "synthetic.py", 1,
                               message, args, exc_info)
    handler.handle(record)
    assert output.getvalue() == ((message % args) + "\n" if expected else "")


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
            assert_error(client.get("/v1/status"),
                         401, "unauthorized")
            assert client.get("/healthz", headers=HEADERS).status_code == 200
            response = client.post("/v1/read-bundle", headers=HEADERS,
                                   json={"seed_item_id": seed, "generation": generation})
            assert response.status_code == 200
        assert client.app.state.admissions._total == 0


@pytest.mark.parametrize("field", ["ip_rate", "client_rate", "max_ip_buckets"])
def test_removed_rate_configuration_is_rejected(config, tmp_path, field):
    from dataclasses import asdict
    from src.service.config import load_http_config

    values = asdict(config)
    values["corpus_path"] = str(config.corpus_path)
    values["credentials_file"] = str(config.credentials_file)
    values[field] = 1 if field == "max_ip_buckets" else {"refill_per_second": 2, "capacity": 10}
    path = tmp_path / "synthetic-old-config.json"
    path.write_text(json.dumps(values))
    with pytest.raises(ValueError, match="cannot load service configuration"):
        load_http_config(path)


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


def test_openapi_error_examples_match_runtime_responses():
    from src.service.http.api import error_response
    from src.service.http.openapi import build_openapi
    from src.service.errors import ERRORS, HTTPFailure

    schema = build_openapi()
    for methods in schema['paths'].values():
        for operation in methods.values():
            for status, response in operation['responses'].items():
                if status == '200':
                    continue
                examples = response['content']['application/json']['examples']
                expected_codes = {code for code, (value, _) in ERRORS.items() if value == int(status)}
                if status == '431':
                    expected_codes = {'request_too_large'}
                assert set(examples) == expected_codes
                for code, example in examples.items():
                    body = example['value']
                    actual = error_response(HTTPFailure(code, status=int(status)), body['error']['request_id'])
                    assert actual.status_code == int(status)
                    assert json.loads(actual.body) == body


def test_openapi_search_examples_follow_query_contract(config):
    from src.service.http.openapi import build_openapi
    from src.retrieval.lexical_query import compile_lexical_query

    operation = build_openapi()['paths']['/v1/search']['post']
    examples = operation['requestBody']['content']['application/json']['examples']
    combined = examples['multiple_keywords']['value']['queries']
    independent = examples['multiple_queries']['value']['queries']
    assert len(combined) == 1
    assert compile_lexical_query(combined[0]).anchor_count == 2
    assert ' AND ' in compile_lexical_query(combined[0]).match_expression
    assert len(independent) == 2
    with client_for(config) as client:
        for example in examples.values():
            response = client.post('/v1/search', json=example['value'], headers=HEADERS)
            assert response.status_code == 200
