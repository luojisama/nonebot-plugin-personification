from __future__ import annotations

import json
import re
from typing import Any

from plugin.personification.agent.tool_registry import AgentTool
from plugin.personification.core.ai_routes import build_fallback_vision_caller
from plugin.personification.core.media_refs import normalize_media_refs
from plugin.personification.core.media_understanding import (
    analyze_audios_with_route_or_fallback,
    analyze_images_with_route_or_fallback,
    analyze_videos_with_route_or_fallback,
    audio_route_available,
    primary_route_supports_native_video,
)
from plugin.personification.skills.skillpacks.sticker_tool.scripts.impl import (
    get_current_audio_urls,
    get_current_image_urls,
    get_current_video_urls,
)
VISION_ANALYZE_PROMPT = """你是 ACG 场景多媒体分析器。
请基于图片、视频、音频和用户问题，输出一个 JSON 对象，不要输出解释性文字。

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
- 只输出 JSON 对象本身；禁止 Markdown 标题、列表、加粗、代码围栏、XML 或解释性前后缀
- JSON 字段值必须是纯文本，不要把 Markdown 或 <think>/<status>/<action> 等控制块放进字段
- 候选可以多个，不要武断唯一结论
- ACG 场景尽量区分角色、作品、组织、道具、界面元素
- 视频要按时间顺序关注动作变化、字幕/OCR、镜头切换和关键帧线索
- 音频要区分直接听到的语音、音效、音乐作用与模型推断；不要凭声音猜真实身份
- 看不准就明确写 uncertain 或留空
- confidence 取 0 到 1"""


_VISION_FENCE_RE = re.compile(r"```(?:[^\n]*)\n?(.*?)```", re.DOTALL)
_VISION_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_VISION_LIST_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)", re.MULTILINE)
_VISION_BOLD_RE = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", re.DOTALL)
_VISION_XML_RE = re.compile(r"</?[A-Za-z][^>]{0,200}>", re.IGNORECASE)


def _strip_vision_markdown(value: str) -> str:
    """Keep tool evidence readable while removing presentation/control wrappers."""

    cleaned = str(value or "")
    cleaned = _VISION_FENCE_RE.sub(lambda match: match.group(1), cleaned)
    cleaned = _VISION_HEADING_RE.sub("", cleaned)
    cleaned = _VISION_LIST_RE.sub("", cleaned)
    cleaned = _VISION_BOLD_RE.sub(r"\2", cleaned)
    cleaned = _VISION_XML_RE.sub("", cleaned)
    return cleaned.strip()


def _plain_vision_value(value: Any) -> Any:
    if isinstance(value, str):
        return _strip_vision_markdown(value)
    if isinstance(value, list):
        return [_plain_vision_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain_vision_value(item) for key, item in value.items()}
    return value


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


def _merge_explicit_and_current_refs(
    explicit_refs: list[str] | None,
    current_refs: list[str],
) -> list[str]:
    """Keep resolved turn media when a model supplies an opaque tool argument.

    The Agent can echo a provenance token in ``videos``/``audios`` even though
    the processor has already materialized the current turn to a usable URL or
    absolute path.  Explicit refs remain first, but must not mask the trusted
    current-media context when they fail normalization.
    """

    merged: list[str] = []
    for item in list(explicit_refs or []) + list(current_refs or []):
        value = str(item or "").strip()
        if value and value not in merged:
            merged.append(value)
    return merged


