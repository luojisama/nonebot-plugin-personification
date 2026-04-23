from __future__ import annotations

import json
from typing import Any

from nonebot_plugin_personification.agent.tool_registry import AgentTool
from nonebot_plugin_personification.core.ai_routes import build_fallback_vision_caller
from nonebot_plugin_personification.core.media_refs import normalize_media_refs
from nonebot_plugin_personification.core.media_understanding import (
    analyze_images_with_route_or_fallback,
    analyze_videos_with_route_or_fallback,
)
from nonebot_plugin_personification.skills.skillpacks.sticker_tool.scripts.impl import get_current_image_urls
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
    return build_fallback_vision_caller(plugin_config)


def _normalize_images(images: list[str] | None, image_urls: list[str] | None = None) -> list[str]:
    merged: list[str] = []
    for item in list(images or []) + list(image_urls or []):
        value = str(item or "").strip()
        if value and value not in merged:
            merged.append(value)
    if not merged:
        merged.extend(get_current_image_urls())
    normalized = normalize_media_refs(images=merged, image_limit=3)
    return list(normalized.get("images") or [])[:3]


async def analyze_images(
    *,
    runtime: Any,
    query: str,
    images: list[str] | None = None,
    image_urls: list[str] | None = None,
    videos: list[str] | None = None,
) -> str:
    prompt = f"{VISION_ANALYZE_PROMPT}\n\n用户问题：{str(query or '').strip() or '请分析图片'}"
    raw_refs = list(images or []) + list(image_urls or [])
    if not raw_refs:
        raw_refs = get_current_image_urls()
    normalized_media = normalize_media_refs(images=raw_refs, videos=list(videos or []), image_limit=3, video_limit=1)
    refs = list(normalized_media.get("images") or [])
    invalid_refs = list(normalized_media.get("image_problems") or [])
    video_refs = list(normalized_media.get("videos") or [])
    invalid_video_refs = list(normalized_media.get("video_problems") or [])
    if not refs and not video_refs:
        return json.dumps(
            {
                "scene_summary": "",
                "ocr_text": [],
                "characters_or_entities": [],
                "franchise_candidates": [],
                "visual_evidence": [],
                "ambiguity_notes": ["missing_media", *invalid_refs, *invalid_video_refs],
                "confidence": 0.0,
            },
            ensure_ascii=False,
        )

    vision_caller = getattr(runtime, "vision_caller", None)
    if vision_caller is None and bool(
        getattr(
            runtime.plugin_config,
            "personification_fallback_enabled",
            getattr(runtime.plugin_config, "personification_vision_fallback_enabled", True),
        )
    ):
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

    outputs: list[tuple[str, str]] = []
    if refs:
        route_output, route_mode = await analyze_images_with_route_or_fallback(
            runtime=runtime,
            prompt=prompt,
            image_refs=refs,
            fallback_vision_caller=vision_caller,
        )
        if route_output:
            outputs.append((route_output, route_mode))
    video_output = ""
    video_mode = ""
    if video_refs:
        video_output, video_mode = await analyze_videos_with_route_or_fallback(
            runtime=runtime,
            prompt=prompt,
            video_refs=video_refs,
        )
        if video_output:
            outputs.append((video_output, video_mode))
    if not outputs:
        return json.dumps(
            {
                "scene_summary": "",
                "ocr_text": [],
                "characters_or_entities": [],
                "franchise_candidates": [],
                "visual_evidence": [],
                "ambiguity_notes": [
                    "vision_unavailable",
                    *invalid_refs,
                    *invalid_video_refs,
                    *(["video_understanding_disabled"] if video_refs and not bool(getattr(runtime.plugin_config, "personification_video_understanding_enabled", False)) else []),
                ],
                "confidence": 0.0,
            },
            ensure_ascii=False,
        )

    if len(outputs) == 1:
        return outputs[0][0]

    per_image: list[dict[str, Any]] = []
    merged_summaries: list[str] = []
    merged_ocr: list[str] = []
    merged_entities: list[dict[str, Any]] = []
    merged_candidates: list[dict[str, Any]] = []
    ambiguity_notes: list[str] = ["multi_media_combined", *invalid_refs, *invalid_video_refs]
    for index, (output, output_mode) in enumerate(outputs, start=1):
        if output_mode and output_mode not in ambiguity_notes:
            ambiguity_notes.append(output_mode)
        parsed: dict[str, Any] | None = None
        try:
            parsed = json.loads(str(output or "").strip())
        except Exception:
            parsed = None
        per_image.append(
            {
                "index": index,
                "analysis": parsed if isinstance(parsed, dict) else str(output or "").strip(),
            }
        )
        if isinstance(parsed, dict):
            summary = str(parsed.get("scene_summary", "") or "").strip()
            if summary:
                merged_summaries.append(f"图{index}：{summary}")
            for item in list(parsed.get("ocr_text") or [])[:8]:
                text = str(item or "").strip()
                if text and text not in merged_ocr:
                    merged_ocr.append(text)
            for item in list(parsed.get("characters_or_entities") or [])[:6]:
                if isinstance(item, dict) and item not in merged_entities:
                    merged_entities.append(item)
            for item in list(parsed.get("franchise_candidates") or [])[:6]:
                if isinstance(item, dict) and item not in merged_candidates:
                    merged_candidates.append(item)
            for item in list(parsed.get("ambiguity_notes") or [])[:4]:
                text = str(item or "").strip()
                if text and text not in ambiguity_notes:
                    ambiguity_notes.append(text)

    return json.dumps(
        {
            "scene_summary": "；".join(merged_summaries)[:300],
            "ocr_text": merged_ocr[:12],
            "characters_or_entities": merged_entities[:12],
            "franchise_candidates": merged_candidates[:12],
            "visual_evidence": per_image,
            "ambiguity_notes": ambiguity_notes[:8],
            "confidence": 0.45,
        },
        ensure_ascii=False,
    )


def build_vision_tool(runtime: Any) -> AgentTool:
    async def _handler(
        query: str,
        images: list[str] | None = None,
        image_urls: list[str] | None = None,
        videos: list[str] | None = None,
    ) -> str:
        return await analyze_images(
            runtime=runtime,
            query=query,
            images=images,
            image_urls=image_urls,
            videos=videos,
        )

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
                "videos": {"type": "array", "items": {"type": "string"}, "description": "视频引用列表（当前默认关闭）"},
            },
            "required": ["query"],
        },
        handler=_handler,
        enabled=lambda: bool(getattr(runtime, "agent_tool_caller", None))
        or getattr(runtime, "vision_caller", None) is not None,
    )

