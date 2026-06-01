import re
import time
from typing import Any, Callable, Dict, Iterable

from ..core.data_store import get_data_store


_IMAGE_B64_RE = re.compile(r"\[IMAGE_B64\][A-Za-z0-9+/=\r\n]+\[/IMAGE_B64\]")


def _compact_qzone_state_content(content: str) -> str:
    return _IMAGE_B64_RE.sub("[配图]", str(content or "")).strip()[:200]


def _remember_qzone_post(state: dict[str, Any], content: str, *, max_items: int = 12) -> None:
    compact = _compact_qzone_state_content(content)
    if not compact:
        return
    recent_raw = state.get("recent_contents")
    recent = list(recent_raw) if isinstance(recent_raw, list) else []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in recent + [compact]:
        text = str(item.get("content") if isinstance(item, dict) else item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    state["recent_contents"] = normalized[-max(1, int(max_items)) :]
    state["last_content"] = compact


async def run_daily_group_fav_report(
    *,
    sign_in_available: bool,
    load_data: Callable[[], Dict[str, Dict[str, Any]]],
    get_now: Callable[[], Any],
    get_bots: Callable[[], Dict[str, Any]],
    superusers: Iterable[str],
    logger: Any,
) -> int:
    """执行每日群好感统计并私聊发送给超级用户。"""
    if not sign_in_available:
        return 0

    try:
        data = load_data()
        today = get_now().strftime("%Y-%m-%d")

        report_lines = []
        total_increase = 0.0

        for user_id, user_data in data.items():
            if not user_id.startswith("group_") or user_id.startswith("group_private_"):
                continue
            if user_data.get("last_update") != today:
                continue

            daily_count = float(user_data.get("daily_fav_count", 0.0))
            if daily_count <= 0:
                continue

            group_id = user_id.replace("group_", "")
            current_fav = float(user_data.get("favorability", 0.0))
            group_name = "未知群聊"

            try:
                bots = get_bots()
                for bot in bots.values():
                    try:
                        group_info = await bot.get_group_info(group_id=int(group_id))
                        group_name = group_info.get("group_name", "未知群聊")
                        break
                    except Exception:
                        continue
            except Exception:
                pass

            report_lines.append(f"群 {group_name}({group_id}): +{daily_count:.2f} (当前: {current_fav:.2f})")
            total_increase += daily_count

        if not report_lines:
            return 0

        summary = (
            f"📊 【每日群聊好感度统计】\n"
            f"日期: {today}\n"
            f"总增长: {total_increase:.2f}\n\n"
            + "\n".join(report_lines)
        )

        bots = get_bots()
        for bot in bots.values():
            for su in superusers:
                try:
                    await bot.send_private_msg(user_id=int(su), message=summary)
                except Exception as e:
                    logger.error(f"发送好感度统计给 {su} 失败: {e}")

        logger.info(f"已发送每日群聊好感度统计，共 {len(report_lines)} 个群聊有变化")
        return len(report_lines)
    except Exception as e:
        logger.error(f"执行每日好感度统计任务出错: {e}")
        return 0


async def run_auto_post_diary(
    *,
    qzone_publish_available: bool,
    get_bots: Callable[[], Dict[str, Any]],
    update_qzone_cookie: Callable[..., Any],
    generate_ai_diary: Callable[..., Any],
    publish_qzone_shuo: Callable[..., Any],
    logger: Any,
) -> bool:
    """执行一次自动说说发布。"""
    if not qzone_publish_available:
        logger.warning("拟人插件：当前未启用空间说说发布能力，无法自动发送说说。")
        return False

    bots = get_bots()
    if not bots:
        logger.warning("拟人插件：未找到有效的 Bot 实例，跳过自动说说发布。")
        return False

    bot = list(bots.values())[0]

    logger.info("拟人插件：正在自动更新 Qzone Cookie...")
    try:
        cookie_ok, cookie_msg = await update_qzone_cookie(bot)
    except Exception as e:
        logger.warning(f"拟人插件：Qzone Cookie 更新失败（{e}），将尝试使用旧 Cookie 继续发布。")
    else:
        if cookie_ok:
            logger.info("拟人插件：Qzone Cookie 更新成功。")
        else:
            logger.warning(f"拟人插件：Qzone Cookie 更新失败（{cookie_msg}），将尝试使用旧 Cookie 继续发布。")

    diary_content = await generate_ai_diary(bot)
    if not diary_content:
        return False

    logger.info("拟人插件：正在自动发布空间说说...")
    success, msg = await publish_qzone_shuo(diary_content, bot.self_id)
    if success:
        store = get_data_store()
        state = store.load_sync("qzone_post_state")
        if not isinstance(state, dict):
            state = {}
        _remember_qzone_post(state, diary_content)
        state["last_post_at"] = time.time()
        store.save_sync("qzone_post_state", state)
        logger.info("拟人插件：空间说说发布成功！")
        return True

    logger.error(f"拟人插件：空间说说发布失败：{msg}")
    return False


def _in_qzone_quiet_hour(now_dt: Any, start: int, end: int) -> bool:
    """判断当前小时是否落在 [start, end) 内（支持跨午夜窗口）。
    start==end → 始终返回 False（无静默期）。
    """
    try:
        hour = int(now_dt.hour)
    except Exception:
        return False
    start = max(0, min(23, int(start)))
    end = max(0, min(24, int(end)))
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    # 跨午夜（如 22 → 7）
    return hour >= start or hour < end


async def run_proactive_qzone_post(
    *,
    qzone_publish_available: bool,
    qzone_proactive_enabled: bool,
    qzone_probability: float,
    qzone_daily_limit: int,
    qzone_min_interval_hours: float,
    get_bots: Callable[[], Dict[str, Any]],
    get_now: Callable[[], Any],
    update_qzone_cookie: Callable[..., Any],
    maybe_generate_qzone_post: Callable[[Any], Any],
    publish_qzone_shuo: Callable[..., Any],
    logger: Any,
    quiet_hour_start: int = 0,
    quiet_hour_end: int = 7,
) -> bool:
    """按内心状态和近期聊天判断，是否主动发一条更日常的空间动态。
    在 [quiet_hour_start, quiet_hour_end) 时间窗口内不触发（避免半夜打扰）。
    """
    if not qzone_publish_available or not qzone_proactive_enabled:
        return False
    _ = qzone_probability  # 兼容旧配置；是否发布交给 LLM 决定，仍受每日上限与最小间隔限制。
    bots = get_bots()
    if not bots:
        return False
    bot = list(bots.values())[0]
    now = get_now()
    # 半夜避开：在 quiet_hour 窗口内直接 skip（不消耗 daily quota）
    if _in_qzone_quiet_hour(now, quiet_hour_start, quiet_hour_end):
        logger.debug(
            f"[qzone] skip proactive post in quiet hour {quiet_hour_start}-{quiet_hour_end}"
        )
        return False
    today = now.strftime("%Y-%m-%d")
    now_ts = time.time()

    store = get_data_store()
    state = store.load_sync("qzone_post_state")
    if not isinstance(state, dict):
        state = {}
    if state.get("date") != today:
        state = {
            "date": today,
            "count": 0,
            "last_post_at": float(state.get("last_post_at", 0) or 0),
            "last_content": str(state.get("last_content", "") or ""),
            "recent_contents": list(state.get("recent_contents", []))
            if isinstance(state.get("recent_contents"), list)
            else [],
        }

    if int(state.get("count", 0) or 0) >= max(1, int(qzone_daily_limit)):
        return False
    min_interval_seconds = max(0.0, float(qzone_min_interval_hours)) * 3600
    last_post_at = float(state.get("last_post_at", 0) or 0)
    if min_interval_seconds and last_post_at and now_ts - last_post_at < min_interval_seconds:
        return False

    try:
        cookie_ok, cookie_msg = await update_qzone_cookie(bot)
    except Exception as e:
        logger.warning(f"拟人插件：主动说说刷新 Cookie 失败（{e}），尝试使用旧 Cookie。")
    else:
        if not cookie_ok:
            logger.warning(f"拟人插件：主动说说刷新 Cookie 失败（{cookie_msg}），尝试使用旧 Cookie。")

    content = await maybe_generate_qzone_post(bot)
    if not content:
        return False

    success, msg = await publish_qzone_shuo(content, bot.self_id)
    if not success:
        logger.error(f"拟人插件：主动说说发布失败：{msg}")
        return False

    state["date"] = today
    state["count"] = int(state.get("count", 0) or 0) + 1
    state["last_post_at"] = now_ts
    _remember_qzone_post(state, content)
    store.save_sync("qzone_post_state", state)
    logger.info("拟人插件：已根据当前状态主动发布一条空间说说。")
    return True


async def run_qzone_social_scan(
    *,
    qzone_publish_available: bool,
    qzone_social_enabled: bool,
    get_bots: Callable[[], Dict[str, Any]],
    update_qzone_cookie: Callable[..., Any],
    scan_qzone_social_feeds: Callable[..., Any],
    logger: Any,
    target_user_id: str = "",
    force: bool = False,
) -> dict[str, Any]:
    """Scan friend Qzone feeds and let the LLM decide lightweight interactions."""
    if not qzone_publish_available or (not qzone_social_enabled and not force):
        return {"ok": False, "skipped": True, "last_error": "qzone_social_disabled"}
    bots = get_bots()
    if not bots:
        return {"ok": False, "skipped": True, "last_error": "no_bot"}
    bot = list(bots.values())[0]
    try:
        cookie_ok, cookie_msg = await update_qzone_cookie(bot)
    except Exception as exc:
        logger.warning(f"拟人插件：空间互动刷新 Cookie 失败（{exc}），尝试使用旧 Cookie。")
    else:
        if not cookie_ok:
            logger.warning(f"拟人插件：空间互动刷新 Cookie 失败（{cookie_msg}），尝试使用旧 Cookie。")
    result = await scan_qzone_social_feeds(
        bot,
        target_user_id=str(target_user_id or ""),
        allow_open_user=bool(force),
    )
    if result.get("ok"):
        logger.info(
            "拟人插件：空间互动扫描完成，"
            f"用户 {result.get('scanned_users', 0)}，动态 {result.get('feeds_seen', 0)}，"
            f"点赞 {result.get('liked', 0)}，评论 {result.get('commented', 0)}。"
        )
    else:
        logger.warning(f"拟人插件：空间互动扫描跳过或失败：{result.get('last_error')}")
    return result


async def run_qzone_inbound_poll(
    *,
    qzone_publish_available: bool,
    qzone_inbound_enabled: bool,
    get_bots: Callable[[], Dict[str, Any]],
    update_qzone_cookie: Callable[..., Any],
    poll_qzone_inbound_messages: Callable[[Any], Any],
    logger: Any,
    force: bool = False,
) -> dict[str, Any]:
    """Poll comments under the bot's own Qzone feeds for near-realtime replies."""
    if not qzone_publish_available or (not qzone_inbound_enabled and not force):
        return {"ok": False, "skipped": True, "last_error": "qzone_inbound_disabled"}
    bots = get_bots()
    if not bots:
        return {"ok": False, "skipped": True, "last_error": "no_bot"}
    bot = list(bots.values())[0]
    try:
        cookie_ok, cookie_msg = await update_qzone_cookie(bot)
    except Exception as exc:
        logger.warning(f"拟人插件：空间消息轮询刷新 Cookie 失败（{exc}），尝试使用旧 Cookie。")
    else:
        if not cookie_ok:
            logger.warning(f"拟人插件：空间消息轮询刷新 Cookie 失败（{cookie_msg}），尝试使用旧 Cookie。")
    result = await poll_qzone_inbound_messages(bot)
    if result.get("ok"):
        logger.info(
            "拟人插件：空间消息轮询完成，"
            f"说说 {result.get('feeds_seen', 0)}，留言 {result.get('inbound_comments', 0)}，"
            f"回复 {result.get('replied', 0)}。"
        )
    else:
        logger.warning(f"拟人插件：空间消息轮询跳过或失败：{result.get('last_error')}")
    return result
