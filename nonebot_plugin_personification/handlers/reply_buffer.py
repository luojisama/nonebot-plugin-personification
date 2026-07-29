import asyncio
import re
import time
from contextlib import asynccontextmanager
from typing import Any, Callable, Dict

from ..core.target_inference import normalize_message_target_for_review
from ..core.turn_media import extract_turn_media_from_event, media_from_batched_events, serialize_turn_media
from .reply_commit import reply_lifecycle_snapshot


_GROUP_BATCH_DELAY_SECONDS = 1.2
_PRIVATE_BATCH_DELAY_SECONDS = 0.8
_MAX_BATCH_EVENTS = 8
_PROCESS_RESPONSE_TIMEOUT_SECONDS = 180.0
_DIRECT_REPLY_PREEMPT_SECONDS = 8.0
async def _handle_reply_timeout(
    *,
    bot: Any,
    event: Any,
    state: dict[str, Any],
    session_key: str,
    timeout_seconds: float,
    logger: Any,
    commit_lock: asyncio.Lock | None = None,
) -> None:
    del bot, event, logger, commit_lock
    delivery_started = bool(state.get("reply_delivery_started", False))
    delivery_confirmed = bool(state.get("reply_delivery_confirmed", False))
    delivery_complete = bool(state.get("reply_delivery_complete", False))
    lifecycle = reply_lifecycle_snapshot(state)
    if delivery_complete:
        delivery_state = "complete"
        outcome = "ok"
        diagnosis_code = "post_send_timeout"
    elif delivery_confirmed:
        delivery_state = "partial"
        outcome = "partial"
        diagnosis_code = "partial_reply_timeout"
    elif delivery_started:
        delivery_state = "dispatching"
        outcome = "outcome_unknown"
        diagnosis_code = "send_outcome_unknown"
    else:
        delivery_state = "not_started"
        outcome = "failed"
        diagnosis_code = "reply_timeout"
    try:
        from ..core import reply_turn_trace

        trace_id = str(state.get("reply_trace_id", "") or "")
        reply_turn_trace.record_stage(
            trace_id=trace_id,
            key="reply_timeout",
            label="回复超时",
            status="warn" if delivery_confirmed else "error",
            detail=(
                f"timeout_seconds={timeout_seconds:g} reply_required={str(bool(state.get('reply_required', False))).lower()} "
                f"delivery_started={str(delivery_started).lower()} "
                f"delivery_confirmed={str(delivery_confirmed).lower()} "
                f"delivery_complete={str(delivery_complete).lower()} "
                f"delivery_state={delivery_state} "
                f"last_phase={lifecycle['last_phase']} "
                f"phase_age_ms={lifecycle['phase_age_ms']} "
                f"elapsed_ms={lifecycle['elapsed_ms']}"
            ),
            hint="基础设施故障保持静默；检查 Provider、工具耗时、状态锁与发送回执",
        )
        reply_turn_trace.finish_trace(
            trace_id=trace_id,
            outcome=outcome,
            diagnosis_code=diagnosis_code,
            detail={
                "timeout_seconds": timeout_seconds,
                "session": session_key,
                "reply_required": bool(state.get("reply_required", False)),
                "delivery_started": delivery_started,
                "delivery_confirmed": delivery_confirmed,
                "delivery_complete": delivery_complete,
                "delivery_state": delivery_state,
                "last_phase": lifecycle["last_phase"],
                "phase_age_ms": lifecycle["phase_age_ms"],
                "elapsed_ms": lifecycle["elapsed_ms"],
                "silent": True,
            },
        )
    except Exception:
        pass


