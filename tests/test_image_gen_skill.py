from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace

from ._loader import load_personification_module

load_personification_module("plugin.personification.agent.tool_registry")
image_gen_main = load_personification_module("plugin.personification.skills.skillpacks.image_gen.scripts.main")
tool_caller_impl = load_personification_module("plugin.personification.skills.skillpacks.tool_caller.scripts.impl")
gemini_transport = load_personification_module("plugin.personification.core.gemini_transport")

PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class OpenAIToolCaller:
    model = "gpt-5.4"

    async def generate_image(self, *_args, **_kwargs):  # noqa: ANN001
        return {"b64_json": PNG_B64}


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
        return {"b64_json": PNG_B64}


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

    assert result == f"[IMAGE_B64]{PNG_B64}[/IMAGE_B64]"
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

    assert result == f"[IMAGE_B64]{PNG_B64}[/IMAGE_B64]"
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

    def _init(self, *, model: str = "gpt-5.3-codex", auth_path: str = "", timeout: float = 30.0, proxy: str = "") -> None:  # noqa: ANN001
        self.model = model
        self.auth_path_override = auth_path
        self.timeout = timeout
        self.proxy = proxy
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
        return {"b64_json": PNG_B64}

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

    assert result == f"[IMAGE_B64]{PNG_B64}[/IMAGE_B64]"
    assert result2 == f"[IMAGE_B64]{PNG_B64}[/IMAGE_B64]"
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

    assert result == f"[IMAGE_B64]{PNG_B64}[/IMAGE_B64]"
    assert caller.calls[-1]["images"] == ["https://example.com/ref.png"]
    assert caller.calls[-1]["reference_mode"] == "input_image"


def test_image_gen_skill_registers_openai_http_route(monkeypatch) -> None:  # noqa: ANN001
    captured: dict[str, object] = {}

    class _Resp:
        def raise_for_status(self):  # noqa: ANN201
            return None

        def json(self):  # noqa: ANN201
            return {"data": [{"b64_json": PNG_B64}]}

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

    assert result == f"[IMAGE_B64]{PNG_B64}[/IMAGE_B64]"
    assert captured["url"] == "https://api.example.test/v1/images/generations"
    assert captured["json"]["size"] == "1536x1024"
    assert captured["headers"]["Authorization"] == "Bearer sk-img"


def test_dedicated_url_never_inherits_main_key() -> None:
    config = SimpleNamespace(
        personification_image_gen_api_type="auto",
        personification_image_gen_api_url="https://third-party.example/v1",
        personification_image_gen_api_key="",
        personification_api_type="openai",
        personification_api_url="https://main.example/v1",
        personification_api_key="main-secret",
    )
    route = image_gen_main.generate_image.__globals__["_configured_image_route"](config)

    assert route["api_url"] == "https://third-party.example/v1"
    assert route["api_key"] == ""
    result = asyncio.run(image_gen_main.generate_image("poster", tool_caller=None, plugin_config=config))
    assert result == {"error": "dedicated image route requires api_type, api_url and api_key"}


def test_gemini_image_route_uses_only_google_api_key_header(monkeypatch) -> None:  # noqa: ANN001
    captured: dict[str, object] = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):  # noqa: ANN201
            return None

        def json(self):  # noqa: ANN201
            return {
                "candidates": [
                    {"content": {"parts": [{"inlineData": {"data": PNG_B64, "mimeType": "image/png"}}]}}
                ]
            }

    class _Client:
        def __init__(self, **kwargs):  # noqa: ANN001
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):  # noqa: ANN201
            return self

        async def __aexit__(self, *_args):  # noqa: ANN001, ANN201
            return None

        async def post(self, url, json=None, headers=None, params=None):  # noqa: ANN001, ANN201
            captured.update(url=url, json=json or {}, headers=headers or {}, params=params or {})
            return _Resp()

    monkeypatch.setattr(image_gen_main.generate_image.__globals__["httpx"], "AsyncClient", _Client)
    config = SimpleNamespace(
        personification_image_gen_api_type="gemini",
        personification_image_gen_api_url="https://gemini-image.example",
        personification_image_gen_api_key="image-secret",
        personification_gemini_auth_mode="auto",
        personification_api_type="openai",
        personification_api_url="",
        personification_api_key="",
    )

    result = asyncio.run(image_gen_main.generate_image("poster", tool_caller=None, plugin_config=config))

    assert result["b64_json"] == PNG_B64
    assert captured["headers"]["x-goog-api-key"] == "image-secret"
    assert "Authorization" not in captured["headers"]
    assert captured["params"] == {}


