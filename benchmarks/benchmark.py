"""插件获取速度基准测试（模拟网络延迟，串行 vs 并发对比）。

在缺少 AstrBot 核心依赖（如 sqlmodel）的环境里也能运行：本脚本会先桩掉
``astrbot`` 包，再导入 ``main.py``，用一个按请求注入固定延迟的假会话测量
各路径的真实代码耗时。

用法::

    python benchmarks/benchmark.py
"""

from __future__ import annotations

import asyncio
import io
import sys
import tempfile
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

LATENCY = 0.10  # 每次 HTTP 往返模拟 100ms


# --------------------------------------------------------------------------- #
# 1) 桩掉 astrbot 包，避免导入真实 AstrBot 核心（它依赖 sqlmodel 等）。
# --------------------------------------------------------------------------- #
_LOGGER = types.SimpleNamespace(
    warning=lambda *a, **k: None,
    info=lambda *a, **k: None,
    error=lambda *a, **k: None,
    debug=lambda *a, **k: None,
)


class _Group:
    def __init__(self, name: str) -> None:
        self.name = name

    def command(self, name: str):
        def deco(fn):
            return fn

        return deco


class _Filter:
    class EventMessageType:
        ALL = object()

    @staticmethod
    def command(*args, **kwargs):
        def deco(fn):
            return fn

        return deco

    @staticmethod
    def command_group(name: str):
        def deco(fn):
            return _Group(name)

        return deco

    @staticmethod
    def event_message_type(*args, **kwargs):
        def deco(fn):
            return fn

        return deco

    @staticmethod
    def llm_tool(*args, **kwargs):
        def deco(fn):
            return fn

        return deco


class _Star:
    def __init__(self, context=None) -> None:
        del context


class _Image:
    def __init__(self) -> None:
        self.file = None

    @classmethod
    def fromURL(cls, url: str) -> "_Image":
        image = cls()
        image.file = url
        return image

    @classmethod
    def fromFileSystem(cls, path: str) -> "_Image":
        image = cls()
        image.file = path
        return image


class _File:
    def __init__(self, name: str = "", file: str = "") -> None:
        self.name = name
        self.file = file


class _Plain:
    def __init__(self, text: str = "") -> None:
        self.text = text


def _install_astrbot_stub() -> None:
    api = types.ModuleType("astrbot.api")
    api.logger = _LOGGER

    event = types.ModuleType("astrbot.api.event")
    event.filter = _Filter()
    event.AstrMessageEvent = type("AstrMessageEvent", (), {})

    star = types.ModuleType("astrbot.api.star")
    star.Star = _Star
    star.Context = type("Context", (), {})

    components = types.ModuleType("astrbot.core.message.components")
    components.File = _File
    components.Image = _Image
    components.Plain = _Plain

    astrbot_path = types.ModuleType("astrbot.core.utils.astrbot_path")
    astrbot_path.get_astrbot_plugin_data_path = lambda: str(
        Path(tempfile.gettempdir()) / "astrbot_plugin_data"
    )
    astrbot_path.get_astrbot_temp_path = lambda: tempfile.gettempdir()

    astrbot = types.ModuleType("astrbot")
    astrbot.api = api
    astrbot.core = types.ModuleType("astrbot.core")
    astrbot.core.utils = types.ModuleType("astrbot.core.utils")
    astrbot.core.message = types.ModuleType("astrbot.core.message")
    astrbot.core.message.components = components
    astrbot.core.utils.astrbot_path = astrbot_path

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event
    sys.modules["astrbot.api.star"] = star
    sys.modules["astrbot.core"] = astrbot.core
    sys.modules["astrbot.core.utils"] = astrbot.core.utils
    sys.modules["astrbot.core.message"] = astrbot.core.message
    sys.modules["astrbot.core.message.components"] = components
    sys.modules["astrbot.core.utils.astrbot_path"] = astrbot_path


# --------------------------------------------------------------------------- #
# 2) 模拟延迟的会话与响应。
# --------------------------------------------------------------------------- #
class LatencyResponse:
    def __init__(
        self, payload=None, image_bytes: bytes = b"", latency: float = 0.0
    ) -> None:
        self._payload = payload
        self._image_bytes = image_bytes
        self._latency = latency
        self.content = types.SimpleNamespace(iter_chunked=self._iter_chunks)

    async def _iter_chunks(self, size: int):
        data = self._image_bytes
        for index in range(0, len(data), size):
            yield data[index : index + size]

    async def __aenter__(self) -> "LatencyResponse":
        if self._latency:
            await asyncio.sleep(self._latency)
        return self

    async def __aexit__(self, *args) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    async def json(self, **_kwargs):
        return self._payload


class LatencySession:
    def __init__(
        self,
        latency: float = LATENCY,
        get_payload=None,
        post_payload=None,
        image_bytes: bytes = b"",
    ) -> None:
        self._latency = latency
        self._get_payload = get_payload
        self._post_payload = post_payload
        self._image_bytes = image_bytes
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    def get(self, url, *, params=None, timeout=None, headers=None):
        del url, params, timeout, headers
        return LatencyResponse(self._get_payload, self._image_bytes, self._latency)

    def post(self, url, *, json=None, timeout=None):
        del url, timeout
        payload = self._post_payload
        if callable(payload):
            payload = payload(json)
        return LatencyResponse(payload, b"", self._latency)


