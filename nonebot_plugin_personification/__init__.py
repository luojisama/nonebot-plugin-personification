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
from nonebot import on_message, on_command, get_plugin_config, logger, get_driver, require, get_bots
from nonebot.typing import T_State
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, PrivateMessageEvent, Message, MessageSegment, MessageEvent, PokeNotifyEvent, Event
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule
from nonebot.exception import FinishedException
from openai import AsyncOpenAI

require("nonebot_plugin_localstore")
import nonebot_plugin_localstore as localstore

require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler

# 尝试导入空间发布函数 (已适配 bot_manager)
try:
    try:
        from plugin.bot_manager import publish_qzone_shuo, update_qzone_cookie
    except ImportError:
        from ..bot_manager import publish_qzone_shuo, update_qzone_cookie
    QZONE_PUBLISH_AVAILABLE = True
except ImportError:
    QZONE_PUBLISH_AVAILABLE = False

from .config import Config
from .utils import add_group_to_whitelist, remove_group_from_whitelist, is_group_whitelisted, add_request, update_request_status, get_group_config, set_group_prompt, set_group_sticker_enabled, set_group_enabled, set_group_schedule_enabled, record_group_msg, get_recent_group_msgs, set_group_style, get_group_style, clear_group_msgs
from .schedule import get_beijing_time, get_schedule_prompt_injection, is_rest_time, get_activity_status

# 尝试加载并导入 htmlrender
try:
    require("nonebot_plugin_htmlrender")
    from nonebot_plugin_htmlrender import md_to_pic
except Exception:
    md_to_pic = None

# 尝试导入签到插件的工具函数
try:
    try:
        from plugin.sign_in.utils import get_user_data, update_user_data, load_data
        from plugin.sign_in.config import get_level_name
    except ImportError:
        from ..sign_in.utils import get_user_data, update_user_data, load_data
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
    type="application",
    homepage="https://github.com/luojisama/nonebot-plugin-shiro-personification",
    supported_adapters={"nonebot.adapters.onebot.v11"},
)

plugin_config = get_plugin_config(Config)
superusers = get_driver().config.superusers
plugin_data_dir = localstore.get_plugin_data_dir()
personification_data_dir = plugin_data_dir / "personification"
user_persona_data_dir = plugin_data_dir / "user_persona"
RUNTIME_CONFIG_PATH = user_persona_data_dir / "runtime_config.json"
LEGACY_RUNTIME_CONFIG_PATH = Path("data/user_persona/runtime_config.json")
PROACTIVE_STATE_PATH = personification_data_dir / "proactive_state.json"
LEGACY_PROACTIVE_STATE_PATH = Path("data/personification/proactive_state.json")

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
        content = plugin_config.personification_system_prompt
        
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

WEB_SEARCH_TOOL_OPENAI = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "搜索互联网获取最新信息、新闻、知识等内容。当需要查找实时信息或不确定某个事实时调用此工具。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词，应简洁明确，支持中英文"
                }
            },
            "required": ["query"]
        }
    }
}

async def do_web_search(query: str) -> str:
    """使用 DuckDuckGo 执行网络搜索"""
    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
            # 优先尝试 Instant Answer API（结构化数据，无需解析 HTML）
            resp = await client.get(
                "https://api.duckduckgo.com/",
                params={"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"},
                headers={"User-Agent": "Mozilla/5.0 (compatible; PersonificationBot/1.0)"}
            )
            data = resp.json()

            results = []
            if data.get("AbstractText"):
                results.append(f"摘要: {data['AbstractText']}")
                if data.get("AbstractURL"):
                    results.append(f"来源: {data['AbstractURL']}")
            for topic in data.get("RelatedTopics", [])[:5]:
                if isinstance(topic, dict) and topic.get("Text"):
                    results.append(f"- {topic['Text']}")

            if results:
                logger.info(f"拟人插件：联网搜索(即时答案) '{query}' 返回 {len(results)} 条结果")
                return "\n".join(results)

            # Instant Answer 无结果时，退回 HTML 搜索并提取摘要
            resp2 = await client.post(
                "https://html.duckduckgo.com/html/",
                data={"q": query},
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            )
            snippets = re.findall(r'class="result__snippet">(.*?)</a>', resp2.text, re.DOTALL)
            if snippets:
                cleaned = [re.sub(r"<[^>]+>", "", s).strip() for s in snippets[:5] if s.strip()]
                logger.info(f"拟人插件：联网搜索(HTML) '{query}' 返回 {len(cleaned)} 条摘要")
                return "搜索结果:\n" + "\n".join(f"- {s}" for s in cleaned)

            return f"未找到关于 {query} 的搜索结果。"
    except Exception as e:
        logger.error(f"拟人插件：联网搜索失败: {e}")
        return f"搜索失败，请稍后再试。"


async def call_ai_api(messages: List[Dict], tools: Optional[List[Dict]] = None, max_tokens: Optional[int] = None, temperature: float = 0.7) -> Optional[str]:
    """通用 AI API 调用函数，支持工具调用"""
    if not plugin_config.personification_api_key:
        logger.warning("拟人插件：未配置 API Key，跳过调用")
        return None

    try:
        # 1. 智能处理 API URL
        api_url = plugin_config.personification_api_url.strip()
        api_type = plugin_config.personification_api_type.lower()
        
        # --- Gemini 官方格式调用分支 ---
        if api_type == "gemini_official":
            # 构造 Gemini 官方请求格式
            # 参考: https://ai.google.dev/api/rest/v1beta/models/generateContent
            
            # 自动识别模型 ID
            model_id = plugin_config.personification_model
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
            if plugin_config.personification_thinking_budget > 0:
                payload["generationConfig"]["thinkingConfig"] = {
                    "includeThoughts": plugin_config.personification_include_thoughts,
                    "thinkingBudget": plugin_config.personification_thinking_budget
                }

            # 支持 Grounding (联网) 配置：根据报错建议，使用 google_search 代替 googleSearchRetrieval
            if plugin_config.personification_web_search:
                # Gemini API 规范: tools 必须包含 function_declarations, google_search_retrieval, code_execution 中的一个
                # 对于搜索，我们使用 google_search_retrieval (早期) 或 google_search (新版)
                # 错误提示 "tools[0].tool_type: required one_of 'tool_type' must have one initialized field"
                # 意味着 google_search 字段可能不被识别，或者结构不对
                # 尝试标准结构: {"googleSearchRetrieval": {}} (注意大小写)
                # 或者 {"google_search": {}} (REST API)
                # 让我们尝试使用 googleSearch (REST API v1beta)
                payload["tools"] = [{"googleSearch": {}}]
            
            # 优化认证逻辑：避免 Header 和 URL 同时携带 Key 导致 400 错误
            headers = {"Content-Type": "application/json"}
            
            # 如果 URL 里没 key 参数，则优先通过 Header 或 URL 注入（二选一）
            if "key=" not in api_url and plugin_config.personification_api_key:
                # 某些中转站喜欢 URL 里的 key，某些喜欢 Header
                # 这里根据你提供的 YAML，默认使用 Header，但如果失败可以尝试把 key 加到 URL
                connector = "&" if "?" in api_url else "?"
                api_url += f"{connector}key={plugin_config.personification_api_key}"
            elif plugin_config.personification_api_key:
                # 如果 URL 里已经有 Key 了，我们就不在 Header 里发 Authorization 了
                pass
            else:
                # 如果都没有，尝试发 Bearer (兼容某些特殊中转)
                headers["Authorization"] = f"Bearer {plugin_config.personification_api_key}"

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
                                        # 可以选择记录日志，但不在群里发送
                                        # logger.debug(f"拟人插件：过滤思考过程: {part.get('text', '')[:50]}...")
                                        continue
                                    
                                    # 2. 拼接文本
                                    if "text" in part:
                                        reply_text += part["text"]
                                
                                # 3. 统一过滤 XML 风格的思考标签 (Gemini 常见格式)
                                # 例如 <thought>...</thought> 或 <thinking>...</thinking>
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
                api_key=plugin_config.personification_api_key,
                base_url=api_url,
                http_client=http_client
            )
            
            max_iterations = 5
            iteration = 0
            reply_content = ""

            # 过滤掉内部元数据 (如 user_id)
            current_messages = []
            for msg in messages:
                clean_msg = {k: v for k, v in msg.items() if k in ["role", "content", "name", "tool_calls", "tool_call_id"]}
                current_messages.append(clean_msg)

            # 合并外部传入工具与联网工具
            openai_tools = list(tools) if tools else []
            if plugin_config.personification_web_search:
                openai_tools.append(WEB_SEARCH_TOOL_OPENAI)

            while iteration < max_iterations:
                iteration += 1

                call_params = {
                    "model": plugin_config.personification_model,
                    "messages": current_messages,
                    "temperature": temperature
                }
                if max_tokens:
                    call_params["max_tokens"] = max_tokens
                if openai_tools:
                    call_params["tools"] = openai_tools
                    call_params["tool_choice"] = "auto"

                response = await client.chat.completions.create(**call_params)

                if isinstance(response, str):
                    reply_content = response.strip()
                    break

                msg = response.choices[0].message

                if msg.tool_calls:
                    current_messages.append(msg)
                    for tool_call in msg.tool_calls:
                        tool_name = tool_call.function.name
                        tool_args = json.loads(tool_call.function.arguments)

                        logger.info(f"拟人插件：AI 正在调用工具 {tool_name} 参数: {tool_args}")

                        if tool_name == "web_search":
                            result = await do_web_search(tool_args.get("query", ""))
                        else:
                            result = f"Error: Tool {tool_name} not found."

                        current_messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_name,
                            "content": result
                        })
                    continue
                else:
                    reply_content = (msg.content or "").strip()
                    break
            
            return reply_content

    except Exception as e:
        logger.error(f"AI 调用失败: {e}")
        return None

