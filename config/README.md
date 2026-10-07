# 工作区、服务与部署配置

首次使用见[使用指南](../docs/usage.md)，服务与数据的职责见[架构说明](../docs/architecture.md)。本页保留运行参数、客户端连接及发布维护的详细参考。

| 使用目标 | 阅读位置 |
|---|---|
| 选择资料保存位置、初始化工作区 | [工作区配置](#工作区配置) |
| 选择连接与部署方式 | [使用方式与部署选择](#使用方式与部署选择) |
| 部署机只运行检索 | [检索部署所需的代码目录](#检索部署所需的代码目录) |
| 文件所有者与访问权限 | [运行身份与文件权限](#运行身份与文件权限) |
| 本机 MCP | [服务配置与启动](#服务配置与启动)、[本机 stdio 连接](#本机-stdio-连接) |
| HTTP MCP / REST | [HTTP 服务配置](#http-服务配置)、[访问凭据](#访问凭据)、[HTTP MCP 连接](#codex-http-mcp-连接) |
| 运行故障 | [运行问题排查](#运行问题排查) |
| 部署与索引维护 | [索引构建与发布](#索引构建与发布)、[Nginx](#nginx-代理配置)、[systemd](#systemd-服务配置)、[维护检查清单](#运维检查清单) |

## 工作区配置

`config.json` 同时配置资料保存位置和检索服务。通常只需选择一种目录布局；原始笔记、会话导出和文章仍在导入时指定，输入文件保持只读。

| 布局 | 适合什么情况 | 配置位置 |
|---|---|---|
| 私有仓库 + `engine/` submodule（推荐） | 希望分别维护通用代码与个人资料，并固定代码版本 | 私有仓库的 `config/config.json` |
| 独立数据目录 | 只想把资料放在指定位置，无需 Git | 代码仓库的 `config/config.json` |
| 同仓库默认目录 | 想先在克隆的仓库里使用，减少目录准备 | 导入可不建配置；服务使用 `config/config.json` |

### 推荐：私有仓库引用公开代码

新建私有工作区并添加公开代码：

```bash
mkdir learn-corpus-private
cd learn-corpus-private
git init
git submodule add https://github.com/def-a-name/learn-corpus.git engine
mkdir config
cp engine/config/config.json.example config/config.json
```

模板中的 `workspace` 已适用于这个布局，无需改路径：

```json
"workspace": {
  "data_root": ".."
}
```

`..` 相对于实际配置文件所在目录，因此指向私有根目录。程序识别 `engine/` 是 Git submodule 后，默认读取所属仓库的 `config/config.json`；从 `engine/` 执行命令即可，无需 export 环境变量。

```text
learn-corpus-private/
├── engine/                 # 公开代码、skill、文档和功能测试
├── config/config.json      # 个人统一配置
├── sources/                # 导入后的标准化来源
├── meta/                   # 登记与检索索引
└── var/                    # 服务执行账本
```

在私有根目录的 `.gitignore` 中加入以下本地状态规则；`sources/` 与登记文件可以在私有仓库提交：

```gitignore
/config/*.json
/config/*.header
/meta/corpus/
/var/
__pycache__/
*.py[cod]
```

再从 `engine/` 安装依赖并按[使用指南](../docs/usage.md#资料导入)开始导入。`sources/`、manifest 和索引由程序生成，不手工建立空登记文件。这个结构只需要本地 Git；需要远端备份时，为外层数据仓库创建私有远端。代码与数据的提交、升级步骤见[仓库关系](../docs/workspaces.md)。

### 使用独立数据目录

数据目录可以不是 Git 仓库，也可以是代码目录内的自定义子目录；使用后者时，将该子目录加入代码仓库的 `.gitignore`。先创建它，再在代码目录复制模板：

```bash
mkdir -p /absolute/path/to/my-corpus
cp config/config.json.example config/config.json
```

将模板中的 `workspace.data_root` 改为该目录的绝对路径：

```json
"workspace": {
  "data_root": "/absolute/path/to/my-corpus"
}
```

照常从代码目录执行导入、维护与本地索引构建，结果写入数据目录。无需配置私有远端或 submodule；Git 提交对照和 Actions 发布才需要数据仓库历史。

### 使用同仓库默认目录

不配置 workspace 时，导入与维护以代码仓库根目录为数据目录，生成仓库内的 `sources/`、`meta/`。准备服务时复制 `config.json.example`，保留 `workspace.data_root = ".."` 即为同一布局。

公开仓库的 `.gitignore` 默认忽略个人 `sources/`、`meta/` 和运行状态。若要将资料纳入同一 Git 历史，使用私有远端并按实际需要调整忽略规则。

### 路径与配置选择

`workspace.data_root` 的相对路径以实际配置文件所在目录解析（配置文件为符号链接时使用目标文件目录），与运行命令时的 cwd 无关。来源操作要求目录已存在；目录名不影响选择，程序不猜测原始输入位置。选定独立数据目录后，输出必须留在该目录内；submodule 里的代码目录也不接受来源输出。符号链接按实际目标检查。

索引默认位于 `<data_root>/meta/corpus`，账本默认位于 `<data_root>/var/execution-ledger.sqlite3`。同机使用通常只改 `workspace.data_root`。部署机或特殊布局可另填 `corpus_path`、`mcp.ledger.path`；这些显式路径仍相对于配置文件解析，并优先于上述默认值。服务只读取索引，不要求部署机具有原始资料或标准化来源。

自动选择配置的顺序为：明确设置的 `LEARN_CORPUS_CONFIG` → Git submodule 所属仓库的 `config/config.json` → 代码目录的 `config/config.json`。只使用选中的一份文件，不合并配置。没有配置时，来源操作使用同仓库默认目录；服务启动会提示先准备配置。配置存在但损坏、指定文件缺失或数据目录无效时，命令报错，不自动换到另一个目录。

日常配置不需要 `.env`。保留两个可选环境变量，供临时操作、测试或自动化使用：

- `LEARN_CORPUS_CONFIG`：选择非默认位置的一份统一配置。例如从代码目录运行 `LEARN_CORPUS_CONFIG=/path/to/config.json venv/bin/python -m src.maintenance.find_unprocessed`。相对文件名以进程 cwd 解析。
- `LEARN_CORPUS_DATA_ROOT`：临时覆盖来源处理的数据目录，优先于 workspace 配置；现有 CI 和公开示例用它选择临时工作区。它不覆盖服务的索引或账本配置。

服务的 `--config FILE` 优先于自动配置选择；省略时使用同一套发现规则。已有配置字段 `corpus_path` 和账本 `path` 继续按原路径语义处理；HTTP/MCP 协议、索引与账本格式不因 workspace 配置改变。

## 使用方式与部署选择

连接方式由客户端位置和运行需求决定：

| 场景 | 连接与运行方式 | 需要准备 |
|---|---|---|
| 本机 stdio MCP | 客户端通过管道连接自己启动的服务子进程 | 本机索引、配置与可写账本目录 |
| 本机 HTTP MCP / REST | 单独启动 HTTP 服务，客户端连接回环地址 | HTTP 配置、Bearer 凭据与客户端连接配置 |
| 跨机 HTTP MCP / REST | 客户端通过 HTTPS 入口访问 HTTP 服务 | 实际域名、证书、代理或其他安全传输设施，以及访问边界与凭据 |

本机 stdio 无需网络监听或反向代理。HTTP 入口由本项目提供，TLS 终止由部署环境负责；服务启动入口不提供证书参数。Nginx 是一种可选的 HTTPS 代理，systemd 是一种可选的 Linux 进程管理方式，均不要求特定路由器或中转链路。

现有 HTTP 与 Nginx 模板展示同机代理：`客户端 → Nginx HTTPS → 127.0.0.1:2699 → Learn Corpus`。systemd 和索引部署模板使用 `/opt/learn-corpus` 作为安装路径示例。域名、证书、服务账户、路径和容量数值都需按环境调整；真实配置与凭据不要提交到仓库。

分机部署时，部署机需要服务代码与依赖、运行配置、HTTP 凭据、已发布的检索索引及独立可写的 MCP 账本目录。索引根目录由 `corpus_path` 指定，包含 `current` 链接和目标索引目录中的 `index.json`、`corpus.sqlite`；无需复制原始资料、`sources/`、来源附件或 `meta/manifest.json`。资料导入、维护和重新构建在持有来源文件的环境完成，然后发布新索引并重启服务。

检索正文已包含在索引中，因此索引仍需按资料内容保护；索引与来源文件的部署边界见[架构说明](../docs/architecture.md#检索索引构建)。使用可选发布脚本时还需准备下文规定的 GitHub 请求头文件和部署配置。

## 检索部署所需的代码目录

部署机运行检索并通过现有脚本更新索引时，无需克隆整个仓库。从同一版本的代码中复制以下四个完整目录，保留 `src/` 包结构及 `src/__init__.py`、`src/config.py`：

```text
<部署目录>/
└── src/
    ├── __init__.py
    ├── config.py
    ├── service/
    ├── retrieval/
    ├── maintenance/
    └── corpus/
```

| 目录 | 部署用途 |
|---|---|
| `src/service/` | HTTP MCP / REST 服务、配置、认证及执行账本；也包含 stdio 实现 |
| `src/retrieval/` | 索引验证、搜索、上下文读取及索引发布能力 |
| `src/maintenance/` | 使用其中的 `deploy_index` 脚本下载、校验和安装已构建的索引，并管理 systemd 服务 |
| `src/corpus/` | 现有部署脚本通过索引构建模块引入的共享代码依赖；这是代码目录，不是资料目录 |

`src/config.py` 与 `corpus/workspace.py` 提供统一配置读取和路径解析。仅运行检索服务需要 `service/`、`retrieval/` 与 `corpus/` 以及上述两个顶层 Python 文件；通过 `src.maintenance.deploy_index` 更新索引还需要 `maintenance/` 与 `corpus/`。以目录为单位复制即可，无需逐文件裁剪。部署机不执行导入或重新构建，`src/ingestion/`、原始资料、`sources/` 和 manifest 无需放过去。两个 skill 在 AI 客户端所在环境加载。

同时准备[requirements.txt](../requirements.txt)并按其中版本安装依赖，以及服务配置、受限 HTTP 凭据、已发布的索引和可写账本目录。`corpus_path` 指向索引根目录，`mcp.ledger.path` 指向独立可写位置。使用索引部署脚本时，还需配置 GitHub 请求头文件、部署配置和 systemd，具体条件见[可选发布流程](#可选的-actions-与-systemd-发布)。

以部署目录为工作目录，检索服务的启动方式仍是：

```bash
venv/bin/python -m src.service.server --config config/config.json
```

代码更新时同步这套目录与依赖，保持同一版本；索引部署脚本只更新索引，不更新服务代码或配置。

## 配置模板

| 示例 | 用途与安装位置 |
|---|---|
| [config.json.example](config.json.example) | workspace 与 HTTP/stdio 统一配置；默认 HTTP 字段匹配同机 HTTPS 代理，其他场景按下文调整；复制为 `config/config.json`，由 `--config` 指定 |
| [deploy-index.json.example](deploy-index.json.example) | 可选的 Actions/systemd 索引发布配置；适用条件见[可选的 Actions 与 systemd 发布](#可选的-actions-与-systemd-发布) |
| [github.header.example](github.header.example) | 可选发布流程的 GitHub API 请求头格式；实际 PAT 文件须满足下文的权限校验，并可由部署脚本执行身份读取，不使用仓库里的示例 |
| [credentials.json.example](credentials.json.example) | 服务端 Bearer 凭据；路径由 `credentials_file` 指定 |
| [nginx.conf.example](nginx.conf.example) | 与 FastAPI 同机的反向代理配置，在 Nginx `http {}` 中 include |
| [systemd.service.example](systemd.service.example) | 可选的系统级 Linux HTTP 服务单元；示例安装位置为 `/etc/systemd/system/learn-corpus.service`，账户与路径按环境调整 |

## 运行身份与文件权限

先确定实际运行身份，再安排文件所有者与访问权限：本机 stdio 通常继承 AI 客户端启动身份，手动 HTTP 使用启动命令的身份，systemd 使用实际单元的 `User` / `Group`。检索服务不要求固定用户名、UID/GID 或 root 身份，文件也不统一要求归 root 所有；示例中的 `learn-corpus` 账户与 `/opt/learn-corpus` 路径可以替换。

| 文件或目录 | 代码要求与准备方式 |
|---|---|
| 服务代码、虚拟环境、配置和检索索引 | 运行身份需能读取文件、遍历所有父目录并执行 Python；服务只读索引。文件所有者与具体权限按环境安排，服务没有统一的属主或权限位校验 |
| HTTP 凭据文件 | 必须是可由 HTTP 运行身份读取的普通文件，group/other 权限位必须为零；`0600` 是常用设置，可读取的 `0400` 也满足代码校验。通常由实际读取账户拥有，不限定账户名 |
| MCP 账本父目录 | 必须预先存在，group/other 权限位必须为零，通常设为 `0700`；运行身份须能遍历目录、创建数据库与事务伴随文件 |
| MCP 账本数据库 | 已有文件必须是普通文件且 group/other 权限位为零；服务创建并最终设置为 `0600`。运行身份须能读写并调整文件权限，通常由该运行账户创建和拥有 |
| 可选部署流程的 GitHub PAT 文件 | 必须能由部署脚本执行身份读取，group/other 权限位必须为零；`0600` 是示例，`0400` 也满足校验。文件所有者由实际管理员安排，代码不检查其用户名 |

凭据和账本的权限位校验是当前实现的约束，不能通过开放组或其他用户权限来解决读取问题。多个进程共用账本时，每个运行身份都须满足上述读写要求；不能仅凭同属一个组就假定可以共享。配置文件与索引根目录由有权维护它们的账户管理，服务能读代码和索引、能写账本即可，不需要对整个部署目录开放写权限。

`src.maintenance.deploy_index` 的完整发布流程要求以 root 执行，负责设置新索引的所有者和权限、切换索引、管理系统级 systemd 服务及失败恢复。检索服务使用实际配置的运行账户；具体发布条件见[可选发布流程](#可选的-actions-与-systemd-发布)。

<a id="统一配置与启动"></a>

## 服务配置与启动

日常索引更新、客户端重连、故障判断与账本轮换的检查顺序见文末[运维检查清单](#运维检查清单)。实际配置可由 `--config` 指定，省略时按[配置选择规则](#路径与配置选择)读取。相对路径以实际配置文件所在目录解析。

从仓库根目录使用同一个入口，具体模式由 `mcp.transport` 选择。新环境先准备配置；已有配置时只调整所需字段，不覆盖运行配置。

以下命令针对同仓库默认布局；submodule 或独立数据目录应使用已选定的配置位置，并在 `<data_root>/var` 准备账本目录，不重复覆盖现有配置。

以下是本机 HTTP 初始化示例，假设由之后运行服务的同一账户执行，且该账户可写项目目录。新环境先将示例复制为真实配置；下面的公开凭据只用于本机启动检查，不能用于部署：

```bash
cp config/config.json.example config/config.json
cp config/credentials.json.example config/credentials.json
chmod 0600 config/credentials.json
install -d -m 0700 var
```

stdio 只需统一配置和数据工作区内的账本目录，不要求 HTTP 凭据文件。改由其他账户运行服务时，按[运行身份与文件权限](#运行身份与文件权限)准备所有者和访问权限；仅由管理员执行上述复制命令，不会自动让其他运行账户能够读取 `0600` 凭据或写入 `0700` 目录。

启动前按下文的[本机 stdio](#本机-stdio-连接)或[HTTP 配置](#http-服务配置)调整连接字段。在 venv 已安装且 `meta/corpus/current` 已构建的前提下，使用以下入口：

```bash
venv/bin/python -m src.service.server --config config/config.json
```

- `mcp.transport = "http"`：启动 HTTP 服务，提供 REST API 与 `/mcp`，凭据和网络边界使用 `http` 配置。
- `mcp.transport = "stdio"`：仅启动管道 MCP，不创建 HTTP 应用或监听端口，也不提供 REST API；正常由 MCP host 执行上述命令并管理管道与进程。
- 工作区字段为 `workspace.data_root`。服务公共字段为 `corpus_timeout_ms` 和可选的 `corpus_path`（默认工作区下的 `meta/corpus`）。HTTP 专属项放在 `http`，MCP execution ledger 放在 `mcp.ledger`，新任务执行限制放在 `mcp.task_limits`，stdio 专属时限放在 `mcp.stdio`。
- 统一格式必须显式选择 `http` 或 `stdio`；不支持 `both` 或按环境自动选择。未选分支可省略；保留时只检查对象结构与已知字段，运行参数只校验选中分支。stdio 不要求 HTTP 凭据文件存在，也不应用 HTTP 认证和并发字段。未知字段、重复 JSON key 和混用新旧结构均拒绝。

`config.json` 选择的是本进程启动模式，不会修改 Codex 配置或关闭已由其他进程运行的服务。host 仍须对应配置 HTTP `url` 或 stdio `command`；切换后重新建立连接，不能只改 server 配置就期待 host 自动换 transport。

服务仍可通过 `python -m src.service.stdio.stdio_server --config FILE` 启动 stdio，但它也只接受明确选择 stdio 的统一配置。

<a id="本机-stdio-mcp"></a>

## 本机 stdio 连接

stdio 入口由客户端启动独立子进程，直接读取本机检索索引，不启动 HTTP 服务。当前使用 POSIX 非阻塞管道，支持范围为 Linux/WSL；Windows 原生管道与 SSH 跨机 stdio 尚未纳入支持范围。跨机使用可选择 HTTP MCP。

使用同一份 [config.json.example](config.json.example) 创建 `config/config.json`，将 `mcp.transport` 改为 `"stdio"`，确认 workspace 指向自己的资料目录；索引另行安装时用 `corpus_path` 指向启动环境内的索引根目录。`http` 分区可保留或省略；相对路径以配置文件目录为基准，索引必须已离线构建，服务不导入来源或自动重建。入口为：

```bash
venv/bin/python -m src.service.server --config config/config.json
```

该命令等待 stdin 中的 MCP 消息；正常使用时由 host 启动。在目标 Codex 中用下面的段落替换同名 HTTP 配置，按实际环境填写绝对路径：

```toml
[mcp_servers.learn_corpus]
command = "/path/to/learn-corpus/venv/bin/python"
args = ["-m", "src.service.server", "--config", "/path/to/learn-corpus/config/config.json"]
cwd = "/path/to/learn-corpus"
startup_timeout_sec = 20
tool_timeout_sec = 30
```

`command`、`args` 和 `cwd` 为 Codex 的 stdio 配置字段，超时数值为示例，不是官方推荐固定值。启动身份需要读取代码、配置和索引的权限；不设置 `url` 或 `bearer_token_env_var`，也不把索引路径作为工具参数传入。[Codex 官方 MCP 配置](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)

| stdio 配置项 | 含义与默认值 |
|---|---|
| `corpus_path` | 可省略，默认 `<data_root>/meta/corpus`；显式填写时为索引根目录 |
| `corpus_timeout_ms` | 必填，与 HTTP 共用 core 时限语义 |
| `mcp.transport` | 必须为 `"stdio"`，仅启动管道 MCP |
| `mcp.stdio.frame_timeout_ms` | 默认 5000；从收到一帧首字节到完整换行的时限，连接空闲不计时 |
| `mcp.stdio.write_timeout_ms` | 默认 5000；每条响应从进入有界输出缓冲到写完的时限，不因部分写出而重置 |
| `mcp.stdio.shutdown_timeout_ms` | 可省略，默认 `corpus_timeout_ms + 1000`；关闭后的清理宽限期 |

单帧上限固定为 32 KiB，JSON 结构与 HTTP MCP 共用 256 个 key、单个数组 20 项及 8 层深度上限，`params._meta` 同样独立限制为 192 个 key 和紧凑 UTF-8 JSON 编码 8 KiB；计量 LF 前的所有字节（CRLF 的 CR 计入），LF 不计入。收到完整非法帧时返回安全 JSON-RPC error；notification 不返回结果。超长未换行输入、半帧超时或 EOF 时残留半帧会关闭连接，不能可信提取 ID 时不反射输入。重复使用仍在处理的工具请求 ID 也关闭连接，避免把错误响应关联到原请求。

每进程只有一个工具执行名额，五个工具共用；忙时立即返回 `isError=true`、`rate_limited`，不在应用中排队。握手、ping、tools/list 和合法通知由接收循环处理；输出缓冲最多两条响应，满时暂停读取以施加管道背压，不产生无限任务或输出队列。各 stdio 进程与 HTTP 服务不共享 transport 并发额度；多个进程可以通过 SQLite 短事务共享同一个 execution ledger。HTTP 分区的 `global_concurrency`、`client_concurrency` 或凭据字段不作用于 stdio，也不能放进 `mcp.stdio`。

取消通知不主动中止检索，实际工作结束前保留名额，正常情况下结果写出后释放。EOF、断管、SIGTERM/SIGINT 或传输错误启动关闭；等待工作结束并关闭 store，超过宽限期则终止整个 stdio 子进程。正常退出码为 0，配置/协议/传输失败为 2，关闭宽限期耗尽为 3；退出码 3 的强制路径不保证 finally 或日志完成，不影响独立运行的 HTTP 服务。

stdout 只输出协议消息，工具错误和 JSON-RPC 错误通过它返回 host；应用安全诊断写 stderr，不自行保存日志文件。stderr 阻塞时丢弃诊断，避免阻塞检索和退出；host 是否捕获、落盘或展示日志由 host 决定。日志不包含 query、正文、路径、凭据或堆栈。

每个进程启动时固定检索索引。切换 `current` 后需由 host 重启相应 stdio 进程才能加载新版；在所有使用旧检索索引的进程退出前，不清理其目录。stdio 不提供热更新；跨请求调用次数、索引版本绑定和 estimated evidence budget 由共享 execution ledger 约束，但不承诺中断恢复或结果重放。

## MCP 任务与执行账本

HTTP 与 stdio MCP 共用 `mcp.ledger` 配置；REST 不创建或更新任务。`path` 可省略，默认使用 workspace 下的 `var/execution-ledger.sqlite3`；显式相对路径以实际统一配置文件所在目录为基准。账本目录须预先存在且 group/other 权限位为零，通常设为 `0700`；数据库创建并最终设为 `0600`，实际运行账户需能读写并调整文件权限，见[权限说明](#运行身份与文件权限)。账本保存 query、`source_title`、`heading_path`、path 和 locator 等敏感元数据，但不保存旧的检索单元 `title`、snippet、正文、凭据或模型推理，不应放在 `sources/`、immutable 检索索引或仓库跟踪目录内。

配置文件由用户或 agent 按部署目标修改；执行账本由服务维护，不通过 SQL 手改任务、次数或预算。索引、登记与日志同样通过项目入口更新，范围见[产物维护边界](../docs/usage.md#产物维护边界)。

| 配置项 | 含义与默认值 |
|---|---|
| `mcp.ledger.path` | 默认 `<data_root>/var/execution-ledger.sqlite3`，独立可写 SQLite 文件 |
| `mcp.ledger.busy_timeout_ms` | 默认 1000；SQLite 锁等待上限，超时后工具 fail closed |
| `mcp.ledger.max_tasks` | 默认 10000；达到后拒绝创建新任务 |
| `mcp.ledger.max_mb` | 默认 256；按 1 MiB = 1,048,576 bytes 换算，达到后拒绝创建新任务 |

初版不运行后台清理、启动时清理或自动 `VACUUM`。容量门限只拒绝新任务，已有任务可以继续结算，因此文件可能略高于 `max_mb`。需要轮换时先停止所有使用该数据库的服务实例，再将数据库移到权限受限的归档位置并创建新库。

`mcp.task_limits` 可省略，字段也可分别省略；缺失值使用下表默认值。限制在创建任务时写入账本，因此修改配置并重启后只影响新任务，已有任务继续使用原快照。

| 配置项 | 含义与默认值 |
|---|---|
| `mcp.task_limits.search_calls` | 默认 16；每个任务允许 admission 的 search 次数 |
| `mcp.task_limits.read_calls` | 默认 32；每个任务允许 admission 的 read 次数 |
| `mcp.task_limits.estimated_evidence_tokens` | 默认 64000；全部成功、已知支出和未解决预留共用的任务累计 evidence 预算 |

三个值必须是正的 64-bit 整数。默认累计预算为 64,000 estimated evidence tokens，客户端通过多次有界 search/read 使用累计额度；单次响应仍最多 8,000 estimated evidence tokens 和 65,536 bytes。

已有配置显式填写的数值继续生效，升级代码不会覆盖它们；需要采用新默认值时，修改或省略相应字段并重启 HTTP 服务或重连 stdio。已有任务的限制快照保持不变，调整后的限制用于之后创建的新任务。

`get_retrieval_task` 始终从完整账本计算 execution 与 total；调用和引用明细按 65,536-byte 单次响应边界保留最近的完整记录。`calls_truncated`、`items_truncated` 分别标明省略，`calls_total`、`items_total` 给出完整数量。字段内容不会被截断，详情接口不分页。

公开响应上限固定为 65,536 bytes；请求头固定为总计 8,192 bytes、最多 64 个 header、Authorization 最多 256 bytes。REST 请求 JSON 总计最多 32 个 key；MCP 请求总计最多 256 个 key，其中 `params._meta` 最多 192 个 key、紧凑 UTF-8 JSON 编码最多 8 KiB。两者单个数组均最多 20 项、整份消息嵌套深度最多 8 层；key 数包含所有嵌套对象。MCP 元数据预算独立于业务参数校验，不允许通过元数据扩展工具参数。这些是代码中的协议和资源边界，不是部署调优项，`config.json` 中出现同名字段会按未知字段拒绝。Nginx 仍应独立限制其接收边界，代理限制与应用常量不构成同一配置来源。

`corpus_path` 指向索引数据目录，不是 `src/retrieval/` 代码目录，也不是单个 SQLite 文件；未填写时使用 workspace 下的 `meta/corpus`。相对路径以 **配置文件所在目录** 为基准；若将配置安装到 `/etc/learn-corpus/config.json`，应明确设置 `workspace.data_root` 或 `corpus_path` 为部署机实际路径。服务启动时固定 current 检索索引；修改配置、凭据或切换 current 后需重启。统一入口可用 `--config` 选择配置；活动分支缺少必填字段、字段值无效或出现未知字段时，服务说明原因并以状态码 2 退出。

## HTTP 服务配置

### 按连接场景调整

`config.json.example` 默认监听回环地址，但允许的 Host 是代理访问域名。直接用 `http://127.0.0.1:2699` 访问时，需要先调整 Host 允许列表；仅启动成功不代表该 URL 已被允许。

| 场景 | 监听与直接对端 | Host 与 Origin |
|---|---|---|
| 本机直连 | 保留 `host = "127.0.0.1"`、`port = 2699` 和 `allowed_peers = ["127.0.0.1"]` | 设置 `allowed_hosts = ["127.0.0.1:2699"]`；不使用浏览器 Origin 的客户端可设置 `allowed_origins = []` |
| 同机 HTTPS 代理 | 保留回环监听与回环 peer；代理上游为 `http://127.0.0.1:2699` | `allowed_hosts` 使用实际访问域名；需要接受 Origin 时填写对应的 HTTPS origin |
| 分机 HTTPS 代理 | `host` 使用后端实际私有监听 IP，或按环境选择 `0.0.0.0`；`allowed_peers` 填写代理连接后端时的实际源 IP | 与代理转发到应用的 Host、请求携带的 Origin 对齐 |

`allowed_peers` 检查直接连接应用的对端，不是最初客户端的地址。服务关闭代理头解析，不依据 `X-Forwarded-For`、`Forwarded` 或 `X-Real-IP` 放行请求。网络入口和后端访问控制需与这一边界一致；跨机传输应覆盖凭据与检索内容的保护，代理到后端的链路也需按环境保障。

Host 为精确匹配，使用非标准端口时包含端口，例如 `corpus.example.com:8443`。Origin 列表只校验请求实际携带的 Origin；空数组允许没有 Origin 的请求，拒绝携带 Origin 的请求，不代表浏览器跨域支持。

### 配置字段

| 配置项 | 说明 |
|---|---|
| `corpus_path` | 索引根目录，项目中为 `meta/corpus/`，包含 `current -> idx_xxx`；检索索引内有 `corpus.sqlite` 和 `index.json` |
| `http.host` / `http.port` | HTTP 模式必填的监听 IP 与端口；统一入口不提供默认值或 CLI 覆盖 |
| `http.credentials_file` | 实际 HTTP 运行身份可读的普通文件，group/other 权限位必须为零；例如 `0600` 或可读取的 `0400` |
| `http.allowed_peers` | 精确允许直接连接 FastAPI 的 ASGI/TCP 对端 IP，不接受 CIDR；同机示例使用 `127.0.0.1`，分机部署改为代理连接后端时的实际源 IP |
| `http.allowed_hosts` | 精确匹配 Host，非标准访问端口需包含端口 |
| `http.allowed_origins` | 允许的 Origin；空数组允许无 Origin 请求，拒绝携带 Origin 的请求 |
| `corpus_timeout_ms` | 一次 corpus core 操作的总时限，覆盖连接锁等待、SQL、聚合和序列化，毫秒 |
| `http.global_concurrency` / `http.client_concurrency` | 全局 / 单客户端并发上限，满额返回 429 |
| `http.body_timeout_ms` | 接收完整请求体的时限，毫秒 |

## 访问凭据

`credentials.json` 是数组，每项只有 `cid`、`key`、`secret`：

- `cid`：客户端 ID，1～64 个字母、数字、下划线或短横线。
- `key`：`<cid>_key_<编号>`，编号为 1～10 位十进制数字；key 全局唯一，同一 cid 可有多个 key 并共用客户端并发额度。
- `secret`：32～64 个 URL-safe 字符（字母、数字、`_`、`-`），推荐用 `secrets.token_urlsafe(32)` 随机生成。服务端保存明文，文件须满足[权限校验](#运行身份与文件权限)，通常设为 `0600`。

客户端发送 `Authorization: Bearer <token>`，其中 `token = base64url("<key>.<secret>")`。生成时去掉末尾 `=`；服务也接受规范的有填充编码。Base64URL 是编码，不是加密。

生成一个随机 secret：

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

填写凭据后，从文件中读取指定 key 并生成 token（按实际位置和 key 修改）：

```bash
python3 - <<'PY'
import base64
import json
from pathlib import Path

rows = json.loads(Path("config/credentials.json").read_text())
entry = next(row for row in rows if row["key"] == "test_key_1")
token = base64.urlsafe_b64encode(
    f"{entry['key']}.{entry['secret']}".encode("ascii")
).decode("ascii").rstrip("=")
print(token)
PY
```

Swagger Authorize 只填写编码后的 token，不包含 `Bearer ` 前缀。修改或删除 key 后重启服务生效。旧 `client_id/key_id/secret_sha256` 配置和旧 token 不再接受，原有摘要无法还原 secret，迁移时重新生成凭据。[示例](credentials.json.example) 是公开测试数据，展示同一 cid 的两个 key，不能用于正式部署。

## Codex HTTP MCP 连接

`POST /mcp` 与 REST 使用同一服务进程、检索索引、凭据和并发额度。当前实现的 MCP revision 为 `2025-06-18`，使用无会话 Streamable HTTP、JSON 响应，不提供 GET SSE、session ID 或恢复重放。注册 `start_retrieval_task`、`search_sources`、`read_bundle`、`get_retrieval_task` 和 `status`；没有 resources、prompts 或管理接口。

仓库内的 Codex 检索策略位于 [learn-corpus-retrieval](../.agents/skills/learn-corpus-retrieval/SKILL.md)，由仓库版本控制并供项目内 Codex 直接发现；可通过 `$learn-corpus-retrieval` 显式调用或让 Codex 按 description 自动匹配。skill 只依赖以下 MCP 连接，不保存 URL 或凭据。

在目标 Codex 配置中添加以下段落，替换示例域名，并在启动该客户端的环境中安全注入已编码的 token：

```toml
[mcp_servers.learn_corpus]
url = "https://corpus.example.com/mcp"
bearer_token_env_var = "LEARN_CORPUS_TOKEN"
startup_timeout_sec = 20
tool_timeout_sec = 30
```

本机直连时将 `url` 改为 `http://127.0.0.1:2699/mcp`，并按[HTTP 配置](#按连接场景调整)同步 Host 允许列表。HTTPS 示例域名与 Nginx、服务配置模板一致，实际使用时一并替换。

环境变量值为现有 Base64URL `key.secret` token，不包含 `Bearer ` 前缀。不要把实际 token 写进 TOML、命令行参数或仓库。连接到本机隔离测试服务时，应确认回环请求不被系统代理转发。客户端配置项见 [Codex 官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。

成功结果位于 `structuredContent`；search/read 保留 REST 证据字段，但 MCP 另有必填 `task_id` 输入和 `execution` 摘要，客户端不再传 `index_id`。`content` 仅提供固定短提示，不复制证据正文。依赖纯文本结果的客户端不在本轮兼容范围内。已用额度（`usage`）只计量 core 证据 JSON，不把 execution 摘要、MCP JSON-RPC 包装或固定提示计作证据或累计预算；整个 MCP structured result 仍不超过 65,536 bytes。

协议封装错误返回 HTTP 400 和 JSON-RPC 固定错误；已识别工具的参数/业务错误返回 HTTP 200、`isError=true`，text 内保留 `error.code/message/request_id`。query 校验、任务预算、调用次数、任务阻断及响应预算错误可附加安全的 `error.details`，只包含参数位置、固定原因、约束、计数或阻断类别，不回显 query 正文。认证、接收限制和并发拒绝沿用 HTTP 401/413/429 等入口错误，客户端先判断 HTTP 状态，再判断工具 `isError`。旧 revision、batch、未知工具及未开放能力均拒绝。

JSON 资源预算超限返回 `-32600 / Request resource limit exceeded`；解析完成后的元数据独立预算超限返回 `-32602 / Metadata resource limit exceeded`，保留已验证的请求 ID。JSON 语法、重复 key 或非法值仍返回 `-32700 / Parse error`。解析阶段提前拒绝时无法可信取得 ID，仍返回 `id=null`；本次未改变客户端对这类响应的等待行为。

检索索引必须符合当前代码支持的策略版本，包括 `qmd-style-v2` 与 `bm25-rrf-v2`。版本不符时拒绝读取，不自动迁移；检索数据流见[架构说明](../docs/architecture.md)。

## Nginx 代理配置

[nginx.conf.example](nginx.conf.example) 是可选的同机 HTTPS 反向代理模板，在 Nginx 的 `http {}` 中 include。其他代理实现也需满足应用的 Host、直接对端、认证和响应边界。

- `listen`、`server_name`、证书路径：填写代理的监听端口、实际访问域名及证书文件。
- 证书、私钥与日志目录：按实际 Nginx 进程身份配置读取和写入权限；文件所有者不要求与 Learn Corpus 服务账户相同。
- `proxy_pass`：同机示例与 `http.host` / `http.port` 一致，均为 `127.0.0.1:2699`；`Host` 原样转发，与 `allowed_hosts` 对齐。分机部署时同时修改上游地址、HTTP 监听 IP 和 `allowed_peers`。
- `X-Forwarded-For`、`Forwarded` 和 `X-Real-IP`：代理转发前清空；应用只依据实际 ASGI/TCP 对端匹配 `allowed_peers`，不解析这些头。
- `rate=5r/s`、`burst=10 nodelay`：全服务共享入口限流，突发额度内立即放行，超额返回 429；不等于应用并发上限。
- `client_max_body_size`：模板为 `32k`，允许完整 MCP envelope；应用仍分别限制 REST 16 KiB、MCP 32 KiB；`proxy_read_timeout 20s` 是两次上游读取之间的超时。
- `access_log`：仅记录状态、耗时、字节数及限流结果；示例 `error_log /dev/null crit` 丢弃错误日志。

修改后，由具备管理目标 Nginx 实例权限的身份先执行 `nginx -t`，通过再 reload。采用示例日志路径且具备读取权限时，可用 `tail -f /var/log/nginx/learn-corpus-access.log` 查看；其他环境使用实际日志路径。

## systemd 服务配置

[systemd.service.example](systemd.service.example) 是可选的系统级 Linux HTTP 服务模板，假设已准备 `learn-corpus` 用户和组，代码位于 `/opt/learn-corpus`。使用其他账户或路径时，同时调整单元、服务配置与文件权限。用户级 systemd 或其他进程管理方式需按实际环境另行配置，不能原样照搬该单元。

- `User` / `Group`：填写实际服务账户与组；`learn-corpus` 是示例名称，不会由单元自动创建。`WorkingDirectory`、`ExecStart` 与服务配置路径按安装位置一起调整，监听地址与端口来自服务配置。
- 本示例仅用于 `mcp.transport = "http"`；stdio 由 host 创建管道与子进程，不使用该 systemd 单元。
- `ProtectSystem=strict`、`ProtectHome=true`：该模板限制文件系统写入并屏蔽 `/home`、`/root`、`/run/user`。原样使用模板时，代码、配置、索引及链接目标需位于这些路径之外；其他启动方式不受这个单元的隔离设置约束。
- `ReadWritePaths=/opt/learn-corpus/var`：示例只为账本目录开放隔离层的写入，按实际 `mcp.ledger.path` 调整；它不授予 Unix 文件权限或改变所有者。若采用示例账户和路径，可由有权设置所有者的管理员执行 `install -d -o learn-corpus -g learn-corpus -m 0700 /opt/learn-corpus/var`。其他环境用实际账户与路径准备目录，服务仍须满足[权限要求](#运行身份与文件权限)。
- `Restart=on-failure`：异常退出重启；停止先 SIGTERM，30 秒后强制清理。`active` 不代表 HTTP 已就绪。

文件系统隔离选项的语义见 [systemd 官方说明](https://github.com/systemd/systemd/blob/main/man/systemd.exec.xml)。以下命令假设单元名为 `learn-corpus`，由具备管理该系统级单元权限的身份执行，例如 root 或经授权的 sudo；服务进程仍使用单元中配置的运行账户：

```bash
systemctl daemon-reload
systemctl start learn-corpus
systemctl status learn-corpus
systemctl restart learn-corpus
journalctl -u learn-corpus -f
```

需要开机启动时，以同样的管理权限执行 `systemctl enable learn-corpus`；读取 journal 的权限按目标机日志策略安排。启动后通过实际使用的 HTTP 入口检查 `/healthz` 和带 Bearer 的 `/v1/status`；使用代理时，还需验证代理入口及客户端 MCP 连接。

服务保留 Uvicorn 默认的正常启动、停止日志（包括 `Application startup complete.` 和 `Uvicorn running on ...`），关闭默认 access log 并过滤异常详情；应用请求日志仍使用固定字段 JSON。


<a id="检索索引的-ci-构建与手动发布"></a>

## 索引构建与发布

### 本地构建与使用

有效来源导入并完成验证后，可在仓库根目录构建与发布本地索引：

```bash
venv/bin/python -m src.retrieval.build_lexical_index --publish
```

不加 `--publish` 时只构建，不切换 `current`。使用本地索引无需 Actions、GitHub PAT、Nginx 或 systemd；服务配置的 `corpus_path` 指向索引根目录。本地命令不重启服务，也不包含部署脚本的服务验收与回滚流程。构建后按[索引更新与验证](#索引更新与验证)完成运行版本验收。

### 可选的 Actions 与 systemd 发布

以下是现有维护脚本支持的发布流程：Actions 构建索引、下载 artifact，再由部署机手动安装并管理 systemd 服务。它只适用于已配置该工作流的仓库及符合脚本要求的 Linux 部署环境，不是使用项目的必要步骤。

构建 CLI 使用 `--repository owner/name`，部署配置使用必填的 `repository` 字段，分别显式指定并校验数据仓库来源；原有部署配置升级时必须补上该字段。构建函数及部署辅助函数的仓库参数也必须显式传入，代码不提供默认仓库；配置示例中的仓库名按实际数据仓库替换。私有数据 commit 固定代码 submodule，代码与数据的对应关系见[工作目录说明](../docs/workspaces.md)。具体校验见 [build_release.py](../src/maintenance/build_release.py) 和 [deploy_index.py](../src/maintenance/deploy_index.py)。部署脚本要求 HTTP 服务监听回环地址或 `0.0.0.0`，且允许回环验收；只监听非回环私有 IP 的分机部署不能直接使用该脚本。它不依赖特定代理或中转链路，也不验收 HTTPS 入口。

构建产物称为 **retrieval index（检索索引）**，版本字段统一为 `index_id`，ID 为 `idx_…`，产物目录为 `<corpus_path>/<index_id>/`，元数据文件为 `index.json`。发布包的 `release.json` 使用 `version=2`，execution ledger 使用 schema 3；旧产物、旧接口字段和旧账本直接拒绝，不自动迁移。切换新版代码前须重新构建检索索引，将 `corpus_path` 指向预先创建的干净目录，账本使用新的数据库文件；客户端同步新的 REST/MCP 字段并重新发现工具。部署脚本可从空目录安装首个索引并创建 `current`；服务代码、配置、真实凭据和账本目录仍须事先准备，脚本不负责升级或迁移它们。

公开仓库的 [ci.yml](../.github/workflows/ci.yml) 运行功能测试、凭据模式检查和公开示例。另提供 [build-index.yml](../.github/workflows/build-index.yml) 作为可复用的真实索引构建流程，它仅由 `workflow_call` 启动。私有数据仓库参照 [调用模板](build-index.workflow.example) 创建自己的 `.github/workflows/build-index.yml`，填写仓库标识，并将 workflow 引用固定为代码 commit；升级时与 `engine/` 的 gitlink 一起更新。模板中的 `main` push 或手动触发属于私有仓库。可复用流程 checkout 调用方的私有数据及固定代码，安装依赖、执行相关测试、检查已提交来源并构建索引，最后上传保留 7 天的 `learn-corpus-index-<run-id>-<attempt>` artifact。数据、日志和 artifact 保留在调用方的私有运行中；失败时不上传产物。workflow 只有 `contents: read`，不连接部署机或切换运行索引，不需要部署用的 GitHub secret。

部署机按[部署代码目录](#检索部署所需的代码目录)准备同一版本的服务代码和 `deploy_index` 依赖，无需完整克隆。脚本每次只更新检索索引，不升级服务代码、依赖或配置。[github.header.example](github.header.example) 仅展示请求头格式，不包含可用凭据。实际 PAT 文件须可由脚本执行身份读取，group/other 权限位必须为零，通常设为 `0600`；内容是 `Authorization: Bearer <PAT>`，PAT 只需本仓库的 Actions 读取权限。不要把 PAT 作为命令参数、提交到仓库或复制进 artifact。

先参照 [deploy-index.json.example](deploy-index.json.example) 创建不纳入版本控制的部署配置，例如 `/opt/learn-corpus/config/deploy-index.json`。`service_config` 指向已有的 HTTP 服务配置；相对路径以部署配置文件所在目录为基准。示例中的 `github_header_file` 指向同目录的 `.example` 占位文件；正式部署时改为真实 PAT 文件路径。`service` 填写实际 systemd 单元名，`service_user` 填写已存在且用于读取索引的账户，不固定为 `learn-corpus`。配置文件由实际部署管理员维护，仅允许授权身份修改，并可由脚本执行身份读取；代码不检查配置文件是否归 root 所有。不要把真实凭据写入示例文件。

完整发布流程要求以 root 执行，脚本会在开始时校验执行身份。以下命令假设项目确实位于 `/opt/learn-corpus`，由 root 或经授权提升为 root 的身份执行；其他安装位置替换相应路径。脚本命令行只接受 `--config`，不接受 run ID 或其他部署选项：

```bash
cd /opt/learn-corpus
/opt/learn-corpus/venv/bin/python -E -s -B -m src.maintenance.deploy_index \
  --config /opt/learn-corpus/config/deploy-index.json
```

部署配置中的 `run_id` 为 `"latest"`（省略时也是此默认值）时，脚本选择预期 workflow 在 `main` 上创建时间最新的成功 run。需要固定部署目标时，把它改成 GitHub Actions workflow run ID，例如 `"12345"` 或 `12345`，而不是 artifact 名称或 artifact ID；数字字符串须只包含 ASCII 数字且表示正整数，读取后转换为整数，允许前导零，不接受空白、正负号或小数。自动选择不等待仍在进行的构建；选中 run 的 artifact 缺失或过期时直接退出，不退回更旧的成功 run。若同一 run 重跑，只接受最新 attempt 成功后的产物，不退回旧 attempt。

脚本向 stdout 输出带 `[deploy]` 前缀的阶段进度并立即刷新，包括配置检查、run 选择、artifact 下载与校验、安装、切换、服务重启和验收；失败时报告回滚或首次部署清理进度。已生效的检索索引会报告复核过程后退出。进度日志不输出凭据或请求头，最后保留成功结果消息；失败退出信息仍写入 stderr。

脚本从部署配置指向的服务配置解析 `corpus_path`、HTTP 监听地址、Host 允许列表及服务凭据，校验 run、commit、artifact ZIP hash、发布元数据、SQLite 和本机代码版本；不兼容时在切换前退出。`corpus_path` 必须是已存在的目录，实际服务身份须能遍历它及所有父目录；脚本不创建索引根目录。新安装的索引目录归配置的 `service_user` 及该账户主组所有，目录设为 `0550`，内部文件设为 `0440`；这是当前脚本的固定行为。脚本使用账户主组，不读取 systemd 单元中的 `Group`，实际服务身份须能按这些权限读取索引。复用已安装索引时脚本不重新设置这些权限。发布前先检查链接状态：

| 执行前状态 | 发布行为 |
|---|---|
| 没有 `current`、`previous`，包括空目录 | 安装指定 run 的索引，创建 `current`，不创建 `previous`；不要求已有运行中的服务 |
| 只有 `current` | 先验收旧服务，再更新 `current` 并创建指向旧索引的 `previous` |
| 同时有 `current`、`previous` | 先验收旧服务，再更新 `current`，将旧 `current` 保存为 `previous` |
| 只有 `previous`，或链接损坏、越界、指向嵌套目录或无效 ID | 在下载前退出，保留原目录内容 |

`current` 和 `previous` 如存在，必须是指向索引根目录直接子目录的有效符号链接；同一索引已生效时复核文件和服务后退出，原先缺少的 `previous` 保持缺少。安装完成后脚本重启服务，并直连本机回环 HTTP `/v1/status` 做带认证的索引验收。HTTP 服务必须可从回环地址访问，且 `allowed_peers` 包含该回环地址；`0.0.0.0` 监听时使用 `127.0.0.1`。请求的 Host 从服务配置的 `allowed_hosts` 选取，脚本不检查 HTTPS 代理是否可用。

更新失败时恢复 `current`、`previous` 的原始链接值，包括移除原先不存在的链接；若尝试过重启，则重启旧服务并验收。首次部署失败且已尝试重启时，先停止服务，再移除本次创建的链接和索引。暂存目录会清理；本次复用的已有索引和其他原有内容不会删除，起初为空的目录在清理成功后恢复为空。停止服务、恢复链接、重启验收或清理本身失败时明确报告失败类别，不报告恢复成功；更新回滚未完成时保留本次安装的索引文件，供排查和恢复。所有失败均以非零状态退出。

成功输出包含选中的 run ID、attempt 和 commit，便于核对。首次安装脚本前先核对部署机上的服务配置，脚本本身不改配置。GitHub artifact 到期后需重新触发目标提交的 workflow，不能靠部署机上的 `previous` 代替下载产物。

## 运行问题排查

先确定问题发生在索引构建、服务启动、客户端连接还是工具调用。导入失败与证据判断问题见[使用指南](../docs/usage.md#常见问题)；本节集中说明运行配置和连接问题。

### 索引或服务无法启动

| 现象 | 优先检查 |
|---|---|
| manifest 缺失或没有 ready source | 尚未接受有效来源；先完成导入再构建，空语料不直接产生可用索引 |
| ready/source set mismatch | manifest 登记与标准化 Markdown 文件集合不一致；检查未登记文件或缺失来源，不向被扫描的来源目录放普通说明文档 |
| `current` 缺失、损坏或索引不可用 | 确认索引已构建并发布、链接在索引根目录内且产物校验通过；服务不会自动重建 |
| 服务配置被拒绝 | 检查显式 transport、必填字段、未知字段及 JSON 格式；相对路径以配置文件所在目录为基准 |
| ledger 无法创建或打开 | 按[权限说明](#运行身份与文件权限)检查实际运行身份、目录遍历与读写权限、group/other 位，以及 schema、锁与磁盘情况；不删除活动数据库消除错误 |

启动参数见[服务配置与启动](#服务配置与启动)，切换版本见[索引更新与验证](#索引更新与验证)。更新链接不等于运行进程已经换用新索引。

### MCP 或 HTTP 连接异常

| 现象 | 优先检查 |
|---|---|
| 客户端没有 Learn Corpus 工具 | host 是否配置正确的 HTTP URL 或 stdio command/cwd；是否重新建立连接 |
| stdio 进程看起来在等待 | stdio 等待 host 通过 stdin 发送协议消息；正常由客户端管理，不是交互式 CLI |
| HTTP 返回 401 或 403 | Bearer 编码与凭据、直接 peer、Host 和 Origin 是否符合实际配置 |
| 返回 429 / `rate_limited` | 并发或入口限制已触发；核对在途操作和配置 |
| 握手成功但没有可用证据正文 | host 是否消费 `structuredContent`，以及业务工具是否成功；工具注册成功不等于搜索和读取均已通过 |

客户端示例见[本机 stdio](#本机-stdio-连接)和[HTTP MCP 连接](#codex-http-mcp-连接)。需要反馈时，按使用指南的[反馈问题](../docs/usage.md#反馈问题)整理信息。

<a id="首版维护检查清单"></a>

## 运维检查清单

操作前确定实际服务配置、`corpus_path`、`mcp.ledger.path`、systemd 单元及使用该账本/索引的 host；路径以实际选中的配置文件为准，相对路径以该配置所在目录解析。不要复制示例路径覆盖生产配置，也不要输出凭据。导入阶段异常先按[导入失败处理](../docs/usage.md#导入失败处理)核对。

### 索引更新与验证

1. 确认来源导入与相应验证已完成，再按[本地构建与使用](#本地构建与使用)执行已授权的构建；需要更新本地 `current` 时选择发布模式。
2. 使用 Actions/systemd 发布时，按[可选发布流程](#可选的-actions-与-systemd-发布)选择目标 run 并执行发布。核对输出中的 run ID、attempt、commit 与 `index_id`；产物缺失或过期时重新构建目标提交，不用旧包替代。其他运行方式按实际索引安装与进程管理方式处理。
3. 检查退出状态和最终结果，确认实际 `current` 指向目标索引，并按[重启与重连](#服务重启客户端重连与旧索引保留)让服务加载目标版本。HTTP 使用带认证的 `/v1/status` 或 MCP `status`，stdio 使用 MCP `status` 核对 `index_id`。部署脚本的本机回环验收成功后，实际通过 HTTPS/MCP 使用时仍须验证该入口；前者成功不自动证明代理、凭据或 host 连接正常。
4. 使用部署脚本时，报告回滚成功后核对运行服务与原索引一致；报告回滚/清理失败时保留现场、日志和安装文件，停止继续切换，逐项查明链接、服务与文件状态。不要仅凭 `previous` 存在就手工覆盖 `current`，也不要用删除目录消除报错。

### 服务重启、客户端重连与旧索引保留

重启或重连后的版本验收按[索引更新与验证](#索引更新与验证)执行。

- HTTP：部署脚本切换索引后会重启对应 systemd 服务；使用本地发布命令切换索引，或修改配置、凭据后，也需要重启该 HTTP 进程。HTTP host 通常不必因同 URL 的服务重启修改连接配置；如 host 缓存错误或工具列表，按其支持的方式重新连接/发现工具，不保证所有 host 自动恢复。
- stdio：必须由 host 结束旧服务子进程并重新建立 stdio 连接，才能加载新版索引/配置；只改链接、只刷新界面或只新建检索 task 不保证启动新进程。
- 已有 MCP task 固定索引和创建时的 limits，不因服务重启或配置修改而迁移。不要把旧任务直接当成新版任务，也不要为规避上限重建同一任务；需要明确新目标或处理版本变更时向用户说明，不能声称可无损续接旧上下文。
- 所有仍固定旧索引的 HTTP/stdio 进程退出前，保留旧目录。`current`、`previous` 不等于全部活跃进程的引用清单；首版没有自动索引回收和保留期限策略，本说明不授权清理其他旧索引。

### 账本容量检查与轮换

1. 通过现有 MCP 连接调用 `status`，读取 `execution_ledger.status`、`task_count`、`database_bytes`、`max_tasks` 与 `max_mb`。容量字节门限为 `max_mb × 1,048,576`，`database_bytes` 按 SQLite page count × page size 计算；不是整个目录占用。REST `/v1/status` 和 `/healthz` 不替代账本检查。
2. `healthy` 表示此次检查未到容量门限；`capacity_exceeded` 时拒绝新任务，但已有任务仍可结算；`unavailable` 表示账本无法检查，不应当作“空库”或“容量正常”。发生不可用时先排查权限、schema、锁等待和磁盘，不删除数据库、不自动修改任务记录。
3. 轮换会使原 task 在新的活动库中不可查询，不能继续用旧 task ID。实际轮换前确认维护窗口、归档位置和权限，以及是否需要保留旧任务查询；这些取舍由用户决定，不在首版设置自动期限或删除规则。
4. 停止所有使用该数据库的 HTTP 实例和 stdio 子进程，确认它们退出。仅停 HTTP 不足以排除共享账本的 stdio 写入。账本采用 SQLite DELETE journal；如异常退出后留有 `-journal` 等伴随文件，保留整套现场并先确认数据库状态，不在活动库上搬移或只复制主文件冒充完整备份。
5. 在确认无写入者且数据库状态明确后，将旧库移到已确认的受限归档位置，保留旧库供核查；不手工建表、不清空表，也不运行未经授权的 `VACUUM`。原路径不存在时，服务会自动创建 schema 3 新库；父目录须预先存在、group/other 权限位为零（通常设为 `0700`），并可由实际运行身份遍历和写入，新文件由该身份创建并最终设为 `0600`。采用示例 systemd 隔离设置时，`ReadWritePaths` 还须覆盖账本目录；管理员准备目录不会自动赋予服务账户写入能力。
6. 恢复服务/host，确认正常启动和 MCP `status` 中的索引、账本健康状态与计数；需要业务验证时按授权启动新任务并 search/read。若新库启动失败，保留两份现场并排查，不盲目覆盖或删除；需要恢复旧库时也必须先停止全部使用者，再核对文件与配置。实际操作及验证结果单独记录，不将本说明视作已经轮换成功。
