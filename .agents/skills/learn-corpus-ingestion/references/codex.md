# Codex rollout 导入

本文件只规定 Codex rollout JSONL 的来源特有规则。执行时必须同时遵守上级 [`SKILL.md`](../SKILL.md) 的共同流程。

## 识别与范围

- 适用于 Codex `sessions/` 下的 rollout `.jsonl`，或用户明确给出的 Codex session ID。
- 单一来源命令给出 session ID 时用 `--session-id` 限定目标；混合批次改用 `--codex-session-id`。多个 ID 分别重复参数，并用 `--include` 或 `--codex-include` 明确原始文件范围。
- fork 判定需要父 rollout 时，把父文件也显式列入本批输入，不为解析单个目标扫描全部 sessions。

## 语料边界

- 只保留 main session、已完成 task、显式 human user 和 `phase=final_answer` 的 assistant response。
- developer/system 指令、tool call/result、reasoning、commentary、runtime 噪声和 subagent 内容不进入正文，只保留必要统计。
- Codex 自动注入且完整匹配 `<skill>...</skill>` 信封的内容属于 runtime，不计为 human user。`<send_user_message_question_reply>` 中格式有效的问题和用户答案合并到同一 task 已有的用户内容，保留其语义和覆盖到回复行的 locator。
- 已完成 task 中至少一条 human user 位于唯一 assistant final 之前时，按原始顺序聚合为一个 exchange 的 human item；多条普通 user message 同时保留逐条 locator。没有 final、存在多个 final、final 后出现 user 或 task 未正常完成时不猜测配对，仍按既有 deferred、excluded 或 review 规则处理。
- 一个或多个连续 task 含至少一条 human user、没有 assistant final，且紧邻的下一 task 正常产生唯一 final 时，把整段 human user 按原始顺序恢复为下一 task 的一个 exchange；前序 task 可以由 `turn_aborted` 或无 final 的 `task_complete` 结束，判定不依赖“继续”等具体措辞，并保存跨 turn 聚合 locator、逐条 locator 和恢复计数。中间出现无法归入该连续链的 task、前序 task 已有 final 或最后一个 task 无法正常配对时不恢复。
- fork 与父会话存在完全相同前缀时，只导入 fork 新增的 delta；没有新增 exchange 的 fork 属于非审核 skip。
- fork 与父会话没有共同的完整 exchange 前缀，但子会话第一个 exchange 来自上述连续 incomplete task 恢复时，保留 fork 父来源及 hash，把子会话中可验证的 exchange 作为完整分叉 delta，并显式记录无共同前缀；其他零前缀 fork 仍进入 `fork_prefix_mismatch_review`。

## deferred、excluded 与 review

- 只有文件末尾确实存在未结束 task 才是 `active_session_deferred`。已被后续 `task_started` 取代的历史残缺 turn 不能伪装成 active。
- subagent-only、no-human、no-final、trivial 和无新增 delta 的 fork 属于非审核 deferred/excluded。
- rollout 同时包含无效 JSONL 和未结束 turn 时，`invalid_jsonl_review` 优先于 `active_session_deferred`。
- fork 的父会话缺失、不可用、前缀不匹配或尚未标准化时进入相应 review；父会话自身仍 deferred 时使用 `fork_parent_deferred`，准备后父文件发生变化时拒绝提交并重新分析，不能猜测 delta。
- 已有处置的原始行、hash、locator 或残缺 turn 边界不再完全匹配时转为 `review_resolution_mismatch_review`。

## 无效 JSONL 的人工处置

只有用户明确审核并决定省略损坏 turn 后，才能在 `meta/source-review-resolutions.json` 记录无效行号/hash 与残缺 turn 边界：

- importer 与 inventory 读取同一处置记录；`check_corpus` 核对已接受来源及其保存的处置 provenance，不要求外部 resolution 文件之后一直保持不变。
- 处置只允许省略已审核的损坏行及其所在残缺 turn；后续仍须依靠显式 human user、`phase=final_answer` 和 task 边界生成 exchange。
- 不修改原始 rollout，不手工修补生成的 conversation source。
- 处置后对相同显式 session 范围正式导入并执行定向 secrets scan；需要预览时可先加 `--dry-run`。
