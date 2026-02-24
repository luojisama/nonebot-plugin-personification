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
    """加载提示词，支持从路径 or 直接字符串，兼容 Windows/Linux，优先使用群组特定配置"""
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
    """将长文本拆分为多个短句，模拟人类分段发送"""
    # 正则：匹配 句号、问号、感叹号、换行符，以及省略号
    pattern = r'([。！？!?\n]+|[…]{1,2}|[.]{3,6})'
    
    parts = re.split(pattern, text)
    segments = []
    buffer = ""
    
    for part in parts:
        if not part:
            continue
            
        if re.match(pattern, part):
            buffer += part
            segments.append(buffer)
            buffer = ""
        else:
            if buffer:
                segments.append(buffer)
                buffer = ""
            buffer = part
            
    if buffer:
        segments.append(buffer)
        
    return segments

async def _buffer_timer(key: str, bot: Bot):
    # 等待 7 秒（用户输入缓冲，收到新消息会重置此时钟）
    delay = 7.0
    await asyncio.sleep(delay)
    
    # 时间到，开始处理
    if key in _msg_buffer:
        data = _msg_buffer.pop(key)
        events = data["events"]
        state = data["state"]
        
        if not events:
            return

        # 拼接消息
        combined_message = Message()
        # 使用第一个事件作为基础
        first_event = events[0]
        
        for i, ev in enumerate(events):
            if isinstance(ev, MessageEvent):
                if i > 0:
                     combined_message.append(MessageSegment.text(" "))
                combined_message.extend(ev.message)
        
        # 将拼接后的消息放入 state
        state["concatenated_message"] = combined_message
        
        try:
            await _process_response_logic(bot, first_event, state)
        except Exception as e:
            logger.error(f"拟人插件：处理拼接消息失败: {e}")

def extract_xml_content(text: str, tag: str) -> Optional[str]:
    """提取 XML 标签内容，支持多行，支持属性"""
    pattern = f"<{tag}.*?>(.*?)</\s*{tag}\s*>"
    match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return None

def parse_yaml_response(response: str) -> Dict[str, Any]:
    """解析 YAML 模板定义的 AI 回复格式"""
    result = {
        "status": extract_xml_content(response, "status"),
        "think": extract_xml_content(response, "think"),
        "action": extract_xml_content(response, "action"),
        "messages": []
    }
    
    output_content = extract_xml_content(response, "output")
    if not output_content:
        output_content = response

    if output_content:
        msg_pattern = r'<message(.*?)>(.*?)</\s*message\s*>'
        matches = finditer = re.finditer(msg_pattern, output_content, re.DOTALL | re.IGNORECASE)
        for match in matches:
            attrs = match.group(1)
            content = match.group(2).strip()
            
            quote_id = None
            quote_match = re.search(r'quote=["\']([^"\']*)["\']', attrs)
            if quote_match:
                quote_id = quote_match.group(1)
            
            sticker_url = extract_xml_content(content, "sticker")
            if sticker_url:
                content = re.sub(r'<sticker.*?>.*?</\s*sticker\s*>', '', content, flags=re.DOTALL | re.IGNORECASE).strip()
            
            result["messages"].append({
                "quote": quote_id,
                "text": content,
                "sticker": sticker_url
            })
            
    return result

