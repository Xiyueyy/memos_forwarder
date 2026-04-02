from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from astrbot.api import logger

from .card_renderer import MemoCardRenderer
from .config import MemosWorkspaceForwarderConfig, SourceConfig


@dataclass(slots=True)
class DispatchResult:
    success_count: int = 0
    failure_count: int = 0
    skipped_disabled_count: int = 0


class MemoDispatcher:
    def __init__(self, context, config: MemosWorkspaceForwarderConfig, renderer: MemoCardRenderer | None = None) -> None:
        self.context = context
        self._config = config
        self._renderer = renderer
        self._source_map = {source.id: source for source in config.sources if source.enabled}
        self._target_map = {
            target.id: target for target in config.targets if target.enabled and target.unified_msg_origin
        }
        self._job_target_origins = {
            job.id: [
                self._normalize_origin(self._target_map[target_id].unified_msg_origin)
                for target_id in job.target_ids
                if target_id in self._target_map
            ]
            for job in config.jobs
            if job.enabled
        }
        self._disabled_origins: set[str] = set()

    @staticmethod
    def _normalize_origin(origin: str) -> str:
        text = str(origin or "").strip()
        if not text:
            return ""

        parts = text.split(":", 2)
        if len(parts) != 3:
            return text

        platform_id, message_type, session_id = parts
        type_aliases = {
            "group": "GroupMessage",
            "groupmessage": "GroupMessage",
            "private": "FriendMessage",
            "friend": "FriendMessage",
            "friendmessage": "FriendMessage",
            "user": "FriendMessage",
            "dm": "FriendMessage",
            "other": "OtherMessage",
            "othermessage": "OtherMessage",
        }
        normalized_type = type_aliases.get(message_type.strip().lower(), message_type.strip())
        return f"{platform_id}:{normalized_type}:{session_id}"

    @staticmethod
    def _resolve_messagechain_cls():
        try:
            from astrbot.core.message.message_event_result import MessageChain

            return MessageChain
        except Exception:
            from astrbot.api.message_components import MessageChain

            return MessageChain

    @staticmethod
    def _resolve_plain_cls():
        try:
            from astrbot.api.message_components import Plain

            return Plain
        except Exception:
            from astrbot.core.message.components import Plain

            return Plain

    @staticmethod
    def _resolve_image_cls():
        try:
            from astrbot.api.message_components import Image

            return Image
        except Exception:
            from astrbot.core.message.components import Image

            return Image

    def _resolve_origins(self, item: dict[str, Any]) -> list[str]:
        job_id = str(item.get("job_id", "")).strip()
        if job_id:
            return list(self._job_target_origins.get(job_id, []))
        origins: set[str] = set()
        for values in self._job_target_origins.values():
            origins.update(values)
        return sorted(origins)

    def _announce(self, item: dict[str, Any]) -> str:
        values = {
            "display_name": str(item.get("creator_display_name") or item.get("creator_name") or item.get("creator_username") or "有人").strip(),
            "username": str(item.get("creator_username", "")).strip(),
            "title": str(item.get("title", "")).strip(),
            "summary": str(item.get("summary", "")).strip(),
            "visibility": str(item.get("visibility_label", "")).strip(),
            "published_at": str(item.get("published_at_text", "")).strip(),
            "link": str(item.get("link", "")).strip(),
            "source": str(item.get("source_title", "")).strip(),
        }
        try:
            text = self._config.announcement_template.format(**values).strip()
        except Exception:
            text = str(self._config.announcement_template).strip()
        return text or "有新手记发布"

    def _detail_lines(self, item: dict[str, Any]) -> list[str]:
        title = str(item.get("title", "")).strip() or "(untitled)"
        summary = str(item.get("summary", "")).strip()
        link = str(item.get("link", "")).strip()
        if len(summary) > self._config.summary_max_chars:
            summary = f"{summary[: self._config.summary_max_chars - 1].rstrip()}..."
        lines = [title]
        if item.get("source_title"):
            lines.append(f"来源：{item['source_title']}")
        if item.get("creator_name"):
            lines.append(f"作者：{item['creator_name']}")
        if item.get("visibility_label"):
            lines.append(f"可见性：{item['visibility_label']}")
        if item.get("published_at_text"):
            lines.append(f"时间：{item['published_at_text']}")
        if summary:
            lines.append(summary)
        if link:
            lines.append(link)
        return lines

    async def _build_chain(self, item: dict[str, Any]):
        MessageChain = self._resolve_messagechain_cls()
        Plain = self._resolve_plain_cls()
        Image = self._resolve_image_cls()

        components: list[Any] = []
        announcement = self._announce(item)
        source = self._source_map.get(str(item.get("source_id", "")).strip())
        card_ok = False

        if self._config.render_memo_card and self._renderer is not None:
            try:
                card_path = await self._renderer.render(item, source)
                if announcement:
                    components.append(Plain(announcement))
                components.append(self._local_image(Image, card_path))
                card_ok = True
            except Exception as exc:
                logger.warning("render memo card failed source=%s memo=%s err=%s", item.get("source_id", ""), item.get("id", ""), exc)

        if not card_ok:
            text_lines = [announcement, "", *self._detail_lines(item)] if announcement else self._detail_lines(item)
            components.append(Plain("\n".join(text_lines)))

        need_extra_images = (not card_ok) or (
            self._config.forward_images and self._config.standalone_images_when_card_enabled
        )
        failed = []
        if need_extra_images:
            images, failed = await self._image_components(item, Image)
            components.extend(images)
        if failed:
            components.append(Plain("\n".join(self._failed_image_lines(failed))))

        try:
            return MessageChain(chain=components)
        except TypeError:
            chain = MessageChain()
            if hasattr(chain, "chain"):
                chain.chain.extend(components)
                return chain
            if hasattr(chain, "message"):
                return chain.message("\n".join(self._detail_lines(item)))
            raise

    @staticmethod
    def _local_image(Image, image_path: Path):
        if hasattr(Image, "fromFileSystem"):
            try:
                return Image.fromFileSystem(str(image_path))
            except Exception:
                pass
        return Image(file=str(image_path), path=str(image_path), url=str(image_path))

    async def _image_components(self, item: dict[str, Any], Image) -> tuple[list[Any], list[dict[str, str]]]:
        if not self._config.forward_images or int(self._config.max_images_per_memo) <= 0:
            return [], []
        source = self._source_map.get(str(item.get("source_id", "")).strip())
        entries = item.get("image_entries", [])
        if not isinstance(entries, list):
            return [], []
        comps, failed = [], []
        for entry in entries[: int(self._config.max_images_per_memo)]:
            if not isinstance(entry, dict):
                continue
            url = str(entry.get("url", "")).strip()
            auth = str(entry.get("auth", "none")).strip().lower()
            if not url:
                continue
            try:
                value = await asyncio.to_thread(self._download_as_base64, source, url, auth)
                comps.append(Image(file=value, url=url))
            except Exception as exc:
                logger.warning("download image failed source=%s url=%s err=%s", item.get("source_id", ""), url, exc)
                failed.append({"url": url, "auth": auth})
        return comps, failed

    @staticmethod
    def _failed_image_lines(entries: list[dict[str, str]]) -> list[str]:
        lines = ["部分图片未能自动转发："]
        protected = sum(1 for item in entries if str(item.get("auth", "")).lower() == "bearer")
        if protected:
            lines.append(f"{protected} 张受保护图片下载失败，可能是 /file 鉴权或反代配置问题")
        public_urls = [str(item.get("url", "")).strip() for item in entries if str(item.get("auth", "")).lower() != "bearer"]
        lines.extend([url for url in public_urls[:3] if url])
        remain = len(public_urls) - min(len(public_urls), 3)
        if remain > 0:
            lines.append(f"其余 {remain} 个公开图片地址已省略")
        return lines

    @staticmethod
    def _download_as_base64(source: SourceConfig | None, url: str, auth: str) -> str:
        headers = {"Accept": "image/*,*/*;q=0.8", "User-Agent": "astrbot_plugin_memos_workspace_forwarder/0.2.0"}
        if auth == "bearer" and source is not None and source.access_token:
            headers["Authorization"] = f"Bearer {source.access_token}"
        try:
            with urlopen(Request(url=url, headers=headers), timeout=int(source.timeout) if source is not None else 15) as resp:  # noqa: S310
                data = resp.read()
                content_type = resp.headers.get_content_type()
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore") if exc.fp else ""
            raise RuntimeError(f"HTTP {exc.code} {exc.reason}: {body}") from exc
        except URLError as exc:
            raise RuntimeError(f"network request failed: {exc}") from exc
        if not data:
            raise RuntimeError("empty image response")
        if content_type and not content_type.startswith("image/") and not MemoDispatcher._looks_like_image_url(url):
            raise RuntimeError(f"unexpected content type: {content_type}")
        return f"base64://{base64.b64encode(data).decode('ascii')}"

    @staticmethod
    def _looks_like_image_url(url: str) -> bool:
        text = url.lower()
        return any(tag in text for tag in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic", ".heif"))

    async def dispatch(self, item: dict[str, Any]) -> DispatchResult:
        origins = self._resolve_origins(item)
        if not origins:
            return DispatchResult(skipped_disabled_count=1)
        payload = await self._build_chain(item)
        result = DispatchResult()
        for origin in origins:
            if origin in self._disabled_origins:
                result.skipped_disabled_count += 1
                continue
            try:
                await self.context.send_message(origin, payload)
                result.success_count += 1
            except Exception as exc:
                logger.error("send memos message failed origin=%s: %s", origin, exc)
                result.failure_count += 1
        return result
