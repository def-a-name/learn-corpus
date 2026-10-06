# Web-chat Markdown 导入

本文件只规定 Chrome 插件导出的 ChatGPT / DeepSeek Markdown 会话。执行时必须同时遵守上级 [`SKILL.md`](../SKILL.md) 的共同流程。

## 识别与完整性

- 以首行 `From:` URL 识别 provider：ChatGPT 必须是受支持的 `/c/<session>` 会话 URL 或 `/g/<project>/c/<session>` 项目内会话 URL；DeepSeek 必须是受支持的 `/a/chat/s/` 分享 URL。
- 只保留导出可见的 `you asked` 与对应 provider response；provenance 标记为 `summary`、`export-visible`、`heuristic`，不能标记为 raw completeness。
- provider、会话 URL、角色标记或正文边界无法唯一判断时进入 review。

## 配对规则

- 按导出顺序一一配对 user block 与紧随其后的 provider response。
- 连续 user block 中没有 response 的前一个 turn 计入 `omitted_unpaired_user_count`，不能把后一个回答误配给它。
- 孤立 response、无法解析的时间或相互矛盾的角色结构进入 review。

## 引用与资源

- DeepSeek `[citation:n]` 必须能解析到 URL 映射；缺失时进入 `citation_targets_missing_review`。
- ChatGPT `sandbox:` 文件先在导出 Markdown 同目录和其 `files/` 子目录解析；无法解析时进入 `sandbox_asset_missing_review`。
- 本地图片或其他本地资源必须存在、位于允许范围内且类型受支持，否则进入对应 review。
- 已解析的本地图片和 sandbox 文件复制到 `sources/assets/conversation/<provider>/<source-id>/`，只重写标准化来源中的链接，不修改原始 Markdown。

## 人工处置边界

- 未经用户明确处置，不省略缺失引用或资源，也不把插件 Markdown 标记为完整原始记录。
- 不提供可复用的 web-chat 缺失资源自动放行规则；恢复的 sandbox 文件应作为外部输入放回导出目录后重新运行 importer。
- 用户明确确认某份 DeepSeek 导出的无目标 citation 不影响正文完整性时，可在 `meta/source-review-resolutions.json` 的 `web_chat_sessions` 中记录 `omit_unresolved_citation_markers`；处置必须绑定 source ID、原文 hash、locator 和 citation 计数，任一不匹配时重新进入 review。
- 已匹配处置的 citation 标记从标准化正文中省略，并在 front matter 和 manifest 记录处置与省略计数。
- 不手工修补生成的 conversation source。