async def _process_yaml_response_logic(
    bot: Bot, 
    event: Event, 
    group_id: str, 
    user_id: str, 
    user_name: str, 
    level_name: str, 
    prompt_config: Dict[str, Any], 
    chat_history: List[Dict],
    trigger_reason: str = ""
):
    """处理基于 YAML 模板的新版响应逻辑"""
    
    # 1. 准备上下文变量
    now = datetime.now(timezone(timedelta(hours=9)))
    week_days = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    weekday_str = week_days[now.weekday()]
    current_time_str = f"{now.year}年{now.month:02d}月{now.day:02d}日 {now.hour:02d}:{now.minute:02d}:{now.second:02d} ({weekday_str}) [东京时间]"
    
    current_status = bot_statuses.get(group_id)
    if not current_status:
        current_status = prompt_config.get("status", "").strip()
        if not current_status:
             current_status = '心情: "平静"\n状态: "正在潜水"\n记忆: ""\n动作: "发呆"'
        bot_statuses[group_id] = current_status

    history_new_text = ""
    recent_msgs = chat_history[:-1] if len(chat_history) > 1 else [] 
    
    for msg in recent_msgs:
        role = msg["role"]
        content = msg["content"]
        
        text_content = ""
        if isinstance(content, list):
             for item in content:
                 if item["type"] == "text":
                     text_content += item["text"]
                 elif item["type"] == "image_url":
                     text_content += "[图片]"
        else:
             text_content = str(content)
             
        if role == "user":
            history_new_text += f"{text_content}\n"
        elif role == "assistant":
            clean_content = re.sub(r' \[发送了表情包:.*?\]', '', text_content)
            history_new_text += f"[我]: {clean_content}\n"
            
    if not history_new_text:
        history_new_text = "(无最近消息)"

    last_msg = chat_history[-1]
    history_last_text = ""
    if isinstance(last_msg["content"], list):
         for item in last_msg["content"]:
             if item["type"] == "text":
                 history_last_text += item["text"]
             elif item["type"] == "image_url":
                 history_last_text += "[图片]"
    else:
         history_last_text = str(last_msg["content"])

    input_template = prompt_config.get("input", "")
    
    input_text = input_template.replace("{trigger_reason}", trigger_reason)
    input_text = input_text.replace("{time}", current_time_str)
    input_text = input_text.replace("{history_new}", history_new_text)
    input_text = input_text.replace("{history_last}", history_last_text)
    input_text = input_text.replace("{status}", current_status)
    input_text = input_text.replace("{long_memory('guild')}", "(暂无长期记忆)")
    
    system_prompt = prompt_config.get("system", "")
    
    group_config = get_group_config(group_id)
    schedule_enabled = group_config.get("schedule_enabled", False)
    global_schedule_enabled = config.personification_schedule_global
    
    if schedule_enabled or global_schedule_enabled:
        schedule_prompt_part = get_schedule_prompt_injection()
        system_prompt += f"\n\n{schedule_prompt_part}"
    
    user_content = input_text
    
    last_msg = chat_history[-1]
    last_images = []
    if isinstance(last_msg["content"], list):
        for item in last_msg["content"]:
            if item["type"] == "image_url":
                img_url_obj = item.get("image_url", {})
                if isinstance(img_url_obj, dict):
                     url = img_url_obj.get("url")
                     if url:
                         last_images.append(url)
                elif isinstance(img_url_obj, str):
                     last_images.append(img_url_obj)

    if last_images:
        user_content = [{"type": "text", "text": input_text}]
        for img_url in last_images:
            user_content.append({"type": "image_url", "image_url": {"url": img_url}})

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content}
    ]
    
    reply_content = await call_ai_api(messages)
    
    if not reply_content:
        logger.warning("拟人插件 (YAML): 未能获取到 AI 回复内容")
        return

    parsed = parse_yaml_response(reply_content)
    
    if parsed["status"]:
        bot_statuses[group_id] = parsed["status"]
        logger.info(f"拟人插件: 更新状态为: {parsed['status']}")
        
    if parsed["think"]:
        logger.debug(f"拟人插件: 思考过程: {parsed['think']}")

    if parsed["action"]:
        action_text = parsed["action"]
        logger.info(f"拟人插件: 执行动作: {action_text}")
        if "戳一戳" in action_text:
            try:
                await bot.send(event, MessageSegment.poke(int(user_id)))
            except Exception as e:
                logger.warning(f"拟人插件: 发送戳一戳失败: {e}")

    if parsed["messages"]:
        for msg in parsed["messages"]:
            text = msg["text"]
            sticker_url = msg["sticker"]
            
            if text:
                segments = split_text_into_segments(text)
                for seg in segments:
                    if seg.strip():
                        await bot.send(event, seg)
                        await asyncio.sleep(random.uniform(1.0, 3.0))
            
            if sticker_url:
                try:
                    if sticker_url.startswith("http"):
                        await bot.send(event, MessageSegment.image(sticker_url))
                    else:
                        sticker_dir = Path(config.personification_sticker_path or (store.get_plugin_data_dir() / "stickers"))
                        target_file = None
                        if sticker_dir.exists():
                             possible = sticker_dir / sticker_url
                             if possible.exists():
                                 target_file = possible
                             else:
                                 for f in sticker_dir.iterdir():
                                     if f.stem == sticker_url:
                                         target_file = f
                                         break
                        
                        if target_file:
                             await bot.send(event, MessageSegment.image(f"file:///{target_file.absolute()}"))
                        else:
                             logger.warning(f"拟人插件: 找不到表情包 {sticker_url}")
                except Exception as e:
                    logger.error(f"发送表情包失败: {e}")
    
    else:
        clean_reply = re.sub(r'<status.*?>.*?</\s*status\s*>', '', reply_content, flags=re.DOTALL | re.IGNORECASE)
        clean_reply = re.sub(r'<think.*?>.*?</\s*think\s*>', '', clean_reply, flags=re.DOTALL | re.IGNORECASE)
        clean_reply = re.sub(r'</?\s*output.*?>', '', clean_reply, flags=re.IGNORECASE)
        clean_reply = re.sub(r'</?\s*message.*?>', '', clean_reply, flags=re.IGNORECASE)
        clean_reply = clean_reply.strip()
        if clean_reply:
             await bot.send(event, clean_reply)
             
    assistant_text = ""
    if parsed["messages"]:
        assistant_text = " ".join([m["text"] for m in parsed["messages"] if m["text"]])
    else:
        assistant_text = clean_reply
        
    chat_history.append({"role": "assistant", "content": assistant_text})

