from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .context_policy import stringify_history_content
from .group_relations import summarize_group_relationships
from .group_roles import render_group_role_label


@dataclass(frozen=True)
class ShortTermTopicState:
    current_message_id: str = ""
    current_thread_id: str = ""
    current_speaker_id: str = ""
    current_speaker: str = ""
    current_text: str = ""
    reply_to_user_id: str = ""
    reply_to_speaker: str = ""
    mentioned_speakers: tuple[str, ...] = ()
    thread_participants: tuple[str, ...] = ()
    bot_in_current_thread: bool = False
    is_reply_to_bot: bool = False
    parallel_thread_count: int = 0


@dataclass(frozen=True)
class GroupConversationContext:
    recent_messages: list[dict[str, Any]] = field(default_factory=list)
    current_thread_id: str = ""
    current_thread_messages: list[dict[str, Any]] = field(default_factory=list)
    other_thread_summaries: list[str] = field(default_factory=list)
    topic_state: ShortTermTopicState = field(default_factory=ShortTermTopicState)
    speaker_relations: dict[str, str] = field(default_factory=dict)
    active_topics: list[str] = field(default_factory=list)
    repeat_clusters: list[dict[str, Any]] = field(default_factory=list)
    quote_chain: list[dict[str, Any]] = field(default_factory=list)
    bot_recent_replies: list[str] = field(default_factory=list)
    emotional_climate: str = "未评估"
    rendered_context: str = ""
    relationship_hint: str = ""


def build_group_conversation_context(
    *,
    recent_messages: list[dict[str, Any]],
    trigger_msg_id: str = "",
    trigger_user_id: str = "",
    bot_self_id: str = "",
    repeat_clusters: list[dict[str, Any]] | None = None,
    bot_recent_replies: list[str] | None = None,
    emotional_climate: str = "未评估",
) -> GroupConversationContext:
    messages = [msg for msg in list(recent_messages or []) if isinstance(msg, dict)]
    speaker_relations: dict[str, str] = {}
    active_topics: list[str] = []
    seen_topics: set[str] = set()
    for msg in messages[-30:]:
        user_id = str(msg.get("user_id", "") or "").strip()
        nickname = str(
            msg.get("nickname")
            or msg.get("speaker")
            or msg.get("user_name")
            or msg.get("role")
            or ""
        ).strip()
        if user_id and nickname:
            speaker_relations[user_id] = nickname
        content = stringify_history_content(msg.get("content", "")).strip()
        if not content:
            continue
        # 带 nickname 一起存，防止 LLM 把无关消息内容串起来误关联
        # （如"鑫仔说地震"+"流影说自己在浙江" 被合成"地震在浙江"）
        speaker_label = nickname or user_id or "未知"
        topic = f"{speaker_label}: {content[:80]}"
        if topic not in seen_topics:
            seen_topics.add(topic)
            active_topics.append(topic)
    rendered_context = render_group_context_structured(messages, trigger_msg_id=trigger_msg_id)
    current_thread_id = _resolve_current_thread_id(messages, trigger_msg_id=trigger_msg_id)
    current_thread_messages = [
        msg for msg in messages if current_thread_id and str(msg.get("thread_id", "") or "") == current_thread_id
    ][-12:]
    other_thread_summaries = _summarize_other_threads(messages, current_thread_id=current_thread_id)
    relationship_hint = summarize_group_relationships(
        messages,
        trigger_msg_id=trigger_msg_id,
        trigger_user_id=trigger_user_id,
        bot_self_id=bot_self_id,
    )
    topic_state = _build_short_term_topic_state(
        messages=messages,
        current_thread_id=current_thread_id,
        current_thread_messages=current_thread_messages,
        trigger_msg_id=trigger_msg_id,
        bot_self_id=bot_self_id,
        speaker_relations=speaker_relations,
    )
    return GroupConversationContext(
        recent_messages=messages[-30:],
        current_thread_id=current_thread_id,
        current_thread_messages=current_thread_messages,
        other_thread_summaries=other_thread_summaries,
        topic_state=topic_state,
        speaker_relations=speaker_relations,
        active_topics=active_topics[-6:],
        repeat_clusters=list(repeat_clusters or [])[:5],
        quote_chain=_build_quote_chain(messages, trigger_msg_id=trigger_msg_id),
        bot_recent_replies=[
            str(item or "").strip()[:120]
            for item in list(bot_recent_replies or [])[:5]
            if str(item or "").strip()
        ],
        emotional_climate=str(emotional_climate or "未评估").strip()[:80] or "未评估",
        rendered_context=rendered_context,
        relationship_hint=relationship_hint,
    )


