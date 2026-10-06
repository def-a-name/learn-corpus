# 工作目录与仓库关系

代码目录保存通用实现、skill、说明与功能测试。数据工作目录保存原始输入之外的标准化来源、登记与个人运行状态；可以用独立私有 Git 仓库管理。公开代码仓库默认不附带已导入资料或索引。

## 指定数据目录

从代码目录执行 Python 命令，在同一执行环境设置：

```bash
export LEARN_CORPUS_DATA_ROOT=/absolute/path/to/data-workspace
```

该目录必须已存在。相对值以进程当前目录解析；建议使用绝对路径。未设置时保持旧行为，以代码目录为数据根目录。设置后，manifest 中的 `sources/...` 路径相对于数据目录，不相对于代码目录。默认布局为：

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

原始输入仍只读，由调用者显式提供：单类导入使用 `--input`，混合批次使用与选中来源对应的 `--notes-input`、`--articles-input`、`--claude-input`、`--codex-input` 或 `--web-chat-input`。原先依赖隐式输入目录的 CLI 调用需要补上参数。独立 inventory 命令也只分析显式提供的输入类型；定向更新还需给出对应输入目录。

首次导入无需建立数据 Git 历史。`check_corpus` 在数据根目录不是 Git 顶层或尚无 HEAD 时跳过 Git 变更对照，仍检查来源与登记；这不等于已验证提交历史。需要检查已提交来源或构建 Actions 发布包时，数据目录必须有对应的 Git 历史。

## 私有仓库固定代码版本

一种维护结构是：

```text
private-repository/
├── engine/           # 公开代码仓库的 submodule
├── sources/
├── meta/
├── 私有说明与操作记录
└── 本地配置
```

从 `engine/` 执行通用命令，将 `LEARN_CORPUS_DATA_ROOT` 设为私有仓库根目录。导入和来源 Git 检查使用私有根目录；通用代码变更在代码仓库提交，私有仓库另提交 submodule 引用变化。公开代码不自动寻找父目录中的资料，也不自动更新私有代码引用。

两个 skill 位于 `engine/.agents/skills/`，私有工作区的开发约定应明确加载位置与记录路由。个人数据和操作记录在私有侧维护；未指定记录位置时，skill 只交付任务结果，不自行向代码目录创建私人日志。

运行服务仍使用 `--config`，`corpus_path` 和账本路径相对于配置文件解析。数据目录环境变量不替代服务配置，也不切换运行中的索引。配置示例中的相对路径按新的配置位置调整，见[配置说明](../config/README.md)。

## 构建版本与运行版本

私有数据 commit 固定 `engine/` 的代码 commit。发布包中的 `commit` 继续表示构建 workflow 的数据仓库 HEAD；可从该 commit 的 gitlink 追溯代码版本，`release.json` 仍为 version 2。构建程序校验实际代码 checkout 与 gitlink 一致且代码工作区干净，不自动跟随公开主线。

构建 CLI 必须通过 `--repository owner/name` 指定预期数据仓库；部署配置必须添加 `repository` 字段。既有部署配置升级时需明确补上真实数据仓库标识，缺失会在部署前失败；不使用示例仓库名替代实际来源。数据 commit、代码 commit、artifact 与运行服务索引分别记录，更新其中一项不自动更新其他项。

测试应使用虚构数据，并移除日常数据目录环境变量：

```bash
env -u LEARN_CORPUS_DATA_ROOT venv/bin/python -m pytest
```

这使现有功能 fixture 使用各自的临时目录；目录隔离测试会为子进程单独指定虚构工作区。