def test_gemini_image_single_attempt_does_not_negotiate_bearer(monkeypatch) -> None:  # noqa: ANN001
    headers_seen: list[dict[str, str]] = []

    class _Resp:
        status_code = 401

        def raise_for_status(self):  # noqa: ANN201
            raise RuntimeError("unauthorized")

    class _Client:
        def __init__(self, **_kwargs):  # noqa: ANN001
            pass

        async def __aenter__(self):  # noqa: ANN201
            return self

        async def __aexit__(self, *_args):  # noqa: ANN001, ANN201
            return None

        async def post(self, _url, json=None, headers=None, params=None):  # noqa: ANN001, ANN201
            del json, params
            headers_seen.append(dict(headers or {}))
            return _Resp()

    globals_ = image_gen_main.generate_image.__globals__
    monkeypatch.setattr(globals_["httpx"], "AsyncClient", _Client)
    monkeypatch.setitem(globals_, "use_single_attempt_retry_policy", lambda: True)
    config = SimpleNamespace(
        personification_image_gen_api_type="gemini",
        personification_image_gen_api_url="https://gemini-image.example",
        personification_image_gen_api_key="image-secret",
        personification_gemini_auth_mode="auto",
        personification_api_type="openai",
        personification_api_url="",
        personification_api_key="",
    )

    result = asyncio.run(image_gen_main.generate_image("poster", tool_caller=None, plugin_config=config))

    assert result == {"error": "image provider request failed"}
    assert headers_seen == [{"Content-Type": "application/json", "x-goog-api-key": "image-secret"}]


def test_gemini_image_single_attempt_reuses_normalized_bearer_cache(monkeypatch) -> None:  # noqa: ANN001
    gemini_transport.clear_gemini_auth_cache()

    class _SeedResp:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

    seed_calls: list[str] = []

    async def _seed(auth):  # noqa: ANN001, ANN202
        seed_calls.append(auth.mode)
        return _SeedResp(401 if len(seed_calls) == 1 else 200)

    asyncio.run(gemini_transport.request_with_gemini_auth(
        endpoint="https://gemini-image-cache.example/v1beta",
        api_key="image-cache-secret",
        auth_mode="auto",
        send=_seed,
    ))
    headers_seen: list[dict[str, str]] = []

    class _Resp:
        status_code = 200

        def json(self):  # noqa: ANN201
            return {
                "candidates": [
                    {"content": {"parts": [{"inlineData": {"data": PNG_B64, "mimeType": "image/png"}}]}}
                ]
            }

    class _Client:
        def __init__(self, **_kwargs):  # noqa: ANN001
            pass

        async def __aenter__(self):  # noqa: ANN201
            return self

        async def __aexit__(self, *_args):  # noqa: ANN001, ANN201
            return None

        async def post(self, _url, json=None, headers=None, params=None):  # noqa: ANN001, ANN201
            del json, params
            headers_seen.append(dict(headers or {}))
            return _Resp()

    globals_ = image_gen_main.generate_image.__globals__
    monkeypatch.setattr(globals_["httpx"], "AsyncClient", _Client)
    monkeypatch.setitem(globals_, "use_single_attempt_retry_policy", lambda: True)
    config = SimpleNamespace(
        personification_image_gen_api_type="gemini",
        personification_image_gen_api_url="https://gemini-image-cache.example",
        personification_image_gen_api_key="image-cache-secret",
        personification_gemini_auth_mode="auto",
        personification_api_type="openai",
        personification_api_url="",
        personification_api_key="",
    )

    result = asyncio.run(image_gen_main.generate_image("poster", tool_caller=None, plugin_config=config))

    assert result["b64_json"] == PNG_B64
    assert headers_seen == [
        {"Content-Type": "application/json", "Authorization": "Bearer image-cache-secret"}
    ]


