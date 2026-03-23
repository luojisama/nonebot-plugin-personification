from typing import Any, Awaitable, Callable, Dict

try:
    from nonebot.adapters import Event
    from nonebot.typing import T_State
except Exception:  # pragma: no cover - fallback for lightweight unit-test stubs
    Event = Any
    T_State = Dict[str, Any]


def build_personification_rule(
    *,
    personification_rule_core: Callable[..., Awaitable[bool]],
    sign_in_available: bool,
    get_user_data: Callable[[str], Dict[str, Any]],
    user_blacklist: Dict[str, float],
    logger: Any,
    group_event_cls: Any,
    private_event_cls: Any,
    is_group_whitelisted: Callable[[str, list[str]], bool],
    plugin_whitelist: list[str],
    load_prompt: Callable[[str], Any],
    load_proactive_state: Callable[[], Dict[str, Dict[str, Any]]],
    is_rest_time: Callable[..., bool],
    probability: float,
    looks_like_private_command: Callable[[str], bool],
) -> Callable[[Event, T_State], Awaitable[bool]]:
    async def _rule(event: Event, state: T_State) -> bool:
        return await personification_rule_core(
            event,
            state,
            sign_in_available=sign_in_available,
            get_user_data=get_user_data,
            user_blacklist=user_blacklist,
            logger=logger,
            group_event_cls=group_event_cls,
            private_event_cls=private_event_cls,
            is_group_whitelisted=is_group_whitelisted,
            plugin_whitelist=plugin_whitelist,
            load_prompt=load_prompt,
            load_proactive_state=load_proactive_state,
            is_rest_time=is_rest_time,
            probability=probability,
            looks_like_private_command=looks_like_private_command,
        )

    return _rule


def build_poke_rule(
    *,
    poke_rule_core: Callable[..., Awaitable[bool]],
    is_group_whitelisted: Callable[[str, list[str]], bool],
    plugin_whitelist: list[str],
    probability: float,
) -> Callable[[Event], Awaitable[bool]]:
    async def _rule(event: Event) -> bool:
        return await poke_rule_core(
            event,
            is_group_whitelisted=is_group_whitelisted,
            plugin_whitelist=plugin_whitelist,
            probability=probability,
        )

    return _rule


def build_poke_notice_rule(
    *,
    poke_notice_rule_core: Callable[..., Awaitable[bool]],
    is_group_whitelisted: Callable[[str, list[str]], bool],
    plugin_whitelist: list[str],
    probability: float,
    logger: Any,
) -> Callable[[Event], Awaitable[bool]]:
    async def _rule(event: Event) -> bool:
        return await poke_notice_rule_core(
            event,
            is_group_whitelisted=is_group_whitelisted,
            plugin_whitelist=plugin_whitelist,
            probability=probability,
            logger=logger,
        )

    return _rule
