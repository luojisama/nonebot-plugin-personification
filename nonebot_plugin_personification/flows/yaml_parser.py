import re
from typing import Any, Dict, Optional


def extract_xml_content(text: str, tag: str) -> Optional[str]:
    """提取 XML 标签内容，支持多行与大小写。"""
    pattern = rf"<{tag}.*?>(.*?)</\s*{tag}\s*>"
    match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return None


def parse_yaml_response(response: str) -> Dict[str, Any]:
    """解析 YAML 模板约定的 AI 输出结构。"""
    result = {
        "status": extract_xml_content(response, "status"),
        "think": extract_xml_content(response, "think"),
        "action": extract_xml_content(response, "action"),
        "messages": [],
    }

    output_content = extract_xml_content(response, "output")
    if not output_content:
        output_content = response

    if output_content:
        msg_pattern = r"<message(.*?)>(.*?)</\s*message\s*>"
        matches = re.finditer(msg_pattern, output_content, re.DOTALL | re.IGNORECASE)
        for match in matches:
            attrs = match.group(1)
            content = match.group(2).strip()

            quote = None
            quote_match = re.search(r'quote=["\']([^"\']*)["\']', attrs, re.IGNORECASE)
            if quote_match:
                quote = quote_match.group(1)

            sticker = extract_xml_content(content, "sticker")
            if sticker:
                content = re.sub(r"<sticker.*?>.*?</\s*sticker\s*>", "", content, flags=re.DOTALL | re.IGNORECASE).strip()

            result["messages"].append(
                {
                    "quote": quote,
                    "text": content,
                    "sticker": sticker,
                }
            )

    return result
