import random
import re
import time
from typing import Any, Callable, Optional, Tuple

try:
    from nonebot.adapters.onebot.v11 import Event
    from nonebot.typing import T_State
except Exception:  # pragma: no cover - fallback for lightweight unit-test stubs
    Event = Any
    T_State = dict[str, Any]


async def personification_rule(
    event: Event,
    state: T_State,
    *,
    sign_in_available: bool,
    get_user_data: Callable[[str], dict],
    user_blacklist: dict[str, float],
    logger: Any,
    group_event_cls: type,
    private_event_cls: type,
    is_group_whitelisted: Callable[[str, list[str]], bool],
    plugin_whitelist: list[str],
    load_prompt: Callable[[str], Any],
    load_proactive_state: Callable[[], dict[str, dict[str, Any]]],
    is_rest_time: Callable[..., bool],
    probability: float,
    looks_like_private_command: Callable[[str], bool],
) -> bool:
    user_id = str(event.user_id)

    if sign_in_available:
        user_data = get_user_data(user_id)
        if user_data.get("is_perm_blacklisted", False):
            return False

    if user_id in user_blacklist:
        if time.time() < user_blacklist[user_id]:
            return False
        del user_blacklist[user_id]
        logger.info(f"用户 {user_id} 的拉黑时间已到，已自动恢复。")

    if isinstance(event, group_event_cls):
        group_id = str(event.group_id)
        if not is_group_whitelisted(group_id, plugin_whitelist):
            return False

        idle_active_state: dict[str, Any] = {}
        try:
            proactive_state = load_proactive_state() or {}
            raw_active_state = proactive_state.get(f"group_idle_active_{group_id}", {})
            if isinstance(raw_active_state, dict):
                until = float(raw_active_state.get("until", 0) or 0)
                if until > time.time():
                    idle_active_state = {
                        "until": until,
                        "topic": str(raw_active_state.get("topic", "") or "").strip()[:30],
                    }
        except Exception as e:
            logger.warning(f"拟人插件: 读取群空闲活跃窗口失败: {e}")
        if idle_active_state:
            state["group_idle_active"] = idle_active_state
        else:
            state.pop("group_idle_active", None)

        is_name_mentioned = False
        try:
            prompt_data = load_prompt(group_id)
            if isinstance(prompt_data, dict):
                names = []
                if prompt_data.get("name"):
                    names.append(str(prompt_data["name"]))
                if isinstance(prompt_data.get("nick_name"), list):
                    names.extend([str(n) for n in prompt_data["nick_name"] if n])
                msg_text = event.get_plaintext()
                for name in names:
                    if name in msg_text:
                        is_name_mentioned = True
                        break
        except Exception as e:
            logger.warning(f"拟人插件: 检查名字提及失败: {e}")

        if event.to_me or is_name_mentioned:
            state["is_random_chat"] = False
            return True

        is_unsuitable_time = not is_rest_time(allow_unsuitable_prob=0.0)
        current_prob = probability * 0.2 if is_unsuitable_time else probability
        if idle_active_state:
            boosted_prob = probability * (0.75 if is_unsuitable_time else 1.35)
            current_prob = min(1.0, max(current_prob, boosted_prob))
        if random.random() < current_prob:
            state["is_random_chat"] = True
            return True
        return False

    if isinstance(event, private_event_cls):
        if looks_like_private_command(event.get_plaintext()):
            return False
        return True

    return False


async def record_msg_rule(_event: Event) -> bool:
    return True


def resolve_record_message(
    event: Any,
    *,
    get_custom_title: Callable[[str], Optional[str]],
    record_group_msg: Callable[[str, str, str], int],
) -> Tuple[Optional[str], bool]:
    """记录群消息，返回 (group_id, should_auto_analyze)。"""
    raw_msg = event.get_plaintext().strip()
    if not raw_msg or raw_msg.startswith("/") or len(raw_msg) >= 500:
        return None, False

    group_id = str(event.group_id)
    user_id = str(event.user_id)
    nickname = event.sender.card or event.sender.nickname or user_id

    custom_title = get_custom_title(user_id)
    if custom_title:
        nickname = custom_title

    count = record_group_msg(group_id, nickname, raw_msg)
    return group_id, count >= 200