def test_api_pool_selects_best_supported_http_route(monkeypatch) -> None:  # noqa: ANN001
    captured: dict[str, object] = {}

    class _Resp:
        def raise_for_status(self): return None  # noqa: ANN201, E701
        def json(self): return {"data": [{"b64_json": PNG_B64}]}  # noqa: ANN201, E701

    class _Client:
        def __init__(self, **kwargs): captured["client_kwargs"] = kwargs  # noqa: ANN001, E701
        async def __aenter__(self): return self  # noqa: ANN201, E701
        async def __aexit__(self, *_args): return None  # noqa: ANN001, ANN201, E701
        async def post(self, url, json=None, headers=None):  # noqa: ANN001, ANN201
            captured.update(url=url, headers=headers, json=json)
            return _Resp()

    monkeypatch.setattr(image_gen_main.generate_image.__globals__["httpx"], "AsyncClient", _Client)
    config = SimpleNamespace(
        personification_image_gen_api_type="auto",
        personification_image_gen_api_url="",
        personification_image_gen_api_key="",
        personification_api_type="anthropic",
        personification_api_url="",
        personification_api_key="",
        personification_api_pools=[
            {"name": "later", "api_type": "openai", "api_url": "https://later.example/v1", "api_key": "later", "priority": 20},
            {"name": "unsupported", "api_type": "anthropic", "api_url": "https://skip.example", "api_key": "skip", "priority": 0},
            {"name": "first", "api_type": "openai", "api_url": "https://first.example/v1", "api_key": "pool-key", "priority": 1, "proxy": "http://proxy.test:8080"},
        ],
    )
    result = asyncio.run(image_gen_main.generate_image("poster", tool_caller=None, plugin_config=config))

    assert result["b64_json"] == PNG_B64
    assert captured["url"] == "https://first.example/v1/images/generations"
    assert captured["headers"]["Authorization"] == "Bearer pool-key"
    assert captured["client_kwargs"]["proxy"] == "http://proxy.test:8080"


def test_codex_dedicated_caller_preserves_proxy() -> None:
    instances: list[object] = []

    def _init(self, *, model, auth_path="", timeout=30.0, proxy=""):  # noqa: ANN001
        self.model, self.auth_path_override, self.timeout, self.proxy = model, auth_path, timeout, proxy
        instances.append(self)

    async def _generate(self, *_args, **_kwargs): return {"b64_json": PNG_B64}  # noqa: ANN001, E701
    cls = type("OpenAICodexToolCaller", (), {"__init__": _init, "generate_image": _generate})
    caller = cls(model="codex", timeout=10, proxy="http://codex.proxy:9000")
    config = SimpleNamespace(personification_image_gen_timeout=90)

    result = asyncio.run(image_gen_main.generate_image("poster", tool_caller=caller, timeout=90, plugin_config=config))

    assert result["b64_json"] == PNG_B64
    assert instances[-1].proxy == "http://codex.proxy:9000"


