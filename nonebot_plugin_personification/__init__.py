import random
import time
import re
from typing import Dict, List, Optional
from pathlib import Path
from nonebot import on_message, on_command, logger, get_driver, require, get_bots
from nonebot.typing import T_State
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment, MessageEvent, PokeNotifyEvent, Event
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule
from nonebot.exception import FinishedException
from openai import AsyncOpenAI

require("nonebot_plugin_apscheduler")
require("nonebot_plugin_localstore")

from nonebot_plugin_apscheduler import scheduler
import nonebot_plugin_localstore

# 尝试 require 其他可选插件
try:
    require("nonebot_plugin_account_manager")
except (ImportError, RuntimeError):
    pass

try:
    require("nonebot_plugin_htmlrender")
except (ImportError, RuntimeError):
    pass

try:
    require("nonebot_plugin_shiro_signin")
except (ImportError, RuntimeError):
    pass

from .config import Config, config, get_level_name

# 获取插件数据目录
data_dir = nonebot_plugin_localstore.get_plugin_data_dir()
# 表情包目录默认为数据目录下的 stickers
default_sticker_path = data_dir / "stickers"
default_sticker_path.mkdir(parents=True, exist_ok=True)

# 尝试导入可选依赖
ACCOUNT_MANAGER_AVAILABLE = False
try:
    from nonebot_plugin_account_manager import publish_qzone_shuo
    ACCOUNT_MANAGER_AVAILABLE = True
except (ImportError, RuntimeError):
    pass

md_to_pic = None
try:
    from nonebot_plugin_htmlrender import md_to_pic
except (ImportError, RuntimeError):
    pass

SIGN_IN_AVAILABLE = False
try:
    from nonebot_plugin_shiro_signin.utils import get_user_data, update_user_data
    from nonebot_plugin_shiro_signin.config import config as sign_in_config
    SIGN_IN_AVAILABLE = True
except (ImportError, RuntimeError):
    pass

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
    supported_adapters={"nonebot.adapters.onebot.v11"},
    extra={
        "author": "luojisama",
        "version": "0.1.5",
    },
)

superusers = get_driver().config.superusers

def load_prompt() -> str:
    """加载提示词，支持从路径或直接字符串，兼容 Windows/Linux"""
    # 1. 优先检查专门的路径配置项
    target_path = config.personification_prompt_path or config.personification_system_path
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
    content = config.personification_system_prompt
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
    if group_id not in config.personification_whitelist:
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
    return random.random() < config.personification_probability

# 注册消息处理器，优先级设为 100，不阻断其他插件
reply_matcher = on_message(rule=Rule(personification_rule), priority=100, block=False)

# 注册表情包水群处理器
async def sticker_chat_rule(event: GroupMessageEvent) -> bool:
    group_id = str(event.group_id)
    if group_id not in config.personification_whitelist:
        return False
    # 概率与随机回复一致
    return random.random() < config.personification_probability

sticker_chat_matcher = on_message(rule=Rule(sticker_chat_rule), priority=101, block=False)

@sticker_chat_matcher.handle()
async def _(bot: Bot, event: GroupMessageEvent, state: T_State):
    # 随机选择一种水群模式 (三种模式概率各 1/3)
    mode = random.choice(["text_only", "sticker_only", "mixed"])
    
    sticker_dir = Path(config.personification_sticker_path) if config.personification_sticker_path else default_sticker_path
    available_stickers = []
    if sticker_dir.exists() and sticker_dir.is_dir():
        available_stickers = [f for f in sticker_dir.iterdir() if f.suffix.lower() in [".jpg", ".png", ".gif", ".webp", ".jpeg"]]

    if mode == "sticker_only":
        if available_stickers:
            random_sticker = random.choice(available_stickers)
            logger.info(f"拟人插件：触发水群 [单独表情包] {random_sticker.name}")
            await sticker_chat_matcher.finish(MessageSegment.image(f"file:///{random_sticker.absolute()}"))
        else:
            mode = "text_only" # 如果没表情包，退化为纯文本

    # 文本模式和混合模式需要调用 AI
    if mode in ["text_only", "mixed"]:
        # 通过 state 传递参数给 handle_reply
        state["is_random_chat"] = True
        state["force_mode"] = mode
        # 这里不需要手动调用 handle_reply，因为 sticker_chat_matcher 本身就会触发 handle_reply (如果优先级和 block 设置正确)
        # 但是由于我们想要复用逻辑，且两个 matcher 是独立的，我们还是手动调用，但要确保参数匹配
        await handle_reply(bot, event, state)

