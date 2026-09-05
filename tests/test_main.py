"""Behavior tests for the R18 picture plugin."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import zipfile
from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "crimson_cosmos_plugin", PLUGIN_ROOT / "main.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
CrimsonCosmosPlugin = MODULE.CrimsonCosmosPlugin


def test_should_use_crimson_cosmos_identity() -> None:
    """Folder, metadata, and plugin class use the new identity consistently."""
    metadata = (PLUGIN_ROOT / "metadata.yaml").read_text(encoding="utf-8")

    assert PLUGIN_ROOT.name == "astrbot_plugin_crimson_cosmos"
    assert "name: astrbot_plugin_crimson_cosmos" in metadata
    assert "display_name: 绯色万象" in metadata
    assert hasattr(MODULE, "CrimsonCosmosPlugin")


class FakeResponse:
    """Minimal async HTTP response used by plugin tests."""

    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.status = 200
        self.content = SimpleNamespace(iter_chunked=self._iter_image_chunks)

    async def _iter_image_chunks(self, _size: int):
        """Yield split image bytes like a chunked network response."""
        yield b"image-"
        yield b"bytes"

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    async def json(self, **_kwargs: object) -> object:
        return self.payload

    async def text(self) -> str:
        return str(self.payload)

    def raise_for_status(self) -> None:
        return None


class FakeSession:
    """Capture the plugin's outgoing HTTP request."""

    def __init__(self, payload: object) -> None:
        self.calls: list[tuple[str, dict[str, str] | None]] = []
        self.post_calls: list[tuple[str, dict[str, object]]] = []
        self.closed = False
        self.payload = payload

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        timeout: object = None,
    ) -> FakeResponse:
        del headers
        del timeout
        self.calls.append((url, params))
        return FakeResponse(self.payload)

    async def close(self) -> None:
        self.closed = True

    def post(
        self,
        url: str,
        *,
        json: dict[str, object],
        timeout: object = None,
    ) -> FakeResponse:
        del timeout
        self.post_calls.append((url, json))
        return FakeResponse(self.payload)


class RoutedSession(FakeSession):
    """Return payloads or errors according to the requested URL."""

    def __init__(self, routes: dict[str, list[object]]) -> None:
        super().__init__(None)
        self.routes = routes

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        timeout: object = None,
    ) -> FakeResponse:
        del headers
        del timeout
        self.calls.append((url, params))
        result = self.routes[url].pop(0)
        if isinstance(result, Exception):
            raise result
        return FakeResponse(result)


class CloseableSession:
    """Minimal session double for the plugin termination path."""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeEvent:
    """Small event double exposing only the plugin-facing event API."""

    def __init__(
        self,
        message: str,
        group_id: str = "10001",
        private: bool = False,
        sender_id: str = "20001",
    ):
        self.message = message
        self.group_id = group_id
        self.private = private
        self.sender_id = sender_id
        self.stopped = False

    def get_message_str(self) -> str:
        return self.message

    def get_group_id(self) -> str:
        return self.group_id

    def is_private_chat(self) -> bool:
        return self.private

    def get_sender_id(self) -> str:
        return self.sender_id

    def image_result(self, image_url: str) -> tuple[str, str]:
        return ("image", image_url)

    def plain_result(self, text: str) -> tuple[str, str]:
        return ("text", text)

    def chain_result(self, chain: list[object]) -> tuple[str, list[object]]:
        return (
            "chain",
            [
                {
                    "type": getattr(component.type, "value", component.type),
                    **({"file": component.file} if hasattr(component, "file") else {}),
                    **({"text": component.text} if hasattr(component, "text") else {}),
                }
                for component in chain
            ],
        )

    def stop_event(self) -> None:
        self.stopped = True


class FakeRecallBot:
    """Capture the OneBot calls used by the auto-recall flow."""

    def __init__(self) -> None:
        self.actions: list[tuple[str, dict[str, object]]] = []

    async def call_action(self, action: str, **kwargs: object) -> dict[str, int] | None:
        """Record an action sent to the OneBot adapter."""
        self.actions.append((action, kwargs))
        if action.startswith("send_"):
            return {"message_id": 123}
        return None