def render_group_conversation_context(context: GroupConversationContext) -> str:
    parts: list[str] = []
    topic_state_block = render_short_term_topic_state(context.topic_state)
    if topic_state_block:
        parts.append(topic_state_block)
    if context.current_thread_messages:
        parts.append(
            "当前对话线程（优先理解和回复这一组，除非被 @ 或明确要求，不要混到其他线程）：\n"
            + render_group_context_structured(
                context.current_thread_messages,
                trigger_msg_id=str(context.current_thread_messages[-1].get("message_id", "") or ""),
            )
        )
    if context.other_thread_summaries:
        parts.append(
            "其他同时进行的群聊线程（只作背景，不要主动串线总结）：\n"
            + "\n".join(f"- {item}" for item in context.other_thread_summaries[:4])
        )
    if context.rendered_context:
        parts.append(context.rendered_context)
    if context.active_topics:
        # 每条话题单独一行 + 显式 speaker 标签，避免 LLM 把不同人说的话题串起来
        topics_block = "\n".join(f"- {topic}" for topic in context.active_topics[:8])
        parts.append(
            "近段发言线索（每行是不同发言者的一句话；不要把不同人说的内容关联成同一件事）：\n"
            + topics_block
        )
    if context.quote_chain:
        quote_lines = []
        for item in context.quote_chain[-5:]:
            speaker = str(item.get("nickname") or item.get("speaker") or item.get("user_id") or "未知").strip()
            content = stringify_history_content(item.get("content", "")).strip()
            if content:
                quote_lines.append(f"- {speaker}: {content[:120]}")
        if quote_lines:
            parts.append("引用链：\n" + "\n".join(quote_lines))
    if context.bot_recent_replies:
        parts.append("bot 最近回复：" + "；".join(context.bot_recent_replies[:5]))
    if context.emotional_climate:
        parts.append(f"对话氛围：{context.emotional_climate}")
    return "\n".join(part for part in parts if part).strip()


def render_short_term_topic_state(state: ShortTermTopicState) -> str:
    if not isinstance(state, ShortTermTopicState) or not (state.current_text or state.current_message_id):
        return ""
    lines = [
        "本轮短期话题状态（结构化线索；只用于判断接谁的话，不替代语义判断）：",
        f"- 当前消息：{state.current_speaker or state.current_speaker_id or '未知'}: {state.current_text[:100] or '[非文本消息]'}",
    ]
    if state.current_thread_id:
        participants = "、".join(state.thread_participants[:6]) if state.thread_participants else "未知"
        lines.append(f"- 当前线程：{state.current_thread_id}；参与者：{participants}")
    else:
        lines.append("- 当前线程：未分配；只按当前消息和引用/提及关系判断")
    if state.reply_to_speaker or state.reply_to_user_id:
        lines.append(
            f"- 当前消息回复对象：{state.reply_to_speaker or state.reply_to_user_id}"
            + ("（bot）" if state.is_reply_to_bot else "")
        )
    if state.mentioned_speakers:
        lines.append(f"- 当前消息提及：{'、'.join(state.mentioned_speakers[:6])}")
    lines.append(f"- bot 是否在当前线程最近发过言：{'是' if state.bot_in_current_thread else '否'}")
    if state.parallel_thread_count > 0:
        lines.append(f"- 同时存在其它线程：{state.parallel_thread_count} 个；只作背景，不要串线总结")
    lines.append(
        "使用纪律：优先围绕当前消息和当前线程接话；如果当前消息是在回复别人且没有 @/引用 bot，"
        "不要把它当成对 bot 的提问；被明确 cue 到时，以 cue 和引用链为准。"
    )
    return "\n".join(lines)


