import random
import time
from typing import Dict, List
from pathlib import Path
from nonebot import on_message, on_command, get_plugin_config, logger, get_driver
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment, MessageEvent, PokeNotifyEvent, Event
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule
from nonebot.exception import FinishedException
from openai import AsyncOpenAI

from .config import Config

# 尝试导入 htmlrender
try:
    from nonebot_plugin_htmlrender import md_to_pic
except ImportError:
    md_to_pic = None

# 尝试导入签到插件的工具函数
try:
    try:
        from nonebot_plugin_sign_in.utils import get_user_data, update_user_data
        from nonebot_plugin_sign_in.config import get_level_name
    except ImportError:
        try:
            from plugin.sign_in.utils import get_user_data, update_user_data
            from plugin.sign_in.config import get_level_name
        except ImportError:
            from ..sign_in.utils import get_user_data, update_user_data
            from ..sign_in.config import get_level_name
    SIGN_IN_AVAILABLE = True
except ImportError:
    SIGN_IN_AVAILABLE = False

if SIGN_IN_AVAILABLE:
    logger.info("拟人插件：已成功关联签到插件，好感度系统已激活。")
else:
    logger.warning("拟人插件：未找到签到插件，好感度系统将以默认值运行。")

__plugin_meta__ = PluginMetadata(
    name="群聊拟人",
    description="实现拟人化的群聊回复，支持好感度系统和自主回复决策",
    usage="在白名单群聊中根据概率随机回复，支持根据好感度改变态度",
    type="application",
    homepage="https://github.com/luojisama/nonebot-plugin-personification",
    config=Config,
    supported_adapters={"~onebot.v11"},
)

plugin_config = get_plugin_config(Config)
superusers = get_driver().config.superusers

def load_prompt() -> str:
    """加载提示词，支持从路径或直接字符串，兼容 Windows/Linux"""
    # 1. 优先检查专门的路径配置项
    target_path = plugin_config.personification_prompt_path or plugin_config.personification_system_path
    if target_path:
        # 处理可能的双引号和转义字符
        raw_path = target_path.strip('"').strip("'")
        # 尝试使用原始路径，如果不存在则尝试正斜杠替换
        path = Path(raw_path).expanduser()
        if not path.is_file():
            path = Path(raw_path.replace("\\", "/")).expanduser()
            
        if path.is_file():
            try:
                content = path.read_text(encoding="utf-8").strip()
                logger.info(f"拟人插件：成功从文件加载人格设定: {path.absolute()}")
                return content
            except Exception as e:
                logger.error(f"加载路径提示词失败 ({path}): {e}")
        else:
            logger.warning(f"拟人插件：路径文件不存在，请检查 .env.prod 配置。尝试路径: {raw_path}")

    # 2. 检查 system_prompt 本身是否是一个存在的路径
    content = plugin_config.personification_system_prompt
    if content and len(content) < 260:
        try:
            raw_path = content.strip('"').strip("'")
            path = Path(raw_path).expanduser()
            if not path.is_file():
                path = Path(raw_path.replace("\\", "/")).expanduser()
                
            if path.is_file():
                file_content = path.read_text(encoding="utf-8").strip()
                logger.info(f"拟人插件：成功从 system_prompt 路径加载人格设定: {path.absolute()}")
                return file_content
        except Exception:
            pass

    return content

# 存储各群聊天记录，用于上下文
chat_histories: Dict[int, List[Dict[str, str]]] = {}
# 存储拉黑的用户及其解封时间戳
user_blacklist: Dict[str, float] = {}

async def personification_rule(event: GroupMessageEvent) -> bool:
    group_id = str(event.group_id)
    user_id = str(event.user_id)
    
    # 检查是否在白名单中
    if group_id not in plugin_config.personification_whitelist:
        return False
    
    # 检查是否在永久黑名单中
    if SIGN_IN_AVAILABLE:
        user_data = get_user_data(user_id)
        if user_data.get("is_perm_blacklisted", False):
            return False

    # 检查是否在临时黑名单中
    if user_id in user_blacklist:
        if time.time() < user_blacklist[user_id]:
            return False
        else:
            # 时间到了，从黑名单移除
            del user_blacklist[user_id]
            logger.info(f"用户 {user_id} 的拉黑时间已到，已自动恢复。")

    # 如果是艾特机器人，则必定触发
    if event.to_me:
        return True
        
    # 根据概率决定是否触发
    return random.random() < plugin_config.personification_probability

