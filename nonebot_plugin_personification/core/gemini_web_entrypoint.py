from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path


def _install_namespace() -> None:
    personification_root = Path(__file__).resolve().parents[1]
    plugin_root = personification_root.parent
    packages = {
        "plugin": plugin_root,
        "plugin.personification": personification_root,
        "plugin.personification.core": personification_root / "core",
        "plugin.personification.native_mcp": personification_root / "native_mcp",
        "plugin.personification.native_mcp.social_research": personification_root
        / "native_mcp"
        / "social_research",
    }
    for name, path in packages.items():
        if name in sys.modules:
            continue
        module = types.ModuleType(name)
        module.__path__ = [str(path)]
        module.__package__ = name
        sys.modules[name] = module


if __name__ == "__main__":
    _install_namespace()
    from plugin.personification.core.gemini_web_helper import main

    asyncio.run(main())
