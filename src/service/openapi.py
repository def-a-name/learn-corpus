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
              "default": MAX_RESPONSE_TOKENS,
              "description": "整个 JSON 响应的估算 token 上限，不是精确模型 token 数。预算不足时减少返回条目；连最小响应也放不下时返回 422。"}
    metadata = {
        "item_id": item_id, "source_type": scope, "title": nullable_string,
        "path": {"type": "string", "description": "标准化来源的相对路径。"},
        "locator": string, "role": nullable_string, "evidence_role": nullable_string,
        "turn_index": {"type": ["integer", "null"]},
    }
    common = {
        "request_id": {**string, "description": "本次请求的追踪标识，可用于关联服务日志。"},
        "generation": generation, "usage": _ref("Usage"),
    }
    schemas = {
        "SearchRequest": _object({
            "queries": _array({"type": "string", "minLength": 1, "maxLength": MAX_QUERY_SCALARS},
                              minItems=1, maxItems=MAX_QUERIES, uniqueItems=True,
                              description="1～6 条查询。每条用普通空格分隔 1～3 个关键词，要求在同一条目中同时命中，例如 SQLite 索引。数组中各条查询独立检索，再合并去重排序，不要求同时命中所有查询。每条最多 256 个 Unicode 字符、1024 UTF-8 字节；NFC 规范化、忽略大小写及空白差异后不能重复。"),
            "scopes": _array(scope, minItems=1, maxItems=len(SUPPORTED_SCOPES), uniqueItems=True,
                             description="省略表示所有来源；conversation=会话，note=笔记，article=文章。传入时不能为空或重复。"),
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_RESULT_LIMIT, "default": 8,
                      "description": "合并去重后的总条数上限，不是每条 query 的条数；预算可能进一步减少结果。"},
            "generation": {**generation, "description": "首次查询建议省略。后续可传回响应中的 generation 固定索引版本；格式正确但与服务当前版本不同返回 409。"},
            "max_estimated_tokens": budget,
        }, ["queries"]),
        "ReadBundleRequest": _object({
            "seed_item_id": {**item_id, "description": "复制搜索响应 results 中某项的 item_id；不要使用示例占位值。"},
            "generation": {**generation, "description": "复制产生该搜索结果的响应顶层 generation；必须与服务当前版本一致。"},
            "max_estimated_tokens": budget,
        }, ["seed_item_id", "generation"]),
        "Usage": _object({"estimated_evidence_tokens": {**integer, "description": "响应的估算 token 用量，按 estimator_version 计算。"}, "estimator_version": string}),
        "SearchResult": _object({
            **metadata, "bundle_key": bundle_key, "rank": {"type": "integer", "minimum": 1},
            "snippet": {**string, "description": "命中附近的文本片段；完整证据使用 read-bundle 读取。"},
            "truncated_before": {**boolean, "description": "片段之前还有正文。"},
            "truncated_after": {**boolean, "description": "片段之后还有正文。"},
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
            **common, "is_truncated": {**boolean, "description": "因响应预算减少了候选结果；不表示超过 limit 的命中总数或是否存在下一页。"},
            "results": _array(_ref("SearchResult"), maxItems=MAX_RESULT_LIMIT),
        }),
        "ReadBundleResponse": _object({
            **common, "bundle_key": bundle_key,
            "bundle_status": {"type": "string", "enum": ["complete", "partial_budget", "partial_error"],
                              "description": "complete=全部返回；partial_budget=预算限制导致部分条目未返回。当前实现不返回 partial_error，完整性故障直接返回错误。"},
            "membership_complete": {**boolean, "description": "是否已确定完整成员集合；为 true 不代表所有正文都已返回，应同时检查 bundle_status。"},
            "missing_item_ids": _array(item_id, uniqueItems=True, description="已知但因预算未返回正文的成员 ID。"),
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

    error_help = {
        400: "请求校验失败：检查必填字段、字段类型、未知字段、重复 JSON 键及查询语法；不接受 URL 查询参数。",
        401: "缺少或无效的 Bearer token；Authorize 中填写 Base64URL(key.secret)，不是 secret 本身。",
        403: "连接对端、Host 或 Origin 不在允许列表中。",
        404: "路由不存在，或指定的证据条目不存在。",
        405: "请求方法不支持；按 Allow 响应头改用正确方法。",
        408: "接收请求体超时。",
        409: "索引版本不匹配；重新搜索，并使用同一响应中的 generation 和 item_id。",
        413: "请求体超过大小限制；Nginx 也可能提前拒绝并返回 HTML。",
        415: "POST 应使用 Content-Type: application/json，不支持压缩的 Content-Encoding。",
        422: "响应预算不足或处理超过时间预算；可提高 token 预算、减少查询或缩小范围。",
        429: "并发或代理请求速率超过限制；稍后重试，代理可能返回非 JSON。",
        431: "请求头数量、总大小或 Authorization 长度超过限制。",
        500: "内部错误；保留 request_id 供排查。",
        503: "索引不可用；检查服务与索引状态。",
    }

    def operation(name, summary, response, request=None, authenticated=True, description=""):
        responses = {"200": {"description": "成功", "content": {
            "application/json": {"schema": _ref(response)},
        }}}
        for status, help_text in error_help.items():
            if not authenticated and status == 401:
                continue
            codes = [code for code, (value, _) in ERRORS.items() if value == status]
            # 请求头超限复用 request_too_large，但 HTTP 状态为 431。
            if status == 431:
                codes = ["request_too_large"]
            examples = {
                code: {"summary": code, "value": {"error": {
                    "code": code, "message": ERRORS[code][1],
                    "request_id": "req_" + "0" * 32,
                }}} for code in codes
            }
            responses[str(status)] = {"description": help_text, "content": {
                "application/json": {"schema": _ref("ErrorResponse"), "examples": examples},
            }}
            if status == 401:
                responses[str(status)]["headers"] = {"WWW-Authenticate": {
                    "description": "认证方式", "schema": {"type": "string", "example": "Bearer"},
                }}
            if status == 405:
                responses[str(status)]["headers"] = {"Allow": {
                    "description": "该路由支持的方法", "schema": {"type": "string", "example": "POST" if request else "GET"},
                }}
        result = {"operationId": name, "summary": summary, "description": description,
                  "responses": responses, "security": [{"BearerAuth": []}] if authenticated else []}
        if request:
            result["requestBody"] = {"required": True, "content": {
                "application/json": {"schema": _ref(request)},
            }}
        return result

    paths = {
        "/healthz": {"get": operation("health", "检查进程健康", "HealthResponse", authenticated=False,
                                    description="无需认证，仍检查来源与 Host/Origin；不是每次重新校验索引的深度检查。")},
        "/v1/status": {"get": operation("status", "查看当前索引版本与服务能力", "StatusResponse",
                                       description="返回当前固定的 generation、各类来源条目数和响应限制。服务重启才会加载新 generation；当前不支持语义检索。")},
        "/v1/search": {"post": operation("search", "检索来源片段", "SearchResponse", "SearchRequest",
            description="一条 query 的多个关键词用空格分隔，最多 3 个，按 AND 同时匹配同一条目。"
                        "例如 `queries: [\"SQLite 索引\"]`；`queries: [\"SQLite\", \"索引\"]` 则是两条独立查询，合并去重后排序。\n\n"
                        "中文连续词按连续字符匹配；非中文词忽略大小写并做前缀匹配。标点可能拆分检索成分，不用逗号代替空格。"
                        "不支持自然语言问答或原始 FTS 语法：不要写 OR、NOT、NEAR、双引号、星号、括号或字段过滤；无需手写 AND。\n\n"
                        "每条查询按 BM25 取前 20 个候选，按 item_id 去重，以等权 RRF（分数为各查询 1/(60+排名) 之和）排序，取总共 limit 条。"
                        "同分依次比较首条查询排名、任意查询最佳排名、item_id；不保证每条查询均有结果入选。"
                        "片段优先围绕首条查询生成，未入选首条查询候选时使用该条目排名最好的查询。\n\n"
                        "无命中返回 200 和空 results。首次请求省略 generation；读取证据时复制响应的 generation 和 results 中的 item_id。")},
        "/v1/read-bundle": {"post": operation("read_bundle", "读取搜索命中项的相关证据", "ReadBundleResponse", "ReadBundleRequest",
            description="先执行 search，再替换示例中的 seed_item_id 和 generation；示例 ID 仅演示格式，不指向真实条目。"
                        "返回种子条目所在证据组，不是任意数量的相邻条目。预算不足会省略完整条目，不截断单条正文；检查 bundle_status 和 missing_item_ids。")},
    }
    paths["/v1/search"]["post"]["requestBody"]["content"]["application/json"]["examples"] = {
        "single_keyword": {"summary": "单关键词（最小请求）", "value": {"queries": ["SQLite"]}},
        "multiple_keywords": {"summary": "同一条目同时匹配两个关键词", "value": {"queries": ["SQLite 索引"]}},
        "multiple_queries": {"summary": "多条查询合并结果", "value": {"queries": ["SQLite 索引", "缓存 策略"], "limit": 8}},
        "filtered": {"summary": "限定笔记和文章及响应预算", "value": {
            "queries": ["缓存 策略"], "scopes": ["note", "article"], "limit": 5,
            "max_estimated_tokens": MAX_RESPONSE_TOKENS,
        }},
    }
    paths["/v1/read-bundle"]["post"]["requestBody"]["content"]["application/json"]["examples"] = {
        "replace_with_search_result": {"summary": "替换为 search 返回的 item_id 和 generation 后执行", "value": {
            "seed_item_id": "itm_" + "a" * 32, "generation": "gen_" + "0" * 20,
            "max_estimated_tokens": MAX_RESPONSE_TOKENS,
        }},
    }
    return {
        "openapi": "3.1.0", "info": {
            "title": "Learn Corpus HTTP API", "version": "1.0.0",
            "description": "只读来源检索。先在 Authorize 输入 Base64URL(key.secret)，执行 search，再用结果调用 read-bundle。"
                           "请求使用严格 JSON，不接受 URL 查询参数或未定义字段。\n\n"
                           "Try it out → Execute 后的 Server response 是实际 HTTP 状态和正文；下方 Responses / Example Value 是文档示例，不能通过选择示例切换服务返回的状态码。"
                           "应用错误包含 code、固定英文 message 和 request_id；代理错误可能返回 HTML。",
        },
        "paths": paths,
        "components": {"schemas": schemas, "securitySchemes": {
            "BearerAuth": {"type": "http", "scheme": "bearer",
                           "description": "填写整个 key.secret 字符串的 Base64URL 编码，不带 Bearer 前缀；生成时可省略尾部 =。不要只填 secret。"},
        }},
    }
