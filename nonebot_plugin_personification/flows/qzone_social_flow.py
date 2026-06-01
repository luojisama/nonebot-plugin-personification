from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

from ..agent.inner_state import DEFAULT_STATE, load_inner_state
from ..core.context_policy import strip_response_control_markers
from ..core.data_store import get_data_store
from ..core.emotion_state import describe_user_emotion_memory, load_emotion_state
from ..core.image_input import summarize_images_with_vision
from ..core.qzone_service import _extract_qzone_comments
from ..core.time_ctx import inject_current_time_context
from .diary_flow import clean_generated_text


_STORE_NAME = "qzone_social_state"
_SCAN_LOCK = asyncio.Lock()

# 权限保密用户黑名单：对方设置了"主人设置了保密"等访问限制时，记录 uin 跳过后续扫描。
# 每 7 天自动重检一次，若仍无权限则继续保持在黑名单。
_PERMISSION_BLOCK_RECHECK_SECONDS = 7 * 24 * 3600
_PERMISSION_DENIED_HINTS = (
    "主人设置了保密",
    "您没有权限查看",
    "没有权限查看",
    "对不起",
    "无权访问",
    "access denied",
    "private space",
    "not authorized",
)


def _is_permission_denied_message(message: Any) -> bool:
    text = str(message or "").strip()
    if not text:
        return False
    lowered = text.lower()
    return any(hint.lower() in lowered for hint in _PERMISSION_DENIED_HINTS)


def _get_permission_block_bucket(state: dict[str, Any]) -> dict[str, Any]:
    bucket = state.get("qzone_permission_blocked")
    if not isinstance(bucket, dict):
        bucket = {}
        state["qzone_permission_blocked"] = bucket
    return bucket


def _is_qzone_user_blocked(state: dict[str, Any], user_id: str, *, now_ts: float | None = None) -> bool:
    target = str(user_id or "").strip()
    if not target:
        return False
    bucket = _get_permission_block_bucket(state)
    entry = bucket.get(target)
    if not isinstance(entry, dict):
        return False
    last_checked = float(entry.get("last_checked_ts", 0) or 0)
    now = float(now_ts or time.time())
    # 一周内不再重试，过期后允许 recheck（调用方调用 fetch 时若再次失败会重新刷新 last_checked_ts）
    return (now - last_checked) < _PERMISSION_BLOCK_RECHECK_SECONDS


def _mark_qzone_user_blocked(state: dict[str, Any], user_id: str, message: str) -> None:
    target = str(user_id or "").strip()
    if not target:
        return
    bucket = _get_permission_block_bucket(state)
    now = time.time()
    entry = bucket.get(target)
    if not isinstance(entry, dict):
        entry = {"first_blocked_ts": now, "blocked_count": 0}
    entry["last_checked_ts"] = now
    entry["last_error_message"] = str(message or "")[:240]
    entry["blocked_count"] = int(entry.get("blocked_count", 0) or 0) + 1
    bucket[target] = entry


def _clear_qzone_user_block(state: dict[str, Any], user_id: str) -> None:
    target = str(user_id or "").strip()
    if not target:
        return
    bucket = _get_permission_block_bucket(state)
    if target in bucket:
        bucket.pop(target, None)


def _handle_qzone_fetch_outcome(
    *,
    state: dict[str, Any],
    user_id: str,
    ok: bool,
    msg: str,
    logger: Any,
) -> bool:
    """根据 fetch_user_feeds 结果维护权限黑名单。返回值表示是否触发了 permission_block。"""
    target = str(user_id or "").strip()
    if not target:
        return False
    if ok:
        _clear_qzone_user_block(state, target)
        return False
    if _is_permission_denied_message(msg):
        bucket = _get_permission_block_bucket(state)
        existed = target in bucket
        _mark_qzone_user_blocked(state, target, msg)
        if not existed:
            logger.info(f"[qzone_social] user {target} marked as permission_blocked: {msg}")
        else:
            logger.debug(f"[qzone_social] user {target} still permission_blocked: {msg}")
        return True
    return False


async def recheck_qzone_permission_blocked_users(
    *,
    bot: Any,
    qzone_social_service: Any,
    logger: Any,
) -> dict[str, Any]:
    """周期性重检 qzone_permission_blocked 中的用户，看 bot 是否重新获得查看权限。

    每周运行一次。对每个 uid：
    - 调用 fetch_user_feeds 探测一次
    - 成功 → 从黑名单移除
    - 仍被拒 → 刷新 last_checked_ts 维持黑名单
    - 其他错误（cookie 失效、网络等）→ 不修改黑名单状态，避免误清
    """
    bot_id = str(getattr(bot, "self_id", "") or "")
    result: dict[str, Any] = {
        "checked": 0,
        "unblocked": 0,
        "still_blocked": 0,
        "errors": 0,
        "last_error": "",
    }
    if not bot_id:
        result["last_error"] = "bot_id_missing"
        return result
    state = await get_data_store().load(_STORE_NAME, default=lambda: {})
    if not isinstance(state, dict):
        state = {}
    bucket = state.get("qzone_permission_blocked")
    if not isinstance(bucket, dict) or not bucket:
        return result
    targets = list(bucket.keys())
    for target_uid in targets:
        if not target_uid or target_uid == bot_id:
            continue
        result["checked"] += 1
        try:
            ok, msg, _feeds = await qzone_social_service.fetch_user_feeds(
                target_uin=target_uid,
                bot_id=bot_id,
                count=1,
                include_comments=False,
            )
        except Exception as exc:
            result["errors"] += 1
            result["last_error"] = f"recheck {target_uid} crashed: {exc}"
            logger.warning(f"[qzone_permission_recheck] error checking {target_uid}: {exc}")
            continue
        if ok:
            _clear_qzone_user_block(state, target_uid)
            result["unblocked"] += 1
            logger.info(f"[qzone_permission_recheck] unblocked {target_uid}, bot has access again")
        elif _is_permission_denied_message(msg):
            _mark_qzone_user_blocked(state, target_uid, msg)
            result["still_blocked"] += 1
        else:
            # 非权限错误（cookie 失效、网络等），不修改黑名单状态
            result["errors"] += 1
            result["last_error"] = f"{target_uid}: {msg}"[:240]
    state["last_permission_recheck_at"] = time.time()
    get_data_store().save_sync(_STORE_NAME, state)
    logger.info(
        f"[qzone_permission_recheck] done: checked={result['checked']} "
        f"unblocked={result['unblocked']} still_blocked={result['still_blocked']} "
        f"errors={result['errors']}"
    )
    return result


def _extract_system_prompt(prompt_data: Any) -> str:
    if isinstance(prompt_data, dict):
        value = prompt_data.get("system")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return str(prompt_data)
    if isinstance(prompt_data, str):
        return prompt_data.strip()
    return ""


def _extract_json_object(raw: Any) -> dict[str, Any] | None:
    # 兜底：先剥掉 yaml prompt 的思维链 XML 包装，避免 JSON 被 <output><message>...</message></output> 吞掉
    text = strip_response_control_markers(str(raw or ""))
    text = clean_generated_text(text).strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text).rstrip("`").strip()
    try:
        parsed = json.loads(text)
    except Exception:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except Exception:
            return None
    return parsed if isinstance(parsed, dict) else None


