from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from ._loader import load_personification_module


action_executor_mod = load_personification_module("plugin.personification.agent.action_executor")
resource_impl = load_personification_module("plugin.personification.skills.skillpacks.resource_collector.scripts.impl")
resource_main = load_personification_module("plugin.personification.skills.skillpacks.resource_collector.scripts.main")


class _Logger:
    def debug(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        return None

    def warning(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        return None


class _Response:
    def __init__(self, text: str) -> None:
        self.text = text


class _HttpClient:
    def __init__(self, html: str) -> None:
        self.html = html
        self.urls: list[str] = []

    async def get(self, url: str, **_kwargs):  # noqa: ANN001
        self.urls.append(url)
        return _Response(self.html)


class _Bot:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, _event, message):  # noqa: ANN001
        self.sent.append(str(message))


def _bing_image_html() -> str:
    return """
    <a class="iusc" m="{&quot;murl&quot;:&quot;https://img.example/a.jpg&quot;,
      &quot;turl&quot;:&quot;https://img.example/a-thumb.jpg&quot;,
      &quot;purl&quot;:&quot;https://page.example/a&quot;,
      &quot;t&quot;:&quot;高清猫猫壁纸&quot;,&quot;ow&quot;:1920,&quot;oh&quot;:1080}"></a>
    <a class="iusc" m="{&quot;murl&quot;:&quot;https://img.example/b.webp&quot;,
      &quot;purl&quot;:&quot;https://page.example/b&quot;,
      &quot;t&quot;:&quot;猫猫参考图&quot;,&quot;ow&quot;:900,&quot;oh&quot;:900}"></a>
    """


def test_search_images_returns_direct_image_urls() -> None:
    client = _HttpClient(_bing_image_html())
    payload = json.loads(
        asyncio.run(
            resource_impl.search_images(
                "猫猫壁纸",
                limit=2,
                http_client=client,
                logger=_Logger(),
            )
        )
    )

    assert payload["ok"] is True
    assert payload["source_type"] == "image"
    assert payload["results"][0]["image_url"] == "https://img.example/a.jpg"
    assert payload["results"][0]["page_url"] == "https://page.example/a"
    assert payload["results"][0]["width"] == 1920
    assert client.urls and "bing.com/images/search" in client.urls[0]


def test_search_and_send_images_queues_real_image_message() -> None:
    async def _run() -> tuple[list[dict], list[str], dict]:
        bot = _Bot()
        executor = action_executor_mod.ActionExecutor(bot, object(), SimpleNamespace(), _Logger())
        queued: list[dict] = []
        executor.bind_pending_actions(queued)
        runtime = SimpleNamespace(
            plugin_config=SimpleNamespace(),
            logger=_Logger(),
            get_now=lambda: 0,
            http_client=_HttpClient(_bing_image_html()),
            vision_caller=None,
        )
        tool = resource_main.build_send_image_tools(runtime, executor)[0]
        result = await tool.handler("猫猫壁纸", count=1)
        for action in queued:
            await executor.execute(action["type"], action["params"])
        return queued, bot.sent, json.loads(result)

    queued, sent, payload = asyncio.run(_run())

    assert payload["ok"] is True
    assert payload["queued"] is True
    assert queued[0]["type"] == "send_image_url"
    assert queued[0]["params"]["url"] == "https://img.example/a.jpg"
    assert queued[0]["params"]["history_text"] == "[联网图片:高清猫猫壁纸]"
    assert sent and sent[0].startswith("[CQ:image,file=https://img.example/a.jpg")
