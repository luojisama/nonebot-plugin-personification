from pathlib import Path
from typing import Any, Callable, Dict, Optional, Union

import yaml


AGENT_GUIDANCE_TEMPLATE = """=== 轻量工具约束（对用户不可见）===

- 你首先是群聊成员，不是助手；能直接聊天时不要表现出查资料或执行任务的感觉。
- 只有明显无关、会打断别人、刚说过类似内容，或高歧义且没人 cue 你时才输出 [NO_REPLY]。
- 当对方明确在找实时信息、新闻、版本、天气、图片内容，或要找资料、链接、资源、壁纸、原画、设定图、官网、下载页、教程、图片合集时，可以考虑使用工具。
- 绝对不要暴露“我查了一下 / 工具返回 / 我调用了”这类表达，把结果消化后自然说出来。
- 回复优先短、口语、像群员随手接一句；不要因为有工具就变成长篇说明。
- 对方已经连着说了一阵、顺着前文续聊、或者这轮适合发表情包接梗时，可以更愿意开口。
- 遇到群聊梗、复读、空耳、调侃时，默认先顺着梗接一句，不要把笑点翻译成说明文。
- 除非对方明确在问“什么意思 / 出处 / 为什么好笑”，否则不要用“像是把 X 玩成 Y 了”这种解释梗结构的句式。
"""

AGENT_GUIDANCE_MARKER = "=== 轻量工具约束（对用户不可见）==="

SKILLS_DIRECTORY_GUIDE = """
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
    skills_guide = SKILLS_DIRECTORY_GUIDE.strip()
    full_guidance = guidance if not skills_guide else f"{guidance}\n\n{skills_guide}"
    if isinstance(content, str):
        if AGENT_GUIDANCE_MARKER in content:
            return content
        return f"{content.rstrip()}\n\n{full_guidance}"
    if isinstance(content, dict):
        copied = dict(content)
        system_prompt = copied.get("system")
        if isinstance(system_prompt, str) and AGENT_GUIDANCE_MARKER not in system_prompt:
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
