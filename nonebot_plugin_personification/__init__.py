import random
import time
import re
import json
import asyncio
import httpx
import aiofiles
import base64
import yaml
from datetime import datetime, timezone, timedelta
from io import BytesIO
from typing import Dict, List, Optional, Any, Union
from pathlib import Path
from PIL import Image

import nonebot
from nonebot import on_message, on_command, get_plugin_config, logger, get_driver, require, get_bots
from nonebot.typing import T_State
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, PrivateMessageEvent, Message, MessageSegment, MessageEvent, PokeNotifyEvent, Event
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule, to_me
from nonebot.exception import FinishedException
from openai import AsyncOpenAI

# Require localstore first
require("nonebot_plugin_localstore")
import nonebot_plugin_localstore as store

require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler

# 尝试导入空间发布函数 (已适配 bot_manager)
try:
    try:
        from plugin.bot_manager import publish_qzone_shuo
    except ImportError:
        from ..bot_manager import publish_qzone_shuo
    QZONE_PUBLISH_AVAILABLE = True
except ImportError:
    QZONE_PUBLISH_AVAILABLE = False

from .config import config, Config, get_level_name
from .utils import (
    add_group_to_whitelist, 
    remove_group_from_whitelist, 
    is_group_whitelisted, 
    add_request, 
    update_request_status, 
    get_group_config, 
    set_group_prompt, 
    set_group_sticker_enabled, 
    set_group_enabled, 
    set_group_schedule_enabled,
    get_user_name
)
from .schedule import get_beijing_time, get_schedule_prompt_injection, is_rest_time, get_activity_status

# 尝试导入 htmlrender
try:
    from nonebot_plugin_htmlrender import md_to_pic
except ImportError:
    md_to_pic = None

# 尝试导入签到插件的工具函数
try:
    try:
        from plugin.sign_in.utils import get_user_data, update_user_data, load_data
    except ImportError:
        from ..sign_in.utils import get_user_data, update_user_data, load_data
    SIGN_IN_AVAILABLE = True
except ImportError:
    SIGN_IN_AVAILABLE = False

if SIGN_IN_AVAILABLE:
    logger.info("拟人插件：已成功关联签到插件，好感度系统已激活。")
else:
    logger.warning("拟人插件：未找到签到插件，好感度系统将以默认值运行。")

__plugin_meta__ = PluginMetadata(
    name="群聊拟人",
    description="实现拟人化的群聊回复，支持好感度系统及自主回复决策",
    usage=(
        "🤖 基础功能：\n"
        "  - 自动回复：在白名单群聊中随机触发或艾特触发\n"
        "  - 戳一戳回复：随机概率响应用户的戳一戳\n"
        "  - 水群模式：随机发送文字、表情包或混合内容\n"
        "  - 申请白名单：申请将当前群聊加入白名单\n\n"
        "❤️ 好感度系统：\n"
        "  - 群好感 / 群好感度：查看当前群聊的整体好感\n\n"
        "⚙️ 管理员命令 (仅超级用户)：\n"
        "  - 拟人配置：查看当前拟人插件的全局及群组配置\n"
        "  - 拟人开启/关闭：开启或关闭当前群的拟人功能\n"
        "  - 拟人作息 [开启/关闭]：开启或关闭当前群的作息模拟\n"
        "  - 开启/关闭表情包：开启或关闭当前群的表情包功能\n"
        "  - 拟人联网 [开启/关闭]：切换 AI 联网搜索功能\n"
        "  - 查看人设：查看当前群生效的人设提示词\n"
        "  - 设置人设 [群号] <提示词>：设置指定群或当前群的人设\n"
        "  - 重置人设：重置当前群的人设为默认配置\n"
        "  - 设置群好感 [群号] [分值]：手动调整群好感\n"
        "  - 永久拉黑 [用户ID/@用户]：禁止用户与 AI 交互\n"
        "  - 取消永久拉黑 [用户ID/@用户]：移除永久黑名单\n"
        "  - 永久黑名单列表：查看所有被封禁的用户\n"
        "  - 同意白名单 [群号]：批准群聊加入白名单\n"
        "  - 拒绝白名单 [群号]：拒绝群聊加入白名单\n"
        "  - 添加白名单 [群号]：将指定群聊添加到白名单\n"
        "  - 移除白名单 [群号]：将群聊移出白名单\n"
        "  - 清除记忆 / 清除上下文 [群号]：清除当前群或指定群的短期对话上下文\n"
        "  - 发个说说：手动触发一次 AI 周记说说发布"
    ),
    config=Config,
)

