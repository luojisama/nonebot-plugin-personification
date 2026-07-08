from __future__ import annotations

import asyncio
import random
import re
import time
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from ..deps import AdminIdentity, require_admin

_INTERACTION_WAIT_SECONDS = 45
_INTERACTION_TIMEOUT_GRACE_SECONDS = 5
_INTERACTION_POLL_SECONDS = 0.25
_STAGE_LOG_LEVEL = {
    "error": "ERROR",
    "warn": "WARNING",
    "warning": "WARNING",
    "ok": "INFO",
    "info": "INFO",
}


def _response_timeout_seconds(cfg: Any) -> float:
    try:
        value = float(getattr(cfg, "personification_response_timeout", 180) or 180)
    except Exception:
        value = 180.0
    return max(30.0, value)


def _interaction_wait_seconds(cfg: Any) -> float:
    # 体检必须覆盖生产回复超时；否则真实链路还在等模型时 WebUI 会先误报失败。
    return max(
        float(_INTERACTION_WAIT_SECONDS),
        _response_timeout_seconds(cfg) + float(_INTERACTION_TIMEOUT_GRACE_SECONDS),
    )


def _first_bot(runtime) -> Any | None:
    try:
        bots = runtime.get_bots() if callable(getattr(runtime, "get_bots", None)) else {}
    except Exception:
        return None
    return next(iter(bots.values()), None) if bots else None


def _bundle_attr(runtime, name: str) -> Any:
    bundle = getattr(runtime, "runtime_bundle", None)
    return getattr(bundle, name, None) if bundle is not None else None


def _extract_qzone_target_user_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    at_match = re.search(r"\[CQ:at,qq=(\d{5,20})[^\]]*\]", text)
    if at_match:
        return at_match.group(1)
    plain_match = re.search(r"\d{5,20}", text)
    return plain_match.group(0) if plain_match else ""


def _qzone_feed_summary(feed: dict[str, Any]) -> dict[str, Any]:
    return {
        "feed_id": str(feed.get("feed_id", "") or ""),
        "owner_uin": str(feed.get("owner_uin", "") or ""),
        "nickname": str(feed.get("nickname", "") or feed.get("owner_nickname", "") or ""),
        "content": str(feed.get("content", "") or "")[:500],
        "created_at": feed.get("created_at", 0),
        "images": list(feed.get("images", []) or [])[:6] if isinstance(feed.get("images"), list) else [],
        "unikey": str(feed.get("unikey", "") or ""),
        "curkey": str(feed.get("curkey", "") or ""),
    }


def _format_qzone_forward_health_record(feed: dict[str, Any], forward_text: str) -> str:
    summary = _qzone_feed_summary(feed)
    author = summary.get("nickname") or summary.get("owner_uin") or "未知用户"
    content = str(summary.get("content") or "").strip() or "（无文字内容）"
    suffix = f" | 附言：{str(forward_text or '').strip()}" if str(forward_text or "").strip() else ""
    return f"转发 {author} 的空间：{content[:120]}{suffix}"


def _record_qzone_forward_test_audit(
    *,
    admin: AdminIdentity,
    target_user_id: str,
    outcome: str,
    detail: dict[str, Any] | None = None,
) -> None:
    try:
        from ...core import webui_audit_log

        webui_audit_log.record(
            action="qzone_forward_test",
            qq=admin.qq,
            device_id=admin.device_id,
            target=str(target_user_id or ""),
            detail=detail or {},
            outcome=outcome,
        )
    except Exception:
        pass


# 这些 OneBot 动作都是"对外发消息"，交互测试需要全部捕获，
# 否则拟人化发送层若走 send_group_msg / 转发消息 / call_api 等非 bot.send 路径，
# 测试会误判为"未回复"，而真实聊天里 bot 其实已经发言。
_SEND_API_ACTIONS = frozenset(
    {
        "send_msg",
        "send_group_msg",
        "send_private_msg",
        "send_group_forward_msg",
        "send_private_forward_msg",
        "send_forward_msg",
    }
)