# 注册消息处理器，优先级设为 100，不阻断其他插件
reply_matcher = on_message(rule=Rule(personification_rule), priority=100, block=False)

# 注册表情包水群处理器
async def sticker_chat_rule(event: GroupMessageEvent) -> bool:
    group_id = str(event.group_id)
    if group_id not in plugin_config.personification_whitelist:
        return False
    # 概率与随机回复一致
    return random.random() < plugin_config.personification_probability

sticker_chat_matcher = on_message(rule=Rule(sticker_chat_rule), priority=101, block=False)

@sticker_chat_matcher.handle()
async def _(bot: Bot, event: GroupMessageEvent):
    # 只有当文件夹中有表情包时才触发
    sticker_dir = Path(plugin_config.personification_sticker_path)
    if sticker_dir.exists() and sticker_dir.is_dir():
        stickers = [f for f in sticker_dir.iterdir() if f.suffix.lower() in [".jpg", ".png", ".gif", ".webp", ".jpeg"]]
        if stickers:
            random_sticker = random.choice(stickers)
            logger.info(f"拟人插件：触发随机水群表情包 {random_sticker.name}")
            # 使用绝对路径并转换为 file:// 协议，以确保在 Linux/Windows 上都有更好的兼容性
            await sticker_chat_matcher.finish(MessageSegment.image(f"file:///{random_sticker.absolute()}"))

# 注册戳一戳处理器
async def poke_rule(event: PokeNotifyEvent) -> bool:
    if event.target_id != event.self_id:
        return False
    group_id = str(event.group_id)
    if group_id not in plugin_config.personification_whitelist:
        return False
    # 使用配置的概率响应
    return random.random() < plugin_config.personification_poke_probability

poke_matcher = on_message(rule=Rule(poke_rule), priority=100, block=False)
# 注意：v11 的戳一戳通常是 Notify 事件，但在一些实现中可能作为消息
from nonebot import on_notice

async def poke_notice_rule(event: PokeNotifyEvent) -> bool:
    if event.target_id != event.self_id:
        return False
    group_id = str(event.group_id)
    if group_id not in plugin_config.personification_whitelist:
        return False
    # 使用配置的概率响应
    return random.random() < plugin_config.personification_poke_probability

poke_notice_matcher = on_notice(rule=Rule(poke_notice_rule), priority=10, block=False)

