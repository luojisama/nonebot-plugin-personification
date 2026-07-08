from __future__ import annotations

import asyncio
import base64
import inspect
import json
import random
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from ..agent.inner_state import load_inner_state, update_state_from_diary
from ..core.agent_bridge import run_text_agent
from ..core.context_policy import strip_response_control_markers
from ..core.data_store import get_data_store
from ..core.emotion_state import describe_group_emotion_memory, load_emotion_state
from ..core.persona_profile import load_persona_profile
from ..core.response_review import is_agent_reply_ooc, rewrite_agent_reply_ooc
from ..core.sticker_library import (
    list_local_sticker_files,
    load_sticker_metadata,
    render_sticker_semantic_summary,
    resolve_sticker_dir,
)
from ..skills.skillpacks.image_gen.scripts.impl import generate_image as generate_codex_image


# 发空间不是群聊对话，而是角色本人写一条要发到自己 QQ 空间的动态。
# 这里必须强调"继续保持人设"，否则旧措辞"你不在群聊中扮演角色"会被模型理解成
# "本轮不用维持人设"，导致空间说说脱离角色设定。本 guard 只负责约束输出格式。
_FLOW_OUTPUT_GUARD = (
    "\n\n本轮不是群聊对话，而是你（角色本人）在写一条要发到自己 QQ 空间的个人动态/说说。"
    "请继续严格保持你的人设、性格和一贯的说话风格，用第一人称、像自己随手发的口吻写，"
    "不要因为这是发动态就变成中立的旁白腔或通用助手腔。"
    "只是把输出格式换成这条用户消息里要求的纯文本/JSON，"
    "不要使用 <status>/<think>/<action>/<output>/<message> 等思维链 XML 包装。"
)

_QZONE_CASUAL_TONE_DISCIPLINE = (
    "额外口吻纪律：不要写成镜头旁白、散文描写、状态报告或“我正在陈述一个画面”的完整句。"
    "少用连续铺景的叙述开头（比如一上来交代时间、天气、外面声音、杯子里的东西），"
    "也不要堆“光、风、疲惫、安静、突然发现”这类意象去撑气氛。"
    "优先像手机上随手敲的一句：短、轻、可以半截、可以吐槽，不必把前因后果说圆。"
    "不要写成整齐的二段式机灵句，尤其别用“脑子/胃/手/嘴先开始……”这类器官拟人、先后对仗、"
    "看似俏皮但模板感很重的句式；宁可更普通、更像真的随手敲。"
)


def filter_sensitive_content(text: str) -> str:
    """Filter obviously unsafe fragments from sampled group history."""
    sensitive_patterns = [
        r"自杀",
        r"跳楼",
        r"毒品",
        r"开盒",
        r"爆照",
        r"约炮",
        r"政治敏感",
        r"血腥",
        r"色情",
    ]

    filtered_text = text
    for pattern in sensitive_patterns:
        filtered_text = re.sub(pattern, "**", filtered_text, flags=re.IGNORECASE)

    if len(filtered_text.strip()) < 2:
        return ""
    return filtered_text


# 发说说的切入角度池：每次随机挑 1-2 个做软性引导，促使话题/题材在多次发布间轮换，
# 避免老是围绕群里正在热议的同一件事写命题作文。
_DIARY_ANGLE_POOL = [
    "眼前的一个生活小观察",
    "突然冒出来的一个念头或联想",
    "此刻的心情或身体感觉",
    "窗外/天气/光线/声音之类的环境细节",
    "刚吃的、想吃的或随手摸到的东西",
    "一个无聊的小吐槽",
    "今天的游戏/动漫里一个很小的细节",
    "一条轻新闻勾起的即时反应",
    "对某件小事的一句反问或牢骚",
    "一个没说完、半截就停下的画面",
]


def _pick_diversity_hint() -> str:
    """随机挑选 1-2 个切入角度，生成软性题材轮换提示。"""
    k = random.choice((1, 2))
    angles = random.sample(_DIARY_ANGLE_POOL, min(k, len(_DIARY_ANGLE_POOL)))
    joined = "或".join(f"「{a}」" for a in angles)
    return (
        f"这次优先从 {joined} 这类角度切入，不必围绕群里正在热议的那个主话题；"
        "在不同题材间轮换（生活/心情/游戏/动漫/新闻/环境各换着来），别连续几条都黏在同一件事上。"
    )


def _spread_sample(lines: list[str], k: int) -> list[str]:
    """跨整个时间窗均匀稀疏取样，保留原有先后顺序。

    直接取最近连续 N 条往往集中在同一段热议话题上；均匀抽样能让窗口内不同时段、
    不同话题的发言都露头，给选题更多样的素材。
    """
    if k <= 0 or not lines:
        return []
    if len(lines) <= k:
        return list(lines)
    if k == 1:
        return [lines[-1]]
    # 含首尾的均匀取样：保证最新一条（也包含最早一条）一定入选。
    last = len(lines) - 1
    picked_indices = sorted({round(i * last / (k - 1)) for i in range(k)})
    return [lines[i] for i in picked_indices]