def _normalize_limit(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except Exception:
        return 0


def _limit_reached(limit: Any, count: Any) -> bool:
    normalized_limit = _normalize_limit(limit)
    if normalized_limit <= 0:
        return False
    try:
        current = int(count or 0)
    except Exception:
        current = 0
    return current >= normalized_limit


def _trim_comment(text: Any, *, max_chars: int = 36) -> str:
    cleaned = clean_generated_text(str(text or ""))
    cleaned = re.sub(r"</?[^>]+>", "", cleaned)
    cleaned = cleaned.strip().strip('"').strip("'").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[:max_chars].rstrip("，、,.。!！?？ ")


def _format_ts(ts: float) -> str:
    if not ts:
        return "未记录"
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "未知"


def _get_persona_snippet(persona_store: Any, user_id: str, max_chars: int) -> str:
    if persona_store is None:
        return ""
    getter = getattr(persona_store, "get_persona_snippet", None)
    if not callable(getter):
        return ""
    try:
        return str(getter(str(user_id), max_chars=max(1, int(max_chars))) or "").strip()
    except Exception:
        return ""


async def _get_friend_profiles(bot: Any, logger: Any) -> dict[str, dict[str, Any]]:
    try:
        friends = await bot.get_friend_list()
    except Exception as exc:
        logger.warning(f"[qzone_social] get_friend_list failed: {exc}")
        return {}
    profiles: dict[str, dict[str, Any]] = {}
    if not isinstance(friends, list):
        return profiles
    for item in friends:
        if not isinstance(item, dict) or item.get("user_id") is None:
            continue
        user_id = str(item.get("user_id"))
        profiles[user_id] = {
            "nickname": str(
                item.get("remark")
                or item.get("nickname")
                or item.get("user_remark")
                or user_id
            )
        }
    return profiles


def _normalize_state(now: datetime) -> dict[str, Any]:
    today = now.strftime("%Y-%m-%d")
    store = get_data_store()
    state = store.load_sync(_STORE_NAME)
    if not isinstance(state, dict):
        state = {}
    if state.get("date") != today:
        state["date"] = today
        state["like_count"] = 0
        state["comment_count"] = 0
        state["per_friend"] = {}
    state.setdefault("seen", {})
    state.setdefault("reacted", {})
    state.setdefault("comment_replies", {})
    state.setdefault("comment_actions", {})
    state.setdefault("profile_records", {})
    state.setdefault("bot_outbound_comments", {})
    state.setdefault("bot_outbound_replies", {})
    state.setdefault("bot_space_comment_baselines", {})
    state.setdefault("per_friend", {})
    state.setdefault("like_count", 0)
    state.setdefault("comment_count", 0)
    return state


def _prune_state_maps(state: dict[str, Any], *, max_items: int = 2000) -> None:
    for key in (
        "seen",
        "reacted",
        "comment_replies",
        "comment_actions",
        "profile_records",
        "bot_outbound_comments",
        "bot_outbound_replies",
        "bot_space_comment_baselines",
    ):
        value = state.get(key)
        if not isinstance(value, dict) or len(value) <= max_items:
            continue
        items = sorted(
            value.items(),
            key=lambda item: float((item[1] if isinstance(item[1], dict) else {}).get("at", 0) or 0),
            reverse=True,
        )
        state[key] = dict(items[:max_items])
    # 权限黑名单按 last_checked_ts 排序裁剪
    block_bucket = state.get("qzone_permission_blocked")
    if isinstance(block_bucket, dict) and len(block_bucket) > max_items:
        items = sorted(
            block_bucket.items(),
            key=lambda item: float((item[1] if isinstance(item[1], dict) else {}).get("last_checked_ts", 0) or 0),
            reverse=True,
        )
        state["qzone_permission_blocked"] = dict(items[:max_items])


def _save_state(state: dict[str, Any], result: dict[str, Any]) -> None:
    state["last_scan_at"] = time.time()
    state["last_result"] = dict(result)
    if result.get("last_error"):
        state["last_error"] = str(result.get("last_error") or "")[:240]
    else:
        state["last_error"] = ""
    _prune_state_maps(state)
    get_data_store().save_sync(_STORE_NAME, state)


def _save_inbound_state(state: dict[str, Any], result: dict[str, Any]) -> None:
    state["last_inbound_scan_at"] = time.time()
    state["last_inbound_result"] = dict(result)
    if result.get("last_error"):
        state["last_inbound_error"] = str(result.get("last_error") or "")[:240]
    else:
        state["last_inbound_error"] = ""
    _prune_state_maps(state)
    get_data_store().save_sync(_STORE_NAME, state)


def _outbound_reply_scan_due(state: dict[str, Any], *, now_ts: float, interval_minutes: Any) -> bool:
    try:
        interval_seconds = max(1, int(interval_minutes or 1)) * 60
    except Exception:
        interval_seconds = 180
    try:
        last_scan_at = float(state.get("last_outbound_reply_scan_at", 0) or 0)
    except Exception:
        last_scan_at = 0.0
    return last_scan_at <= 0 or float(now_ts or 0) - last_scan_at >= interval_seconds


def _mark_outbound_reply_scan(state: dict[str, Any], *, now_ts: float) -> None:
    state["last_outbound_reply_scan_at"] = float(now_ts or time.time())


def _daily_friend_state(state: dict[str, Any], user_id: str, today: str) -> dict[str, Any]:
    per_friend = state.setdefault("per_friend", {})
    if not isinstance(per_friend, dict):
        per_friend = {}
        state["per_friend"] = per_friend
    item = per_friend.get(user_id)
    if not isinstance(item, dict) or item.get("date") != today:
        item = {"date": today, "like_count": 0, "comment_count": 0, "action_count": 0}
        per_friend[user_id] = item
    return item


def _mark_seen(state: dict[str, Any], feed_key: str) -> None:
    seen = state.setdefault("seen", {})
    if isinstance(seen, dict):
        seen[feed_key] = {"at": time.time()}


def _mark_reacted(state: dict[str, Any], feed_key: str, *, action: str, comment: str = "") -> None:
    reacted = state.setdefault("reacted", {})
    if isinstance(reacted, dict):
        reacted[feed_key] = {"at": time.time(), "action": action, "comment": comment}


def _feed_already_reacted(state: dict[str, Any], feed_key: str) -> bool:
    reacted = state.get("reacted")
    return isinstance(reacted, dict) and feed_key in reacted


def _comment_already_replied(state: dict[str, Any], comment_key: str) -> bool:
    replies = state.get("comment_replies")
    return isinstance(replies, dict) and comment_key in replies


def _mark_comment_replied(state: dict[str, Any], comment_key: str, *, reply: str) -> None:
    replies = state.setdefault("comment_replies", {})
    if isinstance(replies, dict):
        replies[comment_key] = {"at": time.time(), "reply": reply}


def _comment_already_processed(state: dict[str, Any], comment_key: str) -> bool:
    actions = state.get("comment_actions")
    if isinstance(actions, dict) and comment_key in actions:
        return True
    return _comment_already_replied(state, comment_key)


def _mark_comment_processed(
    state: dict[str, Any],
    comment_key: str,
    *,
    action: str,
    reply: str = "",
    reason: str = "",
) -> None:
    actions = state.setdefault("comment_actions", {})
    if isinstance(actions, dict):
        actions[comment_key] = {
            "at": time.time(),
            "action": str(action or "ignore"),
            "reply": str(reply or "")[:80],
            "reason": str(reason or "")[:120],
        }
    if action == "reply" and reply:
        _mark_comment_replied(state, comment_key, reply=reply)


def _bot_space_feed_has_baseline(state: dict[str, Any], feed_key: str) -> bool:
    baselines = state.get("bot_space_comment_baselines")
    return isinstance(baselines, dict) and feed_key in baselines


def _mark_bot_space_feed_baseline(
    state: dict[str, Any],
    feed_key: str,
    comments: list[dict[str, Any]],
) -> None:
    baselines = state.setdefault("bot_space_comment_baselines", {})
    if not isinstance(baselines, dict):
        baselines = {}
        state["bot_space_comment_baselines"] = baselines
    max_created_at = 0.0
    for comment in comments:
        try:
            max_created_at = max(max_created_at, float(comment.get("created_at", 0) or 0))
        except Exception:
            continue
    baselines[feed_key] = {"at": time.time(), "max_created_at": max_created_at}


def _bot_space_comment_is_before_baseline(
    state: dict[str, Any],
    feed_key: str,
    comment: dict[str, Any],
) -> bool:
    baselines = state.get("bot_space_comment_baselines")
    if not isinstance(baselines, dict):
        return False
    baseline = baselines.get(feed_key)
    if not isinstance(baseline, dict):
        return False
    try:
        max_created_at = float(baseline.get("max_created_at", 0) or 0)
        created_at = float(comment.get("created_at", 0) or 0)
    except Exception:
        return False
    return bool(max_created_at and created_at and created_at < max_created_at)


def _profile_evidence_already_recorded(state: dict[str, Any], evidence_key: str) -> bool:
    records = state.get("profile_records")
    return isinstance(records, dict) and evidence_key in records


def _mark_profile_evidence_recorded(state: dict[str, Any], evidence_key: str) -> None:
    records = state.setdefault("profile_records", {})
    if isinstance(records, dict):
        records[evidence_key] = {"at": time.time()}


async def _record_qzone_profile_evidence(
    *,
    persona_store: Any,
    user_id: str,
    evidence_key: str,
    kind: str,
    content: str,
    image_summary: str = "",
    state: dict[str, Any],
    result: dict[str, Any],
    logger: Any,
) -> None:
    if persona_store is None or not user_id or not evidence_key:
        return
    if _profile_evidence_already_recorded(state, evidence_key):
        return
    text = str(content or "").strip()
    visual = str(image_summary or "").strip()
    if not text and not visual:
        return
    lines = [f"[QQ空间{kind}]"]
    if text:
        lines.append(text[:240])
    if visual:
        lines.append(f"图片摘要：{visual[:180]}")
    record_message = getattr(persona_store, "record_message", None)
    if not callable(record_message):
        return
    try:
        await record_message(str(user_id), "\n".join(lines))
    except Exception as exc:
        logger.warning(f"[qzone_social] record qzone profile evidence failed for {user_id}: {exc}")
        return
    _mark_profile_evidence_recorded(state, evidence_key)
    result["profile_records"] = int(result.get("profile_records", 0) or 0) + 1


def _collect_candidates(
    *,
    friend_profiles: dict[str, dict[str, Any]],
    proactive_state: dict[str, dict[str, Any]],
    persona_store: Any,
    persona_snippet_max_chars: int,
    target_user_id: str = "",
    allow_open_user: bool = False,
) -> list[dict[str, Any]]:
    if target_user_id:
        profile = friend_profiles.get(str(target_user_id))
        if not profile and not allow_open_user:
            return []
        snippet = _get_persona_snippet(persona_store, str(target_user_id), persona_snippet_max_chars)
        state = proactive_state.get(str(target_user_id), {})
        return [
            {
                "user_id": str(target_user_id),
                "nickname": str((profile or {}).get("nickname", "") or target_user_id),
                "persona_snippet": snippet or "暂无画像",
                "last_interaction": float((state if isinstance(state, dict) else {}).get("last_interaction", 0) or 0),
                "is_friend": bool(profile),
            }
        ]

    candidates: list[dict[str, Any]] = []
    for user_id, user_state in proactive_state.items():
        uid = str(user_id)
        if uid.startswith("group_") or uid not in friend_profiles:
            continue
        if not isinstance(user_state, dict):
            continue
        last_interaction = float(user_state.get("last_interaction", 0) or 0)
        if last_interaction <= 0:
            continue
        profile = friend_profiles[uid]
        snippet = _get_persona_snippet(persona_store, uid, persona_snippet_max_chars)
        candidates.append(
            {
                "user_id": uid,
                "nickname": str(profile.get("nickname", "") or uid),
                "persona_snippet": snippet,
                "last_interaction": last_interaction,
                "is_friend": True,
            }
        )
    candidates.sort(key=lambda item: float(item.get("last_interaction", 0) or 0), reverse=True)
    if candidates:
        return candidates[:12]

    # Fresh deployments may have friend access and Qzone permission before private
    # proactive_state has any last_interaction records. Still keep the hard bot-friend
    # boundary; unreadable spaces are filtered by fetch_user_feeds permissions.
    fallback: list[dict[str, Any]] = []
    for uid, profile in friend_profiles.items():
        user_id = str(uid)
        if not user_id or user_id.startswith("group_"):
            continue
        snippet = _get_persona_snippet(persona_store, user_id, persona_snippet_max_chars)
        fallback.append(
            {
                "user_id": user_id,
                "nickname": str(profile.get("nickname", "") or user_id),
                "persona_snippet": snippet,
                "last_interaction": 0.0,
                "is_friend": True,
            }
        )
    fallback.sort(key=lambda item: item["user_id"])
    return fallback[:12]


async def _summarize_images(
    *,
    vision_caller: Any,
    images: list[str],
    logger: Any,
) -> str:
    if not vision_caller or not images:
        return ""
    return await summarize_images_with_vision(
        vision_caller=vision_caller,
        image_urls=images[:3],
        sticker_like=False,
        sticker_prompt="请用一句话描述这张 QQ 空间动态配图的主体和情绪氛围。",
        person_prompt="请用一句话描述这张 QQ 空间动态配图的主体、场景和情绪氛围。不要编造图中不存在的文字或事件。",
        cache_namespace="qzone_social_v1",
        logger=logger,
    )


async def _decide_feed_action(
    *,
    feed: dict[str, Any],
    candidate: dict[str, Any],
    system_prompt: str,
    call_ai_api: Callable[..., Awaitable[Optional[str]]],
    image_summary: str,
    inner_state: dict[str, Any],
    emotion_memory: str,
) -> dict[str, Any]:
    prompt = (
        "你正在阅读一位好友刚发的 QQ 空间动态。把它当成熟人朋友圈，决定是否互动。\n"
        "输出严格 JSON：{\"action\":\"ignore|like|comment|like_comment\",\"comment\":\"可选评论\",\"reason\":\"极短原因\"}。\n\n"
        f"好友 QQ：{candidate['user_id']}\n"
        f"好友昵称：{candidate['nickname']}\n"
        f"好友画像：{candidate.get('persona_snippet') or '暂无'}\n"
        f"上次互动：{_format_ts(float(candidate.get('last_interaction', 0) or 0))}\n"
        f"近期情绪记忆：{emotion_memory or '暂无'}\n\n"
        f"你的当前心情：{inner_state.get('mood', DEFAULT_STATE['mood'])}\n"
        f"你的当前精力：{inner_state.get('energy', DEFAULT_STATE['energy'])}\n\n"
        f"动态作者：{feed.get('nickname') or candidate['nickname']}（{feed.get('owner_uin') or candidate['user_id']}）\n"
        f"动态时间戳：{feed.get('created_at') or 0}\n"
        f"动态正文：{feed.get('content') or '（无文字）'}\n"
        f"图片摘要：{image_summary or ('有图片，但视觉摘要不可用' if feed.get('images') else '无图片')}\n\n"
        "互动要求：\n"
        "- 默认倾向轻互动：日常熟人内容能点赞就点赞；动态里有具体可接的细节就 like_comment；只有确实没话可说时才 ignore。\n"
        "- 评论像真人随手留言：4-15 个中文字符为主，最多 30 个；可以是半句话、一个反问、一句吐槽、跳跃的小联想；不必工整结尾。\n"
        "- 允许跳跃：评论不必紧扣动态全部内容，抓一个细节回应即可，不用补全前因后果。\n"
        "- 不要小作文、不要客服腔、不要互联网黑话和热梗、不要 hashtag、不要堆叠表情符号。\n"
        "- 不要暴露你在分析画像、情绪记忆或系统规则。\n"
        "- 对沉重、争议、隐私、求助、疾病、事故等不适合轻浮互动的内容，优先 ignore 或仅 like。\n"
        "- 图片摘要不可用时，不要评论具体画面。"
    )
    messages = inject_current_time_context(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]
    )
    result = await call_ai_api(messages)
    payload = _extract_json_object(result)
    if payload is None:
        return {"action": "ignore", "comment": "", "reason": "llm_parse_failed"}
    action = str(payload.get("action", "") or "").strip().lower()
    if action not in {"ignore", "like", "comment", "like_comment"}:
        action = "ignore"
    comment = _trim_comment(payload.get("comment", ""))
    if action in {"comment", "like_comment"} and not comment:
        action = "like" if action == "like_comment" else "ignore"
    return {
        "action": action,
        "comment": comment,
        "reason": str(payload.get("reason", "") or "").strip()[:80],
    }