@reply_matcher.handle()
@poke_notice_matcher.handle()
async def handle_reply(bot: Bot, event: Event):
    # 如果是通知事件，需要特殊处理
    is_poke = False
    user_id = ""
    group_id = 0
    message_content = ""
    sender_name = ""

    if isinstance(event, PokeNotifyEvent):
        is_poke = True
        user_id = str(event.user_id)
        group_id = event.group_id
        message_content = "[你被对方戳了戳，你感到有点疑惑和好奇，想知道对方要做什么]"
        sender_name = "戳戳怪"
    elif isinstance(event, GroupMessageEvent):
        group_id = event.group_id
        user_id = str(event.user_id)
        message_content = event.get_plaintext().strip()
        sender_name = event.sender.card or event.sender.nickname or user_id
    else:
        return

    # 如果没配置 API KEY，直接跳过
    if not plugin_config.personification_api_key:
        return

    user_name = sender_name
    
    if not message_content and not is_poke:
        return

    # 1. 获取好感度与态度
    attitude_desc = "态度普通，像平常一样交流。"
    level_name = "未知"
    group_favorability = 100.0
    group_level = "普通"
    group_attitude = ""
    
    if SIGN_IN_AVAILABLE:
        try:
            # 获取个人好感度
            user_data = get_user_data(user_id)
            favorability = user_data.get("favorability", 0.0)
            level_name = get_level_name(favorability)
            attitude_desc = plugin_config.personification_favorability_attitudes.get(level_name, attitude_desc)
            
            # 获取群聊好感度
            group_key = f"group_{group_id}"
            group_data = get_user_data(group_key)
            group_favorability = group_data.get("favorability", 100.0)
            group_level = get_level_name(group_favorability)
            group_attitude = plugin_config.personification_favorability_attitudes.get(group_level, "")
        except Exception as e:
            logger.error(f"获取好感度数据失败: {e}")

    # 2. 维护聊天历史上下文
    if group_id not in chat_histories:
        chat_histories[group_id] = []
    
    chat_histories[group_id].append({"role": "user", "content": f"{user_name}: {message_content}"})
    # 限制上下文长度
    if len(chat_histories[group_id]) > plugin_config.personification_history_len:
        chat_histories[group_id] = chat_histories[group_id][-plugin_config.personification_history_len:]

    # 3. 构建 Prompt
    base_prompt = load_prompt()
    
    # 整合态度：结合个人和群聊的整体氛围
    combined_attitude = f"你对该用户的个人态度是：{attitude_desc}"
    if group_attitude:
        combined_attitude += f"\n当前群聊整体氛围带给你的感受是：{group_attitude} (基于群好感度 {group_favorability:.2f})"
    
    system_prompt = (
        f"你的身份核心设定如下：\n"
        f"\"\"\"\n{base_prompt}\n\"\"\"\n\n"
        f"当前对话背景：\n"
        f"- 对方昵称：{user_name}\n"
        f"- 对方个人好感等级：{level_name}\n"
        f"- 群聊整体好感等级：{group_level}\n"
        f"- 你的当前综合心理状态：\n{combined_attitude}\n\n"
        "【回复要求】\n"
        "1. 必须完全符合你的『身份核心设定』，包括语气、称呼和专业背景。\n"
        "2. 根据『综合心理状态』调整回复。即使好感度较低，也请保持基本的友善和礼貌。随着好感度提升，你可以表现得更加热情和主动。\n"
        "3. **关键指令（极其重要）**：\n"
        "   - 如果你觉得当前对话氛围很好，或者对方说话让你很开心，请在回复末尾添加标记 [氛围好]。\n"
        "   - **关于 [NO_REPLY] 标记的使用规则**：\n"
        "     - **严禁**因个人心情不好、讨厌对方或简单的意见不合而使用该标记。\n"
        "     - **仅当**对方发送了**严重的恶意人身攻击、极端侮辱性言论、或包含违规色情内容**时，才允许输出 [NO_REPLY]。\n"
        "     - 触发后该用户将被拉黑，请务必审慎判定，不要滥用权力。\n"
        "4. 回复必须精简，禁止废话。"
    )

    # 4. 构建消息历史
    messages = [
         {"role": "system", "content": f"{system_prompt}\n\n当前表情包库中已加载表情包，你可以根据氛围决定是否发送，但请勿在回复中直接输出任何文件名。"}
     ]
    messages.extend(chat_histories[group_id])

    # 4. 调用 AI API
    try:
        client = AsyncOpenAI(
            api_key=plugin_config.personification_api_key,
            base_url=plugin_config.personification_api_url
        )
        
        response = await client.chat.completions.create(
            model=plugin_config.personification_model,
            messages=messages,
            timeout=30
        )
        
        reply_content = response.choices[0].message.content.strip()
        
        # 移除 AI 回复中可能包含的 [表情:xxx] 标签
        import re
        reply_content = re.sub(r'\[表情:[^\]]+\]', '', reply_content).strip()
        # 移除末尾可能残留的表情包文件名（通常是 32 位 MD5 乱码）
        reply_content = re.sub(r'\s*[a-fA-F0-9]{32}$', '', reply_content).strip()
        
        # 5. 处理 AI 的回复决策
        if "[NO_REPLY]" in reply_content:
            duration = plugin_config.personification_blacklist_duration
            user_blacklist[user_id] = time.time() + duration
            logger.info(f"AI 决定不回复群 {group_id} 中 {user_name}({user_id}) 的消息，将其拉黑 {duration} 秒")
            
            # 扣除个人及群聊好感度
            penalty_desc = ""
            if SIGN_IN_AVAILABLE:
                try:
                    # 个人扣除
                    penalty = round(random.uniform(0, 0.3), 2)
                    user_data = get_user_data(user_id)
                    current_fav = user_data.get("favorability", 0.0)
                    new_fav = max(0.0, current_fav - penalty)
                    
                    # 增加拉黑次数统计
                    current_blacklist_count = user_data.get("blacklist_count", 0) + 1
                    is_perm = False
                    if current_blacklist_count >= 25:
                        is_perm = True
                    
                    update_user_data(user_id, favorability=new_fav, blacklist_count=current_blacklist_count, is_perm_blacklisted=is_perm)
                    
                    # 群聊扣除: 扣多 (0.5)
                    group_key = f"group_{group_id}"
                    group_data = get_user_data(group_key)
                    g_current_fav = group_data.get("favorability", 100.0)
                    g_new_fav = max(0.0, g_current_fav - 0.5)
                    update_user_data(group_key, favorability=g_new_fav)
                    
                    penalty_desc = f"\n个人好感度：-{penalty} (当前：{new_fav})\n群聊好感度：-0.50 (当前：{g_new_fav:.2f})\n累计拉黑次数：{current_blacklist_count}/25"
                    if is_perm:
                        penalty_desc += "\n⚠️ 该用户已触发 25 次拉黑，已自动加入永久黑名单。"
                    
                    logger.info(f"用户 {user_id} 拉黑，累计 {current_blacklist_count} 次。扣除个人 {penalty}，扣除群 {group_id} 0.5 好感度")
                except Exception as e:
                    logger.error(f"扣除好感度或更新黑名单失败: {e}")

            # 通知管理员
            for admin_id in superusers:
                try:
                    await bot.send_private_msg(
                        user_id=int(admin_id),
                        message=f"【群好感变动】\n群：{group_id}\n用户：{user_name}({user_id})\n事件：AI 触发拉黑 ⛔\n变动：-0.50 (群好感)\n原因：AI 决定不予回复\n{penalty_desc.strip()}"
                    )
                except Exception as e:
                    logger.error(f"发送拉黑通知给管理员 {admin_id} 失败: {e}")
            return

        # 6. 处理氛围加分逻辑 [氛围好]
        has_good_atmosphere = "[氛围好]" in reply_content
        if has_good_atmosphere:
            reply_content = reply_content.replace("[氛围好]", "").strip()
            if SIGN_IN_AVAILABLE:
                try:
                    group_key = f"group_{group_id}"
                    group_data = get_user_data(group_key)
                    
                    today = time.strftime("%Y-%m-%d")
                    last_update = group_data.get("last_update", "")
                    daily_count = group_data.get("daily_fav_count", 0.0)
                    
                    # 跨天重置上限
                    if last_update != today:
                        daily_count = 0.0
                    
                    if daily_count < 10.0:
                        g_current_fav = group_data.get("favorability", 100.0)
                        g_new_fav = g_current_fav + 0.1
                        daily_count += 0.1
                        update_user_data(group_key, favorability=g_new_fav, daily_fav_count=daily_count, last_update=today)
                        logger.info(f"AI 觉得群 {group_id} 氛围良好，好感度 +0.1 (今日已加: {daily_count}/10)")
                        
                        # 通知管理员
                        for admin_id in superusers:
                            try:
                                await bot.send_private_msg(
                                    user_id=int(admin_id),
                                    message=f"【群好感变动】\n群：{group_id}\n事件：AI 觉得氛围良好 ✨\n变动：+0.1\n当前好感：{g_new_fav:.2f}\n今日进度：{daily_count}/10"
                                )
                            except Exception as e:
                                logger.error(f"发送好感增加通知失败: {e}")
                except Exception as e:
                    logger.error(f"增加群聊好感度失败: {e}")

        # 7. 决定是否发送表情包
        sticker_segment = None
        sticker_name = ""
        if random.random() < plugin_config.personification_sticker_probability:
            sticker_dir = Path(plugin_config.personification_sticker_path)
            if sticker_dir.exists() and sticker_dir.is_dir():
                stickers = [f for f in sticker_dir.iterdir() if f.suffix.lower() in [".jpg", ".png", ".gif", ".webp", ".jpeg"]]
                if stickers:
                    random_sticker = random.choice(stickers)
                    sticker_name = random_sticker.stem  # 获取文件名作为表情包描述
                    # 使用绝对路径并转换为 file:// 协议，以确保在 Linux/Windows 上都有更好的兼容性
                    sticker_segment = MessageSegment.image(f"file:///{random_sticker.absolute()}")
                    logger.info(f"拟人插件：随机挑选了表情包 {random_sticker.name}")

        # 将 AI 的回复也记录到上下文中
        assistant_content = reply_content
        if sticker_name:
            assistant_content += f" [发送了表情包: {sticker_name}]"
        chat_histories[group_id].append({"role": "assistant", "content": assistant_content})

        # 发送回复
        if sticker_segment:
            if reply_content:
                await bot.send(event, reply_content)
                # 稍微延迟一下，显得更自然
                import asyncio
                await asyncio.sleep(random.uniform(0.5, 1.5))
            await bot.send(event, sticker_segment)
        else:
            await bot.send(event, reply_content)

        # 8. 如果是戳一戳触发，有 30% 概率戳回去
        if is_poke and random.random() < 0.3:
            try:
                # 稍微延迟一下再戳回去
                import asyncio
                await asyncio.sleep(random.uniform(0.5, 1.0))
                # 使用 MessageSegment.poke 发送戳一戳消息（某些实现支持，V11 标准通常是这个）
                # 这里的 user_id 是发件人 ID
                await bot.send(event, MessageSegment.poke(int(user_id)))
                logger.info(f"拟人插件：已戳回去给用户 {user_id}")
            except Exception as e:
                logger.error(f"戳回去失败: {e}")

    except FinishedException:
        raise
    except Exception as e:
        logger.error(f"拟人插件 API 调用失败: {e}")