superusers = get_driver().config.superusers

def load_prompt(group_id: str = None) -> Union[str, Dict[str, Any]]:
    """加载提示词，支持从路径或直接字符串，兼容 Windows/Linux，优先使用群组特定配置"""
    content = None
    
    # 0. 检查群组特定配置
    if group_id:
        group_config = get_group_config(group_id)
        if "custom_prompt" in group_config:
            content = group_config["custom_prompt"]

    # 1. 优先检查专门的路径配置项
    if not content:
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
                    # 如果是 YAML 文件，直接解析
                    if path.suffix.lower() in [".yml", ".yaml"]:
                         with open(path, "r", encoding="utf-8") as f:
                             logger.info(f"拟人插件：成功加载 YAML 模板: {path.absolute()}")
                             return yaml.safe_load(f)
                    
                    content = path.read_text(encoding="utf-8").strip()
                    logger.info(f"拟人插件：成功从文件加载人格设定: {path.absolute()} (内容长度: {len(content)})")
                    return content
                except Exception as e:
                    logger.error(f"加载路径提示词失败 ({path}): {e}")
            else:
                logger.warning(f"拟人插件：配置文件不存在，将使用默认提示词。尝试路径: {raw_path}")

    # 2. 检查 system_prompt 本身是否是一个存在的路径
    if not content:
        content = config.personification_system_prompt
        
    if content and len(content) < 260:
        try:
            raw_path = content.strip('"').strip("'")
            path = Path(raw_path).expanduser()
            if not path.is_file():
                path = Path(raw_path.replace("\\", "/")).expanduser()
            
            if path.is_file():
                # 如果是 YAML 文件，直接解析
                if path.suffix.lower() in [".yml", ".yaml"]:
                     with open(path, "r", encoding="utf-8") as f:
                         logger.info(f"拟人插件：成功加载 YAML 模板: {path.absolute()}")
                         return yaml.safe_load(f)

                file_content = path.read_text(encoding="utf-8").strip()
                logger.info(f"拟人插件：成功从 system_prompt 路径加载人格设定: {path.absolute()}")
                return file_content
        except Exception:
            pass
            
    # 如果内容看起来像 YAML (包含 input: 和 system:)，尝试解析
    if content and isinstance(content, str) and ("input:" in content or "system:" in content):
        try:
             parsed = yaml.safe_load(content)
             if isinstance(parsed, dict) and ("input" in parsed or "system" in parsed):
                 return parsed
        except:
             pass

    return content

# 模块级唯一 ID，用于诊断是否被多次加载
_module_instance_id = random.randint(1000, 9999)
logger.info(f"拟人插件：模块加载中 (Instance ID: {_module_instance_id})")

chat_histories: Dict[str, List[Dict]] = {}
# 存储每个群组的 bot 状态 (YAML 模式专用)
bot_statuses: Dict[str, str] = {}
# 存储拉黑的用户及其解封时间戳
user_blacklist: Dict[str, float] = {}

# 消息去重缓存，防止在多 Bot 或插件重复加载环境下触发多次回复
_processed_msg_ids: Dict[int, float] = {}

# 消息缓冲池
_msg_buffer: Dict[str, Dict] = {}

def is_msg_processed(message_id: int) -> bool:
    """检查消息是否已处理，使用全局驱动器配置存储以支持多实例去重"""
    driver = get_driver()
    if not hasattr(driver, "_personification_msg_cache"):
        driver._personification_msg_cache = {}
    
    cache = driver._personification_msg_cache
    now = time.time()
    
    # 清理过期缓存
    if len(cache) > 100: # 限制缓存大小防止内存泄漏
        expired = [mid for mid, ts in cache.items() if now - ts > 60]
        for mid in expired:
            del cache[mid]
    
    if message_id in cache:
        logger.debug(f"拟人插件：[Inst {_module_instance_id}] 拦截重复消息 ID: {message_id}")
        return True
    
    cache[message_id] = now
    logger.debug(f"拟人插件：[Inst {_module_instance_id}] 开始处理新消息 ID: {message_id}")
    return False

