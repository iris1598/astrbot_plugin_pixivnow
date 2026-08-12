"""PixivNow 搜索结果卡片渲染器。

视觉语言参考 astrbot_plugin_rika_share：柔和渐变背景、品牌色光晕、
半透明磨砂卡片、圆角媒体与亮/暗双主题。
"""

from __future__ import annotations

import io
import logging
import math
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps


ACCENT = (0, 150, 250)
logger = logging.getLogger(__name__)

_FONT_CANDIDATES = {
    "win32": (
        ("msyh.ttc", "msyhbd.ttc"),
        ("simhei.ttf", "msyhbd.ttc"),
        ("Deng.ttf", "Dengb.ttf"),
        ("simsun.ttc", "simsun.ttc"),
        ("NotoSansCJK-Regular.ttc", "NotoSansCJK-Bold.ttc"),
    ),
    "darwin": (
        ("PingFang.ttc", "PingFang.ttc"),
        ("Hiragino Sans GB.ttc", "Hiragino Sans GB.ttc"),
        ("STHeiti Medium.ttc", "STHeiti Medium.ttc"),
    ),
    "linux": (
        ("NotoSansCJK-Regular.ttc", "NotoSansCJK-Bold.ttc"),
        ("NotoSansCJKsc-Regular.otf", "NotoSansCJKsc-Bold.otf"),
        ("NotoSansSC-Regular.ttf", "NotoSansSC-Bold.ttf"),
        ("SourceHanSansSC-Regular.otf", "SourceHanSansSC-Bold.otf"),
        ("wqy-zenhei.ttc", "wqy-zenhei.ttc"),
        ("wqy-microhei.ttc", "wqy-microhei.ttc"),
        ("DroidSansFallbackFull.ttf", "DroidSansFallbackFull.ttf"),
    ),
}

_FONT_DIRS = (
    Path(__file__).resolve().parent / "assets" / "fonts",
    Path("C:/Windows/Fonts"),
    Path("/System/Library/Fonts"),
    Path("/System/Library/Fonts/Supplemental"),
    Path("/usr/share/fonts/opentype/noto"),
    Path("/usr/share/fonts/opentype/noto-cjk"),
    Path("/usr/share/fonts/truetype/noto-cjk"),
    Path("/usr/share/fonts/noto-cjk"),
    Path("/usr/share/fonts/truetype/wqy"),
    Path("/usr/share/fonts/truetype/droid"),
    Path("/usr/share/fonts/truetype/arphic"),
    Path("/usr/share/fonts/opentype/source-han-sans"),
    Path("/usr/share/fonts/truetype/noto"),
    Path("/usr/share/fonts"),
    Path("/usr/local/share/fonts"),
)