async def analyze_images(
    *,
    runtime: Any,
    query: str,
    images: list[str] | None = None,
    image_urls: list[str] | None = None,
    videos: list[str] | None = None,
    audios: list[str] | None = None,
) -> str:
    prompt = f"{VISION_ANALYZE_PROMPT}\n\n用户问题：{str(query or '').strip() or '请分析这段媒体'}"
    raw_refs = list(images or []) + list(image_urls or [])
    if not raw_refs:
        raw_refs = get_current_image_urls()
    raw_videos = _merge_explicit_and_current_refs(videos, get_current_video_urls())
    raw_audios = _merge_explicit_and_current_refs(audios, get_current_audio_urls())
    normalized_media = normalize_media_refs(
        images=raw_refs,
        videos=raw_videos,
        audios=raw_audios,
        image_limit=3,
        video_limit=1,
        audio_limit=1,
    )
    refs = list(normalized_media.get("images") or [])
    invalid_refs = list(normalized_media.get("image_problems") or [])
    video_refs = list(normalized_media.get("videos") or [])
    invalid_video_refs = list(normalized_media.get("video_problems") or [])
    audio_refs = list(normalized_media.get("audios") or [])
    invalid_audio_refs = list(normalized_media.get("audio_problems") or [])
    if not refs and not video_refs and not audio_refs:
        return json.dumps(
            {
                "scene_summary": "",
                "ocr_text": [],
                "characters_or_entities": [],
                "franchise_candidates": [],
                "visual_evidence": [],
                "ambiguity_notes": [
                    "missing_media",
                    *invalid_refs,
                    *invalid_video_refs,
                    *invalid_audio_refs,
                ],
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

    outputs: list[tuple[str, str, str]] = []
    media_routes: list[dict[str, Any]] = []
    if refs:
        route_output, route_mode = await analyze_images_with_route_or_fallback(
            runtime=runtime,
            prompt=prompt,
            image_refs=refs,
            fallback_vision_caller=vision_caller,
        )
        if route_output:
            outputs.append((route_output, route_mode, "image"))
    video_output = ""
    video_mode = ""
    if video_refs:
        video_attempts: list[dict[str, Any]] = []
        video_output, video_mode = await analyze_videos_with_route_or_fallback(
            runtime=runtime,
            prompt=prompt,
            video_refs=video_refs,
            context_terms=[str(query or "").strip()],
            route_attempts=video_attempts,
        )
        media_routes.append(
            {
                "kind": "video",
                "selected_route": video_mode if video_output else "",
                "attempts": video_attempts,
                "diagnostic_codes": [
                    "gemini_web_route_fallback_used"
                    for item in video_attempts
                    if item.get("route") == "video_gemini_web"
                    and item.get("status") not in {"ok", "skipped"}
                    and video_output
                    and video_mode != "video_gemini_web"
                ][:1],
            }
        )
        if video_output:
            outputs.append((video_output, video_mode, "video"))
    audio_output = ""
    audio_mode = ""
    if audio_refs:
        audio_attempts: list[dict[str, Any]] = []
        audio_output, audio_mode = await analyze_audios_with_route_or_fallback(
            runtime=runtime,
            prompt=prompt,
            audio_refs=audio_refs,
            context_terms=[str(query or "").strip()],
            route_attempts=audio_attempts,
        )
        media_routes.append(
            {
                "kind": "audio",
                "selected_route": audio_mode if audio_output else "",
                "attempts": audio_attempts,
                "diagnostic_codes": [
                    "gemini_web_route_fallback_used"
                    for item in audio_attempts
                    if item.get("route") == "audio_gemini_web"
                    and item.get("status") not in {"ok", "skipped"}
                    and audio_output
                    and audio_mode != "audio_gemini_web"
                ][:1],
            }
        )
        if audio_output:
            outputs.append((audio_output, audio_mode, "audio"))
    if not outputs:
        video_disabled = video_mode == "video_disabled"
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
                    *invalid_audio_refs,
                    *(["video_understanding_disabled"] if video_refs and video_disabled else []),
                ],
                "media_routes": media_routes,
                "confidence": 0.0,
            },
            ensure_ascii=False,
        )

    if len(outputs) == 1:
        output, output_mode, output_kind = outputs[0]
        try:
            parsed_output = json.loads(str(output or "").strip())
        except Exception:
            parsed_output = None
        if isinstance(parsed_output, dict):
            parsed_output = _plain_vision_value(parsed_output)
            notes = [str(item or "").strip() for item in list(parsed_output.get("ambiguity_notes") or [])]
            if output_mode and output_mode not in notes:
                notes.append(output_mode)
            parsed_output["ambiguity_notes"] = [item for item in notes if item][:8]
            parsed_output["analysis_route"] = output_mode
            parsed_output["media_routes"] = media_routes
            return json.dumps(parsed_output, ensure_ascii=False)
        return json.dumps(
            {
                "scene_summary": _strip_vision_markdown(str(output or ""))[:16000],
                "ocr_text": [],
                "characters_or_entities": [],
                "franchise_candidates": [],
                "visual_evidence": [{"kind": output_kind, "analysis": _strip_vision_markdown(str(output or ""))}],
                "ambiguity_notes": [output_mode] if output_mode else [],
                "confidence": 0.5,
                "analysis_route": output_mode,
                "media_routes": media_routes,
            },
            ensure_ascii=False,
        )

    per_image: list[dict[str, Any]] = []
    merged_summaries: list[str] = []
    merged_ocr: list[str] = []
    merged_entities: list[dict[str, Any]] = []
    merged_candidates: list[dict[str, Any]] = []
    ambiguity_notes: list[str] = [
        "multi_media_combined",
        *invalid_refs,
        *invalid_video_refs,
        *invalid_audio_refs,
    ]
    for index, (output, output_mode, output_kind) in enumerate(outputs, start=1):
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
                "kind": output_kind,
                "analysis": _plain_vision_value(parsed) if isinstance(parsed, dict) else _strip_vision_markdown(str(output or "")),
            }
        )
        if isinstance(parsed, dict):
            parsed = _plain_vision_value(parsed)
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
            "media_routes": media_routes,
        },
        ensure_ascii=False,
    )


def build_vision_tool(runtime: Any) -> AgentTool:
    async def _handler(
        query: str,
        images: list[str] | None = None,
        image_urls: list[str] | None = None,
        videos: list[str] | None = None,
        audios: list[str] | None = None,
    ) -> str:
        return await analyze_images(
            runtime=runtime,
            query=query,
            images=images,
            image_urls=image_urls,
            videos=videos,
            audios=audios,
        )

    return AgentTool(
        name="vision_analyze",
        description=(
            "分析用户当前发送的图片、视频或音频，适合识别人物、作品、截图界面、画面元素、OCR 文本、视频动作变化、语音内容和可能的 ACG 候选。"
            "支持全模态模型原生音视频、Gemini Web 实验路径、外部全模态 API、关键帧分镜与音频转写降级；社交 MCP 结果含 video_ref 时可把它作为 videos 输入继续读正文视频。"
            "输出候选和证据，不强行给单一结论。工具结果必须保持为 JSON 字符串，禁止 Markdown 标题、列表、加粗、代码围栏、XML 和解释性前后缀。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "用户问题或分析目标"},
                "images": {"type": "array", "items": {"type": "string"}, "description": "图片引用列表"},
                "videos": {"type": "array", "items": {"type": "string"}, "description": "视频引用列表；省略时自动使用当前消息或引用消息中的视频"},
                "audios": {"type": "array", "items": {"type": "string"}, "description": "音频引用列表；省略时自动使用当前消息或引用消息中的一条 QQ 语音"},
            },
            "required": ["query"],
        },
        handler=_handler,
        enabled=lambda: (
            bool(getattr(runtime, "agent_tool_caller", None))
            or getattr(runtime, "vision_caller", None) is not None
            or primary_route_supports_native_video(runtime)
            or audio_route_available(runtime)
        ),
    )