async def call_ai_api(messages: List[Dict], tools: Optional[List[Dict]] = None, max_tokens: Optional[int] = None, temperature: float = 0.7) -> Optional[str]:
    """通用 AI API 调用函数，支持工具调用"""
    if not config.personification_api_key:
        logger.warning("拟人插件：未配置 API Key，跳过调用")
        return None

    try:
        # 1. 智能处理 API URL
        api_url = config.personification_api_url.strip()
        api_type = config.personification_api_type.lower()
        
        # --- Gemini 官方格式调用分支 ---
        if api_type == "gemini_official":
            # 构造 Gemini 官方请求格式
            # 参考: https://ai.google.dev/api/rest/v1beta/models/generateContent
            
            # 自动识别模型 ID
            model_id = config.personification_model
            # 如果 URL 中没有包含 generateContent，则自动补全
            if "generateContent" not in api_url:
                if not api_url.endswith("/"):
                    api_url += "/"
                if "models/" not in api_url:
                    api_url += f"v1beta/models/{model_id}:generateContent"
                else:
                    api_url += ":generateContent"
            
            # 转换消息格式为 Gemini 格式
            gemini_contents = []
            system_instruction = None
            
            for msg in messages:
                role = msg.get("role")
                content = msg.get("content", "")
                
                parts = []
                if isinstance(content, list):
                    for item in content:
                        if item["type"] == "text":
                            parts.append({"text": item["text"]})
                        elif item["type"] == "image_url":
                            image_url = item["image_url"]["url"]
                            if image_url.startswith("data:"):
                                try:
                                    mime_type, base64_data = image_url.split(";base64,")
                                    mime_type = mime_type.replace("data:", "")
                                    parts.append({
                                        "inline_data": {
                                            "mime_type": mime_type,
                                            "data": base64_data
                                        }
                                    })
                                except Exception as e:
                                    logger.warning(f"解析 base64 图片失败: {e}")
                            else:
                                # Gemini 官方 API 暂不支持直接传 URL，通常需要先上传到 Google AI File API
                                # 这里如果不是 base64，我们只能忽略或者报错，但为了兼容性，我们先跳过
                                logger.warning(f"Gemini 官方格式暂不支持非 base64 图片 URL: {image_url}")
                else:
                    parts.append({"text": str(content)})
                
                if role == "system":
                    system_instruction = {"parts": parts}
                elif role == "user":
                    gemini_contents.append({"role": "user", "parts": parts})
                elif role == "assistant":
                    gemini_contents.append({"role": "model", "parts": parts})

            # 构造请求体
            payload = {
                "contents": gemini_contents,
                "generationConfig": {
                    "temperature": temperature,
                }
            }
            
            if max_tokens:
                payload["generationConfig"]["maxOutputTokens"] = max_tokens
                
            if system_instruction:
                payload["systemInstruction"] = system_instruction

            # 支持 Thinking (思考) 配置
            if config.personification_thinking_budget > 0:
                payload["generationConfig"]["thinkingConfig"] = {
                    "includeThoughts": config.personification_include_thoughts,
                    "thinkingBudget": config.personification_thinking_budget
                }

            # 支持 Grounding (联网) 配置：根据报错建议，使用 google_search 代替 googleSearchRetrieval
            if config.personification_web_search:
                payload["tools"] = [{"google_search": {}}]
            
            # 优化认证逻辑：避免 Header 和 URL 同时携带 Key 导致 400 错误
            headers = {"Content-Type": "application/json"}
            
            # 如果 URL 里没 key 参数，则优先通过 Header 或 URL 注入（二选一）
            if "key=" not in api_url and config.personification_api_key:
                # 某些中转站喜欢 URL 里的 key，某些喜欢 Header
                # 这里根据你提供的 YAML，默认使用 Header，但如果失败可以尝试把 key 加到 URL
                connector = "&" if "?" in api_url else "?"
                api_url += f"{connector}key={config.personification_api_key}"
            elif config.personification_api_key:
                # 如果 URL 里已经有 Key 了，我们就不在 Header 里发 Authorization 了
                pass
            else:
                # 如果都没有，尝试发 Bearer (兼容某些特殊中转)
                headers["Authorization"] = f"Bearer {config.personification_api_key}"

            max_retries = 3
            for attempt in range(max_retries):
                try:
                    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=20.0)) as client:
                        if attempt == 0:
                            logger.info(f"拟人插件：正在使用 Gemini 官方格式调用 API: {api_url}")
                        else:
                            logger.warning(f"拟人插件：Gemini API 调用重试 ({attempt + 1}/{max_retries})...")

                        response = await client.post(api_url, json=payload, headers=headers)
                        
                        if response.status_code != 200:
                            error_detail = response.text
                            logger.error(f"拟人插件：Gemini API 返回错误 ({response.status_code}): {error_detail}")
                            response.raise_for_status()
                        
                        data = response.json()
                        
                        # 提取回复内容
                        # 路径: candidates[0].content.parts[0].text
                        candidates = data.get("candidates", [])
                        if candidates:
                            parts = candidates[0].get("content", {}).get("parts", [])
                            if parts:
                                reply_text = ""
                                for part in parts:
                                    # 1. 检查是否存在 thought 字段 (Gemini 2.0 Flash Thinking 官方格式)
                                    # 如果 part 中包含 thought: true，则该部分为思考过程
                                    if part.get("thought", False):
                                        continue
                                    
                                    # 2. 拼接文本
                                    if "text" in part:
                                        reply_text += part["text"]
                                
                                # 3. 统一过滤 XML 风格的思考标签 (Gemini 常见格式)
                                reply_text = re.sub(r'<thought>.*?</thought>', '', reply_text, flags=re.DOTALL)
                                reply_text = re.sub(r'<thinking>.*?</thinking>', '', reply_text, flags=re.DOTALL)
                                # 4. 过滤 Markdown 代码块风格的思考 (```thinking ... ```)
                                reply_text = re.sub(r'```thinking\s*.*?```', '', reply_text, flags=re.DOTALL)
                                
                                return reply_text.strip()
                        
                        logger.warning(f"拟人插件：Gemini 官方接口返回空结果: {data}")
                        return None
                        
                except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
                    logger.warning(f"拟人插件：API 请求网络异常 (尝试 {attempt + 1}/{max_retries}): {e}")
                    if attempt == max_retries - 1:
                        logger.error(f"拟人插件：API 请求最终失败: {e}")
                        return None
                    await asyncio.sleep(2)
                except Exception as e:
                    logger.error(f"拟人插件：API 请求未知错误: {e}")
                    return None

            return None

        # --- OpenAI 兼容格式调用分支 (保留原逻辑) ---
        # 自动识别 Gemini 类型并切换到官方 OpenAI 兼容接口
        if api_type == "gemini" and "api.openai.com" in api_url:
            api_url = "https://generativelanguage.googleapis.com/v1beta/openai/"
            logger.info(f"拟人插件：检测到 Gemini 类型，自动切换至官方兼容接口: {api_url}")
        
        # 自动补全 /v1 后缀 (针对非 Gemini 官方地址)
        if "generativelanguage.googleapis.com" not in api_url:
            if not api_url.endswith(("/v1", "/v1/")):
                api_url = api_url.rstrip("/") + "/v1"

        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as http_client:
            client = AsyncOpenAI(
                api_key=config.personification_api_key,
                base_url=api_url,
                http_client=http_client
            )
            
            max_iterations = 3
            iteration = 0
            reply_content = ""
            
            # 过滤掉内部元数据 (如 user_id)
            current_messages = []
            for msg in messages:
                clean_msg = {k: v for k, v in msg.items() if k in ["role", "content", "name", "tool_calls", "tool_call_id"]}
                current_messages.append(clean_msg)

            while iteration < max_iterations:
                iteration += 1
                
                call_params = {
                    "model": config.personification_model,
                    "messages": current_messages,
                    "temperature": temperature
                }
                if max_tokens:
                    call_params["max_tokens"] = max_tokens
                if tools:
                    call_params["tools"] = tools
                    call_params["tool_choice"] = "auto"
                
                # 兼容 gemini-official 模式 (虽然已经在上面处理了，这里是防止 config 错配)
                if api_type == "gemini_official":
                     # 如果用户配置错了，强制回退到 openai 兼容模式调用 gemini
                     pass

                try:
                    completion = await client.chat.completions.create(**call_params)
                    msg = completion.choices[0].message
                    
                    if not msg.content and not msg.tool_calls:
                         logger.warning("拟人插件：API 返回空内容")
                         return None

                    if msg.content:
                        reply_content = msg.content

                    if msg.tool_calls:
                        current_messages.append(msg)
                        for tool_call in msg.tool_calls:
                            tool_name = tool_call.function.name
                            tool_args = json.loads(tool_call.function.arguments)
                            
                            logger.info(f"拟人插件：AI 正在调用工具 {tool_name} 参数: {tool_args}")
                            
                            result = ""
                            if tool_name == "search_web":
                                result = "Error: search_web tool is removed. Please use native grounding."
                            elif tool_name == "google_search":
                                result = "Error: google_search tool is removed. Please use native grounding."
                            else:
                                result = f"Error: Tool {tool_name} not found."
                            
                            current_messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "name": tool_name,
                                "content": result
                            })
                        continue # 继续下一轮循环，将工具结果发给 AI
                    
                    return reply_content

                except Exception as e:
                    logger.error(f"拟人插件：OpenAI 兼容 API 调用失败: {e}")
                    return None

            return reply_content

    except Exception as e:
        logger.error(f"拟人插件：API 整体调用异常: {e}")
        return None