async def sticker_chat_rule(
    event: Event,
    *,
    is_group_whitelisted: Callable[[str, list[str]], bool],
    plugin_whitelist: list[str],
    probability: float,
) -> bool:
    if event.to_me:
        return False
    group_id = str(event.group_id)
    if not is_group_whitelisted(group_id, plugin_whitelist):
        return False
    return random.random() < probability


async def poke_rule(
    event: Event,
    *,
    is_group_whitelisted: Callable[[str, list[str]], bool],
    plugin_whitelist: list[str],
    probability: float,
) -> bool:
    target_id = getattr(event, "target_id", None)
    self_id = getattr(event, "self_id", None)
    group_id = getattr(event, "group_id", None)
    if target_id is None or self_id is None or group_id is None:
        return False
    if target_id != self_id:
        return False
    group_id = str(group_id)
    if not is_group_whitelisted(group_id, plugin_whitelist):
        return False
    return random.random() < probability


async def poke_notice_rule(
    event: Event,
    *,
    is_group_whitelisted: Callable[[str, list[str]], bool],
    plugin_whitelist: list[str],
    probability: float,
    logger: Any,
) -> bool:
    notice_type = str(getattr(event, "notice_type", "") or "").strip().lower()
    sub_type = str(getattr(event, "sub_type", "") or "").strip().lower()
    if notice_type != "notify" or sub_type != "poke":
        return False

    target_id = getattr(event, "target_id", None)
    self_id = getattr(event, "self_id", None)
    group_id = getattr(event, "group_id", None)
    if target_id is None or self_id is None or group_id is None:
        logger.debug("收到 notify 事件，但缺少 poke 所需字段，已忽略。")
        return False

    logger.info(f"收到戳一戳事件: target_id={target_id}, self_id={self_id}")
    if target_id != self_id:
        return False
    group_id = str(group_id)
    if not is_group_whitelisted(group_id, plugin_whitelist):
        logger.info(f"群 {group_id} 不在白名单 {plugin_whitelist} 或动态白名单中")
        return False
    res = random.random() < probability
    logger.info(f"戳一戳响应判定: 概率={probability}, 结果={res}")
    return res


def split_text_into_segments(text: str) -> list[str]:
    pattern = r"([。！？!?\n]+|[…]{1,2}|[.]{3,6})"
    parts = re.split(pattern, text)
    segments = []
    buffer = ""

    for part in parts:
        if not part:
            continue
        if re.match(pattern, part):
            buffer += part
            segments.append(buffer)
            buffer = ""
        else:
            if buffer:
                segments.append(buffer)
                buffer = ""
            buffer = part

    if buffer:
        segments.append(buffer)
    return segments


def split_segment_if_long(segment: str, max_chars: int) -> list[str]:
    if max_chars <= 0 or len(segment) <= max_chars:
        return [segment]

    sub_pattern = r"([，；,;]+)"
    sub_parts = re.split(sub_pattern, segment)
    result: list[str] = []
    buffer = ""

    for part in sub_parts:
        if not part:
            continue
        if re.match(sub_pattern, part):
            buffer += part
            if len(buffer) >= max_chars // 2:
                result.append(buffer)
                buffer = ""
        else:
            if buffer and len(buffer) + len(part) > max_chars:
                result.append(buffer)
                buffer = part
            else:
                buffer += part

    if buffer:
        result.append(buffer)

    final: list[str] = []
    for text in result:
        while len(text) > max_chars:
            final.append(text[:max_chars])
            text = text[max_chars:]
        if text:
            final.append(text)
    return final if final else [segment]
