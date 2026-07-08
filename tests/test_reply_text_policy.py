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


def test_normalize_visible_reply_text_drops_reasoning_trace_and_orphan_bracket() -> None:
    raw = "轻松\n\nStep 1: 检查\nStep 2: 输出\n\n轻松"
    assert reply_text_policy.normalize_visible_reply_text(raw) == "轻松"
    assert reply_text_policy.normalize_visible_reply_text("[") == ""


def test_normalize_visible_reply_text_reduces_formulaic_opening_tics() -> None:
    assert reply_text_policy.normalize_visible_reply_text("等下，这什么表情") == "这什么表情"
    assert reply_text_policy.normalize_visible_reply_text("这也太危险了吧，你到底在玩什么") == "挺危险的，你到底在玩什么"
    assert reply_text_policy.normalize_visible_reply_text("这也抽得太狠了吧") == "抽得挺狠"
    assert reply_text_policy.normalize_visible_reply_text("被雷炸了，这也太刺激了吧") == "被雷炸了，挺刺激的"
    assert reply_text_policy.normalize_visible_reply_text("这也行") == "这也行"
    assert reply_text_policy.normalize_visible_reply_text("我先看看情况，等会再说") == ""
    assert reply_text_policy.normalize_visible_reply_text("先看看情况等会再说吧") == ""
    assert reply_text_policy.normalize_visible_reply_text("我先围观一下，别急着吵") == "别急着吵"


def test_looks_like_formulaic_reply_tic_detects_repeated_reply_templates() -> None:
    assert reply_text_policy.looks_like_formulaic_reply_tic("等下，这什么表情")
    assert reply_text_policy.looks_like_formulaic_reply_tic("被雷炸了，这也太刺激了吧")
    assert reply_text_policy.looks_like_formulaic_reply_tic("我先看看情况，等会再说")
    assert not reply_text_policy.looks_like_formulaic_reply_tic("这也行")


def test_looks_like_markdown_reply_detects_visible_formatting() -> None:
    assert reply_text_policy.looks_like_markdown_reply("**广州** 这两天雨多")
    assert reply_text_policy.looks_like_markdown_reply("- 22日雨\n- 23日雨")
    assert reply_text_policy.looks_like_markdown_reply("Step 1: 检查\nStep 2: 输出")
    assert not reply_text_policy.looks_like_markdown_reply("广州这两天雨多，伞别离手")


def test_looks_like_question_reply_detects_visible_questions() -> None:
    assert reply_text_policy.looks_like_question_reply("你那边是哪儿啊，我别乱猜天气。")
    assert reply_text_policy.looks_like_question_reply("这个几点开始？")
    assert not reply_text_policy.looks_like_question_reply("地点没拿准，我别乱猜天气。")
