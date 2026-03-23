import asyncio
import random
from typing import Any, Awaitable, Callable, Optional


async def handle_background_style_analysis(
    *,
    group_id: str,
    analyze_group_style: Callable[[str], Awaitable[Optional[str]]],
    set_group_style: Callable[[str, str], None],
    clear_group_msgs: Callable[[str], None],
    logger: Any,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    random_uniform: Callable[[float, float], float] = random.uniform,
) -> None:
    """后台执行群风格分析。"""
    try:
        await sleep(random_uniform(1, 5))
        style_desc = await analyze_group_style(group_id)
        if style_desc:
            set_group_style(group_id, style_desc)
            clear_group_msgs(group_id)
            logger.info(f"拟人插件：群 {group_id} 风格自动学习完成并已清空记录。")
    except Exception as e:
        logger.error(f"拟人插件：自动学习群 {group_id} 风格失败: {e}")


async def handle_manual_diary_command(
    matcher: Any,
    *,
    bot: Any,
    qzone_publish_available: bool,
    update_qzone_cookie: Callable[[Any], Awaitable[tuple[bool, str]]],
    generate_ai_diary: Callable[[Any], Awaitable[str]],
    publish_qzone_shuo: Callable[[str, str], Awaitable[tuple[bool, str]]],
) -> None:
    """处理手动发说说命令。"""
    if not qzone_publish_available:
        await matcher.finish("当前未启用空间说说发布能力。")

    await matcher.send("正在刷新空间 Cookie、生成 AI 周记并发布，请稍候...")
    cookie_ok, cookie_msg = await update_qzone_cookie(bot)
    if not cookie_ok:
        await matcher.send(f"空间 Cookie 自动更新失败，继续尝试使用现有 Cookie 发布：{cookie_msg}")
    diary_content = await generate_ai_diary(bot)
    if not diary_content:
        await matcher.finish("AI 生成周记失败，请检查网络 or API 配置。")

    success, msg = await publish_qzone_shuo(diary_content, bot.self_id)
    if success:
        await matcher.finish(f"✅ AI 说说发布成功！\n\n内容：\n{diary_content}")
    await matcher.finish(f"❌ {msg}")


async def handle_learn_style_command(
    matcher: Any,
    *,
    group_id: str,
    get_recent_group_msgs: Callable[[str, int], list[dict]],
    analyze_group_style: Callable[[str], Awaitable[Optional[str]]],
    set_group_style: Callable[[str, str], None],
    clear_group_msgs: Callable[[str], None],
    logger: Any,
    finished_exception_cls: Optional[type[BaseException]] = None,
) -> None:
    """处理学习群聊风格命令。"""
    msgs = get_recent_group_msgs(group_id, limit=300)
    if len(msgs) < 10:
        await matcher.finish("当前群聊记录太少啦，多聊一会儿再来学习吧！(至少需要 10 条)")

    await matcher.send("正在分析最近 300 条群聊记录，请稍候...")
    try:
        style_desc = await analyze_group_style(group_id)
        if style_desc:
            set_group_style(group_id, style_desc)
            clear_group_msgs(group_id)
            await matcher.finish(f"✅ 群聊风格学习完成并已重置记录！\n\n{style_desc}\n\n已应用到本群的拟人回复中。")
        await matcher.finish("分析失败，AI 未返回有效内容。")
    except Exception as e:
        if finished_exception_cls and isinstance(e, finished_exception_cls):
            raise
        logger.error(f"学习群聊风格失败: {e}")
        await matcher.finish(f"学习失败: {e}")


async def handle_view_style_command(
    matcher: Any,
    *,
    args_text: str,
    event_group_id: Optional[str],
    get_group_style: Callable[[str], str],
) -> None:
    """处理查看群聊风格命令。"""
    if args_text and args_text.isdigit():
        target_id = args_text
    elif event_group_id:
        target_id = event_group_id
    else:
        await matcher.finish("请在群聊中使用，或指定群号：查看群聊风格 [群号]")

    style = get_group_style(target_id)
    if style:
        await matcher.finish(f"📊 群 {target_id} 的聊天风格：\n\n{style}")
    await matcher.finish(f"群 {target_id} 还没有学习过聊天风格哦！\n请管理员在群内发送 '学习群聊风格'。")