def _discover_fonts(custom_path: str | None = None) -> tuple[str | None, str | None]:
    """跨平台查找中日韩字体，返回常规与粗体字体路径。"""
    if custom_path:
        custom = Path(custom_path).expanduser()
        if custom.is_file():
            return str(custom), str(custom)
        if custom.is_dir():
            found = sorted(
                p for p in custom.rglob("*")
                if p.is_file() and p.suffix.lower() in {".ttf", ".ttc", ".otf"}
            )
            if found:
                bold = next((p for p in found if "bold" in p.name.lower()), found[0])
                regular = next((p for p in found if "regular" in p.name.lower()), found[0])
                return str(regular), str(bold)
        logger.warning("PixivNow 渲染字体路径无效，将自动探测系统字体: %s", custom_path)

    platform = sys.platform if sys.platform in _FONT_CANDIDATES else "linux"
    for regular_name, bold_name in _FONT_CANDIDATES[platform]:
        for directory in _FONT_DIRS:
            regular = directory / regular_name
            bold = directory / bold_name
            if regular.is_file():
                return str(regular), str(bold) if bold.is_file() else None

    if platform == "linux":
        def fontconfig(pattern: str) -> str | None:
            try:
                result = subprocess.run(
                    ["fc-match", "-f", "%{file}\n", pattern],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                return None
            path = Path(result.stdout.splitlines()[0].strip()) if result.stdout.strip() else None
            return str(path) if path and path.is_file() else None

        regular = fontconfig(":lang=zh-cn") or fontconfig("sans-serif:lang=zh-cn")
        bold = fontconfig(":lang=zh-cn:style=Bold") or regular
        if regular:
            return regular, bold

    # 常见字体包的目录层级会随 Linux 发行版变化，最后做一次浅范围递归兜底。
    for directory in _FONT_DIRS:
        if not directory.is_dir():
            continue
        found = sorted(
            p for p in directory.rglob("*")
            if p.is_file() and p.suffix.lower() in {".ttf", ".ttc", ".otf"}
        )
        if found:
            bold = next((p for p in found if "bold" in p.name.lower()), None)
            return str(found[0]), str(bold) if bold else None
    return None, None


@dataclass(frozen=True)
class GridTheme:
    gradient_top: tuple[int, int, int]
    gradient_bottom: tuple[int, int, int]
    text_primary: tuple[int, int, int]
    text_secondary: tuple[int, int, int]
    text_tertiary: tuple[int, int, int]
    card_fill: tuple[int, int, int, int]
    card_border: tuple[int, int, int, int]
    pill_fill: tuple[int, int, int, int]
    footer_fill: tuple[int, int, int, int]
    placeholder_top: tuple[int, int, int]
    placeholder_bottom: tuple[int, int, int]
    shadow_alpha: int
    glow_alpha: int


THEMES = {
    "dark": GridTheme(
        gradient_top=(36, 43, 63),
        gradient_bottom=(18, 22, 31),
        text_primary=(245, 247, 252),
        text_secondary=(174, 182, 200),
        text_tertiary=(123, 133, 152),
        card_fill=(30, 36, 51, 246),
        card_border=(65, 74, 96, 255),
        pill_fill=(43, 50, 70, 255),
        footer_fill=(27, 32, 45, 255),
        placeholder_top=(44, 51, 71),
        placeholder_bottom=(20, 24, 35),
        shadow_alpha=105,
        glow_alpha=32,
    ),
    "light": GridTheme(
        gradient_top=(255, 255, 255),
        gradient_bottom=(241, 244, 249),
        text_primary=(26, 33, 48),
        text_secondary=(85, 96, 122),
        text_tertiary=(140, 149, 169),
        card_fill=(255, 255, 255, 248),
        card_border=(218, 224, 234, 255),
        pill_fill=(250, 251, 253, 255),
        footer_fill=(250, 251, 253, 255),
        placeholder_top=(228, 233, 242),
        placeholder_bottom=(243, 246, 251),
        shadow_alpha=34,
        glow_alpha=18,
    ),
}


class PixivGridRenderer:
    """渲染 3×3 Pixiv 搜索结果卡片。"""

    def __init__(
        self,
        theme: str = "dark",
        columns: int = 3,
        font_path: str | None = None,
    ):
        self.theme_name = theme if theme in THEMES else "dark"
        self.theme = THEMES[self.theme_name]
        self.columns = max(1, columns)
        self._regular_font, self._bold_font = _discover_fonts(font_path)
        self._font_cache: dict[tuple[int, bool], ImageFont.ImageFont] = {}
        if not self._regular_font:
            logger.warning(
                "未找到支持中文的渲染字体，PixivNow 搜索图文字可能变小或显示方框；"
                "请在 render_font_path 中指定字体文件或目录"
            )

    def _font(self, size: int, bold: bool = False) -> ImageFont.ImageFont:
        key = (size, bold)
        if key in self._font_cache:
            return self._font_cache[key]
        path = self._bold_font if bold and self._bold_font else self._regular_font
        if path:
            try:
                font = ImageFont.truetype(path, size)
            except OSError:
                font = ImageFont.load_default()
        else:
            font = ImageFont.load_default()
        self._font_cache[key] = font
        return font

    @staticmethod
    def _gradient(size, top, bottom):
        width, height = size
        image = Image.new("RGB", size, top)
        draw = ImageDraw.Draw(image)
        for y in range(height):
            t = y / max(height - 1, 1)
            color = tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
            draw.line((0, y, width, y), fill=color)
        return image.convert("RGBA")

    @staticmethod
    def _radial_glow(size, center, color, alpha):
        width, height = size
        layer = Image.new("RGBA", size, (0, 0, 0, 0))
        px = layer.load()
        cx, cy = center
        radius = max(width, height) * 0.72
        for y in range(height):
            for x in range(width):
                dist = math.hypot(x - cx, y - cy) / radius
                if dist < 1:
                    a = round(alpha * (1 - dist) ** 2)
                    px[x, y] = (*color, a)
        return layer

    @staticmethod
    def _fit_line(text: str, font, max_width: int) -> str:
        text = " ".join(str(text).replace("\n", " ").split())
        if font.getlength(text) <= max_width:
            return text
        suffix = "…"
        while text and font.getlength(text + suffix) > max_width:
            text = text[:-1]
        return text.rstrip() + suffix

    @staticmethod
    def _rounded_image(image: Image.Image, size, radius: int) -> Image.Image:
        fitted = ImageOps.fit(image.convert("RGB"), size, method=Image.Resampling.LANCZOS)
        fitted = fitted.convert("RGBA")
        mask = Image.new("L", size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius, fill=255)
        fitted.putalpha(mask)
        return fitted

    def _placeholder(self, size):
        image = self._gradient(size, self.theme.placeholder_top, self.theme.placeholder_bottom)
        draw = ImageDraw.Draw(image)
        cx, cy = size[0] // 2, size[1] // 2
        draw.rounded_rectangle((cx - 38, cy - 30, cx + 38, cy + 30), 12, outline=(*self.theme.text_tertiary, 105), width=3)
        draw.ellipse((cx - 21, cy - 15, cx - 7, cy - 1), fill=(*self.theme.text_tertiary, 105))
        draw.polygon(((cx - 29, cy + 19), (cx - 5, cy - 2), (cx + 9, cy + 10), (cx + 25, cy - 8), (cx + 34, cy + 19)), fill=(*self.theme.text_tertiary, 105))
        return image

    def render(self, items: list[dict], thumbs: list[bytes | None], keyword="", page=0, mode="") -> Image.Image:
        theme = self.theme
        columns = self.columns
        rows = max(1, math.ceil(len(items) / columns))
        width, outer_pad, gap = 1080, 36, 18
        card_w = (width - outer_pad * 2 - gap * (columns - 1)) // columns
        media_h, info_h = 268, 106
        card_h = media_h + info_h
        header_h, footer_h = 152, 78
        height = header_h + outer_pad + rows * card_h + (rows - 1) * gap + outer_pad + footer_h

        canvas = self._gradient((width, height), theme.gradient_top, theme.gradient_bottom)
        canvas.alpha_composite(self._radial_glow((width, height), (130, 70), ACCENT, theme.glow_alpha))
        draw = ImageDraw.Draw(canvas)

        title_font = self._font(34, bold=True)
        meta_font = self._font(19)
        chip_font = self._font(18, bold=True)
        item_title_font = self._font(21, bold=True)
        author_font = self._font(17)
        badge_font = self._font(21, bold=True)

        draw.rounded_rectangle((outer_pad, 30, outer_pad + 68, 36), 3, fill=(*ACCENT, 230))
        draw.text((outer_pad, 56), keyword or "搜索结果", font=title_font, fill=theme.text_primary)
        subtitle = "PIXIV DISCOVERY  ·  输入序号选择作品"
        draw.text((outer_pad, 105), subtitle, font=meta_font, fill=theme.text_tertiary)

        chips = [f"第 {page} 页" if page else "搜索结果", f"{len(items)} 个作品"]
        if mode:
            chips.append(mode.upper())
        chip_x = width - outer_pad
        for label in reversed(chips):
            chip_w = round(chip_font.getlength(label)) + 30
            chip_x -= chip_w
            draw.rounded_rectangle((chip_x, 65, chip_x + chip_w, 103), 19, fill=theme.pill_fill, outline=theme.card_border, width=1)
            draw.text((chip_x + 15, 73), label, font=chip_font, fill=theme.text_secondary)
            chip_x -= 10

        for idx, item in enumerate(items):
            col, row = idx % columns, idx // columns
            x = outer_pad + col * (card_w + gap)
            y = header_h + outer_pad + row * (card_h + gap)

            shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
            ImageDraw.Draw(shadow).rounded_rectangle((x + 2, y + 7, x + card_w - 2, y + card_h + 7), 24, fill=(0, 0, 0, theme.shadow_alpha))
            canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(13)))
            draw = ImageDraw.Draw(canvas)
            draw.rounded_rectangle((x, y, x + card_w, y + card_h), 24, fill=theme.card_fill, outline=theme.card_border, width=1)

            raw = thumbs[idx] if idx < len(thumbs) else None
            try:
                media = self._rounded_image(Image.open(io.BytesIO(raw)), (card_w, media_h), 24) if raw else self._placeholder((card_w, media_h))
            except Exception:
                media = self._placeholder((card_w, media_h))
            canvas.alpha_composite(media, (x, y))

            overlay = Image.new("RGBA", (card_w, 82), (0, 0, 0, 0))
            od = ImageDraw.Draw(overlay)
            for oy in range(82):
                od.line((0, oy, card_w, oy), fill=(0, 0, 0, round(115 * (oy / 81) ** 1.7)))
            canvas.alpha_composite(overlay, (x, y + media_h - 82))

            bx, by = x + 16, y + 16
            draw = ImageDraw.Draw(canvas)
            draw.ellipse((bx, by, bx + 46, by + 46), fill=(*ACCENT, 245), outline=(255, 255, 255, 95), width=1)
            num = str(idx + 1)
            box = draw.textbbox((0, 0), num, font=badge_font)
            draw.text((bx + 23 - (box[2] - box[0]) / 2, by + 23 - (box[3] - box[1]) / 2 - box[1]), num, font=badge_font, fill=(255, 255, 255))

            illust_type = str(item.get("illustType") or "ILLUST").upper()
            type_w = round(chip_font.getlength(illust_type)) + 22
            draw.rounded_rectangle((x + card_w - type_w - 14, y + 16, x + card_w - 14, y + 50), 17, fill=(12, 15, 24, 135), outline=(255, 255, 255, 45))
            draw.text((x + card_w - type_w - 3, y + 23), illust_type, font=self._font(15, bold=True), fill=(255, 255, 255, 225))

            title = self._fit_line(item.get("title") or "无标题", item_title_font, card_w - 32)
            author = self._fit_line("@" + str(item.get("userName") or "未知画师"), author_font, card_w - 32)
            draw.text((x + 16, y + media_h + 16), title, font=item_title_font, fill=theme.text_primary)
            draw.text((x + 16, y + media_h + 53), author, font=author_font, fill=theme.text_secondary)
            work_id = str(item.get("id") or "")
            if work_id:
                id_text = f"ID {work_id}"
                id_w = author_font.getlength(id_text)
                draw.text((x + card_w - 16 - id_w, y + media_h + 53), id_text, font=author_font, fill=theme.text_tertiary)

        footer_y = height - footer_h
        draw.rounded_rectangle((outer_pad, footer_y, width - outer_pad, height - 24), 22, fill=theme.footer_fill, outline=theme.card_border)
        footer = "1–9 选图    N 下一页    P 上一页    P+数字 跳页    E / 0 退出"
        draw.text((outer_pad + 24, footer_y + 18), footer, font=meta_font, fill=theme.text_secondary)
        brand = "PixivNow"
        brand_w = meta_font.getlength(brand)
        draw.ellipse((width - outer_pad - brand_w - 47, footer_y + 23, width - outer_pad - brand_w - 37, footer_y + 33), fill=(*ACCENT, 255))
        draw.text((width - outer_pad - brand_w - 25, footer_y + 18), brand, font=meta_font, fill=theme.text_tertiary)
        return canvas.convert("RGB")

    def render_ranking(
        self,
        items: list[dict],
        thumbs: list[bytes | None],
        mode: str = "daily",
        content: str = "all",
        page: int = 1,
        date: str = "",
    ) -> Image.Image:
        """渲染 Pixiv 排行榜海报：前三名主卡 + 其余横向榜单。"""
        theme = self.theme
        width, pad, gap = 1080, 36, 18
        hero_w = (width - pad * 2 - gap * 2) // 3
        hero_media_h, hero_info_h = 300, 116
        hero_h = hero_media_h + hero_info_h
        row_h = 156
        remaining = max(0, len(items) - 3)
        header_h = 178
        footer_h = 78
        height = header_h + pad + hero_h + (gap if remaining else 0) + remaining * (row_h + gap) + pad + footer_h

        canvas = self._gradient((width, height), theme.gradient_top, theme.gradient_bottom)
        canvas.alpha_composite(self._radial_glow((width, height), (145, 65), ACCENT, theme.glow_alpha))
        draw = ImageDraw.Draw(canvas)

        title_font = self._font(38, bold=True)
        eyebrow_font = self._font(18, bold=True)
        meta_font = self._font(18)
        chip_font = self._font(17, bold=True)
        hero_title_font = self._font(21, bold=True)
        author_font = self._font(17)
        rank_font = self._font(24, bold=True)
        row_title_font = self._font(23, bold=True)

        draw.rounded_rectangle((pad, 30, pad + 68, 36), 3, fill=(*ACCENT, 230))
        draw.text((pad, 54), "PIXIV RANKING", font=eyebrow_font, fill=ACCENT)
        draw.text((pad, 84), "插画排行榜", font=title_font, fill=theme.text_primary)
        sub = date.strip() or "实时榜单"
        draw.text((pad, 137), sub, font=meta_font, fill=theme.text_tertiary)

        chips = [mode.upper(), content.upper(), f"第 {page} 页"]
        chip_x = width - pad
        for label in reversed(chips):
            chip_w = round(chip_font.getlength(label)) + 30
            chip_x -= chip_w
            draw.rounded_rectangle((chip_x, 78, chip_x + chip_w, 116), 19, fill=theme.pill_fill, outline=theme.card_border)
            draw.text((chip_x + 15, 86), label, font=chip_font, fill=theme.text_secondary)
            chip_x -= 10

        medal_colors = ((255, 190, 54), (174, 185, 205), (203, 132, 78))
        top = items[:3]
        for index, item in enumerate(top):
            x = pad + index * (hero_w + gap)
            y = header_h + pad
            shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
            ImageDraw.Draw(shadow).rounded_rectangle((x + 2, y + 7, x + hero_w - 2, y + hero_h + 7), 24, fill=(0, 0, 0, theme.shadow_alpha))
            canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(13)))
            draw = ImageDraw.Draw(canvas)
            draw.rounded_rectangle((x, y, x + hero_w, y + hero_h), 24, fill=theme.card_fill, outline=theme.card_border)

            raw = thumbs[index] if index < len(thumbs) else None
            try:
                media = self._rounded_image(Image.open(io.BytesIO(raw)), (hero_w, hero_media_h), 24) if raw else self._placeholder((hero_w, hero_media_h))
            except Exception:
                media = self._placeholder((hero_w, hero_media_h))
            canvas.alpha_composite(media, (x, y))

            overlay = Image.new("RGBA", (hero_w, 82), (0, 0, 0, 0))
            od = ImageDraw.Draw(overlay)
            for oy in range(82):
                od.line((0, oy, hero_w, oy), fill=(0, 0, 0, round(120 * (oy / 81) ** 1.7)))
            canvas.alpha_composite(overlay, (x, y + hero_media_h - 82))

            rank = str(item.get("rank") or index + 1)
            medal = medal_colors[index]
            draw = ImageDraw.Draw(canvas)
            draw.ellipse((x + 16, y + 16, x + 68, y + 68), fill=(*medal, 248), outline=(255, 255, 255, 120), width=1)
            rank_box = draw.textbbox((0, 0), rank, font=rank_font)
            draw.text((x + 42 - (rank_box[2] - rank_box[0]) / 2, y + 42 - (rank_box[3] - rank_box[1]) / 2 - rank_box[1]), rank, font=rank_font, fill=(255, 255, 255))

            title = self._fit_line(item.get("title") or "无标题", hero_title_font, hero_w - 32)
            author = self._fit_line("@" + str(item.get("user_name") or item.get("userName") or "未知画师"), author_font, hero_w - 32)
            draw.text((x + 16, y + hero_media_h + 17), title, font=hero_title_font, fill=theme.text_primary)
            draw.text((x + 16, y + hero_media_h + 56), author, font=author_font, fill=theme.text_secondary)
            work_id = str(item.get("illust_id") or item.get("id") or "")
            if work_id:
                id_text = f"ID {work_id}"
                draw.text((x + hero_w - 16 - author_font.getlength(id_text), y + hero_media_h + 56), id_text, font=author_font, fill=theme.text_tertiary)

        list_y = header_h + pad + hero_h + gap
        for offset, item in enumerate(items[3:]):
            index = offset + 3
            y = list_y + offset * (row_h + gap)
            draw = ImageDraw.Draw(canvas)
            draw.rounded_rectangle((pad, y, width - pad, y + row_h), 22, fill=theme.card_fill, outline=theme.card_border)
            thumb_w = 210
            raw = thumbs[index] if index < len(thumbs) else None
            try:
                media = self._rounded_image(Image.open(io.BytesIO(raw)), (thumb_w, row_h), 22) if raw else self._placeholder((thumb_w, row_h))
            except Exception:
                media = self._placeholder((thumb_w, row_h))
            canvas.alpha_composite(media, (pad, y))

            rank = str(item.get("rank") or index + 1)
            rank_x = pad + thumb_w + 28
            draw = ImageDraw.Draw(canvas)
            draw.text((rank_x, y + 35), f"#{rank}", font=rank_font, fill=ACCENT)
            text_x = rank_x + 76
            max_text_w = width - pad - text_x - 28
            title = self._fit_line(item.get("title") or "无标题", row_title_font, max_text_w)
            author = self._fit_line("@" + str(item.get("user_name") or item.get("userName") or "未知画师"), author_font, max_text_w)
            draw.text((text_x, y + 30), title, font=row_title_font, fill=theme.text_primary)
            draw.text((text_x, y + 77), author, font=author_font, fill=theme.text_secondary)
            work_id = str(item.get("illust_id") or item.get("id") or "")
            draw.text((text_x, y + 108), f"作品 ID  {work_id}", font=author_font, fill=theme.text_tertiary)

        footer_y = height - footer_h
        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle((pad, footer_y, width - pad, height - 24), 22, fill=theme.footer_fill, outline=theme.card_border)
        draw.text((pad + 24, footer_y + 18), "Pixiv 官方排行榜 · 图片由 PixivNow 代理获取", font=meta_font, fill=theme.text_secondary)
        brand = "PixivNow"
        brand_w = meta_font.getlength(brand)
        draw.ellipse((width - pad - brand_w - 47, footer_y + 23, width - pad - brand_w - 37, footer_y + 33), fill=(*ACCENT, 255))
        draw.text((width - pad - brand_w - 25, footer_y + 18), brand, font=meta_font, fill=theme.text_tertiary)
        return canvas.convert("RGB")

    def _detail_base(self, height: int, section: str, title: str, subtitle: str = ""):
        theme = self.theme
        width, pad = 900, 42
        canvas = self._gradient((width, height), theme.gradient_top, theme.gradient_bottom)
        canvas.alpha_composite(self._radial_glow((width, height), (110, 55), ACCENT, theme.glow_alpha))
        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle((pad, 28, pad + 64, 34), 3, fill=(*ACCENT, 230))
        draw.text((pad, 52), section, font=self._font(17, bold=True), fill=ACCENT)
        draw.text((pad, 82), self._fit_line(title, self._font(34, bold=True), width - pad * 2), font=self._font(34, bold=True), fill=theme.text_primary)
        if subtitle:
            draw.text((pad, 130), self._fit_line(subtitle, self._font(18), width - pad * 2), font=self._font(18), fill=theme.text_tertiary)
        return canvas, draw

    def _detail_footer(self, canvas: Image.Image, y: int, link: str = "") -> None:
        theme = self.theme
        width, pad = canvas.width, 42
        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle((pad, y, width - pad, y + 54), 20, fill=theme.footer_fill, outline=theme.card_border)
        label = self._fit_line(link or "PixivNow", self._font(17), width - pad * 2 - 180)
        draw.text((pad + 20, y + 16), label, font=self._font(17), fill=theme.text_secondary)
        brand = "PixivNow"
        brand_w = self._font(17).getlength(brand)
        draw.ellipse((width - pad - brand_w - 42, y + 22, width - pad - brand_w - 32, y + 32), fill=(*ACCENT, 255))
        draw.text((width - pad - brand_w - 21, y + 16), brand, font=self._font(17), fill=theme.text_tertiary)

    def render_illust_detail(self, item: dict, cover: bytes | None = None) -> Image.Image:
        theme = self.theme
        width, pad, hero_h = 900, 42, 500
        title = str(item.get("title") or "无标题")
        author = str(item.get("userName") or item.get("user_name") or "未知画师")
        canvas, draw = self._detail_base(910, "PIXIV ARTWORK", title, f"@{author}")
        try:
            media = self._rounded_image(Image.open(io.BytesIO(cover)), (width - pad * 2, hero_h), 26) if cover else self._placeholder((width - pad * 2, hero_h))
        except Exception:
            media = self._placeholder((width - pad * 2, hero_h))
        canvas.alpha_composite(media, (pad, 178))

        info_y = 704
        work_id = str(item.get("id") or item.get("illust_id") or "")
        page_count = int(item.get("pageCount") or 1)
        stats = []
        if item.get("bookmarkCount") is not None:
            stats.append(("收藏", str(item["bookmarkCount"])))
        if item.get("likeCount") is not None:
            stats.append(("喜欢", str(item["likeCount"])))
        stats.extend((("页数", str(page_count)), ("作品 ID", work_id)))
        x = pad
        for label, value in stats:
            text = f"{label}  {value}"
            chip_w = round(self._font(17, bold=True).getlength(text)) + 30
            draw.rounded_rectangle((x, info_y, x + chip_w, info_y + 40), 20, fill=theme.pill_fill, outline=theme.card_border)
            draw.text((x + 15, info_y + 9), text, font=self._font(17, bold=True), fill=theme.text_secondary)
            x += chip_w + 10

        tags = item.get("tags") or []
        if isinstance(tags, dict):
            tags = tags.get("tags") or []
        tag_names = [str(t.get("tag") if isinstance(t, dict) else t) for t in tags][:8]
        tag_text = "  ".join(f"#{tag}" for tag in tag_names if tag)
        if tag_text:
            draw.text((pad, info_y + 62), self._fit_line(tag_text, self._font(17), width - pad * 2), font=self._font(17), fill=theme.text_tertiary)
        self._detail_footer(canvas, 830, f"https://www.pixiv.net/artworks/{work_id}")
        return canvas.convert("RGB")

    def render_user_detail(self, user: dict, avatar: bytes | None = None) -> Image.Image:
        theme = self.theme
        width, pad = 900, 42
        name = str(user.get("name") or "未知画师")
        user_id = str(user.get("userId") or user.get("id") or "")
        canvas, draw = self._detail_base(670, "PIXIV CREATOR", name, f"USER ID  {user_id}")
        card_y = 184
        draw.rounded_rectangle((pad, card_y, width - pad, 548), 28, fill=theme.card_fill, outline=theme.card_border)

        avatar_size = 176
        try:
            source = Image.open(io.BytesIO(avatar)).convert("RGB") if avatar else self._placeholder((avatar_size, avatar_size)).convert("RGB")
            fitted = ImageOps.fit(source, (avatar_size, avatar_size), method=Image.Resampling.LANCZOS).convert("RGBA")
            mask = Image.new("L", (avatar_size, avatar_size), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, avatar_size - 1, avatar_size - 1), fill=255)
            fitted.putalpha(mask)
        except Exception:
            fitted = self._placeholder((avatar_size, avatar_size))
        canvas.alpha_composite(fitted, (pad + 32, card_y + 36))
        draw = ImageDraw.Draw(canvas)
        draw.ellipse((pad + 28, card_y + 32, pad + 32 + avatar_size + 4, card_y + 36 + avatar_size + 4), outline=(*ACCENT, 210), width=4)

        text_x = pad + 242
        stats = [
            ("关注", user.get("following")),
            ("MyPixiv", user.get("mypixivCount")),
        ]
        x = text_x
        for label, value in stats:
            if value is None:
                continue
            text = f"{label}  {value}"
            chip_w = round(self._font(18, bold=True).getlength(text)) + 32
            draw.rounded_rectangle((x, card_y + 42, x + chip_w, card_y + 84), 21, fill=theme.pill_fill, outline=theme.card_border)
            draw.text((x + 16, card_y + 51), text, font=self._font(18, bold=True), fill=theme.text_secondary)
            x += chip_w + 12

        comment = " ".join(str(user.get("comment") or "这位画师还没有填写个人简介。").replace("\n", " ").split())
        desc_font = self._font(20)
        lines = []
        rest = comment
        while rest and len(lines) < 5:
            line = self._fit_line(rest, desc_font, width - text_x - pad - 32)
            if line.endswith("…"):
                consumed = max(1, len(line) - 1)
                lines.append(line[:-1])
                rest = rest[consumed:]
            else:
                lines.append(line)
                rest = ""
        y = card_y + 122
        for line in lines:
            draw.text((text_x, y), line, font=desc_font, fill=theme.text_secondary)
            y += 34
        self._detail_footer(canvas, 578, f"https://www.pixiv.net/users/{user_id}")
        return canvas.convert("RGB")

    def render_novel_detail(self, novel: dict, cover: bytes | None = None) -> Image.Image:
        theme = self.theme
        width, pad = 900, 42
        title = str(novel.get("title") or "无标题")
        author = str(novel.get("userName") or "未知作者")
        canvas, draw = self._detail_base(860, "PIXIV NOVEL", title, f"@{author}")
        card_y = 184
        draw.rounded_rectangle((pad, card_y, width - pad, 738), 28, fill=theme.card_fill, outline=theme.card_border)
        cover_w, cover_h = 260, 390
        try:
            media = self._rounded_image(Image.open(io.BytesIO(cover)), (cover_w, cover_h), 22) if cover else self._placeholder((cover_w, cover_h))
        except Exception:
            media = self._placeholder((cover_w, cover_h))
        canvas.alpha_composite(media, (pad + 26, card_y + 28))

        text_x = pad + 318
        use_words = novel.get("wordCount") if novel.get("useWordCount") else novel.get("characterCount") or novel.get("textCount")
        chips = [("字数", use_words if use_words is not None else "未知"), ("小说 ID", novel.get("id") or "")]
        x = text_x
        for label, value in chips:
            text = f"{label}  {value}"
            chip_w = round(self._font(17, bold=True).getlength(text)) + 28
            draw.rounded_rectangle((x, card_y + 30, x + chip_w, card_y + 70), 20, fill=theme.pill_fill, outline=theme.card_border)
            draw.text((x + 14, card_y + 39), text, font=self._font(17, bold=True), fill=theme.text_secondary)
            x += chip_w + 10

        content = " ".join(str(novel.get("content") or novel.get("description") or "暂无正文摘要").replace("\n", " ").split())
        body_font = self._font(20)
        max_w = width - text_x - pad - 28
        y = card_y + 100
        line = ""
        lines = []
        for char in content:
            test = line + char
            if body_font.getlength(test) > max_w and line:
                lines.append(line)
                line = char
                if len(lines) >= 10:
                    break
            else:
                line = test
        if line and len(lines) < 10:
            lines.append(line)
        if len("".join(lines)) < len(content) and lines:
            lines[-1] = self._fit_line(lines[-1] + "…", body_font, max_w)
        for line in lines:
            draw.text((text_x, y), line, font=body_font, fill=theme.text_secondary)
            y += 33
        novel_id = str(novel.get("id") or "")
        self._detail_footer(canvas, 768, f"https://www.pixiv.net/novel/show.php?id={novel_id}")
        return canvas.convert("RGB")