# --- 群聊好感度管理 ---
group_fav_query = on_command("群好感", aliases={"群好感度"}, priority=5, block=True)
@group_fav_query.handle()
async def _(bot: Bot, event: GroupMessageEvent):
    if not SIGN_IN_AVAILABLE:
        await group_fav_query.finish("签到插件未就绪，无法查询好感度。")
    
    group_id = event.group_id
    group_key = f"group_{group_id}"
    data = get_user_data(group_key)
    
    favorability = data.get("favorability", 100.0)
    daily_count = data.get("daily_fav_count", 0.0)
    
    # 统一分级系统
    status = get_level_name(favorability) if SIGN_IN_AVAILABLE else "普通"
    
    # 颜色风格统一 (粉色系)
    title_color = "#ff69b4"
    text_color = "#d147a3"
    border_color = "#ffb6c1"

    # 构建 Markdown 文本 (风格向签到插件靠拢)
    md = f"""
<div style="padding: 20px; background-color: #fff5f8; border-radius: 15px; border: 2px solid {border_color}; font-family: 'Microsoft YaHei', sans-serif;">
    <h1 style="color: {title_color}; text-align: center; margin-bottom: 20px;">🌸 群聊好感度详情 🌸</h1>
    
    <div style="background: white; padding: 15px; border-radius: 12px; border: 1px solid {border_color}; margin-bottom: 15px;">
        <p style="margin: 5px 0; color: #666;">群号: <strong style="color: {text_color};">{group_id}</strong></p>
        <p style="margin: 5px 0; color: #666;">当前等级: <strong style="color: {text_color}; font-size: 1.2em;">{status}</strong></p>
    </div>

    <div style="display: flex; gap: 10px; margin-bottom: 15px;">
        <div style="flex: 1; background: white; padding: 10px; border-radius: 10px; border: 1px solid {border_color}; text-align: center;">
            <div style="font-size: 0.8em; color: #999;">好感分值</div>
            <div style="font-size: 1.4em; font-weight: bold; color: {text_color};">{favorability:.2f}</div>
        </div>
        <div style="flex: 1; background: white; padding: 10px; border-radius: 10px; border: 1px solid {border_color}; text-align: center;">
            <div style="font-size: 0.8em; color: #999;">今日增长</div>
            <div style="font-size: 1.4em; font-weight: bold; color: {text_color};">{daily_count:.1f}/10.0</div>
        </div>
    </div>

    <div style="font-size: 0.9em; color: #888; background: rgba(255,255,255,0.5); padding: 10px; border-radius: 8px; line-height: 1.4;">
        ✨ 良好的聊天氛围会增加好感，触发拉黑行为则会扣除。群好感度越高，AI 就会表现得越热情哦~
    </div>
</div>
"""
    
    pic = None
    if md_to_pic:
        try:
            pic = await md_to_pic(md, width=450)
        except FinishedException:
            raise
        except Exception as e:
            logger.error(f"渲染群好感图片失败: {e}")
            # 继续走文本回退逻辑
    
    if pic:
        await group_fav_query.finish(MessageSegment.image(pic))
    else:
        # 文本回退
        msg = (
            f"📊 群聊好感度详情\n"
            f"群号：{group_id}\n"
            f"当前好感：{favorability:.2f}\n"
            f"当前等级：{status}\n"
            f"今日增长：{daily_count:.1f} / 10.0\n"
            f"✨ 你的热情会让 AI 更有温度~"
        )
        await group_fav_query.finish(msg)