class FakeRecallEvent(FakeEvent):
    """Event double that exposes the OneBot bot used for recall."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.bot = FakeRecallBot()


def make_plugin(
    config: dict[str, object], payload: object
) -> tuple[object, FakeSession]:
    """Build a plugin instance without requiring a live AstrBot context."""
    plugin = CrimsonCosmosPlugin.__new__(CrimsonCosmosPlugin)
    plugin._config = config
    session = FakeSession(payload)
    plugin._session = session
    return plugin, session


async def collect_results(plugin: object, event: FakeEvent) -> list[tuple[str, str]]:
    """Collect results yielded by the async message handler."""
    return [result async for result in plugin.on_message(event)]


def stub_temp_dir(tmp_path: Path):
    """把插件的临时目录重定向到 ``tmp_path``，测试结束后恢复。"""
    original = MODULE.get_astrbot_temp_path
    MODULE.get_astrbot_temp_path = lambda: str(tmp_path)

    class _Restore:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            MODULE.get_astrbot_temp_path = original

    return _Restore()


def test_should_describe_group_replies_as_an_enabled_allowlist() -> None:
    """The group access copy matches its enable-switch and allowlist behavior."""
    schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    access_items = schema["access_settings"]["items"]

    assert access_items["enable_group"]["description"] == "启用群聊回复"
    assert (
        access_items["enable_group"]["hint"]
        == "开启后启用群聊回复；列表留空时回复所有群聊。"
    )
    assert access_items["allowed_group_ids"]["description"] == "启用群聊回复的群号列表"
    assert (
        access_items["allowed_group_ids"]["hint"]
        == "留空时回复所有群聊；填写后只回复列表中的群聊。"
    )


def test_should_allow_all_groups_when_the_group_allowlist_is_empty() -> None:
    """An enabled group channel treats an empty allowlist as unrestricted."""
    cases = [
        (False, ["10001"], "10001", False),
        (True, ["10001", 10002], "10002", True),
        (True, ["10001"], "99999", False),
        (True, [], "10001", True),
    ]

    for enabled, allowed_group_ids, group_id, expected in cases:
        plugin, _session = make_plugin(
            {
                "enable_group": enabled,
                "allowed_group_ids": allowed_group_ids,
            },
            None,
        )

        assert (
            plugin._is_event_allowed(FakeEvent("色图", group_id=group_id)) is expected
        )


def test_should_expose_a_private_reply_allowlist() -> None:
    """The private allowlist explains that an empty list permits every user."""
    schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    item = schema["access_settings"]["items"]["allowed_private_user_ids"]

    assert item["type"] == "list"
    assert item["description"] == "启用私信回复的 QQ 号列表"
    assert item["hint"] == "留空时回复所有私信；填写后只回复列表中的 QQ 号。"
    assert item["default"] == []


def test_should_expose_administrators_without_restoring_immediate_command() -> None:
    """Administrator IDs bypass cooldowns without restoring the removed command."""
    schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))

    assert not hasattr(CrimsonCosmosPlugin, "immediate_image")
    administrators = schema["access_settings"]["items"]["admin_user_ids"]
    assert administrators["type"] == "list"
    assert administrators["default"] == []


def test_should_query_wallhaven_with_each_selected_category_when_triggered() -> None:
    """Wallhaven requests select General, Anime, and People via its bit mask."""
    expected_masks = {
        "general": "100",
        "anime": "010",
        "people": "001",
    }

    for category, expected_mask in expected_masks.items():
        plugin, session = make_plugin(
            {
                "enable_group": True,
                "enable_private": False,
                "allowed_group_ids": ["10001"],
                "keywords": ["色图"],
                "keyword_match_mode": "exact",
                "image_source": "wallhaven",
                "wallhaven_api_key": "test-key",
                "wallhaven_categories": [category],
            },
            {"data": [{"path": "https://images.example/wallhaven.jpg"}]},
        )

        results = asyncio.run(collect_results(plugin, FakeEvent("色图")))

        assert results == [("image", "https://images.example/wallhaven.jpg")]
        assert session.calls == [
            (
                "https://wallhaven.cc/api/v1/search",
                {
                    "apikey": "test-key",
                    "categories": expected_mask,
                    "purity": "001",
                    "sorting": "random",
                },
            )
        ]


def test_should_query_wallhaven_with_each_selected_content_rating() -> None:
    """Wallhaven content ratings are translated into its purity bit mask."""
    expected_masks = {
        "SFW": "100",
        "Sketchy": "010",
        "NSFW": "001",
    }

    for rating, expected_mask in expected_masks.items():
        plugin, session = make_plugin(
            {
                "enable_group": True,
                "enable_private": False,
                "allowed_group_ids": ["10001"],
                "keywords": ["色图"],
                "keyword_match_mode": "exact",
                "image_source": "wallhaven",
                "wallhaven_api_key": "test-key",
                "wallhaven_categories": ["anime"],
                "wallhaven_purity": [rating],
            },
            {"data": [{"path": "https://images.example/wallhaven.jpg"}]},
        )

        results = asyncio.run(collect_results(plugin, FakeEvent("色图")))

        assert results == [("image", "https://images.example/wallhaven.jpg")]
        assert session.calls[0][1]["purity"] == expected_mask


def test_should_translate_chinese_wallhaven_filters_and_sort_modes() -> None:
    """Chinese Wallhaven options map to the expected API parameters."""
    expected_sorting = {
        "最新": {"sorting": "date_added"},
        "热门": {"sorting": "toplist", "topRange": "1d"},
        "榜单": {"sorting": "toplist", "topRange": "1M"},
    }

    for sort_mode, sorting_params in expected_sorting.items():
        plugin, session = make_plugin(
            {
                "enable_group": True,
                "enable_private": False,
                "allowed_group_ids": ["10001"],
                "keywords": ["色图"],
                "keyword_match_mode": "exact",
                "image_source": "wallhaven",
                "wallhaven_api_key": "test-key",
                "wallhaven_categories": ["动漫"],
                "wallhaven_purity": ["擦边"],
                "wallhaven_sorting": sort_mode,
            },
            {"data": [{"path": "https://images.example/wallhaven.jpg"}]},
        )

        results = asyncio.run(collect_results(plugin, FakeEvent("色图")))

        assert results == [("image", "https://images.example/wallhaven.jpg")]
        assert session.calls == [
            (
                "https://wallhaven.cc/api/v1/search",
                {
                    "apikey": "test-key",
                    "categories": "010",
                    "purity": "010",
                    **sorting_params,
                },
            )
        ]


def test_should_rotate_wallhaven_images_across_consecutive_requests() -> None:
    """Consecutive Wallhaven requests do not always return the first result."""
    plugin, _session = make_plugin(
        {
            "wallhaven_api_key": "test-key",
            "wallhaven_categories": ["动漫"],
            "wallhaven_purity": ["擦边"],
            "wallhaven_sorting": "榜单",
        },
        {
            "data": [
                {"path": "https://images.example/first.jpg"},
                {"path": "https://images.example/second.jpg"},
                {"path": "https://images.example/third.jpg"},
            ]
        },
    )

    first = asyncio.run(plugin._fetch_wallhaven_images(1))
    second = asyncio.run(plugin._fetch_wallhaven_images(1))

    assert first == ["https://images.example/first.jpg"]
    assert second == ["https://images.example/second.jpg"]


def test_should_include_configured_wallhaven_tags_in_the_search_query() -> None:
    """Configured Wallhaven tags narrow the NSFW search request."""
    plugin, session = make_plugin(
        {
            "enable_group": True,
            "enable_private": False,
            "allowed_group_ids": ["10001"],
            "keywords": ["色图"],
            "keyword_match_mode": "exact",
            "image_source": "wallhaven",
            "wallhaven_api_key": "test-key",
            "wallhaven_categories": ["anime"],
            "wallhaven_tags": ["bra", "black lingerie", ""],
        },
        {"data": [{"path": "https://images.example/wallhaven.jpg"}]},
    )

    results = asyncio.run(collect_results(plugin, FakeEvent("色图")))

    assert results == [("image", "https://images.example/wallhaven.jpg")]
    assert session.calls == [
        (
            "https://wallhaven.cc/api/v1/search",
            {
                "apikey": "test-key",
                "categories": "010",
                "purity": "001",
                "sorting": "random",
                "q": "bra black lingerie",
            },
        )
    ]


def test_should_return_three_wallhaven_images_for_a_chinese_quantity_request() -> None:
    """A request such as ``来三份色图`` returns three Wallhaven images."""
    plugin, session = make_plugin(
        {
            "enable_group": True,
            "enable_private": False,
            "allowed_group_ids": ["10001"],
            "keywords": ["色图"],
            "keyword_match_mode": "exact",
            "image_source": "wallhaven",
            "wallhaven_api_key": "test-key",
            "wallhaven_categories": ["anime"],
        },
        {
            "data": [
                {"path": "https://images.example/one.jpg"},
                {"path": "https://images.example/two.jpg"},
                {"path": "https://images.example/three.jpg"},
            ]
        },
    )

    results = asyncio.run(collect_results(plugin, FakeEvent("来三份色图")))

    assert results == [
        ("image", "https://images.example/one.jpg"),
        ("image", "https://images.example/two.jpg"),
        ("image", "https://images.example/three.jpg"),
    ]
    assert len(session.calls) == 1


def test_should_show_configured_notice_when_disabled_group_matches_keyword() -> None:
    """A disabled group gets one controlled notice only after a keyword match."""
    plugin, session = make_plugin(
        {
            "enable_group": False,
            "keywords": ["色图"],
            "keyword_match_mode": "contains",
            "group_disabled_message": "本喵暂时不提供此服务喵~",
            "block_other_handlers": True,
        },
        None,
    )
    matched_event = FakeEvent("此群聊里有坏银，来点色图")

    matched_results = asyncio.run(collect_results(plugin, matched_event))
    unmatched_results = asyncio.run(collect_results(plugin, FakeEvent("普通聊天")))
    private_results = asyncio.run(
        collect_results(plugin, FakeEvent("色图", private=True))
    )

    assert matched_results == [("text", "本喵暂时不提供此服务喵~")]
    assert matched_event.stopped is True
    assert unmatched_results == []
    assert private_results == []
    assert session.calls == []


def test_should_expose_the_disabled_group_notice_in_message_settings() -> None:
    """The WebUI exposes the controlled notice with the requested default copy."""
    schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))

    field = schema["message_settings"]["items"]["group_disabled_message"]

    assert field["description"] == "群聊关闭提示"
    assert field["default"] == "本喵暂时不提供此服务喵~"
    assert "群聊回复关闭且触发关键词时发送" in field["hint"]


def test_should_parse_alias_tags_before_a_suffix_keyword(tmp_path: Path) -> None:
    """A tag directly before the keyword is converted through the alias map."""
    plugin, session = make_plugin(
        {
            "enable_group": True,
            "allowed_group_ids": ["10001"],
            "keywords": ["涩图"],
            "keyword_match_mode": "exact",
            "image_source": "lolicon",
            "lolicon_tag_aliases": "DeepSeek=deepseek",
            "lolicon_image_size": "small",
        },
        {
            "error": "",
            "data": [
                {
                    "pid": 123,
                    "urls": {"small": "https://images.example/deepseek.jpg"},
                }
            ],
        },
    )

    with stub_temp_dir(tmp_path):
        results = asyncio.run(collect_results(plugin, FakeEvent("DeepSeek涩图")))

    assert len(results) == 1
    assert results[0][0] == "image"
    assert results[0][1].startswith(str(tmp_path))
    assert session.post_calls[0][1]["tag"] == [["deepseek"]]


def test_should_merge_defaults_close_the_session_and_report_invalid_sources() -> None:
    """Construction and safe configuration failures do not leave a session open."""
    plugin = CrimsonCosmosPlugin(
        None,
        {
            "enable_group": True,
            "allowed_group_ids": ["10001"],
            "image_source": "unsupported",
        },
    )
    session = CloseableSession()
    plugin._session = session

    results = asyncio.run(collect_results(plugin, FakeEvent("色图")))
    asyncio.run(plugin.terminate())

    assert results == [
        ("text", "正在获取喵~"),
        ("text", "涩图获取失败了喵，请稍后再试~"),
    ]
    assert session.closed is True
    assert plugin._session is None


def test_should_retry_failed_base64_image_as_a_local_temp_file(tmp_path: Path) -> None:
    """NapCat base64 failures retry once through a temporary local file URI."""

    class Bot:
        def __init__(self) -> None:
            self.actions: list[tuple[str, dict[str, object]]] = []
            self.local_file_existed = False

        async def call_action(self, action: str, **kwargs: object) -> dict[str, int]:
            self.actions.append((action, kwargs))
            image_ref = kwargs["message"][0]["data"]["file"]
            if len(self.actions) == 1:
                raise RuntimeError("base64 upload failed")
            local_path = Path(MODULE.urlsplit(image_ref).path.lstrip("/"))
            if MODULE.urlsplit(image_ref).netloc:
                local_path = Path(
                    f"//{MODULE.urlsplit(image_ref).netloc}{MODULE.urlsplit(image_ref).path}"
                )
            if len(local_path.parts) and local_path.parts[0].endswith(":"):
                local_path = Path(str(local_path))
            self.local_file_existed = local_path.exists()
            return {"message_id": 123}

    class Event(FakeRecallEvent):
        def __init__(self) -> None:
            super().__init__("色图")
            self.bot = Bot()

    plugin, _session = make_plugin({"auto_recall": False}, None)
    event = Event()
    original_temp_path = MODULE.get_astrbot_temp_path
    MODULE.get_astrbot_temp_path = lambda: str(tmp_path)
    try:
        sent = asyncio.run(
            plugin._send_image_with_auto_recall(
                event,
                "base64:///9j/aW1hZ2UtYnl0ZXM=",
                failure_message="发送失败",
            )
        )
    finally:
        MODULE.get_astrbot_temp_path = original_temp_path

    assert sent is True
    assert event.bot.local_file_existed is True
    assert event.bot.actions[1][1]["message"][0]["data"]["file"].startswith("file:///")
    assert list(tmp_path.iterdir()) == []


def test_should_not_send_pixiv_pid_when_image_delivery_fails(tmp_path: Path) -> None:
    """A delivery failure must not be followed by a standalone Pixiv PID."""

    class Bot:
        def __init__(self) -> None:
            self.actions: list[tuple[str, dict[str, object]]] = []

        async def call_action(self, action: str, **kwargs: object) -> dict[str, int]:
            self.actions.append((action, kwargs))
            if kwargs["message"][0]["type"] == "image":
                raise RuntimeError("image delivery failed")
            return {"message_id": 123}

    class Event(FakeRecallEvent):
        def __init__(self) -> None:
            super().__init__("色图")
            self.bot = Bot()

    plugin, _session = make_plugin(
        {
            "enable_group": True,
            "allowed_group_ids": ["10001"],
            "keywords": ["色图"],
            "image_source": "lolicon",
            "lolicon_image_size": "small",
            "show_pixiv_pid": True,
            "fetching_message": "",
            "failure_message": "图片发送失败，请稍后重试。",
            "auto_recall": False,
        },
        {
            "error": "",
            "data": [
                {
                    "pid": 123,
                    "urls": {"small": "https://images.example/one.jpg"},
                }
            ],
        },
    )
    event = Event()

    with stub_temp_dir(tmp_path):
        results = asyncio.run(collect_results(plugin, event))

    assert results == []
    assert event.bot.actions[2][1]["message"] == [
        {
            "type": "text",
            "data": {"text": "图片发送失败，请稍后重试。"},
        }
    ]


def test_should_send_jm_file_directly_when_auto_recall_is_disabled(tmp_path) -> None:
    """JM files use the direct OneBot path before the generic file component."""
    plugin, _ = make_plugin({"auto_recall": False}, None)
    event = FakeRecallEvent("/jm 下载 456")
    file_path = tmp_path / "JM456.zip"
    file_path.write_bytes(b"zip")

    sent = asyncio.run(plugin._send_file_with_auto_recall(event, file_path, "完成"))

    assert sent is True
    assert event.bot.actions[0] == (
        "send_group_msg",
        {
            "group_id": 10001,
            "message": [
                {"type": "text", "data": {"text": "完成"}},
                {"type": "file", "data": {"name": "JM456.zip", "file": str(file_path)}},
            ],
        },
    )


def test_should_fetch_lolicon_images_with_filters_aliases_and_pid_output(
    tmp_path: Path,
) -> None:
    """Lolicon receives configured filters and returns downloaded local images."""
    plugin, session = make_plugin(
        {
            "enable_group": True,
            "allowed_group_ids": ["10001"],
            "keywords": ["色图"],
            "image_source": "lolicon",
            "lolicon_r18_mode": "mix",
            "lolicon_exclude_ai": True,
            "lolicon_aspect_ratio": "lt1",
            "lolicon_image_size": "regular",
            "lolicon_proxy": "https://proxy.example",
            "lolicon_tag_aliases": "白丝=white_pantyhose\n猫耳=cat_ears",
            "show_pixiv_pid": True,
        },
        {
            "error": "",
            "data": [
                {
                    "pid": 123,
                    "urls": {"regular": "https://images.example/one.jpg"},
                },
                {
                    "pid": 456,
                    "urls": {"regular": "https://images.example/two.jpg"},
                },
            ],
        },
    )

    with stub_temp_dir(tmp_path):
        results = asyncio.run(
            collect_results(plugin, FakeEvent("来两份白丝、猫耳色图"))
        )

    assert results[0][0] == "image"
    assert results[1][0] == "image"
    assert results[0][1].startswith(str(tmp_path))
    assert results[1][1].startswith(str(tmp_path))
    assert results[2] == ("text", "Pixiv PID: 123,456")
    assert session.post_calls == [
        (
            "https://api.lolicon.app/setu/v2",
            {
                "r18": 2,
                "num": 2,
                "excludeAI": True,
                "size": ["regular"],
                "tag": [["white_pantyhose", "cat_ears"]],
                "aspectRatio": "lt1",
                "proxy": "https://proxy.example",
            },
        )
    ]


def test_should_fall_back_to_the_next_pixiv_image_proxy(tmp_path: Path) -> None:
    """Lolicon image URLs use the first reachable configured proxy."""
    plugin, session = make_plugin(
        {
            "lolicon_image_size": "small",
            "lolicon_proxy_order": [
                "https://i.loli.best",
                "https://i.pixiv.nl",
            ],
            "lolicon_proxy_timeout_seconds": 4,
        },
        {
            "error": "",
            "data": [
                {
                    "pid": 123,
                    "urls": {
                        "small": "https://i.pximg.net/c/540x540_70/img-master/img/2026/01/02/03/04/05/123_p0_master1200.jpg"
                    },
                }
            ],
        },
    )
    original_get = session.get
    attempted_urls: list[str] = []

    def get_with_failed_primary(
        url: str,
        *,
        params: dict[str, str] | None = None,
        timeout: object = None,
        headers: dict[str, str] | None = None,
    ) -> FakeResponse:
        attempted_urls.append(url)
        if url.startswith("https://i.loli.best/"):
            raise MODULE.aiohttp.ClientError("primary proxy unavailable")
        return original_get(url, params=params, timeout=timeout, headers=headers)

    session.get = get_with_failed_primary

    with stub_temp_dir(tmp_path):
        paths, pids = asyncio.run(plugin._fetch_lolicon_images(1))

    assert len(paths) == 1
    assert Path(paths[0]).read_bytes() == b"image-bytes"
    assert pids == ["123"]
    assert attempted_urls == [
        "https://i.loli.best/c/540x540_70/img-master/img/2026/01/02/03/04/05/123_p0_master1200.jpg",
        "https://i.pixiv.nl/c/540x540_70/img-master/img/2026/01/02/03/04/05/123_p0_master1200.jpg",
    ]


def test_should_expose_lolicon_settings_in_the_config_schema() -> None:
    """AstrBot's config UI exposes the supported Lolicon controls."""
    schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    sources = schema["source_settings"]["items"]
    lolicon = schema["lolicon_settings"]["items"]

    assert "lolicon" in sources["image_source"]["options"]
    assert "lolicon" in sources["image_source_order"]["options"]
    assert lolicon["lolicon_r18_mode"]["default"] == "r18"
    assert lolicon["lolicon_exclude_ai"]["default"] is True
    assert lolicon["lolicon_image_size"]["default"] == "small"
    image_size_hint = lolicon["lolicon_image_size"]["hint"]
    assert "original 原图" in image_size_hint
    assert "regular 常规尺寸" in image_size_hint
    assert "small 小尺寸" in image_size_hint
    assert "thumb 缩略图" in image_size_hint
    assert "mini 极小缩略图" in image_size_hint
    assert lolicon["lolicon_proxy_order"]["default"][0] == "https://i.pixiv.re"