class _CapturingBot:
    """透传真实 bot（消息真的发到 QQ），同时捕获各种发送路径的内容用于回显。

    覆盖 send / send_*_msg / 转发 / call_api，确保无论回复链路用哪种发送方式
    （含拟人化发送层、多段、图片、表情、转发）都能被捕获并回显。
    """

    def __init__(self, real: Any, *, trace_id: str = "") -> None:
        self._real = real
        self.captured: list[str] = []
        self.self_id = getattr(real, "self_id", "")
        self.trace_id = trace_id

    def _record_attempt(self, detail: str) -> None:
        from ...core import reply_turn_trace

        reply_turn_trace.record_stage(
            trace_id=self.trace_id,
            key="send_attempt",
            label="发送回复",
            status="info",
            detail=str(detail)[:500],
        )

    def _record_outcome(self, *, ok: bool, detail: str) -> None:
        from ...core import reply_turn_trace

        if ok:
            reply_turn_trace.record_stage(
                trace_id=self.trace_id, key="send_success", label="发送成功",
                status="ok", detail=str(detail)[:500],
            )
        else:
            reply_turn_trace.record_stage(
                trace_id=self.trace_id, key="send_failed", label="发送失败",
                status="error", detail=str(detail)[:500],
                hint="检查协议端发送权限、好友/群关系、账号风控或消息格式",
            )

    def _capture(self, message: Any) -> None:
        try:
            text = str(message)
        except Exception:
            return
        if text:
            self.captured.append(text)

    async def send(self, event: Any, message: Any, **kwargs: Any) -> Any:
        self._record_attempt(message)
        try:
            result = await self._real.send(event, message, **kwargs)
        except Exception as exc:
            self._record_outcome(ok=False, detail=str(exc))
            raise
        self._capture(message)
        self._record_outcome(ok=True, detail=result)
        return result

    async def call_api(self, api: str, **data: Any) -> Any:
        action = str(api or "").strip()
        if action in _SEND_API_ACTIONS:
            self._record_attempt(data.get("message", data))
        try:
            result = await self._real.call_api(api, **data)
        except Exception as exc:
            if action in _SEND_API_ACTIONS:
                self._record_outcome(ok=False, detail=str(exc))
            raise
        if action in _SEND_API_ACTIONS:
            self._capture(data.get("message", ""))
            self._record_outcome(ok=True, detail=result)
        return result

    async def _send_via_action(self, action: str, message: Any, **data: Any) -> Any:
        self._record_attempt(message)
        try:
            result = await getattr(self._real, action)(message=message, **data)
        except Exception as exc:
            self._record_outcome(ok=False, detail=str(exc))
            raise
        self._capture(message)
        self._record_outcome(ok=True, detail=result)
        return result

    async def send_msg(self, *, message: Any = "", **data: Any) -> Any:
        return await self._send_via_action("send_msg", message, **data)

    async def send_group_msg(self, *, message: Any = "", **data: Any) -> Any:
        return await self._send_via_action("send_group_msg", message, **data)

    async def send_private_msg(self, *, message: Any = "", **data: Any) -> Any:
        return await self._send_via_action("send_private_msg", message, **data)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def _build_probe_event(bot: Any, *, group_id: str, user_id: str, text: str) -> Any:
    from nonebot.adapters.onebot.v11 import (
        GroupMessageEvent,
        Message,
        MessageSegment,
        PrivateMessageEvent,
    )
    from nonebot.adapters.onebot.v11.event import Sender

    self_id = int(getattr(bot, "self_id", 0) or 0)
    if group_id:
        # 群聊探测显式 @bot：与真实"直呼 bot"一致，确保被判为定向消息、
        # 立即处理（不进随机插话/批延迟），避免被语义帧判为"非对我说"而沉默。
        msg = MessageSegment.at(self_id) + MessageSegment.text(" " + text)
        raw_message = f"[CQ:at,qq={self_id}] {text}"
    else:
        msg = Message(text)
        raw_message = text
    common = dict(
        time=int(time.time()),
        self_id=self_id,
        post_type="message",
        message_id=random.randint(1, 2_000_000_000),
        user_id=int(user_id),
        message=msg,
        original_message=msg,
        raw_message=raw_message,
        font=0,
        sender=Sender(user_id=int(user_id), nickname="测试用户"),
        to_me=True,
    )
    if group_id:
        return GroupMessageEvent(
            message_type="group", sub_type="normal", group_id=int(group_id), anonymous=None, **common
        )
    return PrivateMessageEvent(message_type="private", sub_type="friend", **common)


def _stage(
    stages: list[dict[str, Any]],
    trace_id: str,
    key: str,
    label: str,
    status: str,
    detail: str = "",
    hint: str = "",
    *,
    started_at: float | None = None,
) -> None:
    from ...core import reply_turn_trace

    if started_at is not None:
        elapsed_ms = int(max(0.0, time.monotonic() - started_at) * 1000)
        detail = f"{detail}（耗时 {elapsed_ms}ms）" if detail else f"耗时 {elapsed_ms}ms"
    item = {"key": key, "label": label, "status": status, "detail": detail, "hint": hint}
    stages.append(item)
    reply_turn_trace.record_stage(
        trace_id=trace_id,
        key=key,
        label=label,
        status=status,
        detail=detail,
        hint=hint,
    )
    try:
        from ...core import plugin_runtime_logs

        plugin_runtime_logs.record(
            level=_STAGE_LOG_LEVEL.get(str(status or "info").lower(), "INFO"),
            source="webui.health",
            message=f"{label or key}: {detail}",
            context={"stage": key, "hint": hint} if hint else {"stage": key},
            trace_id=trace_id,
            min_level="DEBUG",
        )
    except Exception:
        pass


