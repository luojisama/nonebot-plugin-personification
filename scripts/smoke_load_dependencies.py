from __future__ import annotations

import sys

import nonebot
from nonebot import load_plugin, require


PLUGIN_NAME = "nonebot_plugin_personification"
REQUIRED_NONEBOT_PLUGINS = (
    "nonebot_plugin_apscheduler",
    "nonebot_plugin_htmlrender",
    "nonebot_plugin_localstore",
)


def _format_exception(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def main() -> int:
    nonebot.init(driver="~none")

    try:
        plugin = load_plugin(PLUGIN_NAME)
    except Exception as exc:
        print(f"failed to load {PLUGIN_NAME}: {_format_exception(exc)}", file=sys.stderr)
        return 1
    if plugin is None:
        print(f"failed to load {PLUGIN_NAME}: load_plugin returned None", file=sys.stderr)
        return 1

    failures: list[str] = []
    for dependency in REQUIRED_NONEBOT_PLUGINS:
        try:
            dependency_plugin = require(dependency)
        except Exception as exc:
            failures.append(f"{dependency}: {_format_exception(exc)}")
            continue
        if dependency_plugin is None:
            failures.append(f"{dependency}: require returned None")

    if failures:
        print(
            "plugin dependency load-order smoke test failed:\n- " + "\n- ".join(failures),
            file=sys.stderr,
        )
        return 1

    print(
        "plugin dependency load-order smoke test passed: "
        + ", ".join(REQUIRED_NONEBOT_PLUGINS)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