set_group_fav = on_command("设置群好感", permission=SUPERUSER, priority=5, block=True)
@set_group_fav.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    if not SIGN_IN_AVAILABLE:
        await set_group_fav.finish("签到插件未就绪，无法设置好感度。")
        
    arg_str = args.extract_plain_text().strip()
    if not arg_str:
        await set_group_fav.finish("用法: 设置群好感 [群号] [分值] 或在群内发送 设置群好感 [分值]")

    parts = arg_str.split()
    
    # 逻辑：如果在群内且只有一个参数，则设置当前群；否则需要指定群号
    target_group = ""
    new_fav = 0.0
    
    if len(parts) == 1:
        if isinstance(event, GroupMessageEvent):
            target_group = str(event.group_id)
            try:
                new_fav = float(parts[0])
            except ValueError:
                await set_group_fav.finish("分值必须为数字。")
        else:
            await set_group_fav.finish("私聊设置请指定群号：设置群好感 [群号] [分值]")
    elif len(parts) >= 2:
        target_group = parts[0]
        try:
            new_fav = float(parts[1])
        except ValueError:
            await set_group_fav.finish("分值必须为数字。")
    
    if not target_group:
        await set_group_fav.finish("未指定目标群号。")

    group_key = f"group_{target_group}"
    update_user_data(group_key, favorability=new_fav)
    
    logger.info(f"管理员 {event.get_user_id()} 将群 {target_group} 的好感度设置为 {new_fav}")
    await set_group_fav.finish(f"✅ 已将群 {target_group} 的好感度设置为 {new_fav:.2f}")

