from __future__ import annotations

from . import impl as legacy


async def run(api_type: str = "openai", base_url: str = "", data_url: str = "") -> str:
    normalized_type = legacy._normalize_api_type(api_type)
    normalized_url = legacy._normalize_openai_base_url(base_url) if base_url else ""
    if data_url:
        parsed = legacy._split_data_url(data_url)
        if parsed:
            mime, _ = parsed
            return f"api_type={normalized_type}; base_url={normalized_url}; data_url_mime={mime}"
        return f"api_type={normalized_type}; base_url={normalized_url}; data_url=invalid"
    return f"api_type={normalized_type}; base_url={normalized_url}"
