from __future__ import annotations

import json
import re
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from nonebot_plugin_personification.agent.tool_registry import AgentTool
from nonebot_plugin_personification.core.sticker_library import (
    analyze_sticker_image,
    list_local_sticker_files,
    load_sticker_metadata,
    render_sticker_semantic_summary,
    resolve_sticker_dir,
)
from nonebot_plugin_personification.core.sticker_semantics import (
    StickerSemanticHint,
    normalize_sticker_semantic_hint,
    parse_sticker_semantic_hint,
)
from nonebot_plugin_personification.core.media_understanding import analyze_images_with_route_or_fallback
from nonebot_plugin_personification.core.sticker_feedback import get_sticker_score
from nonebot_plugin_personification.core.image_result_cache import (
    build_image_cache_key,
    get_cached_image_result,
    normalize_cache_text,
    set_cached_image_result,
)
from nonebot_plugin_personification.skills.skillpacks.vision_caller.scripts.impl import VisionCaller


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

UNDERSTAND_STICKER_PROMPT = (
    "请完整理解这张静态表情包/截图，用一句中文概括主体、动作/表情、图中文字和适用场景；"
    "如果有明显不适用场景也顺带点出。控制在60字内，直接输出，不加前缀。"
)

SELECT_STICKER_DESCRIPTION = """从表情包库中语义选择一张合适的图片发出。
调用时机：当你觉得这个时候发一张表情包比说话更自然时。
参数说明：
- mood：结构化表情标签，格式固定为"情绪标签|场景标签"，如"搞笑|接梗""无语|吐槽""撒娇|卖萌"
- context：当前对话的一句话摘要，如"对方在分享一件搞笑的事"
- proactive：是否是你主动发出（True=主动，False=回应对方）
不要为了发表情包而发，只在真正合适的时机调用。"""

# Prompt sent to the vision+search LLM when identifying image subjects.
# The LLM receives the image and is asked to first describe, then use its
# web_search tool to look up possible sources / character names.
IDENTIFY_IMAGE_PROMPT = """你是一个图片内容分析助手，擅长识别动漫、游戏、影视角色及相关作品。

## 任务
1. 先仔细观察图片，描述你看到的内容（角色外貌、场景、文字、画风等关键特征）。
2. 根据你的观察，使用 web_search 工具主动搜索，推断图片中的人物/角色可能是谁、出自哪部作品。
3. 综合图片观察和搜索结果，给出你的最终判断。

## 输出格式
- 先输出身份推断（使用"可能是/疑似"等表述，说明角色名和作品名）
- 再输出图片描述（1-2句）
- 如果无法确定，说明原因并给出最接近的猜测

## 注意
- 必须至少调用一次 web_search，不要只凭记忆判断
- 搜索关键词要具体，如"蓝色短发 猫耳 动漫角色"或角色名+作品名
- 控制在150字以内"""


DEFAULT_STICKER_CONFIG = {
    "semantic_threshold": 1,
    "gif_whitelist": [],
}

_CURRENT_IMAGE_URLS: ContextVar[List[str]] = ContextVar(
    "personification_current_image_urls",
    default=[],
)
_CURRENT_IMAGE_TEXT: ContextVar[str] = ContextVar(
    "personification_current_image_text",
    default="",
)


# ---------------------------------------------------------------------------
# Sticker helpers
# ---------------------------------------------------------------------------

def is_gif_sticker(message_segment: dict) -> bool:
    file_name = str(message_segment.get("data", {}).get("file", "") or "").lower()
    seg_type = str(message_segment.get("data", {}).get("type", "") or "").lower()
    return file_name.endswith(".gif") or seg_type == "gif"


def load_sticker_config(skills_root: Optional[Path]) -> dict:
    config = dict(DEFAULT_STICKER_CONFIG)
    if skills_root is None:
        return config

    config_path = Path(skills_root) / "sticker" / "config.yaml"
    if not config_path.exists():
        return config

    try:
        import yaml
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return config

    if not isinstance(loaded, dict):
        return config

    threshold = loaded.get("semantic_threshold")
    if isinstance(threshold, int):
        config["semantic_threshold"] = threshold
    gif_whitelist = loaded.get("gif_whitelist")
    if isinstance(gif_whitelist, list):
        config["gif_whitelist"] = [str(item) for item in gif_whitelist]
    return config