# --- 永久黑名单管理 ---
perm_blacklist_add = on_command("永久拉黑", permission=SUPERUSER, priority=5, block=True)
@perm_blacklist_add.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    if not SIGN_IN_AVAILABLE:
        await perm_blacklist_add.finish("签到插件未就绪，无法操作。")
        
    target_id = args.extract_plain_text().strip()
    # 支持艾特
    for seg in event.get_message():
        if seg.type == "at":
            target_id = str(seg.data["qq"])
            break
            
    if not target_id:
        await perm_blacklist_add.finish("用法: 永久拉黑 [用户ID/@用户]")

    update_user_data(target_id, is_perm_blacklisted=True)
    await perm_blacklist_add.finish(f"✅ 已将用户 {target_id} 加入永久黑名单。")

perm_blacklist_del = on_command("取消永久拉黑", permission=SUPERUSER, priority=5, block=True)
@perm_blacklist_del.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    if not SIGN_IN_AVAILABLE:
        await perm_blacklist_del.finish("签到插件未就绪，无法操作。")
        
    target_id = args.extract_plain_text().strip()
    for seg in event.get_message():
        if seg.type == "at":
            target_id = str(seg.data["qq"])
            break
            
    if not target_id:
        await perm_blacklist_del.finish("用法: 取消永久拉黑 [用户ID/@用户]")

    update_user_data(target_id, is_perm_blacklisted=False)
    await perm_blacklist_del.finish(f"✅ 已将用户 {target_id} 从永久黑名单中移除。")