async def personification_rule(event: MessageEvent, state: T_State) -> bool:
    user_id = str(event.user_id)
    
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

    if isinstance(event, GroupMessageEvent):
        group_id = str(event.group_id)
        # 检查是否在白名单中
        if not is_group_whitelisted(group_id, plugin_config.personification_whitelist):
            return False
        
        # 检查名字提及 (针对 YAML 配置)
        is_name_mentioned = False
        try:
            # 加载配置以获取名字
            # 注意：频繁读取可能会有性能影响，但在当前架构下这是获取群组特定配置的唯一方式
            prompt_data = load_prompt(group_id)
            if isinstance(prompt_data, dict):
                names = []
                # 获取 name
                if "name" in prompt_data and prompt_data["name"]:
                    names.append(str(prompt_data["name"]))
                # 获取 nick_name
                if "nick_name" in prompt_data and isinstance(prompt_data["nick_name"], list):
                    names.extend([str(n) for n in prompt_data["nick_name"] if n])
                
                # 检查消息文本是否包含名字
                msg_text = event.get_plaintext()
                for name in names:
                    if name in msg_text:
                        is_name_mentioned = True
                        break
        except Exception as e:
            logger.warning(f"拟人插件: 检查名字提及失败: {e}")

        # 如果是艾特机器人 或 提到名字，则视为直接交互 (100% 触发)
        if event.to_me or is_name_mentioned:
            state["is_random_chat"] = False # 标记为非随机 (直接交互)
            return True
            
        # 根据概率决定是否触发 (随机水群模式)
        # 作息时间判断：如果不是休息时间（上课、深夜等），将触发概率降低至 20%
        # allow_unsuitable_prob=0.0 表示严格检查是否为休息时间
        is_unsuitable_time = not is_rest_time(allow_unsuitable_prob=0.0)
        
        base_prob = plugin_config.personification_probability
        
        if is_unsuitable_time:
            # 如果是不适合的时间，概率降低为原来的 20%
            current_prob = base_prob * 0.2
        else:
            current_prob = base_prob

        if random.random() < current_prob:
            state["is_random_chat"] = True # 标记为随机触发
            return True
        
        return False
    
    elif isinstance(event, PrivateMessageEvent):
        # 私聊始终响应（除黑名单外）
        return True
    
    return False

# 注册消息处理器，优先级设为 100，如果是艾特或概率触发则阻断
reply_matcher = on_message(rule=Rule(personification_rule), priority=100, block=True)

# 注册群聊消息记录器 (优先级最低，只记录不阻断)
# 必须单独注册，不能放在 reply_matcher 中，因为 personification_rule 会拦截不符合条件的消息
async def record_msg_rule(event: GroupMessageEvent) -> bool:
    return True

record_msg_matcher = on_message(rule=Rule(record_msg_rule), priority=999, block=False)

@record_msg_matcher.handle()
async def _(bot: Bot, event: GroupMessageEvent):
    # 记录群聊消息 (用于学习群聊风格)
    raw_msg = event.get_plaintext().strip()
    
    # 记录消息
    if raw_msg and not raw_msg.startswith("/") and len(raw_msg) < 500:
         group_id = str(event.group_id)
         user_id = str(event.user_id)
         
         # 优先使用改名卡设置的称号
         nickname = event.sender.card or event.sender.nickname or user_id
         try:
             # 尝试从签到插件获取自定义称号
             # 动态导入以避免循环依赖
             try:
                 from plugin.sign_in.utils import get_user_data
                 user_data = get_user_data(user_id)
                 custom_title = user_data.get("custom_title")
                 if custom_title:
                     nickname = custom_title
             except ImportError:
                 # 如果是在同级目录
                 from ..sign_in.utils import get_user_data
                 user_data = get_user_data(user_id)
                 custom_title = user_data.get("custom_title")
                 if custom_title:
                     nickname = custom_title
         except Exception as e:
             # 忽略获取失败，回退到默认昵称
             # logger.debug(f"拟人插件：获取自定义称号失败: {e}")
             pass
             
         count = record_group_msg(group_id, nickname, raw_msg)
         
         # 自动触发分析：满 200 条记录
         if count >= 200:
             # 使用 asyncio.create_task 将耗时任务放入后台运行，避免阻塞当前消息处理
             # 同时避免因为 await analyze_group_style 耗时过长导致 bot 掉线
             logger.info(f"拟人插件：群 {group_id} 消息已满 200 条，已创建后台任务进行风格分析...")
             asyncio.create_task(background_analyze_style(group_id))

async def background_analyze_style(group_id: str):
    """后台运行风格分析任务"""
    try:
        # 增加随机延迟，避免多个群同时触发
        await asyncio.sleep(random.uniform(1, 5))
        
        style_desc = await analyze_group_style(group_id)
        if style_desc:
            set_group_style(group_id, style_desc)
            # 自动触发不发送消息通知，避免干扰，但清空记录
            clear_group_msgs(group_id)
            logger.info(f"拟人插件：群 {group_id} 风格自动学习完成并已清空记录。")
    except Exception as e:
        logger.error(f"拟人插件：自动学习群 {group_id} 风格失败: {e}")

# 注册申请白名单命令
apply_whitelist = on_command("申请白名单", priority=5, block=True)

@apply_whitelist.handle()
async def handle_apply_whitelist(bot: Bot, event: GroupMessageEvent):
    group_id = str(event.group_id)
    
    if is_group_whitelisted(group_id, plugin_config.personification_whitelist):
        await apply_whitelist.finish("本群已经在白名单中啦！")
        
    group_info = await bot.get_group_info(group_id=int(group_id))
    group_name = group_info.get("group_name", "未知群聊")
    
    # 尝试添加申请记录
    if not add_request(group_id, str(event.user_id), group_name):
        await apply_whitelist.finish("已有申请正在审核中，请勿重复提交~")
    
    msg = f"收到白名单申请：\n群名称：{group_name}\n群号：{group_id}\n申请人：{event.user_id}\n\n请回复：\n同意白名单 {group_id}\n拒绝白名单 {group_id}"
    
    sent_count = 0
    for superuser in superusers:
        try:
            await bot.send_private_msg(user_id=int(superuser), message=msg)
            sent_count += 1
        except Exception as e:
            logger.error(f"发送申请通知给超级用户 {superuser} 失败: {e}")
    
    if sent_count > 0:
        await apply_whitelist.finish("已向管理员发送申请，请耐心等待审核~")
    else:
        await apply_whitelist.finish("发送申请失败，未能联系到管理员。")

agree_whitelist = on_command("同意白名单", permission=SUPERUSER, priority=5, block=True)