# 注册戳一戳处理器
async def poke_rule(event: PokeNotifyEvent) -> bool:
    if event.target_id != event.self_id:
        return False
    group_id = str(event.group_id)
    if group_id not in config.personification_whitelist:
        return False
    # 使用配置的概率响应
    return random.random() < config.personification_poke_probability

poke_matcher = on_message(rule=Rule(poke_rule), priority=100, block=False)
# 注意：v11 的戳一戳通常是 Notify 事件，但在一些实现中可能作为消息
from nonebot import on_notice

async def poke_notice_rule(event: PokeNotifyEvent) -> bool:
    # 打印调试信息，确认事件是否到达
    logger.info(f"收到戳一戳事件: target_id={event.target_id}, self_id={event.self_id}")
    if event.target_id != event.self_id:
        return False
    group_id = str(event.group_id)
    if group_id not in config.personification_whitelist:
        logger.info(f"群 {group_id} 不在白名单 {config.personification_whitelist}")
        return False
    # 使用配置的概率响应
    prob = config.personification_poke_probability
    res = random.random() < prob
    logger.info(f"戳一戳响应判定: 概率={prob}, 结果={res}")
    return res

poke_notice_matcher = on_notice(rule=Rule(poke_notice_rule), priority=10, block=False)

@reply_matcher.handle()
@poke_notice_matcher.handle()
async def handle_reply(bot: Bot, event: Event, state: T_State):
    # 如果是通知事件，需要特殊处理
    is_poke = False
    user_id = ""
    group_id = 0
    message_content = ""
    sender_name = ""
    
    # 从 state 获取可能的参数
    is_random_chat = state.get("is_random_chat", False)
    force_mode = state.get("force_mode", None)

    if isinstance(event, PokeNotifyEvent):
        is_poke = True
        user_id = str(event.user_id)
        group_id = event.group_id
        message_content = "[你被对方戳了戳，你感到有点疑惑和好奇，想知道对方要做什么]"
        sender_name = "戳戳怪"
        logger.info(f"拟人插件：检测到来自 {user_id} 的戳一戳")
    elif isinstance(event, GroupMessageEvent):
        group_id = event.group_id
        user_id = str(event.user_id)
        
        # 提取文本和图片
        message_text = ""
        image_urls = []
        import httpx
        import base64
        
        for seg in event.message:
            if seg.type == "text":
                message_text += seg.data.get("text", "")
            elif seg.type == "image":
                url = seg.data.get("url")
                if url:
                    try:
                        # 尝试将图片转换为 base64 以提高 AI 兼容性 (特别是 Gemini)
                        async with httpx.AsyncClient() as client:
                            resp = await client.get(url, timeout=10)
                            if resp.status_code == 200:
                                mime_type = resp.headers.get("Content-Type", "image/jpeg")
                                base64_data = base64.b64encode(resp.content).decode("utf-8")
                                image_urls.append(f"data:{mime_type};base64,{base64_data}")
                            else:
                                # 如果下载失败，保留原 URL 作为备选
                                image_urls.append(url)
                    except Exception as e:
                        logger.warning(f"下载图片失败，保留原 URL: {e}")
                        image_urls.append(url)
        
        message_content = message_text.strip()
        sender_name = event.sender.card or event.sender.nickname or user_id
        
        # 如果是随机水群触发，修改提示词
        if is_random_chat:
            message_content = f"[你观察到群里正在聊天，你决定主动插话分享一些想法。当前群员 {sender_name} 刚刚说了: {message_content}]"
            # 水群触发时，如果是图片消息，也把图片带上
            if image_urls and not message_text.strip():
                message_content = f"[你观察到群里 {sender_name} 发送了一张图片，你决定评价一下或以此展开话题]"
    else:
        return

    # 如果没配置 API KEY，直接跳过
    if not config.personification_api_key:
        logger.warning("拟人插件：未配置 API Key，跳过回复")
        return

    user_name = sender_name
    
    if not message_content and not is_poke:
        return

    logger.info(f"拟人插件：正在处理来自 {user_name} ({user_id}) 的消息...")

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
            attitude_desc = config.personification_favorability_attitudes.get(level_name, attitude_desc)
            
            # 获取群聊好感度
            group_key = f"group_{group_id}"
            group_data = get_user_data(group_key)
            group_favorability = group_data.get("favorability", 100.0)
            group_level = get_level_name(group_favorability)
            group_attitude = config.personification_favorability_attitudes.get(group_level, "")
        except Exception as e:
            logger.error(f"获取好感度数据失败: {e}")

    # 2. 维护聊天历史上下文
    if group_id not in chat_histories:
        chat_histories[group_id] = []
    
    # 构建当前消息内容
    if image_urls:
        current_user_content = [{"type": "text", "text": f"{user_name}: {message_content}"}]
        for url in image_urls:
            current_user_content.append({"type": "image_url", "image_url": {"url": url}})
        chat_histories[group_id].append({"role": "user", "content": current_user_content})
    else:
        chat_histories[group_id].append({"role": "user", "content": f"{user_name}: {message_content}"})
    
    # 限制上下文长度
    if len(chat_histories[group_id]) > config.personification_history_len:
        chat_histories[group_id] = chat_histories[group_id][-config.personification_history_len:]

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
        "   - 触发后该用户将被拉黑，请务必审慎判定，不要滥用权力。\n"
        "4. **图片与表情包识别**：你现在可以看见对方发送的图片和表情包了。请结合图片内容进行回复，如果对方只发了图片，你可以评价图片或以此展开话题。\n"
        "5. 回复必须精简，禁止废话。"
    )

    # 获取表情包列表（如果启用了）
    available_stickers = []
    sticker_dir = Path(config.personification_sticker_path) if config.personification_sticker_path else default_sticker_path
    if sticker_dir.exists() and sticker_dir.is_dir():
        available_stickers = [f.stem for f in sticker_dir.iterdir() if f.suffix.lower() in [".jpg", ".png", ".gif", ".webp", ".jpeg"]]

    # 4. 构建消息历史
    messages = [
         {"role": "system", "content": f"{system_prompt}\n\n当前表情包库中有以下表情包文件名供参考: {', '.join(available_stickers[:20]) if available_stickers else '暂无'}"}
     ]
    messages.extend(chat_histories[group_id])

    # 4. 调用 AI API
    try:
        # 1. 智能处理 API URL
        api_url = config.personification_api_url.strip()
        api_type = config.personification_api_type.lower()
        
        # 自动识别 Gemini 类型并切换到官方 OpenAI 兼容接口
        if api_type == "gemini" and "api.openai.com" in api_url:
            api_url = "https://generativelanguage.googleapis.com/v1beta/openai/"
            logger.info(f"拟人插件：检测到 Gemini 类型，自动切换至官方兼容接口: {api_url}")
        
        # 根据指南要求：自动补全 /v1 后缀 (针对非 Gemini 官方地址)
        if "generativelanguage.googleapis.com" not in api_url:
            if not api_url.endswith(("/v1", "/v1/")):
                api_url = api_url.rstrip("/") + "/v1"
                logger.info(f"拟人插件：根据 OpenAI 规范自动补全 URL 后缀 -> {api_url}")

        client = AsyncOpenAI(
            api_key=config.personification_api_key,
            base_url=api_url,
            timeout=60.0
        )
        
        try:
            response = await client.chat.completions.create(
                model=config.personification_model,
                messages=messages,
            )
        except Exception as e:
            # 捕获常见的 API 错误并进行人性化处理
            error_msg = str(e)
            
            # 检查是否返回了 HTML (通常是中转站错误或 502/504)
            if "<!DOCTYPE html>" in error_msg or "<html>" in error_msg.lower():
                logger.error(f"拟人插件：API 返回了 HTML 错误页面，可能是中转平台故障或地址填写错误。URL: {api_url}")
                return

            # 如果包含图片且报错，尝试降级到纯文本
            error_str = error_msg.lower()
            is_vision_error = any(kw in error_str for kw in ["vision", "content", "image", "mimetype", "inlinedata", "400"])
            
            if image_urls and is_vision_error:
                logger.warning(f"拟人插件：视觉模型调用失败，正在尝试降级至纯文本模式... 错误原因: {e}")
                fallback_messages = []
                for msg in messages:
                    if isinstance(msg["content"], list):
                        text_content = "".join([item["text"] for item in msg["content"] if item["type"] == "text"])
                        fallback_messages.append({"role": msg["role"], "content": text_content})
                    else:
                        fallback_messages.append(msg)
                
                response = await client.chat.completions.create(
                    model=config.personification_model,
                    messages=fallback_messages,
                    timeout=30.0
                )
            else:
                logger.error(f"拟人插件：API 调用发生错误: {e}")
                return
        
        # 增加对响应对象的类型检查，防止某些非标 API 返回字符串
        if isinstance(response, str):
            logger.warning(f"拟人插件：API 返回了字符串而非对象: {response}")
            reply_content = response.strip()
        else:
            try:
                reply_content = response.choices[0].message.content.strip()
            except (AttributeError, IndexError, TypeError) as e:
                logger.error(f"拟人插件：解析响应对象失败: {e}, 原始响应: {response}")
                # 如果确实解析不了，尝试把整个响应转为字符串，或者抛出异常
                if hasattr(response, "__str__"):
                    reply_content = str(response).strip()
                else:
                    raise ValueError(f"无法从响应中提取内容: {response}")

        # 移除 AI 回复中可能包含的 [表情:xxx] 或 [发送了表情包: xxx] 标签
        import re
        reply_content = re.sub(r'\[表情:[^\]]*\]', '', reply_content)
        reply_content = re.sub(r'\[发送了表情包:[^\]]*\]', '', reply_content).strip()
        
        # 移除 AI 可能吐出的长串十六进制乱码 (例如：766E51F799FC83269D0C9F71409599EF)
        reply_content = re.sub(r'[A-F0-9]{16,}', '', reply_content).strip()
        
        # 5. 处理 AI 的回复决策
        if "[NO_REPLY]" in reply_content:
            duration = config.personification_blacklist_duration
            user_blacklist[user_id] = time.time() + duration
            logger.info(f"AI 决定不回复群 {group_id} 中 {user_name}({user_id}) 的消息，将其拉黑 {duration} 秒")
            
            # 扣除个人及群聊好感度
            penalty_desc = ""
            if SIGN_IN_AVAILABLE:
                try:
                    # 个人扣除
                    penalty = round(random.uniform(0, 0.3), 2)
                    user_data = get_user_data(user_id)
                    current_fav = float(user_data.get("favorability", 0.0))
                    new_fav = round(max(0.0, current_fav - penalty), 2)
                    
                    # 增加拉黑次数统计
                    current_blacklist_count = int(user_data.get("blacklist_count", 0)) + 1
                    is_perm = False
                    if current_blacklist_count >= 25:
                        is_perm = True
                    
                    update_user_data(user_id, favorability=new_fav, blacklist_count=current_blacklist_count, is_perm_blacklisted=is_perm)
                    
                    # 群聊扣除: 扣多 (0.5)
                    group_key = f"group_{group_id}"
                    group_data = get_user_data(group_key)
                    g_current_fav = float(group_data.get("favorability", 100.0))
                    g_new_fav = round(max(0.0, g_current_fav - 0.5), 2)
                    update_user_data(group_key, favorability=g_new_fav)
                    
                    penalty_desc = f"\n个人好感度：-{penalty:.2f} (当前：{new_fav:.2f})\n群聊好感度：-0.50 (当前：{g_new_fav:.2f})\n累计拉黑次数：{current_blacklist_count}/25"
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
                        g_current_fav = float(group_data.get("favorability", 100.0))
                        g_new_fav = round(g_current_fav + 0.1, 2)
                        daily_count = round(float(daily_count) + 0.1, 2)
                        update_user_data(group_key, favorability=g_new_fav, daily_fav_count=daily_count, last_update=today)
                        logger.info(f"AI 觉得群 {group_id} 氛围良好，好感度 +0.10 (今日已加: {daily_count:.2f}/10.00)")
                        
                        # 通知管理员
                        for admin_id in superusers:
                            try:
                                await bot.send_private_msg(
                                    user_id=int(admin_id),
                                    message=f"【群好感变动】\n群：{group_id}\n事件：AI 觉得氛围良好 ✨\n变动：+0.10\n当前好感：{g_new_fav:.2f}\n今日进度：{daily_count:.2f}/10.00"
                                )
                            except Exception as e:
                                logger.error(f"发送好感增加通知失败: {e}")
                except Exception as e:
                    logger.error(f"增加群聊好感度失败: {e}")

        # 7. 决定是否发送表情包
        sticker_segment = None
        sticker_name = ""
        
        # 根据模式决定是否选择表情包
        should_get_sticker = False
        if force_mode == "mixed":
            should_get_sticker = True
        elif force_mode == "text_only":
            should_get_sticker = False
        elif random.random() < config.personification_sticker_probability:
            should_get_sticker = True

        if should_get_sticker:
            sticker_dir = Path(config.personification_sticker_path) if config.personification_sticker_path else default_sticker_path
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
            <div style="font-size: 1.4em; font-weight: bold; color: {text_color};">{daily_count:.2f}/10.00</div>
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
            f"今日增长：{daily_count:.2f} / 10.00\n"
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
        from nonebot_plugin_shiro_signin.utils import load_data
    except ImportError:
        await perm_blacklist_list.finish("无法加载签到插件的数据模块。")
        
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
    
    if md_to_pic:
        try:
            pic = await md_to_pic(md, width=400)
            await perm_blacklist_list.finish(MessageSegment.image(pic))
        except FinishedException:
            raise
        except Exception as e:
            logger.error(f"渲染永久黑名单图片失败: {e}")
    
    # 退化方案
    msg = "🚫 永久黑名单列表 🚫\n"
    for item in blacklisted_items:
        msg += f"\n- {item['id']} ({item['count']}次拉黑 / 好感:{item['fav']:.2f})"
    await perm_blacklist_list.finish(msg)