def resolve_sticker_path(sticker_dir: Path, sticker_name: str) -> Path | None:
    base_dir = resolve_sticker_dir(sticker_dir)
    raw_name = str(sticker_name or "").strip()
    if not raw_name or not base_dir.exists():
        return None
    if raw_name.startswith(("http://", "https://", "file://")):
        return None

    try:
        resolved_dir = base_dir.resolve(strict=True)
    except Exception:
        return None

    direct_path = Path(raw_name)
    if direct_path.is_absolute():
        return None
    if ".." in direct_path.parts:
        return None

    candidate = (resolved_dir / direct_path).resolve()
    if candidate.exists() and candidate.is_file() and resolved_dir in candidate.parents:
        return candidate

    for file_path in list_local_sticker_files(resolved_dir, include_gif=True):
        if file_path.stem == raw_name or file_path.name == raw_name:
            return file_path
    return None


async def understand_sticker(
    message_segment: dict,
    image_url: str,
    vision_caller: Optional[Any],
) -> str:
    if is_gif_sticker(message_segment):
        return "[NO_REPLY]"
    runtime = vision_caller if hasattr(vision_caller, "plugin_config") else None
    fallback_caller = getattr(runtime, "vision_caller", None) if runtime is not None else vision_caller
    if fallback_caller is None and runtime is None:
        return ""
    if runtime is not None:
        result = await analyze_sticker_image(
            runtime=runtime,
            image_refs=[image_url],
            fallback_vision_caller=fallback_caller,
        )
        payload = {
            "description": result.description,
            "ocr_text": result.ocr_text,
            "use_hint": result.use_hint,
            "avoid_hint": result.avoid_hint,
            "mood_tags": result.mood_tags,
            "scene_tags": result.scene_tags,
        }
        return render_sticker_semantic_summary(payload) or result.summary
    return await fallback_caller.describe(UNDERSTAND_STICKER_PROMPT, image_url)


def _extract_runtime_and_vision_caller(value: Optional[Any]) -> tuple[Any | None, Optional[VisionCaller]]:
    if value is None:
        return None, None
    if hasattr(value, "plugin_config"):
        return value, getattr(value, "vision_caller", None)
    return None, value


# ---------------------------------------------------------------------------
# Context var helpers
# ---------------------------------------------------------------------------

def set_current_image_urls(image_urls: List[str]) -> object:
    return _CURRENT_IMAGE_URLS.set(list(image_urls))


def reset_current_image_urls(token: object) -> None:
    _CURRENT_IMAGE_URLS.reset(token)


def get_current_image_urls() -> List[str]:
    return list(_CURRENT_IMAGE_URLS.get())


def set_current_image_context(
    image_urls: List[str], user_text: str = ""
) -> tuple[object, object]:
    urls_token = _CURRENT_IMAGE_URLS.set(list(image_urls))
    text_token = _CURRENT_IMAGE_TEXT.set(str(user_text or "").strip())
    return urls_token, text_token


def reset_current_image_context(tokens: tuple[object, object]) -> None:
    urls_token, text_token = tokens
    _CURRENT_IMAGE_URLS.reset(urls_token)
    _CURRENT_IMAGE_TEXT.reset(text_token)


def get_current_image_text() -> str:
    return str(_CURRENT_IMAGE_TEXT.get() or "").strip()


# ---------------------------------------------------------------------------
# Sticker metadata / scoring
# ---------------------------------------------------------------------------

def _normalize_matching_text(text: str) -> str:
    normalized = re.sub(r"\s+", "", str(text or "").strip().lower())
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "", normalized)
    return normalized


def _text_overlap_score(context_text: str, candidate_text: str) -> int:
    current = _normalize_matching_text(context_text)
    target = _normalize_matching_text(candidate_text)
    if len(current) < 2 or len(target) < 2:
        return 0
    score = 0
    if current in target or target in current:
        score += 3
    max_span = min(8, len(target))
    for span in range(max_span, 1, -1):
        for start in range(0, len(target) - span + 1):
            if target[start : start + span] in current:
                score += 1
                return score
    return score