async def _process_response_logic(bot: Bot, event: Event, state: T_State):
    if hasattr(event, "message_id"):
        if is_msg_processed(event.message_id):
            return

    is_poke = False
    user_id = ""
    group_id = 0
    message_content = ""
    sender_name = ""
    trigger_reason = ""
    image_urls = []
    
    is_random_chat = state.get("is_random_chat", False)
    force_mode = state.get("force_mode", None)

    if isinstance(event, PokeNotifyEvent):
        is_poke = True
        user_id = str(event.user_id)
        group_id = str(event.group_id)
        message_content = "[你被对方戳了戳，你感到有点疑惑和好奇，想知道对方要做什么]"
        sender_name = "戳戳怪"
        logger.info(f"拟人插件：检测到来自 {user_id} 的戳一戳")
    elif isinstance(event, MessageEvent):
        user_id = str(event.user_id)
        if isinstance(event, GroupMessageEvent):
            group_id = str(event.group_id)
            sender_name = event.sender.nickname or event.sender.card or user_id
        else:
            group_id = f"private_{user_id}"
            sender_name = event.sender.nickname or user_id
        
        message_text = ""
        source_message = state.get("concatenated_message", event.message)
        for seg in source_message:
            if seg.type == "text":
                message_text += seg.data.get("text", "")
            elif seg.type == "face":
                face_id = seg.data.get("id", "")
                message_text += f"[表情id:{face_id}]"
            elif seg.type == "mface":
                summary = seg.data.get("summary", "表情包")
                message_text += f"[{summary}]"
            elif seg.type == "image":
                url = seg.data.get("url")
                file_name = seg.data.get("file", "").lower()
                if url:
                    try:
                        async with httpx.AsyncClient() as client:
                            resp = await client.get(url, timeout=10)
                            if resp.status_code == 200:
                                mime_type = resp.headers.get("Content-Type", "image/jpeg")
                                if "image/gif" in mime_type or file_name.endswith(".gif"):
                                    continue
                                
                                try:
                                    img_obj = Image.open(BytesIO(resp.content))
                                    w, h = img_obj.size
                                    if w <= 1280 and h <= 1280:
                                        message_text += "[发送了一个表情包]"
                                    else:
                                        message_text += "[发送了一张图片]"
                                except Exception:
                                     message_text += "[发送了一张图片]"

                                base64_data = base64.b64encode(resp.content).decode("utf-8")
                                image_urls.append(f"data:{mime_type};base64,{base64_data}")
                            else:
                                if not file_name.endswith(".gif"):
                                    message_text += "[发送了一张图片]"
                                    image_urls.append(url)
                    except Exception as e:
                        logger.warning(f"下载图片失败，保留原 URL: {e}")
                        if not file_name.endswith(".gif"):
                            message_text += "[发送了一张图片]"
                            image_urls.append(url)
        
        reply = getattr(event, "reply", None)
        if reply:
            reply_msg = getattr(reply, "message", None) or (reply.get("message") if isinstance(reply, dict) else None)
            if reply_msg:
                message_text += "\n[引用内容]: "
                try:
                    if isinstance(reply_msg, (list, tuple, Message)):
                        for seg in reply_msg:
                            seg_type = getattr(seg, "type", None) or (seg.get("type") if isinstance(seg, dict) else None)
                            data = getattr(seg, "data", None) or (seg.get("data") if isinstance(seg, dict) else {})
                            if seg_type == "text":
                                message_text += data.get("text", "")
                            elif seg_type == "image":
                                url = data.get("url")
                                if url:
                                    message_text += "[图片]"
                                    image_urls.append(url)
                except Exception:
                     pass

        message_content = message_text.strip()
        base_prompt = load_prompt(group_id)
        is_yaml_mode = isinstance(base_prompt, dict)
        
        if is_yaml_mode:
            if is_poke:
                trigger_reason = "对方戳了戳你。"
            elif is_random_chat:
                trigger_reason = "你正在【潜水】观察群聊。这只是群员之间的普通对话，并非对你说话。除非话题非常吸引你或者你被提及，否则请保持沉默并回复 [SILENCE]。"
            else:
                trigger_reason = f"对方（{sender_name}）正在【主动】与你搭话，请认真回复。"
            if image_urls and not message_content:
                message_content = "[发送了一张图片]"
        else:
            if image_urls and not message_content:
                if is_random_chat:
                    message_content = f"[你观察到群里 {sender_name} 发送了一张图片，这只是群员间的交流，你决定是否要评价一下]"
                else:
                    message_content = f"[对方发送了一张图片，是在对你说话]"
            elif is_random_chat:
                message_content = f"[提示：当前为【随机插话模式】。群员 {sender_name} 正在和别人聊天，内容是: {message_content}。如果话题与你无关，请务必回复 [SILENCE]]"
            else:
                message_content = f"[提示：对方正在【直接】对你说话：{message_content}]"
    else:
        return

    if not config.personification_api_key:
        return

    user_name = sender_name
    if not message_content and not is_poke and not image_urls:
        return

    if group_id not in chat_histories:
        chat_histories[group_id] = []

    user_persona = ""
    try:
        persona_data_path = Path("data/user_persona/data.json")
        if persona_data_path.exists():
            async with aiofiles.open(persona_data_path, mode="r", encoding="utf-8") as f:
                persona_json = json.loads(await f.read())
                personas = persona_json.get("personas", {})
                if user_id in personas:
                    user_persona = personas[user_id].get("data", "")
    except Exception:
        pass

    attitude_desc = "态度普通，像平常一样交流。"
    level_name = "未知"
    group_favorability = 100.0
    group_attitude = ""
    
    if SIGN_IN_AVAILABLE:
        try:
            user_data = get_user_data(user_id)
            favorability = user_data.get("favorability", 0.0)
            level_name = get_level_name(favorability)
            attitude_desc = config.personification_favorability_attitudes.get(level_name, attitude_desc)
            group_key = f"group_{group_id}"
            group_data = get_user_data(group_key)
            group_favorability = group_data.get("favorability", 100.0)
            group_level = get_level_name(group_favorability)
            group_attitude = config.personification_favorability_attitudes.get(group_level, "")
        except Exception:
            pass

    safe_user_name = user_name.replace(":", "：").replace("\n", " ").strip()
    safe_user_name = f"{safe_user_name}({user_id})"
    msg_prefix = f"[{safe_user_name}]: "
    
    if image_urls:
        current_user_content = [{"type": "text", "text": f"{msg_prefix}{message_content}"}]
        for url in image_urls:
            current_user_content.append({"type": "image_url", "image_url": {"url": url}})
        chat_histories[group_id].append({"role": "user", "content": current_user_content})
    else:
        chat_histories[group_id].append({"role": "user", "content": f"{msg_prefix}{message_content}"})
    
    limit = 50 if str(group_id).startswith("private_") else 20
    if len(chat_histories[group_id]) > limit:
        chat_histories[group_id] = chat_histories[group_id][-limit:]

    base_prompt = load_prompt(group_id)
    if isinstance(base_prompt, dict):
         if not trigger_reason and is_poke:
             trigger_reason = "对方戳了戳你。"
         await _process_yaml_response_logic(
             bot, event, group_id, user_id, user_name, level_name, base_prompt, chat_histories[group_id],
             trigger_reason=trigger_reason
         )
         return
    
    combined_attitude = f"你对该用户的个人态度是：{attitude_desc}"
    if group_attitude:
        combined_attitude += f"\n当前群聊整体氛围带给你的感受是：{group_attitude}"
    
    web_search_hint = "你现在拥有联网搜索能力，可以获取最新的实时信息、新闻和知识来回答用户。" if config.personification_web_search else ""
    now = datetime.now(timezone(timedelta(hours=9)))
    week_days = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    weekday_str = week_days[now.weekday()]
    current_time_str = f"{now.year}年{now.month:02d}月{now.day:02d}日 {now.hour:02d}:{now.minute:02d}:{now.second:02d} ({weekday_str}) [东京时间]"

    group_config = get_group_config(group_id)
    schedule_enabled = group_config.get("schedule_enabled", False)
    global_schedule_enabled = config.personification_schedule_global
    schedule_prompt_part = get_schedule_prompt_injection() if (schedule_enabled or global_schedule_enabled) else "- **作息指令**：你必须时刻感知当前时间。如果现在是晚上/周末/假期，严禁声称自己在上课/工作。"

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

    if str(group_id).startswith("private_"):
        system_prompt += (
            f"\n## 私聊特殊规则（高优先级）\n"
            f"1. **自主回复决策**：私聊中你拥有完全的自主权。如果对方发送的内容重复、无意义或你不想理会，请直接输出 **[SILENCE]** 结束话题。\n"
            f"2. **极简回复模式**：对于高频、低信息量的私聊内容，必须使用极简回复（1-2句以内）。\n"
        )

    if user_persona:
        system_prompt += f"\n## 对方的用户画像\n{user_persona}\n"
    
    system_prompt += (
        f"\n{web_search_hint}\n"
        f"## 核心行动准则（绝对优先）\n"
        f"1. **身份一致性**：严禁提及你是 AI。\n"
        f"2. **回复风格**：回复必须精简、口语化。长度限制在 30 个汉字左右。\n"
        f"3. **社交距离感**：分清【直接对话】与【潜水观察】。在潜水时尽量保持沉默，除非有必要插话。\n"
        f"4. **互动决策**：认为没有必要回复时请直接输出 **[SILENCE]**。氛围极好末尾加 [氛围好]。检测到恶毒内容输出 [BLOCK]。\n"
        f"5. **视觉感知**：标记为 [发送了一个表情包] 视为梗图，调侃或玩梗即可。标记为 [发送了一张图片] 则正常评价。\n"
    )

    available_stickers = []
    if group_config.get("sticker_enabled", True):
        sticker_dir = Path(config.personification_sticker_path or (store.get_plugin_data_dir() / "stickers"))
        if sticker_dir.exists() and sticker_dir.is_dir():
            available_stickers = [f.stem for f in sticker_dir.iterdir() if f.suffix.lower() in [".jpg", ".png", ".gif", ".webp", ".jpeg"]]

    messages = [
         {"role": "system", "content": f"{system_prompt}\n\n当前可用表情包参考: {', '.join(available_stickers[:15]) if available_stickers else '暂无'}"}
     ]
    messages.extend(chat_histories[group_id])

    try:
        if str(group_id).startswith("private_"):
            p_state = load_proactive_state()
            if user_id not in p_state: p_state[user_id] = {}
            p_state[user_id]["last_interaction"] = time.time()
            save_proactive_state(p_state)

        reply_content = await call_ai_api(messages)
        if not reply_content:
            if image_urls:
                fallback_messages = []
                for msg in messages:
                    if isinstance(msg.get("content"), list):
                        text_content = "".join([item["text"] for item in msg["content"] if item["type"] == "text"])
                        fallback_messages.append({"role": msg["role"], "content": text_content})
                    else:
                        fallback_messages.append(msg)
                reply_content = await call_ai_api(fallback_messages)
            if not reply_content: return

        reply_content = re.sub(r'\[表情:[^\]]*\]', '', reply_content)
        reply_content = re.sub(r'\[发送了表情包:[^\]]*\]', '', reply_content)
        reply_content = re.sub(r'[A-F0-9]{16,}', '', reply_content).strip()
        
        if "[SILENCE]" in reply_content: return

        if "[BLOCK]" in reply_content or "[NO_REPLY]" in reply_content:
            user_blacklist[user_id] = time.time() + config.personification_blacklist_duration
            if SIGN_IN_AVAILABLE:
                penalty = round(random.uniform(0, 0.3), 2)
                user_data = get_user_data(user_id)
                new_fav = round(max(0.0, float(user_data.get("favorability", 0.0)) - penalty), 2)
                current_blacklist_count = int(user_data.get("blacklist_count", 0)) + 1
                update_user_data(user_id, favorability=new_fav, blacklist_count=current_blacklist_count, is_perm_blacklisted=(current_blacklist_count >= 25))
                if not str(group_id).startswith("private_"):
                    group_key = f"group_{group_id}"
                    g_data = get_user_data(group_key)
                    update_user_data(group_key, favorability=round(max(0.0, float(g_data.get("favorability", 100.0)) - 0.5), 2))
            return

        if "[氛围好]" in reply_content:
            reply_content = reply_content.replace("[氛围好]", "").strip()
            if SIGN_IN_AVAILABLE and not str(group_id).startswith("private_"):
                group_key = f"group_{group_id}"
                g_data = get_user_data(group_key)
                today = time.strftime("%Y-%m-%d")
                daily_count = float(g_data.get("daily_fav_count", 0.0)) if g_data.get("last_update") == today else 0.0
                if daily_count < 10.0:
                    update_user_data(group_key, favorability=round(float(g_data.get("favorability", 100.0)) + 0.1, 2), daily_fav_count=round(daily_count + 0.1, 2), last_update=today)

        sticker_segment = None
        sticker_name = ""
        if group_config.get("sticker_enabled", True):
            if force_mode == "mixed" or (not force_mode and random.random() < config.personification_sticker_probability):
                sticker_dir = Path(config.personification_sticker_path or (store.get_plugin_data_dir() / "stickers"))
                if sticker_dir.exists():
                    stickers = [f for f in sticker_dir.iterdir() if f.suffix.lower() in [".jpg", ".png", ".gif", ".webp", ".jpeg"]]
                    if stickers:
                        random_sticker = random.choice(stickers)
                        sticker_name = random_sticker.stem
                        sticker_segment = MessageSegment.image(f"file:///{random_sticker.absolute()}")

        assistant_content = reply_content
        if sticker_name: assistant_content += f" [发送了表情包: {sticker_name}]"
        chat_histories[group_id].append({"role": "assistant", "content": assistant_content})

        final_reply = reply_content.strip()
        if final_reply:
            segments = split_text_into_segments(final_reply)
            for i, seg in enumerate(segments):
                if not seg.strip(): continue
                await bot.send(event, seg)
                if i < len(segments) - 1 or sticker_segment:
                    await asyncio.sleep(random.uniform(2.0, 4.0))

        if sticker_segment:
            await bot.send(event, sticker_segment)

    except FinishedException: raise
    except Exception as e: logger.error(f"拟人插件 API 调用失败: {e}")