# --- AI 周记功能 ---

def filter_sensitive_content(text: str) -> str:
    """过滤敏感词汇（简单正则方案）"""
    # 敏感词库（示例，建议根据实际需求扩展）
    sensitive_patterns = [
        r"政治", r"民主", r"政府", r"主席", r"书记", r"国家",  # 政治相关（示例）
        r"色情", r"做爱", r"淫秽", r"成人", r"福利姬", r"裸",  # 色情相关（示例）
        # 可以继续添加更多敏感词模式
    ]
    
    filtered_text = text
    for pattern in sensitive_patterns:
        filtered_text = re.sub(pattern, "**", filtered_text, flags=re.IGNORECASE)
    
    # 过滤掉过短的消息（通常是杂音）
    if len(filtered_text.strip()) < 2:
        return ""
        
    return filtered_text

async def get_recent_chat_context(bot: Bot) -> str:
    """随机获取两个群的最近聊天记录作为周记素材"""
    try:
        # 获取群列表
        group_list = await bot.get_group_list()
        if not group_list:
            return ""
        
        # 随机选择两个群（如果有的话）
        sample_size = min(2, len(group_list))
        selected_groups = random.sample(group_list, sample_size)
        
        context_parts = []
        for group in selected_groups:
            group_id = group["group_id"]
            group_name = group.get("group_name", str(group_id))
            
            try:
                # 获取最近 50 条消息
                messages = await bot.get_group_msg_history(group_id=group_id, count=50)
                if messages and "messages" in messages:
                    msg_list = messages["messages"]
                    chat_text = ""
                    for m in msg_list:
                        sender_name = m.get("sender", {}).get("nickname", "未知")
                        # 提取纯文本内容
                        raw_msg = m.get("message", "")
                        content = ""
                        if isinstance(raw_msg, list):
                            content = "".join([seg["data"]["text"] for seg in raw_msg if seg["type"] == "text"])
                        elif isinstance(raw_msg, str):
                            content = re.sub(r"\[CQ:[^\]]+\]", "", raw_msg)
                        
                        # 执行内容过滤
                        safe_content = filter_sensitive_content(content)
                        
                        if safe_content.strip():
                            chat_text += f"{sender_name}: {safe_content.strip()}\n"
                    
                    if chat_text:
                        context_parts.append(f"【群聊：{group_name} 的最近记录】\n{chat_text}")
            except Exception as e:
                logger.warning(f"获取群 {group_id} 历史记录失败: {e}")
                continue
                
        return "\n\n".join(context_parts)
    except Exception as e:
        logger.error(f"获取聊天上下文失败: {e}")
        return ""