def _score_sticker(meta: dict, hint: StickerSemanticHint, proactive: bool, context: str = "") -> int:
    mood_tags = [str(tag) for tag in (meta.get("mood_tags", []) or []) if tag]
    scene_tags = [str(tag) for tag in (meta.get("scene_tags", []) or []) if tag]
    score = 0
    if hint.mood_tag in mood_tags:
        score += 5
    if hint.scene_tag in scene_tags:
        score += 6
    if proactive and meta.get("proactive_send") is True:
        score += 2
    if not proactive and meta.get("proactive_send") is False:
        score += 1
    if hint.scene_tag in {"打招呼", "冷场时", "卖萌"} and meta.get("proactive_send") is True:
        score += 1
    if context:
        score += _text_overlap_score(context, str(meta.get("description", "") or ""))
        score += _text_overlap_score(context, str(meta.get("ocr_text", "") or ""))
        score += min(3, _text_overlap_score(context, str(meta.get("use_hint", "") or "")))
        score -= min(4, _text_overlap_score(context, str(meta.get("avoid_hint", "") or "")))
    return score


def rank_sticker_candidates(
    sticker_dir: Path,
    *,
    mood: str,
    context: str,
    proactive: bool,
    plugin_config: Any,
    skills_root: Optional[Path] = None,
    minimum_score: int = 1,
    limit: int = 6,
    feedback_state: Optional[dict[str, Any]] = None,
) -> List[dict[str, Any]]:
    sticker_dir = resolve_sticker_dir(sticker_dir)
    if not sticker_dir.exists():
        return []

    stickers = list_local_sticker_files(sticker_dir)
    if not stickers:
        return []

    if not getattr(plugin_config, "personification_sticker_semantic", True):
        return [{"score": 1, "path": stickers[0], "meta": {}}]

    hint = parse_sticker_semantic_hint(mood)
    if hint is None:
        normalized_hint = normalize_sticker_semantic_hint(mood)
        hint = parse_sticker_semantic_hint(normalized_hint)
    if hint is None:
        return []

    config = load_sticker_config(skills_root)
    metadata = load_sticker_metadata(sticker_dir)
    threshold = max(int(config.get("semantic_threshold", 1)), int(minimum_score))
    candidates: List[dict[str, Any]] = []
    for sticker in stickers:
        meta = metadata.get(sticker.name, {})
        if isinstance(meta, str):
            meta = {"description": meta}
        if not isinstance(meta, dict):
            meta = {}
        if meta.get("is_sticker") is False:
            continue
        try:
            weight = max(0.0, float(meta.get("weight", 1.0) or 1.0))
        except (TypeError, ValueError):
            weight = 1.0
        if weight <= 0.0:
            continue
        score = _score_sticker(meta, hint, proactive, context)
        feedback_score = max(0.3, get_sticker_score(sticker.stem, feedback_state))
        weighted_score = score * weight * feedback_score
        if weighted_score < threshold:
            continue
        candidates.append(
            {
                "score": weighted_score,
                "path": sticker,
                "meta": meta,
                "summary": render_sticker_semantic_summary(meta),
            }
        )
    candidates.sort(key=lambda item: (-int(item["score"]), str(item["path"])))
    return candidates[: max(1, int(limit))]


def _parse_sticker_choice_payload(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            return ""
        try:
            data = json.loads(match.group(0))
        except Exception:
            return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("pick", "") or "").strip()