def _comment_targets_user(comment: dict[str, Any], user_id: str) -> bool:
    target = str(user_id or "").strip()
    if not target:
        return False
    return any(
        str(comment.get(key, "") or "").strip() == target
        for key in ("reply_to_user_id", "parent_user_id")
    )


def _format_qzone_comment_thread(
    comments: list[dict[str, Any]],
    current_comment: dict[str, Any],
    *,
    bot_id: str,
    max_items: int = 8,
) -> str:
    current_key = str(current_comment.get("comment_key", "") or "")
    try:
        current_ts = float(current_comment.get("created_at", 0) or 0)
    except Exception:
        current_ts = 0.0
    ordered = sorted(
        [item for item in comments if isinstance(item, dict)],
        key=lambda item: float(item.get("created_at", 0) or 0),
    )
    selected: list[dict[str, Any]] = []
    for item in ordered:
        try:
            item_ts = float(item.get("created_at", 0) or 0)
        except Exception:
            item_ts = 0.0
        item_key = str(item.get("comment_key", "") or "")
        if current_ts and item_ts and item_ts > current_ts:
            continue
        selected.append(item)
        if current_key and item_key == current_key:
            break
    if not selected:
        selected = [current_comment]

    lines: list[str] = []
    for item in selected[-max(1, int(max_items or 8)):]:
        commenter_id = str(item.get("user_id", "") or "")
        nickname = str(item.get("nickname", "") or commenter_id or "未知")
        speaker = "我" if bot_id and commenter_id == bot_id else nickname
        reply_to = str(item.get("reply_to_user_id", "") or item.get("parent_user_id", "") or "")
        relation = ""
        if reply_to:
            if bot_id and reply_to == bot_id:
                relation = " 回复我"
            else:
                relation_name = str(item.get("parent_nickname", "") or reply_to)
                relation = f" 回复{relation_name}"
        marker = "（当前）" if current_key and str(item.get("comment_key", "") or "") == current_key else ""
        content = _trim_comment(item.get("content", ""), max_chars=60)
        if content:
            lines.append(f"{speaker}{relation}{marker}：{content}")
    return "\n".join(lines) or "暂无评论链"


