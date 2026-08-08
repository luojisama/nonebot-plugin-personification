from __future__ import annotations

import json
from inspect import isawaitable
from typing import Any

from ..agent.tool_registry import AgentTool
from .protocol_adapter import ProtocolResult, get_protocol_adapter


_SECTIONS = frozenset({"profile", "members", "announcements"})


def _safe_limit(value: Any, *, default: int, maximum: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(1, min(maximum, parsed))


def _failure_payload(result: ProtocolResult, *, section: str, group_id: str) -> str:
    return json.dumps(
        {
            "ok": False,
            "section": section,
            "group_id": group_id,
            "status": result.status,
            "error": result.code,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def build_current_group_context_tool(
    *,
    bot: Any,
    group_id: str,
    plugin_config: Any = None,
    logger: Any = None,
    policy_authorizer: Any = None,
) -> AgentTool:
    gid = str(group_id or "").strip()
    adapter = get_protocol_adapter(bot, plugin_config=plugin_config, logger=logger)

    async def _handler(section: str = "profile", limit: int = 30) -> str:
        normalized_section = str(section or "").strip().lower()
        if normalized_section not in _SECTIONS:
            return json.dumps(
                {
                    "ok": False,
                    "section": normalized_section,
                    "group_id": gid,
                    "status": "definite_failure",
                    "error": "invalid_section",
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        profile_result = await adapter.get_group_info(group_id=gid)
        if not profile_result.ok:
            return _failure_payload(profile_result, section=normalized_section, group_id=gid)

        if normalized_section == "profile":
            profile = dict(profile_result.data or {})
            profile.pop("avatar_url", None)
            data: Any = profile
        elif normalized_section == "members":
            member_result = await adapter.get_group_member_list(group_id=gid)
            if not member_result.ok:
                return _failure_payload(member_result, section=normalized_section, group_id=gid)
            member_limit = _safe_limit(limit, default=30, maximum=50)
            candidates = sorted(
                list(member_result.data or []),
                key=lambda item: (
                    {"owner": 0, "admin": 1, "member": 2}.get(str(item.get("role", "")), 3),
                    -int(item.get("last_sent_time", 0) or 0),
                    str(item.get("user_id", "")),
                ),
            )
            members: list[dict[str, Any]] = []
            for member in candidates:
                if len(members) >= member_limit:
                    break
                if policy_authorizer is not None:
                    try:
                        authorization = policy_authorizer(str(member.get("user_id", "") or ""))
                        if isawaitable(authorization):
                            authorization = await authorization
                        if bool(getattr(authorization, "blocked", True)) or not bool(
                            getattr(authorization, "allow_context_read", False)
                        ):
                            continue
                    except Exception as exc:
                        if logger is not None:
                            logger.warning(
                                "[current_group_context] policy authorization failed closed "
                                f"type={type(exc).__name__}"
                            )
                        continue
                members.append(member)
            data = [
                {
                    key: member.get(key)
                    for key in ("user_id", "nickname", "card", "role", "title", "last_sent_time")
                }
                for member in members
            ]
        else:
            notice_result = await adapter.get_group_notices(group_id=gid)
            if not notice_result.ok:
                return _failure_payload(notice_result, section=normalized_section, group_id=gid)
            notice_limit = _safe_limit(limit, default=10, maximum=20)
            data = [
                {
                    "sender_id": notice.get("sender_id", ""),
                    "publish_time": notice.get("publish_time", 0),
                    "text": str((notice.get("message") or {}).get("text", "") or ""),
                    "image_count": len((notice.get("message") or {}).get("images", []) or []),
                    "pinned": bool((notice.get("settings") or {}).get("pinned", False)),
                    "confirm_required": bool(
                        (notice.get("settings") or {}).get("confirm_required", False)
                    ),
                }
                for notice in list(notice_result.data or [])[:notice_limit]
            ]

        return json.dumps(
            {
                "ok": True,
                "section": normalized_section,
                "group_id": gid,
                "data": data,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    return AgentTool(
        name="get_current_group_context",
        description=(
            "读取当前聊天群的实时群资料、成员目录或群公告。"
            "群号由当前事件固定，不能查询其它群；只在当前对话确实需要这些群内事实时调用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "section": {
                    "type": "string",
                    "enum": sorted(_SECTIONS),
                    "description": "要读取的当前群信息区段。",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "description": "成员或公告返回上限。",
                },
            },
            "required": ["section"],
            "additionalProperties": False,
        },
        handler=_handler,
        local=True,
        metadata={
            "source_kind": "first_party_runtime",
            "intent_tags": ["plugin_question", "group_context", "lookup"],
            "evidence_kind": "profile",
            "risk_level": "low",
            "side_effect": "none",
            "final_behavior": "continue",
            "retryable": True,
            "latency_class": "normal",
        },
    )


def register_current_group_context_tool(
    registry: Any,
    *,
    bot: Any,
    event: Any,
    plugin_config: Any = None,
    logger: Any = None,
    policy_authorizer: Any = None,
) -> None:
    raw_group_id = str(getattr(event, "group_id", "") or "").strip()
    if (
        registry is None
        or not raw_group_id.isascii()
        or not raw_group_id.isdecimal()
        or int(raw_group_id) <= 0
    ):
        return
    registry.register(
        build_current_group_context_tool(
            bot=bot,
            group_id=raw_group_id,
            plugin_config=plugin_config,
            logger=logger,
            policy_authorizer=policy_authorizer,
        )
    )


__all__ = [
    "build_current_group_context_tool",
    "register_current_group_context_tool",
]
