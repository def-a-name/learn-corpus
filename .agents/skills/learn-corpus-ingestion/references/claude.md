# Claude 会话导入

本文件只规定 Claude Markdown 会话的来源特有规则。执行时必须同时遵守上级 [`SKILL.md`](../SKILL.md) 的共同流程。

## 识别与命令

- 适用于 Claude 导出的 Markdown session；正式命令为 `python3 -m src.ingestion.import_claude --kind session --include <relative.md>`，同批多个文件可重复 `--include`。
- 只导入主对话中可恢复的 human user 与对应 assistant final response。
- Claude 明确 document 或普通文档不是 session，改走 [`markdown-documents.md`](markdown-documents.md)，不要用 `import_claude --kind document` 批量兜底。

## 语料边界

- 主会话可使用导出结构和现有启发式识别最终回答；provenance 必须标记为 `summary`、`export-visible`、`heuristic`。
- 导出不提供可靠 provider session ID 时，不得伪造。
- subagent 消息、tool use/result、thinking/reasoning、runtime/system 噪声不进入正文；相关数量只写入 provenance 统计。
- 没有对应 assistant final response 的 user turn 计入 unpaired/omitted 统计，不能与后一个回答误配。

## 非审核跳过与审核边界

- 可确定的 subagent-only、runtime-only、trivial 或 `no_final` 来源属于非审核 skip。
- 角色边界、主会话归属或最终回答无法可靠判断时进入 review，不得用文档 importer 降级导入。
- 不手工修改生成的 conversation source。