def split_text_into_segments(text: str) -> List[str]:
    """将回复文本拆分为多个段落，模拟人类分段发送"""
    segments = []
    # 按照换行符拆分
    parts = text.split('\n')
    
    current_segment = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
            
        # 如果当前段落加上新部分超过一定长度，或者是独立的句子（以标点结尾），则拆分
        if len(current_segment) + len(part) > 20 or (current_segment and current_segment[-1] in ['。', '！', '？', '!', '?', '~']):
            if current_segment:
                segments.append(current_segment)
            current_segment = part
        else:
            if current_segment:
                current_segment += "\n" + part
            else:
                current_segment = part
                
    if current_segment:
        segments.append(current_segment)
        
    return segments

async def _process_yaml_response_logic(bot: Bot, event: GroupMessageEvent, group_id: str, user_id: str, user_name: str, level_name: str, yaml_data: Dict[str, Any], chat_history: List[Dict]):
    """
    处理基于 YAML 状态机的回复逻辑
    """
    try:
        # 1. 获取当前状态
        current_status = bot_statuses.get(group_id, "default")
        
        # 2. 查找匹配的 input 规则
        matched_response = None
        new_status = current_status
        
        inputs = yaml_data.get("input", [])
        if not inputs:
            logger.warning(f"YAML 模板没有 input 规则")
            return
            
        # 获取用户消息
        user_msg = event.get_plaintext().strip()
        
        for rule in inputs:
            # 检查状态匹配
            rule_status = rule.get("status", "default")
            # 支持列表或字符串
            if isinstance(rule_status, list):
                if current_status not in rule_status:
                    continue
            elif rule_status != current_status and rule_status != "*":
                continue
                
            # 检查关键词匹配 (match)
            match_keywords = rule.get("match", [])
            if match_keywords:
                is_match = False
                for keyword in match_keywords:
                    if keyword in user_msg:
                        is_match = True
                        break
                if not is_match:
                    continue
            
            # 找到匹配规则
            matched_response = rule
            break
            
        if not matched_response:
            # 如果没有匹配的规则，是否回退到 AI?
            # 这里简单处理：如果没有匹配，尝试使用 default 状态的 fallback 或者交给 AI
            # 暂时策略：交给 AI (return 这里的函数，让外层继续)
            return

        # 3. 执行回复
        response_template = matched_response.get("response", "")
        if isinstance(response_template, list):
            response_template = random.choice(response_template)
            
        # 替换变量
        response_text = response_template.replace("{user}", user_name)
        
        # 更新状态
        if "set_status" in matched_response:
            new_status = matched_response["set_status"]
            bot_statuses[group_id] = new_status
            logger.info(f"拟人插件：状态变更为 {new_status}")
            
        # 发送回复
        await bot.send(event, response_text)
        
        # 记录历史
        chat_histories[group_id].append({"role": "user", "content": user_msg})
        chat_histories[group_id].append({"role": "assistant", "content": response_text})

    except Exception as e:
        logger.error(f"YAML 逻辑处理失败: {e}")

