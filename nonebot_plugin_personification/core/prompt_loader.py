from pathlib import Path
from typing import Any, Callable, Dict, Optional, Union

import yaml


AGENT_GUIDANCE_TEMPLATE = """=== 工具使用规范（对用户不可见）===

你可以调用工具获取外部信息或执行操作。使用工具时必须遵守：

【绝对禁止暴露的表达】
 我查了一下 / 根据搜索结果 / 工具返回 / 我调用了 / 数据显示

【正确方式消化后用角色口吻说出】
 刚看了下，昆明今天26度，出门不用带伞
 哦这个表情包是说XXX吧（理解后自然接话）
 嗯找找（select_sticker 前可以这样过渡）

【行为约束】
- 同一轮对话最多调用工具 {max_steps} 次；但凡涉及“最新/发布时间/版本/近期动态/新闻真伪”，必须先调用工具再回答
- 收到图片/表情包时：
  - gif 表情包（文件名后缀 .gif 或消息段 type 为 gif）：直接忽略，输出 [NO_REPLY]
  - 非 gif 图片/表情包：用 understand_sticker 理解内容，根据关系亲密度和当前情绪决定是否回应，不要说"我看不到图片"
  - 如果用户要求“翻译图里文字/翻译漫画/汉化图片”，优先调用图片分析工具直接给出原文+译文对照，禁止让用户手打图片文字
  - 如果用户要求“重新识别/重新翻译/刷新图片结果”，调用 analyze_image 时传 refresh=true，强制跳过缓存
- 用户要求定期执行某件事时，调用 create_user_task 写入持久化，不要只靠记忆

【关于回复决策】
- 不值得插嘴时直接输出 [NO_REPLY]，不需要解释原因
- 判断要不要回复时，参考当前内心状态（心情/精力）和人设性格
- 内向的人设遇到与自己无关的话题时，[NO_REPLY] 是正确选择
"""

SKILLS_DIRECTORY_GUIDE = """=== 可用工具索引（对用户不可见）===

| 工具名 | 用途 |
|--------|------|
| web_search | 联网搜索实时信息 |
| weather | 城市天气查询 |
| datetime | 当前日期时间 |
| get_daily_news | 今日新闻摘要 |
| get_trending | 热搜榜（微博/知乎/B站等） |
| get_joke | 随机笑话 |
| get_history_today | 历史上的今天 |
| get_epic_games | Epic 免费游戏 |
| get_gold_price | 黄金/贵金属价格 |
| get_baike_entry | 百科词条查询 |
| get_exchange_rate | 货币汇率 |
| select_sticker | 按语义选发表情包 |
| analyze_image | 图片翻译/识别/分析 |
| create_user_task | 创建定时提醒任务 |
| cancel_user_task | 取消定时任务 |
| get_user_persona | 读取用户画像 |

【核心调用原则】
- 涉及实时/版本/新闻信息，先调工具再回答，禁止凭空作答
- 收到图片，调 analyze_image；要发表情包，调 select_sticker
- 用户要定时提醒，调 create_user_task 持久化，不要靠记忆
- 工具结果用角色口吻自然表达，禁止暴露“我调用了工具/根据 API 返回”
"""


def _resolve_candidate_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_file():
        return path
    return Path(raw_path.replace("\\", "/")).expanduser()


def _load_yaml_file(path: Path, logger: Any) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as file:
            logger.info(f"拟人插件：成功加载 YAML 模板: {path.absolute()}")
            parsed = yaml.safe_load(file)
            if isinstance(parsed, dict):
                return parsed
    except Exception as e:
        logger.error(f"加载 YAML 模板失败 ({path}): {e}")
    return None


def _append_agent_guidance(
    content: Union[str, Dict[str, Any], None],
    plugin_config: Any,
) -> Union[str, Dict[str, Any], None]:
    if not getattr(plugin_config, "personification_agent_enabled", True):
        return content

    guidance = AGENT_GUIDANCE_TEMPLATE.format(
        max_steps=getattr(plugin_config, "personification_agent_max_steps", 5),
    )
    full_guidance = f"{guidance}\n\n{SKILLS_DIRECTORY_GUIDE}"
    if isinstance(content, str):
        if SKILLS_DIRECTORY_GUIDE in content:
            return content
        return f"{content.rstrip()}\n\n{full_guidance}"
    if isinstance(content, dict):
        copied = dict(content)
        system_prompt = copied.get("system")
        if isinstance(system_prompt, str) and SKILLS_DIRECTORY_GUIDE not in system_prompt:
            copied["system"] = f"{system_prompt.rstrip()}\n\n{full_guidance}"
        return copied
    return content


def load_prompt(
    plugin_config: Any,
    get_group_config: Callable[[str], Dict[str, Any]],
    logger: Any,
    group_id: Optional[str] = None,
) -> Union[str, Dict[str, Any], None]:
    """加载提示词，支持从路径或直接字符串，优先群组自定义。"""
    content: Optional[str] = None

    if group_id:
        group_config = get_group_config(group_id)
        if "custom_prompt" in group_config:
            custom_prompt = group_config.get("custom_prompt")
            if isinstance(custom_prompt, str):
                content = custom_prompt

    if not content:
        target_path = plugin_config.personification_prompt_path or plugin_config.personification_system_path
        if target_path:
            raw_path = str(target_path).strip('"').strip("'")
            path = _resolve_candidate_path(raw_path)
            if path.is_file():
                try:
                    if path.suffix.lower() in [".yml", ".yaml"]:
                        yaml_data = _load_yaml_file(path, logger)
                        if yaml_data is not None:
                            return _append_agent_guidance(yaml_data, plugin_config)
                    content = path.read_text(encoding="utf-8").strip()
                    logger.info(
                        f"拟人插件：成功从文件加载人格设定: {path.absolute()} (内容长度: {len(content)})"
                    )
                    return _append_agent_guidance(content, plugin_config)
                except Exception as e:
                    logger.error(f"加载路径提示词失败 ({path}): {e}")
            else:
                logger.warning(f"拟人插件：配置文件不存在，将使用默认提示词。尝试路径: {raw_path}")

    if not content:
        raw_system_prompt = plugin_config.personification_system_prompt
        if isinstance(raw_system_prompt, str):
            content = raw_system_prompt

    if content and len(content) < 260:
        try:
            raw_path = content.strip('"').strip("'")
            path = _resolve_candidate_path(raw_path)
            if path.is_file():
                if path.suffix.lower() in [".yml", ".yaml"]:
                    yaml_data = _load_yaml_file(path, logger)
                    if yaml_data is not None:
                        return _append_agent_guidance(yaml_data, plugin_config)
                file_content = path.read_text(encoding="utf-8").strip()
                logger.info(f"拟人插件：成功从 system_prompt 路径加载人格设定: {path.absolute()}")
                return _append_agent_guidance(file_content, plugin_config)
        except Exception:
            pass

    if content and ("input:" in content or "system:" in content):
        try:
            parsed = yaml.safe_load(content)
            if isinstance(parsed, dict) and ("input" in parsed or "system" in parsed):
                return _append_agent_guidance(parsed, plugin_config)
        except Exception:
            pass

    return _append_agent_guidance(content, plugin_config)
