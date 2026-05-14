from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import shutil
import time
import uuid
from pathlib import Path

import astrbot.api.message_components as Comp
import jmcomic
import yaml
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path


class DownloadSizeLimitExceeded(Exception):
    pass


@register("astrbot_plugin_jm", "Lemoec", "下载 JM 漫画并转换为 PDF 发送", "2.0.0")
class JmPlugin(Star):
    jm_help_msg = "使用方式: /jm <漫画ID>"
    jm_search_help_msg = "使用方式: /jm搜索 <关键词1> <关键词2>..."
    jm_failed_msg = "hentai！一天到晚看本子真是没救了喵"
    cleanup_interval_seconds = 24 * 60 * 60
    download_ttl_seconds = 24 * 60 * 60
    max_download_bytes = 150 * 1024 * 1024
    search_limit = 10

    def __init__(self, context: Context):
        super().__init__(context)
        self.data_dir = Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_jm"
        self.pdf_root = self.data_dir / "pdf"
        self.option_file = self.data_dir / "jm_option.yml"
        self._download_locks: dict[int, asyncio.Lock] = {}
        self._cleanup_task: asyncio.Task | None = None

    async def initialize(self):
        self.pdf_root.mkdir(parents=True, exist_ok=True)
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

    async def terminate(self):
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cleanup_task