# --- 主消息处理器 ---
chat_handler = on_message(priority=99, block=False)
@chat_handler.handle()
async def _(bot: Bot, event: GroupMessageEvent):
    group_id = str(event.group_id)
    user_id = str(event.user_id)
    raw_msg = event.get_plaintext().strip()
    
    # 0. 检查黑名单
    if user_id in user_blacklist:
        if time.time() < user_blacklist[user_id]:
            logger.debug(f"用户 {user_id} 在黑名单中，忽略消息")
            return
        else:
            del user_blacklist[user_id]
            
    # 1. 白名单检查
    whitelist = load_whitelist()
    if not is_group_whitelisted(group_id, config.personification_whitelist):
        return

    # 2. 检查是否提及 bot 或概率触发
    is_mentioned = to_me(event)
    should_reply = False
    
    if is_mentioned:
        should_reply = True
        # 移除 @bot 部分，避免干扰
        # raw_msg 这里其实已经去不掉 @ 了，因为是 plaintext。
        # 但通常 AI 能理解。
    elif random.random() < config.personification_probability:
        should_reply = True
        
    # 主动消息触发逻辑 (如果没被提及也没随机触发，检查是否主动插话)
    if not should_reply and config.personification_proactive_enabled:
         # 检查是否休息时间
         if is_rest_time():
             # 检查好感度
             if SIGN_IN_AVAILABLE:
                 group_key = f"group_{group_id}"
                 group_data = get_user_data(group_key)
                 fav = float(group_data.get("favorability", 100.0))
                 if fav >= config.personification_proactive_threshold:
                     # 检查每日限额
                     # 这里简单用随机概率模拟频率控制，避免每次都发
                     # 实际应该记录发送次数，这里简化处理：降低概率
                     if random.random() < 0.05: # 5% 概率主动插话
                         should_reply = True
                         logger.info(f"拟人插件：触发主动插话 (好感度 {fav})")

    if not should_reply:
        return

    # 3. 消息去重
    if is_msg_processed(event.message_id):
        return

    # 4. 获取上下文
    if group_id not in chat_histories:
        chat_histories[group_id] = []
        
    # 添加用户消息到历史
    # 处理图片
    content_list = []
    has_image = False
    image_urls = []
    
    for seg in event.message:
        if seg.type == "text":
            text = seg.data["text"].strip()
            if text:
                content_list.append({"type": "text", "text": text})
        elif seg.type == "image":
            has_image = True
            url = seg.data.get("url")
            if url:
                image_urls.append(url)
                content_list.append({"type": "image_url", "image_url": {"url": url}})
                
    if not content_list:
        return # 空消息

    chat_histories[group_id].append({"role": "user", "content": content_list})
    
    # 保持历史记录长度
    if len(chat_histories[group_id]) > config.personification_history_len:
        chat_histories[group_id] = chat_histories[group_id][-config.personification_history_len:]

    # 5. 准备 Prompt
    # 获取用户昵称
    user_name = get_user_name(event)
    
    # 获取好感度等级
    level_name = "陌生"
    favorability = 0.0
    group_attitude = ""
    
    if SIGN_IN_AVAILABLE:
        # 获取个人好感
        user_data = get_user_data(user_id)
        favorability = float(user_data.get("favorability", 0.0))
        # 获取群好感
        group_key = f"group_{group_id}"
        group_data = get_user_data(group_key)
        group_fav = float(group_data.get("favorability", 100.0))
        
        # 综合评定：以群好感为主，个人为辅？或者取平均？
        # 这里逻辑：主要看个人，但群氛围影响态度
        level_name = get_level_name(favorability)
        
        # 群氛围描述
        if group_fav < 20:
            group_attitude = "群里氛围很差，大家都在吵架，你感到很压抑。"
        elif group_fav > 150:
            group_attitude = "群里氛围超级好，大家都是好朋友，你感到非常开心。"
            
    # 获取态度描述
    attitude_desc = config.personification_favorability_attitudes.get(level_name, "")
    
    # 获取用户画像 (如果可用)
    user_persona = ""
    # 这里可以尝试从 redis 或其他地方获取 analysis 插件的结果，暂时留空
    
    # 加载系统 Prompt
    base_prompt = load_prompt(group_id)
    
    # 如果是 YAML 模式，转入 YAML 逻辑处理
    if isinstance(base_prompt, dict):
         await _process_yaml_response_logic(
             bot, event, group_id, user_id, user_name, level_name, base_prompt, chat_histories[group_id]
         )
         return

    # 文本 Prompt 组装
    # 整合态度
    attitude_desc = attitude_desc or "态度普通，像平常一样交流。"
    combined_attitude = f"你对该用户的个人态度是：{attitude_desc}"
    if group_attitude:
        combined_attitude += f"\n当前群聊整体氛围带给你的感受是：{group_attitude}"
    
    # 联网说明
    web_search_hint = ""
    if config.personification_web_search:
        web_search_hint = "你现在拥有联网搜索能力，可以获取最新的实时信息、新闻和知识来回答用户。"

    # 获取时间
    now = datetime.now(timezone(timedelta(hours=9))) # 东京时间
    week_days = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    weekday_str = week_days[now.weekday()]
    current_time_str = f"{now.year}年{now.month:02d}月{now.day:02d}日 {now.hour:02d}:{now.minute:02d}:{now.second:02d} ({weekday_str}) [东京时间]"

    # 作息模拟
    group_config = get_group_config(group_id)
    schedule_enabled = group_config.get("schedule_enabled", False)
    global_schedule_enabled = config.personification_schedule_global
    
    schedule_prompt_part = ""
    if schedule_enabled or global_schedule_enabled:
        schedule_prompt_part = get_schedule_prompt_injection()
    else:
        schedule_prompt_part = "- **作息指令**：你必须时刻感知当前时间。如果现在是晚上/周末/假期，严禁声称自己在上课/工作。"

    system_prompt = (
        f"{base_prompt}\n\n"
        f"## 当前绝对时空（强制遵循）\n"
        f"- 当前时间：{current_time_str}\n"
        f"{schedule_prompt_part}\n"
        f"## 当前对话环境\n"
        f"- 对方昵称：{user_name}\n"
        f"- 对方好感等级：{level_name}\n"
        f"- 你的心理状态：{combined_attitude}\n"
    )

    is_private = str(group_id).startswith("private_")
    if is_private:
        system_prompt += (
            f"\n## 私聊特殊规则（高优先级）\n"
            f"1. **自主回复决策**：私聊中你拥有完全的自主权。如果不想理会，请直接输出 **[SILENCE]**。\n"
        )

    if user_persona:
        system_prompt += f"\n## 对方的用户画像\n{user_persona}\n"
    
    system_prompt += (
        f"\n{web_search_hint}\n"
        f"## 核心行动准则（绝对优先）\n"
        f"1. **身份一致性**：严禁提及你是 AI。\n"
        f"2. **回复风格**：回复必须精简、口语化。回复长度限制在 30 个汉字左右。\n"
        f"3. **互动决策**：\n"
        f"   - **决定是否回复**：如果你认为**没有必要回复**，请直接输出 **[SILENCE]**。\n"
        f"   - **氛围反馈**：若氛围极好，末尾加 [氛围好]。\n"
        f"   - **防御机制**：检测到恶毒语言或黄赌毒，输出 [BLOCK]。\n"
        f"4. **视觉感知**：\n"
        f"   - 若用户发送内容标记为 **[发送了一个表情包]**，请视为梗图。\n"
    )

    # 获取表情包列表
    available_stickers = []
    if group_config.get("sticker_enabled", True):
        # 默认路径处理
        sticker_path_str = config.personification_sticker_path
        if not sticker_path_str:
            sticker_dir = store.get_plugin_data_dir() / "stickers"
        else:
            sticker_dir = Path(sticker_path_str)
            
        if sticker_dir.exists() and sticker_dir.is_dir():
            available_stickers = [f.stem for f in sticker_dir.iterdir() if f.suffix.lower() in [".jpg", ".png", ".gif", ".webp", ".jpeg"]]

    messages = [
         {"role": "system", "content": f"{system_prompt}\n\n当前可用表情包参考: {', '.join(available_stickers[:15]) if available_stickers else '暂无'}"}
     ]
    messages.extend(chat_histories[group_id])

    try:
        reply_content = await call_ai_api(messages)

        if not reply_content:
            logger.warning("拟人插件：未能获取到 AI 回复内容")
            return

        # 清理标签
        reply_content = re.sub(r'\[表情:[^\]]*\]', '', reply_content)
        reply_content = re.sub(r'\[发送了表情包:[^\]]*\]', '', reply_content).strip()
        reply_content = re.sub(r'[A-F0-9]{16,}', '', reply_content).strip()
        
        if "[SILENCE]" in reply_content:
            logger.info(f"AI 决定结束与群 {group_id} 中 {user_name} 的对话 (SILENCE)")
            return

        if "[BLOCK]" in reply_content or "[NO_REPLY]" in reply_content:
            duration = config.personification_blacklist_duration
            user_blacklist[user_id] = time.time() + duration
            logger.info(f"AI 决定拉黑群 {group_id} 中 {user_name}，时长 {duration} 秒")
            # 扣除好感度逻辑略
            return

        # 氛围好逻辑
        if "[氛围好]" in reply_content:
            reply_content = reply_content.replace("[氛围好]", "").strip()
            # 加分逻辑略

        # 表情包决策
        sticker_segment = None
        sticker_name = ""
        should_get_sticker = False
        if group_config.get("sticker_enabled", True):
             if random.random() < config.personification_sticker_probability:
                should_get_sticker = True
        
        if should_get_sticker and available_stickers:
            # 重新获取目录
            if not config.personification_sticker_path:
                sticker_dir = store.get_plugin_data_dir() / "stickers"
            else:
                sticker_dir = Path(config.personification_sticker_path)
                
            stickers = [f for f in sticker_dir.iterdir() if f.suffix.lower() in [".jpg", ".png", ".gif", ".webp", ".jpeg"]]
            if stickers:
                random_sticker = random.choice(stickers)
                sticker_name = random_sticker.stem
                sticker_segment = MessageSegment.image(f"file:///{random_sticker.absolute()}")

        # 记录回复
        assistant_content = reply_content
        if sticker_name:
            assistant_content += f" [发送了表情包: {sticker_name}]"
        chat_histories[group_id].append({"role": "assistant", "content": assistant_content})

        # 发送
        final_reply = reply_content.strip()
        if final_reply:
            segments = split_text_into_segments(final_reply)
            for i, seg in enumerate(segments):
                if not seg.strip(): continue
                await bot.send(event, seg)
                if i < len(segments) - 1 or sticker_segment:
                    await asyncio.sleep(random.uniform(3.0, 5.0))

        if sticker_segment:
            await bot.send(event, sticker_segment)

    except Exception as e:
        logger.error(f"拟人插件 API 调用失败: {e}")