def make_sample_jpeg(size=(768, 768)) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 90, 140)).save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# 3) 测量辅助。
# --------------------------------------------------------------------------- #
async def measure(coro_factory, runs: int = 3) -> float:
    await coro_factory()  # 预热
    best = float("inf")
    for _ in range(runs):
        start = time.perf_counter()
        await coro_factory()
        best = min(best, time.perf_counter() - start)
    return best


def build_plugin(plugin_module):
    plugin = plugin_module.CrimsonCosmosPlugin.__new__(plugin_module.CrimsonCosmosPlugin)
    return plugin


async def run_benchmark() -> list[dict[str, object]]:
    import main as plugin_mod

    sample = make_sample_jpeg()
    rows: list[dict[str, object]] = []

    # --- 自定义 API：多图并行拉取 --------------------------------------- #
    plugin = build_plugin(plugin_mod)
    plugin._config = {
        "custom_api_url": "https://api.example/image",
        "custom_api_image_url_path": "url",
    }
    plugin._session = LatencySession(get_payload={"url": "https://images.example/x.jpg"})
    seq = await measure(lambda: _sequential_custom(plugin))
    par = await measure(lambda: _parallel_custom(plugin))
    rows.append(_row("自定义 API · 3 张", seq, par))

    # --- Nekos API：多图并行拉取 ---------------------------------------- #
    plugin = build_plugin(plugin_mod)
    plugin._config = {"nekos_api_rating": "露骨"}
    plugin._session = LatencySession(
        get_payload={"url": "https://cdn.nekosapi.com/x.webp"}
    )
    seq = await measure(lambda: _sequential_nekos(plugin))
    par = await measure(lambda: plugin._fetch_nekos_api_images(3))
    rows.append(_row("Nekos API · 3 张", seq, par))

    # --- Lolicon：代理下载并行化 ---------------------------------------- #
    plugin = build_plugin(plugin_mod)
    plugin._config = {
        "lolicon_image_size": "small",
        "lolicon_proxy_order": [],
    }
    plugin._session = LatencySession(
        post_payload=lambda req: {
            "error": "",
            "data": [
                {
                    "pid": index + 1,
                    "urls": {"small": f"https://images.example/{index + 1}.jpg"},
                }
                for index in range(req["num"])
            ],
        },
        image_bytes=sample,
    )
    seq = await measure(lambda: _sequential_lolicon(plugin))
    par = await measure(lambda: plugin._fetch_lolicon_images(3))
    rows.append(_row("Lolicon · 3 张下载", seq, par))

    # --- 过审处理：下载 + 扰动并行化 ------------------------------------ #
    plugin = build_plugin(plugin_mod)
    plugin._config = {
        "bypass_mode": "transform",
        "bypass_noise": 8,
        "bypass_rotate": 1.0,
        "bypass_flip": True,
        "bypass_resize_ratio": 0.98,
        "bypass_jpeg_quality": 90,
        "bypass_hue_shift": 0,
        "bypass_brightness": 1.0,
    }
    plugin._session = LatencySession(image_bytes=sample)
    tmpdir = tempfile.mkdtemp(prefix="crimson_cosmos_bench_")
    plugin_mod.get_astrbot_temp_path = lambda: tmpdir
    url = "https://images.example/sample.jpg"
    seq = await measure(lambda: _sequential_bypass(plugin, url))
    par = await measure(lambda: asyncio.gather(*(plugin._prepare_image_ref(url) for _ in range(3))))
    rows.append(_row("过审处理 · 3 张", seq, par))

    return rows


async def _sequential_custom(plugin) -> None:
    for _ in range(3):
        await plugin._fetch_custom_image([])


async def _parallel_custom(plugin) -> None:
    await asyncio.gather(*(plugin._fetch_custom_image([]) for _ in range(3)))


async def _sequential_nekos(plugin) -> None:
    for _ in range(3):
        await plugin._fetch_nekos_api_images(1)


async def _sequential_lolicon(plugin) -> None:
    # 旧逻辑：一次 POST + 3 次串行代理下载。
    payload = {"r18": 1, "num": 3, "excludeAI": True, "size": ["small"]}
    images = await plugin._request_lolicon_data(payload)
    for image in images:
        url = image["urls"]["small"]
        async with plugin._session.get(url) as response:
            async for _chunk in response.content.iter_chunked(64 * 1024):
                pass


async def _sequential_bypass(plugin, url: str) -> None:
    for _ in range(3):
        await plugin._prepare_image_ref(url)


def _row(label: str, seq: float, par: float) -> dict[str, object]:
    speedup = seq / par if par > 0 else float("inf")
    return {
        "label": label,
        "seq_ms": round(seq * 1000, 1),
        "par_ms": round(par * 1000, 1),
        "speedup": round(speedup, 2),
    }


def main() -> None:
    _install_astrbot_stub()
    rows = asyncio.run(run_benchmark())

    print("获取速度基准（模拟单次 HTTP 往返 = 100ms）\n")
    print(f"{'场景':<18} {'串行(旧)':>10} {'并发(新)':>10} {'加速比':>8}")
    print("-" * 50)
    for row in rows:
        print(
            f"{row['label']:<18} {row['seq_ms']:>7}ms {row['par_ms']:>7}ms "
            f"{row['speedup']:>7}x"
        )


if __name__ == "__main__":
    main()
