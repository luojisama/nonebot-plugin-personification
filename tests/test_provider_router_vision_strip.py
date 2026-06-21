"""回归测试：_strip_image_parts_for_text_only_provider 把多模态 content 降级成纯文本。

修复线上 bug：messages 含 OpenAI vision 格式 image_url 时，fallback 到不支持视觉的
provider（如 DeepSeek、Qwen 等第三方网关）会让上游返回 400
'unknown variant image_url, expected text'。
"""
from __future__ import annotations

from ._loader import load_personification_module

provider_router = load_personification_module("plugin.personification.core.provider_router")


def test_provider_router_treats_mimo_v25_pro_as_multimodal() -> None:
    assert provider_router.provider_supports_vision("openai", "mimo-v2.5-pro") is True
    assert provider_router.provider_supports_vision("anthropic", "mimo-v2.5-pro") is True
    assert provider_router.provider_supports_vision("openai", "mimo-v2.5") is True


def test_strip_image_parts_keeps_text_and_replaces_image() -> None:
    fn = provider_router._strip_image_parts_for_text_only_provider
    messages = [
        {"role": "system", "content": "你好"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "看这张图"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
            ],
        },
    ]
    out = fn(messages)
    assert out[0] == {"role": "system", "content": "你好"}
    assert out[1]["content"] == "看这张图 [图片]"


def test_strip_image_parts_handles_multiple_images() -> None:
    fn = provider_router._strip_image_parts_for_text_only_provider
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "对比这两张"},
                {"type": "image_url", "image_url": {"url": "..."}},
                {"type": "image_url", "image_url": {"url": "..."}},
            ],
        }
    ]
    out = fn(messages)
    assert out[0]["content"] == "对比这两张 [图片×2]"


def test_strip_image_parts_handles_image_only() -> None:
    fn = provider_router._strip_image_parts_for_text_only_provider
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "..."}},
            ],
        }
    ]
    out = fn(messages)
    assert out[0]["content"] == "[图片]"


def test_strip_image_parts_passthrough_for_plain_string_content() -> None:
    fn = provider_router._strip_image_parts_for_text_only_provider
    messages = [
        {"role": "system", "content": "纯文字 system"},
        {"role": "user", "content": "纯文字 user"},
    ]
    out = fn(messages)
    assert out == messages  # 不应改写


def test_strip_image_parts_does_not_mutate_input() -> None:
    fn = provider_router._strip_image_parts_for_text_only_provider
    original = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "保留"},
                {"type": "image_url", "image_url": {"url": "x"}},
            ],
        }
    ]
    snapshot = [dict(m) for m in original]
    fn(original)
    # 输入应保持不变（外层 dict 浅拷，content 是原列表，但函数不应改它）
    assert original[0]["content"][0] == snapshot[0]["content"][0]
    assert original[0]["content"][1] == snapshot[0]["content"][1]
