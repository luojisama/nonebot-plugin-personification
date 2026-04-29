import re
import time
from typing import Any, Callable, Dict, Iterable

from ..core.data_store import get_data_store


_IMAGE_B64_RE = re.compile(r"\[IMAGE_B64\][A-Za-z0-9+/=\r\n]+\[/IMAGE_B64\]")


def _compact_qzone_state_content(content: str) -> str:
    return _IMAGE_B64_RE.sub("[配图]", str(content or "")).strip()[:200]


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
        logger.info("拟人插件：空间说说发布成功！")
        return True

    logger.error(f"拟人插件：空间说说发布失败：{msg}")
    return False


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
) -> bool:
    """按内心状态和近期聊天判断，是否主动发一条更日常的空间动态。"""
    if not qzone_publish_available or not qzone_proactive_enabled:
        return False
    _ = qzone_probability  # 兼容旧配置；是否发布交给 LLM 决定，仍受每日上限与最小间隔限制。
    bots = get_bots()
    if not bots:
        return False
    bot = list(bots.values())[0]
    now = get_now()
    today = now.strftime("%Y-%m-%d")
    now_ts = time.time()

    store = get_data_store()
    state = store.load_sync("qzone_post_state")
    if not isinstance(state, dict):
        state = {}
    if state.get("date") != today:
        state = {"date": today, "count": 0, "last_post_at": float(state.get("last_post_at", 0) or 0)}

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
    state["last_content"] = _compact_qzone_state_content(content)
    store.save_sync("qzone_post_state", state)
    logger.info("拟人插件：已根据当前状态主动发布一条空间说说。")
    return True