async def generate_ai_diary(bot: Bot) -> str:
    """让 AI 根据聊天记录生成一段周记"""
    system_prompt = load_prompt()
    chat_context = await get_recent_chat_context(bot)
    
    # 基础人设要求
    base_requirements = (
        "1. 语气必须完全符合你的人设（绪山真寻：变成女初中生的宅男，语气笨拙、弱气、容易害羞）。\n"
        "2. 字数严格限制在 200 字以内。\n"
        "3. 直接输出日记内容，不要包含日期或其他无关文字。\n"
        "4. 严禁涉及任何政治、色情、暴力等违规内容。\n"
        "5. 严禁包含任何图片描述、[图片] 占位符或多媒体标记，只能是纯文字内容。"
    )

    async def call_ai(prompt: str) -> Optional[str]:
        try:
            client = AsyncOpenAI(
                api_key=config.personification_api_key,
                base_url=config.personification_api_url
            )
            response = await client.chat.completions.create(
                model=config.personification_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt}
                ],
                timeout=30
            )
            if response and response.choices and response.choices[0].message.content:
                return response.choices[0].message.content.strip()
            return None
        except Exception as e:
            logger.warning(f"AI 生成尝试失败: {e}")
            return None

    # 尝试方案 A：结合群聊素材生成
    if chat_context:
        rich_prompt = (
            "任务：请以日记的形式写一段简短的周记，记录你这一周在群里看到的趣事。\n"
            "素材：以下是最近群里的聊天记录（已脱敏），你可以参考其中的话题：\n"
            f"{chat_context}\n\n"
            f"要求：\n{base_requirements}"
        )
        result = await call_ai(rich_prompt)
        if result:
            return result
        logger.warning("拟人插件：带素材的 AI 生成失败（可能是触发了 API 安全拦截），尝试保底模式...")

    # 尝试方案 B：保底模式（不带素材，降低被拦截概率）
    basic_prompt = (
        "任务：请以日记的形式写一段简短的周记，记录你这一周的心情。\n"
        f"要求：\n{base_requirements}"
    )
    result = await call_ai(basic_prompt)
    return result or ""