def clean_generated_text(text: str) -> str:
    """Strip model-side thinking/status wrappers."""
    cleaned = re.sub(r"<status.*?>.*?</\s*status\s*>", "", text, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<think.*?>.*?</\s*think\s*>", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"</?\s*output.*?>", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"</?\s*message.*?>", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text).rstrip("`").strip()
    try:
        payload = json.loads(text)
    except Exception:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return None
        try:
            payload = json.loads(match.group(0))
        except Exception:
            return None
    return payload if isinstance(payload, dict) else None


def _trim_qzone_content(text: str, *, max_chars: int = 50) -> str:
    cleaned = clean_generated_text(text)
    cleaned = re.sub(r"^(POST|SKIP)\s*[|：:]\s*", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"#[^#\s]{1,24}", "", cleaned).strip()
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) <= max_chars:
        return cleaned
    candidate = cleaned[:max_chars]
    for index in range(len(candidate) - 1, max(0, len(candidate) - 24), -1):
        if candidate[index] in "。！？!?\n":
            return candidate[: index + 1].strip()
    return candidate.rstrip("，、,. ") + "..."


def _compact_qzone_history_content(content: str) -> str:
    cleaned = re.sub(r"\[IMAGE_B64\][A-Za-z0-9+/=\r\n]+\[/IMAGE_B64\]", "", str(content or ""))
    cleaned = _trim_qzone_content(cleaned, max_chars=80)
    return cleaned.strip()


def _load_recent_qzone_posts(limit: int = 8) -> list[str]:
    try:
        state = get_data_store().load_sync("qzone_post_state")
    except Exception:
        return []
    if not isinstance(state, dict):
        return []
    candidates: list[Any] = []
    recent = state.get("recent_contents")
    if isinstance(recent, list):
        candidates.extend(recent)
    if state.get("last_content"):
        candidates.append(state.get("last_content"))
    items: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        raw = item.get("content") if isinstance(item, dict) else item
        text = _compact_qzone_history_content(str(raw or ""))
        if not text or text in seen:
            continue
        seen.add(text)
        items.append(text)
    return items[-max(1, int(limit)) :]


def _format_recent_qzone_posts(posts: list[str]) -> str:
    if not posts:
        return "- 暂无"
    return "\n".join(f"- {item}" for item in posts[-8:])


def _format_qzone_quota_block(quota: Optional[dict]) -> str:
    """把月度额度快照渲染成给 agent 自我节奏控制的提示块。"""
    if not isinstance(quota, dict) or not quota:
        return ""
    used = int(quota.get("used", 0) or 0)
    limit = int(quota.get("limit", 0) or 0)
    remaining = int(quota.get("remaining", max(0, limit - used)) or 0)
    days_left = int(quota.get("days_left", 0) or 0)
    days_in_month = int(quota.get("days_in_month", 0) or 0)
    lines = [
        f"- 本月发空间额度：上限 {limit} 条，已发 {used} 条，剩余 {remaining} 条。",
    ]
    if days_in_month:
        lines.append(f"- 本月共 {days_in_month} 天，还剩 {days_left} 天。")
    if limit > 0 and days_in_month > 0:
        ideal_rate = limit / days_in_month
        elapsed_days = max(1, days_in_month - days_left + 1)
        expected_used = ideal_rate * elapsed_days
        if remaining <= 0:
            pace = "额度已用完，本轮必须 skip。"
        elif remaining <= max(1, round(ideal_rate * max(1, days_left) * 0.5)):
            pace = "额度偏紧，请明显更克制：只在真的很想发、且内容有意思时才 post，否则 skip。"
        elif used > expected_used + 1:
            pace = "发得比平均节奏快了些，适当收一收，没有强烈冲动就 skip。"
        else:
            pace = "额度还算宽裕，但也别为了发而发；有真想发的就发，没有就 skip。"
        lines.append(f"- 节奏建议：{pace}")
    return "你的发空间额度与节奏（请像真人一样自己把控，不要把额度发满当任务）：\n" + "\n".join(lines)


def _normalize_similarity_text(text: str) -> str:
    cleaned = _compact_qzone_history_content(text)
    cleaned = re.sub(r"[\s，。！？!?、,.；;：:~…·'\"“”‘’（）()【】\[\]#]+", "", cleaned)
    return cleaned.lower()


def _char_bigrams(text: str) -> set[str]:
    value = _normalize_similarity_text(text)
    if len(value) < 2:
        return {value} if value else set()
    return {value[index : index + 2] for index in range(len(value) - 1)}


def _longest_common_substring_len(left: str, right: str) -> int:
    a = _normalize_similarity_text(left)
    b = _normalize_similarity_text(right)
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for ca in a:
        curr = [0]
        for index, cb in enumerate(b, start=1):
            value = prev[index - 1] + 1 if ca == cb else 0
            curr.append(value)
            if value > best:
                best = value
        prev = curr
    return best


def _qzone_post_similarity(left: str, right: str) -> float:
    a = _char_bigrams(left)
    b = _char_bigrams(right)
    if not a or not b:
        return 0.0
    return (2.0 * len(a & b)) / (len(a) + len(b))


def _is_too_similar_to_recent_qzone_post(content: str, recent_posts: list[str]) -> bool:
    """字面级去重兜底（prompt 内已要求 LLM 主题级避开重复，这里做硬护栏）。
    收紧后阈值：公共子串 ≥6 / 公共子串 ≥4 + 相似度 ≥0.35。
    """
    text = _normalize_similarity_text(content)
    if len(text) < 6:
        return False
    for recent in recent_posts or []:
        other = _normalize_similarity_text(recent)
        if not other:
            continue
        if text == other or text in other or other in text:
            return True
        longest = _longest_common_substring_len(text, other)
        if longest >= 6:
            return True
        if longest >= 4 and _qzone_post_similarity(text, other) >= 0.35:
            return True
    return False


_QZONE_STICKER_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}


def _normalize_qzone_sticker_match_text(text: str) -> str:
    value = str(text or "").strip().lower()
    value = re.sub(r"\s+", "", value)
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", value)


def _qzone_sticker_overlap_score(context: str, candidate: str) -> int:
    current = _normalize_qzone_sticker_match_text(context)
    target = _normalize_qzone_sticker_match_text(candidate)
    if len(current) < 2 or len(target) < 2:
        return 0
    if current in target or target in current:
        return 4
    max_span = min(8, len(target))
    for span in range(max_span, 1, -1):
        for start in range(0, len(target) - span + 1):
            if target[start : start + span] in current:
                return 1
    return 0


def _score_qzone_sticker_candidate(meta: dict[str, Any], context: str) -> float:
    if not isinstance(meta, dict):
        meta = {}
    if meta.get("is_sticker") is False:
        return -1.0
    try:
        weight = max(0.0, float(meta.get("weight", 1.0) or 1.0))
    except (TypeError, ValueError):
        weight = 1.0
    if weight <= 0.0:
        return -1.0
    score = weight
    if meta.get("proactive_send") is True:
        score += 2.0
    style = str(meta.get("style", "anime") or "anime").strip().lower()
    if style == "anime":
        score += 1.0
    elif style == "meme":
        score += 0.3
    summary = render_sticker_semantic_summary(meta)
    score += _qzone_sticker_overlap_score(context, summary)
    score += _qzone_sticker_overlap_score(context, str(meta.get("description", "") or ""))
    score += _qzone_sticker_overlap_score(context, str(meta.get("use_hint", "") or ""))
    score -= min(3, _qzone_sticker_overlap_score(context, str(meta.get("avoid_hint", "") or "")))
    return score


def _select_qzone_sticker_image(
    *,
    plugin_config: Any,
    content: str,
    image_prompt: str,
) -> Path | None:
    sticker_root = getattr(plugin_config, "personification_sticker_path", "data/stickers")
    sticker_dir = resolve_sticker_dir(sticker_root)
    files = [
        path
        for path in list_local_sticker_files(sticker_dir, include_gif=False)
        if path.suffix.lower() in _QZONE_STICKER_IMAGE_SUFFIXES
    ]
    if not files:
        return None
    context = "\n".join(part for part in (str(content or ""), str(image_prompt or "")) if part.strip())
    metadata = load_sticker_metadata(sticker_dir)
    candidates: list[tuple[float, Path]] = []
    for file_path in files:
        meta = metadata.get(file_path.name, {}) if isinstance(metadata, dict) else {}
        score = _score_qzone_sticker_candidate(meta, context)
        if score < 0:
            continue
        candidates.append((score, file_path))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], item[1].name))
    top = candidates[: min(6, len(candidates))]
    if len(top) == 1:
        return top[0][1]
    weights = [max(0.1, item[0]) for item in top]
    return random.choices([item[1] for item in top], weights=weights, k=1)[0]


async def _maybe_build_qzone_sticker_image_marker(
    *,
    plugin_config: Any,
    content: str,
    image_prompt: str,
    logger: Any,
) -> str:
    if plugin_config is None:
        return ""
    try:
        selected = await asyncio.to_thread(
            _select_qzone_sticker_image,
            plugin_config=plugin_config,
            content=content,
            image_prompt=image_prompt,
        )
    except Exception as exc:
        if logger is not None:
            logger.debug(f"[qzone] select sticker image failed: {exc}")
        return ""
    if selected is None:
        return ""
    try:
        payload = await asyncio.to_thread(selected.read_bytes)
    except Exception as exc:
        if logger is not None:
            logger.debug(f"[qzone] read sticker image failed: {selected}: {exc}")
        return ""
    if not payload:
        return ""
    b64 = base64.b64encode(payload).decode("ascii")
    if logger is not None:
        logger.info(f"[qzone] use local sticker image as post attachment: {selected.name}")
    return f"\n[IMAGE_B64]{b64}[/IMAGE_B64]"


async def _maybe_generate_qzone_image_marker(
    *,
    tool_caller: Any,
    image_prompt: str,
    content: str = "",
    plugin_config: Any = None,
    logger: Any,
) -> str:
    prompt = str(image_prompt or "").strip()
    if not prompt:
        return ""
    sticker_marker = await _maybe_build_qzone_sticker_image_marker(
        plugin_config=plugin_config,
        content=content,
        image_prompt=prompt,
        logger=logger,
    )
    if sticker_marker:
        return sticker_marker
    if tool_caller is None:
        return ""
    try:
        result = await generate_codex_image(
            prompt,
            tool_caller=tool_caller,
            size="1024x1024",
            image_model="gpt-image-2",
        )
    except Exception as exc:
        logger.warning(f"[qzone] Codex 配图生成失败: {exc}")
        return ""
    if not isinstance(result, dict):
        return ""
    b64 = str(result.get("b64_json", "") or "").strip()
    if not b64:
        error = str(result.get("error", "") or "").strip()
        if error:
            logger.warning(f"[qzone] Codex 配图生成失败: {error}")
        return ""
    return f"\n[IMAGE_B64]{b64}[/IMAGE_B64]"


# 营业感叹腔/网络流行语 tic：『(也)太……了吧 / X爆了 / 绝了 / 谁懂 / 笑死 / yyds』等，
# 这类口号式收尾会让说说显得网感营业、千篇一律，需要改写成平铺直叙。
_NET_SLANG_TIC_RE = re.compile(
    r"也?太[一-鿿]{0,8}了吧|[一-鿿]爆了|绝了|谁懂|笑死|绷不住|好家伙|yyds",
    re.IGNORECASE,
)

_QZONE_STIFF_TIC_RE = re.compile(
    r"([脑胃手嘴眼心身体][子睛脚]?[没不还已]?[在先]?.{0,10}[催拐喊动馋困累]|"
    r".{1,12}[，,；;].{0,8}先.{0,12}了|今天只想.{2,18})"
)


async def _rewrite_qzone_net_slang(
    text: str,
    *,
    tool_caller: Any,
    registry: Any = None,
    plugin_config: Any = None,
    agent_max_steps: int = 4,
    persona_system: Any = "",
    timeout: float = 8.0,
    logger: Any = None,
) -> str:
    """把带营业感叹腔/网络流行语的说说改写成平铺直叙的一句。"""
    if tool_caller is None:
        return ""
    messages: list[dict[str, Any]] = []
    persona = str(persona_system or "").strip()
    if persona:
        messages.append({"role": "system", "content": persona[:1200]})
    messages.append(
        {
            "role": "system",
            "content": (
                "下面这条 QQ 空间说说带有营业感叹腔/网络流行语"
                "（如『也太……了吧 / ……爆了 / 绝了 / 谁懂 / 笑死 / yyds』）。"
                "用你自己的口吻改写成平铺直叙的一句日常碎碎念，去掉所有感叹营业腔和网络流行语，"
                "只描述那个画面或念头本身，不喊口号、不强行制造情绪；如果原句像散文旁白或状态报告，"
                "也一起改成更随手、更口语的一句，12-50 字。只输出改写后的句子。"
            ),
        }
    )
    messages.append({"role": "user", "content": str(text or "").strip()[:300]})
    try:
        if registry is not None:
            return await run_text_agent(
                messages=messages,
                plugin_config=plugin_config,
                logger=logger,
                tool_caller=tool_caller,
                registry=registry,
                max_steps=agent_max_steps,
                trigger_reason="qzone_net_slang_rewrite",
                chat_intent_hint="qzone_net_slang_rewrite",
            )
        response = await asyncio.wait_for(tool_caller.chat_with_tools(messages, [], False), timeout=timeout)
    except Exception:
        return ""
    return str(getattr(response, "content", "") or "").strip()


async def _rewrite_qzone_stiff_tic(
    text: str,
    *,
    tool_caller: Any,
    registry: Any = None,
    plugin_config: Any = None,
    agent_max_steps: int = 4,
    persona_system: Any = "",
    timeout: float = 8.0,
    logger: Any = None,
) -> str:
    """把模板化的机灵句/器官拟人句改成更真一点的随手短句。"""
    if tool_caller is None:
        return ""
    messages: list[dict[str, Any]] = []
    persona = str(persona_system or "").strip()
    if persona:
        messages.append({"role": "system", "content": persona[:1200]})
    messages.append(
        {
            "role": "system",
            "content": (
                "下面这条 QQ 空间说说有点像模板化的机灵句：二段式、先后对仗、器官拟人、"
                "或“今天只想……”这种太工整的口播感。请保留角色口吻和原本的小念头，"
                "改写成更像真人随手敲的一句日常碎碎念。可以更普通、更短、更松散，"
                "不要解释为什么改，不要补设定，不要喊口号，12-45 个中文字符。只输出改写后的句子。"
            ),
        }
    )
    messages.append({"role": "user", "content": str(text or "").strip()[:300]})
    try:
        if registry is not None:
            return await run_text_agent(
                messages=messages,
                plugin_config=plugin_config,
                logger=logger,
                tool_caller=tool_caller,
                registry=registry,
                max_steps=agent_max_steps,
                trigger_reason="qzone_stiff_tic_rewrite",
                chat_intent_hint="qzone_stiff_tic_rewrite",
            )
        response = await asyncio.wait_for(tool_caller.chat_with_tools(messages, [], False), timeout=timeout)
    except Exception:
        return ""
    return str(getattr(response, "content", "") or "").strip()


async def _review_qzone_post(
    text: str,
    *,
    tool_caller: Any,
    registry: Any = None,
    plugin_config: Any = None,
    agent_max_steps: int = 4,
    persona_system: Any = "",
    logger: Any,
) -> str:
    """对生成的说说做去 AI 腔 + 去营业感叹腔审阅。

    1) 引入工具调用循环后，正文最容易漏出"根据搜索结果/查了一下/参考链接"这类搜索腔，
       用群聊同款的 `is_agent_reply_ooc` 正则 + `rewrite_agent_reply_ooc` 重写兜住；
       重写失败则丢弃该条（宁缺勿发）。
    2) 再兜一层『(也)太……了吧 / X爆了 / 绝了 / yyds』等营业感叹腔，改写成平铺直叙。
    3) 对看起来过度工整的 QZone 机灵句做一次轻改写，避免“越改越怪”的模板感。
    """
    if not text or tool_caller is None:
        return text
    if is_agent_reply_ooc(text):
        rewritten = await rewrite_agent_reply_ooc(
            tool_caller=tool_caller,
            original_text=text,
            persona_system=str(persona_system or "")[:1200],
            output_mode="chat_short",
        )
        if not rewritten:
            if logger is not None:
                logger.info(f"[qzone] OOC rewrite failed, drop post: {text}")
            return ""
        text = _trim_qzone_content(rewritten)
    if text and _NET_SLANG_TIC_RE.search(text):
        toned = await _rewrite_qzone_net_slang(
            text,
            tool_caller=tool_caller,
            registry=registry,
            plugin_config=plugin_config,
            agent_max_steps=agent_max_steps,
            persona_system=persona_system,
            logger=logger,
        )
        toned = _trim_qzone_content(toned)
        if toned and not _NET_SLANG_TIC_RE.search(toned):
            text = toned
        elif logger is not None:
            logger.info(f"[qzone] net-slang rewrite ineffective, keeping: {text}")
    if text and _QZONE_STIFF_TIC_RE.search(text):
        toned = await _rewrite_qzone_stiff_tic(
            text,
            tool_caller=tool_caller,
            registry=registry,
            plugin_config=plugin_config,
            agent_max_steps=agent_max_steps,
            persona_system=persona_system,
            logger=logger,
        )
        toned = _trim_qzone_content(toned)
        if toned and not _QZONE_STIFF_TIC_RE.search(toned):
            text = toned
        elif logger is not None:
            logger.info(f"[qzone] stiff-tic rewrite ineffective, keeping: {text}")
    return text


async def _build_qzone_post_with_optional_image(
    *,
    content: str,
    image_prompt: str,
    tool_caller: Any,
    registry: Any = None,
    plugin_config: Any = None,
    agent_max_steps: int = 4,
    logger: Any,
    recent_posts: Optional[list[str]] = None,
    persona_system: Any = "",
) -> str:
    text = _trim_qzone_content(content)
    if not text:
        return ""
    text = await _review_qzone_post(
        text,
        tool_caller=tool_caller,
        registry=registry,
        plugin_config=plugin_config,
        agent_max_steps=agent_max_steps,
        persona_system=persona_system,
        logger=logger,
    )
    if not text:
        return ""
    if _is_too_similar_to_recent_qzone_post(text, recent_posts or []):
        logger.info(f"[qzone] skip generated post because it repeats recent content: {text}")
        return ""
    image_marker = await _maybe_generate_qzone_image_marker(
        tool_caller=tool_caller,
        image_prompt=image_prompt,
        content=text,
        plugin_config=plugin_config,
        logger=logger,
    )
    return f"{text}{image_marker}"


async def get_recent_chat_context(bot: Any, logger: Any) -> str:
    """Sample recent non-bot group messages as diary context."""
    try:
        group_list = await bot.get_group_list()
        if not group_list:
            return ""
        bot_id = str(getattr(bot, "self_id", "") or "")

        # 多采几个群、每群跨时间窗稀疏取样，避免说说素材被单一群的单一热议话题主导。
        selected_groups = random.sample(group_list, min(3, len(group_list)))
        context_parts = []
        for group in selected_groups:
            group_id = group["group_id"]
            group_name = group.get("group_name", str(group_id))

            try:
                messages = await bot.get_group_msg_history(group_id=group_id, count=40)
            except Exception as e:
                logger.warning(f"[diary] get group history failed: {group_id}: {e}")
                continue

            if not messages or "messages" not in messages:
                continue

            lines = []
            for msg in messages["messages"]:
                sender = msg.get("sender", {}) if isinstance(msg.get("sender"), dict) else {}
                sender_id = str(sender.get("user_id") or msg.get("user_id") or "").strip()
                if bot_id and sender_id == bot_id:
                    continue
                sender_name = sender.get("nickname", "未知")
                raw_msg = msg.get("message", "")
                content = ""

                if isinstance(raw_msg, list):
                    text_parts = []
                    for seg in raw_msg:
                        if isinstance(seg, dict) and seg.get("type") == "text":
                            text_parts.append(str((seg.get("data") or {}).get("text", "")))
                    content = "".join(text_parts)
                elif isinstance(raw_msg, str):
                    content = re.sub(r"\[CQ:[^\]]+\]", "", raw_msg)

                safe_content = filter_sensitive_content(content)
                if safe_content.strip():
                    lines.append(f"{sender_name}: {safe_content.strip()}")

            # 跨整个窗口均匀稀疏取样，让不同时段/话题都露头，而非只盯最近一段热聊。
            lines = _spread_sample(lines, 12)
            if lines:
                context_parts.append(f"群聊 {group_name} 的最近聊天：\n" + "\n".join(lines))

        return "\n\n".join(context_parts)
    except Exception as e:
        logger.error(f"[diary] get recent chat context failed: {e}")
        return ""


def _render_qzone_persona_snapshot(system_prompt: Any) -> str:
    """从人设里抽取身份/风格规则，拼成发空间用的人设快照。

    复用群聊同款 `load_persona_profile`（支持 str / YAML 头 / dict 三种人设形态，
    会兜底 DEFAULT_PERSONA_PROFILE）。只取 identity_rules + style_rules——
    group-chat 的 boundary_rules（接话/[SILENCE] 之类）对"写一条个人动态"不适用，
    带进来反而会干扰。这样即便发空间没有群上下文、人设是 dict 只用了 system 字段，
    身份与说话风格约束也能被显式注入，避免说说脱离人设。
    """
    profile = load_persona_profile(system_prompt)
    identity = profile.get("identity_rules", []) if isinstance(profile, dict) else []
    style = profile.get("style_rules", []) if isinstance(profile, dict) else []
    identity_lines = "\n".join(f"- {str(item)}" for item in list(identity)[:4])
    style_lines = "\n".join(f"- {str(item)}" for item in list(style)[:4])
    if not identity_lines and not style_lines:
        return ""
    return (
        "\n\n## 人设快照（发空间也要严格保持）\n"
        f"[身份一致性]\n{identity_lines or '- 保持角色一致，不提及自己是 AI/模型/程序'}\n"
        f"[语气风格]\n{style_lines or '- 用角色一贯的口语化风格，避免客服腔和通用助手腔'}"
    )


async def _generate_once(
    system_prompt: Any,
    user_prompt: str,
    *,
    plugin_config: Any = None,
    call_ai_api: Callable[..., Awaitable[Optional[str]]],
    use_builtin_search: bool = False,
    tool_caller: Any = None,
    registry: Any = None,
    logger: Any = None,
    agent_max_steps: int = 4,
) -> str:
    # 在格式 guard 之外再注入人设快照，强化发空间时的角色一致性。
    suffix = _render_qzone_persona_snapshot(system_prompt) + _FLOW_OUTPUT_GUARD
    if isinstance(system_prompt, str):
        system_text = system_prompt + suffix
    elif isinstance(system_prompt, dict):
        copied = dict(system_prompt)
        sys_text = str(copied.get("system", "") or "")
        copied["system"] = sys_text + suffix
        system_text = copied
    else:
        system_text = system_prompt
    messages = [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_prompt},
    ]
    supports_builtin_search = True
    try:
        signature = inspect.signature(call_ai_api)
        supports_builtin_search = (
            "use_builtin_search" in signature.parameters
            or any(param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values())
        )
    except (TypeError, ValueError):
        supports_builtin_search = True
    _qz_token = None
    try:
        from ..core.llm_context import reset_llm_context, set_llm_context

        _qz_token = set_llm_context(purpose="qzone_diary")
    except Exception:
        _qz_token = None
    try:
        if tool_caller is not None and registry is not None:
            # 与群聊同等：走完整 Agent 管线，而不是轻量工具循环。
            try:
                result = await run_text_agent(
                    messages=messages,
                    plugin_config=plugin_config,
                    logger=logger,
                    tool_caller=tool_caller,
                    registry=registry,
                    max_steps=agent_max_steps,
                    use_builtin_search_hint=use_builtin_search,
                    trigger_reason="qzone_diary",
                    chat_intent_hint="qzone_diary",
                )
            except Exception as exc:
                if logger is not None:
                    logger.warning(f"[diary] full Agent failed, skip direct-model fallback: {exc}")
                result = ""
        elif supports_builtin_search:
            result = await call_ai_api(messages, use_builtin_search=use_builtin_search)
        else:
            result = await call_ai_api(messages)
    finally:
        if _qz_token is not None:
            try:
                reset_llm_context(_qz_token)
            except Exception:
                pass
    if not result:
        return ""
    # 兜底：若 LLM 仍输出思维链 XML，剥除标签再走 clean_generated_text
    stripped = strip_response_control_markers(result)
    return clean_generated_text(stripped or result)


def _schedule_diary_state_update(
    *,
    diary_text: str,
    tool_caller: Any,
    data_dir: Optional[Path],
    logger: Any,
) -> None:
    if not diary_text or tool_caller is None or data_dir is None:
        return
    asyncio.create_task(
        update_state_from_diary(
            diary_text,
            Path(data_dir),
            tool_caller,
            logger,
        )
    )


async def generate_ai_diary(
    bot: Any,
    *,
    load_prompt: Callable[[], Any],
    call_ai_api: Callable[..., Awaitable[Optional[str]]],
    logger: Any,
    plugin_config: Any = None,
    tool_caller: Any = None,
    data_dir: Optional[Path] = None,
    registry: Any = None,
    agent_max_steps: int = 4,
) -> str:
    """Generate a short Qzone post from recent chat context."""
    system_prompt = load_prompt()
    chat_context = await get_recent_chat_context(bot, logger)
    recent_posts = _load_recent_qzone_posts()
    emotion_hint = ""
    if data_dir is not None:
        try:
            emotion_state = await load_emotion_state(Path(data_dir))
            group_hints = [
                describe_group_emotion_memory(emotion_state, str(group_id))
                for group_id in list((emotion_state or {}).get("per_group", {}).keys())[:3]
            ]
            group_hints = [hint for hint in group_hints if hint]
            if group_hints:
                emotion_hint = "最近群情绪记忆：\n" + "\n".join(f"- {hint}" for hint in group_hints)
        except Exception as e:
            logger.debug(f"[diary] load emotion_state failed: {e}")

    base_requirements = (
        "请写一条自然、像真人随手发的 QQ 空间说说，不要写周记小作文。\n"
        "输出严格 JSON：{\"content\":\"正文\",\"image_prompt\":\"可选英文配图提示词\"}。\n"
        f"{_QZONE_CASUAL_TONE_DISCIPLINE}\n"
        "1. 正文 12-50 个中文字符，像随手发的一句日常碎碎念。\n"
        "2. 只抓一个很小的生活瞬间或念头，不要总结聊天、日报、作文或公告。\n"
        "3. 允许跳跃：可以半句话突然转到另一个画面，或在一个观察后接一句不相关的吐槽；不必上下文连贯。\n"
        "4. 标点可以省略，可用空格、句号、问号代替逗号；可以是反问、牢骚、发现、未说完的半句；不必有结论。\n"
        "5. 语气贴合角色，但不要互联网黑话、热梗、夸张营业感、AI 客服腔、对仗工整的总结句或文艺旁白句。\n"
        "5b. 严禁用『(也)太……了吧 / ……爆了 / ……绝了 / 谁懂啊 / 笑死 / 绷不住了 / yyds / 好耶』这类营业感叹腔收尾或起势；"
        "把这种感叹换成平铺直叙的一句话，或干脆只描述那个画面/动作本身，不喊口号、不强行制造情绪。\n"
        "5c. 避开看似俏皮但像生成器模板的表达：不要让脑子、胃、手、嘴等器官轮流上台催促，"
        "不要写“X 没在怎样，Y 先怎样了”这类过分整齐的对仗句。\n"
        "6. 不要列条目、不要标题、不要 hashtag、不要说自己是 AI。\n"
        "7. 必须避开最近说说已经反复出现的话题、题材、具体意象、食物、动作和句式；如果最近写过类似的，就彻底换一个不同的话题和角度。\n"
        "8. 触发点要小而具体，写成自己的即时反应，不要新闻播报、不要复述大家正在热议的主话题。\n"
        "9. image_prompt 只有在适合配一张日常氛围图时填写英文画面描述；不适合就留空。"
    )
    recent_block = "最近已经发过的说说，禁止复读这些内容或近似句式：\n" + _format_recent_qzone_posts(recent_posts)
    diversity_hint = _pick_diversity_hint()

    if chat_context:
        rich_prompt = (
            "下面是最近的一些聊天片段，仅作为氛围参考。\n"
            "不要复述、也不要总结大家正在热议的那个主话题；可以从里面挑一个不显眼的小细节、"
            "边角料或一闪而过的念头当触发点，写成自己此刻的碎碎念。如果聊天内容没有特别想接的，"
            "完全可以抛开它，写自己当下的心情或一个生活小观察。\n\n"
            f"{diversity_hint}\n\n"
            f"{chat_context}\n\n"
            f"{emotion_hint}\n\n"
            f"{recent_block}\n\n"
            f"{base_requirements}"
        )
        raw_rich_result = await _generate_once(
            system_prompt,
            rich_prompt,
            plugin_config=plugin_config,
            call_ai_api=call_ai_api,
            use_builtin_search=True,
            tool_caller=tool_caller,
            registry=registry,
            logger=logger,
            agent_max_steps=agent_max_steps,
        )
        payload = _extract_json_object(raw_rich_result)
        rich_result = ""
        if payload:
            rich_result = await _build_qzone_post_with_optional_image(
                content=str(payload.get("content", "") or ""),
                image_prompt=str(payload.get("image_prompt", "") or ""),
                tool_caller=tool_caller,
                registry=registry,
                plugin_config=plugin_config,
                agent_max_steps=agent_max_steps,
                logger=logger,
                recent_posts=recent_posts,
                persona_system=system_prompt,
            )
        elif raw_rich_result:
            rich_result = _trim_qzone_content(raw_rich_result)
            if _is_too_similar_to_recent_qzone_post(rich_result, recent_posts):
                logger.info(f"[qzone] skip rich raw post because it repeats recent content: {rich_result}")
                rich_result = ""
        if rich_result:
            _schedule_diary_state_update(
                diary_text=rich_result,
                tool_caller=tool_caller,
                data_dir=data_dir,
                logger=logger,
            )
            return rich_result

        logger.warning("[diary] rich prompt generation failed, fallback to basic prompt")

    basic_prompt = (
        "请直接写一条自然的短说说，像是角色自己随手发的碎碎念。\n"
        "触发点可以是自己当下的心情、一个生活小观察，或借助常识从今天的游戏、动漫、轻新闻里挑一个细节；"
        "重点是每次换着题材来，别老写同一类东西。\n\n"
        f"{diversity_hint}\n\n"
        f"{recent_block}\n\n"
        f"{base_requirements}"
    )
    raw_result = await _generate_once(
        system_prompt,
        basic_prompt,
        plugin_config=plugin_config,
        call_ai_api=call_ai_api,
        use_builtin_search=True,
        tool_caller=tool_caller,
        registry=registry,
        logger=logger,
        agent_max_steps=agent_max_steps,
    )
    payload = _extract_json_object(raw_result)
    if payload:
        result = await _build_qzone_post_with_optional_image(
            content=str(payload.get("content", "") or ""),
            image_prompt=str(payload.get("image_prompt", "") or ""),
            tool_caller=tool_caller,
            registry=registry,
            plugin_config=plugin_config,
            agent_max_steps=agent_max_steps,
            logger=logger,
            recent_posts=recent_posts,
            persona_system=system_prompt,
        )
    else:
        result = _trim_qzone_content(raw_result)
        if _is_too_similar_to_recent_qzone_post(result, recent_posts):
            logger.info(f"[qzone] skip basic raw post because it repeats recent content: {result}")
            result = ""
    if result:
        _schedule_diary_state_update(
            diary_text=result,
            tool_caller=tool_caller,
            data_dir=data_dir,
            logger=logger,
        )
    return result


async def maybe_generate_proactive_qzone_post(
    bot: Any,
    *,
    load_prompt: Callable[[], Any],
    call_ai_api: Callable[..., Awaitable[Optional[str]]],
    logger: Any,
    plugin_config: Any = None,
    data_dir: Optional[Path] = None,
    tool_caller: Any = None,
    registry: Any = None,
    agent_max_steps: int = 4,
    quota: Optional[dict] = None,
) -> str:
    """根据近期聊天、内心状态与本月额度，决定是否主动发一条更日常的空间动态。"""
    system_prompt = load_prompt()
    chat_context = await get_recent_chat_context(bot, logger)
    if not chat_context:
        chat_context = "最近群聊可用文本很少；可以把今天的游戏、动漫、轻新闻或自己的状态当成触发点。"
    recent_posts = _load_recent_qzone_posts()

    inner_state = {}
    if data_dir is not None:
        try:
            inner_state = await load_inner_state(Path(data_dir))
        except Exception as e:
            logger.warning(f"[qzone] load inner_state failed: {e}")
    emotion_hint = ""
    if data_dir is not None:
        try:
            emotion_state = await load_emotion_state(Path(data_dir))
            group_hints = [
                describe_group_emotion_memory(emotion_state, str(group_id))
                for group_id in list((emotion_state or {}).get("per_group", {}).keys())[:3]
            ]
            group_hints = [hint for hint in group_hints if hint]
            if group_hints:
                emotion_hint = "\n".join(f"- {hint}" for hint in group_hints)
        except Exception as e:
            logger.debug(f"[qzone] load emotion_state failed: {e}")

    mood = str((inner_state or {}).get("mood", "平静") or "平静")
    energy = str((inner_state or {}).get("energy", "正常") or "正常")
    pending = (inner_state or {}).get("pending_thoughts", [])
    pending_lines = []
    if isinstance(pending, list):
        for item in pending[-4:]:
            if not isinstance(item, dict):
                continue
            thought = str(item.get("thought", "") or "").strip()
            if thought:
                pending_lines.append(f"- {thought}")
    pending_block = "\n".join(pending_lines) if pending_lines else "- 无明显挂念"

    quota_block = _format_qzone_quota_block(quota)
    decision_prompt = (
        "你现在在考虑要不要发一条 QQ 空间说说。\n"
        "请基于最近聊天内容、当前心情和挂念，以及你本月还剩多少发空间额度，"
        "判断你此刻是不是真的有想发动态的冲动、以及现在发是否合适。\n"
        "输出严格 JSON：{\"action\":\"skip|post\",\"content\":\"正文\",\"image_prompt\":\"可选英文配图提示词\",\"reason\":\"极短原因\"}。\n\n"
        f"当前心情：{mood}\n"
        f"当前精力：{energy}\n"
        f"最近挂念：\n{pending_block}\n\n"
        f"近期群情绪记忆：\n{emotion_hint or '- 暂无明显群情绪记忆'}\n\n"
        f"最近聊天片段：\n{chat_context}\n\n"
        "最近已经发过的说说，禁止复读这些内容或近似句式：\n"
        f"{_format_recent_qzone_posts(recent_posts)}\n\n"
        + (f"{quota_block}\n\n" if quota_block else "")
        + "要求：\n"
        "1. 如果没有明确想说的话，或按上面的额度节奏建议此刻不该发，action=skip。\n"
        "2. 如果想发，action=post，并给出 content。\n"
        "3. 正文 12-50 个中文字符，像真人随手发的一句话，别写长篇大段。\n"
        "4. 只写一个小瞬间、小吐槽或突然想到的念头，不要列表、标题、hashtag 或总结腔。\n"
        "5. 不要为了发而发，不要重复最近已经说过很多遍的话题或题材，每次换着不同的话题和角度来，不要互联网黑话和热梗。\n"
        "5b. 严禁用『(也)太……了吧 / ……爆了 / ……绝了 / 谁懂啊 / 笑死 / yyds』这类营业感叹腔；改成平铺直叙或只描述画面本身，不喊口号。\n"
        f"6. {_QZONE_CASUAL_TONE_DISCIPLINE}\n"
        "7. 触发点可以是当下心情、一个生活小观察，或今天游戏、动漫、轻新闻里的一个细节，但写成自己的日常反应、不要复述群里正在热议的主话题、不要像新闻标题。\n"
        f"8. {_pick_diversity_hint()}\n"
        "9. 如果适合配图，image_prompt 写英文画面描述，要求贴合人设和正文氛围；不适合就留空。"
    )
    result = await _generate_once(
        system_prompt,
        decision_prompt,
        plugin_config=plugin_config,
        call_ai_api=call_ai_api,
        use_builtin_search=True,
        tool_caller=tool_caller,
        registry=registry,
        logger=logger,
        agent_max_steps=agent_max_steps,
    )
    if not result:
        return ""
    payload = _extract_json_object(result)
    if payload:
        if str(payload.get("action", "") or "").strip().lower() != "post":
            return ""
        return await _build_qzone_post_with_optional_image(
            content=str(payload.get("content", "") or ""),
            image_prompt=str(payload.get("image_prompt", "") or ""),
            tool_caller=tool_caller,
            registry=registry,
            plugin_config=plugin_config,
            agent_max_steps=agent_max_steps,
            logger=logger,
            recent_posts=recent_posts,
            persona_system=system_prompt,
        )
    if result.startswith("POST|"):
        text = _trim_qzone_content(result.split("|", 1)[1])
        text = await _review_qzone_post(
            text,
            tool_caller=tool_caller,
            registry=registry,
            plugin_config=plugin_config,
            agent_max_steps=agent_max_steps,
            persona_system=system_prompt,
            logger=logger,
        )
        if not text:
            return ""
        if _is_too_similar_to_recent_qzone_post(text, recent_posts):
            logger.info(f"[qzone] skip POST raw post because it repeats recent content: {text}")
            return ""
        return text
    return ""
