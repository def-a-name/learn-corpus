"""分离代码位置与标准化 corpus 的数据工作目录。"""

from __future__ import annotations

from pathlib import Path
import os


CODE_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT_ENV = "LEARN_CORPUS_DATA_ROOT"


def resolve_data_root() -> Path:
    """显式工作目录必须存在；未设置时保持原有单仓库默认值。"""

    raw = os.environ.get(DATA_ROOT_ENV)
    if raw is None:
        return CODE_ROOT
    if not raw.strip():
        raise SystemExit(f"{DATA_ROOT_ENV} must be a nonempty directory path")
    root = Path(raw).expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"{DATA_ROOT_ENV} must point to an existing directory")
    if root != CODE_ROOT and root.is_relative_to(CODE_ROOT):
        raise SystemExit(f"{DATA_ROOT_ENV} must not be nested inside the code directory")
    return root


DATA_ROOT = resolve_data_root()
# 保留内部历史名称，既有调用和默认布局仍使用同一个数据根目录。
REPO_ROOT = DATA_ROOT
MANIFEST_PATH = REPO_ROOT / "meta" / "manifest.json"


def validate_data_path(path: Path) -> None:
    """显式双仓库模式拒绝越界或写入嵌套的代码目录。"""

    if DATA_ROOT_ENV not in os.environ:
        return
    resolved = path.resolve()
    if not resolved.is_relative_to(DATA_ROOT):
        raise ValueError("data output path is outside the selected data root")
    if CODE_ROOT != DATA_ROOT and resolved.is_relative_to(CODE_ROOT):
        raise ValueError("data output path must not target the code directory")


def relative_to_repo(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path.resolve())
