from __future__ import annotations

from ._loader import load_personification_module

config_search = load_personification_module("plugin.personification.core.config_search")


def test_config_search_index_contains_full_pinyin_and_initials() -> None:
    tokens = config_search.build_config_search_index("好感度态度表", "配置中心")
    compact = " ".join(tokens)

    assert "haogandutaidubiao" in compact
    assert "hgdtdb" in tokens
    assert "peizhizhongxin" in compact
    assert "pzzx" in tokens
