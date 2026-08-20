"""Serve configured adult images, Jable reports, and JM albums."""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
import re
import shutil
import time
import zipfile
from collections.abc import AsyncGenerator
from html import unescape
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
    "admin_user_ids": [],
    "keywords": ["色图"],
    "keyword_match_mode": "exact",
    "block_other_handlers": True,
    "cooldown_seconds": 0,
    "image_source": "custom",
    "image_source_order": [],
    "request_retry_count": 3,
    "custom_api_url": "",
    "custom_api_image_url_path": "url",
    "custom_api_tag_parameter": "tag",
    "waifu_im_nsfw_mode": "r18",
    "waifu_im_excluded_tags": [],
    "waifu_im_orientation": "",
    "nekos_api_rating": "露骨",
    "lolicon_r18_mode": "r18",
    "lolicon_exclude_ai": True,
    "lolicon_aspect_ratio": "",
    "lolicon_image_size": "small",
    "lolicon_proxy": "",
    "lolicon_proxy_order": [
        "https://i.loli.best",
        "https://pixiv.cat",
        "https://i.pixiv.nl",
        "https://i.pixiv.re",
    ],
    "lolicon_proxy_timeout_seconds": 8,
    "lolicon_tag_aliases": "",
    "show_pixiv_pid": False,
    "jable_show_cover": True,
    "jable_show_code": True,
    "jable_show_title": True,
    "jable_show_stars": True,
    "jable_show_themes": True,
    "jable_show_detail_link": True,
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
    "auto_recall": False,
    "recall_delay_seconds": 60,
    "multi_image_send_mode": "direct",
    "single_image_forward": False,
    "fetching_message": "正在获取喵~",
    "cooldown_message": "冷却中呢喵~",
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
    "custom_api_settings",
    "wallhaven_settings",
    "waifu_im_settings",
    "nekos_api_settings",
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
        self._cooldown_until: dict[tuple[str, str, str], float] = {}
        self._jm_cooldown_until: dict[tuple[str, str, str], float] = {}
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

    def _is_event_allowed(self, event: AstrMessageEvent) -> bool:
        """Check the plugin's existing private and group access settings.

        Args:
            event: AstrBot message event.

        Returns:
            Whether the current session may use plugin commands.
        """
        if event.is_private_chat():
            return bool(self._config.get("enable_private", False))
        allowed_groups = self._config.get("allowed_group_ids", [])
        return (
            bool(self._config.get("enable_group", False))
            and isinstance(allowed_groups, list)
            and str(event.get_group_id())
            in {str(group_id).strip() for group_id in allowed_groups}
        )

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
            yield event.plain_result("用法：/jm 搜索 <关键词> [页码]")
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
            yield event.plain_result("用法：/jm 详情 <数字ID>")
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
            yield event.plain_result("用法：/jm 热门 [日|周|月] [页码]")
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
            yield event.plain_result("用法：/jm 下载 <数字ID>")
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
                "private"
                if event.is_private_chat()
                else str(event.get_group_id()).strip(),
                str(event.get_sender_id()).strip(),
            )
            now = time.monotonic()
            if now < cooldowns.get(cooldown_key, 0.0):
                cooldown_message = str(
                    self._config.get("cooldown_message", "") or ""
                ).strip()
                if cooldown_message:
                    yield event.plain_result(cooldown_message)
                if self._config.get("block_other_handlers", True):
                    event.stop_event()
                return
            cooldowns[cooldown_key] = now + cooldown_seconds
        fetching_message = str(self._config.get("fetching_message", "") or "").strip()
        if fetching_message:
            yield event.plain_result(fetching_message)

        try:
            result = await asyncio.to_thread(self._execute_jm_action, action, *args)
        except RuntimeError as error:
            yield event.plain_result(str(error))
            if self._config.get("block_other_handlers", True):
                event.stop_event()
            return
        except Exception:
            logger.warning(
                "[CrimsonCosmos] JM action failed: %s", action, exc_info=True
            )
            yield event.plain_result("JM 获取失败，请检查网络、域名或代理配置。")
            if self._config.get("block_other_handlers", True):
                event.stop_event()
            return

        text = str(result.get("text", ""))
        if image_path := result.get("image"):
            image_file = Path(str(image_path)).resolve()
            encoded_cover = base64.b64encode(image_file.read_bytes()).decode("ascii")
            delivery_status = await self._send_image_with_auto_recall(
                event,
                f"base64://{encoded_cover}",
                text,
                failure_message="JM 获取失败，请稍后重试。",
            )
            if delivery_status is None:
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
            yield event.plain_result(text)

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
        bot = getattr(event, "bot", None)
        if bot is None or not callable(getattr(bot, "call_action", None)):
            return False
        routing_params: dict[str, Any] = {}
        raw_event = getattr(getattr(event, "message_obj", None), "raw_message", None)
        get_raw_value = getattr(raw_event, "get", None)
        if callable(get_raw_value) and (self_id := get_raw_value("self_id")):
            routing_params["self_id"] = self_id
        if event.is_private_chat():
            user_id = str(event.get_sender_id()).strip()
            if not user_id.isdigit():
                return False
            action = "send_private_msg"
            recipient = {"user_id": int(user_id)}
        else:
            group_id = str(event.get_group_id()).strip()
            if not group_id.isdigit():
                return False
            action = "send_group_msg"
            recipient = {"group_id": int(group_id)}
        try:
            response = await bot.call_action(
                action,
                **recipient,
                message=[
                    {"type": "text", "data": {"text": text}},
                    {
                        "type": "file",
                        "data": {"name": file_path.name, "file": str(file_path)},
                    },
                ],
                **routing_params,
            )
        except Exception:
            logger.warning(
                "[CrimsonCosmos] Auto-recall JM file send failed", exc_info=True
            )
            return False
        message_id = response.get("message_id") if isinstance(response, dict) else None
        if self._config.get("auto_recall", False) and message_id is not None:
            try:
                delay = max(0.0, float(self._config.get("recall_delay_seconds", 60)))
            except (TypeError, ValueError):
                delay = 60.0
            self._schedule_recall(
                bot,
                {
                    "message_id": message_id,
                    "due_at": time.time() + delay,
                    "routing_params": routing_params,
                },
            )
        return True

    @filter.command_group("av")
    def av(self):
        """Jable 影片查询指令组。"""
        pass

    @filter.command("helpav")
    async def helpav(self, event: AstrMessageEvent):
        """显示插件所有参考命令。"""
        yield event.plain_result(
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

    async def _handle_jable_command(
        self, event: AstrMessageEvent, message: str
    ) -> AsyncGenerator[Any, None]:
        """Handle a registered AV command with plugin access controls."""
        if event.is_private_chat():
            if not self._config.get("enable_private", False):
                return
        else:
            allowed_groups = self._config.get("allowed_group_ids", [])
            if (
                not self._config.get("enable_group", False)
                or not isinstance(allowed_groups, list)
                or str(event.get_group_id())
                not in {str(group_id).strip() for group_id in allowed_groups}
            ):
                return

        block_other_handlers = self._config.get("block_other_handlers", True)
        jable_request = self._parse_jable_request(message)
        if jable_request is None:
            yield event.plain_result(
                "用法：/av 热门 今日|本周|本月|全部 1-30、"
                "/av 新片 1-30、/av 主题|女优 名称 "
                "[近期最佳|最近更新|最多观看|最高收藏] 1-30"
            )
            if block_other_handlers:
                event.stop_event()
            return

        fetching_message = str(self._config.get("fetching_message", "") or "").strip()
        if fetching_message:
            yield event.plain_result(fetching_message)
        target_url, rank_request, list_name = jable_request
        ranks = (
            list(range(rank_request[0], rank_request[1] + 1))
            if isinstance(rank_request, tuple)
            else [rank_request]
        )
        videos: list[dict[str, Any]] = []
        self._jable_listing_cache: dict[str, str] = {}
        first_result = await asyncio.gather(
            self._fetch_jable_video((target_url, ranks[0], list_name)),
            return_exceptions=True,
        )
        if isinstance(first_result[0], dict):
            videos.append(first_result[0])
        else:
            logger.warning(
                "[CrimsonCosmos] Jable item request failed: %s", first_result[0]
            )
        for offset in range(1, len(ranks), 2):
            results = await asyncio.gather(
                *(
                    self._fetch_jable_video((target_url, rank, list_name))
                    for rank in ranks[offset : offset + 2]
                ),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, dict):
                    videos.append(result)
                else:
                    logger.warning(
                        "[CrimsonCosmos] Jable item request failed: %s", result
                    )
        self._jable_listing_cache.clear()
        if not videos:
            logger.warning("[CrimsonCosmos] Jable request failed", exc_info=True)
            failure_message = str(self._config.get("failure_message", "") or "").strip()
            yield event.plain_result(failure_message or "影片获取失败，请稍后重试。")
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
        sent_as_forward = (
            show_cover
            and len(videos) > 1
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
                        [Image.fromURL(video["cover"]), Plain(report)]
                    )
        elif not show_cover:
            for report in reports:
                yield event.plain_result(report)
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
        keyword = str(keyword).strip()
        try:
            rank = int(rank)
        except (TypeError, ValueError):
            rank = 0
        if not keyword or not 1 <= rank <= 30:
            yield event.plain_result("用法：/av 搜索 <关键词> [1-30]")
            if block_other_handlers:
                event.stop_event()
            return

        fetching_message = str(self._config.get("fetching_message", "") or "").strip()
        if fetching_message:
            yield event.plain_result(fetching_message)
        try:
            video = await self._fetch_missav_video(keyword, rank)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
            logger.warning("[CrimsonCosmos] MissAV search failed: %s", error)
            failure_message = str(self._config.get("failure_message", "") or "").strip()
            yield event.plain_result(failure_message or "影片获取失败，请稍后重试。")
            if block_other_handlers:
                event.stop_event()
            return

        report = (
            f"🎬 MissAV 搜索 第 {rank} 名\n"
            f"车牌号：{video['code']}\n"
            f"标题：{video['title']}\n"
            f"链接：{video['url']}"
        )
        delivery_status = await self._send_image_with_auto_recall(
            event, video["cover"], report
        )
        if delivery_status is None:
            yield event.chain_result([Image.fromURL(video["cover"]), Plain(report)])
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
            yield event.plain_result("用法：/av 磁力 <番号或 MissAV 详情链接>")
            if block_other_handlers:
                event.stop_event()
            return

        fetching_message = str(self._config.get("fetching_message", "") or "").strip()
        if fetching_message:
            yield event.plain_result(fetching_message)
        try:
            video = await self._fetch_missav_video(
                target, prefer_exact=True, include_magnets=True
            )
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
            logger.warning("[CrimsonCosmos] MissAV magnet request failed: %s", error)
            failure_message = str(self._config.get("failure_message", "") or "").strip()
            yield event.plain_result(failure_message or "磁力获取失败，请稍后重试。")
            if block_other_handlers:
                event.stop_event()
            return

        magnets = "\n".join(
            f"{index}. {magnet}"
            for index, magnet in enumerate(video["magnets"], start=1)
        )
        yield event.plain_result(f"🧲 {video['title']}\n{magnets}")
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

        if event.is_private_chat():
            if not self._config.get("enable_private", False):
                return
        else:
            allowed_groups = self._config.get("allowed_group_ids", [])
            if (
                not self._config.get("enable_group", False)
                or not isinstance(allowed_groups, list)
                or str(event.get_group_id())
                not in {str(group_id).strip() for group_id in allowed_groups}
            ):
                return

        block_other_handlers = self._config.get("block_other_handlers", True)
        if message.lower().startswith(("/av", "/jm")):
            return

        request = self._parse_image_request(message)
        if request is None:
            return
        image_count, message_tags = request
        if image_count > MAX_IMAGES_PER_REQUEST:
            yield event.plain_result(f"单次最多获取 {MAX_IMAGES_PER_REQUEST} 张图片。")
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
            sender_id = str(event.get_sender_id()).strip()
            conversation_id = (
                str(event.get_group_id()).strip()
                if not event.is_private_chat()
                else "private"
            )
            cooldown_key = (
                "private" if event.is_private_chat() else "group",
                conversation_id,
                sender_id,
            )
            now = time.monotonic()
            if now < cooldowns.get(cooldown_key, 0.0):
                cooldown_message = str(
                    self._config.get("cooldown_message", "") or ""
                ).strip()
                if cooldown_message:
                    yield event.plain_result(cooldown_message)
                if block_other_handlers:
                    event.stop_event()
                return
            cooldowns[cooldown_key] = now + cooldown_seconds

        fetching_message = str(self._config.get("fetching_message", "") or "").strip()
        if fetching_message:
            yield event.plain_result(fetching_message)

        configured_sources = self._config.get("image_source_order", [])
        if not isinstance(configured_sources, list) or not configured_sources:
            configured_sources = [self._config.get("image_source", "custom")]
        sources = list(
            dict.fromkeys(
                str(source).strip().lower()
                for source in configured_sources
                if str(source).strip()
            )
        )
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
                    elif source == "custom":
                        image_urls = [
                            await self._fetch_custom_image(message_tags)
                            for _ in range(image_count)
                        ]
                        image_pids = []
                    elif source == "lolicon":
                        image_urls, image_pids = await self._fetch_lolicon_images(
                            image_count, message_tags
                        )
                    elif source == "waifu_im":
                        image_urls = await self._fetch_waifu_im_images(
                            image_count, message_tags
                        )
                        image_pids = []
                    elif source == "nekos_api":
                        image_urls = await self._fetch_nekos_api_images(
                            image_count, message_tags
                        )
                        image_pids = []
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
            failure_message = str(self._config.get("failure_message", "") or "").strip()
            if failure_message:
                yield event.plain_result(failure_message)
            elif isinstance(last_error, ValueError):
                yield event.plain_result(str(last_error))
            else:
                yield event.plain_result("图片获取失败，请稍后重试。")
            if block_other_handlers:
                event.stop_event()
            return

        pid_text = (
            "Pixiv PID: " + ",".join(image_pids)
            if self._config.get("show_pixiv_pid", False) and image_pids
            else None
        )
        forward_requested = (
            len(image_urls) > 1
            and self._config.get("multi_image_send_mode") == "forward"
        ) or (
            len(image_urls) == 1
            and bool(self._config.get("single_image_forward", False))
        )
        forward_texts = (
            [f"Pixiv PID: {pid}" for pid in image_pids]
            if pid_text and forward_requested
            else None
        )
        forward_status = (
            await self._send_forward_images(event, image_urls, forward_texts)
            if forward_requested
            else None
        )
        if forward_status is False:
            failure_message = str(self._config.get("failure_message", "") or "").strip()
            yield event.plain_result(failure_message or "图片发送失败，请稍后重试。")
            if block_other_handlers:
                event.stop_event()
            return
        sent_as_forward = forward_status is True
        all_images_delivered = True
        if not sent_as_forward:
            delivery_failure_message = (
                str(self._config.get("failure_message", "") or "").strip() or None
            )
            for image_url in image_urls:
                delivery_status = await self._send_image_with_auto_recall(
                    event,
                    image_url,
                    failure_message=delivery_failure_message,
                )
                if delivery_status is None:
                    yield event.image_result(image_url)
                elif not delivery_status:
                    all_images_delivered = False
        if pid_text and not sent_as_forward and all_images_delivered:
            yield event.plain_result(pid_text)
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
        messages = []
        for index, image_url in enumerate(image_urls):
            content: list[dict[str, Any]] = []
            if texts and index < len(texts):
                content.append({"type": "text", "data": {"text": texts[index]}})
            content.append({"type": "image", "data": {"file": image_url}})
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
        try:
            delay = max(0.0, float(self._config.get("recall_delay_seconds", 60)))
        except (TypeError, ValueError):
            delay = 60.0
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
    ) -> bool | None:
        """Send an image through OneBot and schedule a recall when enabled.

        Args:
            event: Incoming AstrBot message event.
            image_url: Remote image URL to send.
            text: Optional text sent in the same recallable message.
            failure_message: Optional plain text to send when direct delivery fails.

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

        message: list[dict[str, Any]] = [{"type": "image", "data": {"file": image_url}}]
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
            if image_url.startswith("base64://"):
                temporary_image: Path | None = None
                try:
                    image_bytes = base64.b64decode(
                        image_url.removeprefix("base64://"), validate=True
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
                try:
                    await bot.call_action(
                        action,
                        **recipient,
                        message=[{"type": "text", "data": {"text": failure_message}}],
                        **routing_params,
                    )
                except Exception:
                    logger.warning(
                        "[CrimsonCosmos] Delivery failure notification send failed",
                        exc_info=True,
                    )
                return False
            return None

        if not self._config.get("auto_recall", False):
            return True

        message_id = response.get("message_id") if isinstance(response, dict) else None
        if message_id is None:
            logger.warning("[CrimsonCosmos] Auto-recall message ID is unavailable")
            return True

        try:
            delay = max(0.0, float(self._config.get("recall_delay_seconds", 60)))
        except (TypeError, ValueError):
            delay = 60.0
        self._schedule_recall(
            bot,
            {
                "message_id": message_id,
                "due_at": time.time() + delay,
                "routing_params": routing_params,
            },
        )
        return True

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

    async def _read_jina_text(self, url: str, timeout: aiohttp.ClientTimeout) -> str:
        """Read a Jina page with bounded retries for temporary failures.

        Args:
            url: Full Jina Reader URL.
            timeout: Per-attempt HTTP timeout.

        Returns:
            Markdown response text.

        Raises:
            aiohttp.ClientError: If all attempts fail or the error is permanent.
            asyncio.TimeoutError: If all attempts time out.
        """
        for attempt in range(3):
            try:
                async with self._session.get(url, timeout=timeout) as response:
                    response.raise_for_status()
                    return await response.text()
            except aiohttp.ClientResponseError as error:
                if error.status not in {429, 500, 502, 503, 504} or attempt == 2:
                    raise
            except asyncio.TimeoutError:
                if attempt == 2:
                    raise
            await asyncio.sleep(2**attempt)
        raise ValueError("Jina Reader 请求失败。")

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
            self._session = aiohttp.ClientSession()
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

    async def _fetch_jable_video(self, request: tuple[str, int, str]) -> dict[str, Any]:
        """Fetch one ranked Jable video through text-reader endpoints.

        Args:
            request: Parsed Jable URL, one-based rank, and display list name.

        Returns:
            Parsed video metadata used by the message report.

        Raises:
            ValueError: If the requested theme or ranked video cannot be parsed.
        """
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        target_url, rank, list_name = request
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
        listing_cache = getattr(self, "_jable_listing_cache", {})
        listing = listing_cache.get(listing_url)
        if listing is None:
            listing = await self._read_jina_text(listing_url, timeout)
            listing_cache[listing_url] = listing
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
            next_page = listing_cache.get(next_page_url)
            if next_page is None:
                next_page = await self._read_jina_text(next_page_url, timeout)
                listing_cache[next_page_url] = next_page
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
        try:
            detail = await self._read_jina_text(
                f"https://r.jina.ai/{video_url}", timeout
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
            logger.warning("[CrimsonCosmos] Jable detail unavailable: %s", video_url)
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
            quantity_match = re.search(
                rf"(?P<count>[1-9]\d*|[一二两三四五六七八九十])"
                rf"\s*(?:张|份|个)\s*(?P<tags>.*?){re.escape(keyword)}",
                message,
            )
            if quantity_match:
                count_text = quantity_match.group("count")
                count = (
                    int(count_text)
                    if count_text.isdigit()
                    else CHINESE_IMAGE_COUNTS[count_text]
                )
                tag_text = quantity_match.group("tags")
                tags = [tag for tag in re.split(r"[\s,，、]+", tag_text.strip()) if tag]
                return count, tags
            suffix_tag_text = (
                message[: -len(keyword)].strip() if message.endswith(keyword) else ""
            )
            suffix_keyword_match = (
                match_mode == "exact"
                and bool(suffix_tag_text)
                and not re.fullmatch(
                    r"(?:请|来|要|给我|发我)*\s*"
                    r"(?:[1-9]\d*|[一二两三四五六七八九十])?\s*"
                    r"(?:张|份|个)?",
                    suffix_tag_text,
                )
            )
            if (
                (
                    match_mode == "exact"
                    and (message == keyword or message.startswith(f"{keyword} "))
                )
                or suffix_keyword_match
                or (match_mode == "prefix" and message.startswith(keyword))
                or (match_mode == "contains" and keyword in message)
            ):
                tag_text = message.replace(keyword, " ", 1)
                tags = [
                    tag
                    for tag in re.split(r"[\s,，、]+", tag_text.strip())
                    if tag and tag not in {"来", "请", "一张", "一份"}
                ]
                return 1, tags
        return None

    async def _fetch_custom_image(self, tags: list[str] | None = None) -> str:
        """Fetch an image URL from the configured JSON API.

        Returns:
            A valid remote image URL from the configured JSON path.

        Raises:
            ValueError: If the API URL, JSON path, or resolved image URL is invalid.
        """
        api_url = str(self._config.get("custom_api_url") or "").strip()
        if not api_url:
            raise ValueError("请先配置自定义色图 API。")
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

        tag_parameter = str(
            self._config.get("custom_api_tag_parameter", "tag") or ""
        ).strip()
        params = {tag_parameter: ",".join(tags)} if tags and tag_parameter else None
        async with self._session.get(
            api_url, params=params, timeout=aiohttp.ClientTimeout(total=20)
        ) as response:
            response.raise_for_status()
            payload: Any = await response.json(content_type=None)

        image_url: Any = payload
        if not isinstance(payload, str):
            path = str(self._config.get("custom_api_image_url_path") or "url").strip()
            for part in path.split("."):
                if isinstance(image_url, dict):
                    image_url = image_url.get(part)
                elif isinstance(image_url, list) and part.isdigit():
                    index = int(part)
                    image_url = image_url[index] if index < len(image_url) else None
                else:
                    image_url = None
                if image_url is None:
                    break

        if not isinstance(image_url, str) or not image_url.startswith(
            ("http://", "https://")
        ):
            raise ValueError("自定义 API 未返回有效的图片链接。")
        return image_url

    async def _fetch_waifu_im_images(
        self, count: int, message_tags: list[str] | None = None
    ) -> list[str]:
        """Fetch filtered random images from Waifu.im.

        Args:
            count: Number of images to request.
            message_tags: Tags parsed from the triggering message.

        Returns:
            Valid remote image URLs.

        Raises:
            ValueError: If Waifu.im returns an invalid or incomplete response.
        """
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        nsfw_mode = str(self._config.get("waifu_im_nsfw_mode", "r18")).lower()
        params: dict[str, Any] = {
            "IsNsfw": {"sfw": "False", "r18": "True", "mix": "All"}.get(
                nsfw_mode, "True"
            ),
            "OrderBy": "RANDOM",
            "PageSize": str(count),
        }
        if message_tags:
            params["IncludedTags"] = message_tags
        excluded_tags = self._config.get("waifu_im_excluded_tags", [])
        if isinstance(excluded_tags, list) and excluded_tags:
            params["ExcludedTags"] = [
                str(tag).strip() for tag in excluded_tags if str(tag).strip()
            ]
        orientation = str(self._config.get("waifu_im_orientation", "")).lower()
        if orientation in {"portrait", "landscape", "square"}:
            params["Orientation"] = orientation.upper()
        async with self._session.get(
            "https://api.waifu.im/images",
            params=params,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            response.raise_for_status()
            body: Any = await response.json(content_type=None)
        items = body.get("items") if isinstance(body, dict) else None
        urls = [
            item["url"]
            for item in items or []
            if isinstance(item, dict)
            and isinstance(item.get("url"), str)
            and item["url"].startswith(("http://", "https://"))
        ]
        if len(urls) != count:
            raise ValueError("Waifu.im 未返回足够的可用图片。")
        return urls

    async def _fetch_nekos_api_images(
        self, count: int, message_tags: list[str] | None = None
    ) -> list[str]:
        """Fetch random filtered images from Nekos API v5.

        Args:
            count: Number of images to request.
            message_tags: Tags parsed from the triggering message.

        Returns:
            Valid remote image URLs.

        Raises:
            ValueError: If Nekos API returns an invalid image URL.
        """
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        configured_rating = str(self._config.get("nekos_api_rating", "露骨")).lower()
        rating = {
            "安全": "safe",
            "暗示": "suggestive",
            "边缘": "borderline",
            "露骨": "explicit",
            "safe": "safe",
            "suggestive": "suggestive",
            "borderline": "borderline",
            "explicit": "explicit",
        }.get(configured_rating, "explicit")
        params: dict[str, Any] = {"rating": rating}
        if message_tags:
            params["tag"] = message_tags[:5]
        urls = []
        for _ in range(count):
            async with self._session.get(
                "https://api.nekosapi.com/v5/images/random",
                params=params,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                response.raise_for_status()
                body: Any = await response.json(content_type=None)
            image_url = body.get("url") if isinstance(body, dict) else None
            if not isinstance(image_url, str) or not image_url.startswith(
                ("http://", "https://")
            ):
                raise ValueError("Nekos API 未返回有效的图片链接。")
            urls.append(image_url)
        return urls

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
            self._session = aiohttp.ClientSession()

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

    async def _fetch_lolicon_images(
        self, count: int, message_tags: list[str] | None = None
    ) -> tuple[list[str], list[str]]:
        """Fetch filtered Pixiv image URLs through the Lolicon API.

        Args:
            count: Number of image URLs to return.
            message_tags: Tags parsed from the triggering message.

        Returns:
            Image URLs and their unique Pixiv IDs.

        Raises:
            ValueError: If Lolicon rejects the request or returns invalid data.
        """
        aliases: dict[str, str] = {}
        raw_aliases = str(self._config.get("lolicon_tag_aliases", "") or "")
        for pair in re.split(r"[,，\n]+", raw_aliases):
            if "=" not in pair:
                continue
            alias, target = (part.strip() for part in pair.split("=", 1))
            if alias and target:
                aliases[alias] = target
        tags = [aliases.get(tag, tag) for tag in message_tags or []]

        r18_modes = {"sfw": 0, "r18": 1, "mix": 2}
        r18 = r18_modes.get(str(self._config.get("lolicon_r18_mode", "r18")).lower(), 1)
        size = str(self._config.get("lolicon_image_size", "small")).lower()
        valid_sizes = {"original", "regular", "small", "thumb", "mini"}
        if size not in valid_sizes:
            size = "small"
        payload: dict[str, Any] = {
            "r18": r18,
            "num": count,
            "excludeAI": bool(self._config.get("lolicon_exclude_ai", True)),
            "size": [size],
        }
        if tags:
            payload["tag"] = [tags]
        aspect_ratio = str(self._config.get("lolicon_aspect_ratio", "") or "").strip()
        if aspect_ratio in {"gt1", "lt1", "eq1"}:
            payload["aspectRatio"] = aspect_ratio
        proxy = str(self._config.get("lolicon_proxy", "") or "").strip()
        if proxy:
            payload["proxy"] = proxy

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
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

        urls: list[str] = []
        pids: list[str] = []
        for image in images:
            if not isinstance(image, dict) or not isinstance(image.get("urls"), dict):
                continue
            image_urls = image["urls"]
            candidates = [size, "original", "regular", "small", "thumb", "mini"]
            image_url = next(
                (
                    image_urls.get(candidate)
                    for candidate in dict.fromkeys(candidates)
                    if isinstance(image_urls.get(candidate), str)
                    and image_urls[candidate].startswith(("http://", "https://"))
                ),
                None,
            )
            if not image_url:
                continue
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
            candidates = proxy_order if parsed_url.hostname in pixiv_hosts else []
            for proxy in dict.fromkeys(
                str(item).strip().rstrip("/") for item in candidates
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
            if not proxy_urls:
                proxy_urls.append(image_url)

            timeout_seconds = max(
                1,
                min(
                    int(self._config.get("lolicon_proxy_timeout_seconds", 8) or 8),
                    30,
                ),
            )
            resolved_url = None
            resolved_image = None
            for proxy_url in proxy_urls:
                try:
                    async with self._session.get(
                        proxy_url,
                        timeout=aiohttp.ClientTimeout(total=timeout_seconds),
                    ) as response:
                        response.raise_for_status()
                        image_buffer = bytearray()
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            image_buffer.extend(chunk)
                            if len(image_buffer) > 20 * 1024 * 1024:
                                raise ValueError("Pixiv 图片超过 20 MiB 限制。")
                        resolved_image = bytes(image_buffer)
                        if not resolved_image:
                            raise ValueError("Pixiv 图片代理返回空内容。")
                    resolved_url = proxy_url
                    break
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                    logger.warning("Pixiv image proxy unavailable: %s", proxy_url)
            if not resolved_url or resolved_image is None:
                continue
            urls.append("base64://" + base64.b64encode(resolved_image).decode("ascii"))
            if pid.isdigit() and pid not in pids:
                pids.append(pid)
            if len(urls) == count:
                break
        if len(urls) != count:
            raise ValueError("Lolicon API 未返回足够的可用图片。")
        return urls, pids