async def choose_sticker_for_context(
    sticker_dir: Path,
    *,
    mood: str,
    context: str,
    proactive: bool,
    plugin_config: Any,
    call_ai_api: Optional[Callable[[list[dict[str, Any]]], Awaitable[Any]]] = None,
    draft_reply: str = "",
    current_visual_summary: str = "",
    preferred_sticker: str = "",
    skills_root: Optional[Path] = None,
    minimum_score: int = 2,
    excluded_stickers: Optional[List[str]] = None,
    feedback_state: Optional[dict[str, Any]] = None,
) -> Path | None:
    candidates = rank_sticker_candidates(
        sticker_dir,
        mood=mood,
        context=context,
        proactive=proactive,
        plugin_config=plugin_config,
        skills_root=skills_root,
        minimum_score=minimum_score,
        limit=8,
        feedback_state=feedback_state,
    )
    if excluded_stickers:
        excluded_set = {Path(n).stem for n in excluded_stickers} | set(excluded_stickers)
        filtered = [c for c in candidates if Path(c["path"]).stem not in excluded_set]
        if filtered:
            candidates = filtered
    candidates = candidates[:6]
    if preferred_sticker:
        preferred_path = resolve_sticker_path(sticker_dir, preferred_sticker)
        if preferred_path is not None and all(Path(item["path"]) != preferred_path for item in candidates):
            metadata = load_sticker_metadata(sticker_dir)
            meta = metadata.get(preferred_path.name, {}) if isinstance(metadata, dict) else {}
            if not isinstance(meta, dict):
                meta = {}
            candidates.append(
                {
                    "score": 1,
                    "path": preferred_path,
                    "meta": meta,
                    "summary": render_sticker_semantic_summary(meta),
                }
            )
    if not candidates:
        return None
    if call_ai_api is None or len(candidates) == 1:
        return Path(candidates[0]["path"])

    rendered_candidates = []
    for item in candidates:
        path = Path(item["path"])
        rendered_candidates.append(
            {
                "file": path.name,
                "score": int(item["score"]),
                "summary": str(item.get("summary", "") or path.stem),
                "meta": item.get("meta", {}),
            }
        )
    review_messages = [
        {
            "role": "system",
            "content": (
                "你是表情包发送判定器。"
                "从候选里挑最合适的一张；如果这轮其实不该发图，就返回 NONE。"
                "优先避免图中文字、画面意思、适用/不适用场景和当前上下文冲突。"
                "只输出 JSON：{\"pick\":\"文件名或NONE\",\"reason\":\"...\"}。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"当前用户消息：{str(context or '').strip()[:240] or '[EMPTY]'}\n"
                f"你准备回复：{str(draft_reply or '').strip()[:120] or '[EMPTY]'}\n"
                f"当前图片/表情包语义：{str(current_visual_summary or '').strip()[:200] or '[EMPTY]'}\n"
                f"是否主动发出：{bool(proactive)}\n"
                f"候选：{json.dumps(rendered_candidates, ensure_ascii=False)}"
            ),
        },
    ]
    try:
        raw = await call_ai_api(review_messages)
    except Exception:
        return Path(candidates[0]["path"])
    picked = _parse_sticker_choice_payload(raw)
    if not picked or picked.upper() == "NONE":
        return None
    for item in candidates:
        if Path(item["path"]).name == picked or Path(item["path"]).stem == picked:
            return Path(item["path"])
    return None


def select_sticker(
    sticker_dir: Path,
    *,
    mood: str,
    context: str,
    proactive: bool,
    plugin_config: Any,
    skills_root: Optional[Path] = None,
    allow_fallback: bool = True,
    minimum_score: int = 1,
) -> str:
    sticker_dir = resolve_sticker_dir(sticker_dir)
    candidates = rank_sticker_candidates(
        sticker_dir,
        mood=mood,
        context=context,
        proactive=proactive,
        plugin_config=plugin_config,
        skills_root=skills_root,
        minimum_score=minimum_score,
    )
    if candidates:
        return str(candidates[0]["path"])

    if not allow_fallback:
        return ""
    stickers = list_local_sticker_files(sticker_dir)
    return str(stickers[0]) if stickers else ""


# ---------------------------------------------------------------------------
# AgentTool builders
# ---------------------------------------------------------------------------