def test_should_not_expose_removed_danbooru_source() -> None:
    """Danbooru is absent from configuration and runtime behavior."""
    schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    sources = schema["source_settings"]["items"]

    assert "danbooru" not in sources["image_source"]["options"]
    assert "danbooru" not in sources["image_source_order"]["options"]
    assert "danbooru_settings" not in schema
    assert not hasattr(CrimsonCosmosPlugin, "_fetch_danbooru_images")


def test_should_expose_chinese_wallhaven_filters_and_sorting_in_the_config_schema() -> (
    None
):
    """The WebUI provides Chinese Wallhaven filters and sorting options."""
    schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    wallhaven = schema["wallhaven_settings"]["items"]

    assert wallhaven["wallhaven_categories"]["options"] == ["通用", "动漫", "人物"]
    assert wallhaven["wallhaven_categories"]["default"] == ["动漫"]
    assert wallhaven["wallhaven_purity"]["options"] == [
        "全年龄",
        "擦边",
        "成人",
    ]
    assert wallhaven["wallhaven_purity"]["default"] == ["成人"]
    assert wallhaven["wallhaven_sorting"]["options"] == ["最新", "热门", "榜单"]
    assert wallhaven["wallhaven_sorting"]["default"] == "最新"


def test_should_expose_message_prompts_as_a_separate_config_card() -> None:
    """Fetching and failure prompts are editable in their own WebUI card."""
    schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    messages = schema["message_settings"]["items"]

    assert messages["fetching_message"]["default"] == "正在获取喵~"
    assert messages["failure_message"]["default"] == "涩图获取失败了喵，请稍后再试~"


def test_should_expose_cooldown_settings_in_the_config_schema() -> None:
    """The WebUI exposes the cooldown duration and configurable prompt."""
    schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    trigger = schema["trigger_settings"]["items"]
    messages = schema["message_settings"]["items"]

    assert trigger["cooldown_seconds"]["type"] == "int"
    assert trigger["cooldown_seconds"]["default"] == 0
    assert messages["cooldown_message"]["default"] == "冷却中呢喵~"


def test_should_expose_other_handler_blocking_in_trigger_settings() -> None:
    """The trigger card exposes the active-reply suppression switch."""
    schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    setting = schema["trigger_settings"]["items"]["block_other_handlers"]

    assert setting["default"] is True


JABLE_LIST_MARKDOWN = """
[![Image 7](https://assets-cdn.jable.tv/contents/videos_screenshots/61000/61158/320x180/1.jpg) 2:23:16](https://jable.tv/videos/snos-361/)
###### [SNOS-361 测试影片标题](https://jable.tv/videos/snos-361/)
852 388 6250
"""
JABLE_DETAIL_MARKDOWN = """
Title: SNOS-361 测试影片标题
##### [角色剧情](https://jable.tv/categories/roleplay/)•[长身](https://jable.tv/tags/tall/)[汽车](https://jable.tv/tags/car/)
"""
MISSAV_SEARCH_HTML = """
<a class="thumbnail group" href="https://missav.ws/dm44/cn/ssis-001" alt="ssis-001">
<a href="https://missav.ws/dm44/cn/ssis-001" alt="ssis-001">SSIS-001 测试影片</a>
<a class="thumbnail group" href="https://missav.ws/dm52/cn/ssis-001-uncensored-leak" alt="ssis-001-uncensored-leak">
"""
MISSAV_DETAIL_HTML = """
<meta property="og:title" content="SSIS-001 测试影片">
<meta property="og:image" content="https://images.example/ssis-001.jpg">
<a rel="nofollow" href="magnet:?xt=urn:btih:1111111111111111111111111111111111111111&amp;dn=one">one</a>
<a rel="nofollow" href="magnet:?xt=urn:btih:2222222222222222222222222222222222222222&amp;dn=two">two</a>
<a rel="nofollow" href="magnet:?xt=urn:btih:3333333333333333333333333333333333333333&amp;dn=three">three</a>
<a rel="nofollow" href="magnet:?xt=urn:btih:4444444444444444444444444444444444444444&amp;dn=four">four</a>
<a rel="nofollow" href="magnet:?xt=urn:btih:5555555555555555555555555555555555555555&amp;dn=five">five</a>
<a rel="nofollow" href="magnet:?xt=urn:btih:6666666666666666666666666666666666666666&amp;dn=six">six</a>
"""
MISSAV_SEARCH_MARKDOWN = """
Title: ssis-001的搜寻结果
Markdown Content:
[中文字幕](https://missav.ws/dm278/cn/chinese-subtitle)
# ssis-001的搜寻结果
[SSIS-001 测试影片](https://missav.ws/dm44/cn/ssis-001)
[SSIS-001 流出版](https://missav.ws/dm52/cn/ssis-001-uncensored-leak)
[返回最顶](https://missav.ws/cn/search/ssis-001#)
"""
MISSAV_DETAIL_MARKDOWN = """
Title: SSIS-001 测试影片
URL Source: https://missav.ws/dm44/cn/ssis-001
[one](magnet:?xt=urn:btih:1111111111111111111111111111111111111111&dn=one)
[two](magnet:?xt=urn:btih:2222222222222222222222222222222222222222&dn=two)
"""


def test_should_report_a_ranked_jable_video_from_an_av_command() -> None:
    """The AV command returns cover, code, title, stars, tags, and optional link."""
    plugin, _session = make_plugin(
        {
            "enable_group": True,
            "allowed_group_ids": ["10001"],
            "fetching_message": "正在获取喵~",
            "failure_message": "获取失败",
            "block_other_handlers": True,
            "auto_recall": False,
            "jable_show_detail_link": True,
        },
        None,
    )
    plugin._session = RoutedSession(
        {
            "https://r.jina.ai/https://jable.tv/hot/?sort_by=video_viewed_month": [
                JABLE_LIST_MARKDOWN
            ],
            "https://r.jina.ai/https://jable.tv/videos/snos-361/": [
                JABLE_DETAIL_MARKDOWN
            ],
        }
    )
    event = FakeEvent("/av 热门 本月 1")

    async def collect_jable() -> list[tuple[str, str]]:
        return [
            result
            async for result in plugin._handle_jable_command(event, event.message)
        ]

    results = asyncio.run(collect_jable())

    assert results == [
        ("text", "正在获取喵~"),
        (
            "chain",
            [
                {
                    "type": "Image",
                    "file": "https://assets-cdn.jable.tv/contents/videos_screenshots/61000/61158/320x180/1.jpg",
                },
                {
                    "type": "Plain",
                    "text": (
                        "🎬 本月热门 第 1 名\n"
                        "车牌号：SNOS-361\n"
                        "标题：测试影片标题\n"
                        "⭐ 红星：6250\n"
                        "#主题：#角色剧情 #长身 #汽车\n"
                        "链接：https://jable.tv/videos/snos-361/"
                    ),
                },
            ],
        ),
    ]
    assert event.stopped is True


def test_should_search_missav_and_report_the_selected_video() -> None:
    """The MissAV search command returns the selected result and cover."""
    plugin, _session = make_plugin(
        {
            "enable_group": True,
            "allowed_group_ids": ["10001"],
            "fetching_message": "正在获取喵~",
            "failure_message": "获取失败",
            "block_other_handlers": True,
            "auto_recall": False,
        },
        None,
    )
    plugin._session = RoutedSession(
        {
            "https://missav.ws/cn/search/SSIS-001": [MISSAV_SEARCH_HTML],
            "https://missav.ws/dm44/cn/ssis-001": [MISSAV_DETAIL_HTML],
        }
    )
    event = FakeEvent("/av 搜索 SSIS-001 1")

    async def run() -> list[object]:
        return [result async for result in plugin.av_search(event, "SSIS-001", 1)]

    assert asyncio.run(run()) == [
        ("text", "正在获取喵~"),
        (
            "chain",
            [
                {"type": "Image", "file": "https://images.example/ssis-001.jpg"},
                {
                    "type": "Plain",
                    "text": (
                        "🎬 MissAV 搜索 第 1 名\n"
                        "车牌号：SSIS-001\n"
                        "标题：SSIS-001 测试影片\n"
                        "链接：https://missav.ws/dm44/cn/ssis-001"
                    ),
                },
            ],
        ),
    ]
    assert event.stopped is True


def test_should_return_only_five_missav_magnets_for_an_exact_code() -> None:
    """The magnet command prefers the exact work and caps the response."""
    plugin, _session = make_plugin(
        {
            "enable_group": True,
            "allowed_group_ids": ["10001"],
            "fetching_message": "",
            "failure_message": "获取失败",
            "block_other_handlers": True,
        },
        None,
    )
    plugin._session = RoutedSession(
        {
            "https://missav.ws/cn/search/SSIS-001": [MISSAV_SEARCH_HTML],
            "https://missav.ws/dm44/cn/ssis-001": [MISSAV_DETAIL_HTML],
        }
    )
    event = FakeEvent("/av 磁力 SSIS-001")

    async def run() -> list[object]:
        return [result async for result in plugin.av_magnet(event, "SSIS-001")]

    results = asyncio.run(run())
    assert len(results) == 1
    assert results[0][0] == "text"
    assert "SSIS-001 测试影片" in results[0][1]
    assert results[0][1].count("magnet:?xt=urn:btih:") == 5
    assert "&amp;" not in results[0][1]
    assert "&dn=one" in results[0][1]
    assert "6666666666666666666666666666666666666666" not in results[0][1]
    assert event.stopped is True


def test_should_reject_non_missav_magnet_urls_without_requesting_them() -> None:
    """Only MissAV detail URLs are accepted by the magnet command."""
    plugin, session = make_plugin(
        {
            "enable_group": True,
            "allowed_group_ids": ["10001"],
            "block_other_handlers": True,
        },
        None,
    )
    event = FakeEvent("/av 磁力 https://example.com/video")

    async def run() -> list[object]:
        return [
            result
            async for result in plugin.av_magnet(event, "https://example.com/video")
        ]

    assert asyncio.run(run()) == [("text", "用法：/av 磁力 <番号或 MissAV 详情链接>")]
    assert session.calls == []
    assert event.stopped is True


