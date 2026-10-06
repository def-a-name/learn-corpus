# Execution ledger

服务端 ledger 维护一次检索任务的机械执行状态。它从原始 MCP 检索结果直接记录事实，不要求模型转写嵌套 JSON；保存引用所需的 `source_title` 与 `heading_path`，不保存正文、snippet、旧的条目 `title`、claim、coverage、conflict 或 gap。

## 生命周期

每个独立问题先调用：

```json
start_retrieval_task({})
```

按主文件的[追问与任务复用规则](../SKILL.md#追问与任务复用)判断任务归属，在当前上下文保存用户目标与返回 `task_id` 的对应关系。服务不会按 query 或 owner 自动选择最近任务，也无法判断新的自然语言请求是否属于旧任务。

账本数据库由服务管理，客户端不创建、读写或清理本地 ledger 文件，也不把账本当作跨任务的证据缓存。

## 每次 MCP 调用

search 必须传 task_id、queries 和 max_estimated_tokens；scopes 与 limit 按检索计划选择：

```json
search_sources({"task_id":"<task_id>","queries":["..."],"max_estimated_tokens":1200})
```

read 必须使用同一 task 的搜索候选，不传 `index_id`：

```json
read_bundle({"task_id":"<task_id>","seed_item_id":"<search item_id>","max_estimated_tokens":2400})
```

服务在执行 core 前原子登记 pending 和 cap，完成后从同一次内部结果记录 `index_id`、usage、bundle/item 关系、完整性状态以及 `source_title`、`heading_path`、role、evidence_role、path、locator。path 与 locator 不进入 MCP search/read 响应，只写入服务端结算副本。旧的条目 `title`、snippet 和正文不写入账本。客户端不调用 begin、complete、fail 或 ack 工具。

工具错误始终返回稳定的 code、message 和 request_id。query 校验可附 `error.details`，包含 `queries[n]`、固定 reason 与安全约束；任务预算、调用次数、阻断及响应预算错误可附余额、计数、阻断类别或固定原因。details 不回显 query 正文，也不替代 execution ledger；无 details 时不得推测内部错误。

每次成功 search/read 都返回 `execution`：task/call ID、状态、`index_id`、search/read 次数、已知 usage、预留、余额和 partial window 数。跨任务切回后确需继续 search/read，或需要执行过程摘要时调用；只用已有正文回答不要求查询账本：

```json
get_retrieval_task({"task_id":"<task_id>"})
```

它始终返回基于完整账本计算的 execution、limits、`calls_total` 与 `items_total`；调用明细只保留单次响应边界内较新的完整条目。不传 `item_ids` 时，`server_returned_items` 返回成功 read 的较新引用元数据；传入 1～20 个已读 `item_id` 时，按请求顺序只返回这些条目，`unavailable_item_ids` 显式列出账本中没有的 ID。`calls_truncated`、`items_truncated` 标记明细省略，`item_filter_applied` 标记是否启用过滤。它不返回正文、snippet、旧的条目 `title` 或完整 missing ID 列表，不分页，也不是结果重放工具；元数据不能替代当前上下文实际取得的正文。

## 摘要与完整来源定位

普通回答遵循主文件的[按证据回答](../SKILL.md#按证据回答)，不为展示简短引用额外查询账本。search/read 不返回 path 或 locator，不能从其他字段推导或拼接。

用户要求检索摘要、请求摘要或检查过程时，查询对应任务，默认输出有上限的诊断报告：

- **任务概况**从当前上下文给出 mode，从账本给出检索索引、实际 limits、search/read 次数、usage/余额，以及调用或条目明细是否截断；
- **异常与调整**列出调用失败、partial、响应截断、不可用 item，以及当前上下文保留的检索维度调整和原因。成功且没有改变判断的常规调用只合并计数；
- **证据状态**使用当前上下文中的 evidence ledger，按问题概括 coverage、当前结论、关键支持、qualification、conflict、核心 gap、相关未读候选及未读原因；evidence questions 超过三个时一并说明 `question_count_reason`；
- **核心来源定位**列出支撑结论、构成冲突或决定关键缺口的最小已读 item 集，并展示 `source_title`、`item_id`、role/evidence_role、必要的 `heading_path`、path 和 locator；账本调用能够可靠对应时再附相关 bundle 状态；
- **整体判断**给出 `answer_status` 与 `stop_reason`。尚在检索时明确写“尚未停止”。

诊断报告默认展示核心来源的 path 与 locator，不需要用户另行要求“完整定位”。逐字段原样复制，不解析、缩写、重建或拼接。首次未过滤查询若因 `items_truncated` 缺少核心 item，使用 `get_retrieval_task({"task_id":"...","item_ids":["..."]})` 补取；`unavailable_item_ids` 非空时明确定位缺口。默认不展开全部成功调用和全部已读 item；用户明确要求完整调试记录时，才在账本单次响应边界内展开完整明细。用户要求简版时仍保留影响结论的失败、核心 gap、`answer_status` 与 `stop_reason`。

否定性问题在证据状态中明确区分 `supported_negative`、`no_support_found` 和 `unresolved`。只有前者列出支持否定 claim 的已读 item；`no_support_found` 只概括检索索引、scopes 和已检索范围，`unresolved` 说明截断、partial、未读候选、预算或错误留下的具体 gap。不从账本中的空候选、调用次数或 item 元数据推导反向事实。

只有诊断报告涉及改动效果时，才对每项效果结论分别标注 `E3`、`E2`、`E1` 或 `E0`：变更后的实际响应、测试、日志或会话直接显示效果为 `E3`；用户或来源明确报告且保留归因为 `E2`；只有实现、配置或规则变化为 `E1`；尚未验证或有关键 gap 为 `E0`。等级在组织报告时依据已有证据归纳，不写入 evidence ledger，不指导检索、候选选择、预算或停止，也不出现在普通回答中。冲突另行标记，不通过降低或提高等级隐藏；分级不能替代正文支持、evidence role、qualification 或 gap。

evidence 摘要只给出条理清楚的证据状态，不重新大段引用正文，不输出完整内部推理，也不写回服务端账本。当前上下文不重复保存服务端可以恢复的完整 queries、调用列表、计数、usage、已读 item 元数据或定位；只简短保留 admission 前失败及安全错误详情、检索维度调整的类型与原因、相关候选未读原因，以及停止时仍存在的核心 gap。正文、evidence ledger 或这些必要事件已不在当前上下文时，明确相应判断无法恢复，不从账本元数据推导。账本明细截断与上下文证据缺失分别说明，不能互相替代。

用户只要求完整来源定位时，收集要引用的已读 `item_id`，使用 `get_retrieval_task({"task_id":"...","item_ids":["..."]})` 只取这些条目。从 `server_returned_items` 提取完整 `path` 与 `locator`，分别作为代码文本字段或表格列原样呈现；不解析或缩写 locator，不拼成 `path#locator` 链接，也不根据日期、UUID 或相邻 part 重建字段。`unavailable_item_ids` 非空时明确说明定位缺口，不改用未经过滤的整份明细猜测。

`calls_total`、`items_total` 是完整账本计数；`calls_truncated=true` 或 `items_truncated=true` 表示对应明细因单次响应边界有省略，返回数组保留较新的完整条目。需要的调用或引用未返回、或账本查询失败时，明确过程/定位缺口，不从截断数组推断某个 query 或 seed 从未尝试，不补造字段，也不为了取得摘要明细重新搜索或读取正文。账本引用列表只表示服务端返回记录，不能将其未在当前上下文读到的条目描述为答案依据。摘要中的证据充分性和停止原因仍来自上下文中的证据判断。

## 职责边界

服务端执行以下机械检查：

- search/read 调用上限及累计证据预算硬上限；
- 首次成功 search 固定检索索引，后续调用自动使用该检索索引；
- 规范化 query 与 scope 组合去重；
- read seed 必须来自已登记的 search 结果；
- 同一 seed 只允许尝试一次；同 bundle 的其他未尝试 seed 必须来自已登记的 search 结果；
- 只有成功 read 实际返回的 `items[]` 才能列为 server-returned items。

候选与证据语义、coverage、gap 和停止原因遵循主文件的[分开维护 execution 与 evidence](../SKILL.md#分开维护-execution-与-evidence)。账本中的 item 元数据不能替代正文；若正文已不在当前可用上下文中，不得仅凭账本声称它支持某个事实。

账本记录的是服务端执行和结算事实，不证明 host 已收到或模型已理解结果。提交后发送失败不退款、不自动重试或重放；没有 ACK 或 MRTR 收讫流程。