# --- 管理命令 ---
group_fav_query = on_command("群好感", aliases={"群好感度"}, priority=5, block=True)
@group_fav_query.handle()
async def _(bot: Bot, event: GroupMessageEvent):
    if not SIGN_IN_AVAILABLE: await group_fav_query.finish("签到插件未就绪。")
    group_id = event.group_id
    group_key = f"group_{group_id}"
    data = get_user_data(group_key)
    favorability = data.get("favorability", 100.0)
    daily_count = data.get("daily_fav_count", 0.0)
    status = get_level_name(favorability)
    
    md = f"""
<div style="padding: 20px; background-color: #fff5f8; border-radius: 15px; border: 2px solid #ffb6c1; font-family: 'Microsoft YaHei', sans-serif;">
    <h1 style="color: #ff69b4; text-align: center; margin-bottom: 20px;">🌸 群聊好感度详情 🌸</h1>
    <div style="background: white; padding: 15px; border-radius: 12px; border: 1px solid #ffb6c1; margin-bottom: 15px;">
        <p style="margin: 5px 0; color: #666;">群号: <strong style="color: #d147a3;">{group_id}</strong></p>
        <p style="margin: 5px 0; color: #666;">当前等级: <strong style="color: #d147a3; font-size: 1.2em;">{status}</strong></p>
    </div>
    <div style="display: flex; gap: 10px; margin-bottom: 15px;">
        <div style="flex: 1; background: white; padding: 10px; border-radius: 10px; border: 1px solid #ffb6c1; text-align: center;">
            <div style="font-size: 0.8em; color: #999;">好感分值</div>
            <div style="font-size: 1.4em; font-weight: bold; color: #d147a3;">{favorability:.2f}</div>
        </div>
        <div style="flex: 1; background: white; padding: 10px; border-radius: 10px; border: 1px solid #ffb6c1; text-align: center;">
            <div style="font-size: 0.8em; color: #999;">今日增长</div>
            <div style="font-size: 1.4em; font-weight: bold; color: #d147a3;">{daily_count:.2f}/10.00</div>
        </div>
    </div>
</div>
"""
    pic = await md_to_pic(md, width=450) if md_to_pic else None
    if pic: await group_fav_query.finish(MessageSegment.image(pic))
    else: await group_fav_query.finish(f"📊 群聊好感度\n群号：{group_id}\n当前好感：{favorability:.2f}\n当前等级：{status}")

