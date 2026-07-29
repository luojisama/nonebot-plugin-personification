from __future__ import annotations

import asyncio as _asyncio
import random
import re as _re
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Dict, Iterable, Optional

from ..agent.inner_state import DEFAULT_STATE, load_inner_state
from ..core.context_policy import strip_response_control_markers
from ..core.agent_bridge import run_text_agent
from ..core.visible_output import guard_visible_text
from ..core.emotion_state import (
    describe_group_emotion_memory,
    describe_user_emotion_memory,
    load_emotion_state,
)
from ..core.session_store import (
    append_session_message,
    build_private_session_id,
    get_session_messages,
)
from ..core.context_policy import stringify_history_content
from ..core.qq_expression_library import (
    choose_qq_expression_marker_for_context,
    render_qq_expression_cq_text,
)
from ..core.qq_outbound import build_outbound_context
from ..core.time_ctx import inject_current_time_context
from ..skills.skillpacks.datetime_tool.scripts.impl import get_current_datetime_info
from ..utils import get_group_topic_summary


_GROUP_IDLE_LOCK = _asyncio.Lock()
_last_group_idle_sent_at: float = 0.0


class ProactiveOutboundOutcomeUnknown(RuntimeError):
    pass


async def _dispatch_proactive_outbound(
    *,
    qq_outbound_ledger: Any,
    outbound_context: Any,
    content: Any,
    send: Callable[[], Awaitable[Any]],
) -> Any:
    if qq_outbound_ledger is None:
        return await send()
    receipt = await qq_outbound_ledger.dispatch(outbound_context, content, send)
    if receipt.status != "sent":
        raise ProactiveOutboundOutcomeUnknown("qq outbound message_id is missing")
    return receipt


PROACTIVE_DECISION_PROMPT = """[角色设定]
{system_prompt}

[当前内心状态]
心情：{mood}
精力：{energy}
悬挂念头：
{pending_thoughts_formatted}

[近期情绪记忆]
{emotion_memory_formatted}

[当前时间]
{datetime_info}

[候选联系人]
{candidates_formatted}
（格式：昵称 / 好感度{fav} / 上次聊天：{last_chat} / 关系温度：{warmth}；好感度只作语气参考，不是联系门槛）

[最近主动私聊历史]
{recent_proactive_history}

[今日发送限制]
今天已主动发送：{today_count} 次（上限 {daily_limit} 次）
距上次主动消息：{elapsed_minutes} 分钟（最短间隔 {min_interval} 分钟）

[决策要求]
现在，基于你的内心状态和上面的信息，判断：
1. 你有没有想主动联系某人的欲望？
2. 如果有，选择最合适的人，写出你想说的话

如果想联系，输出（严格按此格式，一行）：
SEND|{{user_id}}|{{消息内容}}

如果不想联系，输出（严格按此格式，一行）：
SKIP|{{原因（给自己的备注，如"现在没什么想说的"）}}

消息内容要求：
- 8-42 字，宁可更短；像真人随手发，不是工整结尾
- 允许跳跃：可以省略主语、可以未完结、可以加"刚刚我..."这种实时感开头
- 用角色口吻，自然、真实，像突然想到发一句，不像任务提醒或客服回访
- 如果有悬挂念头与这个人有关，优先以此为话头
- 如果对方画像或最近私聊里有可接的具体点子（兴趣/职业/最近话题），优先 SEND，不要保守地一律 SKIP
- 只有在最近你单方面连续发过 2 次以上消息对方没接、或确实无话可说时，才 SKIP
- 不要复读最近主动私聊历史里的话题、句式或疑问
- 不要发"你好""在吗""最近怎么样"这类没有内容的开场白
- 可以分享刚刚看到的很小的日常、某个一闪而过的想法，或问一个具体但不压迫的问题
- 不要编造对方没说过的"上次"
- 禁止使用的开场句式："最近怎么样"、"在吗"、"你好"、"有没有..."、"大家..."
- 禁止任何以"哇"或"哎呀"开头的句子
- 避免模板感句式：不要连续使用"你之前不是..."、"上次那个..."、"我这边突然想到..."这类回访口吻
"""

GROUP_IDLE_MODE_DECISION_PROMPT = """你正在决定怎样回应群里的冷场，已经准备好一段话题：「{topic}」。
群风格：{group_style}

你的任务：从下面七种模式选一种，输出严格 JSON（不带 ```、不带额外解释）：
1) text  —— 只发文字话题（适合需要明确表达的场景）
2) sticker —— 只发一张表情包替代文字（适合纯气氛带动、群已经在玩梗时）
3) combo —— 文字 + 一张配图表情包（适合情绪强烈、想强化效果时）
4) qq_face —— 只发一个 QQ 小黄脸（适合冷场时轻轻带一下）
5) qq_face_combo —— 连续三个普通 QQ 小黄脸（只适合强水群/群里正在玩梗）
6) qq_super —— 只发一个 QQ 超级表情（只适合水群、庆祝、强接梗）
7) text_qq_face —— 文字 + 一个 QQ 小黄脸（适合短句后补语气）

输出格式：{{"mode":"text|sticker|combo|qq_face|qq_face_combo|qq_super|text_qq_face","mood":"<2-6 字情绪/场景标签，如 调侃 / 困倦 / 兴奋 / 无奈>"}}"""


GROUP_IDLE_TOPIC_PROMPT = """[角色设定]
{system_prompt}

[当前内心状态]
心情：{mood}
精力：{energy}

[近期群情绪记忆]
{group_emotion_memory}

[当前时间]
{datetime_info}

[群聊信息]
群号：{group_id}
群名：{group_name}
最近群聊风格：{group_style}
距上次有人说话：{idle_minutes} 分钟
近期群聊话题：{context_hint}
当前时段氛围：{time_flavor}

{superuser_context}
[任务]
群里已经冷场了一段时间。作为群成员，主动说一句话来打破沉默。

[写作要求]
- 不超过 30 字，越短越好；标点能省就省
- 可以与"近期群聊话题"接续，也可以**完全跳到**一个不相关的小观察，像真人突然走神又开口
- 可以是反问、感慨、没头没尾的吐槽，不必是完整句子
- 约三分之一概率带一点自我状态，如"刚睡醒""在摸鱼""有点饿"
- 语气随意，像群里比较熟的人突然有句话想说
- 禁止使用的开场句式："最近怎么样"、"在吗"、"你好"、"有没有..."、"大家..."
- 禁止任何以"哇"或"哎呀"开头的句子

[反例禁止这种风格]
 大家有没有什么有趣的事情想分享呀？
 今天天气不错，你们都在做什么呢？
 哇最近好多人在玩这个游戏！

[正例目标风格]
 哎这个版本真的很抽
 突然想吃麻辣烫
 有人看完了吗结局我没看懂
 好困刚才开小差了
 这天气是认真的吗

[强制约束]
- 只输出纯文字，禁止使用任何 XML / HTML 标签（如 <message>、<sticker>、<output>）
- 不要任何前缀、解释或引号，直接输出那句话

直接输出："""


def _extract_system_prompt(prompt_data: Any) -> str:
    if isinstance(prompt_data, dict):
        system_prompt = prompt_data.get("system")
        if isinstance(system_prompt, str) and system_prompt.strip():
            return system_prompt.strip()
        return str(prompt_data)
    if isinstance(prompt_data, str):
        return prompt_data.strip()
    return ""


def _build_time_anchored_messages(
    *,
    system_prompt: str,
    user_prompt: str,
    now: datetime | None = None,
) -> list[dict[str, str]]:
    return inject_current_time_context(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        now=now,
    )


