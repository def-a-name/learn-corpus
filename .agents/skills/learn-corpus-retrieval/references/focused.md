# Focused retrieval

聚焦型用于答案预计收敛到一个 logical exchange、消息或文档 section，或同一 source 中紧邻的少量事实。

## 初始搜索

- 建立一个 evidence question。
- 初始围绕一个事实目标生成 query，优先使用问题中的 2～3 个高区分 anchors；原句或明确标识通常一条即可，来源语言未知的概念查询按主文件的[查询语言规则](../SKILL.md#选择查询语言)默认使用中英文各一条。
- `limit` 取 5～8。为 `read_bundle` 留出累计 evidence token 空间，不默认请求服务最大响应预算。
- 同一意图确有全称/缩写或旧称歧义时，可在同一次 search 中增加必要 variant；语言变体遵循共同规则，不因 focused 模式而省略。
- search/read 均不传 `index_id`，由服务端固定并自动注入。

不要从预期答案偷取未在问题中出现的命令、函数名、数值或结论。问题清楚时直接使用其实体、动作和约束。

## 选择与读取

按 `index_id + bundle_key` 折叠结果，根据 evidence question、所需 role/evidence_role 和 snippet 选择最可能包含答案的 bundle 与 seed；相关性相近时才用 rank 作次级排序。命中 conversation 任一角色或文档 section 任一 part 后，先用命中 seed 读取有界窗口，再根据返回正文判断是否需要改写 query；不要因为答案 part 没直接进入排名而立即继续搜索。

已读正文直接覆盖必要事实时停止，不要求窗口 complete 或多个来源；追问按主文件的任务复用规则先检查已有正文。

## 未收敛时

若首轮为空或正文未覆盖问题，只针对明确 gap 依次考虑：

1. 去掉最弱 anchor；
2. 使用问题侧的同义或跨语言概念表达；
3. 修正 scope；
4. 使用已读正文暴露、且服务于原问题 gap 的 exact entity。

通常只进行一次必要改写。若候选持续分散到多个 source 或阶段，或没有单一 logical unit 能闭合答案，显式重分类为 exploratory；读取对应参考文件。总 search 上限以任务返回的 `limits.search_calls` 为准，重分类不重置任何记录。