class ReplyConcurrencyController:
    def __init__(self, *, session_limit: int = 3, global_limit: int = 12) -> None:
        self._global_semaphore = asyncio.Semaphore(max(1, int(global_limit)))
        self._session_limit = max(1, int(session_limit))
        self._session_semaphores: dict[str, asyncio.Semaphore] = {}
        self._commit_locks: dict[str, asyncio.Lock] = {}
        self._direct_counts: dict[str, int] = {}
        self._direct_idle: dict[str, asyncio.Event] = {}

    def commit_lock(self, key: str) -> asyncio.Lock:
        return self._commit_locks.setdefault(key, asyncio.Lock())

    def _session_semaphore(self, key: str) -> asyncio.Semaphore:
        return self._session_semaphores.setdefault(
            key,
            asyncio.Semaphore(self._session_limit),
        )

    def _idle_event(self, key: str) -> asyncio.Event:
        event = self._direct_idle.get(key)
        if event is None:
            event = asyncio.Event()
            event.set()
            self._direct_idle[key] = event
        return event

    async def wait_for_direct_idle(self, key: str) -> None:
        await self._idle_event(key).wait()

    @asynccontextmanager
    async def direct_turn(self, key: str):
        self._direct_counts[key] = int(self._direct_counts.get(key, 0) or 0) + 1
        self._idle_event(key).clear()
        try:
            async with self._session_semaphore(key):
                async with self._global_semaphore:
                    yield self.commit_lock(key)
        finally:
            remaining = max(0, int(self._direct_counts.get(key, 1) or 1) - 1)
            if remaining:
                self._direct_counts[key] = remaining
            else:
                self._direct_counts.pop(key, None)
                self._idle_event(key).set()


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
                from ..core.qq_expression_library import semantic_text_for_qq_expression_segment

                parts.append(semantic_text_for_qq_expression_segment("mface", data, default_mface_kind="super"))
            elif seg_type == "face":
                from ..core.qq_expression_library import semantic_text_for_qq_expression_segment

                parts.append(semantic_text_for_qq_expression_segment("face", data))
    except Exception:
        return ""
    return "".join(parts).strip()


def _is_sticker_only_event(event: Any) -> bool:
    """判断一条消息是否「纯表情」（只有 face/mface/image 段，无实质文本）。

    按 segment type 判定，不靠占位符文本匹配——避免把表情误当成复读内容。
    """
    message = getattr(event, "message", None)
    if message is None:
        return False
    saw_sticker = False
    try:
        for seg in message:
            seg_type = getattr(seg, "type", None)
            data = getattr(seg, "data", {}) or {}
            if seg_type == "text":
                if str(data.get("text", "") or "").strip():
                    return False
            elif seg_type in {"face", "mface", "image"}:
                saw_sticker = True
            elif seg_type in {"at", "reply"}:
                continue
            else:
                # 其它段（语音、视频、json 卡片等）不算纯表情
                return False
    except Exception:
        return False
    return saw_sticker


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


def _is_reply_to_bot(event: Any, bot_self_id: str, *, message_target: str = "") -> bool:
    if not bot_self_id:
        return False
    normalized_target = normalize_message_target_for_review(message_target)
    if normalized_target in {"external_plugin", "others"}:
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
    if normalized_target == "bot":
        return True
    return bool(reply_user_id and reply_user_id == bot_self_id)


def _select_merged_event(events: list[Any]) -> Any:
    for event in reversed(events):
        if _has_reply_semantics(event):
            return event
    return events[-1]


def _serialize_batched_event(
    item: dict[str, Any],
    *,
    selected_event: Any = None,
) -> dict[str, Any]:
    event = item.get("event")
    current_origin = "current" if event is selected_event else "batch"
    return {
        "message_id": str(getattr(event, "message_id", "") or "").strip(),
        "user_id": str(getattr(event, "user_id", "") or "").strip(),
        "group_id": str(getattr(event, "group_id", "") or "").strip(),
        "sender_name": _extract_sender_name(event),
        "text": _extract_plain_text(event)[:200],
        "is_direct_mention": bool(item.get("is_direct_mention")),
        "is_reply_to_bot": bool(item.get("is_reply_to_bot")),
        "has_reply_semantics": _has_reply_semantics(event),
        "media": serialize_turn_media(
            extract_turn_media_from_event(
                event,
                current_origin=current_origin,
            )
        ),
    }


