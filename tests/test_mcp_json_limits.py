"""使用虚构元数据验证 MCP 预算与 REST 边界隔离。"""

import json

import pytest

from src.service.json_boundary import JSONLimitFailure, strict_json
from src.service.mcp_protocol import parse_envelope, RPCFailure
from test_mcp import rpc
from test_api import config, client_for  # noqa: F401
from test_public_core import open_core  # noqa: F401


def test_nested_metadata_search_and_read(config):
    meta = {"synthetic-context": {f"field_{i}": "fictional" for i in range(40)}}
    with client_for(config) as client:
        task = rpc(client, "tools/call", {
            "name": "start_retrieval_task", "arguments": {},
        }).json()["result"]["structuredContent"]["task_id"]
        params = {"_meta": meta, "name": "search_sources",
                  "arguments": {"task_id": task, "queries": ["quasar"], "limit": 1,
                                "max_estimated_tokens": 2000}}
        response = rpc(client, "tools/call", params)
        assert response.status_code == 200
        search = response.json()["result"]["structuredContent"]
        args = {"task_id": task, "seed_item_id": search["results"][0]["item_id"],
                "max_estimated_tokens": 4000}
        result = rpc(client, "tools/call", {"_meta": meta, "name": "read_bundle", "arguments": args})
        assert result.json()["result"]["isError"] is False
        params["arguments"]["synthetic_unknown"] = True
        invalid = rpc(client, "tools/call", params).json()["result"]
        assert invalid["isError"] is True


@pytest.mark.parametrize("count,accepted", [(192, True), (193, False)])
def test_metadata_key_boundary(config, count, accepted):
    with client_for(config) as client:
        response = rpc(client, "tools/call", {"name": "status", "arguments": {},
                       "_meta": {f"field_{i}": 0 for i in range(count)}}, rpc_id=17)
        value = response.json()
        assert value["id"] == 17
        assert response.status_code == (200 if accepted else 400)
        if not accepted:
            assert value["error"] == {"code": -32602, "message": "Metadata resource limit exceeded"}


@pytest.mark.parametrize("size,accepted", [(8192, True), (8193, False)])
def test_metadata_byte_boundary(config, size, accepted):
    meta = {"x": "a" * (size - 8)}
    assert len(json.dumps(meta, separators=(",", ":")).encode()) == size
    with client_for(config) as client:
        response = rpc(client, "tools/call", {"name": "status", "_meta": meta}, rpc_id=19)
        assert response.status_code == (200 if accepted else 400)
        assert response.json()["id"] == 19


@pytest.mark.parametrize("count,accepted", [(256, True), (257, False)])
def test_mcp_total_keys_boundary(count, accepted):
    value = {"jsonrpc": "2.0", "id": 1, "method": "ping",
             "params": {"_meta": {f"field_{i}": 0 for i in range(count - 5)}}}
    raw = json.dumps(value).encode()
    if accepted:
        assert parse_envelope(raw) == value
    else:
        with pytest.raises(RPCFailure) as caught:
            parse_envelope(raw)
        assert caught.value.code == -32600
        assert caught.value.message == "Request resource limit exceeded"


def test_rest_key_limit_unchanged():
    assert len(strict_json(json.dumps({f"k{i}": 0 for i in range(32)}).encode())) == 32
    with pytest.raises(JSONLimitFailure):
        strict_json(json.dumps({f"k{i}": 0 for i in range(33)}).encode())


def test_mcp_array_limit_is_resource_error():
    raw = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping",
                      "params": {"_meta": {"synthetic": list(range(21))}}}).encode()
    with pytest.raises(RPCFailure) as caught:
        parse_envelope(raw)
    assert caught.value.code == -32600
