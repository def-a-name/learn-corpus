from __future__ import annotations

import json

import pytest

from src.service.http.mcp import PROTOCOL_VERSION, tool_definitions
from src.retrieval.lexical_store import IndexUnavailableError
from test_api import HEADERS, TOKEN, config, client_for, assert_error  # noqa: F401
from test_public_core import open_core  # noqa: F401


MCP_HEADERS = {**HEADERS, 'Accept': 'application/json, text/event-stream',
               'MCP-Protocol-Version': PROTOCOL_VERSION}


def rpc(client, method, params=None, *, headers=None, rpc_id=1):
    body = {'jsonrpc': '2.0', 'id': rpc_id, 'method': method}
    if params is not None:
        body['params'] = params
    return client.post('/mcp', json=body, headers=MCP_HEADERS if headers is None else headers)


def call(client, name, arguments=None):
    return rpc(client, 'tools/call', {'name': name, 'arguments': arguments or {}})


def test_handshake_discovery_and_status(config):
    with client_for(config) as client:
        headers = {key: value for key, value in MCP_HEADERS.items() if key != 'MCP-Protocol-Version'}
        result = rpc(client, 'initialize', {'protocolVersion': PROTOCOL_VERSION,
                     'capabilities': {}, 'clientInfo': {'name': 'synthetic-client', 'version': '0.0.1'}}, headers=headers)
        assert result.json()['result'] == {'protocolVersion': PROTOCOL_VERSION, 'capabilities': {'tools': {}},
                                          'serverInfo': {'name': 'learn-corpus', 'version': '1.0.0'}}
        assert 'mcp-session-id' not in result.headers
        response = client.post('/mcp', json={'jsonrpc': '2.0', 'method': 'notifications/initialized'}, headers=MCP_HEADERS)
        assert response.status_code == 202 and response.content == b''
        assert rpc(client, 'ping').json()['result'] == {}
        tools = rpc(client, 'tools/list').json()['result']['tools']
        assert [tool['name'] for tool in tools] == ['search_sources', 'read_bundle', 'status']
        assert tools == tool_definitions()
        assert '$ref' not in json.dumps(tools)
        for tool in tools:
            assert tool['inputSchema']['additionalProperties'] is False
            assert tool['outputSchema']['additionalProperties'] is False
        result = call(client, 'status').json()['result']
        assert result['isError'] is False
        assert result['structuredContent'] == client.get('/v1/status', headers=HEADERS).json()
        assert_error(client.get('/mcp', headers=MCP_HEADERS), 405, 'method_not_allowed')
        assert_error(client.delete('/mcp', headers=MCP_HEADERS), 405, 'method_not_allowed')


@pytest.mark.parametrize('query', [{'queries': ['quasar']}, {'queries': ['quasar'], 'max_estimated_tokens': 350},
                                  {'queries': ['absentword']}, {'queries': ['quasar'], 'scopes': ['article']}])
def test_rest_mcp_search_read_deep_parity(config, query):
    with client_for(config) as client:
        response = call(client, 'search_sources', query).json()['result']
        assert response['isError'] is False
        value = response['structuredContent']
        rest = client.post('/v1/search', json=query, headers=HEADERS).json()
        rest['request_id'] = value['request_id']
        assert value == rest
        if not value['results']:
            return
        request = {'generation': value['generation'], 'seed_item_id': value['results'][0]['item_id']}
        for budget in (8000, 450):
            request['max_estimated_tokens'] = budget
            value = call(client, 'read_bundle', request).json()['result']
            rest = client.post('/v1/read-bundle', json=request, headers=HEADERS).json()
            if value['isError']:
                error = json.loads(value['content'][0]['text'])
                assert error['error']['code'] == rest['error']['code']
            else:
                rest['request_id'] = value['structuredContent']['request_id']
                assert value['structuredContent'] == rest


@pytest.mark.parametrize('method', ['resources/list', 'resources/templates/list', 'prompts/list', 'read_item', 'admin/rebuild'])
def test_no_other_capabilities(config, method):
    with client_for(config) as client:
        response = rpc(client, method)
        assert response.status_code == 400
        assert response.json()['error'] == {'code': -32601, 'message': 'Method not found'}


@pytest.mark.parametrize('body,code', [
    (b'{', -32700), (b'{"jsonrpc":"2.0","jsonrpc":"2.0"}', -32700),
    (b'[]', -32600), (b'[{"jsonrpc":"2.0","id":1,"method":"ping"}]', -32600),
    (b'{"jsonrpc":"2.0","id":true,"method":"ping"}', -32600),
    (b'{"jsonrpc":"2.0","id":null,"method":"ping"}', -32600),
    (b'{"jsonrpc":"2.0","id":1,"method":"ping","extra":1}', -32600),
    (b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"read_item"}}', -32602),
    (b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"cursor":"secret"}}', -32602),
    (b'[[[[[[[[[0]]]]]]]]]', -32600), (b'"\\ud800"', -32700), (b'\xff', -32700),
])
def test_protocol_parser_failures_are_safe(config, body, code):
    with client_for(config) as client:
        response = client.post('/mcp', content=body, headers={**MCP_HEADERS, 'Content-Type': 'application/json'})
        assert response.status_code == 400
        assert response.json()['error']['code'] == code
        assert 'secret' not in response.text and TOKEN not in response.text


