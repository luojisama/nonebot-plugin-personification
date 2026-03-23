from typing import Any, Callable, Dict, Iterable


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

    logger.info("拟人插件：正在自动发布周记说说...")
    success, msg = await publish_qzone_shuo(diary_content, bot.self_id)
    if success:
        logger.info("拟人插件：每周说说发布成功！")
        return True

    logger.error(f"拟人插件：每周说说发布失败：{msg}")
    return False
