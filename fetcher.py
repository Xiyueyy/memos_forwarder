from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin
from urllib.request import Request, urlopen

from astrbot.api import logger

from .config import MemosWorkspaceForwarderConfig, SourceConfig
from .storage import MemoForwarderStorage


class MemosFetcher:
    """Fetch memos from the Memos API with bearer auth."""

    _VISIBILITY_FILTERS = {
        "workspace": 'visibility == "PROTECTED"',
        "protected": 'visibility == "PROTECTED"',
        "public": 'visibility == "PUBLIC"',
        "private": 'visibility == "PRIVATE"',
        "workspace_or_public": 'visibility in ["PROTECTED", "PUBLIC"]',
        "private_or_workspace": 'visibility in ["PRIVATE", "PROTECTED"]',
        "private_or_public": 'visibility in ["PRIVATE", "PUBLIC"]',
        "workspace_or_protected": 'visibility in ["PRIVATE", "PROTECTED"]',
        "all_mine": "",
    }

    _VISIBILITY_LABELS = {
        "PRIVATE": "私有",
        "PROTECTED": "工作区",
        "PUBLIC": "公开",
    }

    _MARKDOWN_IMAGE_RE = re.compile(
        r"!\[[^\]]*\]\((?:<)?(?P<url>[^)\s>]+)(?:\s+\"[^\"]*\")?(?:>)?\)",
        re.IGNORECASE,
    )
    _HTML_IMAGE_RE = re.compile(
        r"<img\b[^>]*?\bsrc=[\"'](?P<url>[^\"']+)[\"']",
        re.IGNORECASE,
    )
    _RAW_IMAGE_URL_RE = re.compile(
        r"(?P<url>https?://[^\s<>()]+?\.(?:png|jpe?g|gif|webp|bmp|heic|heif)(?:\?[^\s<>()]+)?)",
        re.IGNORECASE,
    )

    def __init__(
        self,
        config: MemosWorkspaceForwarderConfig,
        storage: MemoForwarderStorage,
    ) -> None:
        self._config = config
        self._storage = storage
        self._resolved_creator_names: dict[str, str] = {}
        self._resolved_creator_refs: dict[str, str] = {}
        self._user_cache: dict[tuple[str, str], dict[str, str]] = {}

    def get_resolved_creator_name(self, source_id: str) -> str:
        return self._resolved_creator_names.get(source_id, "")

    def get_resolved_creator_ref(self, source_id: str) -> str:
        return self._resolved_creator_refs.get(source_id, "")

    async def fetch(self, job) -> list[dict[str, Any]]:
        source_ids = list(getattr(job, "source_ids", []) or [])
        return await self.fetch_source_ids(source_ids)

    async def fetch_source_ids(self, source_ids: list[str]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        source_map = {source.id: source for source in self._config.sources if source.enabled}
        for source_id in source_ids:
            source = source_map.get(source_id)
            if source is None:
                continue
            items.extend(await self._fetch_single_source(source))
        return items

    async def _fetch_single_source(self, source: SourceConfig) -> list[dict[str, Any]]:
        try:
            creator_name = source.creator_name.strip()
            creator_ref = self._normalize_creator_ref(creator_name)
            self._resolved_creator_names[source.id] = creator_name
            self._resolved_creator_refs[source.id] = creator_ref

            items: list[dict[str, Any]] = []
            page_token = ""
            max_pages = max(int(source.max_pages), 1)
            for _ in range(max_pages):
                payload = await asyncio.to_thread(
                    self._request_memos_page,
                    source,
                    page_token,
                )
                memos = payload.get("memos", [])
                if isinstance(memos, list):
                    for memo in memos:
                        if not isinstance(memo, dict):
                            continue
                        if creator_name and not self._memo_matches_creator(
                            memo,
                            creator_name,
                            creator_ref,
                        ):
                            continue
                        item = await self._convert_memo_to_item(source, memo, creator_name)
                        if item:
                            items.append(item)

                page_token = str(payload.get("nextPageToken", "")).strip()
                if not page_token:
                    break

            return items
        except Exception as exc:
            logger.warning("fetch source=%s failed: %s", source.id, exc)
            return []

    def _request_memos_page(
        self,
        source: SourceConfig,
        page_token: str,
    ) -> dict[str, Any]:
        query = {
            "pageSize": max(1, min(int(source.page_size), 1000)),
            "orderBy": "display_time desc",
        }
        if page_token:
            query["pageToken"] = page_token
        memo_filter = self._build_filter(source)
        if memo_filter:
            query["filter"] = memo_filter
        url = f"{source.base_url}/api/v1/memos?{urlencode(query)}"
        return self._request_json(
            url,
            headers=self._build_headers(source),
            timeout=source.timeout,
        )

    def _build_filter(self, source: SourceConfig) -> str:
        clauses: list[str] = []

        visibility_clause = self._VISIBILITY_FILTERS.get(source.visibility_mode, "")
        if visibility_clause:
            clauses.append(visibility_clause)

        if source.raw_filter:
            clauses.append(source.raw_filter)

        return " && ".join(f"({clause})" for clause in clauses if clause)

    def _memo_matches_creator(
        self,
        memo: dict[str, Any],
        creator_name: str,
        creator_ref: str,
    ) -> bool:
        expected_values = {
            value
            for value in {
                creator_name.strip(),
                creator_ref.strip(),
                f"users/{creator_name.strip()}" if creator_name.strip() else "",
            }
            if value
        }
        if not expected_values:
            return True

        memo_creator = str(memo.get("creator", "")).strip()
        if memo_creator in expected_values:
            return True

        memo_creator_username = self._extract_username(memo_creator)
        return memo_creator_username in expected_values

    async def _convert_memo_to_item(
        self,
        source: SourceConfig,
        memo: dict[str, Any],
        fallback_creator_name: str,
    ) -> dict[str, Any] | None:
        memo_name = str(memo.get("name", "")).strip()
        if not memo_name:
            return None

        creator_ref = str(memo.get("creator", "")).strip()
        creator_profile = await self._get_creator_profile(source, creator_ref)
        creator_display_name = (
            creator_profile.get("display_name")
            or creator_profile.get("username")
            or self._extract_username(creator_ref)
            or fallback_creator_name
            or "未知用户"
        )
        creator_username = (
            creator_profile.get("username")
            or self._extract_username(creator_ref)
            or fallback_creator_name
        )

        visibility = str(memo.get("visibility", "")).strip().upper()
        display_time = (
            str(memo.get("displayTime", "")).strip()
            or str(memo.get("updateTime", "")).strip()
            or str(memo.get("createTime", "")).strip()
        )
        snippet = str(memo.get("snippet", "")).strip()
        content = str(memo.get("content", "")).strip()
        title = self._extract_title(memo, snippet, content, memo_name)
        summary = snippet or self._build_fallback_summary(content)
        image_entries = self._extract_image_entries(source, memo, content)

        return {
            "id": memo_name,
            "memo_name": memo_name,
            "source_id": source.id,
            "source_title": self._build_source_title(source, fallback_creator_name),
            "title": title,
            "summary": summary,
            "content": content,
            "link": f"{source.base_url}/{memo_name}",
            "creator_name": creator_display_name,
            "creator_display_name": creator_display_name,
            "creator_username": creator_username,
            "creator_avatar_url": creator_profile.get("avatar_url", ""),
            "creator_resource_name": creator_ref,
            "visibility": visibility,
            "visibility_label": self._VISIBILITY_LABELS.get(visibility, visibility or "未知"),
            "published_at": display_time,
            "published_at_text": self._format_time_text(display_time),
            "image_entries": image_entries,
            "image_count": len(image_entries),
        }

    async def _get_creator_profile(
        self,
        source: SourceConfig,
        creator_ref: str,
    ) -> dict[str, str]:
        normalized_ref = creator_ref.strip()
        if not normalized_ref:
            return {"display_name": "", "username": "", "avatar_url": ""}

        cache_key = (source.base_url, normalized_ref)
        cached = self._user_cache.get(cache_key)
        if cached is not None:
            return cached

        url = f"{source.base_url}/api/v1/{quote(normalized_ref, safe='/')}"
        try:
            payload = await asyncio.to_thread(
                self._request_json,
                url,
                self._build_headers(source),
                source.timeout,
            )
        except Exception as exc:
            logger.debug("fetch creator profile failed source=%s creator=%s err=%s", source.id, creator_ref, exc)
            profile = {
                "display_name": self._extract_username(normalized_ref),
                "username": self._extract_username(normalized_ref),
                "avatar_url": "",
            }
            self._user_cache[cache_key] = profile
            return profile

        avatar_url = self._normalize_external_url(source, str(payload.get("avatarUrl", "")).strip())
        profile = {
            "display_name": str(payload.get("displayName", "")).strip(),
            "username": str(payload.get("username", "")).strip() or self._extract_username(normalized_ref),
            "avatar_url": avatar_url,
        }
        self._user_cache[cache_key] = profile
        return profile

    def _extract_image_entries(
        self,
        source: SourceConfig,
        memo: dict[str, Any],
        content: str,
    ) -> list[dict[str, str]]:
        entries: list[dict[str, str]] = []
        seen_urls: set[str] = set()

        attachments = memo.get("attachments", [])
        if isinstance(attachments, list):
            for attachment in attachments:
                if not isinstance(attachment, dict):
                    continue
                if not self._is_image_attachment(attachment):
                    continue
                url = self._build_attachment_url(source, attachment)
                self._append_image_entry(
                    entries,
                    seen_urls,
                    url=url,
                    auth=self._determine_url_auth(source, url),
                )

        for raw_url in self._iter_inline_image_urls(content):
            normalized_url = self._normalize_external_url(source, raw_url)
            self._append_image_entry(
                entries,
                seen_urls,
                url=normalized_url,
                auth=self._determine_url_auth(source, normalized_url),
            )

        return entries

    def _append_image_entry(
        self,
        entries: list[dict[str, str]],
        seen_urls: set[str],
        *,
        url: str,
        auth: str,
    ) -> None:
        normalized_url = url.strip()
        if not normalized_url or normalized_url in seen_urls:
            return
        seen_urls.add(normalized_url)
        entries.append({"url": normalized_url, "auth": auth})

    def _iter_inline_image_urls(self, content: str) -> list[str]:
        urls: list[str] = []
        for pattern in (
            self._MARKDOWN_IMAGE_RE,
            self._HTML_IMAGE_RE,
            self._RAW_IMAGE_URL_RE,
        ):
            for match in pattern.finditer(content):
                url = str(match.group("url") or "").strip()
                if url:
                    urls.append(url)
        return urls

    @staticmethod
    def _is_image_attachment(attachment: dict[str, Any]) -> bool:
        mime_type = str(attachment.get("type", "")).strip().lower()
        return mime_type.startswith("image/") and mime_type != "image/svg+xml"

    @staticmethod
    def _build_attachment_url(source: SourceConfig, attachment: dict[str, Any]) -> str:
        external_link = str(attachment.get("externalLink", "")).strip()
        if external_link:
            return external_link

        attachment_name = str(attachment.get("name", "")).strip()
        filename = str(attachment.get("filename", "")).strip()
        if not attachment_name or not filename:
            return ""

        quoted_name = quote(attachment_name, safe="/")
        quoted_filename = quote(filename, safe="")
        return f"{source.base_url}/file/{quoted_name}/{quoted_filename}"

    @staticmethod
    def _normalize_external_url(source: SourceConfig, url: str) -> str:
        text = str(url or "").strip().strip("<>").strip()
        if not text:
            return ""
        if text.startswith("http://") or text.startswith("https://"):
            return text
        if text.startswith("//"):
            scheme = "https" if source.base_url.startswith("https://") else "http"
            return f"{scheme}:{text}"
        return urljoin(f"{source.base_url}/", text.lstrip("/"))

    @staticmethod
    def _determine_url_auth(source: SourceConfig, url: str) -> str:
        if url.startswith(f"{source.base_url}/file/") and source.access_token:
            return "bearer"
        return "none"

    @staticmethod
    def _extract_title(
        memo: dict[str, Any],
        snippet: str,
        content: str,
        memo_name: str,
    ) -> str:
        _ = snippet, content, memo_name
        prop = memo.get("property", {})
        if isinstance(prop, dict):
            title = str(prop.get("title", "")).strip()
            if title:
                return title
        return ""

    @staticmethod
    def _build_fallback_summary(content: str) -> str:
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        return " ".join(lines)[:280].strip()

    def _format_time_text(self, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return text
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        try:
            return parsed.astimezone().strftime(self._config.time_format)
        except Exception:
            return parsed.isoformat()

    @staticmethod
    def _extract_username(resource_name: str) -> str:
        if resource_name.startswith("users/"):
            return resource_name.split("/", 1)[-1].strip()
        return resource_name

    @classmethod
    def _normalize_creator_ref(cls, creator_name: str) -> str:
        text = creator_name.strip()
        if not text:
            return ""
        if text.startswith("users/"):
            return text
        return f"users/{text}"

    @staticmethod
    def _build_source_title(source: SourceConfig, creator_name: str) -> str:
        if creator_name:
            return f"Memos / {creator_name}"
        return f"Memos / {source.id}"

    @staticmethod
    def _build_headers(source: SourceConfig) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {source.access_token}",
            "Accept": "application/json",
            "User-Agent": "astrbot_plugin_memos_workspace_forwarder/0.1.0",
        }

    @staticmethod
    def _request_json(url: str, headers: dict[str, str], timeout: int) -> dict[str, Any]:
        request = Request(url=url, headers=headers)
        try:
            with urlopen(request, timeout=timeout) as response:  # noqa: S310
                body = response.read().decode("utf-8", errors="ignore")
                payload = json.loads(body or "{}")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore") if exc.fp else ""
            raise RuntimeError(f"HTTP {exc.code} {exc.reason}: {body}") from exc
        except URLError as exc:
            raise RuntimeError(f"网络请求失败: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"返回的 JSON 无法解析: {exc}") from exc

        if not isinstance(payload, dict):
            raise RuntimeError("返回结果不是对象")
        return payload
