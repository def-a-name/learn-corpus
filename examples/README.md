# 公开示例

[sqlite-fts5.md](sqlite-fts5.md) 是 [SQLite FTS5 官方文档](https://www.sqlite.org/fts5.html)的 Markdown 演示副本。保留所选文档的正文、标题、来源 URL 及页面原有更新时间；去掉剪藏保存日期、标签和空元数据，将网页相对链接转为官方绝对地址，并去除行尾空白。它不是原始剪藏的无损副本，也不代表当前网站的最新版本。

SQLite 官方声明其交付代码与文档属于 public domain，见[官方版权说明](https://www.sqlite.org/copyright.html)。示例文章的来源与授权独立于本项目 MIT License。

## 运行完整流程

从代码目录执行，使用已经安装项目依赖的 Python：

```bash
venv/bin/python examples/run_example.py
```

脚本创建独立的临时数据目录，显式导入这一篇文章、构建并发布临时索引，然后启动本机 stdio MCP，完成握手、任务创建、`unicode61` 搜索、bundle 正文读取、任务详情与状态检查。退出后回收这次临时目录；不会借用现有语料、实际配置或个人服务，也不写入代码目录的 `sources/`、`meta/`。

输出包括来源数、检索单元数、词法查询、命中与读取数量、来源标题和索引版本。它验证导入到证据读取的运行流程，不生成模型答案或开展检索质量评测。

## 保留自己的示例数据

需要继续检索时，按[工作目录说明](../docs/workspaces.md)选择已存在的数据目录，从代码根目录执行：

```bash
export LEARN_CORPUS_DATA_ROOT=/absolute/path/to/data-workspace
venv/bin/python -m src.ingestion.import_articles --input examples --include sqlite-fts5.md
venv/bin/python -m src.retrieval.build_lexical_index --publish
```

随后按[配置说明](../config/README.md)设置 `corpus_path` 和账本位置，由 MCP 客户端连接服务。命中摘要用于发现候选，引用依据来自进一步读取的正文。
