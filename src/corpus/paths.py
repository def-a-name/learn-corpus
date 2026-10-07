"""分离代码位置与标准化 corpus 的数据工作目录。"""

from __future__ import annotations

from pathlib import Path

from src.corpus.workspace import CODE_ROOT, DATA_ROOT_ENV, resolve_workspace


def resolve_data_root() -> Path:
    """从统一配置或临时覆盖解析标准化资料根目录。"""

    return resolve_workspace()[0]


DATA_ROOT, DATA_ROOT_EXPLICIT = resolve_workspace()
# 保留内部历史名称，既有调用和默认布局仍使用同一个数据根目录。
REPO_ROOT = DATA_ROOT
MANIFEST_PATH = REPO_ROOT / "meta" / "manifest.json"


def validate_data_path(path: Path) -> None:
    """显式双仓库模式拒绝越界或写入嵌套的代码目录。"""

    if not DATA_ROOT_EXPLICIT:
        return
    resolved = path.resolve()
    if not resolved.is_relative_to(DATA_ROOT):
        raise ValueError("data output path is outside the selected data root")
    if CODE_ROOT != DATA_ROOT and CODE_ROOT.is_relative_to(DATA_ROOT) and resolved.is_relative_to(CODE_ROOT):
        raise ValueError("data output path must not target the code directory")


def relative_to_repo(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path.resolve())