async def auto_post_diary():
    """定时任务：每周发送一次说说"""
    if not ACCOUNT_MANAGER_AVAILABLE:
        logger.warning("拟人插件：未找到 account_manager 插件，无法自动发送说说。")
        return
        
    bots = get_bots()
    if not bots:
        logger.warning("拟人插件：未找到有效的 Bot 实例，跳过自动说说发布。")
        return
    
    # 获取第一个 Bot 实例
    bot = list(bots.values())[0]
    
    diary_content = await generate_ai_diary(bot)
    if not diary_content:
        return
        
    logger.info(f"拟人插件：正在自动发布周记说说...")
    success, msg = await publish_qzone_shuo(diary_content, bot.self_id)
    if success:
        logger.info("拟人插件：每周说说发布成功！")
    else:
        logger.error(f"拟人插件：每周说说发布失败：{msg}")

# 每周日晚上 21:00 发送
try:
    scheduler.add_job(auto_post_diary, "cron", day_of_week="sun", hour=21, minute=0, id="ai_weekly_diary", replace_existing=True)
    logger.info("拟人插件：已成功注册 AI 每周说说定时任务 (周日 21:00)")
except Exception as e:
    logger.error(f"拟人插件：注册定时任务失败: {e}")

manual_diary_cmd = on_command("发个说说", permission=SUPERUSER, priority=5, block=True)

@manual_diary_cmd.handle()
async def handle_manual_diary(bot: Bot):
    if not ACCOUNT_MANAGER_AVAILABLE:
        await manual_diary_cmd.finish("未找到 account_manager 插件，无法发布说说。")
        
    await manual_diary_cmd.send("正在生成 AI 周记并发布，请稍候...")
    
    diary_content = await generate_ai_diary(bot)
    if not diary_content:
        await manual_diary_cmd.finish("AI 生成周记失败，请检查网络或 API 配置。")
        
    success, msg = await publish_qzone_shuo(diary_content, bot.self_id)
    if success:
        await manual_diary_cmd.finish(f"✅ AI 说说发布成功！\n\n内容：\n{diary_content}")
    else:
        await manual_diary_cmd.finish(f"❌ 发布失败：{msg}")