set_group_fav = on_command("设置群好感", permission=SUPERUSER, priority=5, block=True)
@set_group_fav.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    if not SIGN_IN_AVAILABLE: await set_group_fav.finish("签到插件未就绪。")
    arg_str = args.extract_plain_text().strip()
    parts = arg_str.split()
    target_group, new_fav = "", 0.0
    if len(parts) == 1 and isinstance(event, GroupMessageEvent):
        target_group, new_fav = str(event.group_id), float(parts[0])
    elif len(parts) >= 2:
        target_group, new_fav = parts[0], float(parts[1])
    if target_group:
        update_user_data(f"group_{target_group}", favorability=new_fav)
        await set_group_fav.finish(f"✅ 已将群 {target_group} 的好感度设置为 {new_fav:.2f}")

set_persona = on_command("设置人设", permission=SUPERUSER, priority=5, block=True)
@set_persona.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    raw_text = args.extract_plain_text().strip()
    parts = raw_text.split(maxsplit=1)
    target_group_id, prompt = None, None
    if len(parts) == 2 and parts[0].isdigit():
        target_group_id, prompt = parts[0], parts[1]
    elif isinstance(event, GroupMessageEvent):
        target_group_id, prompt = str(event.group_id), raw_text
    if target_group_id and prompt:
        set_group_prompt(target_group_id, prompt)
        await set_persona.finish(f"已更新群 {target_group_id} 的人设。")

view_persona = on_command("查看人设", permission=SUPERUSER, priority=5, block=True)
@view_persona.handle()
async def _(bot: Bot, event: GroupMessageEvent):
    prompt = load_prompt(str(event.group_id))
    nodes = [{"type": "node", "data": {"name": "当前生效人设", "uin": str(bot.self_id), "content": str(prompt)}}]
    try: await bot.call_api("send_group_forward_msg", group_id=event.group_id, messages=nodes)
    except Exception: await view_persona.finish(f"当前生效人设：\n{prompt}")

reset_persona = on_command("重置人设", permission=SUPERUSER, priority=5, block=True)
@reset_persona.handle()
async def _(bot: Bot, event: GroupMessageEvent):
    set_group_prompt(str(event.group_id), None)
    await reset_persona.finish("已重置本群人设为默认配置。")