# --- 管理命令 ---
# 群好感查询
group_fav_query = on_command("群好感", aliases={"群好感度"}, priority=5, block=True)
@group_fav_query.handle()
async def _(bot: Bot, event: GroupMessageEvent):
    if not SIGN_IN_AVAILABLE:
        await group_fav_query.finish("签到插件未就绪，无法查询好感度。")
    
    group_id = event.group_id
    group_key = f"group_{group_id}"
    data = get_user_data(group_key)
    favorability = data.get("favorability", 100.0)
    level = get_level_name(favorability)
    await group_fav_query.finish(f"当前群好感度：{favorability:.2f} ({level})")

# 设置群好感
set_group_fav = on_command("设置群好感", permission=SUPERUSER, priority=5, block=True)
@set_group_fav.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    if not SIGN_IN_AVAILABLE:
        await set_group_fav.finish("签到插件未就绪。")
    
    args_str = args.extract_plain_text().split()
    if len(args_str) < 2:
        await set_group_fav.finish("用法：设置群好感 [群号] [数值]")
    
    gid, val = args_str[0], float(args_str[1])
    update_user_data(f"group_{gid}", favorability=val)
    await set_group_fav.finish(f"已将群 {gid} 好感度设置为 {val}")

# 设置人设
set_persona = on_command("设置人设", permission=SUPERUSER, priority=5, block=True)
@set_persona.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    raw_text = args.extract_plain_text().strip()
    if not raw_text:
        await set_persona.finish("请提供提示词！")
    
    # 简单处理：如果是群聊，默认设置本群
    target_group = str(event.group_id) if isinstance(event, GroupMessageEvent) else ""
    # 解析参数逻辑略，直接设置
    if target_group:
        set_group_prompt(target_group, raw_text)
        await set_persona.finish("人设已更新。")

# 开启/关闭拟人
toggle_personification = on_command("拟人开启", aliases={"拟人关闭"}, permission=SUPERUSER, priority=5, block=True)
@toggle_personification.handle()
async def _(bot: Bot, event: GroupMessageEvent):
    cmd = event.get_plaintext().strip()
    enable = "开启" in cmd
    set_group_enabled(str(event.group_id), enable)
    await toggle_personification.finish(f"已{'开启' if enable else '关闭'}本群拟人功能。")

# 开启/关闭作息
toggle_schedule = on_command("拟人作息", permission=SUPERUSER, priority=5, block=True)
@toggle_schedule.handle()
async def _(bot: Bot, event: GroupMessageEvent, args: Message = CommandArg()):
    arg = args.extract_plain_text().strip()
    if "开启" in arg:
        set_group_schedule_enabled(str(event.group_id), True)
        await toggle_schedule.finish("已开启本群作息模拟。")
    elif "关闭" in arg:
        set_group_schedule_enabled(str(event.group_id), False)
        await toggle_schedule.finish("已关闭本群作息模拟。")
    else:
        await toggle_schedule.finish("用法：拟人作息 开启/关闭")
