import asyncio
import re
import time
from typing import Any, Callable, Dict


_GROUP_BATCH_DELAY_SECONDS = 1.2
_PRIVATE_BATCH_DELAY_SECONDS = 0.8
_MAX_BATCH_EVENTS = 8
_PROCESS_RESPONSE_TIMEOUT_SECONDS = 75.0
_DIRECT_REPLY_PREEMPT_SECONDS = 8.0


def _has_reply_semantics(event: Any) -> bool:
    for attr in ("reply", "quoted", "quote"):
        value = getattr(event, attr, None)
        if value:
            return True
    if getattr(event, "reply_to_message_id", None):
        return True
    return False


def _extract_sender_name(event: Any) -> str:
    sender = getattr(event, "sender", None)
    if sender is None:
        return str(getattr(event, "user_id", "") or "未知")
    return str(
        getattr(sender, "card", None)
        or getattr(sender, "nickname", None)
        or getattr(event, "user_id", "")
        or "未知"
    ).strip()


def _extract_plain_text(event: Any) -> str:
    parts: list[str] = []
    message = getattr(event, "message", None)
    if message is None:
        return ""
    try:
        for seg in message:
            seg_type = getattr(seg, "type", None)
            data = getattr(seg, "data", {}) or {}
            if seg_type == "text":
                parts.append(str(data.get("text", "") or ""))
            elif seg_type == "mface":
                parts.append(str(data.get("summary", "") or ""))
            elif seg_type == "face":
                parts.append(f"[表情id:{str(data.get('id', '') or '').strip()}]")
    except Exception:
        return ""
    return "".join(parts).strip()


def _normalize_repeat_key(text: str) -> str:
    normalized = re.sub(r"\s+", "", str(text or "").strip().lower())
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "", normalized)
    return normalized[:120]


def _is_direct_mention(event: Any, bot_self_id: str) -> bool:
    if not bot_self_id:
        return False
    try:
        for seg in getattr(event, "message", []) or []:
            if getattr(seg, "type", None) != "at":
                continue
            qq = str((getattr(seg, "data", {}) or {}).get("qq", "")).strip()
            if qq == bot_self_id:
                return True
    except Exception:
        return False
    return False


def _is_reply_to_bot(event: Any, bot_self_id: str) -> bool:
    if not bot_self_id:
        return False
    reply = getattr(event, "reply", None)
    if not reply:
        return False
    sender = getattr(reply, "sender", None)
    if sender is None and isinstance(reply, dict):
        sender = reply.get("sender")
    reply_user_id = ""
    if isinstance(sender, dict):
        reply_user_id = str(sender.get("user_id", "") or "").strip()
    elif sender is not None:
        reply_user_id = str(getattr(sender, "user_id", "") or "").strip()
    if not reply_user_id:
        reply_user_id = str(getattr(reply, "self_id", "") or "").strip()
        if not reply_user_id and isinstance(reply, dict):
            reply_user_id = str(reply.get("self_id", "") or "").strip()
    return bool(reply_user_id and reply_user_id == bot_self_id)


def _select_merged_event(events: list[Any]) -> Any:
    for event in reversed(events):
        if _has_reply_semantics(event):
            return event
    return events[-1]


def _serialize_batched_event(item: dict[str, Any]) -> dict[str, Any]:
    event = item.get("event")
    return {
        "message_id": str(getattr(event, "message_id", "") or "").strip(),
        "user_id": str(getattr(event, "user_id", "") or "").strip(),
        "group_id": str(getattr(event, "group_id", "") or "").strip(),
        "sender_name": _extract_sender_name(event),
        "text": _extract_plain_text(event)[:200],
        "is_direct_mention": bool(item.get("is_direct_mention")),
        "is_reply_to_bot": bool(item.get("is_reply_to_bot")),
        "has_reply_semantics": _has_reply_semantics(event),
    }