def _build_repeat_clusters(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    clusters: dict[str, dict[str, Any]] = {}
    for item in items:
        event = item.get("event")
        # 纯表情消息不参与复读簇统计：多人刷表情不应被当成「反复在说」而触发吐槽/跟读。
        if _is_sticker_only_event(event):
            continue
        text = _extract_plain_text(event)
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


def _session_key(
    event: Any,
    *,
    group_message_event_cls: Any,
    bot_self_id: str = "",
) -> str:
    user_id = str(getattr(event, "user_id", "") or "")
    if isinstance(event, group_message_event_cls):
        scope = str(getattr(event, "group_id", "") or "")
    else:
        scope = f"private_{user_id}"
    return f"{bot_self_id}:{scope}" if bot_self_id else scope


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
    response_timeout_seconds: float = _PROCESS_RESPONSE_TIMEOUT_SECONDS,
    concurrency_controller: ReplyConcurrencyController | None = None,
    user_policy_gate: Any = None,
) -> None:
    started_at = time.monotonic()
    await asyncio.sleep(max(0.0, float(delay or 0.0)))
    try:
        from ..core import reply_turn_trace

        if float(delay or 0.0) > 0.01:
            reply_turn_trace.record_stage(
                key="buffer_wait",
                label="缓冲等待",
                status="info",
                detail=f"delay_ms={int(float(delay or 0.0) * 1000)}",
            )
    except Exception:
        pass

    entry = msg_buffer.get(key)
    if not isinstance(entry, dict):
        return

    if concurrency_controller is not None:
        await concurrency_controller.wait_for_direct_idle(key)

    if entry.get("processing"):
        if entry.get("pending_items"):
            entry["pending_ready"] = True
        entry["timer_task"] = None
        return

    items = list(entry.get("items") or [])
    if user_policy_gate is not None and items:
        allowed = await asyncio.gather(
            *(
                user_policy_gate.allows_current(item.get("event"))
                for item in items
            )
        )
        items = [item for item, is_allowed in zip(items, allowed) if is_allowed]
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
                        response_timeout_seconds=response_timeout_seconds,
                        concurrency_controller=concurrency_controller,
                        user_policy_gate=user_policy_gate,
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

    serialized_items = [
        _serialize_batched_event(item, selected_event=selected_event)
        for item in items
    ]
    repeat_clusters = _build_repeat_clusters(items)
    if combined_message is not None:
        state["concatenated_message"] = combined_message
    state["merged_event_context"] = {
        "event_count": len(events),
        "selected_event_index": max(0, events.index(selected_event)),
    }
    state["batched_events"] = serialized_items
    state["turn_media_context"] = serialize_turn_media(media_from_batched_events(serialized_items))
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
    timeout_seconds = max(30.0, float(response_timeout_seconds or _PROCESS_RESPONSE_TIMEOUT_SECONDS))
    state["response_deadline"] = time.monotonic() + timeout_seconds
    try:
        from ..core import reply_turn_trace

        reply_turn_trace.record_stage(
            key="buffer_dequeue",
            label="缓冲出队",
            status="info",
            detail=(
                f"session={key} trigger={trigger_type} events={len(events)} "
                f"elapsed_ms={int((time.monotonic() - started_at) * 1000)} "
                f"timeout={timeout_seconds:.0f}s"
            ),
        )
    except Exception:
        pass

    try:
        await asyncio.wait_for(
            process_response_logic(bot, selected_event, state),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        logger.warning(
            f"拟人插件：会话 {key} 单轮回复超时（>{timeout_seconds:.0f}s），已放弃旧批次。"
        )
        await _handle_reply_timeout(
            bot=bot,
            event=selected_event,
            state=state,
            session_key=key,
            timeout_seconds=timeout_seconds,
            logger=logger,
        )
    except asyncio.CancelledError:
        if int(entry.get("superseded_generation", 0) or 0) >= current_generation:
            logger.info(f"拟人插件：会话 {key} 当前批次已被新的直呼消息抢占。")
        raise
    except Exception as exc:
        if finished_exception_cls and isinstance(exc, finished_exception_cls):
            logger.debug("拟人插件：拼接消息处理提前结束（FinishedException）")
        else:
            delivery_state = (
                "complete"
                if state.get("reply_delivery_complete")
                else "partial"
                if state.get("reply_delivery_confirmed")
                else "dispatching"
                if state.get("reply_delivery_started")
                else "not_started"
            )
            logger.error(
                f"拟人插件：处理拼接消息失败，保持静默: "
                f"type={type(exc).__name__} delivery_state={delivery_state}"
            )
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
                            response_timeout_seconds=response_timeout_seconds,
                            concurrency_controller=concurrency_controller,
                            user_policy_gate=user_policy_gate,
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
                            response_timeout_seconds=response_timeout_seconds,
                            concurrency_controller=concurrency_controller,
                            user_policy_gate=user_policy_gate,
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
    concurrency_controller: ReplyConcurrencyController | None = None,
    response_timeout_seconds: float = _PROCESS_RESPONSE_TIMEOUT_SECONDS,
    finished_exception_cls: Any = None,
    user_policy_gate: Any = None,
) -> None:
    if user_policy_gate is not None and not await user_policy_gate.allows_current(event):
        return
    if isinstance(event, poke_event_cls):
        if concurrency_controller is None:
            await process_response_logic(bot, event, state)
            return
        bot_self_id = str(getattr(bot, "self_id", "") or "")
        group_id = str(getattr(event, "group_id", "") or "")
        user_id = str(getattr(event, "user_id", "") or "")
        scope = group_id or f"private_{user_id}"
        session_key = f"{bot_self_id}:{scope}" if bot_self_id else scope
        direct_state = dict(state)
        direct_state["batch_session_key"] = session_key
        direct_state["reply_required"] = True
        timeout_seconds = max(30.0, float(response_timeout_seconds or _PROCESS_RESPONSE_TIMEOUT_SECONDS))
        direct_state["response_deadline"] = time.monotonic() + timeout_seconds
        async with concurrency_controller.direct_turn(session_key) as commit_lock:
            direct_state["reply_commit_lock"] = commit_lock
            try:
                await asyncio.wait_for(
                    process_response_logic(bot, event, direct_state),
                    timeout=timeout_seconds,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"拟人插件：会话 {session_key} poke turn 超时（>{timeout_seconds:.0f}s），已终止本轮。"
                )
                await _handle_reply_timeout(
                    bot=bot,
                    event=event,
                    state=direct_state,
                    session_key=session_key,
                    timeout_seconds=timeout_seconds,
                    logger=logger,
                    commit_lock=commit_lock,
                )
        return

    if not isinstance(event, message_event_cls):
        return

    bot_self_id = str(getattr(bot, "self_id", "") or "")
    session_key = _session_key(
        event,
        group_message_event_cls=group_message_event_cls,
        bot_self_id=bot_self_id,
    )
    delay = _batch_delay(event, group_message_event_cls=group_message_event_cls)
    is_private_session = not isinstance(event, group_message_event_cls)
    is_direct_mention = _is_direct_mention(event, bot_self_id)
    is_reply_to_bot = _is_reply_to_bot(
        event,
        bot_self_id,
        message_target=str(state.get("message_target", "") or ""),
    )
    targets_bot = normalize_message_target_for_review(state.get("message_target")) == "bot"
    reply_required = bool(
        not state.get("is_random_chat", False)
        and (is_private_session or is_direct_mention or is_reply_to_bot or targets_bot)
    )
    state["reply_required"] = reply_required
    state.setdefault("received_wall_at", time.time())
    immediate_flush = reply_required
    if immediate_flush and concurrency_controller is not None:
        entry = msg_buffer.get(session_key)
        if isinstance(entry, dict):
            if entry.get("processing"):
                entry["newer_batch_for_current"] = True
                entry["superseded_generation"] = max(
                    int(entry.get("superseded_generation", 0) or 0),
                    int(entry.get("current_generation", 0) or 0),
                )
                active_task = entry.get("active_task")
                if active_task and not active_task.done():
                    active_task.cancel()
        direct_state = dict(state)
        direct_state["batch_session_key"] = session_key
        direct_state["batch_event_count"] = 1
        direct_state["batched_events"] = []
        direct_state["turn_media_context"] = serialize_turn_media(
            extract_turn_media_from_event(event, current_origin="current")
        )
        timeout_seconds = max(30.0, float(response_timeout_seconds or _PROCESS_RESPONSE_TIMEOUT_SECONDS))
        direct_state["response_deadline"] = time.monotonic() + timeout_seconds
        async with concurrency_controller.direct_turn(session_key) as commit_lock:
            direct_state["reply_commit_lock"] = commit_lock
            try:
                await asyncio.wait_for(
                    process_response_logic(bot, event, direct_state),
                    timeout=timeout_seconds,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"拟人插件：会话 {session_key} direct turn 超时（>{timeout_seconds:.0f}s），已终止本轮。"
                )
                await _handle_reply_timeout(
                    bot=bot,
                    event=event,
                    state=direct_state,
                    session_key=session_key,
                    timeout_seconds=timeout_seconds,
                    logger=logger,
                    commit_lock=commit_lock,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if finished_exception_cls and isinstance(exc, finished_exception_cls):
                    logger.debug("拟人插件：direct turn 提前结束（FinishedException）")
                else:
                    delivery_state = (
                        "complete"
                        if direct_state.get("reply_delivery_complete")
                        else "partial"
                        if direct_state.get("reply_delivery_confirmed")
                        else "dispatching"
                        if direct_state.get("reply_delivery_started")
                        else "not_started"
                    )
                    logger.error(
                        f"拟人插件：会话 {session_key} direct turn 处理失败，保持静默: "
                        f"type={type(exc).__name__} delivery_state={delivery_state}"
                    )
        return
    entry = msg_buffer.setdefault(session_key, _new_entry(delay))
    entry["delay"] = delay
    now_ts = time.monotonic()
    item = {
        "event": event,
        "state": dict(state),
        "is_direct_mention": is_direct_mention,
        "is_reply_to_bot": is_reply_to_bot,
        "received_at": now_ts,
    }
    if concurrency_controller is not None:
        item["state"]["reply_commit_lock"] = concurrency_controller.commit_lock(session_key)

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
