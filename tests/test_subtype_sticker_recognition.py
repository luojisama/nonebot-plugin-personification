"""sub_type=1 表情包识别：vision 短路 + 占位文本注入，避免 LLM 解释表情包像素。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from ._loader import load_personification_module


pipeline_sticker = load_personification_module(
    "plugin.personification.handlers.reply_pipeline.pipeline_sticker"
)


class _FakeSegment:
    def __init__(self, data: dict[str, Any]) -> None:
        self.type = "image"
        self.data = data


class _StubHttpClient:
    """对 download_safe_image_bytes 透明：返回最小 PNG 字节让流程往下走。"""
    pass


def _silent_logger():
    return SimpleNamespace(info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None)


async def _run_extract(seg, *, runtime=None, monkeypatch=None, classify_called: list[bool] | None = None):
    if monkeypatch is None:
        raise RuntimeError("monkeypatch required")
    # 给 download_safe_image_bytes 一个固定的小 PNG（防止真的去请求 URL）
    async def _fake_download(*, url, file_name, http_client, logger):
        return ("image/png", b"\x89PNG\r\n\x1a\n" + b"0" * 64, False)

    monkeypatch.setattr(pipeline_sticker, "download_safe_image_bytes", _fake_download)

    # 让 classify_incoming_image 被记录（默认应该不被 sub_type=1 路径调用）
    async def _fake_classify(*, runtime, image_url, source_kind, width, height, file_id):
        if classify_called is not None:
            classify_called.append(True)
        return SimpleNamespace(
            is_sticker_like=False,
            text_label="[图片]",
            kind="photo",
            source="vision",
            reason="",
        )

    monkeypatch.setattr(pipeline_sticker, "classify_incoming_image", _fake_classify)

    message_text_ref: list[str] = []
    image_urls: list[str] = []
    sticker_candidates_ref: list = []
    stop_reply_ref = [False]

    await pipeline_sticker.extract_images_from_segment(
        seg,
        runtime=runtime or SimpleNamespace(),
        http_client=_StubHttpClient(),
        message_text_ref=message_text_ref,
        image_urls=image_urls,
        sticker_candidates_ref=sticker_candidates_ref,
        logger=_silent_logger(),
        stop_reply_ref=stop_reply_ref,
    )
    return SimpleNamespace(
        message_text=message_text_ref,
        image_urls=image_urls,
        sticker_candidates=sticker_candidates_ref,
        stop_reply=stop_reply_ref[0],
    )


def test_subtype_1_short_circuits_vision(monkeypatch) -> None:
    """sub_type=1 时不调 vision、不放 data_url 到 image_urls。"""
    seg = _FakeSegment({
        "url": "https://multimedia.nt.qq.com.cn/x.jpg",
        "file": "abc.jpg",
        "sub_type": 1,
        "summary": "[动画表情]",
    })
    classify_called: list[bool] = []
    out = asyncio.run(_run_extract(seg, monkeypatch=monkeypatch, classify_called=classify_called))
    assert not classify_called, "sub_type=1 不应触发 vision 分类"
    assert out.image_urls == [], "表情包不应进入 image_urls（防止 LLM 当照片解释）"
    assert len(out.message_text) == 1
    assert "[对方发送了一个表情包" in out.message_text[0]
    assert "动画表情" in out.message_text[0]
    # 但仍应收集到 sticker_candidates 供后续自动学习
    assert len(out.sticker_candidates) == 1


def test_subtype_1_no_summary_still_works(monkeypatch) -> None:
    seg = _FakeSegment({
        "url": "https://multimedia.nt.qq.com.cn/x.jpg",
        "file": "abc.jpg",
        "sub_type": 1,
    })
    out = asyncio.run(_run_extract(seg, monkeypatch=monkeypatch))
    assert out.message_text == ["[对方发送了一个表情包]"]


def test_subtype_as_string_1_also_works(monkeypatch) -> None:
    """协议端可能把 sub_type 当字符串发，应自动转 int。"""
    seg = _FakeSegment({
        "url": "https://example.com/x.jpg",
        "file": "x.jpg",
        "sub_type": "1",
    })
    classify_called: list[bool] = []
    out = asyncio.run(_run_extract(seg, monkeypatch=monkeypatch, classify_called=classify_called))
    assert not classify_called
    assert out.image_urls == []


def test_subtype_camelcase_also_works(monkeypatch) -> None:
    """部分协议端用 subType 驼峰。"""
    seg = _FakeSegment({
        "url": "https://example.com/x.jpg",
        "file": "x.jpg",
        "subType": 1,
    })
    out = asyncio.run(_run_extract(seg, monkeypatch=monkeypatch))
    assert out.image_urls == []
    assert out.message_text[0].startswith("[对方发送了一个表情包")


def test_subtype_0_or_missing_goes_normal_path(monkeypatch) -> None:
    """普通图片 sub_type=0：走原 vision 分类路径，data_url 仍注入 image_urls。"""
    seg = _FakeSegment({
        "url": "https://example.com/photo.jpg",
        "file": "photo.jpg",
        "sub_type": 0,
    })
    classify_called: list[bool] = []
    out = asyncio.run(_run_extract(seg, monkeypatch=monkeypatch, classify_called=classify_called))
    assert classify_called, "普通图片应触发 vision 分类"
    assert len(out.image_urls) == 1
    assert out.image_urls[0].startswith("data:image/png;base64,")


def test_heuristic_sticker_classification_also_skips_image_urls(monkeypatch) -> None:
    """如果 vision 启发式判断为 sticker，也不应把 data_url 加入 image_urls。"""
    async def _fake_download(*, url, file_name, http_client, logger):
        return ("image/png", b"\x89PNG\r\n\x1a\n" + b"0" * 64, False)

    async def _fake_classify_returns_sticker(*, runtime, image_url, source_kind, width, height, file_id):
        return SimpleNamespace(
            is_sticker_like=True,
            text_label="[表情包]",
            kind="sticker",
            source="heuristic",
            reason="0x0 size",
        )

    monkeypatch.setattr(pipeline_sticker, "download_safe_image_bytes", _fake_download)
    monkeypatch.setattr(pipeline_sticker, "classify_incoming_image", _fake_classify_returns_sticker)

    seg = _FakeSegment({"url": "https://example.com/x.png", "file": "x.png"})
    message_text_ref: list[str] = []
    image_urls: list[str] = []
    sticker_candidates_ref: list = []

    asyncio.run(pipeline_sticker.extract_images_from_segment(
        seg,
        runtime=SimpleNamespace(),
        http_client=_StubHttpClient(),
        message_text_ref=message_text_ref,
        image_urls=image_urls,
        sticker_candidates_ref=sticker_candidates_ref,
        logger=_silent_logger(),
        stop_reply_ref=[False],
    ))
    assert image_urls == [], "启发式判断为表情包时同样不应把图片注入 vision"
    assert len(sticker_candidates_ref) == 1


def test_subtype_garbage_value_falls_back_to_normal(monkeypatch) -> None:
    """sub_type 是非数字字符串 → 不识别为表情包，走原路径。"""
    seg = _FakeSegment({
        "url": "https://example.com/photo.jpg",
        "file": "photo.jpg",
        "sub_type": "not-a-number",
    })
    classify_called: list[bool] = []
    out = asyncio.run(_run_extract(seg, monkeypatch=monkeypatch, classify_called=classify_called))
    assert classify_called, "异常 sub_type 值应该 fallback 到 vision 分类"
