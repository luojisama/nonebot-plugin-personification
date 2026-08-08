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
    simulate_htmlrender_without_md_to_pic = "--simulate-htmlrender-without-md-to-pic" in sys.argv[1:]

    if simulate_htmlrender_without_md_to_pic:
        try:
            require("nonebot_plugin_htmlrender")
            htmlrender_module = sys.modules.get("nonebot_plugin_htmlrender")
            if htmlrender_module is None:
                raise RuntimeError("nonebot_plugin_htmlrender module is missing after require")
            if hasattr(htmlrender_module, "md_to_pic"):
                delattr(htmlrender_module, "md_to_pic")
        except Exception as exc:
            print(
                f"failed to prepare htmlrender compatibility smoke: {_format_exception(exc)}",
                file=sys.stderr,
            )
            return 1

    try:
        plugin = load_plugin(PLUGIN_NAME)
    except Exception as exc:
        print(f"failed to load {PLUGIN_NAME}: {_format_exception(exc)}", file=sys.stderr)
        return 1
    if plugin is None:
        print(f"failed to load {PLUGIN_NAME}: load_plugin returned None", file=sys.stderr)
        return 1

    failures: list[str] = []
    metadata = getattr(plugin, "metadata", None)
    if metadata is None:
        failures.append("plugin metadata is missing")
    else:
        if metadata.name != "拟人化聊天":
            failures.append(f"unexpected plugin metadata name: {metadata.name!r}")
        if metadata.homepage != "https://github.com/luojisama/nonebot-plugin-personification":
            failures.append(f"unexpected plugin metadata homepage: {metadata.homepage!r}")
    if simulate_htmlrender_without_md_to_pic:
        loaded_module = sys.modules.get(PLUGIN_NAME)
        if loaded_module is None or getattr(loaded_module, "md_to_pic", object()) is not None:
            failures.append("missing htmlrender md_to_pic did not enter the explicit degraded path")
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