enable_personification = on_command("开启拟人", permission=SUPERUSER, priority=5, block=True)
@enable_personification.handle()
async def _(bot: Bot, event: GroupMessageEvent):
    set_group_enabled(str(event.group_id), True)
    await enable_personification.finish("本群拟人功能已开启。")

disable_personification = on_command("关闭拟人", permission=SUPERUSER, priority=5, block=True)
@disable_personification.handle()
async def _(bot: Bot, event: GroupMessageEvent):
    set_group_enabled(str(event.group_id), False)
    await disable_personification.finish("本群拟人功能已关闭。")

enable_stickers = on_command("开启表情包", permission=SUPERUSER, priority=5, block=True)
@enable_stickers.handle()
async def _(bot: Bot, event: GroupMessageEvent):
    set_group_sticker_enabled(str(event.group_id), True)
    await enable_stickers.finish("本群表情包功能已开启。")

disable_stickers = on_command("关闭表情包", permission=SUPERUSER, priority=5, block=True)
@disable_stickers.handle()
async def _(bot: Bot, event: GroupMessageEvent):
    set_group_sticker_enabled(str(event.group_id), False)
    await disable_stickers.finish("本群表情包功能已关闭。")

enable_schedule = on_command("拟人作息", permission=SUPERUSER, priority=5, block=True)
@enable_schedule.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    status = args.extract_plain_text().strip()
    if "全局" in status:
        config.personification_schedule_global = "开启" in status
        save_plugin_runtime_config()
        await enable_schedule.finish(f"拟人作息模拟已全局{'开启' if config.personification_schedule_global else '关闭'}。")
    elif isinstance(event, GroupMessageEvent):
        set_group_schedule_enabled(str(event.group_id), status == "开启")
        await enable_schedule.finish(f"本群作息模拟功能已{status}。")

view_config = on_command("拟人配置", permission=SUPERUSER, priority=5, block=True)
@view_config.handle()
async def _(bot: Bot, event: GroupMessageEvent):
    group_id = str(event.group_id)
    group_config = get_group_config(group_id)
    global_conf = f"API: {config.personification_api_type}\n模型: {config.personification_model}\n概率: {config.personification_probability}\n联网: {config.personification_web_search}"
    group_conf = f"当前群: {group_id}\n拟人: {group_config.get('enabled', '默认')}\n表情包: {group_config.get('sticker_enabled', True)}\n作息: {group_config.get('schedule_enabled', False)}"
    nodes = [{"type": "node", "data": {"name": "全局/群配置", "uin": str(bot.self_id), "content": f"{global_conf}\n\n{group_conf}"}}]
    try: await bot.call_api("send_group_forward_msg", group_id=event.group_id, messages=nodes)
    except Exception: await view_config.finish(f"{global_conf}\n\n{group_conf}")

@scheduler.scheduled_job("cron", hour=23, minute=59, id="personification_daily_fav_report")
async def daily_group_fav_report():
    if not SIGN_IN_AVAILABLE: return
    try:
        data, today, report_lines, total_increase = load_data(), datetime.now().strftime("%Y-%m-%d"), [], 0.0
        for uid, udata in data.items():
            if uid.startswith("group_") and not uid.startswith("group_private_") and udata.get("last_update") == today:
                daily_count = float(udata.get("daily_fav_count", 0.0))
                if daily_count > 0:
                    report_lines.append(f"群 {uid[6:]}: +{daily_count:.2f} (当前: {udata.get('favorability', 0.0):.2f})")
                    total_increase += daily_count
        if report_lines:
            summary = f"📊 【每日群聊好感度统计】\n日期: {today}\n总增长: {total_increase:.2f}\n\n" + "\n".join(report_lines)
            for b in get_bots().values():
                for su in superusers: await b.send_private_msg(user_id=int(su), message=summary)
    except Exception: pass

perm_blacklist_add = on_command("永久拉黑", permission=SUPERUSER, priority=5, block=True)
@perm_blacklist_add.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    if not SIGN_IN_AVAILABLE: await perm_blacklist_add.finish("签到插件未就绪。")
    target_id = args.extract_plain_text().strip()
    for seg in event.get_message():
        if seg.type == "at": target_id = str(seg.data["qq"])
    if target_id:
        update_user_data(target_id, is_perm_blacklisted=True)
        await perm_blacklist_add.finish(f"✅ 已将用户 {target_id} 加入永久黑名单。")

perm_blacklist_del = on_command("取消永久拉黑", permission=SUPERUSER, priority=5, block=True)
@perm_blacklist_del.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    if not SIGN_IN_AVAILABLE: await perm_blacklist_del.finish("签到插件未就绪。")
    target_id = args.extract_plain_text().strip()
    for seg in event.get_message():
        if seg.type == "at": target_id = str(seg.data["qq"])
    if target_id:
        update_user_data(target_id, is_perm_blacklisted=False)
        await perm_blacklist_del.finish(f"✅ 已将用户 {target_id} 从永久黑名单中移除。")

perm_blacklist_list = on_command("永久黑名单列表", permission=SUPERUSER, priority=5, block=True)
@perm_blacklist_list.handle()
async def _(bot: Bot, event: MessageEvent):
    if not SIGN_IN_AVAILABLE: await perm_blacklist_list.finish("签到插件未就绪。")
    data = load_data()
    items = [{"id": uid, "count": ud.get('blacklist_count', 0), "fav": ud.get('favorability', 0.0)} for uid, ud in data.items() if not uid.startswith("group_") and ud.get("is_perm_blacklisted", False)]
    if not items: await perm_blacklist_list.finish("目前没有永久黑名单用户。")
    msg = "🚫 永久黑名单列表 🚫\n" + "\n".join([f"- {i['id']} ({i['count']}次 / {i['fav']:.2f})" for i in items])
    await perm_blacklist_list.finish(msg)

