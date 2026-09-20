from __future__ import annotations

import json

import pytest

from src.service.http.mcp import PROTOCOL_VERSION, tool_definitions
from src.service.ledger_config import TaskLimitsConfig
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


def start_task(client):
    result = call(client, 'start_retrieval_task').json()['result']
    assert result['isError'] is False
    return result['structuredContent']['task_id']


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
        assert [tool['name'] for tool in tools] == [
            'start_retrieval_task', 'search_sources', 'read_bundle',
            'get_retrieval_task', 'status',
        ]
        assert tools == tool_definitions()
        assert '$ref' not in json.dumps(tools)
        for tool in tools:
            assert tool['inputSchema']['additionalProperties'] is False
            assert tool['outputSchema']['additionalProperties'] is False
        result = call(client, 'status').json()['result']
        assert result['isError'] is False
        status = result['structuredContent']
        ledger = status.pop('execution_ledger')
        assert status == client.get('/v1/status', headers=HEADERS).json()
        assert ledger['status'] == 'healthy' and ledger['task_count'] == 0
        assert_error(client.get('/mcp', headers=MCP_HEADERS), 405, 'method_not_allowed')
        assert_error(client.delete('/mcp', headers=MCP_HEADERS), 405, 'method_not_allowed')


@pytest.mark.parametrize('query', [{'queries': ['quasar']}, {'queries': ['quasar'], 'max_estimated_tokens': 350},
                                  {'queries': ['absentword']}, {'queries': ['quasar'], 'scopes': ['article']}])
def test_rest_mcp_search_read_deep_parity(config, query):
    with client_for(config) as client:
        task_id = start_task(client)
        mcp_query = {**query, 'task_id': task_id,
                     'max_estimated_tokens': query.get('max_estimated_tokens', 8000)}
        response = call(client, 'search_sources', mcp_query).json()['result']
        assert response['isError'] is False
        value = response['structuredContent']
        rest = client.post('/v1/search', json={key: child for key, child in mcp_query.items()
                                              if key != 'task_id'}, headers=HEADERS).json()
        rest['request_id'] = value['request_id']
        execution = value.pop('execution')
        assert value == rest
        assert execution['task_id'] == task_id and execution['search_calls'] == 1
        if not value['results']:
            return
        request = {'task_id': task_id, 'seed_item_id': value['results'][0]['item_id'],
                   'max_estimated_tokens': 450}
        result = call(client, 'read_bundle', request).json()['result']
        rest_request = {'generation': value['generation'], 'seed_item_id': request['seed_item_id'],
                        'max_estimated_tokens': request['max_estimated_tokens']}
        rest = client.post('/v1/read-bundle', json=rest_request, headers=HEADERS).json()
        if result['isError']:
            error = json.loads(result['content'][0]['text'])
            assert error['error']['code'] == rest['error']['code']
        else:
            structured = result['structuredContent']
            rest['request_id'] = structured['request_id']
            execution = structured.pop('execution')
            assert structured == rest
            assert execution['read_calls'] == 1


def test_task_switching_bounded_detail_and_owner_isolation(config):
    from test_api import encode_token

    second_key = 'other-client_key_1'
    second_secret = 'b' * 43
    config.credentials_file.write_text(json.dumps([
        {'cid': 'synthetic-client', 'key': 'synthetic-client_key_1', 'secret': 'a' * 43},
        {'cid': 'other-client', 'key': second_key, 'secret': second_secret},
    ]))
    config.credentials_file.chmod(0o600)
    with client_for(config) as client:
        task_a = start_task(client)
        task_b = start_task(client)
        for task_id, query in ((task_a, 'quasar'), (task_b, 'absentword')):
            result = call(client, 'search_sources', {
                'task_id': task_id, 'queries': [query], 'max_estimated_tokens': 1500,
            }).json()['result']
            assert result['isError'] is False
        search = call(client, 'get_retrieval_task', {'task_id': task_a}).json()['result']
        detail = search['structuredContent']
        assert detail['execution']['search_calls'] == 1
        assert detail['calls_total'] == 1 and detail['calls_truncated'] is False
        assert detail['items_total'] == 0 and detail['items_truncated'] is False
        assert detail['calls'][0]['queries'] == ['quasar']
        assert detail['server_returned_items'] == []

        duplicate = call(client, 'search_sources', {
            'task_id': task_a, 'queries': [' QUASAR '], 'max_estimated_tokens': 500,
        }).json()['result']
        assert json.loads(duplicate['content'][0]['text'])['error']['code'] == 'search_already_attempted'

        other_headers = {**MCP_HEADERS, 'Authorization': 'Bearer ' + encode_token(second_key, second_secret)}
        foreign = rpc(client, 'tools/call', {
            'name': 'get_retrieval_task', 'arguments': {'task_id': task_a},
        }, headers=other_headers).json()['result']
        assert json.loads(foreign['content'][0]['text'])['error']['code'] == 'task_not_found'