def _latest_bot_reply_for_feed(state: dict[str, Any], feed_key: str) -> str:
    prefix = f"{feed_key}:"
    latest_at = 0.0
    latest_reply = ""
    for key in ("comment_actions", "bot_outbound_replies"):
        values = state.get(key)
        if not isinstance(values, dict):
            continue
        for item_key, item in values.items():
            if feed_key and not str(item_key).startswith(prefix):
                continue
            if not isinstance(item, dict) or str(item.get("action", "") or "") != "reply":
                continue
            reply = str(item.get("reply", "") or "").strip()
            if not reply:
                continue
            try:
                at = float(item.get("at", 0) or 0)
            except Exception:
                at = 0.0
            if at >= latest_at:
                latest_at = at
                latest_reply = reply
    return latest_reply


def _reply_count_for_feed(state: dict[str, Any], feed_key: str) -> int:
    prefix = f"{feed_key}:"
    count = 0
    for key in ("comment_actions", "bot_outbound_replies"):
        values = state.get(key)
        if not isinstance(values, dict):
            continue
        for item_key, item in values.items():
            if feed_key and not str(item_key).startswith(prefix):
                continue
            if isinstance(item, dict) and str(item.get("action", "") or "") == "reply":
                count += 1
    return count


async def _decide_bot_comment_reply(
    *,
    feed: dict[str, Any],
    comment: dict[str, Any],
    commenter_profile: dict[str, Any],
    system_prompt: str,
    call_ai_api: Callable[..., Awaitable[Optional[str]]],
    inner_state: dict[str, Any],
    emotion_memory: str,
    allow_third_party_chime_in: bool = True,
    bot_id: str = "",
    is_reply_to_bot: bool = False,
    previous_bot_reply: str = "",
    recent_thread: str = "",
    interaction_rounds: int = 0,
) -> dict[str, Any]:
    third_party_rule = (
        "- 如果对方明显是在 @ 别人或接续别人的话，决策可以是 ignore，也可以选择以好友身份轻量插一句。\n"
        if allow_third_party_chime_in
        else "- 如果对方明显是在 @ 别人或接续别人的话，action=ignore；不要插入第三方对话。\n"
    )
    continuation_block = ""
    if is_reply_to_bot:
        continuation_block = (
            "当前是评论回响场景：对方这条留言是在回复你上一句空间评论。\n"
            f"你上一句评论：{previous_bot_reply or '（未记录）'}\n"
            f"这条评论链最近内容：\n{recent_thread or '暂无评论链'}\n"
            f"你在这条动态下已连续回复轮数：{max(0, int(interaction_rounds or 0))}\n\n"
            "继续互动判断：\n"
            "- 不要默认必须回复；先判断对方这句是否还有继续聊下去的价值。\n"
            "- 如果对方只是很短的敷衍、复读、补一个占位词、话题已经自然结束，action=ignore，让对话停住。\n"
            "- 如果对方是在接你的话继续开玩笑、追问、补充信息，或你能自然回一句不尴尬，action=reply。\n"
            "- 已经来回多轮时更克制，除非对方明显把话递给你。\n\n"
        )
    prompt = (
        "有人在 QQ 空间动态下留言或评论区里聊天。判断是否自然回复或插一句话。\n"
        "输出严格 JSON：{\"action\":\"ignore|reply\",\"reply\":\"可选回复\",\"reason\":\"极短原因\"}。\n\n"
        f"{continuation_block}"
        f"你的 QQ：{bot_id or '未知'}\n"
        f"留言用户 QQ：{comment['user_id']}\n"
        f"留言用户昵称：{comment.get('nickname') or commenter_profile.get('nickname') or comment['user_id']}\n"
        f"用户画像：{commenter_profile.get('persona_snippet') or '暂无'}\n"
        f"近期情绪记忆：{emotion_memory or '暂无'}\n\n"
        f"你的当前心情：{inner_state.get('mood', DEFAULT_STATE['mood'])}\n"
        f"你的当前精力：{inner_state.get('energy', DEFAULT_STATE['energy'])}\n\n"
        f"相关空间动态正文：{feed.get('content') or '（无文字）'}\n"
        f"对方留言：{comment.get('content') or ''}\n\n"
        "回复要求：\n"
        "- 留言可能是直接对你说，也可能是 ta 在和评论区里另一个人聊；先判断这条话锋指向谁。\n"
        f"{third_party_rule}"
        "- 不要解释“这句话是什么意思”，不要说“太泛了/要看上下文”。\n"
        "- 非评论回响场景默认倾向 reply；评论回响场景由上面的“继续互动判断”决定，不要硬续。\n"
        "- 回复 6-30 个中文字符，像在空间评论区随手回一句；可以是半句话、反问、跳跃的小联想，不必工整。\n"
        "- 不要客服腔、小作文、互联网黑话、系统说明，也不要暴露画像或规则。"
    )
    messages = inject_current_time_context(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]
    )
    result = await call_ai_api(messages)
    payload = _extract_json_object(result)
    if payload is None:
        return {"action": "ignore", "reply": "", "reason": "llm_parse_failed"}
    action = str(payload.get("action", "") or "").strip().lower()
    reply = _trim_comment(payload.get("reply", ""), max_chars=44)
    if action != "reply" or not reply:
        return {"action": "ignore", "reply": "", "reason": str(payload.get("reason", "") or "")[:80]}
    return {"action": "reply", "reply": reply, "reason": str(payload.get("reason", "") or "")[:80]}


