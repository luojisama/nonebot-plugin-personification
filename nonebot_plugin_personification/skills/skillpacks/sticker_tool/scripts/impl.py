from __future__ import annotations

import json
from contextvars import ContextVar
from pathlib import Path
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional

from nonebot_plugin_personification.agent.tool_registry import AgentTool
from nonebot_plugin_personification.core.image_result_cache import (
    build_image_cache_key,
    get_cached_image_result,
    has_refresh_hint,
    normalize_cache_text,
    set_cached_image_result,
)
from nonebot_plugin_personification.core.sticker_cache import get_sticker_files
from nonebot_plugin_personification.skills.skillpacks.vision_caller.scripts.impl import VisionCaller


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

UNDERSTAND_STICKER_PROMPT = """用一句中文描述这张表情包：角色/表情/动作（有文字也说），以及适合用在什么场合。30字以内，直接输出，不加前缀。
示例：卡通猫咪捂脸大笑，适合回应搞笑的事或接梗"""

SELECT_STICKER_DESCRIPTION = """从表情包库中语义选择一张合适的图片发出。
调用时机：当你觉得这个时候发一张表情包比说话更自然时。
参数说明：
- mood：你当前的情绪状态，如"被逗笑了""有点无语""想撒娇"
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


async def understand_sticker(
    message_segment: dict,
    image_url: str,
    vision_caller: Optional[VisionCaller],
) -> str:
    if is_gif_sticker(message_segment):
        return "[NO_REPLY]"
    if vision_caller is None:
        return ""
    return await vision_caller.describe(UNDERSTAND_STICKER_PROMPT, image_url)


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

def _stickers_json_path(sticker_dir: Path) -> Path:
    return sticker_dir / "stickers.json"


def _load_sticker_metadata(sticker_dir: Path) -> dict:
    metadata_path = _stickers_json_path(sticker_dir)
    if not metadata_path.exists():
        return {}
    try:
        loaded = json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _tokenize_semantic_text(text: str) -> List[str]:
    plain = str(text or "").strip().lower()
    if not plain:
        return []
    normalized = re.sub(r"[\s\r\n\t]+", " ", plain)
    chunks = [part for part in re.split(r"[^\w\u4e00-\u9fff]+", normalized) if part]
    tokens: List[str] = []
    for chunk in chunks:
        if len(chunk) >= 2:
            tokens.append(chunk)
        if any("\u4e00" <= ch <= "\u9fff" for ch in chunk) and len(chunk) >= 4:
            for size in (4, 3, 2):
                if len(chunk) < size:
                    continue
                for idx in range(0, len(chunk) - size + 1):
                    tokens.append(chunk[idx : idx + size])
    seen: set[str] = set()
    deduped: List[str] = []
    for token in tokens:
        if token and token not in seen:
            seen.add(token)
            deduped.append(token)
    return deduped


def _contains_semantic_overlap(haystack: str, token: str) -> bool:
    current = str(haystack or "").lower()
    needle = str(token or "").lower()
    if not current or not needle:
        return False
    return needle in current


def _score_sticker(meta: dict, mood: str, context: str, proactive: bool) -> int:
    description = str(meta.get("description", "") or "")
    mood_tags = [str(tag) for tag in (meta.get("mood_tags", []) or []) if tag]
    scene_tags = [str(tag) for tag in (meta.get("scene_tags", []) or []) if tag]
    haystack = " ".join([description, " ".join(mood_tags), " ".join(scene_tags)]).lower()
    score = 0
    mood_tokens = _tokenize_semantic_text(mood)
    context_tokens = _tokenize_semantic_text(context)

    for token in mood_tokens:
        if _contains_semantic_overlap(haystack, token):
            score += 3
    for token in context_tokens:
        if _contains_semantic_overlap(haystack, token):
            score += 2

    for tag in mood_tags:
        lowered = tag.lower()
        if _contains_semantic_overlap(str(mood).lower(), lowered):
            score += 4
        if _contains_semantic_overlap(str(context).lower(), lowered):
            score += 2
    for tag in scene_tags:
        lowered = tag.lower()
        if _contains_semantic_overlap(str(context).lower(), lowered):
            score += 5
        if _contains_semantic_overlap(str(mood).lower(), lowered):
            score += 2

    if proactive and meta.get("proactive_send") is True:
        score += 2
    if not proactive and meta.get("proactive_send") is False:
        score += 1
    if description and ("适合" in description or "用于" in description):
        score += 1
    return score


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
    sticker_dir = Path(sticker_dir)
    if not sticker_dir.exists():
        return ""

    stickers = get_sticker_files(sticker_dir)
    if not stickers:
        return ""

    if not getattr(plugin_config, "personification_sticker_semantic", True):
        return str(sorted(stickers)[0])

    config = load_sticker_config(skills_root)
    metadata = _load_sticker_metadata(sticker_dir)
    candidates: List[tuple[int, str]] = []

    for sticker in stickers:
        meta = metadata.get(sticker.name, {})
        if isinstance(meta, str):
            meta = {"description": meta}
        if not isinstance(meta, dict):
            meta = {}
        score = _score_sticker(meta, mood, context, proactive)
        threshold = max(int(config.get("semantic_threshold", 1)), int(minimum_score))
        if score >= threshold:
            candidates.append((score, str(sticker)))

    if candidates:
        candidates.sort(key=lambda item: (-item[0], item[1]))
        return candidates[0][1]

    if not allow_fallback:
        return ""
    return str(sorted(stickers)[0])


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
                "mood": {"type": "string", "description": "当前情绪状态"},
                "context": {"type": "string", "description": "当前对话的一句话摘要"},
                "proactive": {"type": "boolean", "description": "是否主动发出"},
            },
            "required": ["mood", "context"],
        },
        handler=_handler,
    )


def build_analyze_image_tool(
    vision_caller: Optional[VisionCaller],
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

    def _is_person_query(text: str) -> bool:
        q = str(text or "").strip().lower()
        if not q:
            return False
        keywords = [
            "人物", "人像", "是谁", "这人", "男女", "年龄",
            "长相", "穿着", "表情", "姿势", "发型",
            "face", "person", "people", "who",
        ]
        return any(token in q for token in keywords)

    def _is_translation_query(text: str) -> bool:
        q = str(text or "").strip().lower()
        if not q:
            return False
        keywords = [
            "翻译", "译一下", "译成", "汉化", "日文", "日语", "英文", "英语",
            "台词", "对白", "字幕", "原文", "这页", "图里字", "图中文字",
            "text", "translate", "translation", "ocr",
        ]
        return any(token in q for token in keywords)

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
        if vision_caller is None:
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
        refresh_mode = bool(refresh) or has_refresh_hint(q) or has_refresh_hint(user_focus_text)
        translation_mode = (
            task_mode == "translate"
            or _is_translation_query(q)
            or _is_translation_query(user_focus_text)
        )
        person_focus = task_mode == "identify" or focus_mode == "person" or (
            focus_mode == "auto" and _is_person_query(q)
        )
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
            return await _finalize_result(await vision_caller.describe(prompt, image_url))

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
        if web_lookup and web_search_handler and hasattr(vision_caller, "describe_with_tools"):
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

            tool_result = await vision_caller.describe_with_tools(
                prompt=tool_prompt,
                image_url=image_url,
                tools=tools,
                tool_handler=_tool_handler,
                max_steps=4,
            )
            if str(tool_result or "").strip():
                return await _finalize_result(str(tool_result))
        visual_desc = await vision_caller.describe(prompt, image_url)
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
        final_answer = await vision_caller.describe(final_prompt, image_url)
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

