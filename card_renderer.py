from __future__ import annotations

import asyncio
import hashlib
import html
import math
import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from astrbot.api import logger

from .config import MemosWorkspaceForwarderConfig, SourceConfig

try:  # pragma: no cover
    from PIL import Image, ImageDraw, ImageFont, ImageOps
except ImportError:  # pragma: no cover
    Image = ImageDraw = ImageFont = ImageOps = None


@dataclass(slots=True)
class _EmojiFontHandle:
    font: Any
    target_size: int
    native_size: int


class MemoCardRenderer:
    """Render a memo card that looks closer to the native Memos post card."""

    _CANVAS_WIDTH = 900
    _PAGE_PADDING = 18
    _CARD_PADDING_X = 16
    _CARD_PADDING_Y = 14
    _AVATAR_SIZE = 32
    _HEADER_GAP = 12
    _SECTION_GAP = 12
    _IMAGE_GAP = 10
    _SINGLE_IMAGE_MAX_HEIGHT = 420
    _GRID_IMAGE_HEIGHT = 190
    _BODY_MAX_CHARS = 2600
    _MAX_DOWNLOAD_BYTES = 12 * 1024 * 1024

    _MARKDOWN_IMAGE_RE = re.compile(
        r"!\[[^\]]*\]\((?:<)?(?P<url>[^)\s>]+)(?:\s+\"[^\"]*\")?(?:>)?\)",
        re.IGNORECASE,
    )
    _HTML_IMAGE_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
    _MARKDOWN_LINK_RE = re.compile(r"\[(?P<text>[^\]]+)\]\((?:<)?[^)\s>]+(?:>)?\)")
    _HTML_BREAK_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
    _HTML_BLOCK_CLOSE_RE = re.compile(r"</(?:p|div|li|blockquote|h[1-6])>", re.IGNORECASE)
    _HTML_TAG_RE = re.compile(r"<[^>]+>")
    _CODE_FENCE_RE = re.compile(r"```(?:[^\n`]*)\n?(?P<code>.*?)```", re.DOTALL)
    _INLINE_CODE_RE = re.compile(r"`([^`]+)`")

    _REGULAR_FONT_CANDIDATES = (
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simsun.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKSC-Regular.otf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    )
    _BOLD_FONT_CANDIDATES = (
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/msyh.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKSC-Bold.otf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    )
    _EMOJI_FONT_CANDIDATES = (
        "C:/Windows/Fonts/seguiemj.ttf",
        "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
        "/usr/share/fonts/emoji/NotoColorEmoji.ttf",
    )
    _EMOJI_FONT_FIXED_SIZES = (109, 128, 136, 160, 96, 72)

    def __init__(self, config: MemosWorkspaceForwarderConfig, cache_root: str | Path) -> None:
        self._config = config
        self._cache_dir = Path(cache_root) / "rendered_cards"
        self._font_cache: dict[tuple[bool, int], Any] = {}
        self._emoji_font_cache: dict[int, Any] = {}

    async def render(self, item: dict[str, Any], source: SourceConfig | None) -> Path:
        if Image is None:
            raise RuntimeError("Pillow is not installed")

        self._cache_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha1(
            "|".join(str(item.get(k, "")).strip() for k in ("id", "published_at", "title")).encode("utf-8")
        ).hexdigest()[:20]
        output_path = self._cache_dir / f"memo_card_{digest}.png"
        await asyncio.to_thread(self._render_sync, item, source, output_path)
        return output_path

    def _render_sync(self, item: dict[str, Any], source: SourceConfig | None, output_path: Path) -> None:
        avatar = self._load_image(
            str(item.get("creator_avatar_url", "")).strip(),
            source=source,
            attach_bearer=True,
        )
        previews = self._load_preview_images(item, source)

        colors = {
            "page_bg": (246, 247, 249, 255),
            "card_bg": (255, 255, 255, 255),
            "border": (229, 231, 235, 255),
            "name": (107, 114, 128, 255),
            "time": (156, 163, 175, 255),
            "body": (31, 41, 55, 255),
            "subtle": (156, 163, 175, 255),
            "avatar_border": (229, 231, 235, 255),
        }

        name_font = self._font(16, bold=False)
        time_font = self._font(12, bold=False)
        body_font = self._font(17, bold=False)
        heading_font = self._font(20, bold=True)
        badge_font = self._font(12, bold=False)
        placeholder_font = self._font(14, bold=True)

        card_width = self._CANVAS_WIDTH - self._PAGE_PADDING * 2
        content_width = card_width - self._CARD_PADDING_X * 2
        header_text_x = self._CARD_PADDING_X + self._AVATAR_SIZE + self._HEADER_GAP
        header_text_width = content_width - self._AVATAR_SIZE - self._HEADER_GAP - 74

        scratch = Image.new("RGBA", (self._CANVAS_WIDTH, 200), (255, 255, 255, 0))
        scratch_draw = ImageDraw.Draw(scratch)

        display_name = str(
            item.get("creator_display_name")
            or item.get("creator_name")
            or item.get("creator_username")
            or "未知用户"
        ).strip()
        time_text = str(item.get("published_at_text", "")).strip()
        visibility_text = str(item.get("visibility_label", "")).strip()

        title = self._select_distinct_title(item)
        content = self._clean_body_text(
            str(item.get("content", "")).strip() or str(item.get("summary", "")).strip()
        )

        name_lines = self._wrap_text(scratch_draw, display_name, name_font, header_text_width, max_lines=2)
        time_lines = self._wrap_text(scratch_draw, time_text, time_font, header_text_width, max_lines=2)
        title_lines = self._wrap_paragraphs(scratch_draw, title, heading_font, content_width, max_lines=3) if title else []
        body_lines = self._wrap_paragraphs(scratch_draw, content, body_font, content_width, max_lines=42)

        name_line_height = self._line_height(scratch_draw, name_font)
        time_line_height = self._line_height(scratch_draw, time_font)
        heading_line_height = self._line_height(scratch_draw, heading_font)
        body_line_height = self._line_height(scratch_draw, body_font)

        header_height = max(
            self._AVATAR_SIZE,
            self._measure_lines_height(name_lines, name_line_height, line_gap=4, paragraph_gap=0)
            + (4 if time_lines else 0)
            + self._measure_lines_height(time_lines, time_line_height, line_gap=2, paragraph_gap=0),
        )
        title_height = self._measure_lines_height(title_lines, heading_line_height, line_gap=6, paragraph_gap=10)
        body_height = self._measure_lines_height(body_lines, body_line_height, line_gap=8, paragraph_gap=14)
        preview_height = self._measure_preview_height(previews, content_width)

        card_height = self._CARD_PADDING_Y * 2 + header_height + body_height
        if title_height:
            card_height += title_height + self._SECTION_GAP
        if preview_height:
            card_height += preview_height + self._SECTION_GAP

        canvas_height = card_height + self._PAGE_PADDING * 2
        canvas = Image.new("RGBA", (self._CANVAS_WIDTH, canvas_height), colors["page_bg"])
        draw = ImageDraw.Draw(canvas)

        card_left = self._PAGE_PADDING
        card_top = self._PAGE_PADDING
        card_right = card_left + card_width
        card_bottom = card_top + card_height

        draw.rounded_rectangle(
            (card_left, card_top, card_right, card_bottom),
            radius=14,
            fill=colors["card_bg"],
            outline=colors["border"],
            width=1,
        )

        content_left = card_left + self._CARD_PADDING_X
        current_y = card_top + self._CARD_PADDING_Y

        self._draw_avatar(
            canvas,
            draw,
            avatar,
            content_left,
            current_y,
            colors,
            display_name,
            placeholder_font,
        )

        text_left = content_left + self._AVATAR_SIZE + self._HEADER_GAP
        self._draw_wrapped_lines(
            canvas,
            draw,
            name_lines,
            name_font,
            text_left,
            current_y,
            fill=colors["name"],
            line_gap=4,
            paragraph_gap=0,
        )
        time_y = current_y + self._measure_lines_height(name_lines, name_line_height, line_gap=4, paragraph_gap=0) + 2
        self._draw_wrapped_lines(
            canvas,
            draw,
            time_lines,
            time_font,
            text_left,
            time_y,
            fill=colors["time"],
            line_gap=2,
            paragraph_gap=0,
        )

        if visibility_text:
            self._draw_visibility_badge(
                draw,
                visibility_text,
                card_right - self._CARD_PADDING_X,
                current_y + 1,
                badge_font,
            )

        current_y += header_height + self._SECTION_GAP

        if title_lines:
            self._draw_wrapped_lines(
                canvas,
                draw,
                title_lines,
                heading_font,
                content_left,
                current_y,
                fill=colors["body"],
                line_gap=6,
                paragraph_gap=10,
            )
            current_y += title_height + self._SECTION_GAP

        self._draw_wrapped_lines(
            canvas,
            draw,
            body_lines,
            body_font,
            content_left,
            current_y,
            fill=colors["body"],
            line_gap=8,
            paragraph_gap=14,
        )
        current_y += body_height

        if preview_height:
            current_y += self._SECTION_GAP
            self._draw_previews(
                canvas,
                draw,
                previews,
                content_left,
                current_y,
                content_width,
                total_count=int(item.get("image_count", 0) or 0),
            )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(output_path, format="PNG", optimize=True)

    def _load_preview_images(self, item: dict[str, Any], source: SourceConfig | None) -> list[Any]:
        previews: list[Any] = []
        entries = item.get("image_entries", [])
        if not isinstance(entries, list):
            return previews

        max_count = max(int(self._config.card_preview_image_count), 0)
        if max_count <= 0:
            return previews

        for entry in entries[:max_count]:
            if not isinstance(entry, dict):
                continue
            url = str(entry.get("url", "")).strip()
            auth = str(entry.get("auth", "none")).strip().lower()
            if not url:
                continue
            image = self._load_image(url, source=source, attach_bearer=(auth == "bearer"))
            if image is not None:
                previews.append(image)
        return previews

    def _load_image(self, url: str, *, source: SourceConfig | None, attach_bearer: bool):
        if not url or Image is None:
            return None

        headers = {
            "Accept": "image/*,*/*;q=0.8",
            "User-Agent": "astrbot_plugin_memos_workspace_forwarder/0.2.0",
        }
        if (
            attach_bearer
            and source is not None
            and source.access_token
            and self._same_site(source.base_url, url)
        ):
            headers["Authorization"] = f"Bearer {source.access_token}"

        try:
            with urlopen(
                Request(url=url, headers=headers),
                timeout=int(source.timeout) if source is not None else 15,
            ) as response:  # noqa: S310
                data = response.read(self._MAX_DOWNLOAD_BYTES + 1)
        except Exception as exc:
            logger.debug("load card image failed url=%s err=%s", url, exc)
            return None

        if not data or len(data) > self._MAX_DOWNLOAD_BYTES:
            return None

        try:
            return Image.open(BytesIO(data)).convert("RGBA")
        except Exception as exc:
            logger.debug("decode card image failed url=%s err=%s", url, exc)
            return None

    def _draw_avatar(
        self,
        canvas,
        draw,
        avatar,
        left: int,
        top: int,
        colors: dict[str, tuple[int, int, int, int]],
        display_name: str,
        placeholder_font,
    ) -> None:
        size = self._AVATAR_SIZE
        box = (left, top, left + size, top + size)
        radius = 10
        if avatar is None:
            draw.rounded_rectangle(box, radius=radius, fill=(243, 244, 246, 255), outline=colors["avatar_border"], width=1)
            text = (display_name or "M")[:2]
            bbox = draw.textbbox((0, 0), text, font=placeholder_font)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]
            draw.text(
                (left + (size - text_w) / 2, top + (size - text_h) / 2 - bbox[1]),
                text,
                font=placeholder_font,
                fill=(107, 114, 128, 255),
            )
            return

        avatar = ImageOps.fit(avatar, (size, size), method=self._resampling())
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, size, size), radius=radius, fill=255)
        avatar.putalpha(mask)
        canvas.alpha_composite(avatar, (left, top))
        draw.rounded_rectangle(box, radius=radius, outline=colors["avatar_border"], width=1)

    def _draw_visibility_badge(self, draw, text: str, right: int, top: int, font) -> None:
        color_map = {
            "公开": ((239, 246, 255, 255), (37, 99, 235, 255), (191, 219, 254, 255)),
            "工作区": ((255, 247, 237, 255), (180, 83, 9, 255), (253, 230, 138, 255)),
            "私有": ((254, 242, 242, 255), (185, 28, 28, 255), (254, 202, 202, 255)),
        }
        bg, fg, border = color_map.get(text, ((243, 244, 246, 255), (75, 85, 99, 255), (229, 231, 235, 255)))
        bbox = draw.textbbox((0, 0), text, font=font)
        width = bbox[2] - bbox[0] + 16
        height = bbox[3] - bbox[1] + 8
        left = right - width
        draw.rounded_rectangle((left, top, right, top + height), radius=height // 2, fill=bg, outline=border, width=1)
        draw.text((left + 8, top + 4 - bbox[1]), text, font=font, fill=fg)

    def _draw_previews(
        self,
        canvas,
        draw,
        previews: list[Any],
        left: int,
        top: int,
        width: int,
        *,
        total_count: int,
    ) -> None:
        if not previews:
            return

        if len(previews) == 1:
            self._draw_single_preview(canvas, previews[0], left, top, width)
            return

        tile_width = (width - self._IMAGE_GAP) // 2
        for index, image in enumerate(previews):
            row = index // 2
            col = index % 2
            x = left + col * (tile_width + self._IMAGE_GAP)
            y = top + row * (self._GRID_IMAGE_HEIGHT + self._IMAGE_GAP)
            self._draw_grid_preview(canvas, draw, image, x, y, tile_width, self._GRID_IMAGE_HEIGHT)

            extra_count = total_count - len(previews)
            if extra_count > 0 and index == len(previews) - 1:
                self._draw_more_overlay(canvas, draw, x, y, tile_width, self._GRID_IMAGE_HEIGHT, extra_count)

    def _draw_single_preview(self, canvas, image, left: int, top: int, max_width: int) -> None:
        image = image.copy()
        image.thumbnail((max_width, self._SINGLE_IMAGE_MAX_HEIGHT), self._resampling())
        width, height = image.size
        mask = Image.new("L", (width, height), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, width, height), radius=12, fill=255)
        image.putalpha(mask)
        canvas.alpha_composite(image, (left, top))

    def _draw_grid_preview(self, canvas, draw, image, left: int, top: int, width: int, height: int) -> None:
        preview = ImageOps.fit(image, (width, height), method=self._resampling())
        mask = Image.new("L", (width, height), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, width, height), radius=12, fill=255)
        preview.putalpha(mask)
        canvas.alpha_composite(preview, (left, top))

    def _draw_more_overlay(self, canvas, draw, left: int, top: int, width: int, height: int, count: int) -> None:
        overlay = Image.new("RGBA", (width, height), (17, 24, 39, 86))
        mask = Image.new("L", (width, height), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, width, height), radius=12, fill=255)
        overlay.putalpha(mask)
        canvas.alpha_composite(overlay, (left, top))

        font = self._font(26, bold=True)
        text = f"+{count} 张"
        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        draw.text(
            (left + (width - text_w) / 2, top + (height - text_h) / 2 - bbox[1]),
            text,
            font=font,
            fill=(255, 255, 255, 255),
        )

    def _measure_preview_height(self, previews: list[Any], width: int) -> int:
        if not previews:
            return 0
        if len(previews) == 1:
            image = previews[0]
            ratio = image.height / max(image.width, 1)
            return min(self._SINGLE_IMAGE_MAX_HEIGHT, max(180, int(width * ratio)))
        rows = math.ceil(len(previews) / 2)
        return rows * self._GRID_IMAGE_HEIGHT + max(rows - 1, 0) * self._IMAGE_GAP

    def _wrap_text(self, draw, text: str, font, max_width: int, *, max_lines: int) -> list[str]:
        text = str(text or "").strip()
        if not text:
            return []

        lines: list[str] = []
        current = ""
        for char in text:
            candidate = f"{current}{char}"
            if self._measure_text_width(draw, candidate, font) <= max_width or not current:
                current = candidate
                continue
            lines.append(current.rstrip())
            current = char.lstrip()
            if len(lines) >= max_lines:
                return self._truncate_lines(draw, lines, font, max_width, max_lines)
        if current:
            lines.append(current.rstrip())
        if len(lines) > max_lines:
            lines = lines[:max_lines]
        if len(lines) == max_lines and sum(len(line) for line in lines) < len(text):
            lines = self._truncate_lines(draw, lines, font, max_width, max_lines)
        return lines

    def _wrap_paragraphs(self, draw, text: str, font, max_width: int, *, max_lines: int) -> list[str]:
        text = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not text:
            return []

        result: list[str] = []
        paragraphs = [part.strip() for part in text.split("\n")]
        visible_count = 0
        for index, paragraph in enumerate(paragraphs):
            if not paragraph:
                if result and result[-1] != "":
                    result.append("")
                continue

            wrapped = self._wrap_text(draw, paragraph, font, max_width, max_lines=max_lines - visible_count)
            if not wrapped:
                continue
            result.extend(wrapped)
            visible_count += len([line for line in wrapped if line])
            if visible_count >= max_lines:
                break
            if index != len(paragraphs) - 1 and result and result[-1] != "":
                result.append("")

        plain_lines = [line for line in result if line]
        if len(plain_lines) >= max_lines:
            consumed = sum(len(line.rstrip(".")) for line in plain_lines[:max_lines])
            if consumed < len(text.replace("\n", "")):
                result = self._truncate_paragraph_result(draw, result, font, max_width)

        return result or ["这条手记没有文字内容。"]

    def _truncate_lines(self, draw, lines: list[str], font, max_width: int, max_lines: int) -> list[str]:
        lines = lines[:max_lines]
        if not lines:
            return ["..."]
        last = lines[-1].rstrip()
        while last and self._measure_text_width(draw, f"{last}...", font) > max_width:
            last = last[:-1]
        lines[-1] = f"{last}..." if last else "..."
        return lines

    def _truncate_paragraph_result(self, draw, lines: list[str], font, max_width: int) -> list[str]:
        for index in range(len(lines) - 1, -1, -1):
            if not lines[index]:
                continue
            line = lines[index].rstrip()
            while line and self._measure_text_width(draw, f"{line}...", font) > max_width:
                line = line[:-1]
            lines[index] = f"{line}..." if line else "..."
            return lines[: index + 1]
        return ["..."]

    def _line_height(self, draw, font) -> int:
        bbox = self._measure_text_bbox(draw, "中Ay🙂", font)
        return max(bbox[3] - bbox[1], 1)

    @staticmethod
    def _measure_lines_height(lines: list[str], line_height: int, *, line_gap: int, paragraph_gap: int) -> int:
        if not lines:
            return 0
        height = 0
        previous_was_text = False
        for line in lines:
            if not line:
                if previous_was_text:
                    height += paragraph_gap
                previous_was_text = False
                continue
            if height > 0 and previous_was_text:
                height += line_gap
            height += line_height
            previous_was_text = True
        return height

    def _draw_wrapped_lines(
        self,
        canvas,
        draw,
        lines: list[str],
        font,
        left: int,
        top: int,
        *,
        fill: tuple[int, int, int, int],
        line_gap: int,
        paragraph_gap: int,
    ) -> None:
        current_y = top
        previous_was_text = False
        line_height = self._line_height(draw, font)
        for line in lines:
            if not line:
                if previous_was_text:
                    current_y += paragraph_gap
                previous_was_text = False
                continue
            if previous_was_text:
                current_y += line_gap
            self._draw_text_with_fallback(canvas, draw, (left, current_y), line, font, fill)
            current_y += line_height
            previous_was_text = True

    def _clean_body_text(self, text: str) -> str:
        if not text:
            return "这条手记没有文字内容。"

        text = html.unescape(text)
        text = self._CODE_FENCE_RE.sub(lambda match: match.group("code").strip(), text)
        text = self._INLINE_CODE_RE.sub(r"\1", text)
        text = self._MARKDOWN_IMAGE_RE.sub("", text)
        text = self._HTML_IMAGE_RE.sub("", text)
        text = self._MARKDOWN_LINK_RE.sub(lambda match: match.group("text"), text)
        text = self._HTML_BREAK_RE.sub("\n", text)
        text = self._HTML_BLOCK_CLOSE_RE.sub("\n", text)
        text = self._HTML_TAG_RE.sub("", text)
        text = text.replace("\u00a0", " ").replace("\t", "    ")
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if len(text) > self._BODY_MAX_CHARS:
            text = f"{text[: self._BODY_MAX_CHARS].rstrip()}\n\n..."
        return text or "这条手记没有文字内容。"

    @staticmethod
    def _select_distinct_title(item: dict[str, Any]) -> str:
        title = str(item.get("title", "")).strip()
        content = str(item.get("content", "")).strip()
        if not title:
            return ""
        if content and title in content[: max(len(title) + 12, 64)]:
            return ""
        return title

    def _font(self, size: int, *, bold: bool):
        cache_key = (bold, size)
        cached = self._font_cache.get(cache_key)
        if cached is not None:
            return cached

        candidates = self._BOLD_FONT_CANDIDATES if bold else self._REGULAR_FONT_CANDIDATES
        font = None
        for candidate in candidates:
            if Path(candidate).exists():
                try:
                    font = ImageFont.truetype(candidate, size=size)
                    break
                except Exception:
                    continue
        if font is None:
            font = ImageFont.load_default()

        self._font_cache[cache_key] = font
        return font

    def _emoji_font(self, size: int):
        cached = self._emoji_font_cache.get(size)
        if cached is not None:
            return cached

        font = None
        for candidate in self._EMOJI_FONT_CANDIDATES:
            if Path(candidate).exists():
                try:
                    native_font = ImageFont.truetype(candidate, size=size)
                    font = _EmojiFontHandle(
                        font=native_font,
                        target_size=size,
                        native_size=size,
                    )
                    break
                except Exception:
                    for fixed_size in self._EMOJI_FONT_FIXED_SIZES:
                        try:
                            native_font = ImageFont.truetype(candidate, size=fixed_size)
                            font = _EmojiFontHandle(
                                font=native_font,
                                target_size=size,
                                native_size=fixed_size,
                            )
                            break
                        except Exception:
                            continue
                    if font is not None:
                        break

        self._emoji_font_cache[size] = font
        return font

    @staticmethod
    def _is_emoji_char(char: str) -> bool:
        if not char:
            return False
        codepoint = ord(char)
        return (
            0x1F1E6 <= codepoint <= 0x1F1FF
            or 0x1F300 <= codepoint <= 0x1FAFF
            or 0x2600 <= codepoint <= 0x27BF
            or codepoint in {0x00A9, 0x00AE, 0x203C, 0x2049, 0x2122, 0x2139, 0x3030, 0x303D, 0x3297, 0x3299}
        )

    @staticmethod
    def _is_emoji_component(char: str) -> bool:
        if not char:
            return False
        codepoint = ord(char)
        return codepoint in {0x200D, 0xFE0E, 0xFE0F, 0x20E3} or 0x1F3FB <= codepoint <= 0x1F3FF

    def _iter_font_runs(self, text: str, font):
        if not text:
            return

        font_size = int(getattr(font, "size", 16) or 16)
        emoji_font = self._emoji_font(font_size)
        if emoji_font is None:
            yield text, font, False
            return

        current_chars: list[str] = []
        current_font = None
        current_is_emoji = False

        for char in text:
            is_emoji = self._is_emoji_char(char) or (
                current_is_emoji and self._is_emoji_component(char)
            )
            target_font = emoji_font if is_emoji else font
            if current_font is target_font and current_is_emoji == is_emoji:
                current_chars.append(char)
                continue

            if current_chars:
                yield "".join(current_chars), current_font, current_is_emoji

            current_chars = [char]
            current_font = target_font
            current_is_emoji = is_emoji

        if current_chars:
            yield "".join(current_chars), current_font, current_is_emoji

    @staticmethod
    def _run_text_length(draw, text: str, font, *, embedded_color: bool) -> float:
        if isinstance(font, _EmojiFontHandle):
            native_font = font.font
            scale = font.target_size / max(font.native_size, 1)
            try:
                return float(
                    draw.textlength(
                        text,
                        font=native_font,
                        embedded_color=embedded_color,
                    )
                ) * scale
            except TypeError:
                return float(draw.textlength(text, font=native_font)) * scale
        try:
            return float(draw.textlength(text, font=font, embedded_color=embedded_color))
        except TypeError:
            return float(draw.textlength(text, font=font))

    @staticmethod
    def _run_text_bbox(draw, text: str, font, *, embedded_color: bool) -> tuple[int, int, int, int]:
        if isinstance(font, _EmojiFontHandle):
            native_font = font.font
            scale = font.target_size / max(font.native_size, 1)
            try:
                bbox = draw.textbbox((0, 0), text, font=native_font, embedded_color=embedded_color)
            except TypeError:
                bbox = draw.textbbox((0, 0), text, font=native_font)
            return tuple(int(round(value * scale)) for value in bbox)
        try:
            return draw.textbbox((0, 0), text, font=font, embedded_color=embedded_color)
        except TypeError:
            return draw.textbbox((0, 0), text, font=font)

    def _draw_text_run(self, canvas, draw, xy: tuple[float, float], text: str, font, fill, *, embedded_color: bool) -> None:
        if isinstance(font, _EmojiFontHandle) and font.native_size != font.target_size:
            native_font = font.font
            scale = font.target_size / max(font.native_size, 1)
            bbox = self._run_text_bbox(draw, text, native_font, embedded_color=embedded_color)
            width = max(bbox[2] - bbox[0], 1)
            height = max(bbox[3] - bbox[1], 1)

            temp = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            temp_draw = ImageDraw.Draw(temp)
            try:
                temp_draw.text(
                    (-bbox[0], -bbox[1]),
                    text,
                    font=native_font,
                    fill=fill,
                    embedded_color=embedded_color,
                )
            except TypeError:
                temp_draw.text((-bbox[0], -bbox[1]), text, font=native_font, fill=fill)

            scaled_width = max(int(round(width * scale)), 1)
            scaled_height = max(int(round(height * scale)), 1)
            scaled = temp.resize((scaled_width, scaled_height), self._resampling())
            paste_x = int(round(xy[0] + bbox[0] * scale))
            paste_y = int(round(xy[1] + bbox[1] * scale))
            canvas.alpha_composite(scaled, (paste_x, paste_y))
            return

        try:
            draw.text(xy, text, font=font, fill=fill, embedded_color=embedded_color)
        except TypeError:
            draw.text(xy, text, font=font, fill=fill)

    def _measure_text_width(self, draw, text: str, font) -> float:
        width = 0.0
        for run_text, run_font, embedded_color in self._iter_font_runs(text, font):
            width += self._run_text_length(draw, run_text, run_font, embedded_color=embedded_color)
        return width

    def _measure_text_bbox(self, draw, text: str, font) -> tuple[int, int, int, int]:
        if not text:
            return (0, 0, 0, 0)

        cursor_x = 0.0
        top = 0
        bottom = 0
        right = 0.0
        has_box = False

        for run_text, run_font, embedded_color in self._iter_font_runs(text, font):
            bbox = self._run_text_bbox(draw, run_text, run_font, embedded_color=embedded_color)
            run_width = self._run_text_length(draw, run_text, run_font, embedded_color=embedded_color)
            if not has_box:
                top = bbox[1]
                bottom = bbox[3]
                has_box = True
            else:
                top = min(top, bbox[1])
                bottom = max(bottom, bbox[3])
            right = max(right, cursor_x + run_width)
            cursor_x += run_width

        return (0, top, int(math.ceil(right)), bottom)

    def _draw_text_with_fallback(self, canvas, draw, xy: tuple[float, float], text: str, font, fill) -> None:
        cursor_x = float(xy[0])
        for run_text, run_font, embedded_color in self._iter_font_runs(text, font):
            self._draw_text_run(
                canvas,
                draw,
                (cursor_x, float(xy[1])),
                run_text,
                run_font,
                fill,
                embedded_color=embedded_color,
            )
            cursor_x += self._run_text_length(draw, run_text, run_font, embedded_color=embedded_color)

    @staticmethod
    def _resampling():
        return getattr(Image, "Resampling", Image).LANCZOS

    @staticmethod
    def _same_site(base_url: str, url: str) -> bool:
        base = urlparse(base_url)
        target = urlparse(url)
        return base.scheme == target.scheme and base.netloc == target.netloc