def _bundle_missing_fields(bundle: Any) -> list[str]:
    required = [
        "personification_rule",
        "reply_processor_deps",
        "msg_buffer",
        "poke_event_cls",
        "message_event_cls",
        "group_message_event_cls",
        "message_cls",
        "message_segment_cls",
    ]
    return [name for name in required if getattr(bundle, name, None) is None]


async def _dispatch_via_plugin_path(
    *,
    runtime: Any,
    bot: Any,
    proxy: _CapturingBot,
    event: Any,
    trace_id: str,
    stages: list[dict[str, Any]],
    target_label: str,
    target_detail: dict[str, str],
    started: float,
    interaction_wait_seconds: float,
    response_timeout_seconds: float,
) -> dict | None:
    """Run the plugin-owned rule/buffer/reply path directly for WebUI diagnostics.

    NoneBot's global ``handle_event`` is useful for integration testing, but it
    hides whether this plugin's rule matched and can drop trace context when the
    reply leaves through the buffer task. The health page needs plugin-local
    evidence, so this path explicitly runs the same rule and buffer helpers that
    the matcher uses in production.
    """

    bundle = getattr(runtime, "runtime_bundle", None)
    if bundle is None:
        _stage(
            stages,
            trace_id,
            "plugin_path_unavailable",
            "插件直连链路",
            "warn",
            "runtime_bundle 不可用，回退 nonebot.message.handle_event",
        )
        return None
    missing = _bundle_missing_fields(bundle)
    if missing:
        _stage(
            stages,
            trace_id,
            "plugin_path_unavailable",
            "插件直连链路",
            "warn",
            f"runtime_bundle 缺少字段：{', '.join(missing)}；回退 nonebot.message.handle_event",
        )
        return None

    try:
        from ...handlers.reply_buffer import handle_reply_event, run_buffer_timer
        from ...handlers.reply_pipeline.processor import process_response_logic as process_response_logic_core
    except Exception as exc:
        _stage(
            stages,
            trace_id,
            "plugin_path_unavailable",
            "插件直连链路",
            "warn",
            f"导入插件回复链路失败：{exc}；回退 nonebot.message.handle_event",
        )
        return None

    state: dict[str, Any] = {}
    try:
        matched = bool(await bundle.personification_rule(event, state))
    except Exception as exc:
        _stage(
            stages,
            trace_id,
            "rule_match",
            "规则匹配",
            "error",
            f"personification_rule 异常：{exc}",
            "查看插件日志中同 trace_id 的异常",
            started_at=started,
        )
        return _finish_payload(
            trace_id=trace_id,
            stages=stages,
            replied=False,
            target=target_label,
            detail=f"规则匹配阶段异常：{exc}",
            diagnosis_code="rule_exception",
            duration_ms=int((time.monotonic() - started) * 1000),
            target_detail=target_detail,
        )

    state_summary = {
        key: state.get(key)
        for key in ("is_random_chat", "message_target", "active_followup", "group_idle_active")
        if key in state
    }
    _stage(
        stages,
        trace_id,
        "rule_match",
        "规则匹配",
        "ok" if matched else "error",
        f"matched={matched}; state={state_summary or '{}'}",
        "" if matched else "规则未匹配：检查私聊命令过滤、群白名单、禁言缓存、黑名单或全局开关",
        started_at=started,
    )
    if not matched:
        return _finish_payload(
            trace_id=trace_id,
            stages=stages,
            replied=False,
            target=target_label,
            detail="personification_rule 未匹配，消息没有进入拟人回复链路。",
            diagnosis_code="rule_not_matched",
            duration_ms=int((time.monotonic() - started) * 1000),
            target_detail=target_detail,
        )

    scheduled_tasks: list[asyncio.Task[Any]] = []
    reply_logger = getattr(runtime, "logger", None) or getattr(
        getattr(bundle.reply_processor_deps, "runtime", None), "logger", None
    )

    async def _process_response_logic(_bot: Any, _event: Any, _state: dict[str, Any]) -> None:
        await process_response_logic_core(_bot, _event, _state, bundle.reply_processor_deps)

    def _start_buffer_timer(key: str, _bot: Any, wait_seconds: float) -> asyncio.Task[Any]:
        task = asyncio.create_task(
            run_buffer_timer(
                key,
                _bot,
                msg_buffer=bundle.msg_buffer,
                process_response_logic=_process_response_logic,
                message_event_cls=bundle.message_event_cls,
                message_cls=bundle.message_cls,
                message_segment_cls=bundle.message_segment_cls,
                logger=reply_logger,
                finished_exception_cls=getattr(bundle, "finished_exception_cls", None),
                delay=wait_seconds,
                response_timeout_seconds=response_timeout_seconds,
            )
        )
        scheduled_tasks.append(task)
        return task

    try:
        await handle_reply_event(
            proxy,
            event,
            state,
            poke_event_cls=bundle.poke_event_cls,
            message_event_cls=bundle.message_event_cls,
            group_message_event_cls=bundle.group_message_event_cls,
            process_response_logic=_process_response_logic,
            msg_buffer=bundle.msg_buffer,
            start_buffer_timer=_start_buffer_timer,
            logger=reply_logger,
        )
    except Exception as exc:
        _stage(
            stages,
            trace_id,
            "buffer_dispatch",
            "缓冲分发",
            "error",
            f"handle_reply_event 异常：{exc}",
            "查看插件日志中同 trace_id 的异常",
            started_at=started,
        )
        return _finish_payload(
            trace_id=trace_id,
            stages=stages,
            replied=False,
            target=target_label,
            detail=f"缓冲分发失败：{exc}",
            diagnosis_code="buffer_exception",
            duration_ms=int((time.monotonic() - started) * 1000),
            target_detail=target_detail,
        )

    _stage(
        stages,
        trace_id,
        "buffer_dispatch",
        "缓冲分发",
        "info",
        f"已进入插件 reply_buffer，scheduled_tasks={len(scheduled_tasks)}",
        started_at=started,
    )
    deadline = started + float(interaction_wait_seconds)
    while time.monotonic() < deadline:
        if proxy.captured:
            break
        if scheduled_tasks and all(task.done() for task in scheduled_tasks):
            break
        await asyncio.sleep(_INTERACTION_POLL_SECONDS)

    for task in list(scheduled_tasks):
        if task.done() and not task.cancelled():
            try:
                exc = task.exception()
            except asyncio.CancelledError:
                exc = None
            if exc is not None:
                _stage(
                    stages,
                    trace_id,
                    "buffer_task_failed",
                    "缓冲任务",
                    "error",
                    str(exc)[:500],
                    "查看插件日志中同 trace_id 的异常",
                    started_at=started,
                )

    ms = int((time.monotonic() - started) * 1000)
    if proxy.captured:
        _stage(stages, trace_id, "capture_reply", "捕获回复", "ok", f"捕获 {len(proxy.captured)} 条发送", started_at=started)
        return _finish_payload(
            trace_id=trace_id,
            stages=stages,
            replied=True,
            target=target_label,
            reply="\n".join(proxy.captured),
            detail=f"已在{'测试群 ' + target_detail.get('group_id', '') if target_detail.get('group_id') else '私聊 ' + target_detail.get('user_id', '')}收到 bot 回复",
            diagnosis_code="ok",
            duration_ms=ms,
            target_detail=target_detail,
        )

    from ...core import reply_turn_trace

    last_trace = reply_turn_trace.get_trace(trace_id) or {}
    diagnosis = str(last_trace.get("diagnosis_code") or "") or "capture_timeout"
    if diagnosis == "ok":
        diagnosis = "capture_timeout"
    active = [task for task in scheduled_tasks if not task.done()]
    if active:
        _stage(
            stages,
            trace_id,
            "reply_timeout",
            "回复超时",
            "warn",
            f"插件回复链路超过 {int(interaction_wait_seconds)}s 仍未发送，已取消本次测试任务",
            "通常是模型/provider 超时、工具调用耗时或 provider 重试过多；查看同 trace_id 阶段耗时",
            started_at=started,
        )
        for task in active:
            task.cancel()
    _stage(
        stages,
        trace_id,
        "capture_reply",
        "捕获回复",
        "error",
        "未捕获到发送",
        "查看上方阶段和插件日志；若已出现 no_reply 阶段则是模型/规则选择沉默，否则多为模型超时或发送失败",
        started_at=started,
    )
    return _finish_payload(
        trace_id=trace_id,
        stages=stages,
        replied=False,
        target=target_label,
        detail="未捕获到回复：已完成插件规则、缓冲、回复链路诊断，请查看 stages / last_trace / 插件日志。",
        diagnosis_code=diagnosis if not active else "reply_timeout",
        duration_ms=ms,
        target_detail=target_detail,
    )