def render_topic_state_trace_detail(state: ShortTermTopicState) -> str:
    if not isinstance(state, ShortTermTopicState):
        return ""
    thread = state.current_thread_id or "-"
    speaker = state.current_speaker_id or state.current_speaker or "-"
    participants = len(state.thread_participants)
    return (
        f"topic_thread={thread} "
        f"topic_speaker={speaker} "
        f"reply_to_bot={str(bool(state.is_reply_to_bot)).lower()} "
        f"bot_in_thread={str(bool(state.bot_in_current_thread)).lower()} "
        f"parallel_threads={int(state.parallel_thread_count or 0)} "
        f"participants={participants}"
    )


def _build_quote_chain(messages: list[dict[str, Any]], *, trigger_msg_id: str = "") -> list[dict[str, Any]]:
    by_id = {
        str(msg.get("message_id", "") or "").strip(): msg
        for msg in messages
        if str(msg.get("message_id", "") or "").strip()
    }
    if not by_id:
        return []
    current_id = str(trigger_msg_id or "").strip()
    if not current_id or current_id not in by_id:
        current_id = str(messages[-1].get("message_id", "") or "").strip() if messages else ""
    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    while current_id and current_id in by_id and current_id not in seen and len(chain) < 8:
        seen.add(current_id)
        current = by_id[current_id]
        chain.append(current)
        current_id = str(current.get("reply_to_msg_id", "") or "").strip()
    return list(reversed(chain))


def _resolve_current_thread_id(messages: list[dict[str, Any]], *, trigger_msg_id: str = "") -> str:
    if not messages:
        return ""
    if trigger_msg_id:
        for msg in reversed(messages):
            if str(msg.get("message_id", "") or "").strip() == trigger_msg_id:
                return str(msg.get("thread_id", "") or "").strip()
    return str(messages[-1].get("thread_id", "") or "").strip()


def _current_message(messages: list[dict[str, Any]], *, trigger_msg_id: str = "") -> dict[str, Any]:
    if trigger_msg_id:
        for msg in reversed(messages):
            if str(msg.get("message_id", "") or "").strip() == trigger_msg_id:
                return msg
    return messages[-1] if messages else {}


def _speaker_label(msg: dict[str, Any], *, fallback_user_id: str = "") -> str:
    return str(
        msg.get("nickname")
        or msg.get("speaker")
        or msg.get("user_name")
        or msg.get("role")
        or fallback_user_id
        or "未知"
    ).strip()


