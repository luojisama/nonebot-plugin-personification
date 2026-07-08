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

    async def generate_image(self, prompt: str, *, size: str, image_model: str, images=None, reference_mode="auto"):  # noqa: ANN001
        self.calls.append(
            {
                "prompt": prompt,
                "size": size,
                "image_model": image_model,
                "images": list(images or []),
                "reference_mode": reference_mode,
            }
        )
        return {"b64_json": "QUJD"}


def _runtime(tool_caller, *, image_model: str = "gpt-image-2"):  # noqa: ANN001
    return SimpleNamespace(
        plugin_config=SimpleNamespace(
            personification_image_gen_enabled=True,
            personification_image_gen_api_type="auto",
            personification_image_gen_api_url="",
            personification_image_gen_api_key="",
            personification_api_type="openai",
            personification_api_url="",
            personification_api_key="",
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
            "images": [],
            "reference_mode": "auto",
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
            "images": [],
            "reference_mode": "auto",
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

    async def _generate_image(self, prompt: str, *, size: str, image_model: str, images=None, reference_mode="auto"):  # noqa: ANN001
        self.calls.append(
            {
                "prompt": prompt,
                "size": size,
                "image_model": image_model,
                "images": list(images or []),
                "reference_mode": reference_mode,
            }
        )
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
            "images": [],
            "reference_mode": "auto",
        },
        {
            "prompt": "poster 2",
            "size": "1536x1024",
            "image_model": "gpt-image-2",
            "images": [],
            "reference_mode": "auto",
        }
    ]


def test_image_gen_skill_passes_reference_images() -> None:
    caller = OpenAICodexToolCaller()
    tool = image_gen_main.build_image_gen_tool(_runtime(caller))

    assert tool is not None
    result = asyncio.run(
        tool.handler(
            "按参考图画海报",
            size="方图",
            image_urls=["https://example.com/ref.png"],
            reference_mode="input_image",
        )
    )

    assert result == "[IMAGE_B64]QUJD[/IMAGE_B64]"
    assert caller.calls[-1]["images"] == ["https://example.com/ref.png"]
    assert caller.calls[-1]["reference_mode"] == "input_image"


def test_image_gen_skill_registers_openai_http_route(monkeypatch) -> None:  # noqa: ANN001
    captured: dict[str, object] = {}

    class _Resp:
        def raise_for_status(self):  # noqa: ANN201
            return None

        def json(self):  # noqa: ANN201
            return {"data": [{"b64_json": "QUJD"}]}

    class _Client:
        def __init__(self, **kwargs):  # noqa: ANN001
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):  # noqa: ANN201
            return self

        async def __aexit__(self, *_args):  # noqa: ANN001, ANN201
            return None

        async def post(self, url, json=None, headers=None):  # noqa: ANN001, ANN201
            captured["url"] = url
            captured["json"] = json or {}
            captured["headers"] = headers or {}
            return _Resp()

    monkeypatch.setattr(image_gen_main.generate_image.__globals__["httpx"], "AsyncClient", _Client)
    runtime = SimpleNamespace(
        plugin_config=SimpleNamespace(
            personification_image_gen_enabled=True,
            personification_image_gen_api_type="openai",
            personification_image_gen_api_url="https://api.example.test/v1",
            personification_image_gen_api_key="sk-img",
            personification_api_type="openai",
            personification_api_url="",
            personification_api_key="",
            personification_image_gen_model="gpt-image-2",
            personification_image_gen_timeout=180,
        ),
        tool_caller=OpenAIToolCaller(),
        agent_tool_caller=OpenAIToolCaller(),
    )
    tool = image_gen_main.build_image_gen_tool(runtime)

    assert tool is not None
    result = asyncio.run(tool.handler("poster", size="横版"))

    assert result == "[IMAGE_B64]QUJD[/IMAGE_B64]"
    assert captured["url"] == "https://api.example.test/v1/images/generations"
    assert captured["json"]["size"] == "1536x1024"
    assert captured["headers"]["Authorization"] == "Bearer sk-img"
