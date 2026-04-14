from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nonebot_plugin_personification.agent.tool_registry import AgentTool
from nonebot_plugin_personification.skills.skillpacks.sticker_tool.scripts.impl import get_current_image_urls
from nonebot_plugin_personification.skills.skillpacks.vision_caller.scripts.impl import build_vision_caller


VISION_ANALYZE_PROMPT = """你是 ACG 场景视觉分析器。
请基于图片和用户问题，输出一个 JSON 对象，不要输出解释性文字。

字段要求：
{
  "scene_summary": "一句话概括画面",
  "ocr_text": ["..."],
  "characters_or_entities": [{"name": "", "type": "character|person|object|ui|organization|unknown", "evidence": ""}],
  "franchise_candidates": [{"name": "", "why": "", "confidence": 0.0}],
  "visual_evidence": ["..."],
  "ambiguity_notes": ["..."],
  "confidence": 0.0
}

要求：
- 候选可以多个，不要武断唯一结论
- ACG 场景尽量区分角色、作品、组织、道具、界面元素
- 看不准就明确写 uncertain 或留空
- confidence 取 0 到 1"""


def _build_fallback_vision_caller(plugin_config: Any):
    class _ConfigProxy:
        def __init__(self, original: Any) -> None:
            self._original = original

        def __getattr__(self, name: str) -> Any:
            if name == "personification_labeler_api_type":
                return getattr(self._original, "personification_vision_fallback_provider", "") or getattr(
                    self._original,
                    "personification_labeler_api_type",
                    "",
                )
            if name == "personification_labeler_api_url":
                return getattr(self._original, "personification_labeler_api_url", "") or getattr(
                    self._original,
                    "personification_api_url",
                    "",
                )
            if name == "personification_labeler_api_key":
                return getattr(self._original, "personification_labeler_api_key", "") or getattr(
                    self._original,
                    "personification_api_key",
                    "",
                )
            if name == "personification_labeler_model":
                return getattr(self._original, "personification_vision_fallback_model", "") or getattr(
                    self._original,
                    "personification_labeler_model",
                    "",
                )
            return getattr(self._original, name)

    return build_vision_caller(_ConfigProxy(plugin_config))


def _normalize_images(images: list[str] | None, image_urls: list[str] | None = None) -> list[str]:
    merged: list[str] = []
    for item in list(images or []) + list(image_urls or []):
        value = str(item or "").strip()
        if value and value not in merged:
            merged.append(value)
    if not merged:
        merged.extend(get_current_image_urls())
    return merged[:3]


async def analyze_images(
    *,
    runtime: Any,
    query: str,
    images: list[str] | None = None,
    image_urls: list[str] | None = None,
) -> str:
    prompt = f"{VISION_ANALYZE_PROMPT}\n\n用户问题：{str(query or '').strip() or '请分析图片'}"
    refs = _normalize_images(images, image_urls=image_urls)
    if not refs:
        return json.dumps(
            {
                "scene_summary": "",
                "ocr_text": [],
                "characters_or_entities": [],
                "franchise_candidates": [],
                "visual_evidence": [],
                "ambiguity_notes": ["missing_images"],
                "confidence": 0.0,
            },
            ensure_ascii=False,
        )

    vision_caller = getattr(runtime, "vision_caller", None)
    if vision_caller is None and bool(getattr(runtime.plugin_config, "personification_vision_fallback_enabled", True)):
        vision_caller = _build_fallback_vision_caller(runtime.plugin_config)
    if vision_caller is None:
        return json.dumps(
            {
                "scene_summary": "",
                "ocr_text": [],
                "characters_or_entities": [],
                "franchise_candidates": [],
                "visual_evidence": [],
                "ambiguity_notes": ["vision_unavailable"],
                "confidence": 0.0,
            },
            ensure_ascii=False,
        )

    outputs: list[str] = []
    for ref in refs:
        target = ref
        if not ref.startswith(("data:", "http://", "https://")) and Path(ref).exists():
            target = Path(ref).as_uri()
        outputs.append(await vision_caller.describe(prompt, target))

    if len(outputs) == 1:
        return outputs[0]

    return json.dumps(
        {
            "scene_summary": "",
            "ocr_text": [],
            "characters_or_entities": [],
            "franchise_candidates": [],
            "visual_evidence": outputs,
            "ambiguity_notes": ["multi_image_raw_merge"],
            "confidence": 0.35,
        },
        ensure_ascii=False,
    )


def build_vision_tool(runtime: Any) -> AgentTool:
    async def _handler(query: str, images: list[str] | None = None, image_urls: list[str] | None = None) -> str:
        return await analyze_images(runtime=runtime, query=query, images=images, image_urls=image_urls)

    return AgentTool(
        name="vision_analyze",
        description=(
            "分析用户当前发送的图片，适合识别人物、作品、截图界面、画面元素、OCR 文本和可能的 ACG 候选。"
            "输出候选和证据，不强行给单一结论。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "用户问题或分析目标"},
                "images": {"type": "array", "items": {"type": "string"}, "description": "图片引用列表"},
            },
            "required": ["query"],
        },
        handler=_handler,
        enabled=lambda: bool(getattr(runtime.plugin_config, "personification_vision_fallback_enabled", True))
        or getattr(runtime, "vision_caller", None) is not None,
    )

