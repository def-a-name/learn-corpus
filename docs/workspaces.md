# 工作目录与仓库关系

代码目录维护通用代码、skill、文档、配置模板和功能测试。数据目录维护标准化来源、登记、索引与个人运行状态；原始输入由调用者指定，可以位于这两个目录之外。

使用者只需克隆公开代码并准备自己的数据目录，不必创建私有 GitHub 仓库。需要版本管理、固定代码版本或使用 Actions 构建真实索引时，可以将数据目录作为私有仓库，通过 `engine/` submodule 引用公开代码。

## 指定数据目录

从代码目录执行 Python 命令，在同一执行环境设置：

```bash
export LEARN_CORPUS_DATA_ROOT=/absolute/path/to/data-workspace
```

该目录必须已存在。相对值以进程当前目录解析；建议使用绝对路径。未设置时，以代码目录为数据根目录。独立数据目录或 submodule 布局应明确设置该变量；程序不会自动寻找父目录。设置后，manifest 中的 `sources/...` 路径相对于数据目录，不相对于代码目录。默认布局为：

```text
data-workspace/
├── sources/
└── meta/
    ├── manifest.json
    ├── ingest.log
    ├── source-inventory.json
    ├── source-review-queue.md
    ├── source-review-resolutions.json
    └── corpus/
```

这些文件由相应程序生成或维护；不通过手工创建空 manifest 初始化。审核处置输入的例外见[产物维护边界](usage.md#产物维护边界)。显式工作目录模式拒绝越界输出及向嵌套代码目录写入资料，符号链接同样按实际目标检查。

`LEARN_CORPUS_DATA_ROOT` 指定导入结果与维护状态的位置，不指定原始输入。原始输入保持只读，导入时仍需显式提供输入参数；具体命令见[源码入口](../src/README.md)和导入 skill。

首次导入无需建立数据 Git 历史。`check_corpus` 在数据根目录不是 Git 顶层或尚无 HEAD 时跳过 Git 变更对照，仍检查来源与登记；这不等于已验证提交历史。需要检查已提交来源或构建 Actions 发布包时，数据目录必须有对应的 Git 历史。

## 私有仓库固定代码版本

选择 submodule 后，通用文件只在公开仓库维护，私有根目录不复制源码、skill、通用文档或配置模板，也不建立旧启动代理和 skill 链接。维护结构为：

```text
private-repository/
├── engine/           # 公开代码、skill、文档、模板和功能测试
├── sources/
├── meta/
├── 私有说明与操作记录
└── 本地配置
```

从 `engine/` 执行通用命令，将 `LEARN_CORPUS_DATA_ROOT` 设为私有仓库根目录。导入和来源 Git 检查使用私有根目录；通用代码变更在代码仓库提交，私有仓库另提交 submodule 引用变化。公开代码不自动寻找父目录中的资料，也不自动更新私有代码引用。

通用代码修改、功能测试和代码提交在公开仓库完成。若使用独立公开 checkout 开发，先在那里提交并推送，再在私有 `engine/` 获取并检出已验证的 commit。私有仓库提交 gitlink 更新；使用 reusable workflow 时，将私有调用 workflow 的引用固定到同一 commit。

从私有根目录进入代码目录后，可这样选择数据目录：

```bash
cd engine
export LEARN_CORPUS_DATA_ROOT="$(git -C .. rev-parse --show-toplevel)"
```

两个 skill 位于 `engine/.agents/skills/`，私有工作区的开发约定应明确加载位置与记录路由。个人数据和操作记录在私有侧维护；未指定记录位置时，skill 只交付任务结果，不自行向代码目录创建私人日志。

运行服务仍使用 `--config`，`corpus_path` 和账本路径相对于配置文件解析。数据目录环境变量不替代服务配置，也不切换运行中的索引。配置示例中的相对路径按新的配置位置调整，见[配置说明](../config/README.md)。

## 构建版本与运行版本

私有数据 commit 固定 `engine/` 的代码 commit。发布包中的 `commit` 继续表示构建 workflow 的数据仓库 HEAD；可从该 commit 的 gitlink 追溯代码版本，`release.json` 仍为 version 2。构建程序校验实际代码 checkout 与 gitlink 一致且代码工作区干净，不自动跟随公开主线。

构建 CLI 必须通过 `--repository owner/name` 指定预期数据仓库；部署配置必须添加 `repository` 字段。两处均使用真实数据仓库标识；缺失或不一致会在构建或部署时失败。真实索引 artifact 保存在构建它的私有仓库中。数据 commit、代码 commit、artifact 与运行服务索引分别记录，更新其中一项不自动更新其他项。

通用测试从代码目录执行，使用虚构数据，并移除日常数据目录环境变量：

```bash
env -u LEARN_CORPUS_DATA_ROOT venv/bin/python -m pytest
```

这使现有功能 fixture 使用各自的临时目录；目录隔离测试会为子进程单独指定虚构工作区。
