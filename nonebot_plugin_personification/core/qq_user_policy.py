from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Any

from .policy_classifier import PolicyEventInput
from .user_policy import PolicyAssessment, PolicyAuthorization, UserPolicyService


QQ_POLICY_ALLOW = "allow"
QQ_POLICY_SILENT = "silent"
QQ_POLICY_DIRECT_CLOSURE = "direct_closure"
QQ_POLICY_DIRECT_CLOSURE_CANDIDATE = "direct_closure_candidate"
_CLASSIFIER_UNAVAILABLE_CLOSURE_COOLDOWN_SECONDS = 60.0


class QQPolicyBlockedDuringTurn(RuntimeError):
    pass


@dataclass(frozen=True)
class QQPolicyDecision:
    user_id: str
    event_key: str
    surface: str
    channel_key: str
    direct: bool
    disposition: str
    assessment: PolicyAssessment
    authorization: PolicyAuthorization

    @property
    def allow_normal_processing(self) -> bool:
        return self.disposition == QQ_POLICY_ALLOW and not self.authorization.blocked

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "event_key": self.event_key,
            "surface": self.surface,
            "channel_key": self.channel_key,
            "direct": self.direct,
            "disposition": self.disposition,
            "verdict": self.assessment.verdict,
            "category": self.assessment.category,
            "intent": self.assessment.intent,
            "severity": self.assessment.severity,
            "confidence": self.assessment.confidence,
            "reason_code": self.assessment.reason_code,
            "blocked": self.authorization.blocked,
            "tier": self.authorization.tier,
        }


def _event_user_id(event: Any) -> str:
    return str(getattr(event, "user_id", "") or "").strip()


def _event_surface(event: Any) -> tuple[str, str]:
    group_id = str(getattr(event, "group_id", "") or "").strip()
    if group_id:
        return "qq_group", f"qq_group:{group_id}"
    user_id = _event_user_id(event)
    return "qq_private", f"qq_private:{user_id}"


def _event_is_direct(event: Any, bot_self_id: str) -> bool:
    if not str(getattr(event, "group_id", "") or "").strip():
        return True
    if bool(getattr(event, "to_me", False)):
        return True
    self_id = str(bot_self_id or getattr(event, "self_id", "") or "").strip()
    if not self_id:
        return False
    try:
        for segment in getattr(event, "message", []) or []:
            if str(getattr(segment, "type", "") or "").strip().lower() != "at":
                continue
            target = str((getattr(segment, "data", {}) or {}).get("qq", "") or "").strip()
            if target == self_id:
                return True
    except Exception:
        pass
    reply = getattr(event, "reply", None)
    sender = getattr(reply, "sender", None) if reply is not None else None
    if isinstance(reply, dict):
        sender = reply.get("sender")
    if isinstance(sender, dict):
        reply_user_id = str(sender.get("user_id", "") or "").strip()
    else:
        reply_user_id = str(getattr(sender, "user_id", "") or "").strip()
    return bool(reply_user_id and reply_user_id == self_id)


def _event_text(event: Any) -> str:
    try:
        return str(event.get_plaintext() or "").strip()[:1200]
    except Exception:
        return ""


def _event_media_summary(event: Any) -> str:
    kinds: list[str] = []
    try:
        for segment in getattr(event, "message", []) or []:
            kind = str(getattr(segment, "type", "") or "").strip().lower()
            if kind in {"image", "face", "mface", "video", "record", "json", "xml"}:
                kinds.append(kind)
    except Exception:
        return ""
    return ",".join(kinds[:12])


def _blocked_authorization(tier: str = "policy_unavailable") -> PolicyAuthorization:
    return PolicyAuthorization(
        blocked=True,
        tier=tier,
        allow_reply=False,
        allow_visible_reaction=False,
        allow_agent_action=False,
        allow_proactive=False,
        allow_qzone=False,
        allow_profile_write=False,
        allow_history_write=False,
        allow_memory_write=False,
        allow_relation_write=False,
        allow_context_read=False,
    )


