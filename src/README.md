# 源码说明

项目 Python 源代码位于此目录，命令统一从仓库根目录使用 `python3 -m` 运行。本页供开发者查找模块、接口约束和手动入口。首次使用见[使用指南](../docs/usage.md)，数据流与设计理由见[架构说明](../docs/architecture.md)，运行配置见[配置说明](../config/README.md)。来源操作按[导入 skill](../.agents/skills/learn-corpus-ingestion/SKILL.md)执行。

部署机运行检索并使用脚本更新索引时，可按[部署代码目录](../config/README.md#检索部署所需的代码目录)复制 `service/`、`retrieval/`、`maintenance/` 与 `corpus/`，保留 `src/` 包结构，无需克隆整个仓库。

从代码根目录执行命令，来源与维护通过统一 `config.json` 的 `workspace.data_root` 选择资料位置，未配置时使用同仓库布局。`src/corpus/paths.py` 的 `CODE_ROOT` 表示代码位置，`DATA_ROOT` 表示资料位置，内部名称 `REPO_ROOT` 为数据根目录别名。上手配置见[配置说明](../config/README.md#工作区配置)，双仓库维护见[仓库关系](../docs/workspaces.md)。

## 模块职责

| 目录 | 职责 | CLI 入口 |
|---|---|---|
| `ingestion/` | 发现、标准化和登记来源 | 是 |
| `retrieval/` | 检索单元（item）投影、词法索引和读取运行时 | 部分 |
| `service/` | HTTP/stdio 服务、共享 MCP 契约与 transport 分派 | 是 |
| `maintenance/` | 人工触发的来源状态、review、健康检查和安全扫描 | 是 |
| `corpus/` | 标准化来源的格式、状态和 ingest log 基础能力 | 否 |

curation 是 manifest 的业务状态，不是独立运行层；相关状态、review 和健康检查命令统一位于 `maintenance/`。

## 关键模块

| 要查看的实现 | 模块 |
|---|---|
| 来源路径、类型、文档与登记契约 | [paths.py](corpus/paths.py)、[scopes.py](corpus/scopes.py)、[document.py](corpus/document.py)、[manifest.py](corpus/manifest.py) |
| 原子存储、附件检查与来源变更 | [storage.py](corpus/storage.py)、[ingest_log.py](corpus/ingest_log.py)、[removal.py](corpus/removal.py) |
| 共同批次与文本清洗 | [batch.py](ingestion/batch.py)、[common.py](ingestion/common.py) |
| 来源策略与格式适配 | [import_claude.py](ingestion/import_claude.py)、[import_codex.py](ingestion/import_codex.py)、[import_web_chat.py](ingestion/import_web_chat.py)、[import_markdown.py](ingestion/import_markdown.py) |
| 检索单元投影与索引构建、验证 | [project_items.py](retrieval/project_items.py)、[build_lexical_index.py](retrieval/build_lexical_index.py)、[index_artifact.py](retrieval/index_artifact.py) |
| 查询、索引读取、上下文与公开响应 | [lexical_query.py](retrieval/lexical_query.py)、[lexical_store.py](retrieval/lexical_store.py)、[bundle.py](retrieval/bundle.py)、[public_core.py](retrieval/public_core.py) |
| 共用数据契约与文本计量 | [contracts.py](retrieval/contracts.py)、[text.py](retrieval/text.py) |
| 服务入口、配置与共享 MCP 协议 | [server.py](service/server.py)、[config.py](service/config.py)、[mcp_protocol.py](service/mcp_protocol.py) |
| 任务执行、预算与持久化账本 | [retrieval_tasks.py](service/retrieval_tasks.py)、[execution_ledger_store.py](service/execution_ledger_store.py) |
| HTTP、stdio 与传输边界 | [http/](service/http/)、[stdio/](service/stdio/)、[json_boundary.py](service/json_boundary.py)、[errors.py](service/errors.py)、[validation.py](service/validation.py) |

## 接口维护规范

`RetrievalCore.search()`、`read_bundle()` 和 `status()` 是 REST/MCP 共用的业务入口。调用方通过 `RequestLimits(corpus_timeout_ms=...)` 限制一次核心操作。方法接收请求 dict，返回 `PublicResponse`：`json_bytes` 是已经校验并计量的紧凑 UTF-8 JSON，`payload` 是其独立解析副本。search/read 的已用额度（`usage`）按完整证据 JSON 估算（包括 `usage` 自身）；adapter 不得追加字段后继续沿用旧的计量结果。协议封装由 transport 层另行限制，具体数值见[配置说明](../config/README.md#mcp-任务与执行账本)。

`LexicalStore.search_lex()`、`read_item()` 是内部接口。公开 metadata 白名单与整响应预算由 `RetrievalCore` 执行，不得将内部 dataclass 直接注册为公开工具结果。[openapi.py](service/http/openapi.py) 提供静态请求、响应和 Bearer 契约，运行期校验仍由入口与 core 执行；修改公开契约时需同步核对两者。

bundle 的成员集合与正文先完整校验，再在预算内选择读取窗口。成功和预算部分响应的 `membership_complete` 均为 true；损坏关系、SQLite 错误和处理超时不能包装成可引用的部分证据。上下文组织和失败边界见[架构说明](../docs/architecture.md#搜索与上下文读取)。

## 命令入口

| 命令 | 用途 |
|---|---|
| `src.ingestion.import_claude` | 导入 Claude 主会话 |
| `src.ingestion.import_codex` | 导入 Codex 主会话和 fork 增量 |
| `src.ingestion.import_web_chat` | 导入 Chrome 插件导出的 ChatGPT/DeepSeek Markdown 会话 |
| `src.ingestion.import_notes` | 导入个人笔记或显式 Claude document |
| `src.ingestion.import_articles` | 导入外部 Markdown 文章 |
| `src.ingestion.import_batch` | 对显式文件范围执行跨来源的统一批次导入 |
| `src.ingestion.build_source_inventory` | 重建来源覆盖快照；可按 provider 和重复 `--include` 合并指定原始文件 |
| `src.ingestion.read_raw_locator` | 按 locator 读取原始片段 |
| `src.retrieval.project_items` | 校验 ready source 并生成确定性内存检索单元投影摘要 |
| `src.retrieval.build_lexical_index` | 构建或复用内容寻址 SQLite 检索索引，可显式发布 current/previous |
| `src.service.server` | 使用显式配置启动 HTTP 服务或 stdio MCP 子进程 |
| `src.maintenance.find_unprocessed` | 列出未处理的来源 |
| `src.maintenance.remove_source` | 一次性删除标准化 Markdown source 及登记附件 |
| `src.maintenance.update_source_status` | 更新来源的 ingest 或 curation 状态 |
| `src.maintenance.rebuild_review_queue` | 重建来源问题队列；`--source-path` 限制本次允许变化的原始文件 |
| `src.maintenance.check_corpus` | 检查来源、manifest 和派生状态一致性 |
| `src.maintenance.scan_secrets` | 扫描 Git 候选文本中的凭据模式 |

查看参数：

```bash
python3 -m <完整模块名> --help
```
