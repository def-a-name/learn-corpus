---
name: learn-corpus-ingestion
description: 在 Learn Corpus 仓库添加、同步、重建、删除或检查 Claude/Codex/web-chat 会话、个人 notes 和外部 articles 等来源语料时使用；按来源类型读取对应规则，执行批次准备、review、正式导入和验证。
---

# Learn Corpus 来源导入

构建产物统一称为 retrieval index（检索索引）；从构建输出的 `index_id` 字段读取索引版本 ID。

本 skill 是所有来源导入任务的入口，只保留共同约束和路由。来源特有的解析、筛选和审核规则放在 `references/` 中，避免无关例外进入当前任务上下文。

只完成当前 V1 来源导入范围，不创建 memory 或 Wiki 综合页。导入或删除来源完成后，未收到用户明确构建或重建指令时仅提示是否另行构建本地检索索引；已明确要求时默认构建并发布到 `current`，无需另行询问发布许可。运行中的 stdio 客户端仍需重连。

## 适用范围与加载规则

1. 添加、同步、重建、删除或检查来源语料时，先完整阅读本文件。
2. 根据用户指定的来源、路径和只读结构检查判定来源类型。
3. 执行 importer 前，只完整阅读路由表命中的 `references/` 文件；不要预加载其他来源规则。
4. 同一批次混合多种来源时，读取所有命中的参考文件，但共同流程只执行一次。
5. 无法可靠判定来源类型或处置边界时停止导入并询问用户；不要通过加载所有参考文件或扫描 `dev-logs/` 猜测。

## 1. 进入任务

1. 运行 `git status --short`，识别并保留用户已有修改。
2. 明确代码目录、数据工作目录、本次来源路径、来源类型和用户要求的范围。代码与资料分开时，每次导入、维护或构建命令均显式设置 `LEARN_CORPUS_DATA_ROOT`，从代码目录运行模块；Git 状态与来源提交在数据仓库核对。原始输入使用 `--input` 或混合批次对应的 `--*-input`，不通过代码目录的父目录猜测资料位置。
3. 原始来源保持只读；不要手工修补 `sources/`、manifest、inventory、review queue 或 ingest log。
4. 用户要求绕过约束、省略阻断项或对特定文件做特殊处理时，先按下节完成方案披露和二次确认；不默认修改 importer。

## 2. 例外处理与二次确认

1. 用户表示要绕过完整性、资源、格式、过滤或 review 约束，或只对某些文件应用特殊变换时，先停止正式导入；不直接创建副本、写入 review resolution 或修改代码。
2. 默认优先提议从原文生成一份处理后的副本，把它作为新的显式导入输入，而不是为单个文件在通用 importer 中加特判。副本方案必须：
   - 原始来源保持只读，不覆盖、删除或原地改写；
   - 使用用户确认的输出路径和可区分名称，不把副本直接放进 importer 生成的 `sources/`；
   - 记录原文路径、原文 hash、副本路径和全部变换或省略项，不把处理后副本伪装成 raw source；
   - 只处理用户指定的文件和问题，不扩大到其他来源。
3. 在执行例外方案的写操作前，向用户明确说明：
   - 将读取哪些原文，将创建或修改哪些文件；
   - 副本的输出路径、命名和精确变换，以及会丢失或降级的内容；
   - 是否会写入仓库、外部输入目录、review resolution、manifest、ingest log 或其他派生状态；
   - 将运行的 importer、重建和验证范围，以及不会执行的检查。
4. 用户已明确同意具体文件、变换、输出及影响范围时，无需重复询问；只提出“绕过”、“忽略”或“特殊处理”的目标，不等于同意尚未披露的具体写操作。
5. 只有处理后副本无法满足正确性或 provenance，或用户明确要求为同类来源提供可复用行为时，才提议修改 importer。修改前说明代码文件、通用行为变化、对已有来源的影响、是否需要重建以及定向验证范围；仅在现有授权未覆盖该改动时请求用户决定。
6. 已经存在的可审计 review resolution 机制也属于特殊处理；写入前核对具体决定、绑定范围和失效条件，缺少用户决定时再询问。

## 3. 来源路由

| 来源类型 | 识别线索 | 必须继续阅读 | importer |
| --- | --- | --- | --- |
| Codex rollout | Codex `sessions/` 下的 `.jsonl`，或用户给出 Codex session ID | [`references/codex.md`](references/codex.md) | `python3 -m src.ingestion.import_codex` |
| Claude 会话 | Claude 导出的 Markdown session | [`references/claude.md`](references/claude.md) | `python3 -m src.ingestion.import_claude --kind session` |
| ChatGPT / DeepSeek 网页会话 | Markdown 首行 `From:` 指向受支持的会话 URL | [`references/web-chat.md`](references/web-chat.md) | `python3 -m src.ingestion.import_web_chat` |
| 个人 notes、外部 articles、显式 Claude document | 用户明确声明的 Markdown 文档来源 | [`references/markdown-documents.md`](references/markdown-documents.md) | `python3 -m src.ingestion.import_notes` 或 `python3 -m src.ingestion.import_articles` |