def test_should_fall_back_to_jina_when_missav_returns_a_challenge() -> None:
    """Cloudflare challenge pages fall back to the existing Jina reader."""
    plugin, _session = make_plugin(
        {
            "enable_group": True,
            "allowed_group_ids": ["10001"],
            "fetching_message": "",
            "failure_message": "获取失败",
            "block_other_handlers": True,
        },
        None,
    )
    challenge = "<script src='/cdn-cgi/challenge-platform/main.js'></script>"
    search_url = "https://missav.ws/cn/search/SSIS-001"
    detail_url = "https://missav.ws/dm44/cn/ssis-001"
    plugin._session = RoutedSession(
        {
            search_url: [challenge, challenge],
            f"https://r.jina.ai/{search_url}": [MISSAV_SEARCH_MARKDOWN],
            detail_url: [challenge, challenge],
            f"https://r.jina.ai/{detail_url}": [MISSAV_DETAIL_MARKDOWN],
        }
    )
    original_sleep = MODULE.asyncio.sleep

    async def no_sleep(_seconds: float) -> None:
        return None

    async def run() -> list[object]:
        return [result async for result in plugin.av_magnet(event, "SSIS-001")]

    event = FakeEvent("/av 磁力 SSIS-001")
    MODULE.asyncio.sleep = no_sleep
    try:
        results = asyncio.run(run())
    finally:
        MODULE.asyncio.sleep = original_sleep

    assert len(results) == 1
    assert results[0][0] == "text"
    assert results[0][1].count("magnet:?xt=urn:btih:") == 2
    assert f"https://r.jina.ai/{search_url}" in {
        url for url, _params in plugin._session.calls
    }


def test_should_ignore_navigation_links_in_jina_search_results() -> None:
    """Jina navigation links before the result heading are not ranked."""
    plugin, _session = make_plugin(
        {
            "enable_group": True,
            "allowed_group_ids": ["10001"],
            "fetching_message": "",
            "failure_message": "获取失败",
            "block_other_handlers": True,
            "auto_recall": False,
        },
        None,
    )
    challenge = "<script src='/cdn-cgi/challenge-platform/main.js'></script>"
    search_url = "https://missav.ws/cn/search/SSIS-001"
    detail_url = "https://missav.ws/dm44/cn/ssis-001"
    plugin._session = RoutedSession(
        {
            search_url: [challenge, challenge],
            f"https://r.jina.ai/{search_url}": [MISSAV_SEARCH_MARKDOWN],
            detail_url: [challenge, challenge],
            f"https://r.jina.ai/{detail_url}": [MISSAV_DETAIL_MARKDOWN],
        }
    )
    original_sleep = MODULE.asyncio.sleep

    async def no_sleep(_seconds: float) -> None:
        return None

    async def run() -> list[object]:
        return [result async for result in plugin.av_search(event, "SSIS-001", 1)]

    event = FakeEvent("/av 搜索 SSIS-001 1")
    MODULE.asyncio.sleep = no_sleep
    try:
        results = asyncio.run(run())
    finally:
        MODULE.asyncio.sleep = original_sleep

    assert results[0][0] == "chain"
    assert "链接：https://missav.ws/dm44/cn/ssis-001" in results[0][1][1][
        "text"
    ].replace(" ", "")


def test_should_map_supported_av_commands_and_reject_invalid_ranks() -> None:
    """All confirmed AV query types map to Jable reader URLs."""
    cases = {
        "/av 热门 今日 1": "https://jable.tv/hot/?sort_by=video_viewed_today",
        "/av 新片 1": "https://jable.tv/latest-updates/",
        "/av 主题 黑丝 1": "https://jable.tv/tags/black-pantyhose/",
        "/av 女优 河北彩花 1": "https://jable.tv/search/?q=%E6%B2%B3%E5%8C%97%E5%BD%A9%E8%8A%B1",
    }

    for message, expected_url in cases.items():
        request = CrimsonCosmosPlugin._parse_jable_request(message)
        assert request is not None
        assert request[0] == expected_url

    assert CrimsonCosmosPlugin._parse_jable_request("/av 热门 本月 0") is None
    assert CrimsonCosmosPlugin._parse_jable_request("/av 新片 31") is None


def test_should_support_four_sort_modes_for_themes_and_models() -> None:
    """Theme and model commands map all four Jable list sort modes."""
    sort_values = {
        "近期最佳": "post_date_and_popularity",
        "最近更新": "post_date",
        "最多观看": "video_viewed",
        "最高收藏": "most_favourited",
    }

    for label, value in sort_values.items():
        theme = CrimsonCosmosPlugin._parse_jable_request(f"/av 主题 黑丝 {label} 1")
        model = CrimsonCosmosPlugin._parse_jable_request(f"/av 女优 河北彩花 {label} 1")
        assert theme is not None and model is not None
        assert theme[0] == f"https://jable.tv/tags/black-pantyhose/?sort_by={value}"
        assert model[0] == (
            f"https://jable.tv/search/?q=%E6%B2%B3%E5%8C%97%E5%BD%A9%E8%8A%B1&sort_by={value}"
        )


def test_should_parse_rank_ranges_up_to_ten_videos() -> None:
    """AV commands accept a bounded inclusive rank range."""
    request = CrimsonCosmosPlugin._parse_jable_request("/av 热门 本月 1-10")

    assert request == (
        "https://jable.tv/hot/?sort_by=video_viewed_month",
        (1, 10),
        "本月热门",
    )
    assert CrimsonCosmosPlugin._parse_jable_request("/av 热门 本月 1-11") is None
    assert CrimsonCosmosPlugin._parse_jable_request("/av 热门 本月 10-1") is None


def test_should_send_jable_ranges_as_one_forward_chat_record() -> None:
    """Each video in a rank range becomes one image-and-text forward node."""
    plugin, _session = make_plugin(
        {
            "enable_group": True,
            "allowed_group_ids": ["10001"],
            "fetching_message": "正在获取喵~",
            "block_other_handlers": True,
            "auto_recall": False,
            "use_forward": True,
            "jable_show_detail_link": False,
        },
        None,
    )

    async def fetch_video(request: tuple[str, int, str]) -> dict[str, object]:
        rank = request[1]
        return {
            "cover": f"https://images.example/{rank}.jpg",
            "code": f"TEST-{rank:03d}",
            "title": f"影片 {rank}",
            "stars": str(rank * 100),
            "tags": ["测试"],
            "url": f"https://jable.tv/videos/test-{rank}/",
            "rank": rank,
            "list_name": "本月热门",
        }

    plugin._fetch_jable_video = fetch_video
    event = FakeRecallEvent("/av 热门 本月 1-3")

    async def run() -> list[object]:
        return [
            result
            async for result in plugin._handle_jable_command(event, event.message)
        ]

    results = asyncio.run(run())

    assert results == []
    assert event.bot.actions[0][0] == "send_group_forward_msg"
    action, payload = event.bot.actions[1]
    assert action == "send_group_forward_msg"
    assert len(payload["messages"]) == 3
    assert all(len(node["data"]["content"]) == 2 for node in payload["messages"])


def test_should_fetch_five_jable_details_concurrently() -> None:
    """A ten-item range uses bounded concurrency instead of serial pairs."""
    plugin, _session = make_plugin(
        {
            "enable_group": True,
            "allowed_group_ids": ["10001"],
            "fetching_message": "",
            "block_other_handlers": True,
            "auto_recall": False,
            "jable_show_cover": False,
        },
        None,
    )
    active = 0
    max_active = 0

    async def fetch_video(request: tuple[str, int, str]) -> dict[str, object]:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0)
        active -= 1
        rank = request[1]
        return {
            "cover": f"https://images.example/{rank}.jpg",
            "code": f"TEST-{rank:03d}",
            "title": f"影片 {rank}",
            "stars": str(rank * 100),
            "tags": ["测试"],
            "url": f"https://jable.tv/videos/test-{rank}/",
            "rank": rank,
            "list_name": "本月热门",
        }

    plugin._fetch_jable_video = fetch_video
    event = FakeRecallEvent("/av 热门 本月 1-10")

    async def run() -> list[object]:
        return [
            result
            async for result in plugin._handle_jable_command(event, event.message)
        ]

    asyncio.run(run())

    assert max_active == 5


def test_should_bound_the_whole_jable_range_to_sixty_seconds() -> None:
    """A range applies one command-level deadline around all ranked items."""
    plugin, _session = make_plugin(
        {
            "enable_group": True,
            "allowed_group_ids": ["10001"],
            "fetching_message": "",
            "block_other_handlers": True,
            "auto_recall": False,
            "jable_show_cover": False,
        },
        None,
    )

    async def fetch_video(request: tuple[str, int, str]) -> dict[str, object]:
        rank = request[1]
        return {
            "cover": f"https://images.example/{rank}.jpg",
            "code": f"TEST-{rank:03d}",
            "title": f"影片 {rank}",
            "stars": str(rank * 100),
            "tags": ["测试"],
            "url": f"https://jable.tv/videos/test-{rank}/",
            "rank": rank,
            "list_name": "本月热门",
        }

    plugin._fetch_jable_video = fetch_video
    event = FakeRecallEvent("/av 热门 本月 1-10")
    original_wait = MODULE.asyncio.wait
    timeouts: list[float | None] = []

    async def tracking_wait(tasks: object, **kwargs: object):
        timeouts.append(kwargs.get("timeout"))
        return await original_wait(tasks, **kwargs)

    MODULE.asyncio.wait = tracking_wait
    try:
        asyncio.run(anext(plugin._handle_jable_command(event, event.message), None))
    finally:
        MODULE.asyncio.wait = original_wait

    assert timeouts == [60]


def test_should_fetch_a_jable_listing_only_once_for_a_range() -> None:
    """A range reuses its listing while fetching details concurrently."""
    plugin, _session = make_plugin(
        {
            "enable_group": True,
            "allowed_group_ids": ["10001"],
            "fetching_message": "",
            "block_other_handlers": True,
            "auto_recall": False,
            "jable_show_detail_link": False,
        },
        None,
    )
    list_url = "https://r.jina.ai/https://jable.tv/hot/?sort_by=video_viewed_month"
    plugin._session = FakeSession(None)
    reads: list[str] = []

    async def read_jina_text(url: str, _timeout: object) -> str:
        reads.append(url)
        await asyncio.sleep(0)
        return JABLE_LIST_MARKDOWN * 3 if url == list_url else JABLE_DETAIL_MARKDOWN

    plugin._read_jina_text = read_jina_text
    event = FakeRecallEvent("/av 热门 本月 1-3")

    async def run() -> list[object]:
        return [
            result
            async for result in plugin._handle_jable_command(event, event.message)
        ]

    asyncio.run(run())

    assert reads.count(list_url) == 1


def test_should_skip_jable_detail_when_themes_are_hidden() -> None:
    """Disabling themes avoids the optional detail-page request entirely."""
    plugin, _session = make_plugin({"jable_show_themes": False}, None)
    list_url = "https://r.jina.ai/https://jable.tv/latest-updates/"
    detail_url = "https://r.jina.ai/https://jable.tv/videos/snos-361/"
    plugin._session = RoutedSession(
        {
            list_url: [JABLE_LIST_MARKDOWN],
            detail_url: [JABLE_DETAIL_MARKDOWN],
        }
    )

    video = asyncio.run(
        plugin._fetch_jable_video(("https://jable.tv/latest-updates/", 1, "新片"))
    )

    assert video["tags"] == []
    assert [url for url, _params in plugin._session.calls].count(detail_url) == 0


