from __future__ import annotations

from ._loader import load_personification_module


reply_text_policy = load_personification_module("plugin.personification.core.reply_text_policy")


def test_normalize_visible_reply_text_strips_markdown_but_keeps_text() -> None:
    raw = """
# 天气

- **22日**会下雨
- [详情](https://example.com/weather)

> 出门带伞

```text
别淋湿
```
"""

    cleaned = reply_text_policy.normalize_visible_reply_text(raw)

    assert "#" not in cleaned
    assert "**" not in cleaned
    assert "- " not in cleaned
    assert "https://" not in cleaned
    assert "```" not in cleaned
    assert "22日会下雨" in cleaned
    assert "详情" in cleaned
    assert "出门带伞" in cleaned
    assert "别淋湿" in cleaned


def test_normalize_visible_reply_text_preserves_image_b64_marker() -> None:
    marker = "[IMAGE_B64]QUJDREVGRw==[/IMAGE_B64]"
    cleaned = reply_text_policy.normalize_visible_reply_text(f"**好了**\n\n{marker}")

    assert "**" not in cleaned
    assert "好了" in cleaned
    assert marker in cleaned


def test_looks_like_markdown_reply_detects_visible_formatting() -> None:
    assert reply_text_policy.looks_like_markdown_reply("**广州** 这两天雨多")
    assert reply_text_policy.looks_like_markdown_reply("- 22日雨\n- 23日雨")
    assert not reply_text_policy.looks_like_markdown_reply("广州这两天雨多，伞别离手")
