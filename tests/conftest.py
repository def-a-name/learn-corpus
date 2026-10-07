"""让通用测试使用空的虚构配置，避免加载开发机的个人工作区。"""

import os
from pathlib import Path
import tempfile


_config_directory = tempfile.TemporaryDirectory(prefix="synthetic-test-config-")
_config_path = Path(_config_directory.name) / "config.json"
_config_path.write_text("{}")
_previous_config = os.environ.get("LEARN_CORPUS_CONFIG")
os.environ["LEARN_CORPUS_CONFIG"] = str(_config_path)


def pytest_unconfigure(config):
    """测试结束时恢复调用环境并清理虚构配置。"""

    if _previous_config is None:
        os.environ.pop("LEARN_CORPUS_CONFIG", None)
    else:
        os.environ["LEARN_CORPUS_CONFIG"] = _previous_config
    _config_directory.cleanup()
