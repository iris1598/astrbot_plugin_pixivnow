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
        ranking_items = [
            {
                "rank": i + 1,
                "illust_id": str(125920001 + i),
                "title": titles[i],
                "user_name": artists[i],
            }
            for i in range(5)
        ]
        ranking = PixivGridRenderer(theme=theme).render_ranking(
            ranking_items,
            thumbs[:5],
            "daily",
            "illust",
            1,
            "2026年8月12日",
        )
        rank_path = out_dir / f"ranking-{theme}.png"
        ranking.save(rank_path, "PNG", optimize=True)
        print(rank_path)

        illust = {
            "id": "125920001",
            "title": "星降る夜の約束",
            "userName": "Haru",
            "bookmarkCount": 24831,
            "likeCount": 19542,
            "pageCount": 4,
            "tags": {"tags": [{"tag": tag} for tag in ["原创", "少女", "星空", "幻想", "夜景"]]},
        }
        illust_image = PixivGridRenderer(theme=theme).render_illust_detail(illust, thumbs[0])
        illust_path = out_dir / f"illust-detail-{theme}.png"
        illust_image.save(illust_path, "PNG", optimize=True)
        print(illust_path)

        user = {
            "userId": "9482103",
            "name": "Haru",
            "following": 386,
            "mypixivCount": 128,
            "comment": "日常与幻想系插画创作者。喜欢画星空、少女和安静的城市风景，感谢每一次收藏与关注。",
        }
        user_image = PixivGridRenderer(theme=theme).render_user_detail(user, thumbs[1])
        user_path = out_dir / f"user-detail-{theme}.png"
        user_image.save(user_path, "PNG", optimize=True)
        print(user_path)

        novel = {
            "id": "28389206",
            "title": "银河尽头的邮局",
            "userName": "青空",
            "useWordCount": True,
            "wordCount": 12840,
            "content": "在银河列车停运后的第七年，我收到了一封来自星海尽头的信。信封没有邮票，收件人却写着我的名字。为了找到寄信人，我重新踏上了那条已经从地图上消失的航线……",
        }
        novel_image = PixivGridRenderer(theme=theme).render_novel_detail(novel, thumbs[2])
        novel_path = out_dir / f"novel-detail-{theme}.png"
        novel_image.save(novel_path, "PNG", optimize=True)
        print(novel_path)


if __name__ == "__main__":
    main()
