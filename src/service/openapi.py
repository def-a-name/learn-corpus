"""描述现有公开契约，仅用于文档，不参与请求校验或响应序列化。"""

from src.retrieval.contracts import SUPPORTED_SCOPES
from src.retrieval.lexical_query import MAX_QUERY_SCALARS
from src.retrieval.lexical_store import MAX_QUERIES, MAX_RESULT_LIMIT
from src.retrieval.public_core import MAX_RESPONSE_TOKENS
from src.service.security import ERRORS


def _object(properties, required=None):
    return {"type": "object", "properties": properties, "additionalProperties": False,
            "required": list(properties) if required is None else required}


def _ref(name):
    return {"$ref": f"#/components/schemas/{name}"}


def _array(items, **constraints):
    return {"type": "array", "items": items, **constraints}


def build_openapi() -> dict:
    """生成不包含运行期凭据、绝对路径或真实语料的静态 OpenAPI。"""

    string = {"type": "string"}
    boolean = {"type": "boolean"}
    nullable_string = {"type": ["string", "null"]}
    integer = {"type": "integer", "minimum": 0}
    generation = {"type": "string", "pattern": "^gen_[0-9a-f]{20}$"}
    item_id = {"type": "string", "pattern": "^itm_[a-z2-7]{32}$"}
    bundle_key = {"type": "string", "pattern": "^bnd_[0-9a-f]{40}$"}
    scope = {"type": "string", "enum": list(SUPPORTED_SCOPES)}
    budget = {"type": "integer", "minimum": 1, "maximum": MAX_RESPONSE_TOKENS,
              "default": MAX_RESPONSE_TOKENS}
    metadata = {
        "item_id": item_id, "source_type": scope, "title": nullable_string,
        "path": {"type": "string", "description": "Relative standardized source path."},
        "locator": string, "role": nullable_string, "evidence_role": nullable_string,
        "turn_index": {"type": ["integer", "null"]},
    }
    common = {
        "request_id": string, "generation": generation, "usage": _ref("Usage"),
    }
    schemas = {
        "SearchRequest": _object({
            "queries": _array({"type": "string", "minLength": 1, "maxLength": MAX_QUERY_SCALARS},
                              minItems=1, maxItems=MAX_QUERIES, uniqueItems=True,
                              description="Unique after normalization; each query uses 1 to 3 lexical anchors, at most 1024 UTF-8 bytes."),
            "scopes": _array(scope, minItems=1, maxItems=len(SUPPORTED_SCOPES), uniqueItems=True,
                             description="Omit to search all scopes."),
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_RESULT_LIMIT, "default": 8},
            "generation": {**generation, "description": "Omit on the first search; reuse the returned generation for subsequent calls."},
            "max_estimated_tokens": budget,
        }, ["queries"]),
        "ReadBundleRequest": _object({
            "seed_item_id": {**item_id, "description": "An item_id returned by search."},
            "generation": {**generation, "description": "The generation returned by search or status."},
            "max_estimated_tokens": budget,
        }, ["seed_item_id", "generation"]),
        "Usage": _object({"estimated_evidence_tokens": integer, "estimator_version": string}),
        "SearchResult": _object({
            **metadata, "bundle_key": bundle_key, "rank": {"type": "integer", "minimum": 1},
            "snippet": string, "truncated_before": boolean, "truncated_after": boolean,
        }),
        "BundleItem": _object({
            **metadata, "body": string, "is_truncated": {"type": "boolean", "const": False},
            "relations": _object({
                "counterpart_item_ids": _array(item_id),
                "previous_part_id": {"anyOf": [item_id, {"type": "null"}]},
                "next_part_id": {"anyOf": [item_id, {"type": "null"}]},
            }),
        }),
        "SearchResponse": _object({
            **common, "is_truncated": boolean,
            "results": _array(_ref("SearchResult"), maxItems=MAX_RESULT_LIMIT),
        }),
        "ReadBundleResponse": _object({
            **common, "bundle_key": bundle_key,
            "bundle_status": {"type": "string", "enum": ["complete", "partial_budget", "partial_error"],
                              "description": "Current implementation returns complete or partial_budget; integrity failures return an error."},
            "membership_complete": boolean, "missing_item_ids": _array(item_id, uniqueItems=True),
            "items": _array(_ref("BundleItem")),
        }),
        "StatusResponse": _object({
            "generation": generation, "source_digest": string, "built_at": string,
            "item_counts": _object({name: integer for name in SUPPORTED_SCOPES}),
            **{name: string for name in (
                "schema_version", "projection_schema_version", "chunk_policy_version",
                "query_policy_version", "ranking_policy_version", "estimator_version",
            )},
            "semantic_search": {"type": "boolean", "const": False},
            "supported_scopes": _array(scope),
            "capabilities": _object({
                "read_bundle": {"type": "boolean", "const": True},
                "max_estimated_tokens": {"type": "integer", "const": MAX_RESPONSE_TOKENS},
                "max_response_bytes": {"type": "integer", "minimum": 1},
                "request_timeout_ms": {"type": "integer", "minimum": 1},
            }),
        }),
        "HealthResponse": _object({"ok": {"type": "boolean", "const": True}}),
        "ErrorResponse": _object({"error": _object({
            "code": {"type": "string", "enum": list(ERRORS)},
            "message": string, "request_id": string,
        })}),
    }

    def operation(name, summary, response, request=None, authenticated=True):
        responses = {"200": {"description": "Success", "content": {
            "application/json": {"schema": _ref(response)},
        }}}
        for status in (400, 401, 403, 404, 405, 408, 409, 413, 415, 422, 429, 431, 500, 503):
            if not authenticated and status == 401:
                continue
            codes = [code for code, (value, _) in ERRORS.items() if value == status]
            description = ", ".join(codes) if codes else "Request headers exceed limits"
            responses[str(status)] = {"description": description, "content": {
                "application/json": {"schema": _ref("ErrorResponse")},
            }}
        result = {"operationId": name, "summary": summary, "responses": responses,
                  "security": [{"BearerAuth": []}] if authenticated else []}
        if request:
            result["requestBody"] = {"required": True, "content": {
                "application/json": {"schema": _ref(request)},
            }}
        return result

    paths = {
        "/healthz": {"get": operation("health", "Check process health", "HealthResponse", authenticated=False)},
        "/v1/status": {"get": operation("status", "Inspect the pinned corpus generation", "StatusResponse")},
        "/v1/search": {"post": operation("search", "Search source snippets", "SearchResponse", "SearchRequest")},
        "/v1/read-bundle": {"post": operation("read_bundle", "Read evidence around a search result", "ReadBundleResponse", "ReadBundleRequest")},
    }
    paths["/v1/search"]["post"]["requestBody"]["content"]["application/json"]["example"] = {
        "queries": ["synthetic example"], "limit": 8, "max_estimated_tokens": MAX_RESPONSE_TOKENS,
    }
    return {
        "openapi": "3.1.0", "info": {
            "title": "Learn Corpus HTTP API", "version": "1.0.0",
            "description": "Read-only source retrieval. Enter the Base64URL encoding of key.secret in Authorize. "
                           "Requests use strict JSON and do not accept URL query parameters. "
                           "Proxy errors may return a non-JSON response.",
        },
        "paths": paths,
        "components": {"schemas": schemas, "securitySchemes": {
            "BearerAuth": {"type": "http", "scheme": "bearer",
                           "description": "Enter Base64URL(key.secret) without the Bearer prefix; omit trailing padding when generating tokens."},
        }},
    }
