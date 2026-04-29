import random
import re
import time
from typing import Any, Callable, Optional, Tuple

from ..core.message_relations import build_event_relation_metadata
from ..core.group_roles import extract_sender_role
from ..core.group_mute import is_group_muted
from ..core.target_inference import TARGET_BOT, TARGET_OTHERS, infer_message_target

try:
    from nonebot.adapters.onebot.v11 import Event
    from nonebot.typing import T_State
except Exception:  # pragma: no cover - fallback for lightweight unit-test stubs
    Event = Any
    T_State = dict[str, Any]


def _normalize_topic_text(text: Any) -> str:
    normalized = str(text or "").strip().lower()
    return "".join(ch for ch in normalized if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def _topic_related(message_text: str, topic: str) -> bool:
    normalized_text = _normalize_topic_text(message_text)
    normalized_topic = _normalize_topic_text(topic)
    if len(normalized_text) < 2 or len(normalized_topic) < 2:
        return False
    if normalized_topic in normalized_text or normalized_text in normalized_topic:
        return True

    max_span = min(6, len(normalized_topic))
    for span in range(max_span, 1, -1):
        for start in range(0, len(normalized_topic) - span + 1):
            if normalized_topic[start : start + span] in normalized_text:
                return True
    return False


def _detect_solo_speaker_follow(
    recent_msgs: list[dict[str, Any]],
    *,
    current_user_id: str,
    current_text: str,
    now_ts: float | None = None,
) -> dict[str, Any]:
    if not recent_msgs:
        return {}
    current_user = str(current_user_id or "").strip()
    if not current_user:
        return {}
    current_time = float(now_ts or time.time())
    recent_window = [
        msg
        for msg in list(recent_msgs)[-8:]
        if isinstance(msg, dict) and current_time - float(msg.get("time", 0) or 0) <= 6 * 60
    ]
    if not recent_window:
        return {}
    recent_non_bot = [msg for msg in recent_window if not bool(msg.get("is_bot"))]
    if len(recent_non_bot) < 3:
        return {}
    tail = recent_non_bot[-4:]
    same_user_msgs = [msg for msg in tail if str(msg.get("user_id", "") or "").strip() == current_user]
    if len(same_user_msgs) < 3:
        return {}
    if str(tail[-1].get("user_id", "") or "").strip() != current_user:
        return {}
    if any(bool(msg.get("is_bot")) for msg in recent_window[-6:]):
        return {}

    topic_seed = str(same_user_msgs[-1].get("content", "") or "").strip()
    if current_text:
        topic_match = any(
            _topic_related(current_text, str(msg.get("content", "") or "").strip())
            or _topic_related(str(msg.get("content", "") or "").strip(), current_text)
            for msg in same_user_msgs[-3:]
        )
        if not topic_match and topic_seed and not _topic_related(current_text, topic_seed):
            return {}

    return {
        "user_id": current_user,
        "count": len(same_user_msgs),
        "topic": topic_seed[:80],
    }


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
    group_chat_follow_probability: float,
    looks_like_private_command: Callable[[str], bool],
    get_recent_group_msgs: Optional[Callable[[str, int], list[dict]]] = None,
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
        if is_group_muted(group_id):
            logger.debug(f"拟人插件：群 {group_id} 处于 bot 禁言期，本轮不进入回复流程。")
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

        group_chat_active_state: dict[str, Any] = {}
        try:
            proactive_state = load_proactive_state() or {}
            raw_chat_state = proactive_state.get(f"group_chat_active_{group_id}", {})
            if isinstance(raw_chat_state, dict):
                until = float(raw_chat_state.get("until", 0) or 0)
                if until > time.time():
                    group_chat_active_state = {
                        "until": until,
                        "topic": str(raw_chat_state.get("topic", "") or "").strip()[:30],
                        "last_user_id": str(raw_chat_state.get("last_user_id", "") or "").strip(),
                    }
        except Exception as e:
            logger.warning(f"拟人插件: 读取群聊活跃窗口失败: {e}")
        if group_chat_active_state:
            state["active_followup"] = group_chat_active_state
        else:
            state.pop("active_followup", None)

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
            state["message_target"] = TARGET_BOT
            return True

        plain_text = str(event.get_plaintext() or "").strip()
        msg_len = len(plain_text)
        if get_recent_group_msgs is not None:
            try:
                recent_msgs = get_recent_group_msgs(group_id, 8)
            except Exception:
                recent_msgs = []
            message_target = infer_message_target(
                event,
                bot_self_id=str(getattr(event, "self_id", "") or ""),
                recent_group_msgs=recent_msgs,
            )
            state["message_target"] = message_target
        else:
            message_target = state.get("message_target", "")

        solo_speaker_follow: dict[str, Any] = {}
        if get_recent_group_msgs is not None:
            solo_speaker_follow = _detect_solo_speaker_follow(
                recent_msgs=recent_msgs,
                current_user_id=user_id,
                current_text=plain_text,
            )
        if solo_speaker_follow:
            state["solo_speaker_follow"] = solo_speaker_follow
        else:
            state.pop("solo_speaker_follow", None)

        if message_target == TARGET_OTHERS and not solo_speaker_follow:
            state["is_random_chat"] = False
            return False

        if group_chat_active_state:
            last_user_id = str(group_chat_active_state.get("last_user_id", "") or "").strip()
            topic = str(group_chat_active_state.get("topic", "") or "").strip()
            same_user = bool(last_user_id) and last_user_id == user_id
            related_topic = _topic_related(plain_text, topic)
            current_prob = float(max(0.0, min(1.0, group_chat_follow_probability)))

            if same_user:
                current_prob = max(current_prob, 0.92)
            elif related_topic:
                current_prob = max(current_prob, 0.78)
            else:
                current_prob *= 0.35

            if random.random() < min(1.0, current_prob):
                state["is_random_chat"] = False
                state["active_followup"] = group_chat_active_state
                return True

        if solo_speaker_follow:
            current_prob = min(1.0, max(probability * 1.45, 0.72))
            if msg_len <= 4:
                current_prob = max(current_prob, 0.68)
            if random.random() < current_prob:
                state["is_random_chat"] = True
                return True

        is_unsuitable_time = not is_rest_time(allow_unsuitable_prob=0.0)
        current_prob = probability * (0.55 if is_unsuitable_time else 1.0)
        if message_target == TARGET_BOT:
            current_prob = max(current_prob, probability)
        if msg_len <= 1:
            current_prob *= 0.5
        elif msg_len <= 4:
            current_prob *= 0.8
        elif msg_len >= 24:
            current_prob *= 1.1
        if idle_active_state:
            topic = str(idle_active_state.get("topic", "") or "").strip()
            if topic and _topic_related(plain_text, topic):
                current_prob = min(1.0, max(current_prob, probability * 0.9))
            else:
                current_prob = min(current_prob, probability * 0.8)
        if random.random() < min(1.0, current_prob):
            state["is_random_chat"] = True
            return True
        return False

    if isinstance(event, private_event_cls):
        if looks_like_private_command(event.get_plaintext()):
            return False
        state["message_target"] = TARGET_BOT
        return True

    return False


async def record_msg_rule(_event: Event) -> bool:
    return True


def _extract_recordable_group_message(event: Any) -> tuple[str, int, str]:
    plain_text = str(event.get_plaintext() or "").strip()
    message = getattr(event, "message", None)
    if message is None:
        return plain_text, 0, ""

    text_parts: list[str] = []
    visual_parts: list[str] = []
    image_count = 0
    try:
        for seg in message:
            seg_type = str(getattr(seg, "type", "") or "").strip().lower()
            data = getattr(seg, "data", {}) or {}
            if seg_type == "text":
                text = str(data.get("text", "") or "")
                if text:
                    text_parts.append(text)
            elif seg_type == "face":
                face_id = str(data.get("id", "") or "").strip()
                token = f"[表情id:{face_id}]" if face_id else "[表情]"
                text_parts.append(token)
                visual_parts.append(token)
            elif seg_type == "mface":
                summary = str(data.get("summary", "") or "表情包").strip() or "表情包"
                token = f"[{summary}]"
                text_parts.append(token)
                visual_parts.append(token)
            elif seg_type == "image":
                image_count += 1
                token = "[图片]"
                text_parts.append(token)
                visual_parts.append(token)
    except Exception:
        return plain_text, 0, ""

    content = "".join(text_parts).strip() or plain_text
    if not content and image_count > 0:
        content = "[发送了一张图片]" if image_count == 1 else f"[发送了{image_count}张图片]"
    visual_summary = " ".join(visual_parts[:6]).strip()
    return content, image_count, visual_summary


def resolve_record_message(
    event: Any,
    *,
    get_custom_title: Callable[[str], Optional[str]],
    record_group_msg: Callable[..., int],
    should_trigger_auto_analyze: Optional[Callable[[str, int], bool]] = None,
) -> Tuple[Optional[str], bool]:
    """记录群消息，返回 (group_id, should_auto_analyze)。"""
    if bool(getattr(event, "_personification_muted_recorded", False)):
        return None, False

    raw_msg, image_count, visual_summary = _extract_recordable_group_message(event)
    if not raw_msg or raw_msg.startswith("/") or len(raw_msg) >= 500:
        return None, False

    group_id = str(event.group_id)
    user_id = str(event.user_id)
    self_id = str(getattr(event, "self_id", "") or "").strip()
    sender = getattr(event, "sender", None)
    nickname = (
        getattr(sender, "card", None)
        or getattr(sender, "nickname", None)
        or user_id
    )

    custom_title = get_custom_title(user_id)
    if custom_title:
        nickname = custom_title

    is_bot_message = bool(self_id) and user_id == self_id
    relation_metadata = build_event_relation_metadata(
        event,
        bot_self_id=self_id,
        source_kind="bot" if is_bot_message else "user",
    )
    count = record_group_msg(
        group_id,
        nickname,
        raw_msg,
        is_bot=is_bot_message,
        user_id=user_id,
        sender_role=extract_sender_role(event),
        image_count=image_count,
        visual_summary=visual_summary,
        **relation_metadata,
    )
    if should_trigger_auto_analyze is None:
        return group_id, count >= 200
    return group_id, bool(should_trigger_auto_analyze(group_id, count))


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
    if is_group_muted(group_id):
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
    if is_group_muted(group_id):
        logger.debug(f"拟人插件：群 {group_id} 处于 bot 禁言期，忽略戳一戳响应。")
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
