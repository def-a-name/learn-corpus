# Markdown 文档导入

本文件规定个人 notes、外部 articles 和用户明确声明的 Claude document。执行时必须同时遵守上级 [`SKILL.md`](../SKILL.md) 的共同流程。

## 路由与命令

| 文档类型 | 命令 | 固定归属 |
| --- | --- | --- |
| 个人 note | `python3 -m src.ingestion.import_notes` | `sources/notes/`，personal layer |
| 外部 article | `python3 -m src.ingestion.import_articles` | `sources/articles/`，external layer |
| 显式 Claude document | `python3 -m src.ingestion.import_notes --input <claude-exec-docs> --origin claude-export --include <relative.md>` | `sources/notes/`，note layer |

- 用户明确指定单个 note 或 article 时，必须把用户指定的目录作为 `--input`，用一个 `--include <relative.md>` 精确导入该文件；不得省略 `--include` 或以 `--limit 1` 代替。
- 用户只提供绝对文件路径时，以文件的直接父目录作为 `--input`，以文件名作为 `--include`；同一来源后续应沿用相同 input root，避免改变 source identity。
- 需要限定多个明确文件时，可重复使用 `--include`；导入器不会发现未指定的 Markdown。
- 不要把未声明类型的 Markdown 批量导入，也不要使用 Claude `--kind document` 兜底未知 Markdown。类型无法判定时询问用户。

## 内容、去重与资源

- 原始 Markdown 保持只读；标准化内容由 importer 生成。
- exact content fingerprint 只在同一 layer 内判定精确重复，`exact_duplicate` 的 canonical 选择必须稳定。
- 受支持的本地资源复制到 `sources/assets/` 并重写标准化链接；不做 OCR。
- 本地资源缺失、路径逃逸或类型不受支持时进入 review。远程 URL 只记录，不自动下载。

## provenance 与审核

- article 的作者、标题、发布日期、原始 URL 等只使用来源中存在或用户明确提供的信息；未知字段不得编造。
- 元数据缺失本身不必进入 review，除非影响来源身份、内容边界、安全或去重判定。
- 文档与会话边界混杂、来源类型冲突或无法安全解析时停止导入并请求用户确认。