def _apply_action_limits(
    *,
    decision: dict[str, Any],
    state: dict[str, Any],
    friend_state: dict[str, Any],
    like_limit: int,
    comment_limit: int,
    per_friend_limit: int,
) -> dict[str, Any]:
    action = str(decision.get("action", "") or "ignore")
    if action == "ignore":
        return decision
    if _limit_reached(per_friend_limit, friend_state.get("action_count", 0)):
        return {"action": "ignore", "comment": "", "reason": "per_friend_limit_reached"}
    want_like = action in {"like", "like_comment"}
    want_comment = action in {"comment", "like_comment"}
    if want_like and _limit_reached(like_limit, state.get("like_count", 0)):
        want_like = False
    if want_comment and _limit_reached(comment_limit, state.get("comment_count", 0)):
        want_comment = False
    if want_like and want_comment:
        action = "like_comment"
    elif want_like:
        action = "like"
    elif want_comment:
        action = "comment"
    else:
        action = "ignore"
    updated = dict(decision)
    updated["action"] = action
    if action == "ignore":
        updated["comment"] = ""
        updated["reason"] = "limit_reached"
    return updated


async def _scan_bot_space_comments(
    *,
    bot: Any,
    qzone_social_service: Any,
    friend_profiles: dict[str, dict[str, Any]],
    proactive_state: dict[str, dict[str, Any]],
    persona_store: Any,
    persona_snippet_max_chars: int,
    system_prompt: str,
    call_ai_api: Callable[..., Awaitable[Optional[str]]],
    inner_state: dict[str, Any],
    emotion_state: dict[str, Any],
    max_feeds: int,
    max_comments_per_feed: int = 20,
    count_checked_feeds: bool = False,
    allow_third_party_chime_in: bool = True,
    process_existing_comments: bool = True,
    state: dict[str, Any],
    result: dict[str, Any],
    logger: Any,
) -> None:
    bot_id = str(getattr(bot, "self_id", "") or "")
    if not bot_id:
        return
    ok, msg, feeds = await qzone_social_service.fetch_user_feeds(
        target_uin=bot_id,
        bot_id=bot_id,
        count=max(1, min(100, int(max_feeds or 20))),
        include_comments=True,
        comment_count=max_comments_per_feed,
    )
    if not ok:
        result["failed"] = int(result.get("failed", 0) or 0) + 1
        result["last_error"] = msg
        logger.warning(f"[qzone_social] fetch bot feeds failed: {msg}")
        return

    checked_feeds = feeds[: max(1, min(100, int(max_feeds or 20)))]
    if count_checked_feeds:
        result["scanned_users"] = int(result.get("scanned_users", 0) or 0) + 1
        result["feeds_seen"] = int(result.get("feeds_seen", 0) or 0) + len(checked_feeds)

    for feed in checked_feeds:
        feed_key = str(feed.get("feed_key", "") or "")
        comments = _extract_qzone_comments(feed.get("raw") if isinstance(feed.get("raw"), dict) else {})
        if not process_existing_comments and feed_key and not _bot_space_feed_has_baseline(state, feed_key):
            for comment in comments[: max(1, min(100, int(max_comments_per_feed or 20)))]:
                commenter_id = str(comment.get("user_id", "") or "")
                if not commenter_id or commenter_id == bot_id:
                    continue
                comment_key = f"{feed_key}:{comment.get('comment_key') or commenter_id}"
                _mark_comment_processed(state, comment_key, action="baseline", reason="existing_before_first_scan")
            _mark_bot_space_feed_baseline(state, feed_key, comments)
            continue
        for comment in comments[: max(1, min(100, int(max_comments_per_feed or 20)))]:
            commenter_id = str(comment.get("user_id", "") or "")
            if not commenter_id or commenter_id == bot_id:
                continue
            comment_key = f"{feed_key}:{comment.get('comment_key') or commenter_id}"
            if _comment_already_processed(state, comment_key):
                continue
            if (
                not process_existing_comments
                and feed_key
                and _bot_space_comment_is_before_baseline(state, feed_key, comment)
            ):
                _mark_comment_processed(state, comment_key, action="baseline", reason="before_feed_baseline")
                continue
            result["inbound_comments"] = int(result.get("inbound_comments", 0) or 0) + 1
            await _record_qzone_profile_evidence(
                persona_store=persona_store,
                user_id=commenter_id,
                evidence_key=f"comment:{comment_key}",
                kind="留言",
                content=str(comment.get("content", "") or ""),
                state=state,
                result=result,
                logger=logger,
            )
            profile = friend_profiles.get(commenter_id, {})
            persona_snippet = _get_persona_snippet(persona_store, commenter_id, persona_snippet_max_chars)
            commenter_profile = {
                "nickname": str(profile.get("nickname", "") or comment.get("nickname") or commenter_id),
                "persona_snippet": persona_snippet,
                "last_interaction": float(
                    (proactive_state.get(commenter_id, {}) if isinstance(proactive_state.get(commenter_id), dict) else {})
                    .get("last_interaction", 0)
                    or 0
                ),
            }
            emotion_memory = describe_user_emotion_memory(emotion_state or {}, commenter_id)
            is_reply_to_bot = _comment_targets_user(comment, bot_id)
            previous_bot_reply = _latest_bot_reply_for_feed(state, feed_key) if is_reply_to_bot else ""
            decision = await _decide_bot_comment_reply(
                feed=feed,
                comment=comment,
                commenter_profile=commenter_profile,
                system_prompt=system_prompt,
                call_ai_api=call_ai_api,
                inner_state=inner_state,
                emotion_memory=emotion_memory,
                allow_third_party_chime_in=allow_third_party_chime_in,
                bot_id=bot_id,
                is_reply_to_bot=is_reply_to_bot,
                previous_bot_reply=previous_bot_reply,
                recent_thread=_format_qzone_comment_thread(
                    comments,
                    comment,
                    bot_id=bot_id,
                ),
                interaction_rounds=_reply_count_for_feed(state, feed_key),
            )
            if str(decision.get("action", "") or "") != "reply":
                _mark_comment_processed(
                    state,
                    comment_key,
                    action="ignore",
                    reason=str(decision.get("reason", "") or ""),
                )
                continue
            reply = str(decision.get("reply", "") or "").strip()
            if not reply:
                _mark_comment_processed(
                    state,
                    comment_key,
                    action="ignore",
                    reason=str(decision.get("reason", "") or "empty_reply"),
                )
                continue
            ok_reply, reply_msg = await qzone_social_service.comment_feed(
                feed=feed,
                bot_id=bot_id,
                content=reply,
                reply_to_comment=comment,
            )
            if ok_reply:
                result["replied"] = int(result.get("replied", 0) or 0) + 1
                _mark_comment_processed(state, comment_key, action="reply", reply=reply)
            else:
                result["failed"] = int(result.get("failed", 0) or 0) + 1
                result["last_error"] = reply_msg
                logger.warning(f"[qzone_social] reply comment failed for {comment_key}: {reply_msg}")