def build_select_sticker_tool(
    sticker_dir: Path,
    plugin_config: Any,
    skills_root: Optional[Path] = None,
) -> AgentTool:
    async def _handler(mood: str, context: str, proactive: bool = False) -> str:
        return select_sticker(
            Path(sticker_dir),
            mood=mood,
            context=context,
            proactive=proactive,
            plugin_config=plugin_config,
            skills_root=skills_root,
        )

    return AgentTool(
        name="select_sticker",
        description=SELECT_STICKER_DESCRIPTION,
        parameters={
                "type": "object",
                "properties": {
                    "mood": {"type": "string", "description": "结构化表情标签，格式：情绪标签|场景标签"},
                    "context": {"type": "string", "description": "当前对话的一句话摘要"},
                    "proactive": {"type": "boolean", "description": "是否主动发出"},
                },
            "required": ["mood", "context"],
        },
        handler=_handler,
    )


def build_understand_sticker_tool(
    vision_caller: Optional[Any],
) -> AgentTool:
    async def _handler(
        image_index: int = 1,
        image_urls: Optional[List[str]] = None,
    ) -> str:
        runtime, effective_vision_caller = _extract_runtime_and_vision_caller(vision_caller)
        if effective_vision_caller is None and runtime is None:
            return "图像识别未启用，请先配置视觉模型。"

        current_image_urls = list(image_urls or [])
        if not current_image_urls:
            current_image_urls = get_current_image_urls()
        if not current_image_urls:
            return "当前上下文没有可分析的图片。"

        idx = max(1, int(image_index) if isinstance(image_index, int) else 1)
        if idx > len(current_image_urls):
            idx = len(current_image_urls)
        image_url = current_image_urls[-idx]
        return await understand_sticker({}, image_url, runtime or effective_vision_caller)

    return AgentTool(
        name="understand_sticker",
        description=(
            "快速理解当前图片/表情包的语义。"
            "当你需要一句话判断这张图表达了什么情绪、动作或适用场景时调用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "image_index": {
                    "type": "integer",
                    "description": "要分析哪一张图，1=最近一张",
                },
            },
        },
        handler=_handler,
        enabled=lambda: vision_caller is not None,
    )


