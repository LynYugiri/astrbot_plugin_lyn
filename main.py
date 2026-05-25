from __future__ import annotations

import asyncio
import contextlib
import gzip
import importlib.util
import json
import re
import shutil
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlparse

import astrbot.api.message_components as Comp
import jmcomic
import yaml
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path


class DownloadSizeLimitExceeded(Exception):
    pass


@register("astrbot_plugin_lyn", "Lemoec", "下载 JM 漫画并转换为 PDF 发送", "2.1.0")
class JmPlugin(Star):
    jm_help_msg = "使用方式: /jm <漫画ID>"
    jm_search_help_msg = "使用方式: /jm搜索 <关键词1> <关键词2>..."
    jm_failed_msg = "hentai！一天到晚看本子真是没救了喵"
    pixiv_help_msg = "使用方式: /pixiv <PixivID/链接>"
    cleanup_interval_seconds = 24 * 60 * 60
    download_ttl_seconds = 24 * 60 * 60
    max_download_bytes = 150 * 1024 * 1024
    search_limit = 10
    pixiv_max_pages = 500
    pixiv_forward_chunk_size = 42
    pixiv_extensions = ("png", "jpg", "gif")
    video_max_size_bytes = 100 * 1024 * 1024
    video_url_pattern = re.compile(r"https?://[^\s\])}>\"'，。！？、；]+", re.IGNORECASE)
    bilibili_bv_pattern = re.compile(r"\bBV[0-9A-Za-z]{10}\b")
    bilibili_av_pattern = re.compile(r"(?:/video/av|\bav)(\d+)\b", re.IGNORECASE)
    video_domains = (
        "bilibili.com",
        "b23.tv",
        "youtube.com",
        "youtu.be",
        "x.com",
        "twitter.com",
        "tiktok.com",
        "douyin.com",
        "kuaishou.com",
        "vimeo.com",
    )

    def __init__(self, context: Context):
        super().__init__(context)
        self.data_dir = Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_lyn"
        self.pdf_root = self.data_dir / "pdf"
        self.video_root = self.data_dir / "video"
        self.option_file = self.data_dir / "jm_option.yml"
        self._download_locks: dict[int, asyncio.Lock] = {}
        self._cleanup_task: asyncio.Task | None = None

    async def initialize(self):
        self.pdf_root.mkdir(parents=True, exist_ok=True)
        self.video_root.mkdir(parents=True, exist_ok=True)
        self._write_option_file(self.option_file, self.pdf_root)
        if not self._has_img2pdf():
            logger.warning("JM PDF 下载依赖 img2pdf 未安装，请确认插件 requirements.txt 已安装完成")
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    @filter.command("jm")
    async def jm(self, event: AstrMessageEvent):
        """下载 JM 漫画，转换为 PDF 后发送文件。"""
        comic_id = self._parse_album_id(event.message_str)
        if comic_id is None:
            yield event.plain_result(f"漫画ID必须为正整数。\n{self.jm_help_msg}")
            return

        yield event.plain_result(f"开始下载 JM{comic_id}，PDF 生成可能需要一段时间。")

        lock = self._lock_for(comic_id)
        try:
            async with lock:
                pdf_path = await asyncio.to_thread(self._download_pdf, comic_id)
        except Exception as exc:
            logger.exception(f"JM{comic_id} 下载失败: {exc}")
            yield event.plain_result(f"{self.jm_failed_msg}\nJM{comic_id} 下载失败：{exc}")
            return

        yield event.chain_result([
            Comp.Plain(f"JM{comic_id} 下载完成：{pdf_path.name}"),
            Comp.File(file=str(pdf_path), name=pdf_path.name, url=pdf_path.as_uri()),
        ])

    @filter.command("jm搜索")
    async def jm_search(self, event: AstrMessageEvent):
        """搜索 JM 漫画信息。"""
        keywords = self._parse_keywords(event.message_str)
        if not keywords:
            yield event.plain_result(self.jm_search_help_msg)
            return

        try:
            result = await asyncio.to_thread(self._search, keywords)
        except Exception as exc:
            logger.exception(f"JM 搜索失败: {exc}")
            yield event.plain_result(f"{self.jm_failed_msg}\n搜索漫画失败: {exc}\n{self.jm_search_help_msg}")
            return

        yield event.plain_result(result)

    @filter.command("pixiv")
    async def pixiv(self, event: AstrMessageEvent):
        """通过 pixiv.re 获取 Pixiv 图片，并私信给触发用户。"""
        pixiv_id = self._parse_pixiv_id(event.message_str)
        if pixiv_id is None:
            yield event.plain_result(f"PixivID 或链接无效。\n{self.pixiv_help_msg}")
            return

        if not self._is_qq_event(event):
            yield event.plain_result("/pixiv 当前仅支持 QQ OneBot v11 平台。")
            return

        sender_id = event.get_sender_id()
        if not sender_id:
            yield event.plain_result("无法获取触发用户 QQ 号，不能发送私信。")
            return

        yield event.plain_result(f"正在获取 Pixiv {pixiv_id}，稍后会私信发送。")
        try:
            image_urls = await asyncio.to_thread(self._resolve_pixiv_images, pixiv_id)
            await self._send_pixiv_private_forward(event, sender_id, image_urls)
        except Exception as exc:
            logger.exception(f"Pixiv {pixiv_id} 获取或发送失败: {exc}")
            yield event.plain_result(f"Pixiv {pixiv_id} 获取或发送失败：{exc}")

    @filter.command("下载")
    async def download_video_file(self, event: AstrMessageEvent):
        """下载视频平台资源，并作为文件发送。"""
        url = self._parse_command_arg(event.message_str) or self._first_message_url(event)
        if not url:
            yield event.plain_result("使用方式: /下载 <视频链接/小程序>")
            return

        async for result in self._download_video(event, url, as_file=True):
            yield result

    @filter.command("视频")
    async def download_video_message(self, event: AstrMessageEvent):
        """下载视频平台资源，并作为视频消息发送。"""
        url = self._parse_command_arg(event.message_str) or self._first_message_url(event)
        if not url:
            yield event.plain_result("使用方式: /视频 <视频链接/小程序>")
            return

        async for result in self._download_video(event, url, as_file=False):
            yield result

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def auto_convert_video_link(self, event: AstrMessageEvent):
        """自动解析视频平台链接或 QQ 小程序卡片，并回复媒体信息。"""
        if self._is_command_message(event.message_str):
            return

        raw_urls = self._extract_message_urls(event)
        if not raw_urls:
            return

        chain = []
        seen = set()
        for raw_url in raw_urls:
            summary = await asyncio.to_thread(self._describe_media_url, raw_url)
            if summary and summary[0] not in seen:
                seen.add(summary[0])
                if summary[1]:
                    chain.append(Comp.Image.fromURL(summary[1]))
                chain.append(Comp.Plain(summary[0]))

        if chain:
            yield event.chain_result(chain)

    def _lock_for(self, comic_id: int) -> asyncio.Lock:
        lock = self._download_locks.get(comic_id)
        if lock is None:
            lock = asyncio.Lock()
            self._download_locks[comic_id] = lock
        return lock

    def _write_option_file(self, option_file: Path, pdf_dir: Path, image_dir: Path | None = None) -> None:
        image_dir = image_dir or self.data_dir / "images"
        options = {
            "log": False,
            "dir_rule": {"base_dir": str(image_dir)},
            "plugins": {
                "after_album": [
                    {
                        "plugin": "img2pdf",
                        "kwargs": {
                            "pdf_dir": str(pdf_dir),
                            "filename_rule": "Aname",
                        },
                    }
                ]
            },
        }
        option_file.parent.mkdir(parents=True, exist_ok=True)
        option_file.write_text(
            yaml.safe_dump(options, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    def _download_pdf(self, comic_id: int) -> Path:
        if not self._has_img2pdf():
            raise RuntimeError("缺少 img2pdf 依赖，请先安装插件 requirements.txt")

        album_pdf_dir = self.pdf_root / str(comic_id) / uuid.uuid4().hex
        album_pdf_dir.mkdir(parents=True, exist_ok=True)

        option_file = album_pdf_dir / "jm_option.yml"
        self._write_option_file(option_file, album_pdf_dir, album_pdf_dir / "images")
        option = jmcomic.create_option_by_file(str(option_file))
        downloader = self._limited_downloader_class(album_pdf_dir)
        try:
            jmcomic.download_album(comic_id, option, downloader=downloader)
        except DownloadSizeLimitExceeded:
            shutil.rmtree(album_pdf_dir, ignore_errors=True)
            raise RuntimeError("漫画文件超过 150MB，已停止下载") from None

        pdf_files = sorted(
            album_pdf_dir.glob("*.pdf"),
            key=lambda path: (path.stat().st_size, path.stat().st_mtime),
            reverse=True,
        )
        if not pdf_files:
            raise FileNotFoundError(f"JM{comic_id} 未生成 PDF")
        if pdf_files[0].stat().st_size > self.max_download_bytes:
            shutil.rmtree(album_pdf_dir, ignore_errors=True)
            raise RuntimeError("漫画文件超过 150MB，已停止下载")
        return pdf_files[0]

    def _limited_downloader_class(self, download_dir: Path):
        max_bytes = self.max_download_bytes

        class LimitedDownloader(jmcomic.JmDownloader):
            def before_image(self, image, img_save_path):
                self._raise_if_too_large()
                return super().before_image(image, img_save_path)

            def after_image(self, image, img_save_path):
                result = super().after_image(image, img_save_path)
                self._raise_if_too_large()
                return result

            def after_album(self, album):
                self._raise_if_too_large()
                return super().after_album(album)

            def _raise_if_too_large(self) -> None:
                total_size = 0
                for path in download_dir.rglob("*"):
                    if not path.is_file():
                        continue
                    with contextlib.suppress(OSError):
                        total_size += path.stat().st_size
                    if total_size > max_bytes:
                        raise DownloadSizeLimitExceeded

        return LimitedDownloader

    def _has_img2pdf(self) -> bool:
        return importlib.util.find_spec("img2pdf") is not None

    async def _cleanup_loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self._cleanup_downloads)
            except Exception as exc:
                logger.warning(f"JM 下载数据清理失败: {exc}")
            await asyncio.sleep(self.cleanup_interval_seconds)

    def _cleanup_downloads(self) -> None:
        if not self.pdf_root.exists():
            return

        expire_before = time.time() - self.download_ttl_seconds
        for album_dir in list(self.pdf_root.iterdir()):
            if not album_dir.is_dir():
                continue

            for task_dir in list(album_dir.iterdir()):
                if not task_dir.is_dir():
                    continue

                with contextlib.suppress(OSError):
                    if task_dir.stat().st_mtime >= expire_before:
                        continue
                    shutil.rmtree(task_dir, ignore_errors=True)

            with contextlib.suppress(OSError):
                if not any(album_dir.iterdir()):
                    album_dir.rmdir()

    def _search(self, keywords: str) -> str:
        query = " +".join(keywords.split())
        client = jmcomic.JmOption.default().new_jm_client()
        page = client.search_site(query, 1)
        if not page or page.page_size <= 0:
            return f"{self.jm_failed_msg}\n没有找到与'{keywords}'相关的漫画。\n{self.jm_search_help_msg}"

        lines = [f"搜索结果: {keywords}", "----------"]
        for index, (album_id, title) in enumerate(page):
            if index >= self.search_limit:
                lines.append(f"仅展示前 {self.search_limit} 条结果。")
                break

            detail_lines = [f"jm号: {album_id}", f"标题: {title}"]
            try:
                photo = client.get_photo_detail(album_id, False)
            except Exception as exc:
                logger.warning(f"JM{album_id} 搜索详情获取失败: {exc}")
            else:
                detail_lines.extend([f"作者: {photo.author}", f"标签: {photo.tags}"])

            lines.extend([*detail_lines, "----------"])
        return "\n".join(lines)

    def _parse_album_id(self, message: str) -> int | None:
        parts = message.strip().split(maxsplit=1)
        if len(parts) < 2:
            return None

        album_id = parts[1].strip()
        if not album_id.isdecimal():
            return None

        value = int(album_id)
        return value if value > 0 else None

    def _parse_keywords(self, message: str) -> str:
        parts = message.strip().split(maxsplit=1)
        return parts[1].strip() if len(parts) >= 2 else ""

    def _parse_command_arg(self, message: str) -> str:
        parts = message.strip().split(maxsplit=1)
        if len(parts) < 2:
            return ""
        return parts[1].strip()

    async def _download_video(self, event: AstrMessageEvent, raw_url: str, as_file: bool):
        url = await asyncio.to_thread(self._resolve_download_url, raw_url)
        if not url:
            yield event.plain_result("没有找到可下载的视频链接。")
            return

        yield event.plain_result("正在解析并下载视频，请稍候。")
        try:
            video_path = await asyncio.to_thread(self._download_video_file, url)
        except Exception as exc:
            logger.exception(f"视频下载失败: {exc}")
            yield event.plain_result(f"视频下载失败：{exc}")
            return

        try:
            file_size = video_path.stat().st_size
            if file_size > self.video_max_size_bytes:
                yield event.plain_result(f"视频文件超过 100MB，已停止发送：{video_path.name}")
                return

            if as_file:
                yield event.chain_result([Comp.File(file=str(video_path), name=video_path.name)])
            else:
                yield event.chain_result([Comp.Video.fromFileSystem(str(video_path))])
        finally:
            asyncio.create_task(self._delete_later(video_path, self.download_ttl_seconds))

    def _resolve_download_url(self, text: str) -> str | None:
        urls = self._extract_urls_from_text(text)
        if not urls:
            return None
        normalized = self._normalize_video_url(urls[0])
        return normalized or urls[0]

    def _get_video_info_safe(self, url: str) -> dict | None:
        try:
            import yt_dlp
        except ImportError:
            return None

        options = {
            "quiet": True,
            "no_warnings": True,
            "nocheckcertificate": True,
            "noplaylist": True,
            "skip_download": True,
        }
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                return downloader.extract_info(url, download=False)
        except Exception as exc:
            logger.warning(f"视频信息解析失败: {exc}")
            return None

    def _describe_media_url(self, raw_url: str) -> tuple[str, str | None] | None:
        url = self._normalize_video_url(raw_url)
        if not url:
            return None

        info = self._get_video_info_safe(url)
        if not info:
            return "\n".join([url, "媒体信息解析失败，请确认 yt-dlp 已安装或稍后重试。"]), None

        page_url = self._canonical_media_url(url, info)
        cover_url = self._media_cover_url(info)
        if self._is_bilibili_host(urlparse(page_url).netloc):
            return self._format_bilibili_summary(page_url, info), cover_url

        return self._format_generic_media_summary(page_url, info), cover_url

    def _format_bilibili_summary(self, page_url: str, info: dict) -> str:
        title = info.get("title") or "未知标题"
        uploader = info.get("uploader") or info.get("channel") or "未知"
        uploader_url = info.get("uploader_url") or self._bilibili_space_url(info)
        category = self._first_text(info.get("categories")) or info.get("category") or info.get("genre") or ""
        description = self._format_description(info.get("description"))
        lines = [page_url, f"标题：{title}"]
        if category:
            lines.append(f"类型：{category}")
        lines.extend([
            f"UP：{uploader} | {uploader_url}" if uploader_url else f"UP：{uploader}",
            "",
            (
                f"播放：{self._format_count(info.get('view_count'))} | "
                f"弹幕：{self._format_count(info.get('danmaku_count') or info.get('comment_count_danmaku'))} | "
                f"收藏：{self._format_count(info.get('favorite_count'))}"
            ),
            (
                f"点赞：{self._format_count(info.get('like_count'))} | "
                f"硬币：{self._format_count(info.get('coin_count'))} | "
                f"评论：{self._format_count(info.get('comment_count'))}"
            ),
            f"简介：{description}",
        ])
        return "\n".join(lines)

    def _media_cover_url(self, info: dict) -> str | None:
        for key in ("thumbnail", "display_id", "cover"):
            value = info.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return value
        thumbnails = info.get("thumbnails")
        if isinstance(thumbnails, list):
            for item in reversed(thumbnails):
                if not isinstance(item, dict):
                    continue
                value = item.get("url")
                if isinstance(value, str) and value.startswith(("http://", "https://")):
                    return value
        return None

    def _format_generic_media_summary(self, page_url: str, info: dict) -> str:
        title = info.get("title") or "未知标题"
        uploader = info.get("uploader") or info.get("channel") or "未知"
        duration = self._format_duration(info.get("duration"))
        filesize = self._format_size(info.get("filesize") or info.get("filesize_approx"))
        description = self._format_description(info.get("description"))
        lines = [
            page_url,
            f"标题：{title}",
            f"作者：{uploader}",
            f"时长：{duration} | 大小：{filesize}",
            f"简介：{description}",
        ]

        best_combined, best_video, best_audio = self._select_best_formats(info.get("formats", []))
        if best_combined:
            lines.append(f"最佳合并流：{self._format_stream_info(best_combined)}")
        elif info.get("url"):
            lines.append("最佳合并流：可用")
        else:
            lines.append("最佳合并流：无")

        lines.append(f"最佳视频流：{self._format_stream_info(best_video) if best_video else '无'}")
        lines.append(f"最佳音频流：{self._format_stream_info(best_audio) if best_audio else '无'}")
        return "\n".join(lines)

    def _bilibili_space_url(self, info: dict) -> str:
        uploader_id = info.get("uploader_id") or info.get("channel_id")
        return f"https://space.bilibili.com/{uploader_id}" if uploader_id else ""

    def _first_text(self, value: object) -> str:
        if isinstance(value, list) and value:
            return str(value[0])
        if isinstance(value, str):
            return value
        return ""

    def _format_count(self, value: int | float | None) -> str:
        if value is None:
            return "未知"
        number = float(value)
        if number >= 10000:
            return f"{number / 10000:.2f}万"
        return str(int(number))

    def _format_description(self, description: object) -> str:
        if not description:
            return "无"
        text = str(description).strip().replace("\r", "").replace("\n", " ")
        return text[:200] + "..." if len(text) > 200 else text

    def _canonical_media_url(self, fallback_url: str, info: dict) -> str:
        for value in (info.get("webpage_url"), info.get("original_url"), fallback_url):
            if not value:
                continue
            normalized = self._normalize_video_url(str(value))
            if normalized:
                return normalized
        return fallback_url

    def _select_best_formats(self, formats: list[dict]) -> tuple[dict | None, dict | None, dict | None]:
        best_combined = None
        best_video = None
        best_audio = None
        for item in formats:
            vcodec = item.get("vcodec", "none")
            acodec = item.get("acodec", "none")
            if vcodec != "none" and acodec != "none":
                best_combined = item
            elif vcodec != "none" and acodec == "none":
                best_video = item
            elif vcodec == "none" and acodec != "none":
                best_audio = item
        return best_combined, best_video, best_audio

    def _format_stream_info(self, info: dict) -> str:
        width = info.get("width") or "?"
        height = info.get("height") or "?"
        ext = info.get("ext") or "?"
        vcodec = info.get("vcodec") or "?"
        acodec = info.get("acodec") or "?"
        filesize = self._format_size(info.get("filesize") or info.get("filesize_approx"))
        return f"{width}x{height}, {ext}, v:{vcodec}, a:{acodec}, {filesize}"

    def _format_size(self, size_bytes: int | float | None) -> str:
        if not size_bytes:
            return "未知"
        size = float(size_bytes)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                return f"{size:.2f} {unit}" if unit != "B" else f"{int(size)} B"
            size /= 1024
        return "未知"

    def _format_duration(self, duration: int | float | None) -> str:
        if not duration:
            return "未知"
        seconds = int(duration)
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"

    def _download_video_file(self, url: str) -> Path:
        try:
            import imageio_ffmpeg
            import yt_dlp
        except ImportError as exc:
            raise RuntimeError("缺少视频下载依赖，请安装 requirements.txt 中的 yt-dlp 和 imageio-ffmpeg") from exc

        task_dir = self.video_root / uuid.uuid4().hex
        task_dir.mkdir(parents=True, exist_ok=True)
        output_template = str(task_dir / "%(title).100s_%(id)s.%(ext)s")
        options = {
            "outtmpl": output_template,
            "format": "bestvideo[vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "merge_output_format": "mp4",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "ffmpeg_location": imageio_ffmpeg.get_ffmpeg_exe(),
        }

        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=True)
            downloaded = Path(downloader.prepare_filename(info))

        mp4_files = sorted(task_dir.glob("*.mp4"), key=lambda path: path.stat().st_mtime, reverse=True)
        if mp4_files:
            return mp4_files[0]
        if downloaded.exists():
            return downloaded

        files = sorted((path for path in task_dir.iterdir() if path.is_file()), key=lambda path: path.stat().st_mtime, reverse=True)
        if not files:
            raise FileNotFoundError("视频下载完成但未找到输出文件")
        return files[0]

    async def _delete_later(self, path: Path, delay_seconds: int) -> None:
        await asyncio.sleep(delay_seconds)
        parent = path.parent
        if parent != self.video_root and self._is_relative_to(parent, self.video_root):
            shutil.rmtree(parent, ignore_errors=True)
            return

        with contextlib.suppress(OSError):
            if path.exists():
                path.unlink()

    def _is_relative_to(self, path: Path, parent: Path) -> bool:
        with contextlib.suppress(ValueError):
            path.relative_to(parent)
            return True
        return False

    def _extract_message_urls(self, event: AstrMessageEvent) -> list[str]:
        urls = self._extract_urls_from_text(event.message_str)
        urls.extend(self._extract_light_app_urls(event))
        return [url for url in urls if self._is_video_platform_url(url)]

    def _first_message_url(self, event: AstrMessageEvent) -> str:
        urls = self._extract_message_urls(event)
        return urls[0] if urls else ""

    def _extract_urls_from_text(self, text: str) -> list[str]:
        return [match.group(0).rstrip("，。！？、；;,.!") for match in self.video_url_pattern.finditer(text or "")]

    def _extract_light_app_urls(self, event: AstrMessageEvent) -> list[str]:
        raw_event = getattr(event.message_obj, "raw_message", None)
        segments = getattr(raw_event, "message", None)
        if not isinstance(segments, list):
            return []

        urls = []
        for segment in segments:
            if segment.get("type") not in {"json", "light_app"}:
                continue

            payload = segment.get("data", {}).get("json") or segment.get("data", {}).get("json_payload")
            url = self._extract_url_from_light_app_payload(payload)
            if url:
                urls.append(url)
        return urls

    def _extract_url_from_light_app_payload(self, payload: object) -> str | None:
        if not payload:
            return None
        try:
            data = json.loads(payload) if isinstance(payload, str) else payload
        except (TypeError, json.JSONDecodeError):
            return None

        candidates = []
        self._collect_light_app_url_candidates(data, candidates)
        for url in candidates:
            if self._is_video_platform_url(url):
                return url
        return candidates[0] if candidates else None

    def _collect_light_app_url_candidates(self, value: object, candidates: list[str]) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, str) and key.lower() in {"url", "jumpurl", "qqdocurl", "pageurl", "srcurl"}:
                    candidates.extend(self._extract_urls_from_text(item))
                else:
                    self._collect_light_app_url_candidates(item, candidates)
        elif isinstance(value, list):
            for item in value:
                self._collect_light_app_url_candidates(item, candidates)

    def _normalize_video_url(self, url: str) -> str | None:
        resolved = self._resolve_redirect_url(url)
        if not self._is_video_platform_url(resolved):
            return None
        bilibili = self._normalize_bilibili_url(resolved)
        return bilibili or resolved.split("#", 1)[0]

    def _resolve_redirect_url(self, url: str) -> str:
        request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "AstrBot/astrbot_plugin_lyn"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.url
        except (OSError, urllib.error.URLError):
            request = urllib.request.Request(url, headers={"User-Agent": "AstrBot/astrbot_plugin_lyn"})
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    return response.url
            except (OSError, urllib.error.URLError):
                return url

    def _is_video_platform_url(self, url: str) -> bool:
        host = urlparse(url).netloc.lower()
        return any(self._is_domain(host, domain) for domain in self.video_domains)

    def _is_domain(self, host: str, domain: str) -> bool:
        return host == domain or host.endswith(f".{domain}")

    def _normalize_bilibili_url(self, url: str) -> str | None:
        parsed = urlparse(url)
        if not self._is_bilibili_host(parsed.netloc):
            return None

        match = self.bilibili_bv_pattern.search(url)
        if match:
            return f"https://www.bilibili.com/video/{match.group(0)}"

        match = self.bilibili_av_pattern.search(url)
        if match:
            bv_id = (
                self._extract_bilibili_bv_from_page(url)
                or self._extract_bilibili_bv_with_ytdlp(url)
                or self._av_to_bv(int(match.group(1)))
            )
            return f"https://www.bilibili.com/video/{bv_id}"
        return None

    def _extract_bilibili_bv_from_page(self, url: str) -> str | None:
        request = urllib.request.Request(url, headers={"User-Agent": "AstrBot/astrbot_plugin_lyn"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                body = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                html = body.decode("utf-8", errors="ignore")
        except (OSError, urllib.error.URLError):
            return None

        match = re.search(r'content="https://www\.bilibili\.com/video/(BV[0-9A-Za-z]{10})/?"', html)
        if match:
            return match.group(1)
        match = self.bilibili_bv_pattern.search(html)
        return match.group(0) if match else None

    def _extract_bilibili_bv_with_ytdlp(self, url: str) -> str | None:
        try:
            import yt_dlp
        except ImportError:
            return None

        options = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
        }
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                info = downloader.extract_info(url, download=False)
        except Exception as exc:
            logger.warning(f"B站 av 链接解析 BV 失败，使用本地转换兜底: {exc}")
            return None

        video_id = str(info.get("id") or "")
        match = self.bilibili_bv_pattern.search(video_id)
        return match.group(0) if match else None

    def _is_bilibili_host(self, host: str) -> bool:
        host = host.lower()
        return self._is_domain(host, "b23.tv") or self._is_domain(host, "bilibili.com")

    def _av_to_bv(self, avid: int) -> str:
        table = "fZodR9XQDSUm21yCkr6zBqiveYah8btxsWpHnJE7jL5VG3guMTKNPAwcF"
        positions = (11, 10, 3, 8, 4, 6)
        value = (avid ^ 177451812) + 100618342136696320
        chars = list("BV1  4 1 7  ")
        for index, position in enumerate(positions):
            chars[position] = table[value // 58 ** index % 58]
        return "".join(chars)

    def _is_command_message(self, message: str) -> bool:
        stripped = (message or "").strip()
        return stripped.startswith(("/", "!", "#", "$", "＞", ">"))

    def _parse_pixiv_id(self, message: str) -> str | None:
        parts = message.strip().split(maxsplit=1)
        if len(parts) < 2:
            return None

        text = parts[1].strip()
        if not text:
            return None

        if text.isdecimal():
            return text

        pixiv_re_match = re.search(r"pixiv\.re/(\d+)(?:-\d+)?\.(?:png|jpg|gif)", text, re.IGNORECASE)
        if pixiv_re_match:
            return pixiv_re_match.group(1)

        artwork_match = re.search(r"/(?:artworks|i)/(\d+)", urlparse(text).path)
        if artwork_match:
            return artwork_match.group(1)

        query_match = re.search(r"(?:^|[?&])(?:illust_id|id)=(\d+)(?:&|$)", urlparse(text).query)
        if query_match:
            return query_match.group(1)

        fallback_match = re.search(r"\b(\d{5,})\b", text)
        return fallback_match.group(1) if fallback_match else None

    def _resolve_pixiv_images(self, pixiv_id: str) -> list[str]:
        image_urls: list[str] = []

        base_url = self._first_existing_pixiv_url(pixiv_id)
        if base_url:
            image_urls.append(base_url)
            start_index = 2
        else:
            start_index = 1

        for index in range(start_index, self.pixiv_max_pages + 1):
            page_url = self._first_existing_pixiv_url(f"{pixiv_id}-{index}")
            if page_url is None:
                break
            image_urls.append(page_url)

        if not image_urls:
            raise FileNotFoundError("未找到图片，请确认 PixivID 存在且 pixiv.re 可访问")
        return image_urls

    def _first_existing_pixiv_url(self, pixiv_name: str) -> str | None:
        for extension in self.pixiv_extensions:
            url = f"https://pixiv.re/{pixiv_name}.{extension}"
            if self._url_exists(url):
                return url
        return None

    def _url_exists(self, url: str) -> bool:
        request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "AstrBot/astrbot_plugin_lyn"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return 200 <= response.status < 400 and response.headers.get_content_maintype() == "image"
        except urllib.error.HTTPError as exc:
            if exc.code not in {403, 405}:
                return False
        except (OSError, urllib.error.URLError):
            return False

        request = urllib.request.Request(url, headers={"User-Agent": "AstrBot/astrbot_plugin_lyn"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return 200 <= response.status < 400 and response.headers.get_content_maintype() == "image"
        except (OSError, urllib.error.URLError):
            return False

    def _is_qq_event(self, event: AstrMessageEvent) -> bool:
        return event.get_platform_name() == "aiocqhttp" and hasattr(event, "bot")

    async def _send_pixiv_private_forward(
        self,
        event: AstrMessageEvent,
        sender_id: str,
        image_urls: list[str],
    ) -> None:
        for start in range(0, len(image_urls), self.pixiv_forward_chunk_size):
            urls = image_urls[start:start + self.pixiv_forward_chunk_size]
            nodes = [
                Comp.Node(
                    uin=event.get_self_id() or "0",
                    name="Pixiv",
                    content=[Comp.Image.fromURL(url)],
                )
                for url in urls
            ]
            chain = MessageChain([Comp.Nodes(nodes)])
            await event.send_message(
                bot=event.bot,
                message_chain=chain,
                is_group=False,
                session_id=sender_id,
            )

    async def terminate(self):
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cleanup_task