def test_configured_task_limits_are_returned_by_new_task(config):
    limits = TaskLimitsConfig(
        search_calls=7, read_calls=11, estimated_evidence_tokens=24000,
    )
    with client_for(config, task_limits=limits) as client:
        result = call(client, 'start_retrieval_task').json()['result']
        assert result['isError'] is False
        task = result['structuredContent']
        assert task['limits'] == {
            'search_calls': 7,
            'read_calls': 11,
            'estimated_evidence_tokens': 24000,
        }
        assert task['execution']['available_estimated_tokens'] == 24000


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


def test_tool_errors_preserve_stable_codes(config):
    with client_for(config) as client:
        task_id = start_task(client)
        cases = [
            ('status', {'extra': 1}, 'invalid_request'),
            ('search_sources', {'task_id': task_id, 'queries': ['quasar'],
                                'limit': True, 'max_estimated_tokens': 500}, 'invalid_request'),
            ('search_sources', {'task_id': task_id, 'queries': ['quasar', 'QUASAR'],
                                'max_estimated_tokens': 500}, 'invalid_request'),
            ('search_sources', {'task_id': task_id, 'queries': ['quasar'],
                                'generation': 'gen_'+'0'*20,
                                'max_estimated_tokens': 500}, 'invalid_request'),
            ('search_sources', {'task_id': task_id, 'queries': ['quasar'],
                                'max_estimated_tokens': 1}, 'budget_exceeded'),
        ]
        for name, args, code in cases:
            response = call(client, name, args)
            assert response.status_code == 200
            result = response.json()['result']
            assert result['isError'] is True and 'structuredContent' not in result
            assert json.loads(result['content'][0]['text'])['error']['code'] == code


def test_invalid_request_does_not_create_call(config):
    with client_for(config) as client:
        task_id = start_task(client)
        result = call(client, 'search_sources', {
            'task_id': task_id, 'queries': ['quasar', 'QUASAR'],
            'max_estimated_tokens': 500,
        }).json()['result']
        assert json.loads(result['content'][0]['text'])['error']['code'] == 'invalid_request'
        detail = call(client, 'get_retrieval_task', {
            'task_id': task_id,
        }).json()['result']['structuredContent']
        assert detail['calls'] == []
        assert detail['execution']['search_calls'] == 0
        assert detail['execution']['reserved_estimated_tokens'] == 0


def test_finalize_failure_does_not_return_core_success(config, monkeypatch):
    with client_for(config) as client:
        task_id = start_task(client)
        original = client.app.state.tasks.ledger.finalize_success

        def fail(*_args, **_kwargs):
            from src.service.execution_ledger_store import LedgerFailure

            raise LedgerFailure('ledger_unavailable')

        monkeypatch.setattr(client.app.state.tasks.ledger, 'finalize_success', fail)
        result = call(client, 'search_sources', {
            'task_id': task_id, 'queries': ['quasar'], 'max_estimated_tokens': 1000,
        }).json()['result']
        assert result['isError'] is True and 'structuredContent' not in result
        assert json.loads(result['content'][0]['text'])['error']['code'] == 'ledger_unavailable'

        monkeypatch.setattr(client.app.state.tasks.ledger, 'finalize_success', original)
        detail = call(client, 'get_retrieval_task', {
            'task_id': task_id,
        }).json()['result']['structuredContent']
        assert detail['calls'][0]['state'] == 'pending'
        assert detail['execution']['reserved_estimated_tokens'] == 1000


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
    assert hashlib.sha256(encoded).hexdigest() == '0076fad28ffba781cb245e4a60442dab6216d1aa1cdafdc5e39c9f8d603a699a'


@pytest.mark.parametrize('scope', ['note', 'article'])
def test_section_unicode_body_parity(config, open_core, scope):
    from dataclasses import replace
    from test_public_core import parts
    core = open_core(parts(scope, 'Synthetic section', ('# Synthetic section\nquasar 虚构正文 "example"', 'quasar synthetic continuation')))
    config = replace(config, corpus_path=core.store._generation_path.parent.parent)
    with client_for(config) as client:
        task_id = start_task(client)
        search = call(client, 'search_sources', {
            'task_id': task_id, 'queries': ['quasar'], 'scopes': [scope],
            'max_estimated_tokens': 2000,
        }).json()['result']['structuredContent']
        args = {'task_id': task_id, 'seed_item_id': search['results'][0]['item_id'],
                'max_estimated_tokens': 4000}
        bundle = call(client, 'read_bundle', args).json()['result']['structuredContent']
        rest = client.post('/v1/read-bundle', json={
            'generation': search['generation'], 'seed_item_id': args['seed_item_id'],
            'max_estimated_tokens': args['max_estimated_tokens'],
        }, headers=HEADERS).json()
        rest['request_id'] = bundle['request_id']
        execution = bundle.pop('execution')
        assert bundle == rest and len(bundle['items']) == 2
        assert execution['task_id'] == task_id


@pytest.mark.parametrize('params,status', [({'requestId': 1}, 202), ({'requestId': False}, 400),
                                          ({'requestId': 1, 'reason': []}, 400)])
def test_cancel_notification_schema(config, params, status):
    with client_for(config) as client:
        response = client.post('/mcp', json={'jsonrpc': '2.0', 'method': 'notifications/cancelled', 'params': params}, headers=MCP_HEADERS)
        assert response.status_code == status
        if status == 202:
            assert response.content == b''
