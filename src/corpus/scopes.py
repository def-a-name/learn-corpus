"""定义标准化来源类型、目录和依赖字段。"""

from __future__ import annotations

from pathlib import PurePosixPath


SOURCE_ROOTS = {
    "conversation": PurePosixPath("sources/conversations"),
    "note": PurePosixPath("sources/notes"),
    "article": PurePosixPath("sources/articles"),
}
SOURCE_ASSET_ROOT = PurePosixPath("sources/assets")
SOURCE_DEPENDENCY_FIELDS = ("duplicate_of", "fork_parent_source_id")
