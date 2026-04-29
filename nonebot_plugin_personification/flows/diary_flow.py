from __future__ import annotations

import asyncio
import json
import random
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from ..agent.inner_state import load_inner_state, update_state_from_diary
from ..core.emotion_state import describe_group_emotion_memory, load_emotion_state
from ..skills.skillpacks.image_gen.scripts.impl import generate_image as generate_codex_image


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


def _trim_qzone_content(text: str, *, max_chars: int = 90) -> str:
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


async def _maybe_generate_qzone_image_marker(
    *,
    tool_caller: Any,
    image_prompt: str,
    logger: Any,
) -> str:
    prompt = str(image_prompt or "").strip()
    if not prompt or tool_caller is None:
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


async def _build_qzone_post_with_optional_image(
    *,
    content: str,
    image_prompt: str,
    tool_caller: Any,
    logger: Any,
) -> str:
    text = _trim_qzone_content(content)
    if not text:
        return ""
    image_marker = await _maybe_generate_qzone_image_marker(
        tool_caller=tool_caller,
        image_prompt=image_prompt,
        logger=logger,
    )
    return f"{text}{image_marker}"


async def get_recent_chat_context(bot: Any, logger: Any) -> str:
    """Sample recent group messages as diary context."""
    try:
        group_list = await bot.get_group_list()
        if not group_list:
            return ""

        selected_groups = random.sample(group_list, min(2, len(group_list)))
        context_parts = []
        for group in selected_groups:
            group_id = group["group_id"]
            group_name = group.get("group_name", str(group_id))

            try:
                messages = await bot.get_group_msg_history(group_id=group_id, count=50)
            except Exception as e:
                logger.warning(f"[diary] get group history failed: {group_id}: {e}")
                continue

            if not messages or "messages" not in messages:
                continue

            lines = []
            for msg in messages["messages"]:
                sender_name = msg.get("sender", {}).get("nickname", "未知")
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

            if lines:
                context_parts.append(f"群聊 {group_name} 的最近聊天：\n" + "\n".join(lines))

        return "\n\n".join(context_parts)
    except Exception as e:
        logger.error(f"[diary] get recent chat context failed: {e}")
        return ""


async def _generate_once(
    system_prompt: Any,
    user_prompt: str,
    *,
    call_ai_api: Callable[..., Awaitable[Optional[str]]],
) -> str:
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    result = await call_ai_api(messages)
    if not result:
        return ""
    return clean_generated_text(result)


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
    tool_caller: Any = None,
    data_dir: Optional[Path] = None,
) -> str:
    """Generate a short Qzone post from recent chat context."""
    system_prompt = load_prompt()
    chat_context = await get_recent_chat_context(bot, logger)
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
        "1. 正文 20-80 个中文字符，短一点，像日常碎碎念。\n"
        "2. 语气符合当前角色设定，不要像总结报告、日报、作文或公告。\n"
        "3. 可以写聊天里看到的小事、自己的心情、突然冒出的念头。\n"
        "4. 不要列条目、不要标题、不要 hashtag、不要说自己是 AI。\n"
        "5. image_prompt 只有在这条说说适合配一张氛围图时填写英文画面描述；不适合就留空。"
    )

    if chat_context:
        rich_prompt = (
            "请结合下面这些最近聊天内容，写一条带一点生活感的 QQ 空间说说。\n"
            "不要逐条复述聊天记录，而是把它们消化成自己的感受、吐槽或碎碎念。\n\n"
            f"{chat_context}\n\n"
            f"{emotion_hint}\n\n"
            f"{base_requirements}"
        )
        raw_rich_result = await _generate_once(
            system_prompt,
            rich_prompt,
            call_ai_api=call_ai_api,
        )
        payload = _extract_json_object(raw_rich_result)
        rich_result = ""
        if payload:
            rich_result = await _build_qzone_post_with_optional_image(
                content=str(payload.get("content", "") or ""),
                image_prompt=str(payload.get("image_prompt", "") or ""),
                tool_caller=tool_caller,
                logger=logger,
            )
        elif raw_rich_result:
            rich_result = _trim_qzone_content(raw_rich_result)
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
        "请直接写一条自然的短说说，像是角色自己随手发的碎碎念。\n\n"
        f"{base_requirements}"
    )
    raw_result = await _generate_once(
        system_prompt,
        basic_prompt,
        call_ai_api=call_ai_api,
    )
    payload = _extract_json_object(raw_result)
    if payload:
        result = await _build_qzone_post_with_optional_image(
            content=str(payload.get("content", "") or ""),
            image_prompt=str(payload.get("image_prompt", "") or ""),
            tool_caller=tool_caller,
            logger=logger,
        )
    else:
        result = _trim_qzone_content(raw_result)
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
    data_dir: Optional[Path] = None,
    tool_caller: Any = None,
) -> str:
    """根据近期聊天与内心状态决定是否主动发一条更日常的空间动态。"""
    system_prompt = load_prompt()
    chat_context = await get_recent_chat_context(bot, logger)
    if not chat_context:
        return ""

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

    decision_prompt = (
        "你现在在考虑要不要发一条 QQ 空间说说。\n"
        "请基于最近聊天内容、当前心情和挂念，判断你此刻是不是真的有想发动态的冲动。\n"
        "输出严格 JSON：{\"action\":\"skip|post\",\"content\":\"正文\",\"image_prompt\":\"可选英文配图提示词\",\"reason\":\"极短原因\"}。\n\n"
        f"当前心情：{mood}\n"
        f"当前精力：{energy}\n"
        f"最近挂念：\n{pending_block}\n\n"
        f"近期群情绪记忆：\n{emotion_hint or '- 暂无明显群情绪记忆'}\n\n"
        f"最近聊天片段：\n{chat_context}\n\n"
        "要求：\n"
        "1. 如果没有明确想说的话，action=skip。\n"
        "2. 如果想发，action=post，并给出 content。\n"
        "3. 正文 15-70 个中文字符，像真人随手发的空间碎碎念，别写长篇大段。\n"
        "4. 可以带一点吐槽、感慨、突然想到的念头，但不要列表、不要标题、不要 hashtag。\n"
        "5. 不要为了发而发，不要重复最近已经说过很多遍的话题。\n"
        "6. 如果适合配图，image_prompt 写英文画面描述，要求贴合人设和正文氛围；不适合就留空。"
    )
    result = await _generate_once(
        system_prompt,
        decision_prompt,
        call_ai_api=call_ai_api,
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
            logger=logger,
        )
    if result.startswith("POST|"):
        return _trim_qzone_content(result.split("|", 1)[1])
    return ""