def _finish_payload(
    *,
    trace_id: str,
    stages: list[dict[str, Any]],
    replied: bool,
    target: str,
    reply: str = "",
    detail: str,
    diagnosis_code: str,
    duration_ms: int = 0,
    target_detail: dict[str, str] | None = None,
) -> dict:
    from ...core import reply_turn_trace

    if replied:
        outcome = "ok"
    elif diagnosis_code in {"no_reply", "model_empty", "stale_reply", "capture_timeout"}:
        outcome = "no_reply"
    else:
        outcome = "failed"
    reply_turn_trace.finish_trace(
        trace_id=trace_id,
        outcome=outcome,
        diagnosis_code=diagnosis_code,
        detail={"detail": detail, "target": target, **(target_detail or {})},
    )
    last_trace = reply_turn_trace.get_trace(trace_id)
    trace_stages = []
    if isinstance(last_trace, dict):
        raw_stages = last_trace.get("stages")
        if isinstance(raw_stages, list):
            trace_stages = raw_stages
    return {
        "replied": replied,
        "duration_ms": duration_ms,
        "target": target,
        "reply": reply[:2000],
        "detail": detail,
        "trace_id": trace_id,
        "diagnosis_code": diagnosis_code,
        "stages": trace_stages or stages,
        "target_detail": target_detail or {},
        "last_trace": last_trace,
    }


