# Learn Corpus

让 AI 从积累的会话、笔记和文章中找寻相关片段，带着证据回答问题。

Learn Corpus 将分散的个人资料整理成可检索的语料库。用户可以用自然语言让 AI 导入指定资料，也可以追问过去的讨论、比较不同来源，或查找某个决定的背景。检索与来源读取由本项目提供，回答由所使用的 AI 完成。

## 主要用途

- **找回讨论背景。** 从历史会话、笔记和保存的文章中找回相关上下文，重新理解过去的方案、决定和变化。
- **围绕问题追查证据。** AI 搜索候选、读取正文，并针对证据缺口继续查找，用于跨会话、跨资料的核对与比较。
- **核对来源和依据。** 区分用户陈述、助手建议和外部资料，根据实际读到的正文回答，保留出处和语境。
- **查看检索过程。** 按需查看检索范围、调用记录、已读来源与停止原因，了解回答依据和检索的局限。

目前支持 Claude Code/Codex 会话记录，Markdown 形式的 ChatGPT/DeepSeek 网页会话、笔记和外部文章。来源处理与检索由项目 skill 组织，资料范围和检索目标由用户指定。

## 工作流程

```mermaid
---
config:
  look: classic
  layout: dagre
  themeVariables:
    fontSize: 14px
  flowchart:
    minNodeWidth: 0
    wrappingWidth: 160
    rankSpacing: 24
    diagramPadding: 12
    padding: 6
---
flowchart LR
    A["会话、笔记、文章"] --> B["导入与<br/>标准化"]
    B --> C["可追溯<br/>来源库"]
    C --> D["离线构建<br/>索引"]
    D --> E["搜索与<br/>读取"]
    E --> F["AI 根据证据<br/>回答"]
```

资料经过整理后构建检索索引。AI 通过 MCP 搜索并读取相关正文，再根据证据回答；服务也提供 REST 接口。本机可使用 stdio 或 HTTP MCP，跨机可使用 HTTP MCP。本机 stdio 由客户端启动服务子进程；网络部署按实际环境配置 HTTPS 入口，Nginx 与 systemd 模板是可选示例，见[配置与部署](config/README.md#使用方式与部署选择)。

资料导入与索引构建可以在其他环境完成。分机部署时，检索服务从索引读取正文与上下文，部署机无需克隆整个仓库或保存原始资料、`sources/` 来源文件；所需代码与运行文件见[部署代码说明](config/README.md#检索部署所需的代码目录)。

## 开始使用

使用时需要支持 MCP 的 AI 客户端，并加载[来源导入 skill](.agents/skills/learn-corpus-ingestion/SKILL.md)和[检索 skill](.agents/skills/learn-corpus-retrieval/SKILL.md)。

首次使用推荐按[公开示例](examples/README.md#在工作区应用示例)，将 [SQLite FTS5 示例文章](examples/sqlite-fts5.md)作为第一个导入对象，完成导入、索引发布、MCP 配置与检索问答。

也可先运行[验证脚本](examples/README.md#全流程验证脚本)，检查导入到 MCP 正文读取的流程；临时索引退出后回收，自己的工作区仍需完成导入和连接配置。连接检索服务后，向 AI 描述问题，例如：

```text
使用 learn-corpus-retrieval 回答：<问题>。
```

AI 做出回答之后，可以向它索要两种详细程度的检索过程：

- **展示检索摘要：** 概括检索范围、关键证据、异常或缺口、停止原因，并给出核心来源定位。
- **展示完整检索过程：** 在检索摘要基础上，进一步展开调用记录和已读检索单元的明细；受响应限制省略的部分会明确说明。

## 使用须知

- 当前采用词法检索，效果受资料覆盖和实际用词影响；回答仍可能遗漏或误判。
- 标准化来源经过筛选和清洗，不是原始记录的无损归档，也不保证完全脱敏；资料和检索索引应妥善保护。
- 当前提供来源导入与标准化、检索索引构建、只读搜索与正文读取，以及 REST、HTTP MCP 和 stdio MCP 接口；不提供 Memory 提取、Wiki 生成或整理、向量或语义检索、服务端模型回答、人工审核 Web 界面、来源或知识写回接口及公网 OAuth。

## 文档与帮助

| 内容 | 阅读位置 |
|---|---|
| 环境准备、资料更新、功能边界和常见问题 | [使用指南](docs/usage.md) |
| 数据流、模块边界及关键设计 | [架构说明](docs/architecture.md) |
| 本机连接、HTTP 配置、部署和运行排查 | [配置与部署](config/README.md) |
| 源码模块与手动命令入口 | [源码说明](src/README.md) |
| 开发与提交约定 | [AGENTS.md](AGENTS.md) |

<a id="导入失败后的处理边界"></a>

反馈问题时说明预期行为、实际结果和错误阶段，去除凭据、私人路径与来源内容。

<a id="参考项目"></a>

## 致谢

项目开发过程中得到了众多开源帮助，特此感谢：

项目：

- [QMD](https://github.com/tobi/qmd)
- [Letta Code](https://github.com/letta-ai/letta-code)

参考资料：

- [SQLite FTS5 Extension](https://www.sqlite.org/fts5.html)
- [Karpathy's LLM Wiki idea](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)

社区：[LINUX DO 社区](https://linux.do)

本项目采用 [MIT License](LICENSE)。演示文章的来源与授权说明单独记录，不由项目许可证重新授权。

公开提交历史由实际开发历史过滤整理，省略个人资料、开发记录和评测部分，并按保留的代码变更调整提交说明。
