from __future__ import annotations

import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.retrieval.lexical_query import (  # noqa: E402
    QueryValidationError,
    compile_lexical_query,
    query_dedupe_key,
)
from src.retrieval.text import normalize_index_text  # noqa: E402


class TestLexicalQuery:
    def test_cjk_anchors_compile_as_exact_character_phrases(self) -> None:
        compiled = compile_lexical_query("星图校准 异常")
        assert compiled.anchors == ("星图校准", "异常")
        assert compiled.match_expression == '"星 图 校 准" AND "异 常"'
        assert compiled.anchor_count == 2
        assert compiled.clause_count == 2

    def test_mixed_query_and_identifier_components(self) -> None:
        cases = {
            "Orion 星图校准": 'orion* AND "星 图 校 准"',
            "probe-7 异常": 'probe* AND 7* AND "异 常"',
            "SYN-0054": "syn* AND 0054*",
            "2026.4.10": "2026* AND 4* AND 10*",
            "astro::sensor 校准序": 'astro* AND sensor* AND "校 准 序"',
        }
        for query, expected in cases.items():
            assert compile_lexical_query(query).match_expression == expected

        identifier = compile_lexical_query("probe-7 异常")
        assert identifier.anchor_count == 2
        assert identifier.clause_count == 3

    def test_index_normalization_separates_cjk_and_keeps_latin(self) -> None:
        assert normalize_index_text("Orion校准 7.1\n异常") == \
            "Orion 校 准 7.1 异 常"
        assert normalize_index_text(None) == ""
        assert normalize_index_text("Cafe\u0301") == "Café"

    def test_nfc_and_dedupe_normalization(self) -> None:
        compiled = compile_lexical_query("  Café   异常  ")
        assert compiled.normalized_query == "Café   异常"
        assert compiled.dedupe_key == "café 异常"
        assert query_dedupe_key("Cafe\u0301 异常") == "café 异常"

    def test_anchor_count_is_enforced_before_component_splitting(self) -> None:
        assert compile_lexical_query("2026.4.10").anchor_count == 1
        assert compile_lexical_query("2026.4.10").clause_count == 3
        with pytest.raises(QueryValidationError, match="between 1 and 3 anchors"):
            compile_lexical_query("one two three four")

    def test_empty_and_non_searchable_anchor_are_rejected(self) -> None:
        for query in ("", "   ", "---", "..."):
            with pytest.raises(QueryValidationError):
                compile_lexical_query(query)

    def test_control_bidi_and_invalid_unicode_are_rejected(self) -> None:
        for query in ("python\nfts", "python\tfts", "python\x00fts", "python\u202efts", "\ud800"):
            with pytest.raises(QueryValidationError):
                compile_lexical_query(query)

    def test_raw_fts_syntax_and_column_filter_are_rejected(self) -> None:
        for query in (
            '"python sqlite"',
            "python*",
            "(python)",
            "{title}:python",
            "^python",
            "title:python",
            "python OR sqlite",
            "python not sqlite",
            "NEAR python",
        ):
            with pytest.raises(QueryValidationError):
                compile_lexical_query(query)

    def test_length_limits_are_enforced(self) -> None:
        assert compile_lexical_query("a" * 256).clause_count == 1
        with pytest.raises(QueryValidationError, match="scalar limit"):
            compile_lexical_query("a" * 257)
