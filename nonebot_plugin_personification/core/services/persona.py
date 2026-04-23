from typing import Any, Callable


def build_custom_title_getter(
    *,
    logger: Any = None,
) -> Callable[[str], str]:
    try:
        try:
            from plugin.sign_in.utils import get_user_data  # type: ignore
        except ImportError:
            from ...sign_in.utils import get_user_data  # type: ignore
    except Exception:
        if logger is not None:
            logger.debug("拟人插件：未启用签到插件称号读取，使用空称号回退。")
        return lambda _user_id: ""

    def _get_custom_title(user_id: str) -> str:
        try:
            user_data = get_user_data(user_id)
            custom_title = user_data.get("custom_title")
            if custom_title:
                return str(custom_title)
        except Exception:
            pass
        return ""

    return _get_custom_title
