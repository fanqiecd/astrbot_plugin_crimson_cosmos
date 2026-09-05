"""Serve configured adult images, Jable reports, and JM albums."""

from __future__ import annotations

import asyncio
import base64
import importlib
import io
import json
import os
import random
import re
import shutil
import socket
import time
import zipfile
from collections.abc import AsyncGenerator
from html import unescape
from itertools import product
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.message.components import File, Image, Plain
from astrbot.core.utils.astrbot_path import (
    get_astrbot_plugin_data_path,
    get_astrbot_temp_path,
)

DEFAULT_CONFIG = {
    "enable_group": False,
    "allowed_group_ids": [],
    "enable_private": False,
    "allowed_private_user_ids": [],
    "admin_user_ids": [],
    "keywords": ["色图"],
    "keyword_match_mode": "exact",
    "block_other_handlers": True,
    "cooldown_seconds": 0,
    "image_source": "lolicon",
    "image_source_order": [],
    "request_retry_count": 3,
    "enable_lolicon": True,
    "lolicon_r18_mode": "r18",
    "lolicon_exclude_ai": True,
    "lolicon_aspect_ratio": "",
    "lolicon_image_size": "small",
    "lolicon_proxy": "",
    "lolicon_proxy_order": [
        "https://i.pixiv.re",
        "https://i.pixiv.nl",
        "https://i.loli.best",
    ],
    "lolicon_proxy_timeout_seconds": 30,
    "lolicon_tag_aliases": "",
    "show_pixiv_pid": False,
    "enable_jable": True,
    "jable_show_cover": True,
    "jable_show_code": True,
    "jable_show_title": True,
    "jable_show_stars": True,
    "jable_show_themes": True,
    "jable_show_detail_link": True,
    "jina_api_key": "",
    "enable_jm": True,
    "jm_client_type": "api",
    "jm_cookies": "",
    "jm_cooldown_seconds": 0,
    "jm_client_domain": "",
    "jm_retry_times": 0,
    "jm_use_proxy": False,
    "jm_proxy_url": "",
    "jm_max_concurrent_photos": 3,
    "jm_max_concurrent_images": 5,
    "jm_search_page_size": 5,
    "jm_auto_delete_after_send": True,
    "wallhaven_api_key": "",
    "wallhaven_categories": ["动漫"],
    "wallhaven_purity": ["成人"],
    "wallhaven_sorting": "最新",
    "wallhaven_tags": [],
    "use_forward": False,
    "auto_recall": False,
    "recall_delay_seconds": 60,
    "bypass_mode": "transform",
    "bypass_noise": 8,
    "bypass_rotate": 1.0,
    "bypass_flip": True,
    "bypass_resize_ratio": 0.98,
    "bypass_jpeg_quality": 90,
    "bypass_hue_shift": 0,
    "bypass_brightness": 1.0,
    "fetching_message": "正在获取喵~",
    "cooldown_message": "冷却中呢喵~",
    "group_disabled_message": "本喵暂时不提供此服务喵~",
    "failure_message": "涩图获取失败了喵，请稍后再试~",
}
CONFIG_GROUPS = (
    "access_settings",
    "trigger_settings",
    "delivery_settings",
    "message_settings",
    "source_settings",
    "lolicon_settings",
    "jable_settings",
    "jm_settings",
    "wallhaven_settings",
)
CHINESE_IMAGE_COUNTS = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}
MAX_IMAGES_PER_REQUEST = 5  # ponytail: fixed cap; make configurable if needed.
MAX_MISSAV_MAGNETS = 5  # ponytail: fixed chat-safe cap; add ranges if requested.
MAX_LOLICON_TAG_GROUPS = 8  # 标签组合上限，避免同义展开后请求体过大。

# 这些域名直连（走代理会触发 TLS 握手异常，或国内可直连无需代理）；
# 其余域名（如 r.jina.ai、missav.ws）通过 trust_env 读取 HTTP(S)_PROXY 走代理，
# 用于访问被墙资源。带前导点表示同时匹配该域及其子域。
_DIRECT_PROXY_DOMAINS = (
    ".pixiv.net",
    ".pximg.net",
    ".loli.best",
    ".pixiv.nl",
    ".pixiv.re",
    ".lolicon.app",
    ".wallhaven.cc",
    ".microlink.io",
)

# 常见中文标签 → Lolicon 候选标签（同义候选为 OR 关系，命中任意一个即可）。
# 错误的候选不会拖累结果：Lolicon 的多组标签按 OR 匹配。
LOLICON_TAG_ALIASES: dict[str, tuple[str, ...]] = {
    # 袜类 / 绝对领域
    "白丝": ("白丝", "白タイツ", "白色连裤袜"),
    "白色丝袜": ("白丝", "白タイツ"),
    "白丝袜": ("白丝", "白タイツ"),
    "黑丝": ("黑丝", "黑タイツ", "黑色连裤袜"),
    "黑色丝袜": ("黑丝", "黑タイツ"),
    "黑丝袜": ("黑丝", "黑タイツ"),
    "丝袜": ("丝袜", "连裤袜", "裤袜"),
    "连裤袜": ("连裤袜", "丝袜"),
    "裤袜": ("裤袜", "丝袜"),
    "吊带袜": ("吊带袜",),
    "过膝袜": ("过膝袜", "膝上袜"),
    "膝上袜": ("膝上袜", "过膝袜"),
    "绝对领域": ("绝对领域",),
    "网袜": ("网袜", "渔网袜", "网眼袜"),
    "渔网袜": ("渔网袜", "网袜"),
    "网眼袜": ("网眼袜", "网袜"),
    "大腿袜": ("大腿袜",),
    # 兽耳 / 拟人特征
    "猫耳": ("猫耳", "猫娘", "猫耳娘"),
    "猫娘": ("猫娘", "猫耳"),
    "猫耳娘": ("猫耳娘", "猫耳"),
    "兔耳": ("兔耳", "兔娘"),
    "兔娘": ("兔娘", "兔耳"),
    "兔女郎": ("兔女郎",),
    "狐耳": ("狐耳", "狐狸"),
    "狐狸": ("狐狸", "狐耳"),
    "兽耳": ("兽耳",),
    "犬耳": ("犬耳",),
    "女仆": ("女仆", "女仆装"),
    "女仆装": ("女仆装", "女仆"),
    "巫女": ("巫女",),
    "修女": ("修女", "修女服"),
    "修女服": ("修女服", "修女"),
    "双马尾": ("双马尾",),
    "马尾": ("马尾", "单马尾"),
    "单马尾": ("单马尾", "马尾"),
    "长发": ("长发",),
    "黑长直": ("黑长直",),
    "短发": ("短发",),
    # 发色
    "金发": ("金发",),
    "银发": ("银发",),
    "白发": ("白发",),
    "粉发": ("粉发",),
    "蓝发": ("蓝发",),
    "绿发": ("绿发",),
    "红发": ("红发",),
    "紫发": ("紫发",),
    "黑发": ("黑发",),
    "棕发": ("棕发",),
    # 服装
    "泳装": ("泳装", "泳衣", "水着"),
    "泳衣": ("泳衣", "泳装"),
    "水着": ("水着", "泳装"),
    "比基尼": ("比基尼",),
    "死库水": ("死库水", "学校泳装"),
    "学校泳装": ("学校泳装", "死库水"),
    "连体泳装": ("连体泳装",),
    "水手服": ("水手服",),
    "jk": ("水手服", "校服"),
    "校服": ("校服", "学生服"),
    "学生服": ("学生服", "校服"),
    "制服": ("制服",),
    "和服": ("和服",),
    "浴衣": ("浴衣",),
    "旗袍": ("旗袍",),
    "体操服": ("体操服", "布鲁马"),
    "布鲁马": ("布鲁马", "体操服"),
    "运动服": ("运动服",),
    "内衣": ("内衣",),
    "情趣内衣": ("情趣内衣",),
    "蕾丝": ("蕾丝", "蕾丝内衣"),
    "蕾丝内衣": ("蕾丝内衣", "蕾丝"),
    "胸罩": ("胸罩",),
    "内裤": ("内裤", "胖次"),
    "胖次": ("胖次", "内裤"),
    "丁字裤": ("丁字裤",),
    "裸体": ("裸体", "全裸"),
    "全裸": ("全裸", "裸体"),
    # 体型 / 角色
    "萝莉": ("萝莉",),
    "loli": ("萝莉",),
    "少女": ("少女",),
    "御姐": ("御姐",),
    "巨乳": ("巨乳",),
    "贫乳": ("贫乳",),
    "爆乳": ("爆乳",),
    "伪娘": ("伪娘",),
    "扶她": ("扶她",),
    "futa": ("扶她",),
    "眼镜": ("眼镜", "眼镜娘"),
    "眼镜娘": ("眼镜娘", "眼镜"),
    # 玩法 / 内容
    "捆绑": ("捆绑", "束缚"),
    "束缚": ("束缚", "捆绑"),
    "触手": ("触手",),
    "足交": ("足交",),
    "口交": ("口交",),
    "中出": ("中出",),
    "露出": ("露出",),
    "痴女": ("痴女",),
    "调教": ("调教",),
    "自慰": ("自慰",),
    "手交": ("手交",),
    "肛交": ("肛交",),
    "妊娠": ("妊娠",),
    "母乳": ("母乳",),
    "母乳喂养": ("母乳",),
}