perm_blacklist_list = on_command("永久黑名单列表", permission=SUPERUSER, priority=5, block=True)
@perm_blacklist_list.handle()
async def _(bot: Bot, event: MessageEvent):
    if not SIGN_IN_AVAILABLE:
        await perm_blacklist_list.finish("签到插件未就绪，无法操作。")
        
    try:
        from plugin.sign_in.utils import load_data
    except ImportError:
        from ..sign_in.utils import load_data
        
    data = load_data()
    blacklisted_items = []
    for uid, udata in data.items():
        if not uid.startswith("group_") and udata.get("is_perm_blacklisted", False):
            blacklisted_items.append({
                "id": uid,
                "count": udata.get('blacklist_count', 0),
                "fav": udata.get('favorability', 0.0)
            })
            
    if not blacklisted_items:
        await perm_blacklist_list.finish("目前没有永久黑名单用户。")

    # 统一风格参数
    title_color = "#ff69b4"
    text_color = "#d147a3"
    border_color = "#ffb6c1"
    bg_color = "#fff5f8"

    # 构建列表 HTML
    items_html = ""
    for item in blacklisted_items:
        items_html += f"""
        <div style="background: white; padding: 12px; border-radius: 10px; border: 1px solid {border_color}; margin-bottom: 10px; display: flex; justify-content: space-between; align-items: center;">
            <div>
                <div style="font-weight: bold; color: {text_color}; font-size: 1.1em;">{item['id']}</div>
                <div style="font-size: 0.85em; color: #999;">好感度: {item['fav']:.2f}</div>
            </div>
            <div style="text-align: right;">
                <div style="color: #ff4d4f; font-weight: bold;">{item['count']} 次拉黑</div>
                <div style="font-size: 0.8em; color: #ff9999;">⚠️ 永久封禁</div>
            </div>
        </div>
        """

    md = f"""
<div style="padding: 20px; background-color: {bg_color}; border-radius: 15px; border: 2px solid {border_color}; font-family: 'Microsoft YaHei', sans-serif;">
    <h1 style="color: {title_color}; text-align: center; margin-bottom: 20px;">🚫 永久黑名单列表 🚫</h1>
    
    <div style="margin-bottom: 15px;">
        {items_html}
    </div>

    <div style="font-size: 0.9em; color: #888; background: rgba(255,255,255,0.5); padding: 10px; border-radius: 8px; line-height: 1.4; text-align: center;">
        此列表中的用户已被永久禁止与 AI 进行交互。<br>使用「取消永久拉黑」指令可恢复权限。
    </div>
</div>
"""
    
    pic = None
    if md_to_pic:
        try:
            pic = await md_to_pic(md, width=400)
        except FinishedException:
            raise
        except Exception as e:
            logger.error(f"渲染永久黑名单图片失败: {e}")
    
    if pic:
        await perm_blacklist_list.finish(MessageSegment.image(pic))
    else:
        # 文本回退
        msg = "🚫 永久黑名单列表：\n" + "\n".join([f"- {i['id']} (拉黑: {i['count']}次)" for i in blacklisted_items])
        await perm_blacklist_list.finish(msg)