class QQUserPolicyGate:
    def __init__(
        self,
        service: UserPolicyService,
        *,
        logger: Any = None,
        legacy_block_checker: Any = None,
        cache_ttl_seconds: float = 60.0,
        cache_max_size: int = 512,
    ) -> None:
        self.service = service
        self.logger = logger
        self.legacy_block_checker = legacy_block_checker
        self.cache_ttl_seconds = max(1.0, float(cache_ttl_seconds))
        self.cache_max_size = max(16, int(cache_max_size))
        self._cache: OrderedDict[str, tuple[float, QQPolicyDecision]] = OrderedDict()
        self._inflight: dict[str, asyncio.Task[QQPolicyDecision]] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _event_key(event: Any, *, bot_self_id: str, surface: str) -> str:
        message_id = str(getattr(event, "message_id", "") or "").strip()
        user_id = _event_user_id(event) or "unknown"
        identity = message_id or f"object-{id(event)}"
        return f"qq:{bot_self_id or 'unknown'}:{surface}:{user_id}:{identity}"

    async def current_authorization(self, user_id: str) -> PolicyAuthorization:
        try:
            if self.legacy_block_checker is not None:
                legacy_blocked = await asyncio.to_thread(
                    self.legacy_block_checker,
                    str(user_id or "").strip(),
                )
                if legacy_blocked:
                    return _blocked_authorization("legacy_permanent")
            return await asyncio.to_thread(self.service.authorize, str(user_id or "").strip())
        except Exception as exc:
            if self.logger is not None:
                self.logger.warning(
                    f"[user_policy] authorization failed closed: {type(exc).__name__}"
                )
            return _blocked_authorization()

    @staticmethod
    def _is_non_human_context_message(
        message: dict[str, Any],
        *,
        bot_self_id: str = "",
    ) -> bool:
        source_kind = str(message.get("source_kind", "") or "").strip().lower()
        if source_kind in {"bot", "bot_reply", "plugin", "plugin_command", "system"}:
            return True
        if bool(message.get("is_bot")):
            return True
        role = str(message.get("role", "") or "").strip().lower()
        if role in {"assistant", "system", "tool"}:
            return True
        user_id = str(message.get("user_id", "") or "").strip()
        return bool(bot_self_id and user_id == str(bot_self_id).strip())

    async def filter_context_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        bot_self_id: str = "",
    ) -> tuple[list[dict[str, Any]], set[str]]:
        """Filter human context against current global policy, failing closed.

        Bot/plugin/system rows remain available for provenance. Human rows without a
        stable user id are excluded because they cannot be authorized. References to
        excluded rows/users are scrubbed from the retained shallow copies as a second
        line of defence against indirect prompt injection.
        """

        source = [item for item in list(messages or []) if isinstance(item, dict)]
        human_user_ids = sorted(
            {
                str(item.get("user_id", "") or "").strip()
                for item in source
                if not self._is_non_human_context_message(item, bot_self_id=bot_self_id)
                and str(item.get("user_id", "") or "").strip()
            }
        )
        authorizations = await asyncio.gather(
            *(self.current_authorization(user_id) for user_id in human_user_ids)
        )
        excluded_user_ids = {
            user_id
            for user_id, authorization in zip(human_user_ids, authorizations)
            if bool(getattr(authorization, "blocked", True))
            or not bool(getattr(authorization, "allow_context_read", False))
        }
        excluded_message_ids: set[str] = set()
        retained: list[dict[str, Any]] = []
        for item in source:
            non_human = self._is_non_human_context_message(
                item,
                bot_self_id=bot_self_id,
            )
            user_id = str(item.get("user_id", "") or "").strip()
            if not non_human and (not user_id or user_id in excluded_user_ids):
                message_id = str(item.get("message_id", "") or "").strip()
                if message_id:
                    excluded_message_ids.add(message_id)
                continue
            retained.append(dict(item))

        for item in retained:
            if str(item.get("reply_to_user_id", "") or "").strip() in excluded_user_ids:
                item["reply_to_user_id"] = ""
            if str(item.get("reply_to_msg_id", "") or "").strip() in excluded_message_ids:
                item["reply_to_msg_id"] = ""
            mentioned = item.get("mentioned_ids", [])
            if isinstance(mentioned, list):
                item["mentioned_ids"] = [
                    value
                    for value in mentioned
                    if str(value or "").strip() not in excluded_user_ids
                ]
        return retained, excluded_user_ids

    async def allows_current(self, event: Any) -> bool:
        user_id = _event_user_id(event)
        if not user_id:
            return False
        authorization = await self.current_authorization(user_id)
        return not authorization.blocked

    async def ensure_current(self, event: Any) -> None:
        if not await self.allows_current(event):
            raise QQPolicyBlockedDuringTurn("QQ user policy blocked during turn")

    async def claim_direct_closure(self, decision: QQPolicyDecision) -> QQPolicyDecision:
        if decision.disposition != QQ_POLICY_DIRECT_CLOSURE_CANDIDATE:
            return decision
        classifier_unavailable = decision.assessment.reason_code == "classifier_unavailable"
        channel_key = decision.channel_key
        cooldown_seconds = None
        if classifier_unavailable:
            # 分类服务暂时不可用不是用户违规；使用独立、短时的提示冷却，既避免
            # 故障期间刷屏，也不让真实边界闭环的 30 天记录吞掉服务恢复提示。
            channel_key = f"{channel_key}:classifier_unavailable"
            cooldown_seconds = _CLASSIFIER_UNAVAILABLE_CLOSURE_COOLDOWN_SECONDS
        try:
            kwargs = {
                "user_id": decision.user_id,
                "channel_key": channel_key,
                "event_key": decision.event_key,
            }
            if cooldown_seconds is not None:
                kwargs["cooldown_seconds"] = cooldown_seconds
            claimed = await asyncio.to_thread(self.service.claim_direct_closure, **kwargs)
        except Exception as exc:
            if self.logger is not None:
                self.logger.warning(
                    f"[user_policy] direct closure claim failed closed: {type(exc).__name__}"
                )
            claimed = False
        return replace(
            decision,
            disposition=QQ_POLICY_DIRECT_CLOSURE if claimed else QQ_POLICY_SILENT,
        )

    async def evaluate(self, event: Any, *, bot_self_id: str = "") -> QQPolicyDecision:
        user_id = _event_user_id(event)
        surface, channel_key = _event_surface(event)
        resolved_bot_id = str(bot_self_id or getattr(event, "self_id", "") or "").strip()
        key = self._event_key(event, bot_self_id=resolved_bot_id, surface=surface)
        now = time.monotonic()
        async with self._lock:
            cached = self._cache.get(key)
            if cached is not None and now - cached[0] <= self.cache_ttl_seconds:
                self._cache.move_to_end(key)
                return cached[1]
            if cached is not None:
                self._cache.pop(key, None)
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(
                    self._evaluate_uncached(
                        event,
                        user_id=user_id,
                        bot_self_id=resolved_bot_id,
                        event_key=key,
                        surface=surface,
                        channel_key=channel_key,
                    )
                )
                self._inflight[key] = task
                task.add_done_callback(
                    lambda completed, cache_key=key: self._schedule_task_finalization(
                        cache_key, completed
                    )
                )
        decision = await asyncio.shield(task)
        return decision

    def _schedule_task_finalization(
        self,
        key: str,
        task: asyncio.Task[QQPolicyDecision],
    ) -> None:
        try:
            asyncio.get_running_loop().create_task(self._finalize_task(key, task))
        except RuntimeError:
            return

    async def _finalize_task(
        self,
        key: str,
        task: asyncio.Task[QQPolicyDecision],
    ) -> None:
        async with self._lock:
            if self._inflight.get(key) is task:
                self._inflight.pop(key, None)
            if task.cancelled():
                return
            try:
                decision = task.result()
            except Exception:
                return
            self._cache[key] = (time.monotonic(), decision)
            self._cache.move_to_end(key)
            while len(self._cache) > self.cache_max_size:
                self._cache.popitem(last=False)

    async def _evaluate_uncached(
        self,
        event: Any,
        *,
        user_id: str,
        bot_self_id: str,
        event_key: str,
        surface: str,
        channel_key: str,
    ) -> QQPolicyDecision:
        direct = _event_is_direct(event, bot_self_id)
        authorization = await self.current_authorization(user_id)
        if authorization.blocked or not user_id:
            return QQPolicyDecision(
                user_id=user_id,
                event_key=event_key,
                surface=surface,
                channel_key=channel_key,
                direct=direct,
                disposition=QQ_POLICY_SILENT,
                assessment=PolicyAssessment(reason_code="active_blacklist", confirmed=False),
                authorization=authorization,
            )

        if bot_self_id and user_id == bot_self_id:
            return QQPolicyDecision(
                user_id=user_id,
                event_key=event_key,
                surface=surface,
                channel_key=channel_key,
                direct=direct,
                disposition=QQ_POLICY_ALLOW,
                assessment=PolicyAssessment(
                    verdict="allow",
                    category="none",
                    intent="ordinary",
                    severity="none",
                    confidence=1.0,
                    reason_code="bot_self_message",
                    confirmed=True,
                ),
                authorization=authorization,
            )

        text = _event_text(event)
        if not text:
            return QQPolicyDecision(
                user_id=user_id,
                event_key=event_key,
                surface=surface,
                channel_key=channel_key,
                direct=direct,
                disposition=QQ_POLICY_ALLOW,
                assessment=PolicyAssessment(
                    verdict="allow",
                    category="none",
                    intent="ordinary",
                    severity="none",
                    confidence=1.0,
                    reason_code="non_text_event",
                    confirmed=True,
                ),
                authorization=authorization,
            )

        classifier = self.service.classifier
        if classifier is None:
            assessment = PolicyAssessment(reason_code="classifier_unavailable", confirmed=False)
        else:
            try:
                assessment = await classifier.classify(
                    PolicyEventInput(
                        user_id=user_id,
                        event_id=event_key,
                        surface=surface,
                        text=text,
                        is_direct=direct,
                        media_summary=_event_media_summary(event),
                    )
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.logger is not None:
                    self.logger.warning(
                        f"[user_policy] QQ classifier failed closed: {type(exc).__name__}"
                    )
                assessment = PolicyAssessment(reason_code="classifier_unavailable", confirmed=False)

        # 分类器不可用只表示策略基础设施暂时失效，不代表用户内容越过边界。
        # 如果继续按默认的 boundary_topic 评估，会把普通消息送进 direct_closure
        # 或 silent 分支，导致私聊消息无回复且没有进入正常 Trace/Agent 链路。
        # 保留 assessment.reason_code 供上层 Trace/诊断使用，但不要把这次故障
        # 写成用户策略事件，也不要污染自动违规计数。
        if assessment.reason_code == "classifier_unavailable":
            return QQPolicyDecision(
                user_id=user_id,
                event_key=event_key,
                surface=surface,
                channel_key=channel_key,
                direct=direct,
                disposition=QQ_POLICY_ALLOW,
                assessment=assessment,
                authorization=authorization,
            )

        try:
            result = await asyncio.to_thread(
                self.service.apply_assessment,
                user_id=user_id,
                idempotency_key=event_key,
                surface=surface,
                assessment=assessment,
                content=text,
                metadata={
                    "bot_id": bot_self_id,
                    "group_id": str(getattr(event, "group_id", "") or ""),
                    "message_id": str(getattr(event, "message_id", "") or ""),
                    "is_direct": direct,
                    "protocol": "onebot_v11",
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.logger is not None:
                self.logger.warning(
                    f"[user_policy] QQ policy persistence failed closed: {type(exc).__name__}"
                )
            return QQPolicyDecision(
                user_id=user_id,
                event_key=event_key,
                surface=surface,
                channel_key=channel_key,
                direct=direct,
                disposition=QQ_POLICY_SILENT,
                assessment=PolicyAssessment(
                    reason_code="policy_store_unavailable",
                    confirmed=False,
                ),
                authorization=_blocked_authorization(),
            )
        assessment = result.assessment
        authorization = await self.current_authorization(user_id)
        if authorization.blocked:
            disposition = QQ_POLICY_SILENT
        elif assessment.should_quarantine:
            disposition = QQ_POLICY_DIRECT_CLOSURE_CANDIDATE if direct else QQ_POLICY_SILENT
        else:
            disposition = QQ_POLICY_ALLOW
        return QQPolicyDecision(
            user_id=user_id,
            event_key=event_key,
            surface=surface,
            channel_key=channel_key,
            direct=direct,
            disposition=disposition,
            assessment=assessment,
            authorization=authorization,
        )


__all__ = [
    "QQ_POLICY_ALLOW",
    "QQ_POLICY_DIRECT_CLOSURE",
    "QQ_POLICY_DIRECT_CLOSURE_CANDIDATE",
    "QQ_POLICY_SILENT",
    "QQPolicyBlockedDuringTurn",
    "QQPolicyDecision",
    "QQUserPolicyGate",
]
