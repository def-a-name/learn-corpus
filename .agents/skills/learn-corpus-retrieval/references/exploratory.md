# Exploratory retrieval

探索型用于同一 source 的时间线、冲突或多阶段问题，跨 source 比较与综合，或无法预判证据分布的问题。

## 拆分问题

默认建立 1～3 个真正需要来源支持的 evidence questions，按问题要求的事实侧面、阶段、角色或来源拆分。例如：初始方案、后续变化、用户是否采纳。只有独立证据目标不能作为现有问题下的 claim 或 gap 管理时才超过三个，并在 evidence plan 中记录一句 `question_count_reason`；额外问题不增加任务预算。不要为了凑数、回答提纲或普通细节创建问题，也不能为了维持默认数量而静默遗漏独立目标。

不要在开始时为所有 evidence questions 各自发出 search。首次 search 承担 discovery：以共同主题为发现目标；没有共同主题时，从优先级最高的 evidence question 开始。它只用于发现相关材料、来源用词和证据分布，snippet 仍不能支持最终事实。先根据候选和已读正文判断实际 gap，再为仍未覆盖的 evidence question 进行 refinement。

初始 discovery query 遵守：

- 使用能识别主题或对象的最小充分 anchor 集，通常只有 1 个；只有主题本身存在明显歧义时才增加第 2 个消歧 anchor。
- 不加入预期结论、原因、最终状态、是否采纳等答案侧词语，除非用户提供了需要原样查找的明确表述。
- 同一发现目标或 evidence question 的同义、缩写、旧称或中英文 variants 可以放在一个 queries 数组中用 RRF 合并。
- variants 必须是并列替代；不把宽 query 和在其后追加 anchors 的窄 query 放在同一数组。宽窄改写根据前一次结果分开执行。
- 不同阶段、来源或事实侧面不能放进同一个 RRF 列表。
- `limit` 取 12～20。为 `read_bundle` 留出累计 evidence token 空间，不默认请求服务最大响应预算。
- 已知跨 scope 时优先分 scope 搜索；每次 search 按主文件的[查询语言规则](../SKILL.md#选择查询语言)选择表达，不根据 scope 猜语言，也不因 exploratory 模式而每次都使用双语。

## 多样化选择

按 evidence question、`source_title`、snippet 相关性和 `index_id + bundle_key` 去重与取样。根据当前 evidence question、所需 role/evidence_role 和 snippet 选择最可能包含答案的候选；相关性相近时才用 rank 作次级排序。避免同一 `source_title` 的重复前言或相邻 parts 占满读取预算，但不把同名来源当作已证明是同一 source；不要求为了凑齐角色重复选择同一 bundle。

只有问题确实需要前后变化，且有其他充分信息能确定结果来自同一 conversation 时，才把 `turn_index` 当作可选顺序线索。`source_title` 相同本身不足以确立这一点；不能据此导航未召回轮次或假定较大轮次是最终决定。

找到第一条看似合理的证据不能停止。先检查每个 evidence question 的 coverage、冲突双方、所需角色和来源，再决定是否继续。

## Refinement

只补明确 gap：未覆盖的正文事实、来源、用户确认依据、阶段或语言。新增候选若只重复已见 bundle，不算进展，也不继续扩大已经充分侧面的候选数。

每次 search 后先判断结果类型，每次 refinement 只优先改变一个维度：

| 结果类型 | 下一步 |
|---|---|
| 零结果，或结果极少且没有合理候选 | 先去掉最弱、最可能属于答案假设的 anchor；不在原 query 上继续追加 anchors |
| 候选很多但含义混杂 | 增加一个问题侧消歧 anchor，或收紧 scope |
| 候选集中在同一 bundle 或同类材料 | 改用当前 gap 所需的阶段、角色、来源类型或语言线索，不只提高 limit |
| snippet 似乎相关，但 read 后确认含义不符 | 使用已读正文暴露的准确术语替换错误 anchor，不把新词不断追加到原 query |
| 一个阶段已覆盖，但缺用户确认、后续变化或指定来源 | 为缺失的角色、阶段或 scope 建立独立 refinement，不重读已覆盖侧面 |
| `is_truncated=true` | 只说明候选受响应预算省略；不因此改变 query 语义或判定无证据 |

实际发生 refinement 时，只在当前上下文额外保留账本无法恢复的调整类型和一句原因，供后续诊断摘要使用：去掉弱 anchor 记为 `drop_weak_anchor`，增加消歧记为 `add_disambiguator`，替换来源用词记为 `replace_source_term`，改变阶段、角色、scope 或语言分别记为 `change_stage`、`change_role`、`change_scope`、`change_language`。已 admission 的前后 queries、结果计数和 usage 由服务端账本恢复，不在上下文复制；没有发生调整时不创建记录。相关候选因预算、优先级或停止条件未读时，只保留 item ID、对应 evidence question 和一句未读原因，不复制 snippet。

否定性问题按主文件的[否定证据规则](../SKILL.md#处理否定结论与无支持结果)执行。先检索对象与行为本身，再根据 gap 搜索明确否定或后续阶段；空结果、条目沉默和助手判断均不能自动升级为用户未做、未采纳或现实中不存在。

只要仍有必要 evidence question 未覆盖，并且存在相关未读候选或合理 refinement，就不得继续读取已覆盖问题的补充材料或可选背景；先把剩余 search/read 预算用于最可能补足必要缺口的操作。

当各 evidence question 已覆盖、冲突双方齐备、剩余 gap 已无合理 query，或触发共同预算/停止条件时结束。空结果只有在相关 scopes 和必要语言 variants 已尝试、且不存在截断或其他关键 gap 后，才可能形成 `no_support_found`；最终仍只能输出带检索索引和检索范围限定的 `no_evidence`。
