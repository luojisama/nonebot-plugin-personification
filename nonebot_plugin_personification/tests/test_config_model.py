from __future__ import annotations

import warnings

from ._loader import load_personification_module

config_mod = load_personification_module("plugin.personification.config")


def test_config_defaults_do_not_emit_deprecated_web_search_warning() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        config_mod.Config()

    assert not [
        item
        for item in caught
        if issubclass(item.category, DeprecationWarning)
        and "personification_web_search" in str(item.message)
    ]


def test_qzone_cookie_deprecated_alias_copies_to_prefixed_field() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cfg = config_mod.Config(qzone_cookie="uin=o123; skey=abc")

    assert cfg.personification_qzone_cookie == "uin=o123; skey=abc"
    assert any(
        issubclass(item.category, DeprecationWarning)
        and "qzone_cookie" in str(item.message)
        for item in caught
    )
