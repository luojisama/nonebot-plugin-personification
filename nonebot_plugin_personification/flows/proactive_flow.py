from __future__ import annotations

import asyncio as _asyncio
import random
import re as _re
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, Optional

from ..agent.inner_state import DEFAULT_STATE, load_inner_state
from ..skills.skillpacks.datetime_tool.scripts.impl import get_current_datetime_info
from ..utils import get_group_topic_summary


_GROUP_IDLE_LOCK = _asyncio.Lock()
_last_group_idle_sent_at: float = 0.0


PROACTIVE_DECISION_PROMPT = """[角色设定]
{system_prompt}

[当前内心状态]
心情：{mood}
精力：{energy}
悬挂念头：
{pending_thoughts_formatted}

[当前时间]
{datetime_info}

[候选联系人]
{candidates_formatted}
（格式：昵称 / 好感度{fav} / 上次聊天：{last_chat} / 关系温度：{warmth}）

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
- 不超过50字
- 用角色口吻，自然、真实，像真人突然想到发消息
- 如果有悬挂念头与这个人有关，优先以此为话头
- 不要发"你好""在吗""最近怎么样"这类没有内容的开场白
- 可以分享刚刚看到的有意思的事、某个想法、问一个具体的问题
- 如果候选人的画像摘要提到了对方的兴趣、职业或最近话题，优先以此为切入点
- 禁止使用的开场句式："最近怎么样"、"在吗"、"你好"、"有没有..."、"大家..."
- 禁止任何以"哇"或"哎呀"开头的句子
"""