def test_should_bound_jable_detail_timeout_and_retries() -> None:
    """Optional detail metadata must not hold a command for three long attempts."""
    plugin, _session = make_plugin({}, None)
    list_url = "https://r.jina.ai/https://jable.tv/latest-updates/"
    detail_url = "https://r.jina.ai/https://jable.tv/videos/snos-361/"

    class TimeoutTrackingSession(RoutedSession):
        def __init__(self, routes: dict[str, list[object]]) -> None:
            super().__init__(routes)
            self.timeouts: list[tuple[str, float | None]] = []

        def get(self, url: str, **kwargs: object) -> FakeResponse:
            timeout = kwargs.get("timeout")
            self.timeouts.append((url, getattr(timeout, "total", None)))
            return super().get(url, **kwargs)

    plugin._session = TimeoutTrackingSession(
        {
            list_url: [JABLE_LIST_MARKDOWN],
            detail_url: [asyncio.TimeoutError()] * 3,
        }
    )
    original_sleep = MODULE.asyncio.sleep

    async def no_sleep(_seconds: float) -> None:
        return None

    MODULE.asyncio.sleep = no_sleep
    try:
        video = asyncio.run(
            plugin._fetch_jable_video(("https://jable.tv/latest-updates/", 1, "新片"))
        )
    finally:
        MODULE.asyncio.sleep = original_sleep

    detail_timeouts = [
        total for url, total in plugin._session.timeouts if url == detail_url
    ]
    assert video["tags"] == ["详情暂不可用"]
    assert detail_timeouts == [10, 10]


def test_should_retry_a_rate_limited_jina_request() -> None:
    """Temporary Jina rate limits are retried before failing the item."""
    plugin, _session = make_plugin({}, None)
    list_url = "https://r.jina.ai/https://jable.tv/latest-updates/"
    detail_url = "https://r.jina.ai/https://jable.tv/videos/snos-361/"
    rate_limit = MODULE.aiohttp.ClientResponseError(
        request_info=None, history=(), status=429, message="rate limited"
    )
    plugin._session = RoutedSession(
        {
            list_url: [rate_limit, JABLE_LIST_MARKDOWN],
            detail_url: [JABLE_DETAIL_MARKDOWN],
        }
    )
    original_sleep = MODULE.asyncio.sleep

    async def no_sleep(_seconds: float) -> None:
        return None

    MODULE.asyncio.sleep = no_sleep
    try:
        video = asyncio.run(
            plugin._fetch_jable_video(("https://jable.tv/latest-updates/", 1, "新片"))
        )
    finally:
        MODULE.asyncio.sleep = original_sleep

    assert video["code"] == "SNOS-361"
    assert [url for url, _params in plugin._session.calls].count(list_url) == 2


def test_should_keep_a_video_when_its_detail_page_stays_rate_limited() -> None:
    """A failed detail page degrades tags instead of dropping the video."""
    plugin, _session = make_plugin({}, None)
    list_url = "https://r.jina.ai/https://jable.tv/latest-updates/"
    detail_url = "https://r.jina.ai/https://jable.tv/videos/snos-361/"
    rate_limits = [
        MODULE.aiohttp.ClientResponseError(
            request_info=None, history=(), status=429, message="rate limited"
        )
        for _ in range(3)
    ]
    plugin._session = RoutedSession(
        {list_url: [JABLE_LIST_MARKDOWN], detail_url: rate_limits}
    )
    original_sleep = MODULE.asyncio.sleep

    async def no_sleep(_seconds: float) -> None:
        return None

    MODULE.asyncio.sleep = no_sleep
    try:
        video = asyncio.run(
            plugin._fetch_jable_video(("https://jable.tv/latest-updates/", 1, "新片"))
        )
    finally:
        MODULE.asyncio.sleep = original_sleep

    assert video["code"] == "SNOS-361"
    assert video["tags"] == ["详情暂不可用"]


def test_should_expose_jable_settings_in_a_separate_config_card() -> None:
    """The WebUI exposes every Jable report field as an independent switch."""
    schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))

    assert schema["jable_settings"]["description"] == "【Jable 影片查询】"
    settings = schema["jable_settings"]["items"]
    assert set(settings) == {
        "enable_jable",
        "jable_show_cover",
        "jable_show_code",
        "jable_show_title",
        "jable_show_stars",
        "jable_show_themes",
        "jable_show_detail_link",
        "jina_api_key",
    }
    assert all(
        settings[name]["default"] is True
        for name in settings
        if name.startswith("jable_show_")
    )
    assert settings["jina_api_key"]["default"] == ""


def test_should_control_each_jable_report_field_independently() -> None:
    """Disabled Jable fields do not affect any independently enabled field."""
    plugin, _session = make_plugin(
        {
            "enable_group": True,
            "allowed_group_ids": ["10001"],
            "fetching_message": "",
            "block_other_handlers": True,
            "auto_recall": False,
            "jable_show_cover": False,
            "jable_show_code": True,
            "jable_show_title": False,
            "jable_show_stars": True,
            "jable_show_themes": False,
            "jable_show_detail_link": False,
        },
        None,
    )

    async def fetch_video(_request: tuple[str, int, str]) -> dict[str, object]:
        return {
            "cover": "https://images.example/1.jpg",
            "code": "TEST-001",
            "title": "隐藏标题",
            "stars": "123",
            "tags": ["隐藏主题"],
            "url": "https://jable.tv/videos/test-001/",
            "rank": 1,
            "list_name": "本月热门",
        }

    plugin._fetch_jable_video = fetch_video
    event = FakeRecallEvent("/av 热门 本月 1")

    async def run() -> list[object]:
        return [
            result
            async for result in plugin._handle_jable_command(event, event.message)
        ]

    assert asyncio.run(run()) == [
        ("text", "🎬 本月热门 第 1 名\n车牌号：TEST-001\n⭐ 红星：123")
    ]
    assert event.bot.actions == []


def test_should_fetch_the_second_jable_page_for_rank_25() -> None:
    """Ranks beyond the first 24 cards continue on the second page."""
    plugin, _session = make_plugin({}, None)
    first_url = "https://r.jina.ai/https://jable.tv/latest-updates/"
    second_url = "https://r.jina.ai/https://jable.tv/latest-updates/?from=2"
    plugin._session = RoutedSession(
        {
            first_url: [JABLE_LIST_MARKDOWN * 24],
            second_url: [JABLE_LIST_MARKDOWN],
            "https://r.jina.ai/https://jable.tv/videos/snos-361/": [
                JABLE_DETAIL_MARKDOWN
            ],
        }
    )

    video = asyncio.run(
        plugin._fetch_jable_video(("https://jable.tv/latest-updates/", 25, "新片"))
    )

    assert video["rank"] == 25
    assert video["code"] == "SNOS-361"
    assert [url for url, _params in plugin._session.calls] == [
        first_url,
        second_url,
        "https://r.jina.ai/https://jable.tv/videos/snos-361/",
    ]


def test_should_register_av_as_a_visible_command_group() -> None:
    """AstrBot's plugin behavior view lists the AV group and subcommands."""
    from astrbot.core.star.filter.command_group import CommandGroupFilter
    from astrbot.core.star.star_handler import star_handlers_registry

    handler = star_handlers_registry.get_handler_by_full_name(
        "crimson_cosmos_plugin_av"
    )

    assert handler is not None
    group = next(
        item for item in handler.event_filters if isinstance(item, CommandGroupFilter)
    )
    assert group.group_name == "av"
    assert {item.command_name for item in group.sub_command_filters} == {
        "热门",
        "新片",
        "主题",
        "女优",
        "搜索",
        "磁力",
    }
    assert CrimsonCosmosPlugin.av_search.__doc__.startswith("按番号")
    assert CrimsonCosmosPlugin.av_magnet.__doc__.startswith("获取")


def test_should_register_and_reply_with_all_plugin_help() -> None:
    """The standalone help command documents every user-facing command group."""
    from astrbot.core.star.filter.command import CommandFilter
    from astrbot.core.star.star_handler import star_handlers_registry

    handler = star_handlers_registry.get_handler_by_full_name(
        "crimson_cosmos_plugin_helpav"
    )
    assert handler is not None
    command = next(
        item for item in handler.event_filters if isinstance(item, CommandFilter)
    )
    assert command.command_name == "helpav"

    plugin, _session = make_plugin({}, None)

    async def run() -> list[tuple[str, str]]:
        return [result async for result in plugin.helpav(FakeEvent("/helpav"))]

    results = asyncio.run(run())
    assert len(results) == 1
    assert results[0][0] == "text"
    assert "/helpav" in results[0][1]
    assert "/av 热门 本月 1-10" in results[0][1]
    assert "/av 搜索 SSIS-001 1" in results[0][1]
    assert "/av 磁力 SSIS-001" in results[0][1]
    assert "近期最佳、最近更新、最多观看、最高收藏" in results[0][1]
    assert "/jm 搜索 全彩 1" in results[0][1]
    assert "/jm 下载 123456" in results[0][1]
    assert "色图" in results[0][1]


def test_should_register_jm_commands_and_reuse_session_access_rules() -> None:
    """JM commands respond only in sessions already allowed by the plugin."""
    plugin, _session = make_plugin(
        {
            "enable_group": True,
            "allowed_group_ids": ["10001"],
            "block_other_handlers": True,
        },
        None,
    )
    plugin._execute_jm_action = lambda action, *args: {
        "text": f"{action}:{','.join(map(str, args))}"
    }

    async def run(event: FakeEvent) -> list[object]:
        return [result async for result in plugin.jm_search(event, "猫", 2)]

    allowed = FakeEvent("/jm 搜索 猫 2")
    denied = FakeEvent("/jm 搜索 猫 2", group_id="99999")

    assert asyncio.run(run(allowed)) == [("text", "search:猫,2")]
    assert allowed.stopped is True
    assert asyncio.run(run(denied)) == []

    async def run_invalid_info() -> list[object]:
        return [result async for result in plugin.jm_info(denied, "bad")]

    async def run_denied_download() -> list[object]:
        return [result async for result in plugin.jm_download(denied, "123")]

    assert asyncio.run(run_invalid_info()) == []
    assert asyncio.run(run_denied_download()) == []
    assert hasattr(CrimsonCosmosPlugin, "jm_info")
    assert hasattr(CrimsonCosmosPlugin, "jm_hot")
    assert hasattr(CrimsonCosmosPlugin, "jm_download")


def test_should_send_configurable_fetching_message_before_every_jm_action() -> None:
    """Every valid JM command announces the request before returning its result."""
    plugin, _session = make_plugin(
        {
            "enable_group": True,
            "allowed_group_ids": ["10001"],
            "fetching_message": "正在获取喵~",
            "block_other_handlers": False,
        },
        None,
    )
    plugin._execute_jm_action = lambda action, *_args: {"text": action}
    event = FakeEvent("")

    async def collect(generator: object) -> list[object]:
        return [result async for result in generator]

    commands = [
        plugin.jm_search(event, "猫", 1),
        plugin.jm_info(event, "123"),
        plugin.jm_hot(event, "周", 1),
        plugin.jm_download(event, "123"),
    ]

    for command, action in zip(
        commands, ("search", "info", "hot", "download"), strict=True
    ):
        assert asyncio.run(collect(command)) == [
            ("text", "正在获取喵~"),
            ("text", action),
        ]


def test_should_apply_a_separate_cooldown_to_jm_commands() -> None:
    """JM commands share a per-group cooldown independent from image requests."""
    plugin, _session = make_plugin(
        {
            "enable_group": True,
            "allowed_group_ids": ["10001"],
            "jm_cooldown_seconds": 60,
            "cooldown_message": "JM 冷却中呢喵~",
            "fetching_message": "",
            "block_other_handlers": True,
        },
        None,
    )
    calls = []
    plugin._execute_jm_action = lambda action, *_args: (
        calls.append(action) or {"text": action}
    )
    first_event = FakeEvent("/jm 搜索 猫", sender_id="20001")
    second_event = FakeEvent("/jm 详情 123", sender_id="20002")

    async def run_first() -> list[object]:
        return [result async for result in plugin.jm_search(first_event, "猫", 1)]

    async def run_second() -> list[object]:
        return [result async for result in plugin.jm_info(second_event, "123")]

    assert asyncio.run(run_first()) == [("text", "search")]
    assert asyncio.run(run_second()) == [("text", "JM 冷却中呢喵~")]
    assert calls == ["search"]
    assert second_event.stopped is True