async def _scan_bot_outbound_comment_replies(
    *,
    bot: Any,
    qzone_social_service: Any,
    plugin_config: Any,
    friend_profiles: dict[str, dict[str, Any]],
    proactive_state: dict[str, dict[str, Any]],
    persona_store: Any,
    persona_snippet_max_chars: int,
    system_prompt: str,
    call_ai_api: Callable[..., Awaitable[Optional[str]]],
    inner_state: dict[str, Any],
    emotion_state: dict[str, Any],
    state: dict[str, Any],
    result: dict[str, Any],
    logger: Any,
    allow_third_party_chime_in: bool = True,
) -> None:
    """对 Bot 之前在好友空间评论过的动态进行 3 分钟近实时反查，看是否有人回复 Bot。"""
    bot_id = str(getattr(bot, "self_id", "") or "")
    if not bot_id:
        return
    outbound = state.get("bot_outbound_comments")
    if not isinstance(outbound, dict) or not outbound:
        return

    lookback_seconds = max(
        0.0,
        float(getattr(plugin_config, "personification_qzone_outbound_reply_lookback_hours", 72.0) or 72.0)
        * 3600.0,
    )
    max_feeds = max(
        1,
        int(getattr(plugin_config, "personification_qzone_outbound_reply_max_feeds", 30) or 30),
    )
    now_ts = time.time()
    items = sorted(
        outbound.items(),
        key=lambda pair: float((pair[1] if isinstance(pair[1], dict) else {}).get("recorded_at", 0) or 0),
        reverse=True,
    )

    by_owner: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for feed_key, info in items:
        if not isinstance(info, dict):
            continue
        recorded_at = float(info.get("recorded_at", 0) or 0)
        if lookback_seconds and recorded_at and now_ts - recorded_at > lookback_seconds:
            outbound.pop(feed_key, None)
            continue
        owner = str(info.get("target_uin", "") or "")
        if not owner or owner == bot_id:
            continue
        by_owner.setdefault(owner, []).append((feed_key, info))
        if sum(len(v) for v in by_owner.values()) >= max_feeds:
            break

    if not by_owner:
        return

    bot_replies_state = state.setdefault("bot_outbound_replies", {})
    if not isinstance(bot_replies_state, dict):
        bot_replies_state = {}
        state["bot_outbound_replies"] = bot_replies_state

    for owner_uin, owner_items in by_owner.items():
        if _is_qzone_user_blocked(state, owner_uin):
            logger.debug(f"[qzone_outbound] skip permission-blocked owner {owner_uin}")
            continue
        try:
            ok, msg, feeds = await qzone_social_service.fetch_user_feeds(
                target_uin=owner_uin,
                bot_id=bot_id,
                count=max(1, min(50, len(owner_items) + 5)),
                include_comments=True,
                comment_count=20,
            )
        except Exception as exc:
            result["failed"] = int(result.get("failed", 0) or 0) + 1
            result["last_error"] = f"outbound fetch_user_feeds failed: {exc}"
            logger.warning(f"[qzone_outbound] fetch failed for owner {owner_uin}: {exc}")
            continue
        if _handle_qzone_fetch_outcome(state=state, user_id=owner_uin, ok=ok, msg=msg, logger=logger):
            continue
        if not ok:
            result["failed"] = int(result.get("failed", 0) or 0) + 1
            result["last_error"] = msg
            logger.warning(f"[qzone_outbound] fetch failed for owner {owner_uin}: {msg}")
            continue

        feed_index = {str(feed.get("feed_key", "") or ""): feed for feed in feeds if isinstance(feed, dict)}
        for feed_key, info in owner_items:
            feed = feed_index.get(feed_key)
            if not feed:
                continue
            comments = _extract_qzone_comments(feed.get("raw") if isinstance(feed.get("raw"), dict) else {})
            last_seen_ts = float(info.get("last_seen_ts", 0) or 0)
            new_max_seen = last_seen_ts
            for comment in comments:
                commenter_id = str(comment.get("user_id", "") or "")
                if not commenter_id or commenter_id == bot_id:
                    continue
                created_at = float(comment.get("created_at", 0) or 0)
                if last_seen_ts and created_at and created_at <= last_seen_ts:
                    continue
                if commenter_id != owner_uin and commenter_id not in friend_profiles:
                    # 仅对原作者或自己好友的回应作出回复，避免给陌生人插嘴
                    if created_at > new_max_seen:
                        new_max_seen = created_at
                    continue
                comment_key = f"{feed_key}:{comment.get('comment_key') or commenter_id}:{int(created_at) or 'na'}"
                if comment_key in bot_replies_state:
                    continue
                result["inbound_comments"] = int(result.get("inbound_comments", 0) or 0) + 1
                profile = friend_profiles.get(commenter_id, {})
                persona_snippet = _get_persona_snippet(persona_store, commenter_id, persona_snippet_max_chars)
                commenter_profile = {
                    "nickname": str(profile.get("nickname", "") or comment.get("nickname") or commenter_id),
                    "persona_snippet": persona_snippet,
                    "last_interaction": float(
                        (proactive_state.get(commenter_id, {}) if isinstance(proactive_state.get(commenter_id), dict) else {})
                        .get("last_interaction", 0)
                        or 0
                    ),
                }
                emotion_memory = describe_user_emotion_memory(emotion_state or {}, commenter_id)
                previous_bot_reply = str(
                    info.get("last_bot_reply")
                    or info.get("bot_comment")
                    or ""
                ).strip()
                is_reply_to_bot = _comment_targets_user(comment, bot_id) or bool(previous_bot_reply)
                decision = await _decide_bot_comment_reply(
                    feed=feed,
                    comment=comment,
                    commenter_profile=commenter_profile,
                    system_prompt=system_prompt,
                    call_ai_api=call_ai_api,
                    inner_state=inner_state,
                    emotion_memory=emotion_memory,
                    allow_third_party_chime_in=allow_third_party_chime_in,
                    bot_id=bot_id,
                    is_reply_to_bot=is_reply_to_bot,
                    previous_bot_reply=previous_bot_reply,
                    recent_thread=_format_qzone_comment_thread(
                        comments,
                        comment,
                        bot_id=bot_id,
                    ),
                    interaction_rounds=int(info.get("reply_chain_count", 0) or 0),
                )
                if str(decision.get("action", "") or "") != "reply":
                    bot_replies_state[comment_key] = {
                        "at": time.time(),
                        "action": "ignore",
                        "reason": str(decision.get("reason", "") or "")[:80],
                    }
                    if created_at > new_max_seen:
                        new_max_seen = created_at
                    continue
                reply_text = str(decision.get("reply", "") or "").strip()
                if not reply_text:
                    bot_replies_state[comment_key] = {
                        "at": time.time(),
                        "action": "ignore",
                        "reason": "empty_reply",
                    }
                    if created_at > new_max_seen:
                        new_max_seen = created_at
                    continue
                ok_reply, reply_msg = await qzone_social_service.comment_feed(
                    feed=feed,
                    bot_id=bot_id,
                    content=reply_text,
                    reply_to_comment=comment,
                )
                if ok_reply:
                    result["replied"] = int(result.get("replied", 0) or 0) + 1
                    info["last_bot_reply"] = reply_text
                    info["last_reply_at"] = time.time()
                    info["reply_chain_count"] = int(info.get("reply_chain_count", 0) or 0) + 1
                    bot_replies_state[comment_key] = {
                        "at": time.time(),
                        "action": "reply",
                        "reply": reply_text,
                    }
                else:
                    result["failed"] = int(result.get("failed", 0) or 0) + 1
                    result["last_error"] = reply_msg
                    logger.warning(
                        f"[qzone_outbound] reply failed feed={feed_key} commenter={commenter_id}: {reply_msg}"
                    )
                if created_at > new_max_seen:
                    new_max_seen = created_at
            if new_max_seen and new_max_seen > last_seen_ts:
                info["last_seen_ts"] = new_max_seen