def _build_repeat_clusters(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    clusters: dict[str, dict[str, Any]] = {}
    for item in items:
        text = _extract_plain_text(item.get("event"))
        key = _normalize_repeat_key(text)
        if not key:
            continue
        cluster = clusters.setdefault(
            key,
            {
                "text": text.strip()[:120],
                "count": 0,
                "speaker_ids": set(),
                "speakers": [],
            },
        )
        cluster["count"] += 1
        user_id = str(getattr(item.get("event"), "user_id", "") or "").strip()
        speaker = _extract_sender_name(item.get("event"))
        if user_id and user_id not in cluster["speaker_ids"]:
            cluster["speaker_ids"].add(user_id)
            cluster["speakers"].append(speaker)

    results: list[dict[str, Any]] = []
    for cluster in clusters.values():
        speaker_count = len(cluster["speaker_ids"])
        count = int(cluster["count"] or 0)
        if speaker_count >= 2 or count >= 3:
            results.append(
                {
                    "text": str(cluster["text"] or ""),
                    "count": count,
                    "speakers": list(cluster["speakers"]),
                }
            )
    results.sort(key=lambda item: (-int(item.get("count", 0) or 0), str(item.get("text", "") or "")))
    return results[:3]


def _select_batch_trigger(items: list[dict[str, Any]]) -> tuple[dict[str, Any], str]:
    for item in reversed(items):
        if item.get("is_direct_mention"):
            return item, "direct_mention"
    for item in reversed(items):
        if item.get("is_reply_to_bot"):
            return item, "reply_to_bot"
    selected_event = _select_merged_event([item["event"] for item in items])
    for item in items:
        if item.get("event") is selected_event:
            return item, "reply_semantics" if _has_reply_semantics(selected_event) else "latest"
    return items[-1], "latest"


def _build_combined_message(
    items: list[dict[str, Any]],
    *,
    message_cls: Any,
    message_segment_cls: Any,
) -> Any:
    combined_message = message_cls()
    for index, item in enumerate(items):
        event = item.get("event")
        if event is None:
            continue
        if index > 0:
            combined_message.append(message_segment_cls.text(" "))
        combined_message.extend(getattr(event, "message", message_cls()))
    return combined_message


def _session_key(event: Any, *, group_message_event_cls: Any) -> str:
    user_id = str(getattr(event, "user_id", "") or "")
    if isinstance(event, group_message_event_cls):
        return str(getattr(event, "group_id", "") or "")
    return f"private_{user_id}"


def _batch_delay(event: Any, *, group_message_event_cls: Any) -> float:
    return _GROUP_BATCH_DELAY_SECONDS if isinstance(event, group_message_event_cls) else _PRIVATE_BATCH_DELAY_SECONDS


def _new_entry(delay: float) -> dict[str, Any]:
    return {
        "items": [],
        "pending_items": [],
        "processing": False,
        "active_task": None,
        "processing_started_at": 0.0,
        "current_trigger_type": "",
        "current_is_random_chat": False,
        "timer_task": None,
        "delay": delay,
        "batch_started_at": 0.0,
        "pending_started_at": 0.0,
        "pending_ready": False,
        "current_generation": 0,
        "superseded_generation": 0,
        "newer_batch_for_current": False,
    }


def _trim_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(items) <= _MAX_BATCH_EVENTS:
        return items
    return items[-_MAX_BATCH_EVENTS:]


def _schedule_delay(started_at: float, delay: float, *, immediate: bool = False) -> float:
    if immediate or started_at <= 0:
        return 0.0
    elapsed = max(0.0, time.monotonic() - started_at)
    return max(0.0, float(delay) - elapsed)


def _cancel_timer(entry: dict[str, Any]) -> None:
    timer_task = entry.get("timer_task")
    if timer_task:
        timer_task.cancel()
    entry["timer_task"] = None


def _schedule_timer(
    *,
    entry: dict[str, Any],
    key: str,
    bot: Any,
    wait_seconds: float,
    start_buffer_timer: Callable[[str, Any, float], Any],
) -> None:
    _cancel_timer(entry)
    entry["timer_task"] = start_buffer_timer(key, bot, wait_seconds)


def _promote_pending_batch(entry: dict[str, Any]) -> None:
    entry["items"] = list(entry.get("pending_items", []))
    entry["pending_items"] = []
    entry["batch_started_at"] = float(entry.get("pending_started_at", 0.0) or time.monotonic())
    entry["pending_started_at"] = 0.0
    entry["pending_ready"] = False


def _processing_age_seconds(entry: dict[str, Any]) -> float:
    started_at = float(entry.get("processing_started_at", 0.0) or 0.0)
    if started_at <= 0:
        return 0.0
    return max(0.0, time.monotonic() - started_at)


def _should_preempt_current_batch(entry: dict[str, Any], *, immediate_flush: bool) -> bool:
    if not immediate_flush or not bool(entry.get("processing")):
        return False
    if bool(entry.get("current_is_random_chat")):
        return True
    if bool(entry.get("newer_batch_for_current")):
        return True
    return _processing_age_seconds(entry) >= _DIRECT_REPLY_PREEMPT_SECONDS


async def run_buffer_timer(
    key: str,
    bot: Any,
    *,
    msg_buffer: Dict[str, Dict[str, Any]],
    process_response_logic: Callable[[Any, Any, Dict[str, Any]], Any],
    message_event_cls: Any,
    message_cls: Any,
    message_segment_cls: Any,
    logger: Any,
    finished_exception_cls: Any = None,
    delay: float = 0.0,
) -> None:
    await asyncio.sleep(max(0.0, float(delay or 0.0)))

    entry = msg_buffer.get(key)
    if not isinstance(entry, dict):
        return

    if entry.get("processing"):
        if entry.get("pending_items"):
            entry["pending_ready"] = True
        entry["timer_task"] = None
        return

    items = list(entry.get("items") or [])
    if not items:
        if entry.get("pending_items"):
            _promote_pending_batch(entry)
            _schedule_timer(
                entry=entry,
                key=key,
                bot=bot,
                wait_seconds=0.0,
                start_buffer_timer=lambda _key, _bot, _delay: asyncio.create_task(
                    run_buffer_timer(
                        _key,
                        _bot,
                        msg_buffer=msg_buffer,
                        process_response_logic=process_response_logic,
                        message_event_cls=message_event_cls,
                        message_cls=message_cls,
                        message_segment_cls=message_segment_cls,
                        logger=logger,
                        finished_exception_cls=finished_exception_cls,
                        delay=_delay,
                    )
                ),
            )
            return
        msg_buffer.pop(key, None)
        return

    entry["processing"] = True
    entry["active_task"] = asyncio.current_task()
    entry["processing_started_at"] = time.monotonic()
    entry["timer_task"] = None
    entry["current_generation"] = int(entry.get("current_generation", 0) or 0) + 1
    current_generation = int(entry["current_generation"])
    entry["newer_batch_for_current"] = False
    entry["items"] = []
    entry["batch_started_at"] = 0.0

    trigger_item, trigger_type = _select_batch_trigger(items)
    selected_event = trigger_item.get("event")
    if selected_event is None:
        entry["processing"] = False
        return

    state = dict(trigger_item.get("state") or {})
    events = [item.get("event") for item in items if isinstance(item.get("event"), message_event_cls)]
    if not events:
        entry["processing"] = False
        return

    combined_message = None
    try:
        combined_message = _build_combined_message(
            items,
            message_cls=message_cls,
            message_segment_cls=message_segment_cls,
        )
    except Exception as exc:
        logger.warning(f"拟人插件：拼接消息构建失败，回退单条处理: {exc}")

    serialized_items = [_serialize_batched_event(item) for item in items]
    repeat_clusters = _build_repeat_clusters(items)
    if combined_message is not None:
        state["concatenated_message"] = combined_message
    state["merged_event_context"] = {
        "event_count": len(events),
        "selected_event_index": max(0, events.index(selected_event)),
    }
    state["batched_events"] = serialized_items
    state["batch_trigger"] = {
        "type": trigger_type,
        "message_id": str(getattr(selected_event, "message_id", "") or "").strip(),
        "user_id": str(getattr(selected_event, "user_id", "") or "").strip(),
    }
    state["repeat_clusters"] = repeat_clusters
    state["batch_event_count"] = len(events)
    state["batch_session_key"] = key
    state["batch_runtime_ref"] = {
        "entry": entry,
        "generation": current_generation,
    }
    entry["current_trigger_type"] = trigger_type
    entry["current_is_random_chat"] = bool(state.get("is_random_chat", False))

    try:
        await asyncio.wait_for(
            process_response_logic(bot, selected_event, state),
            timeout=_PROCESS_RESPONSE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            f"拟人插件：会话 {key} 单轮回复超时（>{_PROCESS_RESPONSE_TIMEOUT_SECONDS:.0f}s），已放弃旧批次。"
        )
    except asyncio.CancelledError:
        if int(entry.get("superseded_generation", 0) or 0) >= current_generation:
            logger.info(f"拟人插件：会话 {key} 当前批次已被新的直呼消息抢占。")
        raise
    except Exception as exc:
        if finished_exception_cls and isinstance(exc, finished_exception_cls):
            logger.debug("拟人插件：拼接消息处理提前结束（FinishedException）")
        else:
            if "concatenated_message" in state:
                retry_state = dict(state)
                retry_state["disable_network_hooks"] = True
                try:
                    await process_response_logic(bot, selected_event, retry_state)
                    logger.warning(
                        f"拟人插件：拼接消息处理失败（{type(exc).__name__}: {exc}），"
                        "已禁用联网/grounding 后重试同批成功"
                    )
                    return
                except Exception:
                    pass

                fallback_state = dict(state)
                fallback_state.pop("concatenated_message", None)
                fallback_state["disable_network_hooks"] = True
                fallback_state["batch_event_count"] = 1
                fallback_state["batched_events"] = [serialized_items[-1]] if serialized_items else []
                fallback_state["repeat_clusters"] = []
                try:
                    await process_response_logic(bot, selected_event, fallback_state)
                    logger.warning(
                        f"拟人插件：拼接消息处理失败（{type(exc).__name__}: {exc}），已回退单条处理成功"
                    )
                    return
                except Exception:
                    pass
            logger.exception("拟人插件：处理拼接消息失败")
    finally:
        entry["processing"] = False
        entry["active_task"] = None
        entry["processing_started_at"] = 0.0
        entry["current_trigger_type"] = ""
        entry["current_is_random_chat"] = False
        if entry.get("pending_items"):
            if entry.get("pending_ready"):
                _promote_pending_batch(entry)
                _schedule_timer(
                    entry=entry,
                    key=key,
                    bot=bot,
                    wait_seconds=0.0,
                    start_buffer_timer=lambda _key, _bot, _delay: asyncio.create_task(
                        run_buffer_timer(
                            _key,
                            _bot,
                            msg_buffer=msg_buffer,
                            process_response_logic=process_response_logic,
                            message_event_cls=message_event_cls,
                            message_cls=message_cls,
                            message_segment_cls=message_segment_cls,
                            logger=logger,
                            finished_exception_cls=finished_exception_cls,
                            delay=_delay,
                        )
                    ),
                )
            else:
                remaining = _schedule_delay(
                    float(entry.get("pending_started_at", 0.0) or time.monotonic()),
                    float(entry.get("delay", _GROUP_BATCH_DELAY_SECONDS) or _GROUP_BATCH_DELAY_SECONDS),
                )
                _promote_pending_batch(entry)
                _schedule_timer(
                    entry=entry,
                    key=key,
                    bot=bot,
                    wait_seconds=remaining,
                    start_buffer_timer=lambda _key, _bot, _delay: asyncio.create_task(
                        run_buffer_timer(
                            _key,
                            _bot,
                            msg_buffer=msg_buffer,
                            process_response_logic=process_response_logic,
                            message_event_cls=message_event_cls,
                            message_cls=message_cls,
                            message_segment_cls=message_segment_cls,
                            logger=logger,
                            finished_exception_cls=finished_exception_cls,
                            delay=_delay,
                        )
                    ),
                )
        elif not entry.get("items"):
            msg_buffer.pop(key, None)


async def handle_reply_event(
    bot: Any,
    event: Any,
    state: Dict[str, Any],
    *,
    poke_event_cls: Any,
    message_event_cls: Any,
    group_message_event_cls: Any,
    process_response_logic: Callable[[Any, Any, Dict[str, Any]], Any],
    msg_buffer: Dict[str, Dict[str, Any]],
    start_buffer_timer: Callable[[str, Any, float], Any],
    logger: Any,
) -> None:
    if isinstance(event, poke_event_cls):
        await process_response_logic(bot, event, state)
        return

    if not isinstance(event, message_event_cls):
        return

    session_key = _session_key(event, group_message_event_cls=group_message_event_cls)
    delay = _batch_delay(event, group_message_event_cls=group_message_event_cls)
    entry = msg_buffer.setdefault(session_key, _new_entry(delay))
    entry["delay"] = delay

    bot_self_id = str(getattr(bot, "self_id", "") or "")
    is_direct_mention = _is_direct_mention(event, bot_self_id)
    is_reply_to_bot = _is_reply_to_bot(event, bot_self_id)
    immediate_flush = bool(is_direct_mention or is_reply_to_bot)
    now_ts = time.monotonic()
    item = {
        "event": event,
        "state": dict(state),
        "is_direct_mention": is_direct_mention,
        "is_reply_to_bot": is_reply_to_bot,
        "received_at": now_ts,
    }

    if entry.get("processing"):
        pending_items = list(entry.get("pending_items") or [])
        pending_items.append(item)
        entry["pending_items"] = _trim_items(pending_items)
        entry["newer_batch_for_current"] = True
        if not float(entry.get("pending_started_at", 0.0) or 0.0):
            entry["pending_started_at"] = now_ts
        if immediate_flush:
            entry["pending_ready"] = True
            if _should_preempt_current_batch(entry, immediate_flush=True):
                entry["superseded_generation"] = max(
                    int(entry.get("superseded_generation", 0) or 0),
                    int(entry.get("current_generation", 0) or 0),
                )
                active_task = entry.get("active_task")
                if active_task and not active_task.done():
                    active_task.cancel()
                    logger.info(f"拟人插件：会话 {session_key} 收到新的直呼消息，抢占当前旧批次。")
        wait_seconds = _schedule_delay(
            float(entry.get("pending_started_at", now_ts) or now_ts),
            delay,
            immediate=bool(entry.get("pending_ready")),
        )
        _schedule_timer(
            entry=entry,
            key=session_key,
            bot=bot,
            wait_seconds=wait_seconds,
            start_buffer_timer=start_buffer_timer,
        )
        logger.debug(f"拟人插件：会话 {session_key} 正在处理中，新消息进入下一批。")
        return

    current_items = list(entry.get("items") or [])
    current_items.append(item)
    entry["items"] = _trim_items(current_items)
    if not float(entry.get("batch_started_at", 0.0) or 0.0):
        entry["batch_started_at"] = now_ts

    wait_seconds = _schedule_delay(
        float(entry.get("batch_started_at", now_ts) or now_ts),
        delay,
        immediate=immediate_flush,
    )
    _schedule_timer(
        entry=entry,
        key=session_key,
        bot=bot,
        wait_seconds=wait_seconds,
        start_buffer_timer=start_buffer_timer,
    )
    logger.debug(f"拟人插件：已缓冲会话 {session_key} 的消息，等待后续...")