def test_should_allow_configured_administrator_during_jm_cooldown() -> None:
    """Configured administrators bypass the JM command cooldown."""
    plugin, _session = make_plugin(
        {
            "enable_group": True,
            "allowed_group_ids": ["10001"],
            "admin_user_ids": ["20001"],
            "jm_cooldown_seconds": 60,
            "fetching_message": "",
        },
        None,
    )
    plugin._execute_jm_action = lambda action, *_args: {"text": action}
    event = FakeEvent("/jm 搜索 猫", sender_id="20001")

    async def run() -> list[object]:
        return [result async for result in plugin.jm_search(event, "猫", 1)]

    assert asyncio.run(run()) == [("text", "search")]
    assert asyncio.run(run()) == [("text", "search")]


def test_should_reject_invalid_jm_pages_before_fetching() -> None:
    """Invalid one-based pages return usage without invoking jmcomic."""
    plugin, _session = make_plugin(
        {"enable_group": True, "allowed_group_ids": ["10001"]}, None
    )
    plugin._execute_jm_action = lambda *_args: (_ for _ in ()).throw(
        AssertionError("JM backend should not be called")
    )

    async def run() -> list[object]:
        return [result async for result in plugin.jm_search(FakeEvent(""), "猫", 0)]

    assert asyncio.run(run()) == [("text", "用法：/jm 搜索 <关键词> [页码]")]


def test_should_stop_other_handlers_when_jm_dependency_is_missing() -> None:
    """A matched JM command remains exclusive when its dependency is unavailable."""
    plugin, _session = make_plugin(
        {
            "enable_group": True,
            "allowed_group_ids": ["10001"],
            "block_other_handlers": True,
        },
        None,
    )
    plugin._execute_jm_action = lambda *_args: (_ for _ in ()).throw(
        RuntimeError("JM 功能不可用，请安装 jmcomic>=2.7.0。")
    )
    event = FakeEvent("/jm 搜索 猫")

    async def run() -> list[object]:
        return [result async for result in plugin.jm_search(event, "猫", 1)]

    assert asyncio.run(run()) == [("text", "JM 功能不可用，请安装 jmcomic>=2.7.0。")]
    assert event.stopped is True


def test_should_search_and_rank_jm_albums_through_the_configured_client() -> None:
    """Search and ranking use the jmcomic client and return bounded text lists."""

    class Page:
        def iter_id_title_tag(self):
            return iter([("101", "猫本", ["全彩", "短篇"])])

        def iter_id_title(self):
            return iter([("202", "周榜本")])

    class Client:
        def search_site(self, keyword: str, page: int):
            assert (keyword, page) == ("猫", 2)
            return Page()

        def week_ranking(self, page: int, category: str):
            assert (page, category) == (3, "0")
            return Page()

    plugin, _session = make_plugin({"jm_search_page_size": 5}, None)
    plugin._build_jm_option = lambda: SimpleNamespace(new_jm_client=lambda: Client())

    search = plugin._execute_jm_action("search", "猫", 2)
    hot = plugin._execute_jm_action("hot", "week", 3)

    assert search["text"] == "JM 搜索：猫（第 2 页）\n101｜猫本\n#全彩 #短篇"
    assert hot["text"] == "JM 周榜（第 3 页）\n202｜周榜本"


def test_should_return_jm_details_with_a_cached_cover(tmp_path: Path) -> None:
    """Detail lookup returns metadata and downloads the cover once."""

    class Album:
        id = "123"
        title = "测试本"
        author = "作者"
        tags = ["全彩"]

        def __len__(self) -> int:
            return 4

    class Client:
        def get_album_detail(self, album_id: str) -> Album:
            assert album_id == "123"
            return Album()

        def download_album_cover(self, album_id: str, path: str) -> None:
            assert album_id == "123"
            Path(path).write_bytes(b"cover")

    plugin, _session = make_plugin({}, None)
    plugin._jm_data_dir = tmp_path
    plugin._build_jm_option = lambda: SimpleNamespace(new_jm_client=lambda: Client())
    plugin._parse_jm_id = lambda value: value

    result = plugin._execute_jm_action("info", "123")

    assert result["text"] == (
        "JM 详情\nID：123\n标题：测试本\n作者：作者\n章节：4\n#全彩"
    )
    assert Path(result["image"]).read_bytes() == b"cover"


def test_should_strip_leading_zeroes_from_jm_album_id(tmp_path: Path) -> None:
    """Numeric JM IDs are normalized before requesting album details."""

    class Album:
        id = "50328"
        title = "测试本"
        author = "作者"
        tags = []

        def __len__(self) -> int:
            return 1

    class Client:
        def get_album_detail(self, album_id: str) -> Album:
            assert album_id == "50328"
            return Album()

        def download_album_cover(self, album_id: str, path: str) -> None:
            assert album_id == "50328"
            Path(path).write_bytes(b"cover")

    plugin, _session = make_plugin({}, None)
    plugin._jm_data_dir = tmp_path
    plugin._build_jm_option = lambda: SimpleNamespace(new_jm_client=lambda: Client())
    plugin._parse_jm_id = lambda value: value

    result = plugin._execute_jm_action("info", "050328")

    assert result["text"].startswith("JM 详情\nID：50328")


def test_should_send_jm_failure_message_when_detail_cover_delivery_fails(
    tmp_path: Path,
) -> None:
    """A failed JM cover delivery falls back to a plain failure message."""

    class Bot:
        def __init__(self) -> None:
            self.actions: list[tuple[str, dict[str, object]]] = []

        async def call_action(self, action: str, **kwargs: object) -> dict[str, int]:
            self.actions.append((action, kwargs))
            if kwargs["message"][0]["type"] == "image":
                raise RuntimeError("image delivery failed")
            return {"message_id": 123}

    class Event(FakeEvent):
        def __init__(self) -> None:
            super().__init__("/jm 详情 123")
            self.bot = Bot()

    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"cover")
    plugin, _session = make_plugin(
        {
            "enable_group": True,
            "allowed_group_ids": ["10001"],
            "fetching_message": "",
        },
        None,
    )
    plugin._execute_jm_action = lambda *_args: {
        "image": cover,
        "text": "JM 详情",
    }
    event = Event()

    async def run() -> list[object]:
        return [result async for result in plugin.jm_info(event, "123")]

    assert asyncio.run(run()) == [
        (
            "chain",
            [
                {"type": "Image", "file": cover.resolve().as_uri()},
                {"type": "Plain", "text": "JM 详情"},
            ],
        )
    ]
    assert event.bot.actions[0][0] == "send_group_msg"
    assert event.bot.actions[0][1]["message"] == [
        {"type": "image", "data": {"file": "base64://Y292ZXI="}},
        {"type": "text", "data": {"text": "JM 详情"}},
    ]
    assert event.bot.actions[2] == (
        "send_group_msg",
        {
            "group_id": 10001,
            "message": [
                {"type": "text", "data": {"text": "JM 获取失败，请稍后重试。"}}
            ],
        },
    )
    assert event.stopped is True


def test_should_not_schedule_jm_recall_when_auto_recall_is_disabled(
    tmp_path: Path,
) -> None:
    """JM direct delivery does not create a recall task when recall is disabled."""
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"cover")
    plugin, _session = make_plugin(
        {
            "enable_group": True,
            "allowed_group_ids": ["10001"],
            "fetching_message": "",
            "auto_recall": False,
        },
        None,
    )
    plugin._execute_jm_action = lambda *_args: {
        "image": cover,
        "text": "JM 详情",
    }

    def fail_schedule(*_args: object) -> None:
        raise AssertionError("auto recall should not be scheduled")

    plugin._schedule_recall = fail_schedule
    event = FakeRecallEvent("/jm 详情 123")

    async def run() -> list[object]:
        return [result async for result in plugin.jm_info(event, "123")]

    assert asyncio.run(run()) == []
    assert event.bot.actions == [
        (
            "send_group_msg",
            {
                "group_id": 10001,
                "message": [
                    {"type": "image", "data": {"file": "base64://Y292ZXI="}},
                    {"type": "text", "data": {"text": "JM 详情"}},
                ],
            },
        )
    ]
    assert event.stopped is True


def test_should_download_a_jm_album_as_a_plain_zip(tmp_path: Path) -> None:
    """Album download creates one unencrypted ZIP containing downloaded images."""

    class Album:
        id = "456"
        title = "下载本"

    album_dir = tmp_path / "downloads" / "456"

    class Downloader:
        def __init__(self, option: object) -> None:
            del option

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def download_album(self, album_id: str) -> Album:
            assert album_id == "456"
            (album_dir / "1").mkdir(parents=True)
            (album_dir / "1" / "001.jpg").write_bytes(b"image")
            return Album()

    option = SimpleNamespace(
        dir_rule=SimpleNamespace(decide_album_root_dir=lambda _album: album_dir)
    )
    plugin, _session = make_plugin({}, None)
    plugin._jm_data_dir = tmp_path
    plugin._build_jm_option = lambda: option
    plugin._parse_jm_id = lambda value: value
    plugin._import_jmcomic = lambda: SimpleNamespace(JmDownloader=Downloader)

    result = plugin._execute_jm_action("download", "456")

    archive = Path(result["file"])
    assert archive.name == "JM456.zip"
    with zipfile.ZipFile(archive) as zipped:
        assert zipped.namelist() == ["1/001.jpg"]
        assert zipped.read("1/001.jpg") == b"image"


def test_should_expose_minimal_jm_settings_and_dependency() -> None:
    """The plugin declares only settings needed by the minimal JM feature."""
    schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    requirements = (PLUGIN_ROOT / "requirements.txt").read_text(encoding="utf-8")

    items = schema["jm_settings"]["items"]
    assert items["jm_client_type"]["options"] == ["api", "html"]
    assert items["jm_cooldown_seconds"]["type"] == "int"
    assert items["jm_cooldown_seconds"]["description"] == "JM 本子冷却时间（秒）"
    assert (
        items["jm_cooldown_seconds"]["hint"]
        == "群聊内所有用户共享 JM 指令冷却；私聊按用户独立冷却。填写 0 可关闭。"
    )
    assert items["jm_cooldown_seconds"]["default"] == 0
    assert items["jm_cookies"]["default"] == ""
    assert items["jm_cookies"]["obvious_hint"] is True
    assert items["jm_auto_delete_after_send"]["default"] is True
    assert "jmcomic>=2.7.0" in requirements


def test_should_parse_jm_cookies_into_client_metadata(tmp_path: Path) -> None:
    """A browser Cookie header is safely passed to the jmcomic client."""
    captured: dict[str, object] = {}

    class OptionClass:
        @staticmethod
        def construct(option_dict: dict[str, object]) -> object:
            captured.update(option_dict)
            return object()

    module = SimpleNamespace(
        JmModuleConfig=SimpleNamespace(option_class=lambda: OptionClass)
    )
    plugin, _session = make_plugin(
        {"jm_cookies": "AVS=token-value; session=abc=123; invalid;  =empty"},
        None,
    )
    plugin._jm_data_dir = tmp_path
    plugin._import_jmcomic = lambda: module

    plugin._build_jm_option()

    assert captured["client"]["postman"]["meta_data"]["cookies"] == {
        "AVS": "token-value",
        "session": "abc=123",
    }


def test_should_default_bypass_to_quality_preserving_transform() -> None:
    """Bypass ships on with a quality-preserving transform preset."""
    schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    delivery = schema["delivery_settings"]["items"]

    assert delivery["bypass_mode"]["default"] == "transform"
    assert delivery["bypass_mode"]["options"] == [
        "off",
        "transform",
        "file",
        "transform_file",
    ]
    assert delivery["bypass_noise"]["default"] == 8
    assert delivery["bypass_rotate"]["default"] == 1.0
    assert delivery["bypass_resize_ratio"]["default"] == 0.98
    assert delivery["bypass_jpeg_quality"]["default"] == 90

    # A config that omits the key entirely still disables processing.
    plugin, _session = make_plugin({}, None)

    assert plugin._bypass_config() is None