如果文件外观与声明类型冲突，先按对应来源参考文件的识别规则判断；仍不确定时请求用户确认。

## 4. 共同导入流程

1. 固定本批显式文件范围；混合来源在同一次调用中使用 `python3 -m src.ingestion.import_batch`，按类型重复传入 `--claude-include`、`--claude-document-include`、`--codex-include`、`--web-chat-include`、`--note-include` 或 `--article-include`。单一来源使用对应 importer 的 `--include`；Codex 的 session ID 在单一来源命令用 `--session-id`，在混合批次用 `--codex-session-id`，且必须同时指定对应的 `--include`。只在用户要求预览时加 importer 的 `--dry-run`；普通正式调用内部也会完整准备并检查。
2. 准备阶段核对发现数、来源身份、hash、locator、资源、审核处置及已有来源；分析结果先定向写入 inventory 和 review queue。`--dry-run` 只返回预览，不写辅助状态或正式产物。
3. 未阻断时确认 `discovered = imported + unchanged + skipped`；整批阻断后实际 `imported=0`，改按 `discovered = deferred_by_batch + unchanged + skipped` 核对，`deferred_by_batch` 表示原计划导入数。按命中的来源参考文件解释 skip/review 原因，不把某一 provider 的状态套用到其他来源。
4. 通用的非审核 skip 包括 `unchanged`、`exact_duplicate` 和确定的 `no_final`；来源参考文件明确列出的 deferred/excluded 状态也不进入 review queue。
5. 任何 review 状态、输出路径冲突、资源解析失败、无法确认的语料边界或无法安全处理的敏感信息都会阻止整批正式导入；可按既有规则脱敏的内容记录脱敏计数。已导入来源重新分析失败时保留旧正文和 manifest，在 inventory/queue 标出新问题。
   原文、附件或处置记录若在准备后改变，命令拒绝提交并要求重新分析；这种并发变化不保证生成新的审核队列项。
6. 仅整批通过时提交 `sources/`、manifest 和 ingest log；manifest 只登记成功接受的来源。核对实际写入与报告后，整理发现数、导入数、跳过数、review 原因、资产变化和未执行的检查；本次请求未包含构建时，汇报结果并提示是否另行构建本地检索索引；已包含构建时，进入下一节并在构建后统一交付。

### 用户明确要求本地构建或重建检索索引时

- 在本批次导入或删除及规定验证完成后，从仓库根目录运行 `python3 -m src.retrieval.build_lexical_index --publish`。若本批次仍有阻断项，先报告，不能用构建掩盖未完成的来源操作。
- 该命令校验并投影 manifest 中 ready 的标准化来源，在 `meta/corpus/<index_id>/` 中构建或复用且验证检索索引，随后发布为 `current`；发布时将原 `current` 保留为 `previous`。失败时报告原因，不手工修补 `sources/` 或派生状态。
- 交付时报告命令返回的索引版本 ID、`reused`、`published`、`previous_index_id` 和 source/item 数量；`published=false` 可能表示目标索引已经是 `current`。提示运行中的 stdio 客户端重连后才能使用新索引；云端 release 构建与部署脚本不属于本地构建步骤。

### 一次性删除标准化来源

- 删除必须用 `python3 -m src.maintenance.remove_source <source-id> --dry-run` 按 manifest `source_id` 精确预览，不接受调用方传入的任意文件路径。
- 确认后去掉 `--dry-run`；命令只删除 ready 的 conversation、note 或 article Markdown、manifest 明确登记的附件和该 manifest 记录，不删除 raw source。
- 这是一次性删除；raw source 仍在时，后续 importer 可重新生成它。
- 存在 `duplicate_of`、`fork_parent_source_id`、共享附件、缺失文件、ID 不匹配或路径越界时停止，不自动级联删除或修复。
- 删除成功后报告实际删除的标准化文件、登记附件、manifest 记录和 ingest log 事件。用户未明确要求构建时，询问是否另行构建并发布本地检索索引；在此之前既有 `current` 和运行中的客户端仍使用旧索引。用户已明确要求构建时按上节直接构建并发布，并提示运行中的客户端重连。

## 5. 验证与派生状态

- 单纯导入时，正式调用会准备整批并更新辅助分析状态；若有 review 阻断，不提交任何本批正式来源。按需用 `--dry-run` 预览，不作为必经步骤。
- 对新增或变化的标准化来源执行定向 secrets scan；`scan_secrets.py` 的显式路径参数必须是文件，不能只传目录。全量导入、全量 dry-run、完整测试集、`check_corpus.py` 或全库 secrets scan 必须先向用户说明原因和范围，由用户决定是否执行。
- importer 在正式写入前定向更新 inventory 和 review queue；manifest 由 importer、`remove_source` 及受控状态维护命令按各自职责更新。ingest log 仅记录 `sources/` 中实际发生的文件变化，包括受控删除；单纯 review 或状态更新不追加来源文件变更事件。
- 修改 importer 或维护组件时，运行覆盖新增功能与受影响旧行为的定向测试；文档分层修改至少执行链接存在性检查和 `git diff --check`。