def build_health_router(*, runtime) -> APIRouter:
    router = APIRouter(prefix="/api/health", tags=["health"])

    @router.get("/check")
    async def check(
        only: str = Query(default=""),
        refresh: bool = Query(default=False),
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        from ...core.diagnostics import get_cached_diagnostics, run_diagnostics

        # 默认返回缓存（秒开）；only=单项 或 refresh=true 时才真实重跑
        if not only and not refresh:
            cached = get_cached_diagnostics()
            if cached is not None:
                return {**cached, "cached": True}
        result = await run_diagnostics(
            plugin_config=getattr(runtime, "plugin_config", None),
            bundle=getattr(runtime, "runtime_bundle", None),
            superusers=getattr(runtime, "superusers", set()),
            get_bots=getattr(runtime, "get_bots", None),
            logger=getattr(runtime, "logger", None),
            only=only.strip(),
        )
        return {**result, "cached": False}

    @router.post("/interaction-test")
    async def interaction_test(
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        """向配置的测试群/私聊真实注入一条消息，走完整回复链路并回显 bot 实际回复。"""
        from ...core import reply_turn_trace

        cfg = getattr(runtime, "plugin_config", None)
        target = str(body.get("target", "") or "").strip()  # "group" | "private"
        group_id = str(getattr(cfg, "personification_webui_test_group_id", "") or "").strip()
        user_id = str(getattr(cfg, "personification_webui_test_user_id", "") or "").strip()
        text = str(body.get("text", "") or "").strip() or "你好呀，回我一句就好"
        target_label = "group" if target == "group" else "private"
        stages: list[dict[str, Any]] = []
        interaction_wait_seconds = _interaction_wait_seconds(cfg)
        response_timeout_seconds = _response_timeout_seconds(cfg)

        if target == "group":
            if not group_id:
                raise HTTPException(status_code=400, detail="未配置测试群（personification_webui_test_group_id）")
            probe_user = user_id or str(admin.qq)
            target_group, target_user = group_id, probe_user
        else:
            if not user_id:
                raise HTTPException(status_code=400, detail="未配置测试私聊用户（personification_webui_test_user_id）")
            target_group, target_user = "", user_id

        trace_id = reply_turn_trace.start_trace(
            session_type=target_label,
            group_id=target_group,
            user_id=target_user,
            detail={"source": "webui_interaction_test", "text": text[:200]},
        )
        target_detail = {"group_id": target_group, "user_id": target_user}
        _stage(stages, trace_id, "runtime_ready", "运行时", "ok" if cfg is not None else "error",
               "运行时配置已就绪" if cfg is not None else "运行时配置缺失")
        if cfg is None:
            return _finish_payload(
                trace_id=trace_id,
                stages=stages,
                replied=False,
                target=target_label,
                detail="运行时未就绪，无法构造交互测试。",
                diagnosis_code="runtime_unavailable",
                target_detail=target_detail,
            )

        bot = _first_bot(runtime)
        if bot is None:
            _stage(stages, trace_id, "bot_connected", "Bot 连接", "error", "Bot 未连接", "检查 OneBot 连接、反向 WebSocket、协议端状态")
            return _finish_payload(
                trace_id=trace_id,
                stages=stages,
                replied=False,
                target=target_label,
                detail="Bot 未连接。",
                diagnosis_code="bot_not_connected",
                target_detail=target_detail,
            )
        _stage(stages, trace_id, "bot_connected", "Bot 连接", "ok",
               f"self_id={getattr(bot, 'self_id', '') or 'unknown'}")

        if target_group:
            try:
                from ...utils import is_group_whitelisted

                enabled = is_group_whitelisted(str(target_group), list(getattr(cfg, "personification_whitelist", []) or []))
            except Exception as exc:
                enabled = False
                _stage(stages, trace_id, "group_whitelist", "群白名单", "warn", f"检查失败：{exc}",
                       "检查 data_store / 群配置是否可读")
            else:
                _stage(stages, trace_id, "group_whitelist", "群白名单", "ok" if enabled else "error",
                       "目标群已启用拟人回复" if enabled else "目标群未启用拟人回复",
                       "" if enabled else "在 WebUI「群开关」启用测试群，或配置 personification_whitelist")
                if not enabled:
                    return _finish_payload(
                        trace_id=trace_id,
                        stages=stages,
                        replied=False,
                        target=target_label,
                        detail="目标群未启用拟人回复，群聊测试不会进入回复链路。",
                        diagnosis_code="not_whitelisted",
                        target_detail=target_detail,
                    )
            try:
                from ...core.group_mute import refresh_bot_group_mute_state

                muted = await refresh_bot_group_mute_state(bot, str(target_group), logger=getattr(runtime, "logger", None))
            except Exception as exc:
                muted = False
                _stage(stages, trace_id, "group_mute", "Bot 禁言", "warn", f"禁言状态查询失败：{exc}",
                       "协议端不支持 get_group_member_info 时会回退缓存")
            else:
                _stage(stages, trace_id, "group_mute", "Bot 禁言", "error" if muted else "ok",
                       "Bot 当前在目标群被禁言" if muted else "未检测到 Bot 被禁言")
                if muted:
                    return _finish_payload(
                        trace_id=trace_id,
                        stages=stages,
                        replied=False,
                        target=target_label,
                        detail="Bot 在测试群被禁言，无法发送回复。",
                        diagnosis_code="bot_muted",
                        target_detail=target_detail,
                    )
        else:
            call_api = getattr(bot, "call_api", None)
            if callable(call_api):
                try:
                    friends = await call_api("get_friend_list")
                    friend_ids = {str(item.get("user_id", "")) for item in list(friends or []) if isinstance(item, dict)}
                    if friend_ids:
                        ok = str(target_user) in friend_ids
                        _stage(stages, trace_id, "friend_relation", "好友关系", "ok" if ok else "warn",
                               "目标用户在好友列表中" if ok else "目标用户不在 get_friend_list 返回中",
                               "" if ok else "若协议端好友列表不完整可忽略；否则检查是否已添加好友")
                    else:
                        _stage(stages, trace_id, "friend_relation", "好友关系", "info", "协议端返回空好友列表")
                except Exception as exc:
                    _stage(stages, trace_id, "friend_relation", "好友关系", "warn", f"好友列表查询失败：{exc}",
                           "协议端不支持 get_friend_list 时可忽略；若私聊失败则检查好友关系")

        try:
            from nonebot.message import handle_event

            event = _build_probe_event(bot, group_id=target_group, user_id=target_user, text=text)
        except Exception as exc:
            _stage(stages, trace_id, "event_build", "构造事件", "error", str(exc),
                   "检查测试群号/QQ 是否为纯数字，以及 OneBot v11 依赖是否正常")
            return _finish_payload(
                trace_id=trace_id,
                stages=stages,
                replied=False,
                target=target_label,
                detail=f"构造测试事件失败：{exc}",
                diagnosis_code="event_build_failed",
                target_detail=target_detail,
            )
        _stage(stages, trace_id, "event_build", "构造事件", "ok",
               f"message_type={getattr(event, 'message_type', '')}, to_me={getattr(event, 'to_me', None)}")

        proxy = _CapturingBot(bot, trace_id=trace_id)
        started = time.monotonic()
        token = reply_turn_trace.set_current_trace_id(trace_id)
        try:
            _stage(stages, trace_id, "dispatch_start", "分发事件", "info", "开始调用插件 personification_rule / reply_buffer")
            direct_result = await _dispatch_via_plugin_path(
                runtime=runtime,
                bot=bot,
                proxy=proxy,
                event=event,
                trace_id=trace_id,
                stages=stages,
                target_label=target_label,
                target_detail=target_detail,
                started=started,
                interaction_wait_seconds=interaction_wait_seconds,
                response_timeout_seconds=response_timeout_seconds,
            )
            if direct_result is not None:
                return direct_result

            _stage(
                stages,
                trace_id,
                "dispatch_fallback",
                "分发回退",
                "warn",
                "插件直连链路不可用，回退调用 nonebot.message.handle_event",
                started_at=started,
            )
            await asyncio.wait_for(handle_event(proxy, event), timeout=interaction_wait_seconds)
        except asyncio.TimeoutError:
            _stage(stages, trace_id, "dispatch_timeout", "分发事件", "warn",
                   f"handle_event 超过 {int(interaction_wait_seconds)}s 未返回",
                   "通常是模型调用或回复链路较慢，继续等待 send 捕获",
                   started_at=started)
        except Exception as exc:
            _stage(stages, trace_id, "dispatch_failed", "分发事件", "error", str(exc)[:500],
                   "查看插件日志中同 trace_id 的异常",
                   started_at=started)
            reply_turn_trace.reset_current_trace_id(token)
            return _finish_payload(
                trace_id=trace_id,
                stages=stages,
                replied=False,
                target=target_label,
                detail=f"分发事件失败：{exc}",
                diagnosis_code="internal_exception",
                duration_ms=int((time.monotonic() - started) * 1000),
                target_detail=target_detail,
            )
        finally:
            try:
                reply_turn_trace.reset_current_trace_id(token)
            except Exception:
                pass

        # 回复经缓冲/模型，可能在 handle_event 返回后才产生，轮询等待
        deadline = started + interaction_wait_seconds
        while not proxy.captured and time.monotonic() < deadline:
            await asyncio.sleep(_INTERACTION_POLL_SECONDS)
        ms = int((time.monotonic() - started) * 1000)
        replied = bool(proxy.captured)
        if replied:
            _stage(
                stages,
                trace_id,
                "capture_reply",
                "捕获回复",
                "ok",
                f"捕获 {len(proxy.captured)} 条 send",
                started_at=started,
            )
            return _finish_payload(
                trace_id=trace_id,
                stages=stages,
                replied=True,
                target=target_label,
                reply="\n".join(proxy.captured),
                detail=f"已在{'测试群 ' + target_group if target_group else '私聊 ' + target_user}收到 bot 回复",
                diagnosis_code="ok",
                duration_ms=ms,
                target_detail=target_detail,
            )
        last_trace = reply_turn_trace.get_trace(trace_id) or {}
        diagnosis = str(last_trace.get("diagnosis_code") or "") or "capture_timeout"
        if diagnosis == "ok":
            diagnosis = "capture_timeout"
        _stage(stages, trace_id, "capture_reply", "捕获回复", "error",
               "未捕获到 bot.send",
               "查看上方阶段和插件日志；常见原因是 NO_REPLY、规则未进入、模型超时或发送失败",
               started_at=started)
        return _finish_payload(
            trace_id=trace_id,
            stages=stages,
            replied=False,
            target=target_label,
            detail="未捕获到回复：已完成分层诊断，请查看 stages / last_trace / 插件日志。",
            diagnosis_code=diagnosis,
            duration_ms=ms,
            target_detail=target_detail,
        )

    @router.post("/qzone-forward-test")
    async def qzone_forward_test(
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        """真实转发指定用户第一条 QZone 动态，用于功能体检。

        这是管理员显式触发的外部写操作：会真的转发到 bot 的 QQ 空间，并消耗
        ``qzone_post_state`` 的月度额度。
        """
        from ...core.data_store import get_data_store
        from ...core.time_ctx import get_configured_now
        from ...jobs.periodic_jobs import build_qzone_quota, record_qzone_post

        cfg = getattr(runtime, "plugin_config", None)
        logger = getattr(runtime, "logger", None)
        target_user_id = _extract_qzone_target_user_id(
            body.get("target_user_id") or body.get("target_uin") or body.get("target") or ""
        )
        forward_text = str(body.get("forward_text", "") or "").strip()[:120]
        if not target_user_id:
            _record_qzone_forward_test_audit(
                admin=admin,
                target_user_id="",
                outcome="bad_request",
                detail={"error": "missing_target_user_id"},
            )
            raise HTTPException(status_code=400, detail="请填写要转发的目标 QQ 号")

        qzone_service = _bundle_attr(runtime, "qzone_social_service")
        forward_method = getattr(qzone_service, "forward_feed", None)
        fetch_method = getattr(qzone_service, "fetch_user_feeds", None)
        if not callable(fetch_method) or not callable(forward_method):
            _record_qzone_forward_test_audit(
                admin=admin,
                target_user_id=target_user_id,
                outcome="unavailable",
                detail={"error": "qzone_social_service_unavailable"},
            )
            raise HTTPException(status_code=503, detail="QZone 转发能力未就绪（运行时未初始化或 qzone 未启用）")

        bot = _first_bot(runtime)
        if bot is None:
            _record_qzone_forward_test_audit(
                admin=admin,
                target_user_id=target_user_id,
                outcome="unavailable",
                detail={"error": "bot_not_connected"},
            )
            raise HTTPException(status_code=503, detail="Bot 未连接")

        now = get_configured_now()
        state = get_data_store().load_sync("qzone_post_state")
        if not isinstance(state, dict):
            state = {}
        monthly_limit = int(getattr(cfg, "personification_qzone_monthly_limit", 30) or 30)
        min_interval_hours = float(getattr(cfg, "personification_qzone_min_interval_hours", 12.0) or 0)
        quota_before = build_qzone_quota(
            state=state,
            now=now,
            monthly_limit=monthly_limit,
            min_interval_hours=min_interval_hours,
        )
        if int(quota_before.get("limit", 0) or 0) > 0 and int(quota_before.get("remaining", 0) or 0) <= 0:
            _record_qzone_forward_test_audit(
                admin=admin,
                target_user_id=target_user_id,
                outcome="quota_blocked",
                detail={"quota": quota_before},
            )
            raise HTTPException(status_code=409, detail="本月 QQ 空间额度已用完，不能继续转发测试")

        update_cookie = _bundle_attr(runtime, "update_qzone_cookie")
        cookie_result: dict[str, Any] = {"attempted": False}
        if callable(update_cookie):
            cookie_result["attempted"] = True
            try:
                cookie_ok, cookie_msg = await update_cookie(bot)
            except Exception as exc:
                cookie_ok, cookie_msg = False, str(exc)
            cookie_result.update({"ok": bool(cookie_ok), "message": str(cookie_msg or "")[:300]})
            if not cookie_ok and logger is not None:
                logger.warning(f"[webui] QZone 转发体检刷新 Cookie 失败：{cookie_msg}")

        bot_id = str(getattr(bot, "self_id", "") or "")
        try:
            fetch_ok, fetch_msg, feeds = await fetch_method(
                target_uin=target_user_id,
                bot_id=bot_id,
                count=1,
                include_comments=False,
            )
        except Exception as exc:
            _record_qzone_forward_test_audit(
                admin=admin,
                target_user_id=target_user_id,
                outcome="fetch_error",
                detail={"error": str(exc)[:500], "cookie": cookie_result},
            )
            raise HTTPException(status_code=500, detail=f"读取目标空间失败：{exc}") from exc
        if not fetch_ok:
            _record_qzone_forward_test_audit(
                admin=admin,
                target_user_id=target_user_id,
                outcome="fetch_failed",
                detail={"message": str(fetch_msg or "")[:500], "cookie": cookie_result},
            )
            return {
                "ok": False,
                "stage": "fetch",
                "target_user_id": target_user_id,
                "error": str(fetch_msg or "读取目标空间失败"),
                "cookie": cookie_result,
                "quota": quota_before,
            }
        if not feeds:
            _record_qzone_forward_test_audit(
                admin=admin,
                target_user_id=target_user_id,
                outcome="no_feed",
                detail={"cookie": cookie_result},
            )
            return {
                "ok": False,
                "stage": "fetch",
                "target_user_id": target_user_id,
                "error": "目标用户空间没有可转发的动态",
                "cookie": cookie_result,
                "quota": quota_before,
            }

        feed = feeds[0]
        try:
            forward_ok, forward_msg = await forward_method(
                feed=feed,
                bot_id=bot_id,
                content=forward_text,
            )
        except Exception as exc:
            _record_qzone_forward_test_audit(
                admin=admin,
                target_user_id=target_user_id,
                outcome="forward_error",
                detail={"error": str(exc)[:500], "feed": _qzone_feed_summary(feed), "cookie": cookie_result},
            )
            raise HTTPException(status_code=500, detail=f"转发失败：{exc}") from exc
        if not forward_ok:
            _record_qzone_forward_test_audit(
                admin=admin,
                target_user_id=target_user_id,
                outcome="forward_failed",
                detail={"message": str(forward_msg or "")[:500], "feed": _qzone_feed_summary(feed), "cookie": cookie_result},
            )
            return {
                "ok": False,
                "stage": "forward",
                "target_user_id": target_user_id,
                "error": str(forward_msg or "转发失败"),
                "feed": _qzone_feed_summary(feed),
                "cookie": cookie_result,
                "quota": quota_before,
            }

        post_state = record_qzone_post(
            _format_qzone_forward_health_record(feed, forward_text),
            now=get_configured_now(),
            kind="forward",
        )
        quota_after = build_qzone_quota(
            state=post_state,
            now=get_configured_now(),
            monthly_limit=monthly_limit,
            min_interval_hours=min_interval_hours,
        )
        if logger is not None:
            logger.info(f"[webui] 管理员 {admin.qq} 转发体检：target={target_user_id}")
        _record_qzone_forward_test_audit(
            admin=admin,
            target_user_id=target_user_id,
            outcome="ok",
            detail={"feed": _qzone_feed_summary(feed), "forward_text": forward_text, "quota": quota_after},
        )
        return {
            "ok": True,
            "target_user_id": target_user_id,
            "forward_text": forward_text,
            "message": str(forward_msg or "ok"),
            "feed": _qzone_feed_summary(feed),
            "cookie": cookie_result,
            "quota": quota_after,
        }

    return router