def test_should_perturb_a_base64_image_into_a_fresh_jpeg_file(tmp_path: Path) -> None:
    """File-mode bypass re-encodes the source and returns a local file ref."""
    import base64
    import io as _io

    from PIL import Image

    source = _io.BytesIO()
    Image.new("RGB", (64, 64), (180, 60, 120)).save(source, format="JPEG")
    encoded = "base64://" + base64.b64encode(source.getvalue()).decode("ascii")

    plugin, _session = make_plugin(
        {
            "bypass_mode": "transform_file",
            "bypass_noise": 10,
            "bypass_rotate": 0,
            "bypass_flip": False,
            "bypass_resize_ratio": 1.0,
            "bypass_hue_shift": 0,
            "bypass_brightness": 1.0,
        },
        None,
    )
    original_temp_path = MODULE.get_astrbot_temp_path
    MODULE.get_astrbot_temp_path = lambda: str(tmp_path)
    try:
        kind, ref = asyncio.run(plugin._prepare_image_ref(encoded))
    finally:
        MODULE.get_astrbot_temp_path = original_temp_path

    assert kind == "file"
    assert Path(ref).exists()
    with Image.open(ref) as reopened:
        assert reopened.format == "JPEG"
    assert Path(ref).read_bytes() != source.getvalue()


def test_should_expand_builtin_synonyms_into_or_tag_groups() -> None:
    """Built-in synonyms turn one tag into OR groups without user aliases."""
    plugin, _session = make_plugin({}, None)

    assert plugin._resolve_lolicon_tag_groups(["白丝"]) == [
        ["白丝"],
        ["白タイツ"],
        ["白色连裤袜"],
    ]
    assert plugin._resolve_lolicon_tag_groups(["JK"]) == [
        ["水手服"],
        ["校服"],
    ]


def test_should_override_builtin_synonyms_with_user_aliases() -> None:
    """User aliases override built-ins and unknown tags pass through."""
    plugin, _session = make_plugin(
        {"lolicon_tag_aliases": "白丝=white_pantyhose\nDeepSeek=deepseek"},
        None,
    )

    assert plugin._resolve_lolicon_tag_groups(["白丝", "DeepSeek", "任意标签"]) == [
        ["white_pantyhose", "deepseek", "任意标签"]
    ]


def test_should_fall_back_to_untagged_lolicon_when_tags_have_no_results(
    tmp_path: Path,
) -> None:
    """Tagged requests with zero results retry once without the tag filter."""

    class Session(FakeSession):
        def __init__(self) -> None:
            super().__init__(None)
            self.posts: list[dict[str, object]] = []

        def post(
            self,
            url: str,
            *,
            json: dict[str, object],
            timeout: object = None,
        ) -> FakeResponse:
            del url, timeout
            self.posts.append(json)
            if "tag" in json:
                return FakeResponse({"error": "", "data": []})
            return FakeResponse(
                {
                    "error": "",
                    "data": [
                        {
                            "pid": 1,
                            "urls": {"small": "https://images.example/one.jpg"},
                        }
                    ],
                }
            )

    plugin, _session = make_plugin({}, None)
    plugin._session = Session()

    with stub_temp_dir(tmp_path):
        paths, pids = asyncio.run(plugin._fetch_lolicon_images(1, ["不存在的标签"]))

    assert pids == ["1"]
    assert len(paths) == 1
    assert Path(paths[0]).read_bytes() == b"image-bytes"
    assert len(plugin._session.posts) == 2
    assert plugin._session.posts[0]["tag"] == [["不存在的标签"]]
    assert "tag" not in plugin._session.posts[1]


def test_should_forward_help_text_when_global_forward_is_enabled() -> None:
    """全局聊天记录开关应覆盖纯文本帮助消息。"""
    plugin, _ = make_plugin({"use_forward": True, "auto_recall": False}, None)
    event = FakeRecallEvent("/helpav")

    async def run() -> list[object]:
        return [result async for result in plugin.helpav(event)]

    results = asyncio.run(run())

    assert results == []
    action, payload = event.bot.actions[0]
    assert action == "send_group_forward_msg"
    content = payload["messages"][0]["data"]["content"]
    assert content[0]["type"] == "text"
    assert "R18 图片与本子查询总帮助" in content[0]["data"]["text"]


def test_should_auto_recall_plain_text_sent_by_the_plugin(tmp_path: Path) -> None:
    """自动撤回应覆盖插件主动发送的纯文本。"""
    plugin, _ = make_plugin(
        {"use_forward": False, "auto_recall": True, "recall_delay_seconds": 0},
        None,
    )
    plugin._recall_tasks_path = tmp_path / "recall_tasks.json"
    plugin._recall_tasks = set()
    plugin._active_recall_ids = set()
    event = FakeRecallEvent("/helpav")

    async def run() -> list[object]:
        results = [result async for result in plugin.helpav(event)]
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return results

    results = asyncio.run(run())

    assert results == []
    assert event.bot.actions[0][0] == "send_group_msg"
    assert event.bot.actions[-1] == ("delete_msg", {"message_id": 123})


def test_should_forward_jm_zip_when_global_forward_is_enabled(tmp_path: Path) -> None:
    """全局聊天记录开关应覆盖 JM 下载文件。"""
    archive = tmp_path / "album.zip"
    archive.write_bytes(b"zip")
    plugin, _ = make_plugin({"use_forward": True, "auto_recall": False}, None)
    event = FakeRecallEvent("/jm 下载 1")

    sent = asyncio.run(plugin._send_file_with_auto_recall(event, archive, "完成"))

    assert sent is True
    action, payload = event.bot.actions[0]
    assert action == "send_group_forward_msg"
    content = payload["messages"][0]["data"]["content"]
    assert [segment["type"] for segment in content] == ["text", "file"]


def test_should_fall_back_to_plain_text_when_forward_delivery_fails() -> None:
    """聊天记录接口失败时应回退普通 AstrBot 文本结果。"""

    class FailingForwardBot(FakeRecallBot):
        async def call_action(
            self, action: str, **kwargs: object
        ) -> dict[str, int] | None:
            self.actions.append((action, kwargs))
            if action.endswith("forward_msg"):
                raise RuntimeError("forward unavailable")
            return {"message_id": 123} if action.startswith("send_") else None

    plugin, _ = make_plugin({"use_forward": True, "auto_recall": False}, None)
    event = FakeRecallEvent("/helpav")
    event.bot = FailingForwardBot()

    async def run() -> list[object]:
        return [result async for result in plugin.helpav(event)]

    results = asyncio.run(run())

    assert len(results) == 1
    assert results[0][0] == "text"
    assert "R18 图片与本子查询总帮助" in results[0][1]
    assert event.bot.actions[0][0] == "send_group_forward_msg"


def test_should_disable_each_optional_content_feature() -> None:
    """Lolicon、Jable、JM 三个开关应阻止对应网络功能。"""
    image_plugin, image_session = make_plugin(
        {
            "enable_group": True,
            "allowed_group_ids": ["10001"],
            "keywords": ["色图"],
            "image_source": "lolicon",
            "enable_lolicon": False,
            "fetching_message": "",
        },
        None,
    )
    jable_plugin, _ = make_plugin({"enable_group": True, "enable_jable": False}, None)
    jm_plugin, _ = make_plugin({"enable_group": True, "enable_jm": False}, None)

    image_results = asyncio.run(collect_results(image_plugin, FakeEvent("色图")))

    async def collect_jable() -> list[object]:
        return [
            result
            async for result in jable_plugin._handle_jable_command(
                FakeEvent("/av 热门 今日 1"), "/av 热门 今日 1"
            )
        ]

    async def collect_jm() -> list[object]:
        return [
            result
            async for result in jm_plugin._handle_jm_action(
                FakeEvent("/jm 详情 1"), "info", "1"
            )
        ]

    assert image_results == [
        ("text", "所有图片来源均已关闭，请至少启用 Lolicon 或配置 Wallhaven。")
    ]
    assert image_session.calls == []
    assert asyncio.run(collect_jable()) == [("text", "Jable 影片查询已关闭。")]
    assert asyncio.run(collect_jm()) == [("text", "JM 本子功能已关闭。")]


def test_should_clamp_recall_delay_to_two_minutes() -> None:
    """撤回延迟必须始终处于两分钟以内。"""
    plugin, _ = make_plugin({"recall_delay_seconds": 999}, None)
    assert plugin._recall_delay_seconds() == 120.0
    plugin._config["recall_delay_seconds"] = -1
    assert plugin._recall_delay_seconds() == 0.0