async def auto_post_diary():
    if not QZONE_PUBLISH_AVAILABLE: return
    bots = get_bots()
    if not bots: return
    bot = list(bots.values())[0]
    diary = await generate_ai_diary(bot)
    if diary: await publish_qzone_shuo(diary, bot.self_id)

try:
    scheduler.add_job(auto_post_diary, "cron", day_of_week="sun", hour=21, minute=0, id="ai_weekly_diary", replace_existing=True)
except Exception: pass

manual_diary_cmd = on_command("发个说说", permission=SUPERUSER, priority=5, block=True)
@manual_diary_cmd.handle()
async def handle_manual_diary(bot: Bot):
    if not QZONE_PUBLISH_AVAILABLE: await manual_diary_cmd.finish("功能未就绪。")
    diary = await generate_ai_diary(bot)
    if diary:
        success, msg = await publish_qzone_shuo(diary, bot.self_id)
        await manual_diary_cmd.finish(f"{'✅' if success else '❌'} {msg if not success else '发布成功'}")

def save_plugin_runtime_config():
    path = Path("data/user_persona/runtime_config.json")
    data = {"web_search": config.personification_web_search, "schedule_global": config.personification_schedule_global, "proactive_enabled": config.personification_proactive_enabled}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f: json.dump(data, f, indent=4)
    except Exception: pass

def load_plugin_runtime_config():
    path = Path("data/user_persona/runtime_config.json")
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                config.personification_web_search = data.get("web_search", config.personification_web_search)
                config.personification_schedule_global = data.get("schedule_global", config.personification_schedule_global)
                config.personification_proactive_enabled = data.get("proactive_enabled", config.personification_proactive_enabled)
        except Exception: pass

load_plugin_runtime_config()

web_search_cmd = on_command("拟人联网", permission=SUPERUSER, priority=5, block=True)
@web_search_cmd.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    action = arg.extract_plain_text().strip()
    if action in ["开启", "关闭"]:
        config.personification_web_search = (action == "开启")
        save_plugin_runtime_config()
        await web_search_cmd.finish(f"功能已{action}。")

proactive_msg_switch_cmd = on_command("拟人主动消息", permission=SUPERUSER, priority=5, block=True)
@proactive_msg_switch_cmd.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    action = arg.extract_plain_text().strip()
    if action in ["开启", "关闭"]:
        config.personification_proactive_enabled = (action == "开启")
        save_plugin_runtime_config()
        await proactive_msg_switch_cmd.finish(f"功能已{action}。")

clear_context_cmd = on_command("清除记忆", aliases={"清除上下文", "重置记忆"}, permission=SUPERUSER, priority=5, block=True)
@clear_context_cmd.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    args_text = arg.extract_plain_text().strip()
    if args_text == "全局":
        chat_histories.clear()
        await clear_context_cmd.finish("已清除全局记忆。")
    target_id = args_text if args_text.isdigit() else (str(event.group_id) if isinstance(event, GroupMessageEvent) else f"private_{event.user_id}")
    if target_id in chat_histories:
        del chat_histories[target_id]
        await clear_context_cmd.finish(f"已清除 {target_id} 的记忆。")

PROACTIVE_STATE_PATH = Path("data/personification/proactive_state.json")
def load_proactive_state():
    if PROACTIVE_STATE_PATH.exists():
        try:
            with open(PROACTIVE_STATE_PATH, "r", encoding="utf-8") as f: return json.load(f)
        except Exception: return {}
    return {}
def save_proactive_state(data: dict):
    PROACTIVE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(PROACTIVE_STATE_PATH, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=4)

async def check_proactive_messaging(target_user_id: Optional[str] = None, bypass_checks: bool = False):
    if not bypass_checks and (not config.personification_proactive_enabled or not SIGN_IN_AVAILABLE or not is_rest_time()): return
    bots = get_bots()
    if not bots: return
    bot = list(bots.values())[0]
    all_data, proactive_state, today_str, current_ts = load_data(), load_proactive_state(), datetime.now().strftime("%Y-%m-%d"), time.time()
    if not target_user_id:
        candidates = [uid for uid, ud in all_data.items() if not uid.startswith("group_") and float(ud.get("favorability", 0.0)) >= config.personification_proactive_threshold and not ud.get("is_perm_blacklisted", False) and (current_ts - proactive_state.get(uid, {}).get("last_interaction", 0) > config.personification_proactive_interval * 60) and (proactive_state.get(uid, {}).get("last_date") != today_str or proactive_state.get(uid, {}).get("count", 0) < config.personification_proactive_daily_limit)]
        if not candidates: return
        target_user_id = random.choice(candidates)
    try:
        ud = get_user_data(target_user_id)
        base = load_prompt()
        prompt = f"{base}\n\n当前：{get_activity_status()}\n目标：{target_user_id}(好感:{ud.get('favorability', 0)})\n任务：生成一条私聊开启话题，20字以内，符合人设。"
        reply = await call_ai_api([{"role": "system", "content": prompt}], temperature=0.9)
        if reply:
            reply = re.sub(r'<.*?>', '', reply).strip()
            if reply and "[SILENCE]" not in reply:
                await bot.send_private_msg(user_id=int(target_user_id), message=reply)
                ps = load_proactive_state()
                u_ps = ps.get(target_user_id, {})
                count = u_ps.get("count", 0) if u_ps.get("last_date") == today_str else 0
                ps[target_user_id] = {"last_date": today_str, "count": count + 1, "last_interaction": time.time()}
                save_proactive_state(ps)
    except Exception: pass

