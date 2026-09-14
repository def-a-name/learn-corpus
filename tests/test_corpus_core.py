from __future__ import annotations

import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.corpus.core import (  # noqa: E402
    clean_message,
    derive_title,
    format_line_locator,
    has_substantive_exchange,
    is_exact_meaningless_exchange,
    is_noise_message,
    is_trivial_session,
    parse_frontmatter,
    parse_line_locator,
    redact_secrets,
    yaml_document,
)


class TestCorpusCore:
    def test_line_locator_round_trip(self) -> None:
        locator = format_line_locator("synthetic-export.md#Session:7", 120, 145)
        assert locator == "synthetic-export.md#Session:7@L120-L145"
        assert parse_line_locator(locator) == ("synthetic-export.md#Session:7", 120, 145)
        assert parse_line_locator("synthetic-export.md#Session:7") == (
            "synthetic-export.md#Session:7",
            None,
            None,
        )

    def test_clean_message_removes_internal_context(self) -> None:
        text = "<environment_context>synthetic</environment_context>\n<local-command-stdout>fixture noise</local-command-stdout>\n合成消息  "
        assert clean_message(text) == "合成消息"

    def test_trivial_session(self) -> None:
        assert is_trivial_session([{"role": "user", "text": "hello"}, {"role": "assistant", "text": "你好"}])
        assert not is_trivial_session([{"role": "user", "text": "虚构遥测探针为什么离线"}])

    def test_exact_meaningless_exchange_is_strict(self) -> None:
        assert is_exact_meaningless_exchange("你好！", "你好。需要我帮你检查代码还是整理文档？")
        assert is_exact_meaningless_exchange(
                "hello",
                "Hello! 👋 我看到你刚登录成功。有什么可以帮你的吗？",
            )
        assert is_exact_meaningless_exchange(
                "hello", "Your account does not have access to Claude Code. Please run /login."
            )
        assert is_exact_meaningless_exchange("hello", "You've hit your limit · resets 8pm")
        assert is_exact_meaningless_exchange("测试", "You've hit your limit · resets 8pm")
        assert is_exact_meaningless_exchange("test", "Test passed.")
        assert not is_exact_meaningless_exchange("你好，请检查虚构遥测探针", "好的")
        assert not is_exact_meaningless_exchange("你好", "虚构遥测探针需要增加校验帧。")
        assert not is_exact_meaningless_exchange("可以", "已执行。")

    def test_noise_and_title_selection(self) -> None:
        noise = '<?xml version="1.0"?><request xsi:noNamespaceSchemaLocation="juice_schema.xsd">Juice number</request>'
        assert is_noise_message(noise)
        title = derive_title(
            "codex",
            [
                {"role": "user", "text": "你好"},
                {"role": "assistant", "text": "你好"},
                {"role": "user", "text": "如何校准虚构的星图传感器"},
            ],
        )
        assert "如何校准虚构的星图传感器" in title

    def test_unusable_assistant_response(self) -> None:
        assert not has_substantive_exchange(
                [{"role": "assistant", "text": "Your account does not have access to Claude Code. Please run /login."}]
            )

    def test_redact_secrets(self) -> None:
        result, count = redact_secrets(
            "api_key=fake-test-key-123 password: fake-test-password Bearer fake-test-bearer-token"  # secret-scan: allow
        )
        assert count == 3
        assert "fake-test-password" not in result
        assert "fake-test-bearer-token" not in result

    def test_redact_json_credential_and_keep_placeholder_stable(self) -> None:
        result, count = redact_secrets(
            '{"token_info":{"access_token":"fake-test-access-token"}}'  # secret-scan: allow
        )
        second, second_count = redact_secrets(result)
        assert count == 1
        assert second_count == 0
        assert second == result
        assert '"access_token":"[REDACTED]"' in result

    def test_redact_chinese_credential_assignment(self) -> None:
        result, count = redact_secrets("用户名 synthetic-user，密码 SyntheticSecret123!")  # secret-scan: allow
        ordinary, ordinary_count = redact_secrets("密码 应该足够长")
        assert count == 1
        assert result == "用户名 synthetic-user，密码 [REDACTED]"
        assert ordinary_count == 0
        assert ordinary == "密码 应该足够长"

    def test_frontmatter_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "note.md"
            path.write_text(yaml_document({"title": "合成笔记", "tags": ["虚构数据"]}, "# 合成正文"), encoding="utf-8")
            metadata, body = parse_frontmatter(path)
        assert metadata["title"] == "合成笔记"
        assert metadata["tags"] == ["虚构数据"]
        assert body == "# 合成正文\n"