def test_should_recall_delivery_failure_notification(tmp_path: Path) -> None:
    """图片发送失败后的插件提示也必须进入自动撤回链路。"""

    class ImageFailingBot(FakeRecallBot):
        async def call_action(
            self, action: str, **kwargs: object
        ) -> dict[str, int] | None:
            self.actions.append((action, kwargs))
            if len(self.actions) == 1:
                raise RuntimeError("image failed")
            if action.startswith("send_"):
                return {"message_id": 456}
            return None

    plugin, _ = make_plugin(
        {"auto_recall": True, "recall_delay_seconds": 0, "use_forward": False},
        None,
    )
    plugin._recall_tasks_path = tmp_path / "recall_tasks.json"
    plugin._recall_tasks = set()
    plugin._active_recall_ids = set()
    event = FakeRecallEvent("色图")
    event.bot = ImageFailingBot()

    async def run() -> bool | None:
        status = await plugin._send_image_with_auto_recall(
            event,
            "https://images.example/fail.jpg",
            failure_message="图片发送失败。",
            prepared=("image", "https://images.example/fail.jpg"),
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return status

    status = asyncio.run(run())

    assert status is False
    assert event.bot.actions[1][1]["message"][0]["data"]["text"] == "图片发送失败。"
    assert event.bot.actions[-1] == ("delete_msg", {"message_id": 456})


def make_wallhaven_plugin(
    overrides: dict[str, object] | None = None,
    urls: list[str] | None = None,
) -> tuple[object, FakeSession]:
    """创建只使用当前 Wallhaven 图片源的测试插件。"""
    config: dict[str, object] = {
        "enable_group": True,
        "allowed_group_ids": ["10001"],
        "keywords": ["色图"],
        "keyword_match_mode": "exact",
        "image_source": "wallhaven",
        "wallhaven_api_key": "test-key",
        "wallhaven_categories": ["动漫"],
        "wallhaven_purity": ["全年龄"],
        "fetching_message": "",
    }
    config.update(overrides or {})
    image_urls = urls or ["https://images.example/current.jpg"]
    return make_plugin(config, {"data": [{"path": url} for url in image_urls]})


def test_should_enforce_quantity_access_and_keyword_rules() -> None:
    """当前图片源仍应复用数量、会话白名单和关键词规则。"""
    plugin, session = make_wallhaven_plugin()
    assert asyncio.run(collect_results(plugin, FakeEvent("来6张色图"))) == [
        ("text", "单次最多获取 5 张图片。")
    ]
    assert session.calls == []

    cases = [
        ("exact", "色图", "10001", False, True),
        ("exact", "来张色图", "10001", False, False),
        ("prefix", "色图 来一张", "10001", False, True),
        ("contains", "请来张色图", "10001", False, True),
        ("exact", "色图", "99999", False, False),
    ]
    for mode, message, group_id, private, should_reply in cases:
        candidate, candidate_session = make_wallhaven_plugin(
            {"keyword_match_mode": mode}
        )
        results = asyncio.run(
            collect_results(candidate, FakeEvent(message, group_id, private))
        )
        assert bool(results) is should_reply
        assert bool(candidate_session.calls) is should_reply


def test_should_enforce_private_allowlist_with_current_image_source() -> None:
    """私聊总开关和用户白名单不依赖已删除的图片源。"""
    cases = [
        ([], "20001", True),
        (["20001", 20002], "20002", True),
        (["20001"], "29999", False),
        ("20001", "20001", False),
    ]
    for allowed_users, sender_id, should_reply in cases:
        plugin, session = make_wallhaven_plugin(
            {
                "enable_group": False,
                "enable_private": True,
                "allowed_private_user_ids": allowed_users,
            }
        )
        results = asyncio.run(
            collect_results(
                plugin,
                FakeEvent("色图", group_id="", private=True, sender_id=sender_id),
            )
        )
        assert bool(results) is should_reply
        assert bool(session.calls) is should_reply


def test_should_pass_parsed_tags_to_wallhaven() -> None:
    """请求语法应被消费，剩余内容作为当前图片源标签。"""
    cases = [
        ("来个色图", None),
        ("请给我来一张色图", None),
        ("发我一张色图", None),
        ("来个白丝色图", "白丝"),
        ("给我发一张白丝、猫耳色图", "白丝 猫耳"),
        ("色图 白丝 猫耳", "白丝 猫耳"),
    ]
    for message, expected_query in cases:
        plugin, session = make_wallhaven_plugin({"keyword_match_mode": "contains"})
        results = asyncio.run(collect_results(plugin, FakeEvent(message)))
        assert results == [("image", "https://images.example/current.jpg")]
        params = session.calls[0][1]
        assert params is not None
        assert params.get("q") == expected_query


def test_should_merge_nested_configuration_cards() -> None:
    """WebUI 分组配置应继续合并为运行时扁平配置。"""
    schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    expected_groups = {
        "access_settings",
        "trigger_settings",
        "delivery_settings",
        "message_settings",
        "source_settings",
        "lolicon_settings",
        "jable_settings",
        "jm_settings",
        "wallhaven_settings",
    }
    assert set(schema) == expected_groups
    assert all(section["type"] == "object" for section in schema.values())

    plugin = CrimsonCosmosPlugin(
        None,
        {
            "access_settings": {"enable_group": True},
            "delivery_settings": {"use_forward": True},
            "lolicon_settings": {"enable_lolicon": False},
            "jable_settings": {"enable_jable": False},
            "jm_settings": {"enable_jm": False},
        },
    )
    assert plugin._config["enable_group"] is True
    assert plugin._config["use_forward"] is True
    assert plugin._config["enable_lolicon"] is False
    assert plugin._config["enable_jable"] is False
    assert plugin._config["enable_jm"] is False


def test_should_send_prompts_and_apply_shared_cooldown() -> None:
    """获取提示、失败提示、群共享冷却与管理员绕过应保持有效。"""
    plugin, session = make_wallhaven_plugin(
        {
            "fetching_message": "正在获取喵~",
            "cooldown_seconds": 60,
            "cooldown_message": "冷却中呢喵~",
        }
    )
    first = asyncio.run(collect_results(plugin, FakeEvent("色图", sender_id="20001")))
    second = asyncio.run(collect_results(plugin, FakeEvent("色图", sender_id="20002")))
    assert first == [
        ("text", "正在获取喵~"),
        ("image", "https://images.example/current.jpg"),
    ]
    assert second == [("text", "冷却中呢喵~")]
    assert len(session.calls) == 1

    administrator, administrator_session = make_wallhaven_plugin(
        {"admin_user_ids": ["20001"], "cooldown_seconds": 60}
    )
    event = FakeEvent("色图", sender_id="20001")
    asyncio.run(collect_results(administrator, event))
    asyncio.run(collect_results(administrator, event))
    assert len(administrator_session.calls) == 2


def test_should_report_exhausted_current_source_failure() -> None:
    """当前图片源失败时应发送可配置失败提示。"""
    plugin, _ = make_wallhaven_plugin(
        {"fetching_message": "正在获取喵~", "failure_message": "获取失败。"}
    )
    plugin._session.payload = {"unexpected": True}

    results = asyncio.run(collect_results(plugin, FakeEvent("色图")))

    assert results == [("text", "正在获取喵~"), ("text", "获取失败。")]


def test_should_forward_and_recall_multiple_current_source_images(
    tmp_path: Path,
) -> None:
    """全局开关应合并转发并撤回多张当前来源图片。"""
    plugin, _ = make_wallhaven_plugin(
        {
            "use_forward": True,
            "auto_recall": True,
            "recall_delay_seconds": 0,
        },
        ["https://images.example/one.jpg", "https://images.example/two.jpg"],
    )
    plugin._recall_tasks_path = tmp_path / "recall_tasks.json"
    plugin._recall_tasks = set()
    plugin._active_recall_ids = set()

    async def run() -> tuple[list[object], FakeRecallBot]:
        event = FakeRecallEvent("来两份色图")
        results = await collect_results(plugin, event)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return results, event.bot

    results, bot = asyncio.run(run())
    assert results == []
    assert bot.actions[0][0] == "send_group_forward_msg"
    assert len(bot.actions[0][1]["messages"]) == 2
    assert bot.actions[-1] == ("delete_msg", {"message_id": 123})


def test_should_restore_persisted_recall_after_reload(tmp_path: Path) -> None:
    """待撤回消息应在插件重新加载后继续执行。"""
    task_file = tmp_path / "recall_tasks.json"
    plugin, _ = make_wallhaven_plugin({"auto_recall": True, "recall_delay_seconds": 60})
    plugin._recall_tasks_path = task_file
    plugin._recall_tasks = set()
    plugin._active_recall_ids = set()

    async def schedule_and_stop() -> None:
        await collect_results(plugin, FakeRecallEvent("色图"))
        await plugin.terminate()

    asyncio.run(schedule_and_stop())
    records = json.loads(task_file.read_text(encoding="utf-8"))
    records[0]["due_at"] = 0
    task_file.write_text(json.dumps(records), encoding="utf-8")

    restored, _ = make_wallhaven_plugin()
    restored._recall_tasks_path = task_file
    restored._recall_tasks = set()
    restored._active_recall_ids = set()

    async def restore() -> FakeRecallBot:
        event = FakeRecallEvent("普通聊天")
        await collect_results(restored, event)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return event.bot

    bot = asyncio.run(restore())
    assert bot.actions == [("delete_msg", {"message_id": 123})]
    assert json.loads(task_file.read_text(encoding="utf-8")) == []


def test_should_only_stop_other_handlers_for_matched_requests() -> None:
    """仅匹配本插件请求时停止后续处理器。"""
    plugin, _ = make_wallhaven_plugin({"block_other_handlers": True})
    matched = FakeEvent("色图")
    unmatched = FakeEvent("普通聊天")

    matched_results = asyncio.run(collect_results(plugin, matched))
    unmatched_results = asyncio.run(collect_results(plugin, unmatched))

    assert matched.stopped is True
    assert matched_results == [("image", "https://images.example/current.jpg")]
    assert unmatched.stopped is False
    assert unmatched_results == []


def test_should_validate_and_dispatch_all_jm_command_wrappers() -> None:
    """JM 指令包装层应校验参数并把合法请求交给统一处理器。"""
    plugin, _ = make_plugin({"enable_group": True}, None)
    event = FakeEvent("/jm")

    async def collect(generator: object) -> list[object]:
        return [result async for result in generator]

    assert asyncio.run(collect(plugin.jm_search(event, "", 1))) == [
        ("text", "用法：/jm 搜索 <关键词> [页码]")
    ]
    assert asyncio.run(collect(plugin.jm_search(event, "测试", "bad"))) == [
        ("text", "用法：/jm 搜索 <关键词> [页码]")
    ]
    assert asyncio.run(collect(plugin.jm_info(event, "abc"))) == [
        ("text", "用法：/jm 详情 <数字ID>")
    ]
    assert asyncio.run(collect(plugin.jm_hot(event, "未知", 1))) == [
        ("text", "用法：/jm 热门 [日|周|月] [页码]")
    ]
    assert asyncio.run(collect(plugin.jm_hot(event, "周", "bad"))) == [
        ("text", "用法：/jm 热门 [日|周|月] [页码]")
    ]
    assert asyncio.run(collect(plugin.jm_download(event, "abc"))) == [
        ("text", "用法：/jm 下载 <数字ID>")
    ]

    calls: list[tuple[str, tuple[object, ...]]] = []

    async def handle(
        _event: FakeEvent, action: str, *args: object
    ) -> AsyncGenerator[object, None]:
        calls.append((action, args))
        yield ("handled", action)

    plugin._handle_jm_action = handle
    assert asyncio.run(collect(plugin.jm_search(event, "测试", 2))) == [
        ("handled", "search")
    ]
    assert asyncio.run(collect(plugin.jm_info(event, "00123"))) == [("handled", "info")]
    assert asyncio.run(collect(plugin.jm_hot(event, "今日", 2))) == [("handled", "hot")]
    assert asyncio.run(collect(plugin.jm_download(event, "00123"))) == [
        ("handled", "download")
    ]
    assert calls == [
        ("search", ("测试", 2)),
        ("info", ("00123",)),
        ("hot", ("day", 2)),
        ("download", ("00123",)),
    ]


def test_should_dispatch_all_av_command_wrappers() -> None:
    """AV 子命令应构造统一且可解析的内部命令文本。"""
    plugin, _ = make_plugin({}, None)
    event = FakeEvent("/av")
    calls: list[str] = []

    async def handle(_event: FakeEvent, message: str) -> AsyncGenerator[object, None]:
        calls.append(message)
        yield ("handled", message)

    plugin._handle_jable_command = handle

    async def collect(generator: object) -> list[object]:
        return [result async for result in generator]

    asyncio.run(collect(plugin.av_hot(event, "今日", "1")))
    asyncio.run(collect(plugin.av_new(event, "2")))
    asyncio.run(collect(plugin.av_theme(event, "黑丝", "最高收藏", "3")))
    asyncio.run(collect(plugin.av_model(event, "测试女优", "最近更新", "4")))
    assert calls == [
        "/av 热门 今日 1",
        "/av 新片 2",
        "/av 主题 黑丝 最高收藏 3",
        "/av 女优 测试女优 最近更新 4",
    ]


def test_should_use_private_forward_and_fall_back_for_invalid_target() -> None:
    """统一文本发送应支持私聊，并在目标 ID 无效时回退普通结果。"""
    plugin, _ = make_plugin({"use_forward": True, "auto_recall": False}, None)
    private_event = FakeRecallEvent("/helpav")
    private_event.private = True
    private_event.group_id = ""
    private_event.sender_id = "20001"

    async def collect(event: FakeEvent) -> list[object]:
        return [result async for result in plugin.helpav(event)]

    assert asyncio.run(collect(private_event)) == []
    assert private_event.bot.actions[0][0] == "send_private_forward_msg"
    assert private_event.bot.actions[0][1]["user_id"] == 20001

    invalid_event = FakeRecallEvent("/helpav")
    invalid_event.group_id = "invalid"
    fallback = asyncio.run(collect(invalid_event))
    assert len(fallback) == 1
    assert fallback[0][0] == "text"
    assert invalid_event.bot.actions == []


def test_should_initialize_session_and_cover_noop_entry_paths() -> None:
    """会话工厂、指令组入口和空消息路径应可安全执行。"""
    plugin, _ = make_plugin(
        {
            "enable_group": True,
            "allowed_group_ids": [],
            "keywords": ["色图"],
        },
        None,
    )

    async def run() -> list[object]:
        session = plugin._make_session()
        assert session.closed is False
        await session.close()
        return await collect_results(plugin, FakeEvent(""))

    assert asyncio.run(run()) == []
    assert plugin._is_event_allowed(FakeEvent("色图")) is True
    plugin._config["allowed_group_ids"] = "10001"
    assert plugin._is_event_allowed(FakeEvent("色图")) is False
