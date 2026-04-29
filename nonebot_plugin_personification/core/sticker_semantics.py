from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


ALLOWED_STICKER_MOOD_TAGS = (
    "搞笑",
    "开心",
    "感动",
    "尴尬",
    "无语",
    "惊讶",
    "委屈",
    "生气",
    "害羞",
    "得意",
    "困惑",
    "赞同",
    "拒绝",
    "期待",
    "失落",
    "撒娇",
    "淡定",
    "震惊",
)
ALLOWED_STICKER_SCENE_TAGS = (
    "回应笑点",
    "接梗",
    "表达赞同",
    "化解尴尬",
    "自嘲",
    "反驳",
    "表达惊讶",
    "安慰对方",
    "撒娇",
    "表示无奈",
    "冷场时",
    "表达期待",
    "庆祝",
    "拒绝请求",
    "结束对话",
    "打招呼",
    "表达关心",
    "吐槽",
    "卖萌",
    "表达疑惑",
)
DEFAULT_STICKER_MOOD_TAG = "淡定"
DEFAULT_STICKER_SCENE_TAG = "表达疑惑"
DEFAULT_STICKER_SEMANTIC_HINT = f"{DEFAULT_STICKER_MOOD_TAG}|{DEFAULT_STICKER_SCENE_TAG}"
_HINT_SPLIT_RE = re.compile(r"[|｜/／]")
_MOOD_TAG_SET = frozenset(ALLOWED_STICKER_MOOD_TAGS)
_SCENE_TAG_SET = frozenset(ALLOWED_STICKER_SCENE_TAGS)


@dataclass(frozen=True)
class StickerSemanticHint:
    mood_tag: str = DEFAULT_STICKER_MOOD_TAG
    scene_tag: str = DEFAULT_STICKER_SCENE_TAG

    def render(self) -> str:
        return f"{self.mood_tag}|{self.scene_tag}"


def parse_sticker_semantic_hint(value: Any) -> StickerSemanticHint | None:
    text = str(value or "").strip()
    if not text:
        return None
    parts = [part.strip() for part in _HINT_SPLIT_RE.split(text, maxsplit=1)]
    if len(parts) != 2:
        return None
    mood_tag, scene_tag = parts
    if mood_tag not in _MOOD_TAG_SET or scene_tag not in _SCENE_TAG_SET:
        return None
    return StickerSemanticHint(mood_tag=mood_tag, scene_tag=scene_tag)


def normalize_sticker_semantic_hint(
    value: Any,
    *,
    fallback: str = DEFAULT_STICKER_SEMANTIC_HINT,
) -> str:
    parsed = parse_sticker_semantic_hint(value)
    if parsed is not None:
        return parsed.render()
    fallback_parsed = parse_sticker_semantic_hint(fallback)
    if fallback_parsed is not None:
        return fallback_parsed.render()
    return DEFAULT_STICKER_SEMANTIC_HINT


def default_sticker_semantic_hint(
    chat_intent: str,
    *,
    is_random_chat: bool = False,
) -> str:
    if chat_intent in {"lookup", "plugin_question", "explanation", "image_generation"}:
        return "淡定|表达疑惑"
    if is_random_chat:
        return "搞笑|接梗"
    if chat_intent == "banter":
        return "开心|吐槽"
    return DEFAULT_STICKER_SEMANTIC_HINT


__all__ = [
    "ALLOWED_STICKER_MOOD_TAGS",
    "ALLOWED_STICKER_SCENE_TAGS",
    "DEFAULT_STICKER_SCENE_TAG",
    "DEFAULT_STICKER_MOOD_TAG",
    "DEFAULT_STICKER_SEMANTIC_HINT",
    "StickerSemanticHint",
    "default_sticker_semantic_hint",
    "normalize_sticker_semantic_hint",
    "parse_sticker_semantic_hint",
]