@pytest.mark.parametrize('name,args,code', [
    ('status', {'extra': 1}, 'invalid_request'),
    ('search_sources', {'queries': ['quasar'], 'limit': True}, 'invalid_request'),
    ('search_sources', {'queries': ['quasar', 'QUASAR']}, 'invalid_request'),
    ('search_sources', {'queries': ['quasar'], 'generation': 'gen_'+'0'*20}, 'generation_mismatch'),
    ('search_sources', {'queries': ['quasar'], 'max_estimated_tokens': 1}, 'budget_exceeded'),
])
def test_tool_errors_preserve_stable_codes(config, name, args, code):
    with client_for(config) as client:
        response = call(client, name, args)
        assert response.status_code == 200
        result = response.json()['result']
        assert result['isError'] is True and 'structuredContent' not in result
        assert json.loads(result['content'][0]['text'])['error']['code'] == code


def test_shared_http_boundary_and_envelope_limit(config):
    with client_for(config) as client:
        assert_error(rpc(client, 'ping', headers={'X-Forwarded-For': '192.0.2.1'}), 401, 'unauthorized')
        for patch, status, code in [({'Host': 'evil.test'}, 403, 'forbidden'),
                                    ({'Origin': 'https://evil.test'}, 403, 'forbidden'),
                                    ({'MCP-Protocol-Version': '2026-07-28'}, 400, 'invalid_request'),
                                    ({'Accept': 'application/json'}, 400, 'invalid_request'),
                                    ({'Content-Encoding': 'gzip'}, 415, 'unsupported_media_type')]:
            assert_error(rpc(client, 'ping', headers={**MCP_HEADERS, **patch}), status, code)
        headers = {**MCP_HEADERS, 'Content-Type': 'application/json'}
        body = b'{"jsonrpc":"2.0","id":1,"method":"ping"}'
        assert client.post('/mcp', content=body+b' '*17000, headers=headers).status_code == 200
        assert_error(client.post('/mcp', content=body+b' '*32768, headers=headers), 413, 'request_too_large')
        assert_error(client.post('/v1/search', content=b' '*17000, headers=HEADERS | {'Content-Type': 'application/json'}), 413, 'request_too_large')
        duplicated = list(headers.items()) + [('MCP-Protocol-Version', PROTOCOL_VERSION)]
        assert_error(client.post('/mcp', content=body, headers=duplicated), 400, 'invalid_request')


def test_errors_logs_and_shared_admission(config, monkeypatch, caplog):
    marker = 'synthetic-private-path-query-body'
    with client_for(config) as client:
        client.app.state.admissions.acquire('client:synthetic-client')
        try:
            assert_error(call(client, 'status'), 429, 'rate_limited')
            assert_error(client.get('/v1/status', headers=HEADERS), 429, 'rate_limited')
        finally:
            client.app.state.admissions.release('client:synthetic-client')
        def fail():
            raise IndexUnavailableError(marker)
        monkeypatch.setattr(client.app.state.core, 'status', fail)
        with caplog.at_level('INFO', logger='learn_corpus.service'):
            response = call(client, 'status')
        assert marker not in response.text and marker not in caplog.text and TOKEN not in caplog.text
        assert json.loads(response.json()['result']['content'][0]['text'])['error']['code'] == 'index_unavailable'
        event = json.loads(caplog.records[-1].message)
        assert event['transport'] == 'mcp' and event['operation'] == 'status'
        assert event['error_category'] == 'index_unavailable'
        assert client.app.state.admissions._total == 0


def test_tools_list_contract_snapshot():
    import hashlib
    encoded = json.dumps(tool_definitions(), sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
    assert hashlib.sha256(encoded).hexdigest() == '6ec1f96a9c9268bfe00b41048c2b1202cc6318db913499584190a23c466546e9'


@pytest.mark.parametrize('scope', ['note', 'article'])
def test_section_unicode_body_parity(config, open_core, scope):
    from dataclasses import replace
    from test_public_core import parts
    core = open_core(parts(scope, 'Synthetic section', ('# Synthetic section\nquasar 虚构正文 "example"', 'quasar synthetic continuation')))
    config = replace(config, corpus_path=core.store._generation_path.parent.parent)
    with client_for(config) as client:
        search = call(client, 'search_sources', {'queries': ['quasar'], 'scopes': [scope]}).json()['result']['structuredContent']
        args = {'generation': search['generation'], 'seed_item_id': search['results'][0]['item_id']}
        bundle = call(client, 'read_bundle', args).json()['result']['structuredContent']
        rest = client.post('/v1/read-bundle', json=args, headers=HEADERS).json()
        rest['request_id'] = bundle['request_id']
        assert bundle == rest and len(bundle['items']) == 2


@pytest.mark.parametrize('params,status', [({'requestId': 1}, 202), ({'requestId': False}, 400),
                                          ({'requestId': 1, 'reason': []}, 400)])
def test_cancel_notification_schema(config, params, status):
    with client_for(config) as client:
        response = client.post('/mcp', json={'jsonrpc': '2.0', 'method': 'notifications/cancelled', 'params': params}, headers=MCP_HEADERS)
        assert response.status_code == status
        if status == 202:
            assert response.content == b''
