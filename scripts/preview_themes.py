"""生成 PixivNow 搜索结果亮色/暗色主题示例图。"""

from __future__ import annotations

import io
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pixiv_grid_renderer import PixivGridRenderer  # noqa: E402


def sample_thumb(index: int) -> bytes:
    palettes = [
        ((92, 173, 255), (252, 182, 210)), ((77, 73, 128), (244, 154, 194)),
        ((75, 184, 170), (255, 216, 145)), ((143, 115, 255), (98, 214, 255)),
        ((255, 154, 118), (255, 222, 157)), ((61, 80, 120), (193, 122, 180)),
        ((92, 193, 255), (245, 245, 255)), ((244, 129, 172), (119, 98, 181)),
        ((86, 201, 155), (240, 212, 128)),
    ]
    top, bottom = palettes[index % len(palettes)]
    image = Image.new("RGB", (640, 480), top)
    draw = ImageDraw.Draw(image)
    for y in range(480):
        t = y / 479
        color = tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        draw.line((0, y, 640, y), fill=color)
    # 用抽象景物代替联网素材，让预览可重复生成。
    draw.ellipse((80 + index * 9, 65, 390 + index * 5, 375), fill=(255, 255, 255, 75))
    draw.polygon(((0, 390), (175, 220 + index * 8), (310, 365), (450, 175), (640, 390), (640, 480), (0, 480)), fill=(25, 37, 65))
    draw.ellipse((430, 55 + index * 4, 525, 150 + index * 4), fill=(255, 244, 192))
    out = io.BytesIO()
    image.save(out, "JPEG", quality=92)
    return out.getvalue()


def main() -> None:
    out_dir = ROOT / "docs" / "previews"
    out_dir.mkdir(parents=True, exist_ok=True)
    titles = ["星降る夜の約束", "午後三時の喫茶店", "海辺の透明な夏", "雨上がりの街角", "猫と小さな宇宙", "花火大会", "空想旅行記", "春風のメロディ", "森の図書館"]
    artists = ["Haru", "moco", "青空", "Yuki", "小林", "nagi", "Mio", "白米", "Luna"]
    items = [
        {"id": str(124830001 + i), "title": titles[i], "userName": artists[i], "illustType": "illust" if i != 4 else "manga"}
        for i in range(9)
    ]
    thumbs = [sample_thumb(i) for i in range(9)]
    for theme in ("light", "dark"):
        image = PixivGridRenderer(theme=theme).render(items, thumbs, "初音ミク", 1, "safe")
        path = out_dir / f"search-grid-{theme}.png"
        image.save(path, "PNG", optimize=True)
        print(path)


if __name__ == "__main__":
    main()