def build_analyze_image_tool(
    vision_caller: Optional[Any],
    web_search_handler: Optional[Callable[[str], Awaitable[str]]] = None,
) -> AgentTool:
    def _extract_focus_text(text: str) -> str:
        raw = str(text or "").strip()
        if not raw:
            return ""
        start_marker = "# 当前需要回应的最新消息"
        end_marker = "# 当前状态"
        if start_marker in raw and end_marker in raw:
            section = raw.split(start_marker, 1)[1]
            section = section.split(end_marker, 1)[0]
            lines = [line.strip() for line in section.splitlines() if line.strip()]
            if lines:
                return lines[-1]
        if "- 对方刚刚说:" in raw:
            section = raw.split("- 对方刚刚说:", 1)[1]
            lines = [line.strip() for line in section.splitlines() if line.strip()]
            if lines:
                return lines[0]
        return raw

    async def _handler(
        query: str = "",
        image_index: int = 1,
        task: str = "describe",
        detail: str = "brief",
        focus: str = "auto",
        web_lookup: bool = True,
        target_language: str = "中文",
        image_urls: Optional[List[str]] = None,
        refresh: bool = False,
    ) -> str:
        runtime, effective_vision_caller = _extract_runtime_and_vision_caller(vision_caller)
        if effective_vision_caller is None and runtime is None:
            return "图像识别未启用，请先配置视觉模型。"

        current_image_urls = list(image_urls or [])
        if not current_image_urls:
            current_image_urls = get_current_image_urls()
        if not current_image_urls:
            return "当前上下文没有可分析的图片。"

        idx = max(1, int(image_index) if isinstance(image_index, int) else 1)
        if idx > len(current_image_urls):
            idx = len(current_image_urls)
        image_url = current_image_urls[-idx]

        task_mode = str(task or "describe").strip().lower()
        detail_mode = str(detail or "brief").strip().lower()
        focus_mode = str(focus or "auto").strip().lower()
        q = str(query or "").strip()
        user_text = get_current_image_text()
        user_focus_text = _extract_focus_text(user_text)
        target_lang = str(target_language or "中文").strip() or "中文"
        refresh_mode = bool(refresh)
        translation_mode = task_mode == "translate"
        person_focus = task_mode == "identify" or focus_mode == "person"
        effective_focus = "person" if person_focus else focus_mode
        cache_payload = {
            "version": "analyze_image_v2",
            "task": task_mode,
            "detail": detail_mode,
            "focus": effective_focus,
            "web_lookup": bool(web_lookup),
            "target_language": normalize_cache_text(target_lang if translation_mode else ""),
            "query": normalize_cache_text(q),
            "context": normalize_cache_text(user_focus_text),
        }
        cache_key = build_image_cache_key(image_url, cache_payload)

        async def _finalize_result(result_text: str) -> str:
            final_text = str(result_text or "").strip()
            if final_text:
                await set_cached_image_result(cache_key, final_text, meta=cache_payload)
            return final_text

        if not refresh_mode:
            cached_result = await get_cached_image_result(cache_key)
            if cached_result:
                return cached_result

        if translation_mode:
            prompt = (
                "请直接读取图片中的文字并翻译。"
                f"目标语言：{target_lang}。\n"
                "输出要求：\n"
                "1. 按阅读顺序分条输出。\n"
                "2. 每条严格使用两行：原文N：... / 译文N：...\n"
                "3. 可在原文中补充简短标签，如“旁白”“气泡”“拟声词”。\n"
                "4. 看不清的内容写“[无法辨认]”。\n"
                "5. 如果没有识别到可翻译文字，直接输出“未识别到可翻译文字”。\n"
                "6. 只输出结果，不要额外解释。"
            )
            if q:
                prompt += f"\n用户要求：{q}"
            if user_focus_text:
                prompt += f"\n随图上下文：{user_focus_text}"
            if runtime is not None:
                result, _mode = await analyze_images_with_route_or_fallback(
                    runtime=runtime,
                    prompt=prompt,
                    image_refs=[image_url],
                    fallback_vision_caller=effective_vision_caller,
                )
                return await _finalize_result(result)
            return await _finalize_result(await effective_vision_caller.describe(prompt, image_url))

        # --- build base prompt ---
        if person_focus and detail_mode == "detailed":
            base_prompt = (
                "请用中文详细分析图中人物，优先给出人数、性别倾向、年龄段、穿着、动作、"
                "表情与情绪，并补充人物之间关系与场景，避免臆测具体身份，控制在180字以内。"
            )
        elif person_focus:
            base_prompt = (
                "请用中文聚焦人物做识别：人数、外观特征、穿着、动作与情绪，避免臆测身份，控制在80字以内。"
            )
        elif focus_mode == "scene" and detail_mode == "detailed":
            base_prompt = (
                "请用中文详细分析场景信息，重点描述环境、物体布局、行为事件与氛围，避免臆测身份，控制在180字以内。"
            )
        elif focus_mode == "scene":
            base_prompt = (
                "请用中文聚焦场景做识别：环境、关键物体、正在发生的事与氛围，控制在80字以内。"
            )
        elif detail_mode == "detailed":
            base_prompt = (
                "请用中文详细分析这张图片，包含主体对象、关键细节、情绪氛围、"
                "可能场景与可执行建议，控制在180字以内。"
            )
        else:
            base_prompt = (
                "请用中文简洁描述这张图片的核心内容与情绪，控制在60字以内。"
            )

        prompt = base_prompt
        if q:
            prompt += f"\n请重点回答：{q}"
        if user_focus_text:
            prompt += (
                "\n用户随图文字上下文：\n"
                f"{user_focus_text}\n"
                "请结合这段文字辅助判断图片含义，尤其是动漫角色、作品梗或名词线索。"
            )
        if web_lookup and web_search_handler and effective_vision_caller is not None and hasattr(effective_vision_caller, "describe_with_tools"):
            tool_prompt = IDENTIFY_IMAGE_PROMPT
            if q:
                tool_prompt += f"\n用户问题：{q}"
            if user_focus_text:
                tool_prompt += f"\n随图文字：{user_focus_text}"
            tools = [
                {
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "description": "联网检索角色和作品信息",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string"},
                            },
                            "required": ["query"],
                        },
                    },
                }
            ]

            async def _tool_handler(name: str, args: Dict[str, Any]) -> str:
                if name != "web_search":
                    return ""
                query_text = str((args or {}).get("query", "") or "").strip()
                if not query_text:
                    return ""
                return await web_search_handler(query_text)

            tool_result = await effective_vision_caller.describe_with_tools(
                prompt=tool_prompt,
                image_url=image_url,
                tools=tools,
                tool_handler=_tool_handler,
                max_steps=4,
            )
            if str(tool_result or "").strip():
                return await _finalize_result(str(tool_result))
        if runtime is not None:
            visual_desc, _mode = await analyze_images_with_route_or_fallback(
                runtime=runtime,
                prompt=prompt,
                image_refs=[image_url],
                fallback_vision_caller=effective_vision_caller,
            )
        else:
            visual_desc = await effective_vision_caller.describe(prompt, image_url)
        if not visual_desc:
            return ""
        if not web_lookup or web_search_handler is None:
            return await _finalize_result(visual_desc)

        seed = " ".join(filter(None, [q, user_focus_text, visual_desc[:100]])).strip()
        web_result = await web_search_handler(seed or "图片角色识别")
        web_text = str(web_result or "").strip()
        if not web_text:
            return await _finalize_result(visual_desc)

        final_prompt = (
            "你将看到图片和联网搜索结果，请综合判断图中人物可能是谁、出自什么作品。"
            "要求：先给一句图片观察，再给2-3个候选（角色+作品），并标注可能性高/中/低；"
            "如果不确定要明确说明。控制在180字以内。\n"
            f"联网结果：\n{web_text[:1000]}"
        )
        if runtime is not None:
            final_answer, _mode = await analyze_images_with_route_or_fallback(
                runtime=runtime,
                prompt=final_prompt,
                image_refs=[image_url],
                fallback_vision_caller=effective_vision_caller,
            )
        else:
            final_answer = await effective_vision_caller.describe(final_prompt, image_url)
        return await _finalize_result(str(final_answer or visual_desc))

    return AgentTool(
        name="analyze_image",
        description=(
            "分析当前对话中的图片内容，可用于图中文字翻译、人物/角色识别或场景解读。"
            "当用户发图、要求翻译图片文字、问图中是谁或让你解读图片含义时调用。"
            "image_index=1 表示最近一张图，2 表示倒数第二张。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "你希望重点分析的问题，例如“这图里的角色是谁”或“把这页漫画翻译成中文”",
                },
                "image_index": {
                    "type": "integer",
                    "description": "要分析哪一张图，1=最近一张",
                },
                "task": {
                    "type": "string",
                    "description": "任务类型：describe=描述图片，identify=识别角色，translate=翻译图中文字",
                    "enum": ["describe", "identify", "translate"],
                },
                "detail": {
                    "type": "string",
                    "description": "分析粒度：brief 或 detailed",
                    "enum": ["brief", "detailed"],
                },
                "focus": {
                    "type": "string",
                    "description": "分析重点：auto / person / scene",
                    "enum": ["auto", "person", "scene"],
                },
                "web_lookup": {
                    "type": "boolean",
                    "description": "是否联网查询，默认 true",
                },
                "target_language": {
                    "type": "string",
                    "description": "翻译任务的目标语言，默认中文",
                },
                "refresh": {
                    "type": "boolean",
                    "description": "是否强制刷新并忽略同图缓存；用户要求重新识别、重新翻译或刷新时设为 true",
                },
            },
        },
        handler=_handler,
        enabled=lambda: vision_caller is not None,
    )