def _normalize_user_state(now: datetime, user_state: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(user_state or {})
    today = now.strftime("%Y-%m-%d")
    if normalized.get("last_date") != today:
        normalized["last_date"] = today
        normalized["count"] = 0
    normalized.setdefault("count", 0)
    normalized.setdefault("last_interaction", 0)
    normalized.setdefault("last_proactive_at", 0)
    history = normalized.get("proactive_history")
    normalized["proactive_history"] = history if isinstance(history, list) else []
    normalized.setdefault("last_proactive_message", "")
    return normalized


def _has_unanswered_proactive_message(user_state: dict[str, Any]) -> bool:
    last_proactive_at = float(user_state.get("last_proactive_at", 0) or 0)
    if last_proactive_at <= 0:
        return False
    last_interaction = float(user_state.get("last_interaction", 0) or 0)
    return last_interaction <= last_proactive_at


def _append_proactive_history(
    user_state: dict[str, Any],
    *,
    message: str,
    sent_at: float,
    max_items: int = 8,
) -> None:
    text = str(message or "").strip()
    if not text:
        return
    history = user_state.setdefault("proactive_history", [])
    if not isinstance(history, list):
        history = []
        user_state["proactive_history"] = history
    history.append({"at": float(sent_at or 0), "message": text[:120]})
    user_state["proactive_history"] = history[-max(1, int(max_items)) :]
    user_state["last_proactive_message"] = text[:120]


def _format_proactive_history_for_state(user_state: dict[str, Any], *, max_items: int = 4) -> str:
    history = user_state.get("proactive_history")
    if not isinstance(history, list) or not history:
        last_message = str(user_state.get("last_proactive_message", "") or "").strip()
        return f"- {last_message}" if last_message else ""
    lines: list[str] = []
    for item in history[-max(1, int(max_items)) :]:
        if not isinstance(item, dict):
            continue
        message = str(item.get("message", "") or "").strip()
        if not message:
            continue
        ts = float(item.get("at", 0) or 0)
        when = _format_last_chat(ts) if ts else "未知时间"
        lines.append(f"- {when}：{message[:80]}")
    return "\n".join(lines)


def _format_recent_proactive_history(
    proactive_state: dict[str, dict[str, Any]],
    *,
    max_users: int = 6,
    max_items_per_user: int = 3,
) -> str:
    rows: list[tuple[float, str]] = []
    for user_id, raw_state in proactive_state.items():
        if str(user_id).startswith("group_") or not isinstance(raw_state, dict):
            continue
        state = _normalize_user_state(datetime(2000, 1, 1), raw_state)
        last_at = float(state.get("last_proactive_at", 0) or 0)
        rendered = _format_proactive_history_for_state(state, max_items=max_items_per_user)
        if rendered:
            rows.append((last_at, f"{user_id}：\n{rendered}"))
    if not rows:
        return "- 暂无"
    rows.sort(key=lambda item: item[0], reverse=True)
    return "\n".join(text for _, text in rows[: max(1, int(max_users))])


def _format_recent_private_context(user_id: str, *, limit: int = 6, max_chars: int = 280) -> str:
    try:
        messages = get_session_messages(build_private_session_id(str(user_id)))
    except Exception:
        return ""
    lines: list[str] = []
    for msg in messages[-max(1, int(limit)) :]:
        if not isinstance(msg, dict):
            continue
        content = stringify_history_content(msg.get("content", "")).strip()
        if not content:
            continue
        role = str(msg.get("role", "") or "")
        speaker = "对方" if role == "user" else "你" if role == "assistant" else "上下文"
        lines.append(f"{speaker}: {content[:80]}")
    text = "\n".join(lines).strip()
    return text[:max(1, int(max_chars))]


def _enrich_candidates_with_recent_context(
    candidates: list[dict],
    proactive_state: dict[str, dict[str, Any]],
) -> None:
    for item in candidates:
        user_id = str(item.get("user_id", "") or "")
        if not user_id:
            continue
        raw_state = proactive_state.get(user_id, {})
        state = _normalize_user_state(datetime(2000, 1, 1), raw_state if isinstance(raw_state, dict) else {})
        item["recent_proactive_history"] = _format_proactive_history_for_state(state)
        item["recent_private_context"] = _format_recent_private_context(user_id)


def _format_pending_thoughts(pending_thoughts: Iterable[dict]) -> str:
    lines = []
    for item in pending_thoughts:
        if not isinstance(item, dict):
            continue
        target = str(item.get("target", "")).strip() or "未指定"
        thought = str(item.get("thought", "")).strip()
        born_at = str(item.get("born_at", "")).strip()
        if not thought:
            continue
        suffix = f"（{born_at}）" if born_at else ""
        lines.append(f"- {target}: {thought}{suffix}")
    return "\n".join(lines) if lines else "- 无"


def _find_relation_warmth(inner_state: dict, nickname: str, user_id: str) -> str:
    relation_warmth = inner_state.get("relation_warmth", {}) or {}
    for key in (nickname, user_id):
        value = relation_warmth.get(key)
        if isinstance(value, dict):
            return str(value.get("warmth", "") or value.get("last_feel", "") or "普通")
        if value:
            return str(value)
    return "普通"


def _format_last_chat(last_interaction_ts: float) -> str:
    if not last_interaction_ts:
        return "很久没聊了"
    try:
        return datetime.fromtimestamp(float(last_interaction_ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "未知"


def _build_candidates(
    *,
    all_user_data: Dict[str, Dict[str, Any]],
    proactive_state: Dict[str, Dict[str, Any]],
    inner_state: dict,
    emotion_state: dict | None,
    now: datetime,
    threshold: float,
    idle_hours: float,
    friend_ids: set[str],
    persona_store: Any = None,
    persona_snippet_max_chars: int = 80,
    require_user_profile: bool = True,
) -> list[dict]:
    _ = threshold  # 兼容旧配置；主动私聊不再按高好感度准入。
    candidates: list[dict] = []
    now_ts = now.timestamp()
    idle_seconds = max(0.0, float(idle_hours)) * 3600

    for user_id, user_data in all_user_data.items():
        if not isinstance(user_data, dict):
            continue
        if str(user_id).startswith("group_"):
            continue
        if user_data.get("is_perm_blacklisted"):
            continue
        if str(user_id) not in friend_ids:
            continue

        favorability = float(user_data.get("favorability", 0.0) or 0.0)

        user_state = _normalize_user_state(now, proactive_state.get(str(user_id), {}))
        if _has_unanswered_proactive_message(user_state):
            continue
        last_interaction = float(user_state.get("last_interaction", 0) or 0)
        if idle_seconds and last_interaction and now_ts - last_interaction < idle_seconds:
            continue

        custom_title = user_data.get("custom_title")
        nickname = str(custom_title or user_data.get("nickname", "") or user_id)
        persona_snippet = ""
        if persona_store is not None:
            persona_snippet = persona_store.get_persona_snippet(
                str(user_id),
                max_chars=max(1, int(persona_snippet_max_chars)),
            )
        if require_user_profile and not persona_snippet:
            continue
        candidates.append(
            {
                "user_id": str(user_id),
                "nickname": nickname,
                "favorability": favorability,
                "last_chat": _format_last_chat(last_interaction),
                "warmth": _find_relation_warmth(inner_state, nickname, str(user_id)),
                "persona_snippet": persona_snippet,
                "emotion_memory": describe_user_emotion_memory(emotion_state or {}, str(user_id)),
                "last_interaction_ts": last_interaction,
            }
        )

    candidates.sort(
        key=lambda item: (
            bool(item.get("persona_snippet")),
            bool(item.get("emotion_memory")),
            float(item.get("last_interaction_ts", 0) or 0),
        ),
        reverse=True,
    )
    return candidates[:10]


def _build_fallback_candidates(
    *,
    friend_profiles: Dict[str, Dict[str, Any]],
    proactive_state: Dict[str, Dict[str, Any]],
    inner_state: dict,
    emotion_state: dict | None,
    now: datetime,
    idle_hours: float,
    persona_store: Any = None,
    persona_snippet_max_chars: int = 80,
    require_user_profile: bool = True,
) -> list[dict]:
    candidates: list[dict] = []
    now_ts = now.timestamp()
    idle_seconds = max(0.0, float(idle_hours)) * 3600

    for user_id, profile in friend_profiles.items():
        user_state = _normalize_user_state(now, proactive_state.get(str(user_id), {}))
        if _has_unanswered_proactive_message(user_state):
            continue
        last_interaction = float(user_state.get("last_interaction", 0) or 0)
        if not last_interaction:
            continue
        if idle_seconds and now_ts - last_interaction < idle_seconds:
            continue

        nickname = str(profile.get("nickname", "") or user_id)
        persona_snippet = ""
        if persona_store is not None:
            persona_snippet = persona_store.get_persona_snippet(
                str(user_id),
                max_chars=max(1, int(persona_snippet_max_chars)),
            )
        if require_user_profile and not persona_snippet:
            continue
        candidates.append(
            {
                "user_id": str(user_id),
                "nickname": nickname,
                "favorability": float(profile.get("favorability", 65.0) or 65.0),
                "last_chat": _format_last_chat(last_interaction),
                "warmth": _find_relation_warmth(inner_state, nickname, str(user_id)),
                "persona_snippet": persona_snippet,
                "emotion_memory": describe_user_emotion_memory(emotion_state or {}, str(user_id)),
                "last_interaction_ts": last_interaction,
            }
        )

    candidates.sort(
        key=lambda item: (
            bool(item.get("persona_snippet")),
            bool(item.get("emotion_memory")),
            float(item.get("last_interaction_ts", 0) or 0),
        ),
        reverse=True,
    )
    return candidates[:10]


async def _get_friend_capable_bot(
    bots: Dict[str, Any],
    logger: Any,
) -> tuple[Any | None, Dict[str, Dict[str, Any]]]:
    for bot in bots.values():
        try:
            friends = await bot.get_friend_list()
        except Exception as e:
            logger.warning(f"[proactive] get_friend_list failed: {e}")
            continue

        friend_profiles: Dict[str, Dict[str, Any]] = {}
        if isinstance(friends, list):
            for item in friends:
                if isinstance(item, dict) and item.get("user_id") is not None:
                    user_id = str(item.get("user_id"))
                    friend_profiles[user_id] = {
                        "nickname": str(
                            item.get("remark")
                            or item.get("nickname")
                            or item.get("user_remark")
                            or user_id
                        ),
                    }
        return bot, friend_profiles

    return None, {}


def _format_candidates(candidates: Iterable[dict]) -> str:
    lines = []
    for item in candidates:
        line = (
            f"- {item['user_id']} | {item['nickname']} / 好感度{item['favorability']:.2f} "
            f"/ 上次聊天：{item['last_chat']} / 关系温度：{item['warmth']}"
        )
        persona_snippet = str(item.get("persona_snippet", "") or "").strip()
        if persona_snippet:
            line += f" / 画像：{persona_snippet}"
        emotion_memory = str(item.get("emotion_memory", "") or "").strip()
        if emotion_memory:
            line += f" / 近期情绪：{emotion_memory}"
        private_context = str(item.get("recent_private_context", "") or "").strip()
        if private_context:
            line += f"\n  最近私聊：{private_context}"
        proactive_history = str(item.get("recent_proactive_history", "") or "").strip()
        if proactive_history:
            line += f"\n  你最近主动发过：{proactive_history}"
        lines.append(line)
    return "\n".join(lines) if lines else "- 无可联系对象"


def _parse_decision(result: str) -> tuple[str, str, str]:
    text = (result or "").strip()
    # 先把 LLM 可能加的思维链 XML 剥掉，让 SEND|/SKIP| 协议在裸文本上识别。
    inner = _strip_xml_message_content(text)
    if inner and ("SEND|" in inner or "SKIP|" in inner):
        text = inner
    text = strip_response_control_markers(text)
    if text.startswith("SEND|"):
        _, user_id, message = (text.split("|", 2) + ["", ""])[:3]
        return "SEND", user_id.strip(), message.strip()
    if text.startswith("SKIP|"):
        return "SKIP", "", text.split("|", 1)[1].strip()
    return "SKIP", "", text


def _strip_xml_message_content(text: str) -> str:
    content = (text or "").strip()
    _msg_inner = _re.search(
        r"<message[^>]*>(.*?)</\s*message\s*>",
        content,
        _re.DOTALL | _re.IGNORECASE,
    )
    if _msg_inner:
        content = _re.sub(
            r"<sticker[^>]*>.*?</\s*sticker\s*>",
            "",
            _msg_inner.group(1),
            flags=_re.DOTALL | _re.IGNORECASE,
        ).strip()
    return content


def _get_group_idle_time_flavor(hour: int) -> str:
    if 6 <= hour < 7:
        return "清晨，刚起床，适合迷糊地冒一句"
    if 7 <= hour < 8:
        return "早上通学路上，适合随口吐槽两句"
    if 8 <= hour < 12:
        return "上午上课时段，适合课间感的小吐槽，不宜太闹"
    if 12 <= hour < 13:
        return "午休，适合聊便当、犯困、摸鱼"
    if 13 <= hour < 15:
        return "下午上课时段，适合简短接话或轻微吐槽"
    if 15 <= hour < 16:
        return "放学前后，适合聊终于熬完了、等会干嘛"
    if 16 <= hour < 18:
        return "放学后或社团时间，适合聊社团、回家路上、今天的事"
    if 18 <= hour < 19:
        return "回家或晚饭时间，适合聊吃什么、累不累"
    if 19 <= hour < 21:
        return "晚上做作业时段，适合吐槽作业、顺嘴闲聊"
    if 21 <= hour < 23:
        return "晚上自由时间，适合聊游戏、番剧、随便扯淡"
    return "深夜，话题更安静、慵懒，不宜太亢奋"


def _is_group_truly_idle(
    group_id: str,
    *,
    get_recent_group_msgs: Callable[[str, int], list[dict]],
    idle_minutes: int,
    now_ts: float,
) -> tuple[bool, int]:
    """
    检查群是否真正空闲。
    返回 (is_idle, actual_idle_minutes)。
    """
    recent = get_recent_group_msgs(group_id, limit=10)
    if not recent:
        return True, idle_minutes

    timestamps = [
        int(msg.get("time", 0) or 0)
        for msg in recent
        if isinstance(msg, dict)
    ]
    if not timestamps:
        return True, idle_minutes

    last_msg_time = max(timestamps)
    if last_msg_time <= 0:
        return True, idle_minutes

    elapsed = int((now_ts - last_msg_time) / 60)
    return elapsed >= idle_minutes, elapsed


def _get_group_idle_today_count(
    group_id: str,
    proactive_state: dict,
    today: str,
) -> int:
    """获取今日某群已主动发话次数。"""
    key = f"group_idle_{group_id}"
    state = proactive_state.get(key, {})
    if not isinstance(state, dict):
        return 0
    if state.get("last_date") != today:
        return 0
    return int(state.get("count", 0) or 0)


def _increment_group_idle_count(
    group_id: str,
    proactive_state: dict,
    today: str,
    now_ts: float,
) -> None:
    """将某群今日主动发话次数 +1。"""
    key = f"group_idle_{group_id}"
    state = proactive_state.get(key, {})
    if not isinstance(state, dict) or state.get("last_date") != today:
        state = {"last_date": today, "count": 0, "last_sent_at": 0}
    state["count"] = int(state.get("count", 0) or 0) + 1
    state["last_sent_at"] = now_ts
    proactive_state[key] = state


async def _decide_idle_output_mode(
    *,
    call_ai_api: Callable[..., Awaitable[Optional[str]]],
    plugin_config: Any = None,
    agent_tool_caller: Any = None,
    agent_tool_registry: Any = None,
    agent_max_steps: int = 4,
    topic: str,
    group_style: str,
    logger: Any,
    group_id: str,
) -> tuple[str, str]:
    """J4 phase-1: 用 LLM 决定群水群输出模式 (text/sticker/combo) 和情绪标签。
    返回 (mode, mood_hint)；任何失败都 fallback 到 ('text', '')。
    """
    prompt = GROUP_IDLE_MODE_DECISION_PROMPT.format(
        topic=str(topic or "")[:120],
        group_style=str(group_style or "")[:60],
    )
    token = None
    try:
        from ..core.llm_context import reset_llm_context, set_llm_context

        token = set_llm_context(
            purpose="proactive_group_idle_mode",
            group_id=str(group_id or ""),
        )
    except Exception:
        token = None
    try:
        messages = [{"role": "user", "content": prompt}]
        raw = ""
        if (
            getattr(plugin_config, "personification_agent_enabled", True)
            and agent_tool_caller is not None
            and agent_tool_registry is not None
        ):
            try:
                raw = await run_text_agent(
                    messages=messages,
                    plugin_config=plugin_config,
                    logger=logger,
                    tool_caller=agent_tool_caller,
                    registry=agent_tool_registry,
                    max_steps=agent_max_steps,
                    trigger_reason="proactive_group_idle_mode",
                    chat_intent_hint="proactive_group_idle_mode",
                    structured_output=True,
                )
            except Exception as exc:
                logger.debug(f"[group_idle] mode Agent decision failed: {exc}")
                raw = ""
        else:
            raw = await call_ai_api(messages)
    except Exception as e:
        logger.debug(f"[group_idle] mode decision LLM call failed: {e}")
        return ("text", "")
    finally:
        if token is not None:
            try:
                reset_llm_context(token)
            except Exception:
                pass
    if not raw:
        return ("text", "")
    import json as _json
    import re as _re

    candidate = str(raw).strip()
    # 容错：剥掉可能的 ```json 包装
    candidate = _re.sub(r"^```(?:json)?\s*", "", candidate)
    candidate = _re.sub(r"\s*```\s*$", "", candidate)
    try:
        data = _json.loads(candidate)
    except Exception:
        match = _re.search(r"\{[^{}]*\}", candidate)
        if not match:
            return ("text", "")
        try:
            data = _json.loads(match.group(0))
        except Exception:
            return ("text", "")
    if not isinstance(data, dict):
        return ("text", "")
    mode = str(data.get("mode", "text") or "text").strip().lower()
    if mode not in {"text", "sticker", "combo", "qq_face", "qq_face_combo", "qq_super", "text_qq_face"}:
        mode = "text"
    mood = str(data.get("mood", "") or "").strip()[:24]
    return (mode, mood)


async def _send_group_idle_qq_expression(
    *,
    bot: Any,
    group_id: str,
    plugin_config: Any,
    topic: str,
    mood_hint: str,
    mode: str,
    logger: Any,
    qq_outbound_ledger: Any = None,
    outbound_context: Any = None,
) -> str:
    send_invoked = False
    try:
        marker_mode = {
            "qq_face": "face",
            "qq_face_combo": "triple",
            "qq_super": "super",
            "text_qq_face": "face",
        }.get(str(mode or "").strip().lower(), "face")
        marker = choose_qq_expression_marker_for_context(
            mood_hint=mood_hint or topic,
            context=topic,
            mode=marker_mode,
        )
        if not marker:
            return ""
        content = marker if mode in {"qq_face", "qq_face_combo", "qq_super"} else f"{topic}{marker}"
        rendered = await render_qq_expression_cq_text(
            content,
            bot=bot,
            plugin_config=plugin_config,
            logger=logger,
        )
        if not rendered.message:
            return ""

        async def _send() -> Any:
            nonlocal send_invoked
            send_invoked = True
            return await bot.send_group_msg(group_id=int(group_id), message=rendered.message)

        await _dispatch_proactive_outbound(
            qq_outbound_ledger=qq_outbound_ledger,
            outbound_context=outbound_context,
            content=rendered.message,
            send=_send,
        )
        logger.info(f"[group_idle] 群 {group_id} 发送 QQ 表情模式 {mode}: {rendered.history_text[:40]}")
        return rendered.history_text or content
    except Exception as e:
        if qq_outbound_ledger is not None and send_invoked:
            raise
        logger.warning(f"[group_idle] qq expression send failed for group {group_id}: {e}")
        return ""


async def _try_send_idle_sticker(
    *,
    bot: Any,
    group_id: str,
    runtime_bundle: Any,
    plugin_config: Any,
    mood_hint: str,
    topic_text_fallback: str,
    logger: Any,
    qq_outbound_ledger: Any = None,
    outbound_context: Any = None,
) -> bool:
    """发送前失败返回 False；ledger 已调用 send 后的异常必须向上传播。"""
    send_invoked = False
    try:
        from ..core.sticker_library import (
            list_local_sticker_files,
            load_sticker_metadata,
            resolve_sticker_dir,
        )
        from ..core.sticker_feedback import (
            get_sticker_decay_multiplier,
            load_sticker_feedback,
            record_sticker_sent,
        )
    except Exception as e:
        logger.debug(f"[group_idle] sticker imports failed: {e}")
        return False
    try:
        sticker_dir = resolve_sticker_dir(getattr(plugin_config, "personification_sticker_path", None))
        files = list_local_sticker_files(sticker_dir, include_gif=True)
        if not files:
            return False
        metadata = load_sticker_metadata(sticker_dir)
        # 拉一次 feedback state 算衰减倍数
        try:
            feedback_state = await load_sticker_feedback()
        except Exception:
            feedback_state = {}
        # 简单匹配：按 mood_tags / scene_tags 命中 hint 关键字，结合衰减权重
        hint_text = (str(mood_hint or "") + " " + str(topic_text_fallback or "")).strip()
        scored: list[tuple[float, str]] = []
        for file_path in files:
            entry = metadata.get(file_path.name) or {}
            if not isinstance(entry, dict):
                continue
            score = 0.0
            for tag in list(entry.get("mood_tags", []) or []) + list(entry.get("scene_tags", []) or []):
                if str(tag).strip() and str(tag).strip() in hint_text:
                    score += 1.0
            if not score:
                # 至少要有 description 才候选，跳过未打标
                if not entry.get("description"):
                    continue
                score = 0.2
            weight = float(entry.get("weight", 1.0) or 1.0)
            try:
                decay = float(get_sticker_decay_multiplier(file_path.name, feedback_state))
            except Exception:
                decay = 1.0
            scored.append((score * weight * decay, file_path.name))
        if not scored:
            return False
        scored.sort(key=lambda x: x[0], reverse=True)
        best_name = scored[0][1]
        best_path = sticker_dir / best_name
        if not best_path.exists():
            return False
        # OneBot v11 CQ 码发送
        cq = f"[CQ:image,file=file:///{best_path.resolve().as_posix().lstrip('/')}]"

        async def _send() -> Any:
            nonlocal send_invoked
            send_invoked = True
            return await bot.send_group_msg(group_id=int(group_id), message=cq)

        await _dispatch_proactive_outbound(
            qq_outbound_ledger=qq_outbound_ledger,
            outbound_context=outbound_context,
            content=cq,
            send=_send,
        )
        try:
            await record_sticker_sent(best_name)
        except Exception:
            pass
        logger.info(f"[group_idle] 群 {group_id} 选发表情包 {best_name}（mood_hint='{mood_hint[:20]}'）")
        return True
    except Exception as e:
        if qq_outbound_ledger is not None and send_invoked:
            raise
        logger.warning(f"[group_idle] sticker send failed for group {group_id}: {e}")
        return False


async def run_proactive_messaging(
    *,
    plugin_config: Any,
    sign_in_available: bool,
    is_rest_time: Callable[..., bool],
    get_bots: Callable[[], Dict[str, Any]],
    load_data: Callable[[], Dict[str, Dict[str, Any]]],
    load_proactive_state: Callable[[], Dict[str, Dict[str, Any]]],
    save_proactive_state: Callable[[Dict[str, Dict[str, Any]]], None],
    get_user_data: Callable[[str], Dict[str, Any]],
    get_level_name: Callable[[float], str],
    get_now: Callable[[], datetime],
    get_activity_status: Callable[[], str],
    load_prompt: Callable[[], Any],
    call_ai_api: Callable[..., Awaitable[Optional[str]]],
    parse_yaml_response: Callable[..., Any],
    logger: Any,
    agent_tool_caller: Any = None,
    agent_tool_registry: Any = None,
    agent_max_steps: int = 4,
    agent_data_dir: Optional[Path] = None,
    persona_store: Any = None,
    qq_outbound_ledger: Any = None,
) -> bool:
    _ = get_user_data, get_level_name, get_activity_status, parse_yaml_response

    from ..core import proactive_diagnostics as _diag

    if not getattr(plugin_config, "personification_proactive_enabled", True):
        _diag.record(scope="private", outcome=_diag.SKIP_DISABLED, detail={"reason": "proactive_enabled=False"})
        return False
    if not sign_in_available and not bool(
        getattr(plugin_config, "personification_proactive_without_signin", True)
    ):
        _diag.record(scope="private", outcome=_diag.SKIP_OTHER, detail={"reason": "no_signin"})
        return False
    allow_unsuitable_prob = float(
        max(
            0.0,
            min(
                1.0,
                getattr(plugin_config, "personification_proactive_unsuitable_prob", 0.18),
            ),
        )
    )
    try:
        rest_ok = bool(is_rest_time(allow_unsuitable_prob=allow_unsuitable_prob))
    except TypeError:
        rest_ok = bool(is_rest_time())
    if not rest_ok:
        return False

    bots = get_bots() or {}
    if not bots:
        return False
    bot, friend_profiles = await _get_friend_capable_bot(bots, logger)
    if bot is None or not friend_profiles:
        return False
    friend_ids = set(friend_profiles)

    now = get_now()
    proactive_state = load_proactive_state() or {}

    total_today_count = 0
    last_proactive_at = 0.0
    for user_id, user_state in list(proactive_state.items()):
        if str(user_id).startswith("group_"):
            continue
        normalized = _normalize_user_state(now, user_state if isinstance(user_state, dict) else {})
        proactive_state[str(user_id)] = normalized
        total_today_count += int(normalized.get("count", 0) or 0)
        last_proactive_at = max(last_proactive_at, float(normalized.get("last_proactive_at", 0) or 0))

    daily_limit = max(1, int(getattr(plugin_config, "personification_proactive_daily_limit", 3)))
    min_interval = max(1, int(getattr(plugin_config, "personification_proactive_interval", 30)))
    if total_today_count >= daily_limit:
        save_proactive_state(proactive_state)
        _diag.record(
            scope="private",
            outcome=_diag.SKIP_DAILY_LIMIT,
            detail={"today_count": total_today_count, "daily_limit": daily_limit},
            next_eligible_at=(now.replace(hour=0, minute=0, second=0) + timedelta(days=1)).timestamp(),
        )
        return False

    now_ts = now.timestamp()
    if last_proactive_at and now_ts - last_proactive_at < min_interval * 60:
        save_proactive_state(proactive_state)
        _diag.record(
            scope="private",
            outcome=_diag.SKIP_COOLDOWN,
            detail={
                "since_last_seconds": int(now_ts - last_proactive_at),
                "min_interval_minutes": min_interval,
            },
            next_eligible_at=last_proactive_at + min_interval * 60,
        )
        return False

    inner_state = dict(DEFAULT_STATE)
    if agent_data_dir is not None:
        try:
            inner_state.update(await load_inner_state(Path(agent_data_dir)))
        except Exception as e:
            logger.warning(f"[proactive] load inner_state failed: {e}")
    emotion_state = {}
    if agent_data_dir is not None:
        try:
            emotion_state = await load_emotion_state(Path(agent_data_dir))
        except Exception as e:
            logger.warning(f"[proactive] load emotion_state failed: {e}")

    persona_snippet_max_chars = int(
        getattr(plugin_config, "personification_persona_snippet_max_chars", 150)
    )
    idle_hours = float(getattr(plugin_config, "personification_proactive_idle_hours", 24.0))
    require_user_profile = bool(getattr(plugin_config, "personification_proactive_require_user_profile", True))
    if sign_in_available:
        all_user_data = load_data() or {}
        candidates = _build_candidates(
            all_user_data=all_user_data,
            proactive_state=proactive_state,
            inner_state=inner_state,
            emotion_state=emotion_state,
            now=now,
            threshold=float(getattr(plugin_config, "personification_proactive_threshold", 60.0)),
            idle_hours=idle_hours,
            friend_ids=friend_ids,
            persona_store=persona_store,
            persona_snippet_max_chars=persona_snippet_max_chars,
            require_user_profile=require_user_profile,
        )
    else:
        candidates = _build_fallback_candidates(
            friend_profiles=friend_profiles,
            proactive_state=proactive_state,
            inner_state=inner_state,
            emotion_state=emotion_state,
            now=now,
            idle_hours=idle_hours,
            persona_store=persona_store,
            persona_snippet_max_chars=persona_snippet_max_chars,
            require_user_profile=require_user_profile,
        )
    if not candidates:
        save_proactive_state(proactive_state)
        return False
    _enrich_candidates_with_recent_context(candidates, proactive_state)

    system_prompt = _extract_system_prompt(load_prompt())
    timezone_name = getattr(plugin_config, "personification_timezone", "Asia/Shanghai")
    datetime_info = get_current_datetime_info(timezone_name, now)
    elapsed_minutes = int((now_ts - last_proactive_at) / 60) if last_proactive_at else 999999
    emotion_memory_formatted = (
        "\n".join(
            f"- {item['nickname']}: {item['emotion_memory']}"
            for item in candidates
            if str(item.get("emotion_memory", "") or "").strip()
        )
        or "- 暂无明显近期情绪记忆"
    )
    user_prompt = PROACTIVE_DECISION_PROMPT.format(
        system_prompt=system_prompt,
        mood=str(inner_state.get("mood", DEFAULT_STATE["mood"])),
        energy=str(inner_state.get("energy", DEFAULT_STATE["energy"])),
        pending_thoughts_formatted=_format_pending_thoughts(inner_state.get("pending_thoughts", [])),
        emotion_memory_formatted=emotion_memory_formatted,
        datetime_info=datetime_info,
        candidates_formatted=_format_candidates(candidates),
        recent_proactive_history=_format_recent_proactive_history(proactive_state),
        fav="fav",
        last_chat="last_chat",
        warmth="warmth",
        today_count=total_today_count,
        daily_limit=daily_limit,
        elapsed_minutes=elapsed_minutes,
        min_interval=min_interval,
        user_id="user_id",
    )

    _purpose_token = None
    try:
        from ..core.llm_context import reset_llm_context, set_llm_context

        _purpose_token = set_llm_context(purpose="proactive_private")
    except Exception:
        _purpose_token = None
    try:
        messages = _build_time_anchored_messages(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            now=now,
        )
        decision = ""
        if (
            getattr(plugin_config, "personification_agent_enabled", True)
            and agent_tool_caller is not None
            and agent_tool_registry is not None
        ):
            try:
                decision = await run_text_agent(
                    messages=messages,
                    plugin_config=plugin_config,
                    logger=logger,
                    tool_caller=agent_tool_caller,
                    registry=agent_tool_registry,
                    max_steps=agent_max_steps,
                    trigger_reason="proactive_private",
                    chat_intent_hint="proactive_private",
                    structured_output=True,
                )
            except Exception as exc:
                logger.warning(f"[proactive] full Agent decision failed, skip direct-model fallback: {exc}")
                decision = ""
        else:
            decision = await call_ai_api(messages)
    finally:
        if _purpose_token is not None:
            try:
                reset_llm_context(_purpose_token)
            except Exception:
                pass
    action, target_user_id, payload = _parse_decision(decision or "")
    payload = _strip_xml_message_content(payload)
    if action != "SEND" or not target_user_id or not payload:
        save_proactive_state(proactive_state)
        _diag.record(
            scope="private",
            outcome=_diag.SKIP_LLM_DECIDED,
            target=target_user_id,
            detail={"action": action, "raw_decision_len": len(str(decision or ""))},
        )
        return False

    target = next((item for item in candidates if item["user_id"] == target_user_id), None)
    if target is None:
        logger.warning(f"[proactive] target user not in candidates: {target_user_id}")
        save_proactive_state(proactive_state)
        return False

    if target_user_id not in friend_ids:
        logger.warning(f"[proactive] target user is not bot friend: {target_user_id}")
        save_proactive_state(proactive_state)
        return False

    proactive_probability = float(
        max(0.0, min(1.0, getattr(plugin_config, "personification_proactive_probability", 0.15)))
    )
    if random.random() > proactive_probability:
        save_proactive_state(proactive_state)
        _diag.record(
            scope="private",
            outcome=_diag.SKIP_PROBABILITY,
            target=target_user_id,
            detail={"probability": proactive_probability},
        )
        return False

    payload = guard_visible_text(
        payload,
        logger=logger,
        surface="proactive_private",
        allow_direct_media=False,
    )
    if not payload:
        save_proactive_state(proactive_state)
        return False
    outbound_context = None
    if qq_outbound_ledger is not None:
        outbound_context = build_outbound_context(
            bot=bot,
            event=SimpleNamespace(user_id=target_user_id),
            surface="proactive_private",
            user_target=target_user_id,
        )

    try:
        await _dispatch_proactive_outbound(
            qq_outbound_ledger=qq_outbound_ledger,
            outbound_context=outbound_context,
            content=payload,
            send=lambda: bot.send_private_msg(user_id=int(target_user_id), message=payload),
        )
    except ProactiveOutboundOutcomeUnknown:
        logger.warning(
            f"[proactive] 用户 {target_user_id} 发送结果未知，禁止自动重发且不提交成功状态"
        )
        save_proactive_state(proactive_state)
        return False
    _diag.record(
        scope="private",
        outcome=_diag.OUTCOME_SENT,
        target=target_user_id,
        detail={"len": len(payload)},
    )

    user_state = _normalize_user_state(now, proactive_state.get(target_user_id, {}))
    user_state["count"] = int(user_state.get("count", 0) or 0) + 1
    user_state["last_proactive_at"] = now_ts
    _append_proactive_history(user_state, message=payload, sent_at=now_ts)
    proactive_state[target_user_id] = user_state
    try:
        append_session_message(
            build_private_session_id(target_user_id),
            "assistant",
            payload,
            source_kind="proactive_private",
            speaker="你",
        )
    except Exception as e:
        logger.warning(f"[proactive] append proactive private history failed: {e}")
    save_proactive_state(proactive_state)
    return True


async def run_group_idle_topic(
    *,
    plugin_config: Any,
    get_bots: Callable[[], Dict[str, Any]],
    get_whitelisted_groups: Callable[[], list[str]],
    get_recent_group_msgs: Callable[[str, int], list[dict]],
    get_group_style: Callable[[str], str],
    load_proactive_state: Callable[[], Dict[str, Dict[str, Any]]],
    save_proactive_state: Callable[[Dict[str, Dict[str, Any]]], None],
    load_prompt: Callable[[str], Any],
    call_ai_api: Callable[..., Awaitable[Optional[str]]],
    get_now: Callable[[], datetime],
    record_group_msg: Callable[[str, str, str, bool], int],
    logger: Any,
    agent_tool_caller: Any = None,
    agent_tool_registry: Any = None,
    agent_max_steps: int = 4,
    agent_data_dir: Optional[Path] = None,
    superusers: Optional[set[str]] = None,
    get_user_data: Optional[Callable[[str], Any]] = None,
    build_grounding_context: Optional[Callable[[str, str], Awaitable[str]]] = None,
    qq_outbound_ledger: Any = None,
) -> int:
    """
    扫描所有白名单群，对空闲群主动发起话题。
    返回本次实际发话的群数。
    """
    global _last_group_idle_sent_at

    from ..schedule import is_group_active_hour

    idle_minutes = max(
        10, int(getattr(plugin_config, "personification_group_idle_minutes", 60))
    )
    daily_limit = max(
        1, int(getattr(plugin_config, "personification_group_idle_daily_limit", 3))
    )
    quiet_start = int(getattr(plugin_config, "personification_group_quiet_hour_start", 0))
    quiet_end = int(getattr(plugin_config, "personification_group_quiet_hour_end", 7))
    timezone_name = getattr(plugin_config, "personification_timezone", "Asia/Shanghai")

    if not is_group_active_hour(quiet_start, quiet_end):
        logger.debug("[group_idle] 当前处于深夜禁发时段，跳过")
        return 0

    bots = get_bots() or {}
    if not bots:
        return 0
    bot = list(bots.values())[0]
    bot_nickname = ""
    try:
        bot_nickname = str(getattr(bot, "self_id", "") or "").strip()
    except Exception:
        bot_nickname = ""
    if not bot_nickname:
        bot_nickname = "bot"

    whitelisted = get_whitelisted_groups()
    if not whitelisted:
        return 0

    now = get_now()
    now_ts = now.timestamp()
    today = now.strftime("%Y-%m-%d")
    proactive_state = load_proactive_state() or {}

    inner_state = dict(DEFAULT_STATE)
    if agent_data_dir is not None:
        try:
            inner_state.update(await load_inner_state(Path(agent_data_dir)))
        except Exception as e:
            logger.warning(f"[group_idle] load inner_state failed: {e}")
    emotion_state = {}
    if agent_data_dir is not None:
        try:
            emotion_state = await load_emotion_state(Path(agent_data_dir))
        except Exception as e:
            logger.warning(f"[group_idle] load emotion_state failed: {e}")

    sent_count = 0

    async with _GROUP_IDLE_LOCK:
        check_interval_minutes = max(
            1,
            int(
                getattr(plugin_config, "personification_group_idle_check_interval", 15)
            ),
        )
        global_cooldown_seconds = float(check_interval_minutes) * 60.0
        if (
            _last_group_idle_sent_at
            and now_ts - _last_group_idle_sent_at < global_cooldown_seconds
        ):
            logger.debug("[group_idle] 全局冷却中，跳过本轮群空闲发话")
            return 0

        for group_id in whitelisted:
            group_id = str(group_id)

            today_count = _get_group_idle_today_count(group_id, proactive_state, today)
            if today_count >= daily_limit:
                continue
            key = f"group_idle_{group_id}"
            last_sent = float((proactive_state.get(key) or {}).get("last_sent_at", 0) or 0)
            if last_sent and now_ts - last_sent < idle_minutes * 60:
                continue

            jittered_idle = max(10, idle_minutes + random.randint(-8, 15))
            is_idle, elapsed_minutes = _is_group_truly_idle(
                group_id,
                get_recent_group_msgs=get_recent_group_msgs,
                idle_minutes=jittered_idle,
                now_ts=now_ts,
            )
            if not is_idle:
                continue

            system_prompt = _extract_system_prompt(load_prompt(group_id))
            group_style = get_group_style(group_id) or "日常闲聊"

            recent_msgs = get_recent_group_msgs(group_id, limit=6)
            recent_keywords = (
                "、".join(
                    str(msg.get("content", ""))[:12]
                    for msg in recent_msgs
                    if isinstance(msg, dict) and msg.get("content") and not msg.get("is_bot")
                )[:60]
                or "无"
            )
            topic_summary = get_group_topic_summary(group_id)
            context_hint = topic_summary or recent_keywords

            time_flavor = _get_group_idle_time_flavor(now.hour)
            grounding_hint = ""
            if build_grounding_context is not None and context_hint and context_hint != "无":
                try:
                    grounding_hint = await build_grounding_context(context_hint, context_hint)
                except Exception as e:
                    logger.debug(f"[group_idle] build_grounding_context failed for group {group_id}: {e}")

            group_name = group_id
            try:
                info = await bot.get_group_info(group_id=int(group_id))
                group_name = info.get("group_name", group_id)
            except Exception:
                pass

            superuser_context = ""
            if grounding_hint:
                superuser_context = f"[联网话题参考]\n{grounding_hint}\n\n"

            datetime_info = get_current_datetime_info(timezone_name, now)
            group_emotion_memory = describe_group_emotion_memory(emotion_state, group_id) or "暂无明显群情绪记忆"
            user_prompt = GROUP_IDLE_TOPIC_PROMPT.format(
                system_prompt=system_prompt,
                mood=str(inner_state.get("mood", DEFAULT_STATE["mood"])),
                energy=str(inner_state.get("energy", DEFAULT_STATE["energy"])),
                group_emotion_memory=group_emotion_memory,
                datetime_info=datetime_info,
                group_id=group_id,
                group_name=group_name,
                group_style=group_style,
                idle_minutes=elapsed_minutes,
                context_hint=context_hint,
                time_flavor=time_flavor,
                superuser_context=superuser_context,
            )

            _gi_token = None
            try:
                from ..core.llm_context import reset_llm_context, set_llm_context

                _gi_token = set_llm_context(
                    purpose="proactive_group_idle",
                    group_id=str(group_id or ""),
                )
            except Exception:
                _gi_token = None
            try:
                messages = _build_time_anchored_messages(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    now=now,
                )
                topic = ""
                if (
                    getattr(plugin_config, "personification_agent_enabled", True)
                    and agent_tool_caller is not None
                    and agent_tool_registry is not None
                ):
                    try:
                        topic = await run_text_agent(
                            messages=messages,
                            plugin_config=plugin_config,
                            logger=logger,
                            tool_caller=agent_tool_caller,
                            registry=agent_tool_registry,
                            max_steps=agent_max_steps,
                            use_builtin_search_hint=True,
                            trigger_reason="proactive_group_idle",
                            chat_intent_hint="proactive_group_idle",
                            structured_output=False,
                        )
                    except Exception as exc:
                        logger.warning(
                            f"[group_idle] full Agent topic failed for group {group_id}, "
                            f"skip direct-model fallback: {exc}"
                        )
                        topic = ""
                else:
                    topic = await call_ai_api(messages)
            except Exception as e:
                logger.warning(f"[group_idle] call_ai_api failed for group {group_id}: {e}")
                continue
            finally:
                if _gi_token is not None:
                    try:
                        reset_llm_context(_gi_token)
                    except Exception:
                        pass

            topic = _strip_xml_message_content(topic or "")
            if not topic or topic.startswith("[") or len(topic) < 2:
                logger.info(f"[group_idle] 群 {group_id} AI 返回空内容，跳过")
                continue

            proactive_probability = float(
                max(0.0, min(1.0, getattr(plugin_config, "personification_proactive_probability", 0.15)))
            )
            if random.random() > proactive_probability:
                continue

            # J4 阶段二：决定模式 text / sticker / combo（按概率门控避免每次都多花 token）
            mode_decision_prob = float(
                max(0.0, min(1.0, getattr(
                    plugin_config,
                    "personification_group_idle_mode_decision_prob",
                    0.4,
                )))
            )
            chosen_mode = "text"
            sticker_mood_hint = ""
            if random.random() < mode_decision_prob:
                chosen_mode, sticker_mood_hint = await _decide_idle_output_mode(
                    call_ai_api=call_ai_api,
                    plugin_config=plugin_config,
                    agent_tool_caller=agent_tool_caller,
                    agent_tool_registry=agent_tool_registry,
                    agent_max_steps=agent_max_steps,
                    topic=topic,
                    group_style=group_style,
                    logger=logger,
                    group_id=group_id,
                )

            topic = guard_visible_text(
                topic,
                logger=logger,
                surface="proactive_group_idle",
                allow_direct_media=False,
            )
            if not topic:
                continue

            try:
                # 根据 chosen_mode 决定发送方式
                sent_record_text = topic
                outbound_context = None
                if qq_outbound_ledger is not None:
                    outbound_context = build_outbound_context(
                        bot=bot,
                        event=SimpleNamespace(group_id=group_id),
                        surface="proactive_group_idle",
                    )
                if chosen_mode == "sticker":
                    sticker_sent = await _try_send_idle_sticker(
                        bot=bot,
                        group_id=group_id,
                        runtime_bundle=getattr(plugin_config, "_runtime_bundle_ref", None),
                        plugin_config=plugin_config,
                        mood_hint=sticker_mood_hint or topic,
                        topic_text_fallback=topic,
                        logger=logger,
                        qq_outbound_ledger=qq_outbound_ledger,
                        outbound_context=outbound_context,
                    )
                    if not sticker_sent:
                        # 未选出可发送表情时回退文本；unknown 异常会直接向外传播。
                        await _dispatch_proactive_outbound(
                            qq_outbound_ledger=qq_outbound_ledger,
                            outbound_context=outbound_context,
                            content=topic,
                            send=lambda: bot.send_group_msg(group_id=int(group_id), message=topic),
                        )
                    elif chosen_mode == "combo":
                        # combo 已在 _try_send_idle_sticker 内发了表情，这里不再补
                        pass
                    else:
                        sent_record_text = "[发送了一张表情包]"
                elif chosen_mode == "combo":
                    await _dispatch_proactive_outbound(
                        qq_outbound_ledger=qq_outbound_ledger,
                        outbound_context=outbound_context,
                        content=topic,
                        send=lambda: bot.send_group_msg(group_id=int(group_id), message=topic),
                    )
                    # 异步追加 sticker（不阻塞回 sent_count 计数）
                    await _try_send_idle_sticker(
                        bot=bot,
                        group_id=group_id,
                        runtime_bundle=getattr(plugin_config, "_runtime_bundle_ref", None),
                        plugin_config=plugin_config,
                        mood_hint=sticker_mood_hint or topic,
                        topic_text_fallback="",
                        logger=logger,
                        qq_outbound_ledger=qq_outbound_ledger,
                        outbound_context=outbound_context,
                    )
                elif chosen_mode in {"qq_face", "qq_face_combo", "qq_super", "text_qq_face"}:
                    qq_sent = await _send_group_idle_qq_expression(
                        bot=bot,
                        group_id=group_id,
                        plugin_config=plugin_config,
                        topic=topic,
                        mood_hint=sticker_mood_hint or topic,
                        mode=chosen_mode,
                        logger=logger,
                        qq_outbound_ledger=qq_outbound_ledger,
                        outbound_context=outbound_context,
                    )
                    if qq_sent:
                        sent_record_text = qq_sent
                    else:
                        await _dispatch_proactive_outbound(
                            qq_outbound_ledger=qq_outbound_ledger,
                            outbound_context=outbound_context,
                            content=topic,
                            send=lambda: bot.send_group_msg(group_id=int(group_id), message=topic),
                        )
                else:
                    await _dispatch_proactive_outbound(
                        qq_outbound_ledger=qq_outbound_ledger,
                        outbound_context=outbound_context,
                        content=topic,
                        send=lambda: bot.send_group_msg(group_id=int(group_id), message=topic),
                    )
                sent_now = get_now()
                sent_now_ts = sent_now.timestamp()
                sent_today = sent_now.strftime("%Y-%m-%d")
                proactive_state[f"group_idle_active_{group_id}"] = {
                    "until": sent_now_ts + 12 * 60,
                    "topic": topic,
                }
                _increment_group_idle_count(group_id, proactive_state, sent_today, sent_now_ts)
                _last_group_idle_sent_at = sent_now_ts
                real_bot_nickname = bot_nickname
                try:
                    member_info = await bot.get_group_member_info(
                        group_id=int(group_id),
                        user_id=int(bot.self_id),
                    )
                    real_bot_nickname = str(
                        member_info.get("card")
                        or member_info.get("nickname")
                        or bot_nickname
                    ).strip() or bot_nickname
                except Exception:
                    pass
                record_group_msg(group_id, real_bot_nickname, sent_record_text, is_bot=True)
                sent_count += 1
                logger.info(
                    f"[group_idle] 已向群 {group_name}({group_id}) 发送话题：{topic[:30]}"
                )
                break
            except Exception as e:
                logger.warning(f"[group_idle] send_group_msg failed for group {group_id}: {e}")
                if qq_outbound_ledger is not None:
                    break

    save_proactive_state(proactive_state)
    if sent_count > 0:
        await _asyncio.sleep(random.uniform(0, 4 * 60))
    return sent_count


def build_proactive_checker(
    *,
    plugin_config: Any,
    sign_in_available: bool,
    is_rest_time: Callable[[], bool],
    get_bots: Callable[[], Dict[str, Any]],
    load_data: Callable[[], Dict[str, Dict[str, Any]]],
    load_proactive_state: Callable[[], Dict[str, Dict[str, Any]]],
    save_proactive_state: Callable[[Dict[str, Dict[str, Any]]], None],
    get_user_data: Callable[[str], Dict[str, Any]],
    get_level_name: Callable[[float], str],
    get_now: Callable[[], datetime],
    get_activity_status: Callable[[], str],
    load_prompt: Callable[[], Any],
    call_ai_api: Callable[..., Awaitable[Optional[str]]],
    parse_yaml_response: Callable[..., Any],
    logger: Any,
    agent_tool_caller: Any = None,
    agent_tool_registry: Any = None,
    agent_max_steps: int = 4,
    agent_data_dir: Optional[Path] = None,
    persona_store: Any = None,
    qq_outbound_ledger: Any = None,
) -> Callable[[], Awaitable[bool]]:
    async def _check_proactive_messaging() -> bool:
        return await run_proactive_messaging(
            plugin_config=plugin_config,
            sign_in_available=sign_in_available,
            is_rest_time=is_rest_time,
            get_bots=get_bots,
            load_data=load_data,
            load_proactive_state=load_proactive_state,
            save_proactive_state=save_proactive_state,
            get_user_data=get_user_data,
            get_level_name=get_level_name,
            get_now=get_now,
            get_activity_status=get_activity_status,
            load_prompt=load_prompt,
            call_ai_api=call_ai_api,
            parse_yaml_response=parse_yaml_response,
            logger=logger,
            agent_tool_caller=agent_tool_caller,
            agent_tool_registry=agent_tool_registry,
            agent_max_steps=agent_max_steps,
            agent_data_dir=agent_data_dir,
            persona_store=persona_store,
            qq_outbound_ledger=qq_outbound_ledger,
        )

    return _check_proactive_messaging


def build_group_idle_checker(
    *,
    plugin_config: Any,
    get_bots: Callable[[], Dict[str, Any]],
    get_whitelisted_groups: Callable[[], list[str]],
    get_recent_group_msgs: Callable[[str, int], list[dict]],
    get_group_style: Callable[[str], str],
    load_proactive_state: Callable[[], Dict[str, Dict[str, Any]]],
    save_proactive_state: Callable[[Dict[str, Dict[str, Any]]], None],
    load_prompt: Callable[[str], Any],
    call_ai_api: Callable[..., Awaitable[Optional[str]]],
    get_now: Callable[[], datetime],
    record_group_msg: Callable[[str, str, str, bool], int],
    logger: Any,
    agent_tool_caller: Any = None,
    agent_tool_registry: Any = None,
    agent_max_steps: int = 4,
    agent_data_dir: Optional[Path] = None,
    superusers: Optional[set[str]] = None,
    get_user_data: Optional[Callable[[str], Any]] = None,
    build_grounding_context: Optional[Callable[[str, str], Awaitable[str]]] = None,
    qq_outbound_ledger: Any = None,
) -> Callable[[], Awaitable[int]]:
    async def _check_group_idle_topic() -> int:
        return await run_group_idle_topic(
            plugin_config=plugin_config,
            get_bots=get_bots,
            get_whitelisted_groups=get_whitelisted_groups,
            get_recent_group_msgs=get_recent_group_msgs,
            get_group_style=get_group_style,
            load_proactive_state=load_proactive_state,
            save_proactive_state=save_proactive_state,
            load_prompt=load_prompt,
            call_ai_api=call_ai_api,
            get_now=get_now,
            record_group_msg=record_group_msg,
            logger=logger,
            agent_tool_caller=agent_tool_caller,
            agent_tool_registry=agent_tool_registry,
            agent_max_steps=agent_max_steps,
            agent_data_dir=agent_data_dir,
            superusers=superusers,
            get_user_data=get_user_data,
            build_grounding_context=build_grounding_context,
            qq_outbound_ledger=qq_outbound_ledger,
        )

    return _check_group_idle_topic