@agree_whitelist.handle()
async def handle_agree_whitelist(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    group_id = args.extract_plain_text().strip()
    if not group_id:
        await agree_whitelist.finish("请提供群号！")
        
    if add_group_to_whitelist(group_id):
        update_request_status(group_id, "approved", str(event.user_id))
        await agree_whitelist.send(f"已将群 {group_id} 加入白名单。")
        try:
            await bot.send_group_msg(group_id=int(group_id), message="🎉 本群申请已通过，拟人功能已激活，快来和我聊天吧~")
        except Exception as e:
            logger.error(f"发送入群通知失败: {e}")
            await agree_whitelist.finish(f"已加入白名单，但发送群通知失败: {e}")
    else:
        await agree_whitelist.finish(f"群 {group_id} 已在白名单中。")

reject_whitelist = on_command("拒绝白名单", permission=SUPERUSER, priority=5, block=True)

@reject_whitelist.handle()
async def handle_reject_whitelist(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    group_id = args.extract_plain_text().strip()
    if not group_id:
        await reject_whitelist.finish("请提供群号！")
    
    update_request_status(group_id, "rejected", str(event.user_id))
    await reject_whitelist.send(f"已拒绝群 {group_id} 的申请。")
    try:
        await bot.send_group_msg(group_id=int(group_id), message="❌ 本群白名单申请未通过。")
    except Exception as e:
        logger.error(f"发送拒绝通知失败: {e}")

add_whitelist = on_command("添加白名单", permission=SUPERUSER, priority=5, block=True)

@add_whitelist.handle()
async def handle_add_whitelist(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    group_id = args.extract_plain_text().strip()
    if not group_id:
        await add_whitelist.finish("请提供群号！")
        
    if add_group_to_whitelist(group_id):
        # 尝试更新申请状态为 approved，如果有的话，保持数据一致性
        update_request_status(group_id, "approved", str(event.user_id))
        
        await add_whitelist.send(f"已将群 {group_id} 添加到白名单。")
        try:
            await bot.send_group_msg(group_id=int(group_id), message="🎉 本群已启用拟人功能，快来和我聊天吧~")
        except Exception as e:
            logger.error(f"发送入群通知失败: {e}")
            await add_whitelist.finish(f"已加入白名单，但发送群通知失败: {e}")
    else:
        await add_whitelist.finish(f"群 {group_id} 已在白名单中。")

remove_whitelist = on_command("移除白名单", permission=SUPERUSER, priority=5, block=True)

@remove_whitelist.handle()
async def handle_remove_whitelist(args: Message = CommandArg()):
    group_id = args.extract_plain_text().strip()
    if not group_id:
        await remove_whitelist.finish("请提供群号！")
        
    if remove_group_from_whitelist(group_id):
        await remove_whitelist.finish(f"已将群 {group_id} 移出白名单。")
    else:
        await remove_whitelist.finish(f"群 {group_id} 不在白名单中（若是配置文件的白名单则无法动态移除）。")

# 注册表情包水群处理器
async def sticker_chat_rule(event: GroupMessageEvent) -> bool:
    # 如果是艾特机器人，由 reply_matcher 负责处理，此处返回 False 避免重复触发
    if event.to_me:
        return False
        
    group_id = str(event.group_id)
    if not is_group_whitelisted(group_id, plugin_config.personification_whitelist):
        return False
    # 概率与随机回复一致
    return random.random() < plugin_config.personification_probability

sticker_chat_matcher = on_message(rule=Rule(sticker_chat_rule), priority=101, block=True)

@sticker_chat_matcher.handle()
async def _(bot: Bot, event: GroupMessageEvent, state: T_State):
    group_id = str(event.group_id)
    group_config = get_group_config(group_id)
    sticker_enabled = group_config.get("sticker_enabled", True)

    # 如果禁用了表情包，只能选纯文本模式
    if not sticker_enabled:
        mode = "text_only"
    else:
        # 随机选择一种水群模式 (三种模式概率各 1/3)
        mode = random.choice(["text_only", "sticker_only", "mixed"])
    
    sticker_dir = Path(plugin_config.personification_sticker_path)
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
    if not is_group_whitelisted(group_id, plugin_config.personification_whitelist):
        return False
    # 使用配置的概率响应
    return random.random() < plugin_config.personification_poke_probability

# 注意：v11 的戳一戳通常是 Notify 事件，但在一些实现中可能作为消息
from nonebot import on_notice

async def poke_notice_rule(event: PokeNotifyEvent) -> bool:
    # 打印调试信息，确认事件是否到达
    logger.info(f"收到戳一戳事件: target_id={event.target_id}, self_id={event.self_id}")
    if event.target_id != event.self_id:
        return False
    group_id = str(event.group_id)
    if not is_group_whitelisted(group_id, plugin_config.personification_whitelist):
        logger.info(f"群 {group_id} 不在白名单 {plugin_config.personification_whitelist} 或动态白名单中")
        return False
    # 使用配置的概率响应
    prob = plugin_config.personification_poke_probability
    res = random.random() < prob
    logger.info(f"戳一戳响应判定: 概率={prob}, 结果={res}")
    return res

poke_notice_matcher = on_notice(rule=Rule(poke_notice_rule), priority=10, block=False)

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
                # 这种情况理论上少见，除非是连续的分隔符后跟文本，或者上一个分隔符被截断
                segments.append(buffer)
                buffer = ""
            buffer = part
            
    if buffer:
        segments.append(buffer)
        
    return segments

async def _buffer_timer(key: str, bot: Bot):
    # 等待 3 秒（用户输入缓冲，收到新消息会重置此时钟）
    delay = 3.0
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

@reply_matcher.handle()
@poke_notice_matcher.handle()
async def handle_reply(bot: Bot, event: Event, state: T_State):
    # 如果是戳一戳，直接处理
    if isinstance(event, PokeNotifyEvent):
        await _process_response_logic(bot, event, state)
        return

    # 消息缓冲逻辑
    if isinstance(event, MessageEvent):
        user_id = str(event.user_id)
        if isinstance(event, GroupMessageEvent):
            group_id = str(event.group_id)
        else:
            group_id = f"private_{user_id}"
            
        key = f"{group_id}_{user_id}"
        
        # 如果已有缓冲，取消旧定时器
        if key in _msg_buffer:
            if "timer_task" in _msg_buffer[key]:
                _msg_buffer[key]["timer_task"].cancel()
            _msg_buffer[key]["events"].append(event)
        else:
            _msg_buffer[key] = {
                "events": [event],
                "state": state
            }
            
        # 启动新定时器
        task = asyncio.create_task(_buffer_timer(key, bot))
        _msg_buffer[key]["timer_task"] = task
        logger.debug(f"拟人插件：已缓冲用户 {user_id} 的消息，等待后续...")

def extract_xml_content(text: str, tag: str) -> Optional[str]:
    """提取 XML 标签内容，支持多行，支持属性"""
    # 匹配 <tag ...>content</tag>
    # 使用 IGNORECASE 忽略大小写，允许标签内空格
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
        # 如果没有找到 <output> 标签，尝试在全文查找 <message>
        # 这可以兼容 AI 忘记写 <output> 标签的情况
        output_content = response

    if output_content:
        # 提取 message 标签
        # 宽松匹配 <message ...>content</message>，允许结束标签空格
        msg_pattern = r'<message(.*?)>(.*?)</\s*message\s*>'
        matches = re.finditer(msg_pattern, output_content, re.DOTALL | re.IGNORECASE)
        for match in matches:
            attrs = match.group(1)
            content = match.group(2).strip()
            
            # 尝试从 attrs 中提取 quote
            quote_id = None
            quote_match = re.search(r'quote=["\']([^"\']*)["\']', attrs)
            if quote_match:
                quote_id = quote_match.group(1)
            
            # 提取 sticker
            sticker_url = extract_xml_content(content, "sticker")
            if sticker_url:
                # 移除 sticker 标签，保留剩余文本，允许结束标签空格
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
    # 使用东京时间 (UTC+9) 以符合日本角色作息
    now = datetime.now(timezone(timedelta(hours=9)))
    week_days = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    weekday_str = week_days[now.weekday()]
    current_time_str = f"{now.year}年{now.month:02d}月{now.day:02d}日 {now.hour:02d}:{now.minute:02d}:{now.second:02d} ({weekday_str}) [东京时间]"
    
    # 获取或初始化 Bot 状态
    current_status = bot_statuses.get(group_id)
    if not current_status:
        # 尝试从 YAML status 字段获取初始状态
        current_status = prompt_config.get("status", "").strip()
        # 如果 YAML 中没有，使用默认模板
        if not current_status:
             current_status = '心情: "平静"\n状态: "正在潜水"\n记忆: ""\n动作: "发呆"'
        bot_statuses[group_id] = current_status

    # 构建最近消息 history_new
    # 使用所有可用历史记录（已在 handle_reply 中根据 limit 截断），格式化为文本
    history_new_text = ""
    # 排除最后一条(即当前消息，单独处理)，保留剩余所有历史
    recent_msgs = chat_history[:-1] if len(chat_history) > 1 else [] 
    
    for msg in recent_msgs:
        role = msg["role"]
        content = msg["content"]
        
        # 处理 content 为 list 的情况 (图片)
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
            # content 已经包含了 [Name(QQ)]: 前缀
            history_new_text += f"{text_content}\n"
        elif role == "assistant":
            # 移除可能的 [发送了表情包...] 后缀用于显示
            clean_content = re.sub(r' \[发送了表情包:.*?\]', '', text_content)
            history_new_text += f"[我]: {clean_content}\n"
            
    if not history_new_text:
        history_new_text = "(无最近消息)"

    # 构建最后消息 history_last
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

    # 构建 System Prompt
    system_prompt = prompt_config.get("system", "")
    
    # 注入作息表 (如果启用)
    group_config = get_group_config(group_id)
    schedule_enabled = group_config.get("schedule_enabled", False)
    global_schedule_enabled = plugin_config.personification_schedule_global
    
    schedule_instruction = "2. **时间锚定**：忽略作息时间限制。你随时都可以自由回复，不需要担心上课或睡觉的问题。"
    system_schedule_instruction = ""
    
    if schedule_enabled or global_schedule_enabled:
        # schedule_prompt_part 已经包含了 "## 作息时间参考（生活规律）..." 这一段
        system_schedule_instruction = get_schedule_prompt_injection()
        # system_prompt += f"\n\n{schedule_prompt_part}" # 移除旧的注入方式，改用模板替换
        schedule_instruction = "2. **时间锚定**：参考【当前时间】判断作息状态。**作息状态仅作为回复的背景设定（占比约20%），主要精力应放在回应对方的内容上。**如果当前是上课或深夜（非休息时间），你回复了消息说明你正在“偷偷玩手机”或“熬夜”，请表现出这种紧张感或困意。"

    # 替换 System 模板变量
    system_prompt = system_prompt.replace("{system_schedule_instruction}", system_schedule_instruction)

    # 替换 Input 模板变量
    input_template = prompt_config.get("input", "")
    
    # 简单的替换逻辑
    input_text = input_template.replace("{trigger_reason}", trigger_reason)
    input_text = input_text.replace("{time}", current_time_str)
    input_text = input_text.replace("{history_new}", history_new_text)
    input_text = input_text.replace("{history_last}", history_last_text)
    input_text = input_text.replace("{status}", current_status)
    input_text = input_text.replace("{schedule_instruction}", schedule_instruction)
    
    # 处理 long_memory
    input_text = input_text.replace("{long_memory('guild')}", "(暂无长期记忆)")
    
    # 2. 构建 API 消息列表
    user_content = input_text
    
    # 检查 chat_history 最后一条消息是否包含图片
    last_msg = chat_history[-1]
    last_images = []
    if isinstance(last_msg["content"], list):
        for item in last_msg["content"]:
            if item["type"] == "image_url":
                # item["image_url"] 可能是 {"url": "..."}
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
    
    # 3. 调用 API
    reply_content = await call_ai_api(messages)
    
    if not reply_content:
        logger.warning("拟人插件 (YAML): 未能获取到 AI 回复内容")
        return

    # 4. 解析响应
    parsed = parse_yaml_response(reply_content)
    
    # 检查全局 [SILENCE] (可能在标签外)
    if "[SILENCE]" in reply_content:
        logger.info(f"AI (YAML) 决定保持沉默 (SILENCE)")
        return

    # 更新状态
    if parsed["status"]:
        bot_statuses[group_id] = parsed["status"]
        logger.info(f"拟人插件: 更新状态为: {parsed['status']}")
        
    # 记录思考过程
    if parsed["think"]:
        logger.debug(f"拟人插件: 思考过程: {parsed['think']}")

    # 处理动作
    if parsed["action"]:
        action_text = parsed["action"]
        logger.info(f"拟人插件: 执行动作: {action_text}")
        if "戳一戳" in action_text:
            try:
                # 尝试发送戳一戳 (需要 adapter 支持，这里假设 OneBot V11)
                # 注意：OneBot V11 的戳一戳通常是 NudgeEvent，发送可能需要 segment
                # 但 send 接口通常接受 MessageSegment
                await bot.send(event, MessageSegment.poke(int(user_id)))
            except Exception as e:
                logger.warning(f"拟人插件: 发送戳一戳失败: {e}")

    # 处理输出消息
    if parsed["messages"]:
        for msg in parsed["messages"]:
            text = msg["text"]
            sticker_url = msg["sticker"]
            # quote_id = msg["quote"] # 暂未处理引用
            
            # 过滤文本中的控制字符
            if text:
                text = text.replace("[SILENCE]", "").replace("[氛围好]", "").strip()
            
            # 发送文本
            if text:
                # 模拟分段发送
                segments = re.split(r'([。！？\n])', text)
                # 合并标点符号到上一段
                merged_segments = []
                current_seg = ""
                for s in segments:
                    if s in "。！？\n":
                        current_seg += s
                        if current_seg.strip():
                            merged_segments.append(current_seg)
                        current_seg = ""
                    else:
                        current_seg += s
                if current_seg.strip():
                    merged_segments.append(current_seg)
                
                # 如果没有标点，直接作为一段
                if not merged_segments and text.strip():
                    merged_segments = [text]

                for seg in merged_segments:
                    if seg.strip():
                        await bot.send(event, seg)
                        # 随机延迟，模拟打字
                        await asyncio.sleep(random.uniform(0.5, 2.0))
            
            # 发送表情包 (如果有 URL)
            if sticker_url:
                try:
                    # 如果是 http 链接
                    if sticker_url.startswith("http"):
                        await bot.send(event, MessageSegment.image(sticker_url))
                    # 如果是本地文件名 (假设不带路径)
                    else:
                        sticker_dir = Path(plugin_config.personification_sticker_path)
                        # 尝试匹配文件名
                        target_file = None
                        if sticker_dir.exists():
                             # 精确匹配
                             possible = sticker_dir / sticker_url
                             if possible.exists():
                                 target_file = possible
                             else:
                                 # 模糊匹配 stem
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
        # 如果解析失败，可能是 AI 没有遵循格式，直接发送原始内容
        # 尝试移除所有可能的 XML 标签
        clean_reply = reply_content
        for tag in ["status", "think", "action", "output", "message"]:
            # 移除带内容的标签 (如 <status>...</status>)
            clean_reply = re.sub(rf'<{tag}.*?>.*?</\s*{tag}\s*>', '', clean_reply, flags=re.DOTALL | re.IGNORECASE)
            # 移除孤立的标签 (如 <message>)
            clean_reply = re.sub(rf'</?\s*{tag}.*?>', '', clean_reply, flags=re.IGNORECASE)
        
        # 移除控制字符
        clean_reply = clean_reply.replace("[SILENCE]", "").replace("[氛围好]", "").strip()
        
        if clean_reply:
             await bot.send(event, clean_reply)
             
    # 记录 Assistant 回复到历史 (为了 history_new)
    # 我们只记录文本内容，忽略复杂的 XML 结构
    assistant_text = ""
    if parsed["messages"]:
        assistant_text = " ".join([m["text"] for m in parsed["messages"] if m["text"]])
    else:
        assistant_text = clean_reply
        
    chat_history.append({"role": "assistant", "content": assistant_text})

async def _process_response_logic(bot: Bot, event: Event, state: T_State):
    # 消息去重逻辑
    if hasattr(event, "message_id"):
        if is_msg_processed(event.message_id):
            return

    # 如果是通知事件，需要特殊处理
    is_poke = False
    user_id = ""
    group_id = 0
    message_content = ""
    sender_name = ""
    trigger_reason = ""
    image_urls = []
    
    # 从 state 获取可能的参数
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
            # 优先获取 QQ 昵称作为真实身份标识，避免群名片包含冒号或对话内容导致 AI 混淆
            sender_name = event.sender.nickname or event.sender.card or user_id
            
            # 尝试获取自定义称号 (优先于 QQ 昵称和群名片)
            try:
                # 动态导入以避免循环依赖
                try:
                    from plugin.sign_in.utils import get_user_data
                    user_data = get_user_data(user_id)
                    custom_title = user_data.get("custom_title")
                    if custom_title:
                        sender_name = custom_title
                except ImportError:
                    from ..sign_in.utils import get_user_data
                    user_data = get_user_data(user_id)
                    custom_title = user_data.get("custom_title")
                    if custom_title:
                        sender_name = custom_title
            except Exception:
                pass
        else:
            # 私聊上下文使用 private_ 前缀
            group_id = f"private_{user_id}"
            sender_name = event.sender.nickname or user_id
            
            # 私聊也尝试获取自定义称号
            try:
                try:
                    from plugin.sign_in.utils import get_user_data
                    user_data = get_user_data(user_id)
                    custom_title = user_data.get("custom_title")
                    if custom_title:
                        sender_name = custom_title
                except ImportError:
                    from ..sign_in.utils import get_user_data
                    user_data = get_user_data(user_id)
                    custom_title = user_data.get("custom_title")
                    if custom_title:
                        sender_name = custom_title
            except Exception:
                pass
        
        # 提取文本和图片
        message_text = ""
        
        source_message = state.get("concatenated_message", event.message)
        for seg in source_message:
            if seg.type == "text":
                message_text += seg.data.get("text", "")
            elif seg.type == "face":
                # QQ默认表情
                face_id = seg.data.get("id", "")
                message_text += f"[表情id:{face_id}]"
            elif seg.type == "mface":
                # 市场表情
                summary = seg.data.get("summary", "表情包")
                message_text += f"[{summary}]"
            elif seg.type == "image":
                url = seg.data.get("url")
                file_name = seg.data.get("file", "").lower()
                if url:
                    try:
                        # 尝试将图片转换为 base64 以提高 AI 兼容性 (特别是 Gemini)
                        async with httpx.AsyncClient() as client:
                            resp = await client.get(url, timeout=10)
                            if resp.status_code == 200:
                                mime_type = resp.headers.get("Content-Type", "image/jpeg")
                                # 如果是 GIF，忽略并不予回复
                                if "image/gif" in mime_type or file_name.endswith(".gif"):
                                    logger.info("拟人插件：检测到 GIF 图片，忽略并不予回复")
                                    continue
                                
                                # 尝试识别图片类型（表情包 vs 照片）
                                try:
                                    img_obj = Image.open(BytesIO(resp.content))
                                    w, h = img_obj.size
                                    # 判定标准：尺寸较小通常为表情包，放宽至 1280 以兼容高清梗图
                                    if w <= 1280 and h <= 1280:
                                        message_text += "[发送了一个表情包]"
                                    else:
                                        message_text += "[发送了一张图片]"
                                except Exception as e:
                                     logger.warning(f"识别图片尺寸失败: {e}")
                                     message_text += "[发送了一张图片]"

                                base64_data = base64.b64encode(resp.content).decode("utf-8")
                                image_urls.append(f"data:{mime_type};base64,{base64_data}")
                            else:
                                # 如果下载失败，且不是 GIF，保留原 URL 作为备选
                                if not file_name.endswith(".gif"):
                                    message_text += "[发送了一张图片]"
                                    image_urls.append(url)
                    except Exception as e:
                        logger.warning(f"下载图片失败，保留原 URL: {e}")
                        if not file_name.endswith(".gif"):
                            message_text += "[发送了一张图片]"
                            image_urls.append(url)
        
        # 处理引用消息
        reply = getattr(event, "reply", None)
        if reply:
            reply_msg = getattr(reply, "message", None) or (reply.get("message") if isinstance(reply, dict) else None)
            if reply_msg:
                message_text += "\n[引用内容]: "
                try:
                    # 确保 reply_msg 是可迭代的
                    if isinstance(reply_msg, (list, tuple, Message)):
                        for seg in reply_msg:
                            seg_type = getattr(seg, "type", None) or (seg.get("type") if isinstance(seg, dict) else None)
                            data = getattr(seg, "data", None) or (seg.get("data") if isinstance(seg, dict) else {})
                            
                            if seg_type == "text":
                                message_text += data.get("text", "")
                            elif seg_type == "image":
                                url = data.get("url")
                                file_name = data.get("file", "").lower()
                                if url:
                                    try:
                                        # 尝试将图片转换为 base64
                                        async with httpx.AsyncClient() as client:
                                            resp = await client.get(url, timeout=10)
                                            if resp.status_code == 200:
                                                mime_type = resp.headers.get("Content-Type", "image/jpeg")
                                                if "image/gif" in mime_type or file_name.endswith(".gif"):
                                                    continue
                                                
                                                message_text += "[图片]"
                                                base64_data = base64.b64encode(resp.content).decode("utf-8")
                                                image_urls.append(f"data:{mime_type};base64,{base64_data}")
                                            else:
                                                if not file_name.endswith(".gif"):
                                                    message_text += "[图片]"
                                                    image_urls.append(url)
                                    except Exception as e:
                                        logger.warning(f"下载引用图片失败: {e}")
                                        if not file_name.endswith(".gif"):
                                            message_text += "[图片]"
                                            image_urls.append(url)
                except Exception as e:
                     logger.warning(f"处理引用消息失败: {e}")

        message_content = message_text.strip()
        
        # 预先加载 Prompt 以确定模式
        base_prompt = load_prompt(group_id)
        is_yaml_mode = isinstance(base_prompt, dict)
        
        if is_yaml_mode:
            # YAML 模式下，设置 trigger_reason，不修改 message_content 为复杂的上下文包裹
            if is_poke:
                trigger_reason = "对方戳了戳你。"
            elif is_random_chat:
                trigger_reason = "你正在【潜水】观察群聊。这只是群员之间的普通对话，并非对你说话。除非话题非常吸引你或者你被提及，否则请保持沉默并回复 [SILENCE]。"
            else:
                trigger_reason = f"对方（{sender_name}）正在【主动】与你搭话，请认真回复。"
                
            # 但如果是图片且无文字，还是需要补充说明，否则 message_content 为空
            if image_urls and not message_content:
                message_content = "[发送了一张图片]"
        else:
            # 旧模式：通过修改 message_content 注入上下文
            # 如果是图片消息且没有文本，补充提示词
            if image_urls and not message_content:
                if is_random_chat:
                    message_content = f"[你观察到群里 {sender_name} 发送了一张图片，这只是群员间的交流，你决定是否要评价一下]"
                else:
                    message_content = f"[对方发送了一张图片，是在对你说话]"
            # 如果是随机水群触发（有文本的情况），修改提示词
            elif is_random_chat:
                message_content = f"[提示：当前为【随机插话模式】。群员 {sender_name} 正在和别人聊天，内容是: {message_content}。如果话题与你无关，请务必回复 [SILENCE]]"
            else:
                message_content = f"[提示：对方正在【直接】对你说话：{message_content}]"
    else:
        return

    # 如果没配置 API KEY，直接跳过
    if not plugin_config.personification_api_key:
        logger.warning("拟人插件：未配置 API Key，跳过回复")
        return

    user_name = sender_name
    
    # 修改判断逻辑：如果有图片也允许继续
    if not message_content and not is_poke and not image_urls:
        return

    if not is_poke:
        logger.info(f"拟人插件：[Bot {bot.self_id}] [Inst {_module_instance_id}] 正在处理来自 {user_name} ({user_id}) 的消息...")
    else:
        logger.info(f"拟人插件：[Bot {bot.self_id}] [Inst {_module_instance_id}] 正在处理来自 {user_name} ({user_id}) 的戳一戳...")

    # 确保聊天历史已初始化，防止 KeyError
    if group_id not in chat_histories:
        chat_histories[group_id] = []

    # --- 获取用户画像 ---
    user_persona = ""
    try:
        # 尝试动态加载用户画像插件的数据
        persona_data_path = user_persona_data_dir / "data.json"
        if persona_data_path.exists():
            async with aiofiles.open(persona_data_path, mode="r", encoding="utf-8") as f:
                persona_json = json.loads(await f.read())
                personas = persona_json.get("personas", {})
                if user_id in personas:
                    user_persona = personas[user_id].get("data", "")
                    logger.info(f"拟人插件：成功为用户 {user_id} 加载画像信息")
    except Exception as e:
        logger.error(f"拟人插件：读取用户画像数据失败: {e}")

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
    
    # 清洗用户昵称，防止冒号等特殊字符误导 AI
    # 将冒号替换为全角冒号，或者其他不影响语义的字符
    safe_user_name = user_name.replace(":", "：").replace("\n", " ").strip()
    
    # 附加 User ID 以区分同名用户或避免名字被误认为内容，同时让 AI 明确知道对话对象
    safe_user_name = f"{safe_user_name}({user_id})"
    
    # 构建当前消息内容 - 使用 [] 包裹名字以明确分隔
    msg_prefix = f"[{safe_user_name}]: "
    
    if image_urls:
        current_user_content = [{"type": "text", "text": f"{msg_prefix}{message_content}"}]
        for url in image_urls:
            current_user_content.append({"type": "image_url", "image_url": {"url": url}})
        chat_histories[group_id].append({"role": "user", "content": current_user_content})
    else:
        chat_histories[group_id].append({"role": "user", "content": f"{msg_prefix}{message_content}"})
    
    # 限制上下文长度
    # 群聊使用最近 20 条，私聊强制使用 50 条以保证对话连续性
    if str(group_id).startswith("private_"):
        limit = 50
    else:
        limit = 20
        
    if len(chat_histories[group_id]) > limit:
        chat_histories[group_id] = chat_histories[group_id][-limit:]

    # 3. 构建 Prompt
    base_prompt = load_prompt(group_id)
    
    # 如果加载的是 YAML 配置 (字典)，则转入新逻辑
    if isinstance(base_prompt, dict):
         # 补充 trigger_reason 如果未设置 (例如 Poke 事件)
         if not trigger_reason and is_poke:
             trigger_reason = "对方戳了戳你。"

         await _process_yaml_response_logic(
             bot, event, group_id, user_id, user_name, level_name, base_prompt, chat_histories[group_id],
             trigger_reason=trigger_reason
         )
         return
    
    # 整合态度：结合个人和群聊的整体氛围
    attitude_desc = attitude_desc or "态度普通，像平常一样交流。"
    combined_attitude = f"你对该用户的个人态度是：{attitude_desc}"
    if group_attitude:
        combined_attitude += f"\n当前群聊整体氛围带给你的感受是：{group_attitude}"
    
    # 联网功能说明
    web_search_hint = ""
    if plugin_config.personification_web_search:
        web_search_hint = "你现在拥有联网搜索能力，可以获取最新的实时信息、新闻和知识来回答用户。"

    # --- 获取时间 ---
    # 使用东京时间 (UTC+9) 以符合日本角色作息
    # 注意：get_beijing_time() 返回的是 UTC+8
    now = datetime.now(timezone(timedelta(hours=9)))
    week_days = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    weekday_str = week_days[now.weekday()]
    # 使用 f-string 拼接中文，避免 Linux 下 strftime 因 locale 问题导致乱码或报错
    current_time_str = f"{now.year}年{now.month:02d}月{now.day:02d}日 {now.hour:02d}:{now.minute:02d}:{now.second:02d} ({weekday_str}) [东京时间]"

    # 检查是否开启了作息模拟
    group_config = get_group_config(group_id)
    schedule_enabled = group_config.get("schedule_enabled", False)
    global_schedule_enabled = plugin_config.personification_schedule_global
    
    schedule_prompt_part = ""
    if schedule_enabled or global_schedule_enabled:
        schedule_prompt_part = get_schedule_prompt_injection()

    # 针对 Gemini 模型或高性能模型优化 Prompt 结构
    # 将人格设定放在最前面，且不使用过于复杂的包装，直接作为系统指令
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

    # 私聊特殊规则
    is_private = str(group_id).startswith("private_")
    if is_private:
        system_prompt += (
            f"\n## 私聊特殊规则（高优先级）\n"
            f"1. **自主回复决策**：私聊中你拥有完全的自主权。如果对方发送的内容重复、无意义（如连续的“在吗”、“你好”、“哈哈”等）或你不想理会，请直接输出 **[SILENCE]** 结束话题，不要强行回复。\n"
            f"2. **极简回复模式**：对于高频、低信息量的私聊内容，必须使用极简回复（1-2句以内），切勿长篇大论。\n"
        )

    if user_persona:
        system_prompt += (
            f"\n## 对方的用户画像（由分析插件提供）\n"
            f"以下是对该用户的专业分析，请你根据这些特征（如职业、性格、兴趣）来调整你的语气和话题侧重点：\n"
            f"{user_persona}\n"
        )
    
    # 注入群聊风格提示词
    group_style = get_group_style(str(group_id))
    if group_style:
         system_prompt += f"\n\n## 当前群聊风格参考\n{group_style}\n请在回复时适当融入上述群聊风格，使对话更自然。\n"

    system_prompt += (
        f"\n{web_search_hint}\n"
        f"## 核心行动准则（绝对优先）\n"
        f"1. **身份一致性**：你必须完全沉浸在上述角色设定中。严禁提及你是 AI、机器人、程序 or 语言模型。严禁以 AI 助手的语气说话。\n"
        f"2. **回复风格**：回复必须精简、口语化，严禁使用长篇大论的列表或过于正式的陈述。回复长度限制在 30 个汉字左右。\n"
        f"3. **社交距离感**：你必须分清【直接对话】与【潜水观察】。在群员之间互相聊天（未艾特你或提及你名字）时，你只是个旁观者，绝不能认为对方是在对你说话。此时应尽量保持沉默，除非你确信有必要插话。\n"
        f"4. **互动决策**：\n"
        f"   - **决定是否回复**：仔细判断对话是否已经自然结束，或者对方只是发送了无意义的感慨/语气词。如果你认为**没有必要回复**，请直接输出 **[SILENCE]**。\n"
        f"   - **氛围反馈**：若氛围极好或对方让你开心，末尾加 [氛围好]。\n"
        f"   - **防御机制**：当检测到对方发送**恶毒语言**（如“杀了你全家”、“去死吧”等诅咒或严重人身攻击）或**黄赌毒恐暴**（色情、赌博、毒品、恐怖主义、暴力）相关内容时，**必须**输出 [BLOCK] 以触发自动拉黑机制。这是为了保护你和维护群聊环境。\n"
        f"4. **视觉感知**：\n"
        f"   - 若用户发送内容标记为 **[发送了一个表情包]**，请将其视为**梗图/表情包**。这通常是幽默、夸张或流行文化引用，**严禁**将其解读为真实发生的严重事件（如受伤、灾难）。请以轻松、调侃、配合玩梗或“看来你很喜欢这个表情”的态度回复。\n"
        f"   - 若标记为 **[发送了一张图片]**，则正常结合图片内容进行符合人设的评价。\n"
    )

    # 获取表情包列表（如果启用了）
    available_stickers = []
    # 检查群组是否允许表情包
    group_config = get_group_config(group_id)
    if group_config.get("sticker_enabled", True):
        sticker_dir = Path(plugin_config.personification_sticker_path)
        if sticker_dir.exists() and sticker_dir.is_dir():
            available_stickers = [f.stem for f in sticker_dir.iterdir() if f.suffix.lower() in [".jpg", ".png", ".gif", ".webp", ".jpeg"]]

    # 4. 构建消息历史
    # 将系统提示词作为第一条消息
    messages = [
         {"role": "system", "content": f"{system_prompt}\n\n当前可用表情包参考: {', '.join(available_stickers[:15]) if available_stickers else '暂无'}"}
     ]
    messages.extend(chat_histories[group_id])

    # 4. 调用 AI API
    try:
        # 记录交互时间 (用于主动消息判定)
        if str(group_id).startswith("private_"):
            try:
                p_state = load_proactive_state()
                if user_id not in p_state:
                    p_state[user_id] = {}
                p_state[user_id]["last_interaction"] = time.time()
                save_proactive_state(p_state)
            except Exception as e:
                logger.error(f"更新最后交互时间失败: {e}")

        # --- 联网工具准备 ---
        # 移除了所有第三方搜索引擎回退逻辑，仅保留原生联网支持标识
        
        # 使用通用的 call_ai_api 函数
        reply_content = await call_ai_api(messages)

        if not reply_content:
            # 如果包含图片且报错，尝试降级到纯文本 (call_ai_api 内部已经处理了基础调用，但我们可以增加一个针对 handle_reply 的特定降级逻辑)
            if image_urls:
                logger.warning("拟人插件：视觉模型调用可能失败，正在尝试降级至纯文本模式...")
                fallback_messages = []
                for msg in messages:
                    if isinstance(msg.get("content"), list):
                        text_content = "".join([item["text"] for item in msg["content"] if item["type"] == "text"])
                        fallback_messages.append({"role": msg["role"], "content": text_content})
                    else:
                        fallback_messages.append(msg)
                reply_content = await call_ai_api(fallback_messages)
            
            if not reply_content:
                logger.warning("拟人插件：未能获取到 AI 回复内容")
                return

        # 移除 AI 回复中可能包含的 [表情:xxx] 或 [发送了表情包: xxx] 标签
        reply_content = re.sub(r'\[表情:[^\]]*\]', '', reply_content)
        reply_content = re.sub(r'\[发送了表情包:[^\]]*\]', '', reply_content).strip()
        
        # 移除 AI 可能吐出的长串十六进制乱码 (例如：766E51F799FC83269D0C9F71409599EF)
        reply_content = re.sub(r'[A-F0-9]{16,}', '', reply_content).strip()
        
        # 5. 处理 AI 的回复决策
        if "[SILENCE]" in reply_content:
            logger.info(f"AI 决定结束与群 {group_id} 中 {user_name}({user_id}) 的对话 (SILENCE)")
            return

        if "[BLOCK]" in reply_content or "[NO_REPLY]" in reply_content:
            duration = plugin_config.personification_blacklist_duration
            user_blacklist[user_id] = time.time() + duration
            logger.info(f"AI 决定拉黑群 {group_id} 中 {user_name}({user_id})，时长 {duration} 秒")
            
            # 扣除个人及群聊好感度
            penalty_desc = ""
            if SIGN_IN_AVAILABLE:
                try:
                    is_private_context = str(group_id).startswith("private_")
                    
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
                    
                    g_new_fav = 0.0
                    if not is_private_context:
                        # 群聊扣除: 扣多 (0.5)
                        group_key = f"group_{group_id}"
                        group_data = get_user_data(group_key)
                        g_current_fav = float(group_data.get("favorability", 100.0))
                        g_new_fav = round(max(0.0, g_current_fav - 0.5), 2)
                        update_user_data(group_key, favorability=g_new_fav)
                    
                    penalty_desc = f"\n个人好感度：-{penalty:.2f} (当前：{new_fav:.2f})"
                    if not is_private_context:
                        penalty_desc += f"\n群聊好感度：-0.50 (当前：{g_new_fav:.2f})"
                    penalty_desc += f"\n累计拉黑次数：{current_blacklist_count}/25"
                    
                    if is_perm:
                        penalty_desc += "\n⚠️ 该用户已触发 25 次拉黑，已自动加入永久黑名单。"
                    
                    logger.info(f"用户 {user_id} 拉黑，累计 {current_blacklist_count} 次。扣除个人 {penalty}，{'私聊不扣除群好感' if is_private_context else f'扣除群 {group_id} 0.5 好感度'}")
                except Exception as e:
                    logger.error(f"扣除好感度或更新黑名单失败: {e}")

            return

        # 6. 处理氛围加分逻辑 [氛围好]
        has_good_atmosphere = "[氛围好]" in reply_content
        if has_good_atmosphere:
            reply_content = reply_content.replace("[氛围好]", "").strip()
            if SIGN_IN_AVAILABLE:
                try:
                    # 私聊场景不计算“群聊好感度”，仅处理个人好感度（如有）或跳过
                    is_private_context = str(group_id).startswith("private_")
                    
                    if not is_private_context:
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
                    else:
                        pass

                except Exception as e:
                    logger.error(f"增加群聊好感度失败: {e}")

        # 7. 决定是否发送表情包
        sticker_segment = None
        sticker_name = ""
        
        # 根据模式决定是否选择表情包
        should_get_sticker = False
        
        # 检查群组是否允许表情包 (默认为开启)
        group_config = get_group_config(group_id)
        is_sticker_enabled = group_config.get("sticker_enabled", True)
        
        if is_sticker_enabled:
            if force_mode == "mixed":
                should_get_sticker = True
            elif force_mode == "text_only":
                should_get_sticker = False
            elif random.random() < plugin_config.personification_sticker_probability:
                should_get_sticker = True

        if should_get_sticker:
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
        
        # 记录 AI 发言到持久化存储 (用于学习群聊风格)
        if isinstance(event, GroupMessageEvent):
            # 获取 Bot 昵称
            bot_nickname = "绪山真寻"
            try:
                # 尝试获取群名片
                bot_member_info = await bot.get_group_member_info(group_id=event.group_id, user_id=int(bot.self_id))
                bot_nickname = bot_member_info.get("card") or bot_member_info.get("nickname") or "绪山真寻"
            except Exception:
                pass
            
            record_group_msg(str(event.group_id), bot_nickname, assistant_content, is_bot=True)

        # 发送回复
        final_reply = reply_content.strip()
        
        if final_reply:
            segments = split_text_into_segments(final_reply)
            # 如果分段失败或只有一个段，回退到原逻辑（为了稳健性）
            if not segments:
                segments = [final_reply]
                
            for i, seg in enumerate(segments):
                if not seg.strip():
                    continue
                await bot.send(event, seg)
                
                # 如果还有后续内容（下一段文字 或 表情包），则延迟
                if i < len(segments) - 1 or sticker_segment:
                    delay = random.uniform(3.0, 5.0)
                    await asyncio.sleep(delay)

        if sticker_segment:
            await bot.send(event, sticker_segment)

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

# --- 群聊人设/功能管理 ---
set_persona = on_command("设置人设", permission=SUPERUSER, priority=5, block=True)
@set_persona.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    raw_text = args.extract_plain_text().strip()
    if not raw_text:
        await set_persona.finish("请提供提示词！格式：设置人设 [群号] <提示词>")

    parts = raw_text.split(maxsplit=1)
    
    target_group_id = None
    prompt = None

    # 如果第一个参数是数字且有后续内容，认为是 [群号] [提示词]
    if len(parts) == 2 and parts[0].isdigit():
        target_group_id = parts[0]
        prompt = parts[1]
    # 如果在群聊中，且不满足上述格式，默认针对当前群
    elif isinstance(event, GroupMessageEvent):
        target_group_id = str(event.group_id)
        prompt = raw_text
    # 私聊必须指定群号
    else:
        await set_persona.finish("私聊使用时请指定群号！格式：设置人设 <群号> <提示词>")

    if not prompt:
        await set_persona.finish("请提供提示词！")
    
    set_group_prompt(target_group_id, prompt)
    await set_persona.finish(f"已更新群 {target_group_id} 的人设。")

view_persona = on_command("查看人设", permission=SUPERUSER, priority=5, block=True)
@view_persona.handle()
async def _(bot: Bot, event: GroupMessageEvent):
    prompt = load_prompt(str(event.group_id))
    
    # 构造合并转发消息节点
    nodes = [
        {
            "type": "node",
            "data": {
                "name": "当前生效人设",
                "uin": str(bot.self_id),
                "content": prompt
            }
        }
    ]
    
    try:
        await bot.call_api("send_group_forward_msg", group_id=event.group_id, messages=nodes)
    except Exception as e:
        logger.error(f"发送人设转发消息失败: {e}")
        # 降级为直接发送（如果内容过长可能会失败，但尽力而为）
        await view_persona.finish(f"当前生效人设（转发失败，转文本发送）：\n{prompt}")

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
    
    if status in ["全局开启", "全局on", "全局true"]:
        plugin_config.personification_schedule_global = True
        save_plugin_runtime_config()
        await enable_schedule.finish("拟人作息模拟已全局开启（所有群默认生效，除非单独关闭）。")
    elif status in ["全局关闭", "全局off", "全局false"]:
        plugin_config.personification_schedule_global = False
        save_plugin_runtime_config()
        await enable_schedule.finish("拟人作息模拟全局开关已关闭（仅在单独开启的群生效）。")
    elif status not in ["开启", "关闭"]:
        global_status = "开启" if plugin_config.personification_schedule_global else "关闭"
        await enable_schedule.finish(f"用法: 拟人作息 [开启/关闭/全局开启/全局关闭]\n当前全局状态：{global_status}")
    
    if not isinstance(event, GroupMessageEvent):
        await enable_schedule.finish("请在群聊中使用此命令开启/关闭单群功能，或使用 '全局开启/全局关闭'。")

    is_enabled = (status == "开启")
    set_group_schedule_enabled(str(event.group_id), is_enabled)
    await enable_schedule.finish(f"本群作息模拟功能已{status}。")

view_config = on_command("拟人配置", permission=SUPERUSER, priority=5, block=True)
@view_config.handle()
async def _(bot: Bot, event: GroupMessageEvent):
    group_id = str(event.group_id)
    group_config = get_group_config(group_id)
    
    # 构建全局配置文本
    global_conf_str = (
        f"API 类型: {plugin_config.personification_api_type}\n"
        f"模型名称: {plugin_config.personification_model}\n"
        f"API URL: {plugin_config.personification_api_url}\n"
        f"回复概率: {plugin_config.personification_probability}\n"
        f"戳一戳概率: {plugin_config.personification_poke_probability}\n"
        f"表情包概率: {plugin_config.personification_sticker_probability}\n"
        f"联网搜索: {'开启' if plugin_config.personification_web_search else '关闭'}\n"
        f"私聊上下文长度: {plugin_config.personification_history_len}\n"
        f"群聊上下文长度: 20 (固定)\n"
        f"思考预算: {plugin_config.personification_thinking_budget}"
    )
    
    # 构建群组配置文本
    is_enabled = group_config.get("enabled", "未设置 (跟随白名单)")
    sticker_enabled = group_config.get("sticker_enabled", True)
    schedule_enabled = group_config.get("schedule_enabled", False)
    custom_prompt_len = len(group_config.get("custom_prompt", "")) if "custom_prompt" in group_config else 0
    prompt_status = f"自定义 ({custom_prompt_len} 字符)" if custom_prompt_len > 0 else "默认全局"
    
    group_conf_str = (
        f"当前群号: {group_id}\n"
        f"拟人功能开关: {is_enabled}\n"
        f"表情包开关: {'开启' if sticker_enabled else '关闭'}\n"
        f"作息模拟开关: {'开启' if schedule_enabled else '关闭'}\n"
        f"人设配置: {prompt_status}"
    )
    
    nodes = [
        {
            "type": "node",
            "data": {
                "name": "全局配置",
                "uin": str(bot.self_id),
                "content": global_conf_str
            }
        },
        {
            "type": "node",
            "data": {
                "name": "当前群配置",
                "uin": str(bot.self_id),
                "content": group_conf_str
            }
        }
    ]
    
    try:
        await bot.call_api("send_group_forward_msg", group_id=event.group_id, messages=nodes)
    except Exception as e:
        logger.error(f"发送配置详情失败: {e}")
        await view_config.finish(f"配置详情发送失败: {e}")

# 每日好感度统计
@scheduler.scheduled_job("cron", hour=23, minute=59, id="personification_daily_fav_report")
async def daily_group_fav_report():
    if not SIGN_IN_AVAILABLE:
        return
        
    try:
        # 获取所有数据
        data = load_data()
        today = datetime.now().strftime("%Y-%m-%d")
        
        report_lines = []
        total_increase = 0.0
        
        for user_id, user_data in data.items():
            # 筛选群聊数据 (排除私聊映射 group_private_)
            if user_id.startswith("group_") and not user_id.startswith("group_private_"):
                # 检查最后更新日期是否为今天
                if user_data.get("last_update") == today:
                    daily_count = float(user_data.get("daily_fav_count", 0.0))
                    if daily_count > 0:
                        group_id = user_id.replace("group_", "")
                        current_fav = float(user_data.get("favorability", 0.0))
                        
                        # 获取群名称
                        group_name = "未知群聊"
                        try:
                            # 尝试获取群信息
                            bots = get_bots()
                            for b in bots.values():
                                try:
                                    g_info = await b.get_group_info(group_id=int(group_id))
                                    group_name = g_info.get("group_name", "未知群聊")
                                    break
                                except:
                                    continue
                        except:
                            pass
                            
                        report_lines.append(f"群 {group_name}({group_id}): +{daily_count:.2f} (当前: {current_fav:.2f})")
                        total_increase += daily_count
        
        if report_lines:
            summary = f"📊 【每日群聊好感度统计】\n日期: {today}\n总增长: {total_increase:.2f}\n\n" + "\n".join(report_lines)
            
            # 发送给超级用户
            bots = get_bots()
            for bot in bots.values():
                for su in superusers:
                    try:
                        await bot.send_private_msg(user_id=int(su), message=summary)
                    except Exception as e:
                        logger.error(f"发送好感度统计给 {su} 失败: {e}")
                        
            logger.info(f"已发送每日群聊好感度统计，共 {len(report_lines)} 个群聊有变化")
            
    except Exception as e:
        logger.error(f"执行每日好感度统计任务出错: {e}")


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

    # 尝试方案 A：结合群聊素材生成
    if chat_context:
        rich_prompt = (
            "任务：请以日记的形式写一段简短的周记，记录你这一周在群里看到的趣事。\n"
            "素材：以下是最近群里的聊天记录（已脱敏），你可以参考其中的话题：\n"
            f"{chat_context}\n\n"
            f"要求：\n{base_requirements}"
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": rich_prompt}
        ]
        result = await call_ai_api(messages)
        if result:
            # 清理 XML 标签
            result = re.sub(r'<status.*?>.*?</\s*status\s*>', '', result, flags=re.DOTALL | re.IGNORECASE)
            result = re.sub(r'<think.*?>.*?</\s*think\s*>', '', result, flags=re.DOTALL | re.IGNORECASE)
            result = re.sub(r'</?\s*output.*?>', '', result, flags=re.IGNORECASE)
            result = re.sub(r'</?\s*message.*?>', '', result, flags=re.IGNORECASE)
            return result.strip()
        logger.warning("拟人插件：带素材的 AI 生成失败（可能是触发了 API 安全拦截），尝试保底模式...")

    # 尝试方案 B：保底模式（不带素材，降低被拦截概率）
    basic_prompt = (
        "任务：请以日记的形式写一段简短的周记，记录你这一周的心情。\n"
        f"要求：\n{base_requirements}"
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": basic_prompt}
    ]
    result = await call_ai_api(messages)
    if result:
        result = re.sub(r'<status.*?>.*?</\s*status\s*>', '', result, flags=re.DOTALL | re.IGNORECASE)
        result = re.sub(r'<think.*?>.*?</\s*think\s*>', '', result, flags=re.DOTALL | re.IGNORECASE)
        result = re.sub(r'</?\s*output.*?>', '', result, flags=re.IGNORECASE)
        result = re.sub(r'</?\s*message.*?>', '', result, flags=re.IGNORECASE)
        return result.strip()
    return ""

async def auto_post_diary():
    """定时任务：每周发送一次说说"""
    if not QZONE_PUBLISH_AVAILABLE:
        logger.warning("拟人插件：未找到 bot_manager (发布说说功能)，无法自动发送说说。")
        return

    bots = get_bots()
    if not bots:
        logger.warning("拟人插件：未找到有效的 Bot 实例，跳过自动说说发布。")
        return

    # 获取第一个 Bot 实例
    bot = list(bots.values())[0]

    # 自动刷新 Cookie
    logger.info("拟人插件：正在自动更新 Qzone Cookie...")
    cookie_ok, cookie_msg = await update_qzone_cookie(bot)
    if cookie_ok:
        logger.info("拟人插件：Qzone Cookie 更新成功。")
    else:
        logger.warning(f"拟人插件：Qzone Cookie 更新失败（{cookie_msg}），将尝试使用旧 Cookie 继续发布。")

    diary_content = await generate_ai_diary(bot)
    if not diary_content:
        return

    logger.info(f"拟人插件：正在自动发布周记说说...")
    success, msg = await publish_qzone_shuo(diary_content, bot.self_id)
    if success:
        logger.info("拟人插件：每周说说发布成功！")
    else:
        logger.error(f"拟人插件：每周说说发布失败：{msg}")

# 每周五晚上 19:00 发送
try:
    scheduler.add_job(auto_post_diary, "cron", day_of_week="fri", hour=19, minute=0, id="ai_weekly_diary", replace_existing=True)
    logger.info("拟人插件：已成功注册 AI 每周说说定时任务 (周五 19:00)")
except Exception as e:
    logger.error(f"拟人插件：注册定时任务失败: {e}")

manual_diary_cmd = on_command("发个说说", permission=SUPERUSER, priority=5, block=True)

@manual_diary_cmd.handle()
async def handle_manual_diary(bot: Bot):
    if not QZONE_PUBLISH_AVAILABLE:
        await manual_diary_cmd.finish("未找到 bot_manager (发布说说功能)，无法发布说说。")
        
    await manual_diary_cmd.send("正在生成 AI 周记并发布，请稍候...")
    
    diary_content = await generate_ai_diary(bot)
    if not diary_content:
        await manual_diary_cmd.finish("AI 生成周记失败，请检查网络 or API 配置。")
        
    success, msg = await publish_qzone_shuo(diary_content, bot.self_id)
    if success:
        await manual_diary_cmd.finish(f"✅ AI 说说发布成功！\n\n内容：\n{diary_content}")
    else:
        await manual_diary_cmd.finish(f"❌ {msg}")

# --- 新增功能：联网开关 ---

def save_plugin_runtime_config():
    """保存运行时配置，如联网开关"""
    path = RUNTIME_CONFIG_PATH
    data = {
        "web_search": plugin_config.personification_web_search,
        "schedule_global": plugin_config.personification_schedule_global,
        "proactive_enabled": plugin_config.personification_proactive_enabled
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        logger.error(f"保存运行时配置失败: {e}")

def load_plugin_runtime_config():
    """加载运行时配置"""
    path = RUNTIME_CONFIG_PATH if RUNTIME_CONFIG_PATH.exists() else LEGACY_RUNTIME_CONFIG_PATH
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                plugin_config.personification_web_search = data.get("web_search", plugin_config.personification_web_search)
                plugin_config.personification_schedule_global = data.get("schedule_global", plugin_config.personification_schedule_global)
                plugin_config.personification_proactive_enabled = data.get("proactive_enabled", plugin_config.personification_proactive_enabled)
        except Exception as e:
            logger.error(f"加载运行时配置失败: {e}")

# 初始化加载
load_plugin_runtime_config()

web_search_cmd = on_command("拟人联网", permission=SUPERUSER, priority=5, block=True)

@web_search_cmd.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    action = arg.extract_plain_text().strip()
    if action in ["开启", "on", "true"]:
        plugin_config.personification_web_search = True
        save_plugin_runtime_config()
        await web_search_cmd.finish("拟人插件模型联网功能已开启（将对所有消息启用搜索功能）。")
    elif action in ["关闭", "off", "false"]:
        plugin_config.personification_web_search = False
        save_plugin_runtime_config()
        await web_search_cmd.finish("拟人插件模型联网功能已关闭。")
    else:
        status = "开启" if plugin_config.personification_web_search else "关闭"
        await web_search_cmd.finish(f"当前联网功能状态：{status}\n使用 '拟人联网 开启/关闭' 来切换。")

proactive_msg_switch_cmd = on_command("拟人主动消息", permission=SUPERUSER, priority=5, block=True)

@proactive_msg_switch_cmd.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    action = arg.extract_plain_text().strip()
    if action in ["开启", "on", "true"]:
        plugin_config.personification_proactive_enabled = True
        save_plugin_runtime_config()
        await proactive_msg_switch_cmd.finish("拟人插件主动消息功能已开启。")
    elif action in ["关闭", "off", "false"]:
        plugin_config.personification_proactive_enabled = False
        save_plugin_runtime_config()
        await proactive_msg_switch_cmd.finish("拟人插件主动消息功能已关闭。")
    else:
        status = "开启" if plugin_config.personification_proactive_enabled else "关闭"
        await proactive_msg_switch_cmd.finish(f"当前主动消息功能状态：{status}\n使用 '拟人主动消息 开启/关闭' 来切换。")

clear_context_cmd = on_command("清除记忆", aliases={"清除上下文", "重置记忆"}, permission=SUPERUSER, priority=5, block=True)
learn_style_cmd = on_command("学习群聊风格", aliases={"分析群聊风格"}, permission=SUPERUSER, priority=5, block=True)
view_style_cmd = on_command("查看群聊风格", aliases={"群聊风格"}, priority=5, block=True)

async def analyze_group_style(group_id: str) -> Optional[str]:
    """分析群聊风格的核心逻辑"""
    msgs = get_recent_group_msgs(group_id, limit=300)
    if not msgs:
        return None
        
    # 构建 prompt
    chat_content = []
    
    # 准备多模态内容
    for msg in msgs:
        line_text = f"({msg['nickname']}): {msg['content']}"
        chat_content.append({"type": "text", "text": line_text + "\n"})
        
        # 如果有图片，添加到 prompt 中
        if msg.get("images"):
            for img_b64 in msg["images"]:
                # 限制图片数量，避免 token 超限 (每个消息最多取 1 张图，总共最多取 10 张图)
                if len(chat_content) < 50: # 仅对最近的一些消息带图，防止过大
                    chat_content.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{img_b64}"
                        }
                    })

    prompt_text = (
        "你是一个群聊风格分析师。请根据以下群聊记录（包含部分图片），总结该群的聊天风格。\n"
        "请包含以下几个方面：\n"
        "1. 整体氛围（如：轻松、严肃、二次元、搞怪等）\n"
        "2. 常用梗或黑话（如果有）\n"
        "3. 成员互动方式（如：互损、互夸、复读等）\n"
        "4. 语言特色（如：口癖、表情包使用习惯等）\n\n"
        "请输出一段精简的描述（200字以内），用于指导 AI 融入该群聊。\n"
        "格式要求：直接输出描述内容，不要包含其他客套话。\n\n"
        "## 聊天记录开始\n"
    )
    
    # 将 prompt_text 作为第一个文本块插入
    chat_content.insert(0, {"type": "text", "text": prompt_text})
    chat_content.append({"type": "text", "text": "\n## 聊天记录结束"})
    
    # 使用多模态消息格式
    messages = [{"role": "user", "content": chat_content}]
    return await call_ai_api(messages, temperature=0.7)

@learn_style_cmd.handle()
async def handle_learn_style(bot: Bot, event: GroupMessageEvent):
    group_id = str(event.group_id)
    
    # 获取最近聊天记录
    msgs = get_recent_group_msgs(group_id, limit=300)
    if len(msgs) < 10:
        await learn_style_cmd.finish("当前群聊记录太少啦，多聊一会儿再来学习吧！(至少需要 10 条)")
        
    await learn_style_cmd.send("正在分析最近 300 条群聊记录，请稍候...")
    
    try:
        style_desc = await analyze_group_style(group_id)
        
        if style_desc:
            set_group_style(group_id, style_desc)
            # 手动触发分析后，也清空记录
            clear_group_msgs(group_id)
            await learn_style_cmd.finish(f"✅ 群聊风格学习完成并已重置记录！\n\n{style_desc}\n\n已应用到本群的拟人回复中。")
        else:
            await learn_style_cmd.finish("分析失败，AI 未返回有效内容。")
            
    except FinishedException:
        raise # 重新抛出 FinishedException，让 NoneBot 处理
    except Exception as e:
        logger.error(f"学习群聊风格失败: {e}")
        await learn_style_cmd.finish(f"学习失败: {e}")

@view_style_cmd.handle()
async def handle_view_style(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    target_id = ""
    
    # 尝试从参数获取群号
    args_text = args.extract_plain_text().strip()
    if args_text and args_text.isdigit():
        target_id = args_text
    elif isinstance(event, GroupMessageEvent):
        target_id = str(event.group_id)
    else:
        await view_style_cmd.finish("请在群聊中使用，或指定群号：查看群聊风格 [群号]")
        
    style = get_group_style(target_id)
    if style:
        await view_style_cmd.finish(f"📊 群 {target_id} 的聊天风格：\n\n{style}")
    else:
        await view_style_cmd.finish(f"群 {target_id} 还没有学习过聊天风格哦！\n请管理员在群内发送 '学习群聊风格'。")

@clear_context_cmd.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    # 确定目标群组或私聊对象
    target_id = ""
    is_group = False
    
    # 尝试从参数获取群号
    args_text = arg.extract_plain_text().strip()
    
    if args_text in ["全局", "all", "所有"]:
        count = len(chat_histories)
        chat_histories.clear()
        # 清除驱动器级别的缓存
        driver = get_driver()
        if hasattr(driver, "_personification_msg_cache"):
             driver._personification_msg_cache.clear()
        
        await clear_context_cmd.finish(f"已清除全局所有群聊/私聊的对话上下文记忆（共 {count} 个会话）。")
    
    if args_text and args_text.isdigit():
        target_id = args_text
        is_group = True
    elif isinstance(event, GroupMessageEvent):
        target_id = str(event.group_id)
        is_group = True
    elif isinstance(event, PrivateMessageEvent):
        target_id = f"private_{event.user_id}"
        is_group = False
    else:
        await clear_context_cmd.finish("无法确定要清除的目标，请指定群号或在群聊/私聊中使用，或使用 '清除记忆 全局'。")
        
    # 同时清除消息缓冲，防止残留消息触发回复
    keys_to_remove = []
    # _msg_buffer 的 key 格式为 "{group_id}_{user_id}"
    for key in list(_msg_buffer.keys()):
        if key.startswith(f"{target_id}_"):
             keys_to_remove.append(key)
    
    for key in keys_to_remove:
        if "timer_task" in _msg_buffer[key]:
            _msg_buffer[key]["timer_task"].cancel()
        del _msg_buffer[key]

    if target_id in chat_histories:
        del chat_histories[target_id]
        msg = f"已清除群 {target_id} 的短期对话上下文记忆。" if is_group and not target_id.startswith("private_") else "已清除当前私聊的短期对话上下文记忆。"
        await clear_context_cmd.finish(msg)
    else:
        await clear_context_cmd.finish("当前没有任何缓存的对话上下文记忆。")

# --- 主动发送消息逻辑 ---

def load_proactive_state() -> dict:
    path = PROACTIVE_STATE_PATH if PROACTIVE_STATE_PATH.exists() else LEGACY_PROACTIVE_STATE_PATH
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_proactive_state(data: dict):
    PROACTIVE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(PROACTIVE_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

async def check_proactive_messaging(target_user_id: Optional[str] = None, bypass_checks: bool = False) -> str:
    """定期检查是否需要主动向高好感度用户发送消息"""
    if not bypass_checks and not plugin_config.personification_proactive_enabled:
        return "主动消息功能未开启"
    
    if not SIGN_IN_AVAILABLE:
        return "签到插件未加载，无法获取好感度数据"

    # 检查是否为休息时间 (允许20%概率在非休息时间打扰)
    if not bypass_checks and not is_rest_time(allow_unsuitable_prob=0.2):
        return "当前不是休息时间 (AI 判定不打扰)"

    # 获取机器人实例
    try:
        bots = get_bots()
        if not bots:
            return "未找到 Bot 实例"
        bot = list(bots.values())[0]
    except Exception as e:
        return f"获取 Bot 实例失败: {e}"

    # 加载所有用户数据
    all_data = load_data()
    if not all_data:
        return "未找到用户数据"

    # 加载主动消息状态
    proactive_state = load_proactive_state()
    today_str = datetime.now().strftime("%Y-%m-%d")
    current_ts = time.time()
    interval_seconds = plugin_config.personification_proactive_interval * 60
    
    # 如果没有指定目标用户，则筛选候选人
    if not target_user_id:
        candidates = []
        
        for user_id, user_data in all_data.items():
            # 过滤掉群组数据
            if user_id.startswith("group_"):
                continue
                
            # 检查好感度阈值
            try:
                fav = float(user_data.get("favorability", 0.0))
            except (ValueError, TypeError):
                fav = 0.0
                
            if not bypass_checks and fav < plugin_config.personification_proactive_threshold:
                continue
                
            # 检查是否在黑名单
            if user_data.get("is_perm_blacklisted", False):
                continue
            
            # 检查最近交互时间 (防止打断对话或过于频繁)
            user_state = proactive_state.get(user_id, {})
            last_interaction = user_state.get("last_interaction", 0)
            if not bypass_checks and (current_ts - last_interaction < interval_seconds):
                # 如果最近刚刚聊过天，跳过
                continue

            # 检查每日主动发起限制
            last_date = user_state.get("last_date", "")
            count = user_state.get("count", 0)
            
            # 如果是新的一天，重置计数
            if last_date != today_str:
                count = 0
                
            if not bypass_checks and count >= plugin_config.personification_proactive_daily_limit:
                continue
                
            candidates.append(user_id)

        if not candidates:
            return "没有符合条件的目标用户"

        # 随机选择一个用户发送，避免同时骚扰多人
        target_user_id = random.choice(candidates)
    
    try:
        # 获取用户当前信息
        user_data = get_user_data(target_user_id)
        if not user_data:
             return f"无法获取用户 {target_user_id} 的数据"

        fav = user_data.get("favorability", 0.0)
        level_name = get_level_name(fav)

        # 始终注入当前时间和活动状态（主动消息需要时间感知，与全局作息开关无关）
        now = get_beijing_time()
        activity_status = get_activity_status()
        activity_line = f"当前时间：{now.strftime('%H:%M')}，当前状态：{activity_status}\n"
        activity_context_line = f"你现在的状态是：{activity_status}（北京时间 {now.strftime('%H:%M')}）\n"
        schedule_considerations = (
            f"1. 如果是深夜或上课时间，应该尽量避免打扰（除非你特别想念对方）。\n"
            f"2. 如果是休息时间，可以尝试发起话题。\n"
            f"3. 请根据你（绪山真寻）的性格来决定。\n\n"
        )

        # 加载基础人设
        base_persona = load_prompt()

        # 注入群聊风格提示词
        group_style = get_group_style(target_user_id)
        if group_style:
             base_persona += f"\n\n## 当前群聊风格参考\n{group_style}\n请在回复时适当融入上述群聊风格，使对话更自然。\n"

        # 1. AI 决策阶段：询问 AI 是否应该发送消息
        decision_prompt = (
            f"{base_persona}\n\n"
            f"## 决策任务\n"
            f"{activity_line}"
            f"目标对象：你的好朋友（好感度：{fav}，关系：{level_name}）\n"
            f"任务：请判断现在是否适合主动向对方发起私聊对话？\n"
            f"考量因素：\n"
            f"{schedule_considerations}"
            f"请只输出 JSON 格式结果：{{\"should_send\": true/false, \"reason\": \"原因\"}}"
        )
        
        decision_messages = [{"role": "system", "content": decision_prompt}]
        decision_reply = await call_ai_api(decision_messages, temperature=0.5)
        
        should_send = False
        try:
            if decision_reply:
                # 尝试提取 JSON
                json_match = re.search(r'\{.*\}', decision_reply, re.DOTALL)
                if json_match:
                    decision_data = json.loads(json_match.group(0))
                    should_send = decision_data.get("should_send", False)
                    reason = decision_data.get("reason", "无")
                    logger.info(f"拟人插件：主动消息决策 - 用户 {target_user_id}, 结果: {should_send}, 原因: {reason}")
                else:
                    logger.warning(f"拟人插件：决策结果无法解析为 JSON: {decision_reply}")
        except Exception as e:
            logger.error(f"拟人插件：决策解析失败: {e}")
            
        if not should_send and not bypass_checks:
            return f"AI 决定不发送消息 (原因: {locals().get('reason', '未知')})"

        # 2. 生成消息阶段
        # 增加联网与时间感知提示
        web_search_hint = ""
        if plugin_config.personification_web_search:
            web_search_hint = "（你可以利用联网能力获取当前热门话题或新闻来开启对话）"

        system_prompt = (
            f"{base_persona}\n\n"
            f"## 当前情境\n"
            f"{activity_context_line}"
            f"你的一位好朋友（好感度：{fav}，关系：{level_name}）现在可能在线。\n"
            f"你决定主动找对方聊两句。\n"
            f"{web_search_hint}\n\n"
            f"## 生成要求\n"
            f"1. **必须严格符合人设**（语气自然、口语化、傲娇/活泼）。\n"
            f"2. **极简短**：像是在QQ/微信上随手发的一条消息，字数控制在 20 字以内！\n"
            f"3. 不要像写信一样，不要有问候语前缀，直接说事。\n"
            f"4. 严禁包含 '[SILENCE]' 或 '[NO_REPLY]'。\n"
            f"5. 不要带任何前缀（如'回复：'），直接输出内容。\n"
        )
        
        # 重新构造 prompt
        prompt_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "（看着手机，想给对方发个消息...）"}
        ]
        
        reply = await call_ai_api(prompt_messages, temperature=0.9)
        
        if reply:
            # 尝试解析 XML/YAML 格式
            try:
                parsed = parse_yaml_response(reply)
                if parsed["messages"]:
                    reply_content = parsed["messages"][0]["text"]
                else:
                    raise ValueError("No messages found")
            except Exception:
                # 降级处理：手动清理 XML 标签
                reply_content = reply.strip().strip('"').strip("'")
                reply_content = re.sub(r'<status.*?>.*?</\s*status\s*>', '', reply_content, flags=re.DOTALL | re.IGNORECASE)
                reply_content = re.sub(r'<think.*?>.*?</\s*think\s*>', '', reply_content, flags=re.DOTALL | re.IGNORECASE)
                reply_content = re.sub(r'</?\s*output.*?>', '', reply_content, flags=re.IGNORECASE)
                reply_content = re.sub(r'</?\s*message.*?>', '', reply_content, flags=re.IGNORECASE)
                reply_content = reply_content.strip()

            if not reply_content:
                 return "AI 生成内容为空 (清理后)"
            
            if "[SILENCE]" in reply_content or "[NO_REPLY]" in reply_content:
                return "AI 决定不回复 (SILENCE)"

            # 发送私聊消息
            await bot.send_private_msg(user_id=int(target_user_id), message=reply_content)
            logger.info(f"拟人插件：主动向用户 {target_user_id} 发送消息: {reply_content}")
            
            # 更新状态
            user_state = proactive_state.get(target_user_id, {})
            current_count = 0
            last_date = user_state.get("last_date", "")
            
            if last_date == today_str:
                current_count = user_state.get("count", 0)
            
            proactive_state[target_user_id] = {
                "last_date": today_str,
                "count": current_count + 1,
                "last_interaction": time.time() # 更新最后交互时间
            }
            save_proactive_state(proactive_state)
            
            return f"成功向用户 {target_user_id} 发送消息: {reply_content}"
            
    except Exception as e:
        logger.error(f"拟人插件：主动发送消息任务异常: {e}")
        return f"发送消息过程发生异常: {e}"

    return "AI 未生成回复"

# 注册测试命令
test_proactive_cmd = on_command("测试主动消息", permission=SUPERUSER, priority=1, block=True)

@test_proactive_cmd.handle()
async def handle_test_proactive(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    target = args.extract_plain_text().strip()
    target_user_id = target if target and target.isdigit() else None
    
    await test_proactive_cmd.send("正在尝试触发主动消息发送逻辑...")
    result = await check_proactive_messaging(target_user_id=target_user_id, bypass_checks=True)
    await test_proactive_cmd.finish(f"执行结果：\n{result}")

# 注册定时任务
try:
    scheduler.add_job(
        check_proactive_messaging, 
        "interval", 
        minutes=max(5, plugin_config.personification_proactive_interval), 
        id="personification_proactive_messaging",
        replace_existing=True
    )
    logger.info(f"拟人插件：主动消息任务已注册，间隔 {max(5, plugin_config.personification_proactive_interval)} 分钟")
except Exception as e:
    logger.error(f"拟人插件：注册主动消息任务失败: {e}")
