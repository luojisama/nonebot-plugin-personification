from pathlib import Path
from typing import Any, Callable, Dict, Optional, Union

import yaml


def _resolve_candidate_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_file():
        return path
    return Path(raw_path.replace("\\", "/")).expanduser()


def _load_yaml_file(path: Path, logger: Any) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            logger.info(f"拟人插件：成功加载 YAML 模板: {path.absolute()}")
            parsed = yaml.safe_load(f)
            if isinstance(parsed, dict):
                return parsed
    except Exception as e:
        logger.error(f"加载 YAML 模板失败 ({path}): {e}")
    return None


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
                            return yaml_data
                    content = path.read_text(encoding="utf-8").strip()
                    logger.info(f"拟人插件：成功从文件加载人格设定: {path.absolute()} (内容长度: {len(content)})")
                    return content
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
                        return yaml_data
                file_content = path.read_text(encoding="utf-8").strip()
                logger.info(f"拟人插件：成功从 system_prompt 路径加载人格设定: {path.absolute()}")
                return file_content
        except Exception:
            pass

    if content and ("input:" in content or "system:" in content):
        try:
            parsed = yaml.safe_load(content)
            if isinstance(parsed, dict) and ("input" in parsed or "system" in parsed):
                return parsed
        except Exception:
            pass

    return content