async def scan_qzone_social_feeds(
    *,
    bot: Any,
    plugin_config: Any,
    qzone_social_service: Any,
    load_prompt: Callable[[], Any],
    call_ai_api: Callable[..., Awaitable[Optional[str]]],
    load_proactive_state: Callable[[], dict[str, dict[str, Any]]],
    get_now: Callable[[], datetime],
    logger: Any,
    persona_store: Any = None,
    vision_caller: Any = None,
    agent_data_dir: Any = None,
    target_user_id: str = "",
    allow_open_user: bool = False,
) -> dict[str, Any]:
    if _SCAN_LOCK.locked():
        return {"ok": False, "skipped": True, "last_error": "qzone_social_scan_already_running"}
    async with _SCAN_LOCK:
        now = get_now()
        today = now.strftime("%Y-%m-%d")
        state = _normalize_state(now)
        result: dict[str, Any] = {
            "ok": True,
            "target_user_id": str(target_user_id or ""),
            "scanned_users": 0,
            "feeds_seen": 0,
            "liked": 0,
            "commented": 0,
            "ignored": 0,
            "failed": 0,
            "inbound_comments": 0,
            "replied": 0,
            "profile_records": 0,
            "last_error": "",
        }
        try:
            friend_profiles = await _get_friend_profiles(bot, logger)
            if not friend_profiles and not (target_user_id and allow_open_user):
                result["ok"] = False
                result["last_error"] = "no_friend_profiles"
                _save_state(state, result)
                return result

            proactive_state = load_proactive_state() or {}
            persona_snippet_max_chars = int(
                getattr(plugin_config, "personification_persona_snippet_max_chars", 150)
            )
            candidates = _collect_candidates(
                friend_profiles=friend_profiles,
                proactive_state=proactive_state,
                persona_store=persona_store,
                persona_snippet_max_chars=persona_snippet_max_chars,
                target_user_id=str(target_user_id or ""),
                allow_open_user=allow_open_user,
            )
            if not candidates:
                result["ok"] = False
                result["skipped"] = True
                result["last_error"] = "target_not_bot_friend" if target_user_id else "no_candidates"
                _save_state(state, result)
                return result

            inner_state = dict(DEFAULT_STATE)
            if agent_data_dir is not None:
                try:
                    inner_state.update(await load_inner_state(agent_data_dir))
                except Exception as exc:
                    logger.warning(f"[qzone_social] load inner_state failed: {exc}")
            emotion_state = {}
            if agent_data_dir is not None:
                try:
                    emotion_state = await load_emotion_state(agent_data_dir)
                except Exception as exc:
                    logger.warning(f"[qzone_social] load emotion_state failed: {exc}")

            system_prompt = _extract_system_prompt(load_prompt())
            allow_third_party_chime_in = bool(
                getattr(plugin_config, "personification_qzone_third_party_chime_in_enabled", True)
            )
            max_feeds = max(1, int(getattr(plugin_config, "personification_qzone_social_max_feeds_per_scan", 10)))
            like_limit = _normalize_limit(getattr(plugin_config, "personification_qzone_social_like_limit", 0))
            comment_limit = _normalize_limit(getattr(plugin_config, "personification_qzone_social_comment_limit", 0))
            per_friend_limit = _normalize_limit(
                getattr(plugin_config, "personification_qzone_social_per_friend_limit", 0)
            )

            processed_feeds = 0
            for candidate in candidates:
                if processed_feeds >= max_feeds:
                    break
                candidate_uid = str(candidate["user_id"])
                if _is_qzone_user_blocked(state, candidate_uid):
                    logger.debug(f"[qzone_social] skip permission-blocked user {candidate_uid}")
                    continue
                ok, msg, feeds = await qzone_social_service.fetch_user_feeds(
                    target_uin=candidate_uid,
                    bot_id=str(getattr(bot, "self_id", "") or ""),
                    count=max_feeds,
                    include_comments=False,
                )
                result["scanned_users"] += 1
                if _handle_qzone_fetch_outcome(state=state, user_id=candidate_uid, ok=ok, msg=msg, logger=logger):
                    continue
                if not ok:
                    result["failed"] += 1
                    result["last_error"] = msg
                    logger.warning(f"[qzone_social] fetch feeds failed for {candidate_uid}: {msg}")
                    continue
                for feed in feeds:
                    if processed_feeds >= max_feeds:
                        break
                    feed_key = str(feed.get("feed_key", "") or "")
                    if not feed_key or _feed_already_reacted(state, feed_key):
                        continue
                    processed_feeds += 1
                    result["feeds_seen"] += 1
                    _mark_seen(state, feed_key)
                    image_summary = await _summarize_images(
                        vision_caller=vision_caller,
                        images=list(feed.get("images") or []),
                        logger=logger,
                    )
                    await _record_qzone_profile_evidence(
                        persona_store=persona_store,
                        user_id=str(candidate["user_id"]),
                        evidence_key=f"feed:{feed_key}",
                        kind="动态",
                        content=str(feed.get("content", "") or ""),
                        image_summary=image_summary,
                        state=state,
                        result=result,
                        logger=logger,
                    )
                    emotion_memory = describe_user_emotion_memory(emotion_state or {}, str(candidate["user_id"]))
                    decision = await _decide_feed_action(
                        feed=feed,
                        candidate=candidate,
                        system_prompt=system_prompt,
                        call_ai_api=call_ai_api,
                        image_summary=image_summary,
                        inner_state=inner_state,
                        emotion_memory=emotion_memory,
                    )
                    friend_state = _daily_friend_state(state, str(candidate["user_id"]), today)
                    decision = _apply_action_limits(
                        decision=decision,
                        state=state,
                        friend_state=friend_state,
                        like_limit=like_limit,
                        comment_limit=comment_limit,
                        per_friend_limit=per_friend_limit,
                    )
                    action = str(decision.get("action", "") or "ignore")
                    if action == "ignore":
                        result["ignored"] += 1
                        continue

                    acted = False
                    comment_text = str(decision.get("comment", "") or "")
                    if action in {"like", "like_comment"}:
                        like_ok, like_msg = await qzone_social_service.like_feed(
                            feed=feed,
                            bot_id=str(getattr(bot, "self_id", "") or ""),
                        )
                        if like_ok:
                            acted = True
                            result["liked"] += 1
                            state["like_count"] = int(state.get("like_count", 0) or 0) + 1
                            friend_state["like_count"] = int(friend_state.get("like_count", 0) or 0) + 1
                            await _record_qzone_profile_evidence(
                                persona_store=persona_store,
                                user_id=str(candidate["user_id"]),
                                evidence_key=f"bot_like:{feed_key}",
                                kind="bot点赞",
                                content="（给 ta 这条动态点了赞）",
                                state=state,
                                result=result,
                                logger=logger,
                            )
                        else:
                            result["failed"] += 1
                            result["last_error"] = like_msg
                            logger.warning(f"[qzone_social] like failed for {feed_key}: {like_msg}")
                    if action in {"comment", "like_comment"} and comment_text:
                        comment_ok, comment_msg = await qzone_social_service.comment_feed(
                            feed=feed,
                            bot_id=str(getattr(bot, "self_id", "") or ""),
                            content=comment_text,
                        )
                        if comment_ok:
                            acted = True
                            result["commented"] += 1
                            state["comment_count"] = int(state.get("comment_count", 0) or 0) + 1
                            friend_state["comment_count"] = int(friend_state.get("comment_count", 0) or 0) + 1
                            outbound = state.setdefault("bot_outbound_comments", {})
                            if isinstance(outbound, dict):
                                outbound[feed_key] = {
                                    "target_uin": str(candidate["user_id"]),
                                    "bot_comment": comment_text,
                                    "last_bot_reply": comment_text,
                                    "recorded_at": time.time(),
                                    "last_seen_ts": time.time(),
                                    "reply_chain_count": 0,
                                }
                            await _record_qzone_profile_evidence(
                                persona_store=persona_store,
                                user_id=str(candidate["user_id"]),
                                evidence_key=f"bot_comment:{feed_key}",
                                kind="bot留言",
                                content=comment_text,
                                state=state,
                                result=result,
                                logger=logger,
                            )
                        else:
                            result["failed"] += 1
                            result["last_error"] = comment_msg
                            logger.warning(f"[qzone_social] comment failed for {feed_key}: {comment_msg}")
                    if acted:
                        friend_state["action_count"] = int(friend_state.get("action_count", 0) or 0) + 1
                        _mark_reacted(state, feed_key, action=action, comment=comment_text)
                    else:
                        result["ignored"] += 1
            if not target_user_id and friend_profiles:
                await _scan_bot_space_comments(
                    bot=bot,
                    qzone_social_service=qzone_social_service,
                    friend_profiles=friend_profiles,
                    proactive_state=proactive_state,
                    persona_store=persona_store,
                    persona_snippet_max_chars=persona_snippet_max_chars,
                    system_prompt=system_prompt,
                    call_ai_api=call_ai_api,
                    inner_state=inner_state,
                    emotion_state=emotion_state,
                    max_feeds=max_feeds,
                    allow_third_party_chime_in=allow_third_party_chime_in,
                    process_existing_comments=False,
                    state=state,
                    result=result,
                    logger=logger,
                )
            _save_state(state, result)
            return result
        except Exception as exc:
            result["ok"] = False
            result["last_error"] = str(exc)
            _save_state(state, result)
            logger.warning(f"[qzone_social] scan failed: {exc}")
            return result


