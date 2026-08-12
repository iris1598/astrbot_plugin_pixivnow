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