try:
    scheduler.add_job(check_proactive_messaging, "interval", minutes=max(5, config.personification_proactive_interval), id="personification_proactive_messaging", replace_existing=True)
except Exception: pass

# 核心消息处理器
reply_matcher = on_message(rule=Rule(personification_rule), priority=100, block=True)
@reply_matcher.handle()
async def _(bot: Bot, event: MessageEvent, state: T_State):
    user_id = str(event.user_id)
    group_id = str(event.group_id) if isinstance(event, GroupMessageEvent) else f"private_{user_id}"
    key = f"{group_id}_{user_id}"
    if key in _msg_buffer:
        if "timer_task" in _msg_buffer[key]: _msg_buffer[key]["timer_task"].cancel()
        _msg_buffer[key]["events"].append(event)
    else: _msg_buffer[key] = {"events": [event], "state": state}
    _msg_buffer[key]["timer_task"] = asyncio.create_task(_buffer_timer(key, bot))

async def personification_rule(event: MessageEvent, state: T_State) -> bool:
    uid = str(event.user_id)
    if SIGN_IN_AVAILABLE and get_user_data(uid).get("is_perm_blacklisted"): return False
    if uid in user_blacklist:
        if time.time() < user_blacklist[uid]: return False
        del user_blacklist[uid]
    if isinstance(event, GroupMessageEvent):
        gid = str(event.group_id)
        if not is_group_whitelisted(gid, config.personification_whitelist): return False
        is_mentioned = event.to_me
        if not is_mentioned:
            prompt = load_prompt(gid)
            if isinstance(prompt, dict):
                names = [str(prompt.get("name", ""))] + [str(n) for n in prompt.get("nick_name", []) if n]
                msg = event.get_plaintext()
                is_mentioned = any(n in msg for n in names if n)
        if is_mentioned:
            state["is_random_chat"] = False
            return True
        prob = config.personification_probability * (0.2 if not is_rest_time() else 1.0)
        if random.random() < prob:
            state["is_random_chat"] = True
            return True
    return isinstance(event, PrivateMessageEvent)

async def handle_reply(bot: Bot, event: Event, state: T_State):
    await _process_response_logic(bot, event, state)

# 表情包水群
async def sticker_chat_rule(event: GroupMessageEvent) -> bool:
    if event.to_me: return False
    gid = str(event.group_id)
    return is_group_whitelisted(gid, config.personification_whitelist) and random.random() < config.personification_probability

sticker_chat_matcher = on_message(rule=Rule(sticker_chat_rule), priority=101, block=True)
@sticker_chat_matcher.handle()
async def _(bot: Bot, event: GroupMessageEvent, state: T_State):
    gid = str(event.group_id)
    gc = get_group_config(gid)
    mode = random.choice(["text_only", "sticker_only", "mixed"]) if gc.get("sticker_enabled", True) else "text_only"
    if mode == "sticker_only":
        sd = Path(config.personification_sticker_path or (store.get_plugin_data_dir() / "stickers"))
        ss = [f for f in sd.iterdir() if f.suffix.lower() in [".jpg", ".png", ".gif", ".webp", ".jpeg"]] if sd.exists() else []
        if ss: await bot.send(event, MessageSegment.image(f"file:///{random.choice(ss).absolute()}"))
        else: mode = "text_only"
    if mode in ["text_only", "mixed"]:
        state["is_random_chat"], state["force_mode"] = True, mode
        await _process_response_logic(bot, event, state)

# 戳一戳
async def poke_notice_rule(event: PokeNotifyEvent) -> bool:
    if event.target_id != event.self_id: return False
    gid = str(event.group_id)
    return is_group_whitelisted(gid, config.personification_whitelist) and random.random() < config.personification_poke_probability

poke_notice_matcher = on_notice(rule=Rule(poke_notice_rule), priority=10, block=False)
@poke_notice_matcher.handle()
async def _(bot: Bot, event: PokeNotifyEvent, state: T_State):
    await _process_response_logic(bot, event, state)

# 白名单申请
apply_whitelist = on_command("申请白名单", priority=5, block=True)
@apply_whitelist.handle()
async def _(bot: Bot, event: GroupMessageEvent):
    gid = str(event.group_id)
    if is_group_whitelisted(gid, config.personification_whitelist): await apply_whitelist.finish("已在白名单中。")
    gn = (await bot.get_group_info(group_id=int(gid))).get("group_name", "未知")
    if not add_request(gid, str(event.user_id), gn): await apply_whitelist.finish("申请审核中。")
    msg = f"收到白名单申请：\n群：{gn}({gid})\n申请人：{event.user_id}\n同意白名单 {gid}"
    for su in superusers:
        try: await bot.send_private_msg(user_id=int(su), message=msg)
        except Exception: pass
    await apply_whitelist.finish("申请已提交。")

agree_whitelist = on_command("同意白名单", permission=SUPERUSER, priority=5, block=True)
@agree_whitelist.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    gid = args.extract_plain_text().strip()
    if add_group_to_whitelist(gid):
        update_request_status(gid, "approved", str(event.user_id))
        await agree_whitelist.send(f"已同意群 {gid}。")
        try: await bot.send_group_msg(group_id=int(gid), message="拟人功能已激活！")
        except Exception: pass

add_whitelist = on_command("添加白名单", permission=SUPERUSER, priority=5, block=True)
@add_whitelist.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    gid = args.extract_plain_text().strip()
    if add_group_to_whitelist(gid): await add_whitelist.finish(f"已添加群 {gid}。")

remove_whitelist = on_command("移除白名单", permission=SUPERUSER, priority=5, block=True)
@remove_whitelist.handle()
async def _(args: Message = CommandArg()):
    gid = args.extract_plain_text().strip()
    if remove_group_from_whitelist(gid): await remove_whitelist.finish(f"已移除群 {gid}。")