def test_antigravity_image_protocol_and_proxy(monkeypatch) -> None:  # noqa: ANN001
    captured: dict[str, object] = {}
    caller = object.__new__(tool_caller_impl.AntigravityCliToolCaller)
    caller.timeout = 30.0
    caller.proxy = "http://agy.proxy:7890"

    async def _token(*_args, **_kwargs): return "token", None  # noqa: ANN001, E701
    async def _project(*_args, **_kwargs): return "project-id"  # noqa: ANN001, E701
    caller._get_access_token = _token
    caller._resolve_project = _project

    class _Resp:
        def raise_for_status(self): return None  # noqa: ANN201, E701
        def json(self): return {"response": {"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": PNG_B64}}]}}]}}  # noqa: ANN201, E701

    class _Client:
        def __init__(self, **kwargs): captured["client_kwargs"] = kwargs  # noqa: ANN001, E701
        async def __aenter__(self): return self  # noqa: ANN201, E701
        async def __aexit__(self, *_args): return None  # noqa: ANN001, ANN201, E701
        async def post(self, url, json=None, headers=None):  # noqa: ANN001, ANN201
            captured.update(url=url, json=json, headers=headers)
            return _Resp()

    nanobanan = load_personification_module("plugin.personification.skills.skillpacks.image_gen.scripts.nanobanan")
    monkeypatch.setattr(nanobanan.httpx, "AsyncClient", _Client)
    result = asyncio.run(nanobanan.generate_image_nanobanan("poster", tool_caller=caller))

    assert result["b64_json"] == PNG_B64
    assert captured["url"] == tool_caller_impl._ANTIGRAVITY_CLI_GENERATE_ENDPOINT
    assert captured["json"]["userAgent"] == "antigravity"
    assert captured["json"]["requestId"]
    assert captured["headers"]["Client-Metadata"]
    assert captured["client_kwargs"]["proxy"] == "http://agy.proxy:7890"


def test_invalid_base64_is_not_exposed_as_image() -> None:
    caller = OpenAICodexToolCaller()

    async def _invalid(*_args, **_kwargs): return {"b64_json": "not-base64!!"}  # noqa: ANN001, E701
    caller.generate_image = _invalid
    result = asyncio.run(image_gen_main.generate_image("poster", tool_caller=caller, plugin_config=SimpleNamespace()))

    assert result == {"error": "provider returned invalid image data"}


def test_image_magic_without_decodable_payload_is_rejected() -> None:
    invalid = base64.b64encode(b"\x89PNG\r\n\x1a\nnot-an-image").decode("ascii")
    validated = image_gen_main.generate_image.__globals__["_validated_b64"](invalid, "image/png")

    assert validated is None


def test_public_tool_rejects_arbitrary_local_reference(tmp_path) -> None:
    local = tmp_path / "secret.png"
    local.write_bytes(base64.b64decode(PNG_B64))
    caller = OpenAICodexToolCaller()

    result = asyncio.run(image_gen_main.generate_image(
        "poster",
        tool_caller=caller,
        images=[str(local.resolve())],
        plugin_config=SimpleNamespace(),
    ))

    assert result["b64_json"] == PNG_B64
    assert caller.calls[-1]["images"] == []
    assert "local_path_not_allowed" in result["warning"]


def test_api_pool_falls_back_and_uses_candidate_model_proxy(monkeypatch) -> None:  # noqa: ANN001
    calls: list[dict[str, object]] = []

    async def fake_openai(prompt, **kwargs):  # noqa: ANN001
        calls.append({"prompt": prompt, **kwargs})
        if len(calls) == 1:
            return {"error": "first failed"}
        return {"b64_json": PNG_B64, "mime_type": "image/png"}

    monkeypatch.setitem(image_gen_main.generate_image.__globals__, "_generate_image_openai_http", fake_openai)
    config = SimpleNamespace(
        personification_image_gen_api_type="auto",
        personification_image_gen_api_url="",
        personification_image_gen_api_key="",
        personification_api_type="anthropic",
        personification_api_url="",
        personification_api_key="",
        personification_api_pools=[
            {"name": "first", "api_type": "openai", "api_url": "https://one.example/v1", "api_key": "one", "model": "image-one", "proxy": "http://proxy-one:8080", "priority": 1},
            {"name": "second", "api_type": "openai", "api_url": "https://two.example/v1", "api_key": "two", "model": "image-two", "proxy": "http://proxy-two:8080", "priority": 2},
        ],
    )

    result = asyncio.run(image_gen_main.generate_image("poster", tool_caller=None, plugin_config=config))

    assert result["b64_json"] == PNG_B64
    assert [(item["image_model"], item["proxy"]) for item in calls] == [
        ("image-one", "http://proxy-one:8080"),
        ("image-two", "http://proxy-two:8080"),
    ]