def build_curate_sticker_tool(
    sticker_dir: Path,
    plugin_config: Any,
) -> AgentTool:
    _CURATE_DESCRIPTION = (
        "\u6574\u7406\u8868\u60c5\u5305\u5e93\u3002\u53ef\u7528\u4e8e\u67e5\u770b\u6240\u6709\u8868\u60c5\u5305\u7684\u72b6\u6001\u5e76\u8c03\u6574\u6bcf\u5f20\u56fe\u7684\u6743\u91cd\u3002"
        "\u6743\u91cd 0=\u7981\u7528\uff08\u4e0d\u518d\u53d1\u9001\uff09\uff0c1=\u6b63\u5e38\uff0c2=\u4f18\u5148\u53d1\u9001\u3002"
        "\u53ef\u7528\u4e8e\u4e3b\u52a8\u8bdc\u65ad\u54ea\u4e9b\u8868\u60c5\u5305\u8d28\u91cf\u5dee\u6216\u4e0d\u5408\u9002\uff0c\u548c\u54ea\u4e9b\u6548\u679c\u597d\u5e94\u591a\u53d1\u3002"
    )

    async def _handler(
        action: str = "review",
        updates: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        act = str(action or "review").strip().lower()
        resolved_dir = resolve_sticker_dir(sticker_dir)
        if not resolved_dir.exists():
            return '{"error": "sticker_dir_not_found"}'

        if act == "review":
            metadata = load_sticker_metadata(resolved_dir)
            feedback_state = None
            try:
                from nonebot_plugin_personification.core.sticker_feedback import load_sticker_feedback
                feedback_state = await load_sticker_feedback()
            except Exception:
                pass
            items = []
            for fname, meta in metadata.items():
                if fname == "_meta":
                    continue
                if not isinstance(meta, dict):
                    continue
                if meta.get("is_sticker") is False:
                    continue
                entry: Dict[str, Any] = {
                    "file": fname,
                    "weight": round(float(meta.get("weight", 1.0) or 1.0), 2),
                    "description": str(meta.get("description", "") or "")[:60],
                    "mood_tags": list(meta.get("mood_tags", []) or [])[:3],
                    "scene_tags": list(meta.get("scene_tags", []) or [])[:3],
                    "proactive_send": bool(meta.get("proactive_send", False)),
                }
                if feedback_state:
                    fb_items = dict(feedback_state.get("items", {}) or {})
                    stem = fname.rsplit(".", 1)[0]
                    fb = fb_items.get(stem) or fb_items.get(fname)
                    if isinstance(fb, dict):
                        entry["sent_count"] = int(fb.get("sent_count", fb.get("send_count", 0)) or 0)
                        entry["positive_count"] = int(fb.get("positive_count", 0) or 0)
                        entry["feedback_score"] = round(get_sticker_score(stem, feedback_state), 2)
                items.append(entry)
            return json.dumps({"total": len(items), "stickers": items}, ensure_ascii=False)

        if act == "set_weights":
            if not updates or not isinstance(updates, list):
                return '{"error": "updates must be a list of {file, weight}"}'
            metadata = load_sticker_metadata(resolved_dir)
            changed = []
            for upd in updates:
                if not isinstance(upd, dict):
                    continue
                fname = str(upd.get("file", "") or "").strip()
                if not fname:
                    continue
                raw_w = upd.get("weight")
                try:
                    w = max(0.0, min(3.0, float(raw_w if raw_w is not None else 1.0)))
                except (TypeError, ValueError):
                    continue
                if fname not in metadata or not isinstance(metadata[fname], dict):
                    continue
                metadata[fname]["weight"] = round(w, 2)
                changed.append({"file": fname, "weight": round(w, 2)})
            if changed:
                import asyncio as _asyncio
                from nonebot_plugin_personification.core.sticker_library import save_sticker_metadata
                await save_sticker_metadata(resolved_dir, metadata)
            return json.dumps({"updated": len(changed), "changes": changed}, ensure_ascii=False)

        return '{"error": "unknown action, use review or set_weights"}'

    return AgentTool(
        name="curate_sticker_library",
        description=_CURATE_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "review=\u67e5\u770b\u8868\u60c5\u5305\u5e93\u72b6\u6001\uff1bset_weights=\u6279\u91cf\u8bbe\u7f6e\u6743\u91cd",
                    "enum": ["review", "set_weights"],
                },
                "updates": {
                    "type": "array",
                    "description": "set_weights \u65f6\u9700\u8981\uff0c\u5217\u8868\u683c\u5f0f: [{\"file\": \"xxx.jpg\", \"weight\": 1.5}]",
                    "items": {
                        "type": "object",
                        "properties": {
                            "file": {"type": "string"},
                            "weight": {"type": "number"},
                        },
                        "required": ["file", "weight"],
                    },
                },
            },
            "required": ["action"],
        },
        handler=_handler,
    )