class CrimsonCosmosPlugin(Star):
    """Reply with a configured image when an approved keyword is received."""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        raw_config = dict(config) if config else {}
        self._config = {
            **DEFAULT_CONFIG,
            **{
                key: value for key, value in raw_config.items() if key in DEFAULT_CONFIG
            },
        }
        for group_name in CONFIG_GROUPS:
            group = raw_config.get(group_name)
            if isinstance(group, dict):
                self._config.update(
                    {
                        key: value
                        for key, value in group.items()
                        if key in DEFAULT_CONFIG
                    }
                )
        self._session: aiohttp.ClientSession | None = None
        self._wallhaven_cursors: dict[tuple[str, str, str, str | None, str], int] = {}
        self._cooldown_until: dict[tuple[str, str], float] = {}
        self._jm_cooldown_until: dict[tuple[str, str], float] = {}
        self._recall_tasks_path = (
            Path(get_astrbot_plugin_data_path())
            / "astrbot_plugin_crimson_cosmos"
            / "recall_tasks.json"
        )
        self._recall_tasks: set[asyncio.Task[None]] = set()
        self._active_recall_ids: set[str] = set()
        self._jm_data_dir = (
            Path(get_astrbot_plugin_data_path())
            / "astrbot_plugin_crimson_cosmos"
            / "jm"
        )

    async def terminate(self) -> None:
        """Close the reusable HTTP session when AstrBot unloads the plugin."""
        getattr(self, "_cooldown_until", {}).clear()
        getattr(self, "_jm_cooldown_until", {}).clear()
        for task in getattr(self, "_recall_tasks", set()):
            task.cancel()
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    @staticmethod
    def _make_session() -> aiohttp.ClientSession:
        """创建按域名分流的会话：Pixiv 等直连，被墙域名（如 r.jina.ai）走代理。

        部署环境的容器内 ``HTTP(S)_PROXY`` 指向代理，对 Pixiv 域名会触发 TLS
        握手异常（unexpected eof），且宿主机无全局 IPv6 路由，因此：
        - 强制 IPv4（TCPConnector family=AF_INET）；
        - 将直连域名注入 no_proxy 环境变量，aiohttp 对其直连；
        - 其余域名（如 r.jina.ai）通过 trust_env 读取 HTTP(S)_PROXY 走代理。
        """
        direct = ",".join(_DIRECT_PROXY_DOMAINS)
        existing = os.environ.get("no_proxy", "").strip()
        merged = f"{existing},{direct}" if existing else direct
        os.environ["no_proxy"] = merged
        os.environ["NO_PROXY"] = merged
        return aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(family=socket.AF_INET),
            trust_env=True,
        )

    def _is_event_allowed(self, event: AstrMessageEvent) -> bool:
        """Check the plugin's existing private and group access settings.

        Args:
            event: AstrBot message event.

        Returns:
            Whether the current session may use plugin commands.
        """
        if event.is_private_chat():
            if not self._config.get("enable_private", False):
                return False
            allowed_users = self._config.get("allowed_private_user_ids", [])
            if not isinstance(allowed_users, list):
                return False
            if not allowed_users:
                return True
            sender_id = str(event.get_sender_id()).strip()
            return sender_id in {
                str(user_id).strip()
                for user_id in allowed_users
                if str(user_id).strip()
            }
        allowed_groups = self._config.get("allowed_group_ids", [])
        if not self._config.get("enable_group", False):
            return False
        if not isinstance(allowed_groups, list):
            return False
        normalized_groups = {
            str(group_id).strip()
            for group_id in allowed_groups
            if str(group_id).strip()
        }
        if not normalized_groups:
            return True
        return str(event.get_group_id()).strip() in normalized_groups

    @filter.command_group("jm")
    def jm(self):
        """JM 本子搜索、详情、榜单与下载指令组。"""
        pass

    @jm.command("搜索")
    async def jm_search(
        self, event: AstrMessageEvent, keyword: str = "", page: int = 1
    ):
        """按关键词和页码搜索 JM 本子。"""
        if not self._is_event_allowed(event):
            return
        keyword = str(keyword).strip()
        try:
            page = int(page)
        except (TypeError, ValueError):
            page = 0
        if not keyword or page < 1:
            result = await self._text_result(event, "用法：/jm 搜索 <关键词> [页码]")
            if result is not None:
                yield result
            return
        async for result in self._handle_jm_action(event, "search", keyword, page):
            yield result

    @jm.command("详情")
    async def jm_info(self, event: AstrMessageEvent, album_id: str = ""):
        """查看指定 JM 本子的详细信息。"""
        if not self._is_event_allowed(event):
            return
        album_id = str(album_id).strip()
        if not album_id.isdigit():
            result = await self._text_result(event, "用法：/jm 详情 <数字ID>")
            if result is not None:
                yield result
            return
        async for result in self._handle_jm_action(event, "info", album_id):
            yield result

    @jm.command("热门")
    async def jm_hot(self, event: AstrMessageEvent, period: str = "周", page: int = 1):
        """查看 JM 日榜、周榜或月榜。"""
        if not self._is_event_allowed(event):
            return
        period_key = {
            "日": "day",
            "今日": "day",
            "day": "day",
            "周": "week",
            "本周": "week",
            "week": "week",
            "月": "month",
            "本月": "month",
            "month": "month",
        }.get(str(period).strip().lower())
        try:
            page = int(page)
        except (TypeError, ValueError):
            page = 0
        if period_key is None or page < 1:
            result = await self._text_result(event, "用法：/jm 热门 [日|周|月] [页码]")
            if result is not None:
                yield result
            return
        async for result in self._handle_jm_action(event, "hot", period_key, page):
            yield result

    @jm.command("下载")
    async def jm_download(self, event: AstrMessageEvent, album_id: str = ""):
        """下载指定 JM 本子并发送普通 ZIP 文件。"""
        if not self._is_event_allowed(event):
            return
        album_id = str(album_id).strip()
        if not album_id.isdigit():
            result = await self._text_result(event, "用法：/jm 下载 <数字ID>")
            if result is not None:
                yield result
            return
        async for result in self._handle_jm_action(event, "download", album_id):
            yield result

    async def _handle_jm_action(
        self, event: AstrMessageEvent, action: str, *args: object
    ) -> AsyncGenerator[Any, None]:
        """Run one JM action after applying existing session access rules.

        Args:
            event: AstrBot message event.
            action: Internal action name.
            *args: Validated action arguments.

        Yields:
            Text, cover, or ZIP results.
        """
        if not self._is_event_allowed(event):
            return
        if not self._config.get("enable_jm", True):
            result = await self._text_result(event, "JM 本子功能已关闭。")
            if result is not None:
                yield result
            if self._config.get("block_other_handlers", True):
                event.stop_event()
            return
        try:
            cooldown_seconds = max(
                0.0, float(self._config.get("jm_cooldown_seconds", 0) or 0)
            )
        except (TypeError, ValueError):
            cooldown_seconds = 0.0
        administrator_ids = {
            str(user_id).strip()
            for user_id in self._config.get("admin_user_ids", [])
            if str(user_id).strip()
        }
        is_administrator = str(event.get_sender_id()).strip() in administrator_ids
        if cooldown_seconds > 0 and not is_administrator:
            cooldowns = getattr(self, "_jm_cooldown_until", None)
            if not isinstance(cooldowns, dict):
                cooldowns = {}
                self._jm_cooldown_until = cooldowns
            cooldown_key = (
                "private" if event.is_private_chat() else "group",
                str(event.get_sender_id()).strip()
                if event.is_private_chat()
                else str(event.get_group_id()).strip(),
            )
            now = time.monotonic()
            if now < cooldowns.get(cooldown_key, 0.0):
                cooldown_message = str(
                    self._config.get("cooldown_message", "") or ""
                ).strip()
                if cooldown_message:
                    result = await self._text_result(event, cooldown_message)
                    if result is not None:
                        yield result
                if self._config.get("block_other_handlers", True):
                    event.stop_event()
                return
            cooldowns[cooldown_key] = now + cooldown_seconds
        fetching_message = str(self._config.get("fetching_message", "") or "").strip()
        if fetching_message:
            result = await self._text_result(event, fetching_message)
            if result is not None:
                yield result

        try:
            result = await asyncio.to_thread(self._execute_jm_action, action, *args)
        except RuntimeError as error:
            error_result = await self._text_result(event, str(error))
            if error_result is not None:
                yield error_result
            if self._config.get("block_other_handlers", True):
                event.stop_event()
            return
        except Exception:
            logger.warning(
                "[CrimsonCosmos] JM action failed: %s", action, exc_info=True
            )
            error_result = await self._text_result(
                event, "JM 获取失败，请检查网络、域名或代理配置。"
            )
            if error_result is not None:
                yield error_result
            if self._config.get("block_other_handlers", True):
                event.stop_event()
            return

        text = str(result.get("text", ""))
        if image_path := result.get("image"):
            image_file = Path(str(image_path)).resolve()
            encoded_cover = base64.b64encode(image_file.read_bytes()).decode("ascii")
            image_url = f"base64://{encoded_cover}"
            if self._config.get("use_forward", False):
                # OneBot forward nodes commonly reject base64:// images; use a
                # local file URI for the chat-record request instead.
                image_url = image_file.as_uri()
                delivery_status = await self._send_forward_images(
                    event, [image_url], [text] if text else None
                )
                if delivery_status is False:
                    delivery_status = await self._send_image_with_auto_recall(
                        event,
                        image_url,
                        text,
                        failure_message="JM 获取失败，请稍后重试。",
                    )
            else:
                delivery_status = await self._send_image_with_auto_recall(
                    event,
                    image_url,
                    text,
                    failure_message="JM 获取失败，请稍后重试。",
                )
            # If OneBot direct delivery is unavailable, let AstrBot upload the
            # local file through its normal message chain as a final fallback.
            if delivery_status is not True:
                yield event.chain_result(
                    [Image.fromFileSystem(str(image_file)), Plain(text)]
                )
        elif file_path := result.get("file"):
            sent = await self._send_file_with_auto_recall(event, Path(file_path), text)
            if not sent:
                yield event.chain_result(
                    [Plain(text), File(name=Path(file_path).name, file=str(file_path))]
                )
        else:
            text_result = await self._text_result(event, text)
            if text_result is not None:
                yield text_result

        if self._config.get("jm_auto_delete_after_send", True):
            for path in result.get("cleanup", []):
                target = Path(path)
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    target.unlink(missing_ok=True)
        if self._config.get("block_other_handlers", True):
            event.stop_event()

    def _import_jmcomic(self):
        """Import the optional JM dependency.

        Returns:
            Imported jmcomic module.

        Raises:
            RuntimeError: If jmcomic is unavailable.
        """
        try:
            return importlib.import_module("jmcomic")
        except ImportError as error:
            raise RuntimeError("JM 功能不可用，请安装 jmcomic>=2.7.0。") from error

    def _parse_jm_id(self, value: str):
        """Normalize a JM album ID through jmcomic.

        Args:
            value: User-provided numeric album ID.

        Returns:
            Parsed JM album ID.
        """
        return self._import_jmcomic().JmcomicText.parse_to_jm_id(value)

    def _build_jm_option(self):
        """Build the minimal jmcomic client and download configuration.

        Returns:
            Configured jmcomic option.
        """
        jmcomic = self._import_jmcomic()
        data_dir = getattr(
            self,
            "_jm_data_dir",
            Path(get_astrbot_plugin_data_path())
            / "astrbot_plugin_crimson_cosmos"
            / "jm",
        )
        download_dir = data_dir / "downloads"
        download_dir.mkdir(parents=True, exist_ok=True)
        option_dict: dict[str, Any] = {
            "dir_rule": {"base_dir": str(download_dir), "rule": "Bd/Aid/Pindex"},
            "download": {
                "threading": {
                    "photo": max(
                        1, int(self._config.get("jm_max_concurrent_photos", 3))
                    ),
                    "image": max(
                        1, int(self._config.get("jm_max_concurrent_images", 5))
                    ),
                }
            },
            "client": {"impl": str(self._config.get("jm_client_type", "api"))},
        }
        domains = [
            domain.strip()
            for domain in str(self._config.get("jm_client_domain", "")).split(",")
            if domain.strip()
        ]
        if domains:
            option_dict["client"]["domain"] = domains
        retry_times = max(0, int(self._config.get("jm_retry_times", 0)))
        if retry_times:
            option_dict["client"]["retry_times"] = retry_times
        proxy = str(self._config.get("jm_proxy_url", "")).strip()
        meta_data: dict[str, Any] = {
            "proxies": proxy
            if self._config.get("jm_use_proxy", False) and proxy
            else {}
        }
        cookies: dict[str, str] = {}
        for part in str(self._config.get("jm_cookies", "") or "").split(";"):
            name, separator, value = part.strip().partition("=")
            if separator and name:
                cookies[name] = value.strip()
        if cookies:
            meta_data["cookies"] = cookies
        option_dict["client"]["postman"] = {"meta_data": meta_data}
        return jmcomic.JmModuleConfig.option_class().construct(option_dict)

    def _execute_jm_action(self, action: str, *args: object) -> dict[str, Any]:
        """Execute one blocking jmcomic operation.

        Args:
            action: Search, info, hot, or download.
            *args: Validated action arguments.

        Returns:
            Message text and optional local media paths.

        Raises:
            ValueError: If the action or returned data is invalid.
        """
        option = self._build_jm_option()
        limit = min(10, max(1, int(self._config.get("jm_search_page_size", 5))))
        if action == "search":
            keyword, page = str(args[0]), int(args[1])
            albums = list(
                option.new_jm_client().search_site(keyword, page).iter_id_title_tag()
            )[:limit]
            if not albums:
                return {"text": "没有找到相关 JM 本子。"}
            lines = [f"JM 搜索：{keyword}（第 {page} 页）"]
            for album_id, title, tags in albums:
                lines.append(f"{album_id}｜{title}")
                if tags:
                    lines.append(" ".join(f"#{tag}" for tag in tags))
            return {"text": "\n".join(lines)}

        if action == "hot":
            period, page = str(args[0]), int(args[1])
            page_data = getattr(option.new_jm_client(), f"{period}_ranking")(page, "0")
            albums = list(page_data.iter_id_title())[:limit]
            labels = {"day": "日", "week": "周", "month": "月"}
            if not albums:
                return {"text": f"JM {labels[period]}榜暂无结果。"}
            lines = [f"JM {labels[period]}榜（第 {page} 页）"]
            lines.extend(f"{album_id}｜{title}" for album_id, title in albums)
            return {"text": "\n".join(lines)}

        album_id = str(args[0])
        parsed_id = str(self._parse_jm_id(album_id))
        if parsed_id.isdigit():
            parsed_id = str(int(parsed_id))
        data_dir = getattr(
            self,
            "_jm_data_dir",
            Path(get_astrbot_plugin_data_path())
            / "astrbot_plugin_crimson_cosmos"
            / "jm",
        )
        if action == "info":
            client = option.new_jm_client()
            album = client.get_album_detail(parsed_id)
            tags = getattr(album, "tags", []) or []
            text = (
                "JM 详情\n"
                f"ID：{album.id}\n"
                f"标题：{album.title}\n"
                f"作者：{getattr(album, 'author', '') or '未知'}\n"
                f"章节：{len(album)}"
            )
            if tags:
                text += "\n" + " ".join(f"#{tag}" for tag in tags)
            cover_dir = data_dir / "covers"
            cover_dir.mkdir(parents=True, exist_ok=True)
            cover_path = cover_dir / f"{parsed_id}.jpg"
            if not cover_path.exists():
                client.download_album_cover(parsed_id, str(cover_path))
            return {
                "text": text,
                **({"image": cover_path} if cover_path.exists() else {}),
            }

        if action == "download":
            jmcomic = self._import_jmcomic()
            with jmcomic.JmDownloader(option) as downloader:
                album = downloader.download_album(parsed_id)
            album_dir = Path(option.dir_rule.decide_album_root_dir(album))
            if not album_dir.exists():
                raise ValueError("JM 下载目录不存在。")
            data_dir.mkdir(parents=True, exist_ok=True)
            archive_path = data_dir / f"JM{album.id}.zip"
            archive_path.unlink(missing_ok=True)
            with zipfile.ZipFile(
                archive_path, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                for image_path in sorted(album_dir.rglob("*")):
                    if image_path.is_file():
                        archive.write(image_path, image_path.relative_to(album_dir))
            return {
                "text": f"JM{album.id}《{album.title}》下载完成。",
                "file": archive_path,
                "cleanup": [album_dir, archive_path],
            }

        raise ValueError(f"Unknown JM action: {action}")

    # ------------------------------------------------------------------ #
    # 过审（反拦截）处理：发送前下载原图、加扰动并重新编码，改变图片指纹
    # 并干扰内容识别；也可改为以文件消息发送，走相对宽松的审核通道。
    # ------------------------------------------------------------------ #

    def _bypass_config(self) -> dict[str, Any] | None:
        """Return normalized bypass parameters, or ``None`` when disabled."""
        mode = str(self._config.get("bypass_mode", "off") or "off").strip().lower()
        if mode not in {"transform", "file", "transform_file"}:
            return None
        try:
            noise = max(0, min(64, int(self._config.get("bypass_noise", 8) or 0)))
        except (TypeError, ValueError):
            noise = 8
        try:
            rotate = max(
                0.0, min(12.0, float(self._config.get("bypass_rotate", 1.0) or 0))
            )
        except (TypeError, ValueError):
            rotate = 1.0
        try:
            resize_ratio = max(
                0.70,
                min(1.0, float(self._config.get("bypass_resize_ratio", 0.98) or 1)),
            )
        except (TypeError, ValueError):
            resize_ratio = 1.0
        try:
            jpeg_quality = max(
                50, min(100, int(self._config.get("bypass_jpeg_quality", 90) or 90))
            )
        except (TypeError, ValueError):
            jpeg_quality = 90
        try:
            hue_shift = max(
                -45, min(45, int(self._config.get("bypass_hue_shift", 0) or 0))
            )
        except (TypeError, ValueError):
            hue_shift = 0
        try:
            brightness = max(
                0.7,
                min(1.4, float(self._config.get("bypass_brightness", 1.0) or 1.0)),
            )
        except (TypeError, ValueError):
            brightness = 1.0
        return {
            "mode": mode,
            "noise": noise,
            "rotate": rotate,
            "flip": bool(self._config.get("bypass_flip", True)),
            "resize_ratio": resize_ratio,
            "jpeg_quality": jpeg_quality,
            "hue_shift": hue_shift,
            "brightness": brightness,
        }

    def _perturb_image(self, data: bytes, cfg: dict[str, Any]) -> bytes:
        """Apply adversarial perturbations and re-encode the image as JPEG."""
        try:
            from PIL import Image, ImageChops, ImageEnhance, ImageOps
        except ImportError as error:
            raise RuntimeError("需要安装 Pillow 以启用过审处理。") from error

        resample_lanczos = getattr(
            getattr(Image, "Resampling", Image), "LANCZOS", Image.LANCZOS
        )
        resample_bicubic = getattr(
            getattr(Image, "Resampling", Image), "BICUBIC", Image.BICUBIC
        )

        with Image.open(io.BytesIO(data)) as opened:
            image = ImageOps.exif_transpose(opened)
            if image.mode == "RGBA":
                background = Image.new("RGB", image.size, (255, 255, 255))
                background.paste(image, mask=image.getchannel("A"))
                image = background
            elif image.mode != "RGB":
                image = image.convert("RGB")

            ratio = cfg["resize_ratio"]
            if ratio < 1.0:
                scale = random.uniform(ratio, 1.0)
                image = image.resize(
                    (
                        max(1, int(image.width * scale)),
                        max(1, int(image.height * scale)),
                    ),
                    resample_lanczos,
                )
            if cfg["rotate"]:
                angle = random.uniform(-cfg["rotate"], cfg["rotate"])
                image = image.rotate(angle, resample=resample_bicubic, expand=False)
            if cfg["flip"] and random.random() < 0.5:
                image = ImageOps.mirror(image)
            if cfg["hue_shift"]:
                hue, saturation, value = image.convert("HSV").split()
                delta = int(round(cfg["hue_shift"] / 360 * 256))
                hue_table = [((index + delta) % 256) for index in range(256)]
                image = Image.merge(
                    "HSV", (hue.point(hue_table), saturation, value)
                ).convert("RGB")
            if cfg["brightness"] != 1.0:
                image = ImageEnhance.Brightness(image).enhance(cfg["brightness"])
            if cfg["noise"]:
                sigma = float(cfg["noise"])
                noise_image = Image.effect_noise(image.size, sigma).convert("RGB")
                image = ImageChops.add(image, noise_image, scale=1.0, offset=-128)

            output = io.BytesIO()
            image.save(
                output,
                format="JPEG",
                quality=cfg["jpeg_quality"],
                subsampling=0,
                optimize=True,
            )
            return output.getvalue()

    async def _load_image_bytes(self, image_ref: str) -> bytes:
        """Load image bytes from an inline base64 string, URL, or local path."""
        if image_ref.startswith("base64://"):
            return base64.b64decode(image_ref.removeprefix("base64://"), validate=True)
        parsed = urlsplit(image_ref)
        if parsed.scheme in {"http", "https"}:
            if self._session is None or self._session.closed:
                self._session = self._make_session()
            async with self._session.get(
                image_ref, timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                response.raise_for_status()
                buffer = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    buffer.extend(chunk)
                    if len(buffer) > 20 * 1024 * 1024:
                        raise ValueError("图片超过 20 MiB 限制。")
                return bytes(buffer)
        if parsed.scheme != "file":
            # 纯本地路径（例如 Windows 的 C:\\...）：urlsplit 会把盘符误判为
            # 单字母协议，这里直接按原路径读取，避免丢掉盘符。
            return Path(image_ref).read_bytes()
        path_value = parsed.path
        if parsed.netloc:
            path_value = f"//{parsed.netloc}{parsed.path}"
        elif (
            path_value.startswith("/") and len(path_value) > 2 and path_value[2] == ":"
        ):
            # Windows file:///C:/... URI: drop the leading slash.
            path_value = path_value[1:]
        return Path(path_value).read_bytes()

    async def _prepare_image_ref(self, image_ref: str) -> tuple[str, str]:
        """Return ``(kind, ref)`` after optionally bypass-processing the image.

        ``kind`` is ``"image"`` or ``"file"``. With bypass disabled ``ref`` is
        the original reference. With bypass enabled, ``"image"`` refs are
        inlined as ``base64://`` data so any OneBot endpoint can load them
        regardless of host; ``"file"`` refs keep a temporary file path that is
        converted to base64 at the OneBot send boundary.
        """
        cfg = self._bypass_config()
        if cfg is None:
            return ("image", image_ref)
        try:
            data = await self._load_image_bytes(image_ref)
            processed = await asyncio.to_thread(self._perturb_image, data, cfg)
        except Exception:
            logger.warning("[CrimsonCosmos] 过审处理失败，回退发送原图", exc_info=True)
            return ("image", image_ref)
        as_file = cfg["mode"] in {"file", "transform_file"}
        if not as_file:
            return (
                "image",
                "base64://" + base64.b64encode(processed).decode("ascii"),
            )
        temporary_dir = Path(get_astrbot_temp_path())
        temporary_dir.mkdir(parents=True, exist_ok=True)
        temporary_path = temporary_dir / (f"crimson_cosmos_bypass_{time.time_ns()}.jpg")
        temporary_path.write_bytes(processed)
        return ("file", str(temporary_path.resolve()))

    async def _inline_local_file_ref(self, file_ref: str) -> str:
        """Inline a local file as base64 so the OneBot endpoint can load it.

        The OneBot protocol endpoint usually runs in a separate process or
        container and cannot read the AstrBot temp directory, so local temp
        files are re-encoded as ``base64://`` data. URLs and base64 data pass
        through unchanged.
        """
        if file_ref.startswith(("http://", "https://", "base64://")):
            return file_ref
        try:
            data = await asyncio.to_thread(Path(file_ref).read_bytes)
        except OSError:
            return file_ref
        return "base64://" + base64.b64encode(data).decode("ascii")

    def _components_from_prepared(
        self, prepared: tuple[str, str], text: str | None = None
    ) -> list[Any]:
        """Build AstrBot message components from a ``(kind, ref)`` pair."""
        kind, ref = prepared
        components: list[Any] = []
        if kind == "file":
            components.append(File(name=Path(ref).name, file=ref))
        elif ref.startswith(("http://", "https://", "base64://")):
            components.append(Image.fromURL(ref))
        else:
            components.append(Image.fromFileSystem(ref))
        if text:
            components.append(Plain(text))
        return components

    @staticmethod
    def _cleanup_local_images(paths: list[str]) -> None:
        """尽力删除已下载到磁盘的临时图片文件。"""
        for ref in paths:
            try:
                Path(ref).unlink(missing_ok=True)
            except OSError:
                continue

    async def _build_image_components(
        self, image_ref: str, text: str | None = None
    ) -> list[Any]:
        """Bypass-process an image and return ready-to-send components."""
        return self._components_from_prepared(
            await self._prepare_image_ref(image_ref), text
        )

    async def _send_onebot_segments(
        self,
        event: AstrMessageEvent,
        segments: list[dict[str, Any]],
        *,
        as_forward: bool,
    ) -> bool | None:
        """通过 OneBot 发送消息段，并按全局配置安排撤回。

        返回 ``None`` 表示当前事件没有可用的 OneBot 发送能力，调用方应
        回退 AstrBot 普通结果；返回 ``False`` 表示尝试发送但失败。
        """
        bot = getattr(event, "bot", None)
        if bot is None or not callable(getattr(bot, "call_action", None)):
            return None

        routing_params: dict[str, Any] = {}
        raw_event = getattr(getattr(event, "message_obj", None), "raw_message", None)
        get_raw_value = getattr(raw_event, "get", None)
        if callable(get_raw_value) and (self_id := get_raw_value("self_id")):
            routing_params["self_id"] = self_id

        if event.is_private_chat():
            target_id = str(event.get_sender_id()).strip()
            if not target_id.isdigit():
                return None
            recipient = {"user_id": int(target_id)}
            action = "send_private_forward_msg" if as_forward else "send_private_msg"
        else:
            target_id = str(event.get_group_id()).strip()
            if not target_id.isdigit():
                return None
            recipient = {"group_id": int(target_id)}
            action = "send_group_forward_msg" if as_forward else "send_group_msg"

        try:
            if as_forward:
                try:
                    uin = int(routing_params.get("self_id", 0))
                except (TypeError, ValueError):
                    uin = 0
                response = await bot.call_action(
                    action,
                    **recipient,
                    messages=[
                        {
                            "type": "node",
                            "data": {
                                "name": "聊天记录",
                                "uin": uin,
                                "content": segments,
                            },
                        }
                    ],
                    **routing_params,
                )
            else:
                response = await bot.call_action(
                    action,
                    **recipient,
                    message=segments,
                    **routing_params,
                )
        except Exception:
            logger.warning("[CrimsonCosmos] OneBot message send failed", exc_info=True)
            return False

        if self._config.get("auto_recall", False):
            message_id = (
                response.get("message_id") if isinstance(response, dict) else None
            )
            if message_id is None:
                logger.warning("[CrimsonCosmos] Sent message ID is unavailable")
            else:
                delay = self._recall_delay_seconds()
                self._schedule_recall(
                    bot,
                    {
                        "message_id": message_id,
                        "due_at": time.time() + delay,
                        "routing_params": routing_params,
                    },
                )
        return True

    async def _text_result(self, event: AstrMessageEvent, text: str) -> Any | None:
        """应用全局发送选项；需要普通回退时返回 AstrBot 文本结果。"""
        if not (
            self._config.get("use_forward", False)
            or self._config.get("auto_recall", False)
        ):
            return event.plain_result(text)
        sent = await self._send_onebot_segments(
            event,
            [{"type": "text", "data": {"text": text}}],
            as_forward=bool(self._config.get("use_forward", False)),
        )
        return None if sent is True else event.plain_result(text)

    async def _send_file_with_auto_recall(
        self, event: AstrMessageEvent, file_path: Path, text: str
    ) -> bool:
        """Send a local file through OneBot and optionally schedule recall.

        Args:
            event: AstrBot message event.
            file_path: Local ZIP path.
            text: Result text sent with the file.

        Returns:
            Whether the file was sent through a recall-capable adapter.
        """
        sent = await self._send_onebot_segments(
            event,
            [
                {"type": "text", "data": {"text": text}},
                {
                    "type": "file",
                    "data": {"name": file_path.name, "file": str(file_path)},
                },
            ],
            as_forward=bool(self._config.get("use_forward", False)),
        )
        return sent is True

    @filter.command_group("av")
    def av(self):
        """Jable 影片查询指令组。"""
        pass

    @filter.command("helpav")
    async def helpav(self, event: AstrMessageEvent):
        """显示插件所有参考命令。"""
        text = (
            "R18 图片与本子查询总帮助\n\n"
            "/helpav\n\n"
            "图片：\n"
            "色图\n"
            "来三份白丝、猫耳色图\n\n"
            "AV 影片：\n"
            "/av 热门 今日 1\n"
            "/av 热门 本周 1\n"
            "/av 热门 本月 1-10\n"
            "/av 热门 全部 1\n\n"
            "/av 新片 1\n"
            "/av 新片 1-10\n\n"
            "/av 主题 黑丝 1\n"
            "/av 主题 黑丝 最高收藏 1-10\n\n"
            "/av 女优 河北彩花 1\n"
            "/av 女优 河北彩花 最近更新 1-10\n\n"
            "/av 搜索 SSIS-001 1\n"
            "/av 磁力 SSIS-001\n"
            "/av 磁力 https://missav.ws/dm44/cn/ssis-001\n\n"
            "JM 本子：\n"
            "/jm 搜索 全彩 1\n"
            "/jm 详情 123456\n"
            "/jm 热门 周 1\n"
            "/jm 下载 123456\n\n"
            "主题和女优排序：近期最佳、最近更新、最多观看、最高收藏\n"
            "AV 排名范围：1-30；连续获取每次最多 10 部。"
        )
        result = await self._text_result(event, text)
        if result is not None:
            yield result

    async def _handle_jable_command(
        self, event: AstrMessageEvent, message: str
    ) -> AsyncGenerator[Any, None]:
        """Handle a registered AV command with plugin access controls."""
        if not self._is_event_allowed(event):
            return
        block_other_handlers = self._config.get("block_other_handlers", True)
        if not self._config.get("enable_jable", True):
            result = await self._text_result(event, "Jable 影片查询已关闭。")
            if result is not None:
                yield result
            if block_other_handlers:
                event.stop_event()
            return
        jable_request = self._parse_jable_request(message)
        if jable_request is None:
            text = (
                "用法：/av 热门 今日|本周|本月|全部 1-30、"
                "/av 新片 1-30、/av 主题|女优 名称 "
                "[近期最佳|最近更新|最多观看|最高收藏] 1-30"
            )
            result = await self._text_result(event, text)
            if result is not None:
                yield result
            if block_other_handlers:
                event.stop_event()
            return

        fetching_message = str(self._config.get("fetching_message", "") or "").strip()
        if fetching_message:
            result = await self._text_result(event, fetching_message)
            if result is not None:
                yield result
        target_url, rank_request, list_name = jable_request
        ranks = (
            list(range(rank_request[0], rank_request[1] + 1))
            if isinstance(rank_request, tuple)
            else [rank_request]
        )
        videos: list[dict[str, Any]] = []
        listing_cache: dict[str, str | asyncio.Task[str]] = {}
        detail_slots = asyncio.Semaphore(5)

        async def fetch_rank(rank: int) -> dict[str, Any]:
            async with detail_slots:
                return await self._fetch_jable_video(
                    (target_url, rank, list_name, listing_cache)
                )

        tasks = [asyncio.create_task(fetch_rank(rank)) for rank in ranks]
        done, pending = await asyncio.wait(tasks, timeout=60)
        if pending:
            logger.warning(
                "[CrimsonCosmos] Jable range timed out with %d item(s) pending",
                len(pending),
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        for task in tasks:
            if task not in done or task.cancelled():
                continue
            try:
                result = task.result()
            except Exception as error:
                logger.warning("[CrimsonCosmos] Jable item request failed: %s", error)
            else:
                videos.append(result)
        listing_cache.clear()
        if not videos:
            logger.warning("[CrimsonCosmos] Jable request failed")
            failure_message = str(self._config.get("failure_message", "") or "").strip()
            result = await self._text_result(
                event, failure_message or "影片获取失败，请稍后重试。"
            )
            if result is not None:
                yield result
            if block_other_handlers:
                event.stop_event()
            return

        reports: list[str] = []
        for video in videos:
            report_lines = [f"🎬 {video['list_name']} 第 {video['rank']} 名"]
            if self._config.get("jable_show_code", True):
                report_lines.append(f"车牌号：{video['code']}")
            if self._config.get("jable_show_title", True):
                report_lines.append(f"标题：{video['title']}")
            if self._config.get("jable_show_stars", True):
                report_lines.append(f"⭐ 红星：{video['stars']}")
            if self._config.get("jable_show_themes", True):
                report_lines.append(
                    f"#主题：{' '.join('#' + tag for tag in video['tags'])}"
                )
            if self._config.get("jable_show_detail_link", True):
                report_lines.append(f"链接：{video['url']}")
            reports.append("\n".join(report_lines))

        show_cover = bool(self._config.get("jable_show_cover", True))
        use_forward = bool(self._config.get("use_forward", False))
        sent_as_forward = (
            show_cover
            and use_forward
            and await self._send_forward_images(
                event, [video["cover"] for video in videos], reports
            )
        )
        if show_cover and not sent_as_forward:
            for video, report in zip(videos, reports, strict=True):
                delivery_status = await self._send_image_with_auto_recall(
                    event, video["cover"], report
                )
                if delivery_status is None:
                    yield event.chain_result(
                        await self._build_image_components(video["cover"], report)
                    )
        elif not show_cover:
            for report in reports:
                result = await self._text_result(event, report)
                if result is not None:
                    yield result
        if block_other_handlers:
            event.stop_event()

    @av.command("热门")
    async def av_hot(self, event: AstrMessageEvent, period: str = "", rank: str = ""):
        """查询今日、本周、本月或全部热门影片。"""
        async for result in self._handle_jable_command(
            event, f"/av 热门 {period} {rank}"
        ):
            yield result

    @av.command("新片")
    async def av_new(self, event: AstrMessageEvent, rank: str = ""):
        """查询 Jable 新片榜。"""
        async for result in self._handle_jable_command(event, f"/av 新片 {rank}"):
            yield result

    @av.command("主题")
    async def av_theme(
        self,
        event: AstrMessageEvent,
        theme: str = "",
        sort_mode: str = "",
        rank: str = "",
    ):
        """按 Jable 主题查询影片。"""
        suffix = f"{sort_mode} {rank}" if rank else sort_mode
        async for result in self._handle_jable_command(
            event, f"/av 主题 {theme} {suffix}"
        ):
            yield result

    @av.command("女优")
    async def av_model(
        self,
        event: AstrMessageEvent,
        model: str = "",
        sort_mode: str = "",
        rank: str = "",
    ):
        """按女优名称查询影片。"""
        suffix = f"{sort_mode} {rank}" if rank else sort_mode
        async for result in self._handle_jable_command(
            event, f"/av 女优 {model} {suffix}"
        ):
            yield result

    @av.command("搜索")
    async def av_search(
        self, event: AstrMessageEvent, keyword: str = "", rank: int = 1
    ):
        """按番号、女优或标题关键词搜索 MissAV 影片。

        Args:
            event: AstrBot 消息事件。
            keyword: 搜索关键词。
            rank: 1 到 30 的结果排名。

        Yields:
            获取提示、影片封面和所选影片信息。
        """
        if not self._is_event_allowed(event):
            return
        block_other_handlers = self._config.get("block_other_handlers", True)
        if not self._config.get("enable_jable", True):
            result = await self._text_result(event, "Jable 影片查询已关闭。")
            if result is not None:
                yield result
            if block_other_handlers:
                event.stop_event()
            return
        keyword = str(keyword).strip()
        try:
            rank = int(rank)
        except (TypeError, ValueError):
            rank = 0
        if not keyword or not 1 <= rank <= 30:
            result = await self._text_result(event, "用法：/av 搜索 <关键词> [1-30]")
            if result is not None:
                yield result
            if block_other_handlers:
                event.stop_event()
            return

        fetching_message = str(self._config.get("fetching_message", "") or "").strip()
        if fetching_message:
            result = await self._text_result(event, fetching_message)
            if result is not None:
                yield result
        try:
            video = await self._fetch_missav_video(keyword, rank)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
            logger.warning("[CrimsonCosmos] MissAV search failed: %s", error)
            failure_message = str(self._config.get("failure_message", "") or "").strip()
            result = await self._text_result(
                event, failure_message or "影片获取失败，请稍后重试。"
            )
            if result is not None:
                yield result
            if block_other_handlers:
                event.stop_event()
            return

        report = (
            f"🎬 MissAV 搜索 第 {rank} 名\n"
            f"车牌号：{video['code']}\n"
            f"标题：{video['title']}\n"
            f"链接：{video['url']}"
        )
        if self._config.get("use_forward", False):
            forward_status = await self._send_forward_images(
                event, [video["cover"]], [report]
            )
            if forward_status is not True:
                yield event.chain_result(
                    await self._build_image_components(video["cover"], report)
                )
        else:
            delivery_status = await self._send_image_with_auto_recall(
                event, video["cover"], report
            )
            if delivery_status is None:
                yield event.chain_result(
                    await self._build_image_components(video["cover"], report)
                )
        if block_other_handlers:
            event.stop_event()

    @av.command("磁力")
    async def av_magnet(self, event: AstrMessageEvent, target: str = ""):
        """获取指定 MissAV 影片的磁力链接。

        Args:
            event: AstrBot 消息事件。
            target: 影片番号或 MissAV 详情链接。

        Yields:
            获取提示和最多五条磁力链接。
        """
        if not self._is_event_allowed(event):
            return
        block_other_handlers = self._config.get("block_other_handlers", True)
        if not self._config.get("enable_jable", True):
            result = await self._text_result(event, "Jable 影片查询已关闭。")
            if result is not None:
                yield result
            if block_other_handlers:
                event.stop_event()
            return
        target = str(target).strip()
        if target.startswith(("http://", "https://")):
            parsed = urlsplit(target)
            valid_target = (
                parsed.scheme == "https"
                and parsed.hostname == "missav.ws"
                and re.fullmatch(r"/(?:dm\d+/)?cn/[^/]+/?", parsed.path) is not None
            )
        else:
            valid_target = bool(target) and "://" not in target
        if not valid_target:
            result = await self._text_result(
                event, "用法：/av 磁力 <番号或 MissAV 详情链接>"
            )
            if result is not None:
                yield result
            if block_other_handlers:
                event.stop_event()
            return

        fetching_message = str(self._config.get("fetching_message", "") or "").strip()
        if fetching_message:
            result = await self._text_result(event, fetching_message)
            if result is not None:
                yield result
        try:
            video = await self._fetch_missav_video(
                target, prefer_exact=True, include_magnets=True
            )
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
            logger.warning("[CrimsonCosmos] MissAV magnet request failed: %s", error)
            failure_message = str(self._config.get("failure_message", "") or "").strip()
            result = await self._text_result(
                event, failure_message or "磁力获取失败，请稍后重试。"
            )
            if result is not None:
                yield result
            if block_other_handlers:
                event.stop_event()
            return

        magnets = "\n".join(
            f"{index}. {magnet}"
            for index, magnet in enumerate(video["magnets"], start=1)
        )
        result = await self._text_result(event, f"🧲 {video['title']}\n{magnets}")
        if result is not None:
            yield result
        if block_other_handlers:
            event.stop_event()

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1000)
    async def on_message(self, event: AstrMessageEvent):
        """处理已配置会话中的关键词图片请求。

        Args:
            event: AstrBot 收到的消息事件。

        Yields:
            获取进度、图片结果、PID 或安全的失败提示。
        """
        await self._resume_recall_tasks(event)
        message = event.get_message_str().strip()
        if not message:
            return

        block_other_handlers = self._config.get("block_other_handlers", True)
        if message.lower().startswith(("/av", "/jm")):
            return

        request = self._parse_image_request(message)
        if request is None:
            return
        if not self._is_event_allowed(event):
            if not event.is_private_chat() and not self._config.get(
                "enable_group", False
            ):
                group_disabled_message = str(
                    self._config.get("group_disabled_message", "") or ""
                ).strip()
                if group_disabled_message:
                    result = await self._text_result(event, group_disabled_message)
                    if result is not None:
                        yield result
                if block_other_handlers:
                    event.stop_event()
            return
        image_count, message_tags = request
        if image_count > MAX_IMAGES_PER_REQUEST:
            result = await self._text_result(
                event, f"单次最多获取 {MAX_IMAGES_PER_REQUEST} 张图片。"
            )
            if result is not None:
                yield result
            if block_other_handlers:
                event.stop_event()
            return

        try:
            cooldown_seconds = max(
                0.0, float(self._config.get("cooldown_seconds", 0) or 0)
            )
        except (TypeError, ValueError):
            cooldown_seconds = 0.0
        administrator_ids = {
            str(user_id).strip()
            for user_id in self._config.get("admin_user_ids", [])
            if str(user_id).strip()
        }
        is_administrator = str(event.get_sender_id()).strip() in administrator_ids
        if cooldown_seconds > 0 and not is_administrator:
            cooldowns = getattr(self, "_cooldown_until", None)
            if not isinstance(cooldowns, dict):
                cooldowns = {}
                self._cooldown_until = cooldowns
            conversation_id = (
                str(event.get_group_id()).strip()
                if not event.is_private_chat()
                else str(event.get_sender_id()).strip()
            )
            cooldown_key = (
                "private" if event.is_private_chat() else "group",
                conversation_id,
            )
            now = time.monotonic()
            if now < cooldowns.get(cooldown_key, 0.0):
                cooldown_message = str(
                    self._config.get("cooldown_message", "") or ""
                ).strip()
                if cooldown_message:
                    result = await self._text_result(event, cooldown_message)
                    if result is not None:
                        yield result
                if block_other_handlers:
                    event.stop_event()
                return
            cooldowns[cooldown_key] = now + cooldown_seconds

        fetching_message = str(self._config.get("fetching_message", "") or "").strip()
        if fetching_message:
            result = await self._text_result(event, fetching_message)
            if result is not None:
                yield result

        configured_sources = self._config.get("image_source_order", [])
        if not isinstance(configured_sources, list) or not configured_sources:
            configured_sources = [self._config.get("image_source", "lolicon")]
        sources = list(
            dict.fromkeys(
                str(source).strip().lower()
                for source in configured_sources
                if str(source).strip()
            )
        )
        if not self._config.get("enable_lolicon", True):
            sources = [source for source in sources if source != "lolicon"]
        try:
            retry_count = min(
                5, max(1, int(self._config.get("request_retry_count", 3)))
            )
        except (TypeError, ValueError):
            retry_count = 3

        image_urls: list[str] | None = None
        image_pids: list[str] = []
        last_error: Exception | None = None
        for source in sources:
            for attempt in range(retry_count):
                try:
                    if source == "wallhaven":
                        image_urls = await self._fetch_wallhaven_images(
                            image_count, message_tags
                        )
                        image_pids = []
                    elif source == "lolicon":
                        image_urls, image_pids = await self._fetch_lolicon_images(
                            image_count, message_tags
                        )
                    else:
                        raise ValueError("未知的图片来源配置。")
                    break
                except (ValueError, aiohttp.ClientError, asyncio.TimeoutError) as error:
                    last_error = error
                    logger.warning(
                        "[CrimsonCosmos] Image source failed: source=%s attempt=%d/%d",
                        source,
                        attempt + 1,
                        retry_count,
                    )
            if image_urls is not None:
                break

        if image_urls is None:
            if not sources:
                result = await self._text_result(
                    event,
                    "所有图片来源均已关闭，请至少启用 Lolicon 或配置 Wallhaven。",
                )
                if result is not None:
                    yield result
            else:
                failure_message = str(
                    self._config.get("failure_message", "") or ""
                ).strip()
                if failure_message:
                    result = await self._text_result(event, failure_message)
                elif isinstance(last_error, ValueError):
                    result = await self._text_result(event, str(last_error))
                else:
                    result = await self._text_result(
                        event, "图片获取失败，请稍后重试。"
                    )
                if result is not None:
                    yield result
            if block_other_handlers:
                event.stop_event()
            return

        temp_paths = [
            ref
            for ref in image_urls
            if not ref.startswith(("http://", "https://", "base64://"))
        ]
        pid_text = (
            "Pixiv PID: " + ",".join(image_pids)
            if self._config.get("show_pixiv_pid", False) and image_pids
            else None
        )
        use_forward = bool(self._config.get("use_forward", False))
        forward_texts = (
            [f"Pixiv PID: {pid}" for pid in image_pids]
            if pid_text and use_forward
            else None
        )
        forward_status = (
            await self._send_forward_images(event, image_urls, forward_texts)
            if use_forward
            else None
        )
        if forward_status is False:
            self._cleanup_local_images(temp_paths)
            failure_message = str(self._config.get("failure_message", "") or "").strip()
            result = await self._text_result(
                event, failure_message or "图片发送失败，请稍后重试。"
            )
            if result is not None:
                yield result
            if block_other_handlers:
                event.stop_event()
            return
        sent_as_forward = forward_status is True
        all_images_delivered = True
        if not sent_as_forward:
            delivery_failure_message = (
                str(self._config.get("failure_message", "") or "").strip() or None
            )
            prepared_refs = await asyncio.gather(
                *(self._prepare_image_ref(image_url) for image_url in image_urls)
            )
            for image_url, prepared in zip(image_urls, prepared_refs, strict=True):
                delivery_status = await self._send_image_with_auto_recall(
                    event,
                    image_url,
                    failure_message=delivery_failure_message,
                    prepared=prepared,
                )
                if delivery_status is None:
                    if prepared == ("image", image_url):
                        yield event.image_result(image_url)
                    else:
                        yield event.chain_result(
                            self._components_from_prepared(prepared)
                        )
                elif not delivery_status:
                    all_images_delivered = False
        if pid_text and not sent_as_forward and all_images_delivered:
            result = await self._text_result(event, pid_text)
            if result is not None:
                yield result
        self._cleanup_local_images(temp_paths)
        if block_other_handlers:
            event.stop_event()

    async def _send_forward_images(
        self,
        event: AstrMessageEvent,
        image_urls: list[str],
        texts: list[str] | None = None,
    ) -> bool | None:
        """Send one or more images as one OneBot forward message.

        Args:
            event: Incoming AstrBot message event.
            image_urls: Remote image URLs to merge.
            texts: Optional text paired with each image node.

        Returns:
            ``True`` when the forward message was sent, ``False`` when an
            attempted delivery failed, or ``None`` when the adapter cannot
            send forward messages and the caller may use normal delivery.
        """
        bot = getattr(event, "bot", None)
        if bot is None or not callable(getattr(bot, "call_action", None)):
            return None

        routing_params: dict[str, Any] = {}
        raw_event = getattr(getattr(event, "message_obj", None), "raw_message", None)
        get_raw_value = getattr(raw_event, "get", None)
        if callable(get_raw_value) and (self_id := get_raw_value("self_id")):
            routing_params["self_id"] = self_id

        if event.is_private_chat():
            user_id = str(event.get_sender_id()).strip()
            if not user_id.isdigit():
                return None
            action = "send_private_forward_msg"
            recipient = {"user_id": int(user_id)}
        else:
            group_id = str(event.get_group_id()).strip()
            if not group_id.isdigit():
                return None
            action = "send_group_forward_msg"
            recipient = {"group_id": int(group_id)}

        try:
            uin = int(routing_params.get("self_id", 0))
        except (TypeError, ValueError):
            uin = 0
        prepared_refs = await asyncio.gather(
            *(self._prepare_image_ref(image_url) for image_url in image_urls)
        )
        messages = []
        for index, (kind, file_ref) in enumerate(prepared_refs):
            content: list[dict[str, Any]] = []
            if texts and index < len(texts):
                content.append({"type": "text", "data": {"text": texts[index]}})
            inline_ref = await self._inline_local_file_ref(file_ref)
            if kind == "file":
                content.append(
                    {
                        "type": "file",
                        "data": {"file": inline_ref, "name": Path(file_ref).name},
                    }
                )
            else:
                content.append({"type": "image", "data": {"file": inline_ref}})
            messages.append(
                {
                    "type": "node",
                    "data": {"name": "聊天记录", "uin": uin, "content": content},
                }
            )
        try:
            response = await bot.call_action(
                action, **recipient, messages=messages, **routing_params
            )
        except Exception:
            logger.warning("[CrimsonCosmos] Forward image send failed", exc_info=True)
            return False

        if not self._config.get("auto_recall", False):
            return True
        message_id = response.get("message_id") if isinstance(response, dict) else None
        if message_id is None:
            logger.warning("[CrimsonCosmos] Forward message ID is unavailable")
            return True
        delay = self._recall_delay_seconds()
        self._schedule_recall(
            bot,
            {
                "message_id": message_id,
                "due_at": time.time() + delay,
                "routing_params": routing_params,
            },
        )
        return True

    async def _send_image_with_auto_recall(
        self,
        event: AstrMessageEvent,
        image_url: str,
        text: str | None = None,
        *,
        failure_message: str | None = None,
        prepared: tuple[str, str] | None = None,
    ) -> bool | None:
        """Send an image through OneBot and schedule a recall when enabled.

        Args:
            event: Incoming AstrBot message event.
            image_url: Remote image URL to send.
            text: Optional text sent in the same recallable message.
            failure_message: Optional plain text to send when direct delivery fails.
            prepared: Optional precomputed ``(kind, ref)`` from ``_prepare_image_ref``
                to avoid re-processing the image.

        Returns:
            ``True`` when the image was sent, ``False`` when delivery failed after
            attempting the optional failure message, or ``None`` when the caller
            should fall back to AstrBot's normal result delivery.
        """
        if not self._config.get("auto_recall", False) and failure_message is None:
            return None

        bot = getattr(event, "bot", None)
        if bot is None or not callable(getattr(bot, "call_action", None)):
            return None

        routing_params: dict[str, Any] = {}
        raw_event = getattr(getattr(event, "message_obj", None), "raw_message", None)
        get_raw_value = getattr(raw_event, "get", None)
        if callable(get_raw_value) and (self_id := get_raw_value("self_id")):
            routing_params["self_id"] = self_id

        if event.is_private_chat():
            user_id = str(event.get_sender_id()).strip()
            if not user_id.isdigit():
                return None
            action = "send_private_msg"
            recipient = {"user_id": int(user_id)}
        else:
            group_id = str(event.get_group_id()).strip()
            if not group_id.isdigit():
                return None
            action = "send_group_msg"
            recipient = {"group_id": int(group_id)}

        if prepared is None:
            prepared = await self._prepare_image_ref(image_url)
        kind, file_ref = prepared
        inline_ref = await self._inline_local_file_ref(file_ref)
        if kind == "file":
            message: list[dict[str, Any]] = [
                {
                    "type": "file",
                    "data": {"file": inline_ref, "name": Path(file_ref).name},
                }
            ]
        else:
            message = [{"type": "image", "data": {"file": inline_ref}}]
        if text:
            message.append({"type": "text", "data": {"text": text}})
        response = None
        try:
            response = await bot.call_action(
                action,
                **recipient,
                message=message,
                **routing_params,
            )
        except Exception:
            if kind == "image" and inline_ref.startswith("base64://"):
                temporary_image: Path | None = None
                try:
                    image_bytes = base64.b64decode(
                        inline_ref.removeprefix("base64://"), validate=True
                    )
                    suffix = (
                        ".png"
                        if image_bytes.startswith(b"\x89PNG\r\n\x1a\n")
                        else ".gif"
                        if image_bytes.startswith((b"GIF87a", b"GIF89a"))
                        else ".webp"
                        if image_bytes[8:12] == b"WEBP"
                        else ".jpg"
                    )
                    temporary_dir = Path(get_astrbot_temp_path())
                    temporary_dir.mkdir(parents=True, exist_ok=True)
                    temporary_image = temporary_dir / (
                        f"crimson_cosmos_{time.time_ns()}{suffix}"
                    )
                    temporary_image.write_bytes(image_bytes)
                    local_message: list[dict[str, Any]] = [
                        {
                            "type": "image",
                            "data": {"file": temporary_image.resolve().as_uri()},
                        }
                    ]
                    if text:
                        local_message.append({"type": "text", "data": {"text": text}})
                    response = await bot.call_action(
                        action,
                        **recipient,
                        message=local_message,
                        **routing_params,
                    )
                except Exception:
                    logger.warning(
                        "[CrimsonCosmos] Local image fallback failed", exc_info=True
                    )
                finally:
                    if temporary_image is not None:
                        temporary_image.unlink(missing_ok=True)
            if response is not None:
                logger.info("[CrimsonCosmos] Image sent through local file fallback")
            else:
                logger.warning(
                    "[CrimsonCosmos] Auto-recall image send failed", exc_info=True
                )
        if response is None:
            if failure_message:
                await self._send_onebot_segments(
                    event,
                    [{"type": "text", "data": {"text": failure_message}}],
                    as_forward=bool(self._config.get("use_forward", False)),
                )
                return False
            return None

        if not self._config.get("auto_recall", False):
            return True

        message_id = response.get("message_id") if isinstance(response, dict) else None
        if message_id is None:
            logger.warning("[CrimsonCosmos] Auto-recall message ID is unavailable")
            return True

        delay = self._recall_delay_seconds()
        self._schedule_recall(
            bot,
            {
                "message_id": message_id,
                "due_at": time.time() + delay,
                "routing_params": routing_params,
            },
        )
        return True

    def _recall_delay_seconds(self) -> float:
        """返回自动撤回延迟（秒），限制在 2 分钟（120 秒）以内。"""
        try:
            return max(
                0.0,
                min(120.0, float(self._config.get("recall_delay_seconds", 60))),
            )
        except (TypeError, ValueError):
            return 60.0

    def _schedule_recall(self, bot: Any, record: dict[str, Any]) -> None:
        """Persist and schedule one OneBot recall record.

        Args:
            bot: OneBot client used to send the image.
            record: Serializable recall task data.
        """
        self._ensure_recall_state()
        recall_id = self._recall_id(record)
        records = self._load_recall_records()
        if recall_id not in {self._recall_id(item) for item in records}:
            records.append(record)
            self._write_recall_records(records)
        if recall_id in self._active_recall_ids:
            return
        self._active_recall_ids.add(recall_id)
        task = asyncio.create_task(self._recall_onebot_message(bot, record))
        self._recall_tasks.add(task)
        task.add_done_callback(self._recall_tasks.discard)

    async def _resume_recall_tasks(self, event: AstrMessageEvent) -> None:
        """Resume persisted recall tasks when a OneBot client is available.

        Args:
            event: Current event that may expose a OneBot client.
        """
        bot = getattr(event, "bot", None)
        if bot is None or not callable(getattr(bot, "call_action", None)):
            return
        self._ensure_recall_state()
        for record in self._load_recall_records():
            self._schedule_recall(bot, record)

    async def _recall_onebot_message(self, bot: Any, record: dict[str, Any]) -> None:
        """Recall a sent OneBot message at its persisted due time.

        Args:
            bot: OneBot client used to recall the image.
            record: Persisted recall task data.

        Returns:
            None.
        """
        recall_id = self._recall_id(record)
        try:
            await asyncio.sleep(max(0.0, float(record.get("due_at", 0)) - time.time()))
            await bot.call_action(
                "delete_msg",
                message_id=record.get("message_id"),
                **record.get("routing_params", {}),
            )
            self._write_recall_records(
                [
                    item
                    for item in self._load_recall_records()
                    if self._recall_id(item) != recall_id
                ]
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("[CrimsonCosmos] Auto-recall failed", exc_info=True)
        finally:
            self._active_recall_ids.discard(recall_id)

    def _ensure_recall_state(self) -> None:
        """Initialize recall state for normal and test-created instances."""
        if not hasattr(self, "_recall_tasks_path"):
            self._recall_tasks_path = (
                Path(get_astrbot_plugin_data_path())
                / "astrbot_plugin_crimson_cosmos"
                / "recall_tasks.json"
            )
        if not hasattr(self, "_recall_tasks"):
            self._recall_tasks = set()
        if not hasattr(self, "_active_recall_ids"):
            self._active_recall_ids = set()

    def _load_recall_records(self) -> list[dict[str, Any]]:
        """Load valid recall records from disk.

        Returns:
            Valid persisted recall records.
        """
        try:
            payload = json.loads(self._recall_tasks_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []
        return (
            [item for item in payload if isinstance(item, dict)]
            if isinstance(payload, list)
            else []
        )

    def _write_recall_records(self, records: list[dict[str, Any]]) -> None:
        """Atomically persist recall records.

        Args:
            records: Serializable recall records to store.
        """
        self._recall_tasks_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self._recall_tasks_path.with_suffix(".tmp")
        temporary_path.write_text(
            json.dumps(records, ensure_ascii=False), encoding="utf-8"
        )
        temporary_path.replace(self._recall_tasks_path)

    @staticmethod
    def _recall_id(record: dict[str, Any]) -> str:
        """Return a stable identifier for a recall record.

        Args:
            record: Persisted recall task data.

        Returns:
            Stable task identifier.
        """
        routing = record.get("routing_params", {})
        return f"{routing.get('self_id', '')}:{record.get('message_id', '')}"

    @staticmethod
    def _parse_jable_request(
        message: str,
    ) -> tuple[str, int | tuple[int, int], str] | None:
        """Parse a supported AV command into a Jable URL and rank.

        Args:
            message: Raw message text beginning with ``/av``.

        Returns:
            Jable URL, one-based rank, and display list name; otherwise ``None``.
        """
        parts = message.strip().split()
        if len(parts) < 3 or parts[0].lower() != "/av":
            return None
        rank_token = parts[-1]
        range_match = re.fullmatch(r"(\d+)-(\d+)", rank_token)
        if range_match:
            start, end = (int(value) for value in range_match.groups())
            if not 1 <= start <= end <= 30 or end - start + 1 > 10:
                return None
            rank: int | tuple[int, int] = (start, end)
        else:
            try:
                rank = int(rank_token)
            except ValueError:
                return None
            if not 1 <= rank <= 30:
                return None

        query_type = parts[1]
        if query_type == "热门" and len(parts) == 4:
            periods = {
                "今日": ("video_viewed_today", "今日热门"),
                "本周": ("video_viewed_week", "本周热门"),
                "本月": ("video_viewed_month", "本月热门"),
                "全部": ("video_viewed", "全部热门"),
                "所有时间": ("video_viewed", "全部热门"),
            }
            period = periods.get(parts[2])
            if period:
                return f"https://jable.tv/hot/?sort_by={period[0]}", rank, period[1]
        elif query_type == "新片" and len(parts) == 3:
            return "https://jable.tv/latest-updates/", rank, "新片"
        elif query_type == "主题" and len(parts) >= 4:
            sort_modes = {
                "近期最佳": "post_date_and_popularity",
                "最近更新": "post_date",
                "最多观看": "video_viewed",
                "最高收藏": "most_favourited",
            }
            sort_label = parts[-2] if parts[-2] in sort_modes else ""
            name = " ".join(parts[2:-2] if sort_label else parts[2:-1]).strip()
            known_tags = {
                "黑丝": "black-pantyhose",
                "黑絲": "black-pantyhose",
                "丝袜": "pantyhose",
                "絲襪": "pantyhose",
            }
            target = (
                f"https://jable.tv/tags/{known_tags[name]}/"
                if name in known_tags
                else f"theme:{name}"
            )
            if sort_label:
                if target.startswith("theme:"):
                    target += f"|{sort_modes[sort_label]}"
                else:
                    target += f"?sort_by={sort_modes[sort_label]}"
            return (
                target,
                rank,
                f"主题 {name}{' · ' + sort_label if sort_label else ''}",
            )
        elif query_type == "女优" and len(parts) >= 4:
            sort_modes = {
                "近期最佳": "post_date_and_popularity",
                "最近更新": "post_date",
                "最多观看": "video_viewed",
                "最高收藏": "most_favourited",
            }
            sort_label = parts[-2] if parts[-2] in sort_modes else ""
            name = " ".join(parts[2:-2] if sort_label else parts[2:-1]).strip()
            sort_query = f"&sort_by={sort_modes[sort_label]}" if sort_label else ""
            return (
                f"https://jable.tv/search/?q={quote(name)}{sort_query}",
                rank,
                f"女优 {name}{' · ' + sort_label if sort_label else ''}",
            )
        return None

    async def _read_jina_text(
        self, url: str, timeout: aiohttp.ClientTimeout, attempts: int = 3
    ) -> str:
        """Read a Jina page with bounded retries for temporary failures.

        Args:
            url: Full Jina Reader URL.
            timeout: Per-attempt HTTP timeout.
            attempts: Maximum number of attempts.

        Returns:
            Markdown response text.

        Raises:
            aiohttp.ClientError: If all attempts fail or the error is permanent.
            asyncio.TimeoutError: If all attempts time out.
        """
        headers: dict[str, str] = {}
        api_key = str(self._config.get("jina_api_key", "") or "").strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        for attempt in range(attempts):
            try:
                async with self._session.get(
                    url, headers=headers, timeout=timeout
                ) as response:
                    response.raise_for_status()
                    return await response.text()
            except aiohttp.ClientResponseError as error:
                if (
                    error.status not in {429, 500, 502, 503, 504}
                    or attempt == attempts - 1
                ):
                    raise
                delay = 4 * (2**attempt) if error.status == 429 else 2**attempt
                await asyncio.sleep(delay)
                continue
            except asyncio.TimeoutError:
                if attempt == attempts - 1:
                    raise
            await asyncio.sleep(2**attempt)

    async def _read_cached_jable_listing(
        self,
        url: str,
        timeout: aiohttp.ClientTimeout,
        cache: dict[str, str | asyncio.Task[str]],
    ) -> str:
        """Share an in-flight Jable listing request across concurrent ranks."""
        cached = cache.get(url)
        if isinstance(cached, str):
            return cached
        if cached is None:
            cached = asyncio.create_task(self._read_jina_text(url, timeout))
            cache[url] = cached
        try:
            listing = await cached
        except asyncio.CancelledError:
            if cache.get(url) is cached:
                cache.pop(url, None)
            raise
        except Exception:
            if cache.get(url) is cached:
                cache.pop(url, None)
            raise
        cache[url] = listing
        return listing

    async def _read_missav_html(self, url: str) -> str:
        """Read one public MissAV page with bounded retries.

        Args:
            url: Validated MissAV page URL.

        Returns:
            Raw HTML response text.

        Raises:
            aiohttp.ClientError: If both HTTP attempts fail.
            asyncio.TimeoutError: If both attempts time out.
            ValueError: If MissAV returns an empty or challenge page.
        """
        if self._session is None or self._session.closed:
            self._session = self._make_session()
        last_error: Exception = ValueError("MissAV 返回空页面。")
        for attempt in range(2):
            try:
                async with self._session.get(
                    url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 Chrome/140 Safari/537.36"
                        ),
                        "Accept-Language": "zh-CN,zh;q=0.9",
                    },
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response:
                    response.raise_for_status()
                    page = await response.text()
                if not page.strip() or "challenge-platform" in page:
                    raise ValueError("MissAV 暂时限制访问。")
                return page
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
                last_error = error
                if attempt == 0:
                    await asyncio.sleep(1)
        try:
            return await self._read_jina_text(
                f"https://r.jina.ai/{url}", aiohttp.ClientTimeout(total=30)
            )
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            raise last_error

    async def _fetch_missav_video(
        self,
        target: str,
        rank: int = 1,
        *,
        prefer_exact: bool = False,
        include_magnets: bool = False,
    ) -> dict[str, Any]:
        """Resolve one MissAV result and parse its public metadata.

        Args:
            target: Search keyword or validated MissAV detail URL.
            rank: One-based search result rank.
            prefer_exact: Prefer an exact non-leak slug for code lookups.
            include_magnets: Parse and require magnet links from the detail page.

        Returns:
            Video metadata and optional magnet links.

        Raises:
            ValueError: If no result, metadata, cover, or requested magnet is found.
            aiohttp.ClientError: If MissAV cannot be fetched.
            asyncio.TimeoutError: If MissAV times out.
        """
        if target.startswith(("http://", "https://")):
            parsed = urlsplit(target)
            if (
                parsed.scheme != "https"
                or parsed.hostname != "missav.ws"
                or re.fullmatch(r"/(?:dm\d+/)?cn/[^/]+/?", parsed.path) is None
            ):
                raise ValueError("MissAV 详情链接无效。")
            detail_url = urlunsplit(
                ("https", "missav.ws", parsed.path.rstrip("/"), "", "")
            )
        else:
            search_url = f"https://missav.ws/cn/search/{quote(target, safe='')}"
            search_page = await self._read_missav_html(search_url)
            result_pattern = re.compile(
                r"href=['\"](?P<url>https://missav\.ws/(?:dm\d+/)?cn/"
                r"(?P<slug>[^'\"/?#]+))['\"][^>]*\balt=['\"][^'\"]*['\"]",
                re.IGNORECASE,
            )
            results = list(
                dict.fromkeys(
                    (match.group("url"), match.group("slug").lower())
                    for match in result_pattern.finditer(search_page)
                )
            )
            if not results:
                search_section = search_page
                result_heading = re.search(
                    r"^# .+的搜寻结果\s*$", search_section, re.MULTILINE
                )
                if result_heading:
                    search_section = search_section[result_heading.end() :]
                if "[返回最顶]" in search_section:
                    search_section = search_section.split("[返回最顶]", 1)[0]
                markdown_pattern = re.compile(
                    r"\[[^\]\n]+\]\((?P<url>https://missav\.ws/"
                    r"(?:dm\d+/cn|cn)/(?P<slug>[^/?#)]+))\)",
                    re.IGNORECASE,
                )
                results = list(
                    dict.fromkeys(
                        (match.group("url"), match.group("slug").lower())
                        for match in markdown_pattern.finditer(search_section)
                    )
                )
            if not results:
                raise ValueError("MissAV 未找到影片。")
            normalized_target = target.strip().lower()
            exact_url = next(
                (url for url, slug in results if slug == normalized_target), None
            )
            if prefer_exact and exact_url:
                detail_url = exact_url
            elif rank > len(results):
                raise ValueError("MissAV 未返回该排名的影片。")
            else:
                detail_url = results[rank - 1][0]

        detail_page = await self._read_missav_html(detail_url)
        title_match = re.search(
            r"<meta[^>]+property=['\"]og:title['\"][^>]+content=['\"]([^'\"]+)",
            detail_page,
            re.IGNORECASE,
        )
        cover_match = re.search(
            r"<meta[^>]+property=['\"]og:image['\"][^>]+content=['\"]([^'\"]+)",
            detail_page,
            re.IGNORECASE,
        )
        slug = urlsplit(detail_url).path.rstrip("/").rsplit("/", 1)[-1]
        if title_match:
            title = re.sub(
                r"\s+-\s+MissAV.*$",
                "",
                unescape(title_match.group(1)).strip(),
                flags=re.IGNORECASE,
            )
        else:
            markdown_title = re.search(r"^Title:\s*(.+)$", detail_page, re.MULTILINE)
            title = markdown_title.group(1).strip() if markdown_title else ""
        cover = (
            unescape(cover_match.group(1)).strip()
            if cover_match
            else f"https://fourhoi.com/{slug}/cover-n.jpg"
        )
        if not title or not cover.startswith(("http://", "https://")):
            raise ValueError("MissAV 影片详情不完整。")
        code = re.sub(
            r"-(?:uncensored-leak|chinese-subtitle)$",
            "",
            slug,
            flags=re.IGNORECASE,
        ).upper()
        magnets = list(
            dict.fromkeys(
                unescape(value)
                for value in [
                    *re.findall(
                        r"href=['\"](magnet:\?xt=urn:btih:[^'\"]+)['\"]",
                        detail_page,
                        re.IGNORECASE,
                    ),
                    *re.findall(
                        r"\((magnet:\?xt=urn:btih:[^)]+)\)",
                        detail_page,
                        re.IGNORECASE,
                    ),
                ]
            )
        )[:MAX_MISSAV_MAGNETS]
        if include_magnets and not magnets:
            raise ValueError("MissAV 页面没有可用磁力。")
        return {
            "cover": cover,
            "code": code,
            "title": title,
            "url": detail_url,
            "magnets": magnets,
        }

    async def _fetch_jable_video(
        self,
        request: tuple[str, int, str]
        | tuple[str, int, str, dict[str, str | asyncio.Task[str]]],
    ) -> dict[str, Any]:
        """Fetch one ranked Jable video through text-reader endpoints.

        Args:
            request: Parsed Jable URL, one-based rank, and display list name.

        Returns:
            Parsed video metadata used by the message report.

        Raises:
            ValueError: If the requested theme or ranked video cannot be parsed.
        """
        if self._session is None or self._session.closed:
            self._session = self._make_session()
        target_url, rank, list_name = request[:3]
        listing_cache = request[3] if len(request) == 4 else {}
        timeout = aiohttp.ClientTimeout(total=30)
        if target_url.startswith("theme:"):
            theme_request = target_url.removeprefix("theme:")
            theme_name, _, sort_value = theme_request.partition("|")
            categories = await self._read_jina_text(
                "https://r.jina.ai/https://jable.tv/categories/", timeout
            )
            theme_match = re.search(
                rf"\[{re.escape(theme_name)}\]\((https://jable\.tv/(?:tags|categories)/[^)]+/)\)",
                categories,
            )
            if not theme_match:
                raise ValueError("Jable 未找到该主题。")
            target_url = theme_match.group(1)
            if sort_value:
                target_url += f"?sort_by={sort_value}"

        listing_url = f"https://r.jina.ai/{target_url}"
        listing = await self._read_cached_jable_listing(
            listing_url, timeout, listing_cache
        )
        card_pattern = re.compile(
            r"\[!\[Image[^]]*]\((?P<cover>https://[^)]+)\)[^]]*]\("
            r"(?P<url>https://jable\.tv/(?:s0/)?videos/[^)]+/)\)\s*"
            r"###### \[(?P<title>.+?)]\([^)]+\)\s*(?P<metrics>[\d ]+)",
            re.DOTALL,
        )
        cards = list(card_pattern.finditer(listing))
        selected_index = rank - 1
        if selected_index >= len(cards):
            separator = "&" if "?" in target_url else "?"
            page_parameter = "from_videos=2" if "/search/" in target_url else "from=2"
            next_page_url = f"https://r.jina.ai/{target_url}{separator}{page_parameter}"
            next_page = await self._read_cached_jable_listing(
                next_page_url, timeout, listing_cache
            )
            selected_index -= len(cards)
            cards = list(card_pattern.finditer(next_page))
        if selected_index < 0 or selected_index >= len(cards):
            raise ValueError("Jable 未返回该排名的影片。")

        card = cards[selected_index]
        video_url = card.group("url").replace("/s0/videos/", "/videos/")
        code = video_url.rstrip("/").rsplit("/", 1)[-1].upper()
        title = re.sub(
            rf"^{re.escape(code)}\s*",
            "",
            card.group("title").strip(),
            flags=re.I,
        )
        stars = card.group("metrics").split()[-1]
        tags: list[str] = []
        if self._config.get("jable_show_themes", True):
            try:
                detail_timeout = aiohttp.ClientTimeout(total=10)
                detail = await self._read_jina_text(
                    f"https://r.jina.ai/{video_url}", detail_timeout, attempts=2
                )
                tags = list(
                    dict.fromkeys(
                        re.findall(
                            r"\[([^]]+)]\(https://jable\.tv/(?:categories|tags)/",
                            detail,
                        )
                    )
                )
            except (aiohttp.ClientError, asyncio.TimeoutError):
                logger.warning(
                    "[CrimsonCosmos] Jable detail unavailable: %s", video_url
                )
                tags = ["详情暂不可用"]
        cover = card.group("cover")
        if "placeholder" in cover:
            async with self._session.get(
                "https://api.microlink.io/",
                params={"url": video_url},
                timeout=timeout,
            ) as response:
                response.raise_for_status()
                metadata: Any = await response.json(content_type=None)
            image = (
                metadata.get("data", {}).get("image", {})
                if isinstance(metadata, dict)
                else {}
            )
            if isinstance(image, dict) and isinstance(image.get("url"), str):
                cover = image["url"]
        if not cover.startswith(("http://", "https://")):
            raise ValueError("Jable 影片详情不完整。")
        return {
            "cover": cover,
            "code": code,
            "title": title,
            "stars": stars,
            "tags": tags,
            "url": video_url,
            "rank": rank,
            "list_name": list_name,
        }

    def _get_requested_image_count(self, message: str) -> int | None:
        """Resolve a keyword match and optional requested image count.

        Args:
            message: Incoming plain-text message.

        Returns:
            The requested image count, or ``None`` when no keyword matches.
        """
        request = self._parse_image_request(message)
        return request[0] if request else None

    def _parse_image_request(self, message: str) -> tuple[int, list[str]] | None:
        """Resolve requested count and message-provided tags.

        Args:
            message: Incoming plain-text message.

        Returns:
            Requested image count and tags, or ``None`` when unmatched.
        """
        keywords = self._config.get("keywords", [])
        if isinstance(keywords, str):
            keywords = [keywords]
        if not isinstance(keywords, list):
            return None

        match_mode = str(self._config.get("keyword_match_mode") or "exact").lower()
        if match_mode not in {"exact", "prefix", "contains"}:
            match_mode = "exact"
        for keyword in keywords:
            keyword = str(keyword).strip()
            if not keyword:
                continue
            keyword_index = message.find(keyword)
            if keyword_index < 0:
                continue
            if match_mode == "prefix" and not message.startswith(keyword):
                continue

            before = message[:keyword_index]
            after = message[keyword_index + len(keyword) :]
            request_match = re.match(
                r"^\s*(?:(?:请|麻烦|劳驾)\s*)?"
                r"(?:(?:(?:给我|帮我)\s*(?:来|发|要|整)?|发我|来|发|要|整)\s*)?"
                r"(?P<count>[1-9]\d*|[一二两三四五六七八九十])?\s*"
                r"(?:张|份|个)?\s*",
                before,
            )
            count_text = request_match.group("count") if request_match else None
            before_tags = before[request_match.end() :] if request_match else before
            if match_mode == "exact" and not (
                message == keyword
                or message.startswith(f"{keyword} ")
                or bool(count_text)
                or (message.endswith(keyword) and bool(before_tags.strip()))
            ):
                continue
            count = 1
            if count_text:
                count = (
                    int(count_text)
                    if count_text.isdigit()
                    else CHINESE_IMAGE_COUNTS[count_text]
                )
            tag_text = " ".join(
                part for part in (before_tags.strip(), after.strip()) if part
            )
            tags = [tag for tag in re.split(r"[\s,，、]+", tag_text) if tag]
            return count, tags
        return None

    async def _fetch_wallhaven_images(
        self, count: int, message_tags: list[str] | None = None
    ) -> list[str]:
        """Fetch wallpapers from the selected Wallhaven filters.

        Args:
            count: Number of image URLs to return.

        Returns:
            Valid remote image URLs returned by Wallhaven.

        Raises:
            ValueError: If Wallhaven is not configured or returns no usable image.
        """
        api_key = str(self._config.get("wallhaven_api_key") or "").strip()
        if not api_key:
            raise ValueError("请先配置 Wallhaven API Key。")

        categories = self._config.get("wallhaven_categories", [])
        if isinstance(categories, str):
            categories = [categories]
        category_aliases = {"通用": "general", "动漫": "anime", "人物": "people"}
        selected_categories = {
            category_aliases.get(str(category).strip(), str(category).strip().lower())
            for category in categories
            if isinstance(category, str)
        }
        category_mask = "".join(
            "1" if category in selected_categories else "0"
            for category in ("general", "anime", "people")
        )
        if category_mask == "000":
            raise ValueError("请至少选择一个 Wallhaven 图片分类。")
        purity = self._config.get("wallhaven_purity", ["成人"])
        if isinstance(purity, str):
            purity = [purity]
        purity_aliases = {"全年龄": "sfw", "擦边": "sketchy", "成人": "nsfw"}
        selected_purity = {
            purity_aliases.get(str(rating).strip(), str(rating).strip().lower())
            for rating in purity
            if isinstance(rating, str)
        }
        purity_mask = "".join(
            "1" if rating in selected_purity else "0"
            for rating in ("sfw", "sketchy", "nsfw")
        )
        if purity_mask == "000":
            raise ValueError("请至少选择一个 Wallhaven 内容分级。")
        tags = self._config.get("wallhaven_tags", [])
        if isinstance(tags, str):
            tags = [tags]
        configured_tags = [
            tag.strip() for tag in tags if isinstance(tag, str) and tag.strip()
        ]
        tag_query = " ".join([*configured_tags, *(message_tags or [])])
        if self._session is None or self._session.closed:
            self._session = self._make_session()

        sorting_modes = {
            "最新": ("date_added", None),
            "热门": ("toplist", "1d"),
            "榜单": ("toplist", "1M"),
            "random": ("random", None),
        }
        sorting, top_range = sorting_modes.get(
            str(self._config.get("wallhaven_sorting", "random") or "random").strip(),
            ("random", None),
        )
        params = {
            "apikey": api_key,
            "categories": category_mask,
            "purity": purity_mask,
            "sorting": sorting,
        }
        if top_range:
            params["topRange"] = top_range
        if tag_query:
            params["q"] = tag_query
        async with self._session.get(
            "https://wallhaven.cc/api/v1/search",
            params=params,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as response:
            response.raise_for_status()
            payload: Any = await response.json(content_type=None)

        images = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(images, list):
            raise ValueError("Wallhaven 未返回可用图片。")
        candidate_urls = list(
            dict.fromkeys(
                image.get("path")
                for image in images
                if isinstance(image, dict)
                and isinstance(image.get("path"), str)
                and image["path"].startswith(("http://", "https://"))
            )
        )
        if len(candidate_urls) < count:
            raise ValueError("Wallhaven 未返回足够的可用图片。")

        cursors = getattr(self, "_wallhaven_cursors", None)
        if not isinstance(cursors, dict):
            cursors = {}
            self._wallhaven_cursors = cursors
        cursor_key = (category_mask, purity_mask, sorting, top_range, tag_query)
        start = cursors.get(cursor_key, 0) % len(candidate_urls)
        image_urls = [
            candidate_urls[(start + index) % len(candidate_urls)]
            for index in range(count)
        ]
        cursors[cursor_key] = start + count
        return image_urls

    def _resolve_lolicon_tag_groups(
        self, message_tags: list[str] | None
    ) -> list[list[str]] | None:
        """把用户标签展开成 Lolicon 的 ``tag`` 结构（组内 AND、组间 OR）。

        内置同义词表和用户别名会为每个标签产生一个或多个候选；多个标签的
        候选做笛卡尔积，超出 ``MAX_LOLICON_TAG_GROUPS`` 时截断。

        Args:
            message_tags: 从消息解析出的标签。

        Returns:
            Lolicon ``tag`` 参数值；无标签时返回 ``None``。
        """
        merged: dict[str, tuple[str, ...]] = dict(LOLICON_TAG_ALIASES)
        raw_aliases = str(self._config.get("lolicon_tag_aliases", "") or "")
        for pair in re.split(r"[,，\n]+", raw_aliases):
            if "=" not in pair:
                continue
            alias, target = (part.strip() for part in pair.split("=", 1))
            if alias and target:
                merged[alias] = (target,)

        ascii_lookup = {
            key.lower(): value for key, value in merged.items() if key.isascii()
        }
        candidates_per_tag: list[list[str]] = []
        for raw_tag in message_tags or []:
            tag = str(raw_tag).strip()
            if not tag:
                continue
            if tag in merged:
                candidates = list(merged[tag])
            elif tag.lower() in ascii_lookup:
                candidates = list(ascii_lookup[tag.lower()])
            else:
                candidates = [tag]
            candidates = [
                candidate for candidate in dict.fromkeys(candidates) if candidate
            ]
            if candidates:
                candidates_per_tag.append(candidates)
        if not candidates_per_tag:
            return None
        return self._cartesian_tag_groups(candidates_per_tag)

    @staticmethod
    def _cartesian_tag_groups(
        candidates_per_tag: list[list[str]],
    ) -> list[list[str]]:
        """对每个标签的候选做笛卡尔积，并在组数上限内截断。"""
        groups: list[list[str]] = []
        for combo in product(*candidates_per_tag):
            groups.append(list(combo))
            if len(groups) >= MAX_LOLICON_TAG_GROUPS:
                break
        return groups

    async def _request_lolicon_data(self, payload: dict[str, Any]) -> list[Any]:
        """发送一次 Lolicon 请求并返回其 ``data`` 列表。"""
        if self._session is None or self._session.closed:
            self._session = self._make_session()
        async with self._session.post(
            "https://api.lolicon.app/setu/v2",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            response.raise_for_status()
            body: Any = await response.json(content_type=None)
        if not isinstance(body, dict):
            raise ValueError("Lolicon API 返回格式异常。")
        if body.get("error"):
            raise ValueError("Lolicon API 请求失败。")
        images = body.get("data")
        if not isinstance(images, list):
            raise ValueError("Lolicon API 未返回图片。")
        return images

    @staticmethod
    def _image_suffix(leading: bytes) -> str:
        """按文件头推断图片扩展名，无法识别时默认按 JPEG 处理。"""
        if leading.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if leading.startswith((b"GIF87a", b"GIF89a")):
            return ".gif"
        if leading.startswith(b"RIFF") and leading[8:12] == b"WEBP":
            return ".webp"
        return ".jpg"

    async def _fetch_lolicon_images(
        self, count: int, message_tags: list[str] | None = None
    ) -> tuple[list[str], list[str]]:
        """Fetch filtered Pixiv images through the Lolicon API.

        图片经反代下载后落盘到 AstrBot 临时目录，返回本地文件路径，避免把
        全部图片字节长期留在内存里。

        Args:
            count: Number of images to return.
            message_tags: Tags parsed from the triggering message.

        Returns:
            Local image file paths and their unique Pixiv IDs.

        Raises:
            ValueError: If Lolicon rejects the request or returns invalid data.
        """
        r18_modes = {"sfw": 0, "r18": 1, "mix": 2}
        r18 = r18_modes.get(str(self._config.get("lolicon_r18_mode", "r18")).lower(), 1)
        size = str(self._config.get("lolicon_image_size", "small")).lower()
        valid_sizes = {"original", "regular", "small", "thumb", "mini"}
        if size not in valid_sizes:
            size = "small"

        tag_groups = self._resolve_lolicon_tag_groups(message_tags)

        def build_payload(with_tags: bool) -> dict[str, Any]:
            payload: dict[str, Any] = {
                "r18": r18,
                "num": count,
                "excludeAI": bool(self._config.get("lolicon_exclude_ai", True)),
                "size": [size],
            }
            if with_tags and tag_groups:
                payload["tag"] = tag_groups
            aspect_ratio = str(
                self._config.get("lolicon_aspect_ratio", "") or ""
            ).strip()
            if aspect_ratio in {"gt1", "lt1", "eq1"}:
                payload["aspectRatio"] = aspect_ratio
            proxy = str(self._config.get("lolicon_proxy", "") or "").strip()
            if proxy:
                payload["proxy"] = proxy
            return payload

        images = await self._request_lolicon_data(build_payload(True))
        if len(images) < count and tag_groups:
            logger.warning("[CrimsonCosmos] Lolicon 标签无足够结果，回退为无标签请求")
            images = await self._request_lolicon_data(build_payload(False))

        async def fetch_image(image: Any) -> tuple[str, str] | None:
            """下载单张 Lolicon 图片到磁盘，返回 ``(本地路径, pid)``。"""
            if not isinstance(image, dict) or not isinstance(image.get("urls"), dict):
                return None
            image_urls = image["urls"]
            size_candidates = [size, "original", "regular", "small", "thumb", "mini"]
            image_url = next(
                (
                    image_urls.get(candidate)
                    for candidate in dict.fromkeys(size_candidates)
                    if isinstance(image_urls.get(candidate), str)
                    and image_urls[candidate].startswith(("http://", "https://"))
                ),
                None,
            )
            if not image_url:
                return None
            pid = str(image.get("pid", "")).strip()
            raw_proxy_order = self._config.get("lolicon_proxy_order", [])
            proxy_order = raw_proxy_order if isinstance(raw_proxy_order, list) else []
            legacy_proxy = str(self._config.get("lolicon_proxy", "") or "").strip()
            if legacy_proxy:
                proxy_order = [legacy_proxy, *proxy_order]
            parsed_url = urlsplit(image_url)
            proxy_urls: list[str] = []
            page_match = re.search(r"_p(\d+)", parsed_url.path)
            pixiv_hosts = {
                "i.pximg.net",
                "i.loli.best",
                "pixiv.cat",
                "i.pixiv.nl",
                "i.pixiv.re",
            }
            proxy_candidates = proxy_order if parsed_url.hostname in pixiv_hosts else []
            for proxy in dict.fromkeys(
                str(item).strip().rstrip("/") for item in proxy_candidates
            ):
                if not proxy.startswith(("http://", "https://")):
                    continue
                if urlsplit(proxy).hostname == "pixiv.cat" and pid.isdigit():
                    page = int(page_match.group(1)) + 1 if page_match else 1
                    suffix = f"-{page}" if page > 1 else ""
                    proxy_urls.append(f"{proxy}/{pid}{suffix}.jpg")
                else:
                    proxy_host = urlsplit(proxy)
                    proxy_urls.append(
                        urlunsplit(
                            (
                                proxy_host.scheme,
                                proxy_host.netloc,
                                parsed_url.path,
                                parsed_url.query,
                                "",
                            )
                        )
                    )
            if image_url not in proxy_urls:
                proxy_urls.append(image_url)

            timeout_seconds = max(
                1,
                min(
                    int(self._config.get("lolicon_proxy_timeout_seconds", 30) or 30),
                    60,
                ),
            )
            download_headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/140 Safari/537.36"
                ),
                "Referer": "https://www.pixiv.net/",
            }
            temporary_dir = Path(get_astrbot_temp_path())
            temporary_dir.mkdir(parents=True, exist_ok=True)
            for index, proxy_url in enumerate(proxy_urls):
                temporary_path = temporary_dir / (
                    f"crimson_cosmos_lolicon_{time.time_ns()}.part"
                )
                try:
                    async with self._session.get(
                        proxy_url,
                        headers=download_headers,
                        timeout=aiohttp.ClientTimeout(total=timeout_seconds),
                    ) as response:
                        response.raise_for_status()
                        leading = bytearray()
                        total = 0
                        try:
                            with temporary_path.open("wb") as handle:
                                async for chunk in response.content.iter_chunked(
                                    64 * 1024
                                ):
                                    total += len(chunk)
                                    if total > 20 * 1024 * 1024:
                                        raise ValueError("Pixiv 图片超过 20 MiB 限制。")
                                    if len(leading) < 12:
                                        leading.extend(chunk[: 12 - len(leading)])
                                    handle.write(chunk)
                        except BaseException:
                            temporary_path.unlink(missing_ok=True)
                            raise
                    if total == 0:
                        temporary_path.unlink(missing_ok=True)
                        raise ValueError("Pixiv 图片返回空内容。")
                except (
                    aiohttp.ClientError,
                    asyncio.TimeoutError,
                    ValueError,
                    OSError,
                ):
                    logger.warning("Pixiv image download failed: %s", proxy_url)
                    if index + 1 < len(proxy_urls):
                        await asyncio.sleep(0.4 * (index + 1))
                    continue
                final_path = temporary_path.with_suffix(
                    self._image_suffix(bytes(leading))
                )
                temporary_path.replace(final_path)
                return (str(final_path.resolve()), pid if pid.isdigit() else "")
            return None

        results = await asyncio.gather(*(fetch_image(image) for image in images))
        paths: list[str] = []
        pids: list[str] = []
        for result in results:
            if result is None:
                continue
            path, pid = result
            paths.append(path)
            if pid and pid not in pids:
                pids.append(pid)
        if len(paths) != count:
            raise ValueError("Lolicon API 未返回足够的可用图片。")
        return paths, pids