### 辅助状态的生成与定向复核

| 路径 | 职责与写入边界 |
| --- | --- |
| `meta/source-inventory.json` | 原始输入的分析快照，记录本批解析状态、hash、locator 和原因；正式 importer 在提交前按显式文件定向更新，独立盘点命令可按授权范围重建。它不是导入成功账本，提交后不必回填 `imported`。 |
| `meta/source-review-queue.md` | 从 inventory 的 review 单元及尚未被新分析覆盖的旧 manifest review 记录生成；正式 importer 在提交前定向重建，不手工编辑。 |
| `meta/source-review-resolutions.json` | 持久保存用户明确决定的处置；不会因导入、删除或队列重建自动清除，后续导入仅在身份、hash、locator 和问题边界匹配时复用。 |
| `meta/manifest.json` | 已接受来源的正式登记；importer 只新增或更新成功接受的来源，`remove_source` 删除指定记录，状态维护命令仅修改已登记来源。未导入来源的 review、deferred、excluded 和 duplicate 留在辅助状态。 |
| `meta/ingest.log` | 对 `sources/` 中文件实际新增、更新或删除的追加记录；不作为输入分析或人工处置记录。 |
| `meta/corpus/` | 可重建的检索索引产物，不参与导入前审核；来源导入或删除不会自动更新既有索引。 |

历史遗留的非 ready manifest 记录只在本批成功提交时按选中原文清理；未选范围不会因局部导入或删除被顺带迁移。

1. 进入任务时记录本批准备导入或重新审核的原始文件相对路径。importer、inventory 和 review queue 必须使用同一批文件；不得借单一来源导入扫描其他来源，或把同目录内未选择的文件带入派生状态。
2. 正式 importer 复用本次准备结果更新 inventory/queue，无需导入后重复运行独立盘点命令。即使文件被 review 阻断、尚未生成标准化来源，也要记录其 inventory 状态和审核项；删除命令不自动更新这两份导入前辅助文件，后续再次导入时按所选范围重新分析。辅助状态不作为已导入来源继续有效的条件。
3. 单独复核已有辅助快照时，inventory 的 `--provider` 使用 `claude-export`、`web-chat`、`codex`、`notes` 或 `articles`，与 importer 重复传入相同的 `--include <相对路径>`；review queue 对每个相同文件传入 `--source-path <绝对路径>`。两个命令的定向模式分别要求目标 inventory、queue 已存在；缺失时不得用全量扫描冒充局部更新。Codex fork 必须把解析所需的父会话文件一并列为本批输入。

   ```bash
   python3 -m src.ingestion.build_source_inventory \
     --provider <source-provider> --<provider>-input <absolute-raw-root> --include <relative-raw-path>
   python3 -m src.maintenance.rebuild_review_queue \
     --source-path <absolute-raw-path>
   ```

4. 用户对 review 项给出明确处置后，按第 2 节约束写入与该文件身份、hash、locator 和问题计数绑定的 `meta/source-review-resolutions.json`；随后对同一文件重新执行 importer。只有 resolution 与当前原文完全匹配且 importer 不再返回 review 时，queue 对应项才应消失。resolution 保留以供再次导入复用。
5. 核对本批文件的 hash、locator、解析状态和 `discovered = retained + skipped`；同时确认 inventory 中未选择的单元及 queue 中未选择的审核项保持不变。同步失败时不得宣称整批处理完成。
6. 只有用户明确要求重新盘点全部已知输入时，才不带 `--provider` 和 `--include`、显式给出全部选中来源的输入目录后运行 `build_source_inventory`，随后不带 `--source-path` 全量重建 queue。不要把其他输入参数指向不存在的目录来模拟局部更新。

其他全量检查经用户授权时可运行：

```bash
python3 -m src.maintenance.check_corpus
python3 -m src.maintenance.scan_secrets
venv/bin/python -m pytest
```

## 6. 通用人工审核边界

- `meta/source-review-resolutions.json` 只记录用户明确确认的处置；AI 不自行新增、扩大或推断处置范围。
- 已记录处置的 locator、hash 或边界不再匹配时必须重新进入 review，不能继续自动导入。
- 不因为内容“看起来完整”或“足够可读”而绕过 provider 参考文件中的完整性和资源要求。

## 7. 记录与交付

- 实质性实现、数据重建和验证结果按数据工作区 `AGENTS.md` 指定的位置记录；个人记录保留在私有侧，不写入公开代码目录。未指定记录位置时只交付结果，不自行创建日志目录。
- 交付时说明修改的文件、实际执行的命令、检查结果、review 阻断项和仍需用户决定的事项。
- 未经用户明确要求，不删除原始来源或已导入语料。
- 用户要求提交来源数据变更时，核对本批受控命令实际修改的正式产物及辅助状态，并确认暂存范围；导入已在提交前更新辅助状态，删除无需为提交额外全量盘点。按仓库 `AGENTS.md` 的 commit message 格式提交。