async def scan_qzone_inbound_messages(
    *,
    bot: Any,
    plugin_config: Any,
    qzone_social_service: Any,
    load_prompt: Callable[[], Any],
    call_ai_api: Callable[..., Awaitable[Optional[str]]],
    load_proactive_state: Callable[[], dict[str, dict[str, Any]]],
    get_now: Callable[[], datetime],
    logger: Any,
    persona_store: Any = None,
    agent_data_dir: Any = None,
) -> dict[str, Any]:
    """Poll comments under the bot's own Qzone feeds and let the LLM decide replies."""
    if _SCAN_LOCK.locked():
        return {"ok": False, "skipped": True, "last_error": "qzone_scan_already_running"}
    async with _SCAN_LOCK:
        state = _normalize_state(get_now())
        result: dict[str, Any] = {
            "ok": True,
            "target_user_id": str(getattr(bot, "self_id", "") or ""),
            "scanned_users": 0,
            "feeds_seen": 0,
            "liked": 0,
            "commented": 0,
            "ignored": 0,
            "failed": 0,
            "inbound_comments": 0,
            "replied": 0,
            "profile_records": 0,
            "last_error": "",
        }
        try:
            friend_profiles = await _get_friend_profiles(bot, logger)

            proactive_state = load_proactive_state() or {}
            persona_snippet_max_chars = int(
                getattr(plugin_config, "personification_persona_snippet_max_chars", 150)
            )
            inner_state = dict(DEFAULT_STATE)
            emotion_state = {}
            if agent_data_dir is not None:
                try:
                    inner_state.update(await load_inner_state(agent_data_dir))
                except Exception as exc:
                    logger.warning(f"[qzone_inbound] load inner_state failed: {exc}")
                try:
                    emotion_state = await load_emotion_state(agent_data_dir)
                except Exception as exc:
                    logger.warning(f"[qzone_inbound] load emotion_state failed: {exc}")

            max_feeds = max(
                1,
                int(getattr(plugin_config, "personification_qzone_inbound_max_feeds_per_scan", 20) or 20),
            )
            max_comments_per_feed = max(
                1,
                int(getattr(plugin_config, "personification_qzone_inbound_max_comments_per_feed", 20) or 20),
            )
            system_prompt = _extract_system_prompt(load_prompt())
            allow_third_party_chime_in = bool(
                getattr(plugin_config, "personification_qzone_third_party_chime_in_enabled", True)
            )
            await _scan_bot_space_comments(
                bot=bot,
                qzone_social_service=qzone_social_service,
                friend_profiles=friend_profiles,
                proactive_state=proactive_state,
                persona_store=persona_store,
                persona_snippet_max_chars=persona_snippet_max_chars,
                system_prompt=system_prompt,
                call_ai_api=call_ai_api,
                inner_state=inner_state,
                emotion_state=emotion_state,
                max_feeds=max_feeds,
                max_comments_per_feed=max_comments_per_feed,
                count_checked_feeds=True,
                allow_third_party_chime_in=allow_third_party_chime_in,
                process_existing_comments=False,
                state=state,
                result=result,
                logger=logger,
            )
            if bool(getattr(plugin_config, "personification_qzone_outbound_reply_enabled", True)):
                now_ts = time.time()
                outbound_interval = getattr(plugin_config, "personification_qzone_outbound_reply_check_interval", 3)
                if _outbound_reply_scan_due(state, now_ts=now_ts, interval_minutes=outbound_interval):
                    try:
                        await _scan_bot_outbound_comment_replies(
                            bot=bot,
                            qzone_social_service=qzone_social_service,
                            plugin_config=plugin_config,
                            friend_profiles=friend_profiles,
                            proactive_state=proactive_state,
                            persona_store=persona_store,
                            persona_snippet_max_chars=persona_snippet_max_chars,
                            system_prompt=system_prompt,
                            call_ai_api=call_ai_api,
                            inner_state=inner_state,
                            emotion_state=emotion_state,
                            state=state,
                            result=result,
                            logger=logger,
                            allow_third_party_chime_in=allow_third_party_chime_in,
                        )
                    except Exception as exc:
                        result["failed"] = int(result.get("failed", 0) or 0) + 1
                        result["last_error"] = f"outbound scan failed: {exc}"
                        logger.warning(f"[qzone_outbound] scan failed: {exc}")
                    finally:
                        _mark_outbound_reply_scan(state, now_ts=time.time())
            result["feeds_seen"] = max(0, int(result.get("feeds_seen", 0) or 0))
            _save_inbound_state(state, result)
            return result
        except Exception as exc:
            result["ok"] = False
            result["last_error"] = str(exc)
            _save_inbound_state(state, result)
            logger.warning(f"[qzone_inbound] poll failed: {exc}")
            return result
