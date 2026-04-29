from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ._loader import load_personification_module

load_personification_module("plugin.personification.agent.tool_registry")
image_gen_main = load_personification_module("plugin.personification.skills.skillpacks.image_gen.scripts.main")


class OpenAIToolCaller:
    model = "gpt-5.4"

    async def generate_image(self, *_args, **_kwargs):  # noqa: ANN001
        return {"b64_json": "QUJD"}


class OpenAICodexToolCaller:
    def __init__(self) -> None:
        self.model = "gpt-5.3-codex"
        self.calls: list[dict[str, str]] = []

    async def generate_image(self, prompt: str, *, size: str, image_model: str):  # noqa: ANN001
        self.calls.append({"prompt": prompt, "size": size, "image_model": image_model})
        return {"b64_json": "QUJD"}


def _runtime(tool_caller, *, image_model: str = "gpt-image-2"):  # noqa: ANN001
    return SimpleNamespace(
        plugin_config=SimpleNamespace(
            personification_image_gen_enabled=True,
            personification_image_gen_model=image_model,
        ),
        tool_caller=tool_caller,
        agent_tool_caller=tool_caller,
    )


def test_image_gen_skill_does_not_register_for_openai_api_caller() -> None:
    assert image_gen_main.build_image_gen_tool(_runtime(OpenAIToolCaller())) is None


def test_image_gen_skill_registers_codex_only_and_passes_image2_model() -> None:
    caller = OpenAICodexToolCaller()
    tool = image_gen_main.build_image_gen_tool(_runtime(caller))

    assert tool is not None
    result = asyncio.run(tool.handler("poster", size="1536x1024"))

    assert result == "[IMAGE_B64]QUJD[/IMAGE_B64]"
    assert caller.calls == [
        {
            "prompt": "poster",
            "size": "1536x1024",
            "image_model": "gpt-image-2",
        }
    ]


def test_image_gen_skill_normalizes_size_aliases() -> None:
    caller = OpenAICodexToolCaller()
    tool = image_gen_main.build_image_gen_tool(_runtime(caller))

    assert tool is not None
    result = asyncio.run(tool.handler("poster", size="竖版"))

    assert result == "[IMAGE_B64]QUJD[/IMAGE_B64]"
    assert caller.calls == [
        {
            "prompt": "poster",
            "size": "1024x1536",
            "image_model": "gpt-image-2",
        }
    ]


def test_image_gen_skill_uses_configured_timeout_on_dedicated_caller() -> None:
    instances: list[object] = []

    def _init(self, *, model: str = "gpt-5.3-codex", auth_path: str = "", timeout: float = 30.0) -> None:  # noqa: ANN001
        self.model = model
        self.auth_path_override = auth_path
        self.timeout = timeout
        self.calls = []
        instances.append(self)

    async def _generate_image(self, prompt: str, *, size: str, image_model: str):  # noqa: ANN001
        self.calls.append({"prompt": prompt, "size": size, "image_model": image_model})
        return {"b64_json": "QUJD"}

    codex_cls = type(
        "OpenAICodexToolCaller",
        (),
        {
            "__init__": _init,
            "generate_image": _generate_image,
        },
    )
    caller = codex_cls(timeout=30.0)
    runtime = SimpleNamespace(
        plugin_config=SimpleNamespace(
            personification_image_gen_enabled=True,
            personification_image_gen_model="gpt-image-2",
            personification_image_gen_timeout=240,
        ),
        tool_caller=caller,
        agent_tool_caller=caller,
    )
    tool = image_gen_main.build_image_gen_tool(runtime)

    assert tool is not None
    result = asyncio.run(tool.handler("poster", size="1024x1024"))
    result2 = asyncio.run(tool.handler("poster 2", size="横版"))

    assert result == "[IMAGE_B64]QUJD[/IMAGE_B64]"
    assert result2 == "[IMAGE_B64]QUJD[/IMAGE_B64]"
    assert caller.calls == []
    assert len(instances) == 2
    dedicated = instances[1]
    assert dedicated is not caller
    assert dedicated.timeout == 240
    assert dedicated.calls == [
        {
            "prompt": "poster",
            "size": "1024x1024",
            "image_model": "gpt-image-2",
        },
        {
            "prompt": "poster 2",
            "size": "1536x1024",
            "image_model": "gpt-image-2",
        }
    ]