GROUP_IDLE_TOPIC_PROMPT = """[角色设定]
{system_prompt}

[当前内心状态]
心情：{mood}
精力：{energy}

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
- 不超过 30 字，越短越好
- 优先结合"近期群聊话题"做延伸或转折，没有则结合时段氛围
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


def _normalize_user_state(now: datetime, user_state: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(user_state or {})
    today = now.strftime("%Y-%m-%d")
    if normalized.get("last_date") != today:
        normalized["last_date"] = today
        normalized["count"] = 0
    normalized.setdefault("count", 0)
    normalized.setdefault("last_interaction", 0)
    normalized.setdefault("last_proactive_at", 0)
    return normalized


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
    now: datetime,
    threshold: float,
    idle_hours: float,
    friend_ids: set[str],
    persona_store: Any = None,
    persona_snippet_max_chars: int = 80,
) -> list[dict]:
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
        if favorability < float(threshold):
            continue

        user_state = _normalize_user_state(now, proactive_state.get(str(user_id), {}))
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
        candidates.append(
            {
                "user_id": str(user_id),
                "nickname": nickname,
                "favorability": favorability,
                "last_chat": _format_last_chat(last_interaction),
                "warmth": _find_relation_warmth(inner_state, nickname, str(user_id)),
                "persona_snippet": persona_snippet,
            }
        )

    candidates.sort(key=lambda item: item["favorability"], reverse=True)
    return candidates[:10]


def _build_fallback_candidates(
    *,
    friend_profiles: Dict[str, Dict[str, Any]],
    proactive_state: Dict[str, Dict[str, Any]],
    inner_state: dict,
    now: datetime,
    idle_hours: float,
    persona_store: Any = None,
    persona_snippet_max_chars: int = 80,
) -> list[dict]:
    candidates: list[dict] = []
    now_ts = now.timestamp()
    idle_seconds = max(0.0, float(idle_hours)) * 3600

    for user_id, profile in friend_profiles.items():
        user_state = _normalize_user_state(now, proactive_state.get(str(user_id), {}))
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
        candidates.append(
            {
                "user_id": str(user_id),
                "nickname": nickname,
                "favorability": float(profile.get("favorability", 65.0) or 65.0),
                "last_chat": _format_last_chat(last_interaction),
                "warmth": _find_relation_warmth(inner_state, nickname, str(user_id)),
                "persona_snippet": persona_snippet,
                "last_interaction_ts": last_interaction,
            }
        )

    candidates.sort(
        key=lambda item: (
            bool(item.get("persona_snippet")),
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
        lines.append(line)
    return "\n".join(lines) if lines else "- 无可联系对象"


def _parse_decision(result: str) -> tuple[str, str, str]:
    text = (result or "").strip()
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
    agent_data_dir: Optional[Path] = None,
    persona_store: Any = None,
) -> bool:
    _ = get_user_data, get_level_name, get_activity_status, parse_yaml_response, agent_tool_caller

    if not getattr(plugin_config, "personification_proactive_enabled", True):
        return False
    if not sign_in_available and not bool(
        getattr(plugin_config, "personification_proactive_without_signin", True)
    ):
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
        return False

    now_ts = now.timestamp()
    if last_proactive_at and now_ts - last_proactive_at < min_interval * 60:
        save_proactive_state(proactive_state)
        return False

    inner_state = dict(DEFAULT_STATE)
    if agent_data_dir is not None:
        try:
            inner_state.update(await load_inner_state(Path(agent_data_dir)))
        except Exception as e:
            logger.warning(f"[proactive] load inner_state failed: {e}")

    persona_snippet_max_chars = int(
        getattr(plugin_config, "personification_persona_snippet_max_chars", 150)
    )
    idle_hours = float(getattr(plugin_config, "personification_proactive_idle_hours", 24.0))
    if sign_in_available:
        all_user_data = load_data() or {}
        candidates = _build_candidates(
            all_user_data=all_user_data,
            proactive_state=proactive_state,
            inner_state=inner_state,
            now=now,
            threshold=float(getattr(plugin_config, "personification_proactive_threshold", 60.0)),
            idle_hours=idle_hours,
            friend_ids=friend_ids,
            persona_store=persona_store,
            persona_snippet_max_chars=persona_snippet_max_chars,
        )
    else:
        candidates = _build_fallback_candidates(
            friend_profiles=friend_profiles,
            proactive_state=proactive_state,
            inner_state=inner_state,
            now=now,
            idle_hours=idle_hours,
            persona_store=persona_store,
            persona_snippet_max_chars=persona_snippet_max_chars,
        )
    if not candidates:
        save_proactive_state(proactive_state)
        return False

    system_prompt = _extract_system_prompt(load_prompt())
    timezone_name = getattr(plugin_config, "personification_timezone", "Asia/Shanghai")
    datetime_info = get_current_datetime_info(timezone_name, now)
    elapsed_minutes = int((now_ts - last_proactive_at) / 60) if last_proactive_at else 999999
    user_prompt = PROACTIVE_DECISION_PROMPT.format(
        system_prompt=system_prompt,
        mood=str(inner_state.get("mood", DEFAULT_STATE["mood"])),
        energy=str(inner_state.get("energy", DEFAULT_STATE["energy"])),
        pending_thoughts_formatted=_format_pending_thoughts(inner_state.get("pending_thoughts", [])),
        datetime_info=datetime_info,
        candidates_formatted=_format_candidates(candidates),
        fav="fav",
        last_chat="last_chat",
        warmth="warmth",
        today_count=total_today_count,
        daily_limit=daily_limit,
        elapsed_minutes=elapsed_minutes,
        min_interval=min_interval,
        user_id="user_id",
    )

    decision = await call_ai_api(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
    )
    action, target_user_id, payload = _parse_decision(decision or "")
    payload = _strip_xml_message_content(payload)
    if action != "SEND" or not target_user_id or not payload:
        save_proactive_state(proactive_state)
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
        return False

    await bot.send_private_msg(user_id=int(target_user_id), message=payload)

    user_state = _normalize_user_state(now, proactive_state.get(target_user_id, {}))
    user_state["count"] = int(user_state.get("count", 0) or 0) + 1
    user_state["last_proactive_at"] = now_ts
    proactive_state[target_user_id] = user_state
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
    agent_data_dir: Optional[Path] = None,
    superusers: Optional[set[str]] = None,
    get_user_data: Optional[Callable[[str], Any]] = None,
    build_grounding_context: Optional[Callable[[str, str], Awaitable[str]]] = None,
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
            user_prompt = GROUP_IDLE_TOPIC_PROMPT.format(
                system_prompt=system_prompt,
                mood=str(inner_state.get("mood", DEFAULT_STATE["mood"])),
                energy=str(inner_state.get("energy", DEFAULT_STATE["energy"])),
                datetime_info=datetime_info,
                group_id=group_id,
                group_name=group_name,
                group_style=group_style,
                idle_minutes=elapsed_minutes,
                context_hint=context_hint,
                time_flavor=time_flavor,
                superuser_context=superuser_context,
            )

            try:
                topic = await call_ai_api(
                    [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ]
                )
            except Exception as e:
                logger.warning(f"[group_idle] call_ai_api failed for group {group_id}: {e}")
                continue

            topic = _strip_xml_message_content(topic or "")
            if not topic or topic.startswith("[") or len(topic) < 2:
                logger.info(f"[group_idle] 群 {group_id} AI 返回空内容，跳过")
                continue

            proactive_probability = float(
                max(0.0, min(1.0, getattr(plugin_config, "personification_proactive_probability", 0.15)))
            )
            if random.random() > proactive_probability:
                continue

            try:
                await bot.send_group_msg(group_id=int(group_id), message=topic)
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
                record_group_msg(group_id, real_bot_nickname, topic, is_bot=True)
                sent_count += 1
                logger.info(
                    f"[group_idle] 已向群 {group_name}({group_id}) 发送话题：{topic[:30]}"
                )
                break
            except Exception as e:
                logger.warning(f"[group_idle] send_group_msg failed for group {group_id}: {e}")

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
    agent_data_dir: Optional[Path] = None,
    persona_store: Any = None,
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
            agent_data_dir=agent_data_dir,
            persona_store=persona_store,
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
    agent_data_dir: Optional[Path] = None,
    superusers: Optional[set[str]] = None,
    get_user_data: Optional[Callable[[str], Any]] = None,
    build_grounding_context: Optional[Callable[[str, str], Awaitable[str]]] = None,
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
            agent_data_dir=agent_data_dir,
            superusers=superusers,
            get_user_data=get_user_data,
            build_grounding_context=build_grounding_context,
        )

    return _check_group_idle_topic