def _ordered_unique(items: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return tuple(out)


def _build_short_term_topic_state(
    *,
    messages: list[dict[str, Any]],
    current_thread_id: str,
    current_thread_messages: list[dict[str, Any]],
    trigger_msg_id: str = "",
    bot_self_id: str = "",
    speaker_relations: dict[str, str] | None = None,
) -> ShortTermTopicState:
    if not messages:
        return ShortTermTopicState()
    relations = dict(speaker_relations or {})
    current = _current_message(messages, trigger_msg_id=trigger_msg_id)
    current_user_id = str(current.get("user_id", "") or "").strip()
    current_speaker = _speaker_label(current, fallback_user_id=current_user_id)
    current_text = stringify_history_content(current.get("content", "")).strip()
    reply_to_user_id = str(current.get("reply_to_user_id", "") or "").strip()
    reply_to_speaker = relations.get(reply_to_user_id, reply_to_user_id)
    reply_to_msg_id = str(current.get("reply_to_msg_id", "") or "").strip()
    if reply_to_msg_id:
        for msg in reversed(messages):
            if str(msg.get("message_id", "") or "").strip() != reply_to_msg_id:
                continue
            reply_to_user_id = reply_to_user_id or str(msg.get("user_id", "") or "").strip()
            reply_to_speaker = _speaker_label(msg, fallback_user_id=reply_to_user_id)
            break

    mentioned_ids_raw = current.get("mentioned_ids", [])
    mentioned_ids = mentioned_ids_raw if isinstance(mentioned_ids_raw, list) else []
    mentioned_speakers = _ordered_unique(
        [relations.get(str(uid or "").strip(), str(uid or "").strip()) for uid in mentioned_ids]
    )

    thread_messages = current_thread_messages or ([current] if current else [])
    participants = _ordered_unique(
        [
            _speaker_label(msg, fallback_user_id=str(msg.get("user_id", "") or ""))
            for msg in thread_messages
            if isinstance(msg, dict)
        ]
    )
    bot_id = str(bot_self_id or "").strip()
    bot_in_thread = False
    is_reply_to_bot = False
    for msg in thread_messages:
        if not isinstance(msg, dict):
            continue
        msg_user_id = str(msg.get("user_id", "") or "").strip()
        source_kind = str(msg.get("source_kind", "") or "").strip().lower()
        if source_kind == "bot_reply" or (bot_id and msg_user_id == bot_id) or str(msg.get("role", "") or "") == "assistant":
            bot_in_thread = True
    if bot_id and reply_to_user_id == bot_id:
        is_reply_to_bot = True
    elif reply_to_msg_id:
        for msg in messages:
            if str(msg.get("message_id", "") or "").strip() != reply_to_msg_id:
                continue
            source_kind = str(msg.get("source_kind", "") or "").strip().lower()
            msg_user_id = str(msg.get("user_id", "") or "").strip()
            is_reply_to_bot = source_kind == "bot_reply" or (bot_id and msg_user_id == bot_id)
            break

    thread_ids = {
        str(msg.get("thread_id", "") or "").strip()
        for msg in messages
        if str(msg.get("thread_id", "") or "").strip()
    }
    if current_thread_id:
        thread_ids.discard(str(current_thread_id))
    return ShortTermTopicState(
        current_message_id=str(current.get("message_id", "") or "").strip(),
        current_thread_id=str(current_thread_id or "").strip(),
        current_speaker_id=current_user_id,
        current_speaker=current_speaker,
        current_text=current_text[:160],
        reply_to_user_id=reply_to_user_id,
        reply_to_speaker=reply_to_speaker,
        mentioned_speakers=mentioned_speakers,
        thread_participants=participants,
        bot_in_current_thread=bot_in_thread,
        is_reply_to_bot=is_reply_to_bot,
        parallel_thread_count=len(thread_ids),
    )


def _summarize_other_threads(messages: list[dict[str, Any]], *, current_thread_id: str = "") -> list[str]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for msg in messages:
        thread_id = str(msg.get("thread_id", "") or "").strip()
        if not thread_id or thread_id == current_thread_id:
            continue
        grouped.setdefault(thread_id, []).append(msg)
    summaries: list[str] = []
    for thread_id, items in grouped.items():
        last = items[-1]
        speaker = str(last.get("nickname") or last.get("speaker") or last.get("user_id") or "未知").strip()
        content = stringify_history_content(last.get("content", "")).strip()
        if content:
            summaries.append(f"{thread_id}: 最近 {speaker}: {content[:80]}")
    return summaries[-4:]


def render_group_context_structured(messages: list[dict[str, Any]], trigger_msg_id: str = "") -> str:
    if not messages:
        return ""

    by_message_id: dict[str, dict[str, Any]] = {}
    by_user_id: dict[str, str] = {}
    for msg in messages:
        message_id = str(msg.get("message_id", "") or "").strip()
        if message_id:
            by_message_id[message_id] = msg
        user_id = str(msg.get("user_id", "") or "").strip()
        speaker = str(
            msg.get("nickname")
            or msg.get("speaker")
            or msg.get("user_name")
            or msg.get("role")
            or "未知"
        ).strip()
        if user_id and speaker:
            by_user_id[user_id] = speaker

    def _resolve_user_name(user_id: str) -> str:
        return by_user_id.get(user_id, user_id or "未知")

    lines: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = stringify_history_content(msg.get("content", ""))[:120]
        if not content:
            continue
        nickname = str(
            msg.get("nickname")
            or msg.get("speaker")
            or msg.get("user_name")
            or msg.get("role")
            or "未知"
        )
        user_id = str(msg.get("user_id", "") or "")
        message_id = str(msg.get("message_id", "") or "")
        label = "当前消息" if trigger_msg_id and message_id == trigger_msg_id else "群聊"
        relation_parts: list[str] = []

        reply_to_msg_id = str(msg.get("reply_to_msg_id", "") or "")
        if reply_to_msg_id:
            referenced = by_message_id.get(reply_to_msg_id)
            if referenced:
                reply_name = str(
                    referenced.get("nickname")
                    or referenced.get("speaker")
                    or referenced.get("user_id")
                    or "未知"
                )
            else:
                reply_name = _resolve_user_name(str(msg.get("reply_to_user_id", "") or ""))
            relation_parts.append(f"回复{reply_name}")
            if label != "当前消息":
                label = "被引用" if trigger_msg_id and reply_to_msg_id == trigger_msg_id else label
        elif msg.get("reply_to_user_id"):
            relation_parts.append(f"回复{_resolve_user_name(str(msg.get('reply_to_user_id', '') or ''))}")

        raw_mentions = msg.get("mentioned_ids", [])
        mentioned_ids = raw_mentions if isinstance(raw_mentions, list) else []
        if mentioned_ids:
            mention_names = [f"@{_resolve_user_name(str(uid or ''))}" for uid in mentioned_ids[:4] if str(uid or "")]
            if mention_names:
                relation_parts.append("提及" + " ".join(mention_names))

        if msg.get("is_at_bot"):
            relation_parts.append("@Bot=是")
        role_label = render_group_role_label(msg.get("sender_role", ""))
        if role_label:
            relation_parts.append(f"身份={role_label}")

        scene = str(msg.get("scene", "") or "").strip()
        scene_map = {
            "private": "私聊",
            "direct": "对你说",
            "observe": "群聊旁观",
            "reply": "机器人回复",
        }
        if scene in scene_map:
            relation_parts.append(f"场景={scene_map[scene]}")
        source_kind = str(msg.get("source_kind", "") or "").strip().lower()
        source_map = {
            "bot_reply": "拟人回复",
            "plugin": "其他插件输出",
            "plugin_command": "用户调用其它插件/命令",
            "system": "系统消息",
            "bot": "机器人消息",
            "mface": "表情包",
            "image": "图片",
        }
        if source_kind in source_map:
            relation_parts.append(f"来源={source_map[source_kind]}")
        thread_id = str(msg.get("thread_id", "") or "").strip()
        if thread_id:
            relation_parts.append(f"线程={thread_id}")

        relation = "|".join(relation_parts) if relation_parts else "普通发言"
        lines.append(f"[{label}][{nickname}|uid={user_id}|{relation}] {content}")
    return "\n".join(lines)


__all__ = [
    "GroupConversationContext",
    "ShortTermTopicState",
    "build_group_conversation_context",
    "render_short_term_topic_state",
    "render_topic_state_trace_detail",
    "render_group_context_structured",
    "render_group_conversation_context",
]
