"""从统一配置选择数据工作区，不读取原始输入或打开索引。"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
from typing import Mapping

from src.config import CONFIG_FIELDS, read_config_object
from src.service.errors import HTTPFailure


CODE_ROOT = Path(__file__).resolve().parents[2]
CONFIG_ENV = "LEARN_CORPUS_CONFIG"
DATA_ROOT_ENV = "LEARN_CORPUS_DATA_ROOT"


def find_config_path(code_root: Path, environ: Mapping[str, str]) -> Path | None:
    """优先使用明确选择的配置；submodule 默认使用所属私有仓库的配置。"""

    if CONFIG_ENV in environ:
        raw = environ[CONFIG_ENV]
        if not raw.strip():
            raise ValueError(f"{CONFIG_ENV} must be a nonempty file path")
        path = Path(raw).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"{CONFIG_ENV} must point to an existing configuration file")
        return path
    candidates = []
    if (code_root / ".git").is_file():
        try:
            result = subprocess.run(
                ["git", "-C", str(code_root), "rev-parse", "--show-superproject-working-tree"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                candidates.append(Path(result.stdout.strip()) / "config/config.json")
        except (OSError, subprocess.TimeoutExpired):
            pass
    candidates.append(code_root / "config/config.json")
    for path in candidates:
        if path.exists() or path.is_symlink():
            if not path.is_file():
                raise ValueError("workspace configuration must be a readable file")
            return path.resolve()
    return None


def workspace_data_root(value: object, config_path: Path, code_root: Path = CODE_ROOT) -> Path:
    """配置路径相对配置文件解析；省略数据目录时使用单仓库布局。"""

    if not isinstance(value, dict):
        raise ValueError("workspace configuration must be an object")
    unknown = sorted(value.keys() - {"data_root"})
    if unknown:
        raise ValueError(f"workspace configuration contains unknown fields: {', '.join(unknown)}")
    if "data_root" not in value:
        return code_root
    raw = value["data_root"]
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("workspace data_root must be a nonempty directory path")
    candidate = Path(raw).expanduser()
    return (candidate if candidate.is_absolute() else config_path.resolve().parent / candidate).resolve()


def resolve_workspace(code_root: Path = CODE_ROOT) -> tuple[Path, bool]:
    """临时数据覆盖优先；明确的目录选择必须有效，不能回落到其他位置。"""

    try:
        raw = os.environ.get(DATA_ROOT_ENV)
        if raw is not None:
            if not raw.strip():
                raise ValueError(f"{DATA_ROOT_ENV} must be a nonempty directory path")
            root, explicit = Path(raw).expanduser().resolve(), True
        else:
            path = find_config_path(code_root, os.environ)
            if path is None:
                return code_root, False
            value = read_config_object(path)
            unknown = sorted(value.keys() - CONFIG_FIELDS)
            if unknown:
                raise ValueError(f"configuration contains unknown fields: {', '.join(unknown)}")
            root = workspace_data_root(value.get("workspace", {}), path, code_root)
            explicit = "data_root" in value.get("workspace", {})
        if not root.is_dir():
            raise ValueError("workspace data root must point to an existing directory")
        return root, explicit
    except HTTPFailure as exc:
        raise SystemExit("cannot load workspace configuration: invalid JSON") from exc
    except (OSError, ValueError) as exc:
        if isinstance(exc, OSError):
            raise SystemExit("cannot load workspace configuration: file cannot be read") from exc
        raise SystemExit(f"cannot load workspace configuration: {exc}") from exc
