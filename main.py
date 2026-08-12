import asyncio
import io
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import quote

import httpx
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star
from PIL import Image as PILImage
from PIL import ImageDraw, ImageFont

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 搜索会话有效期（秒）：无操作则自动过期
SEARCH_SESSION_TTL = 120
# 每页显示的搜索结果数
SEARCH_PAGE_SIZE = 9

# 匹配缩略图 URL 中的 /c/{size}/ 前缀
SIZE_PREFIX_RE = re.compile(r"/c/[^/]+/")
# 缩略图文件名的质量后缀
THUMB_SUFFIX_RE = re.compile(r"_(square1200|custom1200)\.(jpg|png|gif)$")


class PixivNowError(Exception):
    """PixivNow/Pixiv 接口调用异常。"""


class PixivNowPlugin(Star):
    """通过自托管的 PixivNow 服务访问 Pixiv 的插件。

    Attributes:
        context: AstrBot 上下文，用于与核心交互。
        config: 插件配置（AstrBotConfig 字典）。
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._temp_files: list[Path] = []
        # 搜索会话：{会话id: {"works": [...], "created": timestamp}}
        self._search_sessions: dict[str, dict] = {}

    # ── 基础工具 ──────────────────────────────────────────────────

    def _base_url(self) -> str:
        """返回 PixivNow 服务地址（去除末尾斜杠）。"""
        url = str(
            self.config.get("pixivnow_url", "https://pixiv.js.org") or ""
        ).strip()
        if not url:
            raise PixivNowError("未配置 PixivNow 地址，请使用 /pixiv seturl 设置")
        return url.rstrip("/")

    def _headers(self) -> dict:
        """构造请求头，若配置了 token 则附带 Authorization、access_key 则附带 X-Access-Key。"""
        headers = {"User-Agent": UA}
        token = str(self.config.get("token", "") or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        access_key = str(self.config.get("access_key", "") or "").strip()
        if access_key:
            headers["X-Access-Key"] = access_key
        return headers

    def _resolve_url(self, url: str) -> str:
        """将 Pixiv 返回的图片 URL 解析为可通过 PixivNow 代理访问的完整地址。

        兼容三种情况：相对代理路径（/-/、/~/）、原始 i.pximg.net/s.pximg.net、
        已经完整的绝对 URL。
        """
        if not url:
            return url
        base = self._base_url()
        if url.startswith(("/-/", "/~/")):
            return base + url
        if "i.pximg.net" in url:
            return url.replace("https://i.pximg.net/", f"{base}/-/")
        if "s.pximg.net" in url:
            return url.replace("https://s.pximg.net/", f"{base}/~/")
        if url.startswith(("http://", "https://")):
            return url
        return base + "/" + url.lstrip("/")

    def _to_regular(self, url: str) -> str:
        """由缩略图 URL 推导出中等清晰度（regular）URL。"""
        u = SIZE_PREFIX_RE.sub("/", url)
        u = u.replace("/custom-thumb/", "/img-master/")
        u = THUMB_SUFFIX_RE.sub(r"_master1200.\2", u)
        return u

    def _url_candidates(self, item: dict, *, prefer_thumb: bool = False) -> list[str]:
        """按优先级返回可下载的图片 URL 候选列表（已去重）。

        PixivNow 服务端在构造缩略图/原图 URL 时只能猜测文件扩展名
        （见 docs/pixiv-web-api.md §2），原图 URL 可能 404（如原图为
        PNG 时 `_p0.jpg` 不存在），因此调用方应依次尝试候选列表。

        Args:
            item: 作品对象。
            prefer_thumb: 为 True 时优先小图（用于拼图缩略图合成）。

        Returns:
            按清晰度从高到低（或从小到大的缩略图）排列的 URL 列表。
        """
        urls = item.get("urls") or {}
        order = (
            ("thumb", "small", "regular", "original")
            if prefer_thumb
            else ("original", "regular", "small", "thumb")
        )
        chain = [str(urls[k]) for k in order if urls.get(k)]
        if item.get("url"):
            chain.append(self._to_regular(str(item["url"])))
            chain.append(str(item["url"]))
        seen: set[str] = set()
        out: list[str] = []
        for u in chain:
            if u and u not in seen:
                seen.add(u)
                out.append(u)
        return out

    def _best_url(self, item: dict) -> str:
        """从作品对象中挑选最清晰的可用图片 URL（首个候选）。"""
        chain = self._url_candidates(item)
        return chain[0] if chain else ""

    async def _download_best(self, item: dict, *, prefer_thumb: bool = False) -> Path:
        """按优先级依次尝试候选 URL 下载图片，全部失败时抛出异常。

        Args:
            item: 作品对象。
            prefer_thumb: 为 True 时优先小图。

        Returns:
            下载成功的临时文件路径。

        Raises:
            PixivNowError: 所有候选 URL 均下载失败。
        """
        last: PixivNowError | None = None
        for url in self._url_candidates(item, prefer_thumb=prefer_thumb):
            try:
                return await self._download(url)
            except PixivNowError as e:
                last = e
                logger.warning(f"候选图片下载失败，尝试下一个: {url} ({e})")
                continue
        raise PixivNowError(f"图片下载失败: {last}")

    async def _request(self, path: str, params: dict | None = None) -> dict | list:
        """请求 PixivNow 接口并返回解析后的 JSON。"""
        url = self._base_url() + path
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(15.0), follow_redirects=True
            ) as client:
                resp = await client.get(url, params=params, headers=self._headers())
        except httpx.HTTPError as e:
            raise PixivNowError(f"请求 PixivNow 失败: {e}") from e

        if resp.status_code != 200:
            raise PixivNowError(f"PixivNow 返回 HTTP {resp.status_code}（{url}）")
        try:
            return resp.json()
        except ValueError as e:
            raise PixivNowError("PixivNow 返回的不是有效 JSON") from e

    def _unwrap(self, data: dict | list) -> dict | list:
        """解开 /ajax/* 的标准信封 {error, message, body}。

        Args:
            data: 接口返回的 JSON。

        Returns:
            信封内的 body。

        Raises:
            PixivNowError: 当 error 为 true 时。
        """
        if isinstance(data, dict) and data.get("error"):
            raise PixivNowError(str(data.get("message") or "Pixiv 接口返回错误"))
        if isinstance(data, dict) and "body" in data:
            return data.get("body")
        return data

    async def _fetch_bytes(self, url: str) -> bytes:
        """下载图片为字节内容（自动经 PixivNow 代理解析）。

        Args:
            url: 图片 URL。

        Returns:
            图片原始字节。

        Raises:
            PixivNowError: 下载失败时。
        """
        target = self._resolve_url(url)
        timeout = int(self.config.get("download_timeout", 20) or 20)
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout), follow_redirects=True
            ) as client:
                resp = await client.get(target, headers=self._headers())
        except httpx.HTTPError as e:
            raise PixivNowError(f"图片下载失败: {e}") from e
        if resp.status_code != 200:
            raise PixivNowError(f"图片下载失败 HTTP {resp.status_code}")
        return resp.content

    async def _download(self, url: str) -> Path:
        """下载图片到临时文件并返回其路径。

        Args:
            url: 图片 URL（自动经 PixivNow 代理解析）。

        Returns:
            临时文件路径。

        Raises:
            PixivNowError: 下载失败时。
        """
        content = await self._fetch_bytes(url)
        suffix = Path(url.split("?")[0]).suffix or ".jpg"
        fd, path = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        p = Path(path)
        self._temp_files.append(p)
        self._schedule_cleanup(p)
        return p

    def _schedule_cleanup(self, path: Path, delay: int = 120) -> None:
        """延迟删除临时文件，给消息平台充足时间读取。"""

        async def _del() -> None:
            await asyncio.sleep(delay)
            try:
                path.unlink(missing_ok=True)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"清理临时文件失败 {path}: {e}")

        asyncio.create_task(_del())

    def _mode_allowed(self, mode: str) -> bool:
        """校验内容模式是否允许使用。

        Args:
            mode: safe/all/r18。

        Returns:
            True 表示允许。
        """
        if mode == "r18" and not self.config.get("r18_enabled", False):
            return False
        return mode in ("safe", "all", "r18")

    def _caption(self, item: dict, *, extra: str = "") -> str:
        """根据作品对象构造文本说明。"""
        lines = [f"标题：{item.get('title', '未知')}"]
        if item.get("userName"):
            lines.append(f"画师：{item['userName']}")
        if item.get("id"):
            lines.append(f"ID：{item['id']}")
        if item.get("bookmarkCount") is not None:
            lines.append(f"收藏：{item['bookmarkCount']}")
        if item.get("likeCount") is not None:
            lines.append(f"喜欢：{item['likeCount']}")
        if item.get("tags"):
            tags = item["tags"]
            if isinstance(tags, dict):
                tags = [t.get("tag", "") for t in tags.get("tags", [])]
            lines.append("标签：" + " ".join(str(t) for t in tags))
        if item.get("id"):
            lines.append(f"链接：https://www.pixiv.net/artworks/{item['id']}")
        if extra:
            lines.append(extra)
        return "\n".join(lines)

    def _load_font(self, size: int) -> ImageFont.ImageFont:
        """按优先级加载支持中文的字体，失败回退默认。"""
        for path in (
            "msyh.ttc",
            "msyh.ttf",
            "Microsoft YaHei.ttf",
            "simhei.ttf",
            "PingFang.ttc",
            "NotoSansCJK-Regular.ttc",
            "arial.ttf",
        ):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
        return ImageFont.load_default()

    def _wrap_text(
        self, text: str, font: ImageFont.ImageFont, max_width: int
    ) -> list[str]:
        """按像素宽度把字符串折行为多行。"""
        if not text:
            return []
        line = ""
        lines: list[str] = []
        for ch in text:
            test = line + ch
            bbox = font.getbbox(test)
            if bbox[2] - bbox[0] > max_width and line:
                lines.append(line)
                line = ch
            else:
                line = test
        if line:
            lines.append(line)
        return lines

    async def _fetch_thumb_bytes(self, item: dict) -> bytes | None:
        """下载单个作品的缩略图字节，失败返回 None。"""
        for url in self._url_candidates(item, prefer_thumb=True):
            try:
                return await self._fetch_bytes(url)
            except PixivNowError:
                continue
        return None

    async def _make_grid(
        self,
        items: list[dict],
        keyword: str = "",
        columns: int = 3,
        page: int = 0,
        mode: str = "",
    ) -> Path:
        """合成包含头部、3×3 缩略图网格、底部的完整结果图。

        每格包含：左上角大号红圆序号 + 居中缩略图 + 标题 + 画师。

        Args:
            items: 作品对象列表（最多 9 个）。
            keyword: 搜索关键词（显示在头部）。
            columns: 列数。
            page: 当前页码（>0 时在头部显示「第 N 页」）。
            mode: 搜索模式（头部显示 safe/all/r18）。

        Returns:
            合成后的临时图片路径。

        Raises:
            PixivNowError: 合成失败时。
        """
        # 画布尺寸
        cell_w, cell_h = 320, 360  # 每格宽高（足够放下 1 行标题 + 1 行画师）
        thumb_size = 300  # 缩略图实际像素
        pad = 12
        rows = (len(items) + columns - 1) // columns
        grid_w = columns * cell_w + (columns + 1) * pad
        grid_h = rows * cell_h + (rows + 1) * pad
        header_h = 60
        footer_h = 44
        canvas_w = grid_w
        canvas_h = header_h + grid_h + footer_h

        bg_color = (24, 24, 28)
        header_color = (42, 92, 170)
        cell_bg = (245, 245, 248)
        text_color = (20, 20, 20)
        sub_color = (110, 110, 120)
        badge_color = (220, 60, 70)
        footer_color = (240, 240, 245)
        footer_text = (80, 80, 95)

        canvas = PILImage.new("RGB", (canvas_w, canvas_h), bg_color)
        draw = ImageDraw.Draw(canvas)

        # 字体
        font_title = self._load_font(22)
        font_sub = self._load_font(18)
        font_header = self._load_font(22)
        font_footer = self._load_font(18)
        font_badge = self._load_font(28)

        # 头部：搜索关键词条
        draw.rectangle([0, 0, canvas_w, header_h], fill=header_color)
        kw_part = f"  🔍 {keyword or '搜索结果'}"
        draw.text((pad, 18), kw_part, fill=(255, 255, 255), font=font_header)
        if page > 0:
            right_text = f"第 {page} 页 · {len(items)} 个"
            if mode:
                right_text += f" · {mode}"
            rb = font_header.getbbox(right_text)
            rw = rb[2] - rb[0]
            draw.text(
                (canvas_w - pad - rw - rb[0], 18),
                right_text,
                fill=(220, 230, 255),
                font=font_header,
            )

        # 并发下载所有缩略图
        thumbs_data = await asyncio.gather(
            *(self._fetch_thumb_bytes(it) for it in items[: columns * rows])
        )

        for idx, (item, raw) in enumerate(zip(items[: columns * rows], thumbs_data)):
            col = idx % columns
            row = idx // columns
            x0 = pad + col * (cell_w + pad)
            y0 = header_h + pad + row * (cell_h + pad)
            x1 = x0 + cell_w
            y1 = y0 + cell_h

            # 卡片底色（圆角矩形）
            self._rounded_rect(draw, [x0, y0, x1, y1], radius=10, fill=cell_bg)

            # 缩略图
            tx = x0 + (cell_w - thumb_size) // 2
            ty = y0 + 10
            if raw:
                try:
                    thumb = PILImage.open(io.BytesIO(raw)).convert("RGB")
                    thumb.thumbnail((thumb_size, thumb_size))
                    canvas.paste(thumb, (tx + (thumb_size - thumb.width) // 2, ty))
                except Exception:  # noqa: BLE001
                    self._placeholder(draw, [tx, ty, tx + thumb_size, ty + thumb_size])
            else:
                self._placeholder(draw, [tx, ty, tx + thumb_size, ty + thumb_size])

            # 红色圆序号（左上角）
            cx, cy, r = x0 + 22, y0 + 22, 18
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=badge_color)
            num = str(idx + 1)
            nb = font_badge.getbbox(num)
            nw, nh = nb[2] - nb[0], nb[3] - nb[1]
            draw.text(
                (cx - nw / 2 - nb[0], cy - nh / 2 - nb[1]),
                num,
                fill=(255, 255, 255),
                font=font_badge,
            )

            # 标题与画师
            title = str(item.get("title") or "无标题").replace("\n", " ")
            user = str(item.get("userName") or "未知画师")
            title_max_w = cell_w - 16
            # 拼图单元格空间有限，标题最多 1 行（完整标题在选图下载时通过 _caption 展示）
            wrapped = self._wrap_text(title, font_title, title_max_w)[:1]
            text_y = y0 + thumb_size + 14
            for line in wrapped:
                draw.text((x0 + 8, text_y), line, fill=text_color, font=font_title)
            # 画师行紧跟标题下方
            draw.text(
                (x0 + 8, text_y + 28),
                f"@{user}",
                fill=sub_color,
                font=font_sub,
            )

        # 底部提示条
        draw.rectangle([0, canvas_h - footer_h, canvas_w, canvas_h], fill=footer_color)
        footer_msg = (
            "1-9 选图  |  N 下一页  |  P 上一页  |  P数字 跳页  |  E/0 退出  "
            f"·  {SEARCH_SESSION_TTL}s 无操作自动退出"
        )
        fb = font_footer.getbbox(footer_msg)
        fw = fb[2] - fb[0]
        draw.text(
            ((canvas_w - fw) // 2 - fb[0], canvas_h - footer_h + 12),
            footer_msg,
            fill=footer_text,
            font=font_footer,
        )

        fd, path = tempfile.mkstemp(suffix=".png")
        with os.fdopen(fd, "wb") as f:
            canvas.save(f, format="PNG")
        p = Path(path)
        self._temp_files.append(p)
        self._schedule_cleanup(p)
        return p

    def _rounded_rect(
        self, draw: ImageDraw.ImageDraw, xy: list, radius: int, fill: tuple
    ) -> None:
        """绘制圆角矩形。"""
        draw.rounded_rectangle(xy, radius=radius, fill=fill)

    def _placeholder(self, draw: ImageDraw.ImageDraw, xy: list) -> None:
        """在缩略图位置绘制占位色块。"""
        draw.rectangle(xy, fill=(200, 200, 210))

    # ── 命令组 ────────────────────────────────────────────────────

    @filter.command_group("pixiv", alias={"pix"})
    def pixiv(self):
        """PixivNow 插画查询命令组。"""

    @pixiv.command("help")
    async def pixiv_help(self, event: AstrMessageEvent):
        """显示帮助信息。"""
        yield event.plain_result(
            "PixivNow 插件使用说明\n"
            "/pixiv random [n] [mode]  随机插画\n"
            "/pixiv rank [mode] [content] [p]  排行榜\n"
            "/pixiv search <关键词> [p] [mode]  搜索插画（结果为 3×3 拼图）\n"
            "  搜索后会话内：\n"
            "    1-9  下载对应原图\n"
            "    N    下一页\n"
            "    P    上一页\n"
            "    P数字 跳转到指定页\n"
            "    E/0  退出搜索会话\n"
            f"  无操作 {SEARCH_SESSION_TTL} 秒自动退出\n"
            "/pixiv illust <id>  画作详情\n"
            "/pixiv user <id>  画师主页\n"
            "/pixiv novel <id>  小说详情\n"
            "/pixiv seturl <地址>  设置 PixivNow 地址（管理员）\n"
            "/pixiv settoken <token>  设置 Pixiv 登录 token（管理员）\n"
            "mode 可选：safe / all / r18"
        )

    @pixiv.command("random")
    async def pixiv_random(self, event: AstrMessageEvent, n: int = 0, mode: str = ""):
        """随机插画。

        Args:
            n(int): 张数，0 表示使用默认。
            mode(string): safe/all/r18。
        """
        count = n if n and n > 0 else int(self.config.get("default_count", 1) or 1)
        count = min(max(count, 1), 10)
        mode = mode or str(self.config.get("default_mode", "safe") or "safe")
        if not self._mode_allowed(mode):
            yield event.plain_result("R18 模式未启用，或模式参数非法。")
            return

        try:
            data = await self._request(
                "/api/illust/random",
                {"format": "json", "max": count, "mode": mode},
            )
        except PixivNowError as e:
            yield event.plain_result(str(e))
            return

        works = data if isinstance(data, list) else []
        if not works:
            yield event.plain_result("没有获取到插画，可能该模式无可展示内容。")
            return

        for idx, item in enumerate(works[:count], 1):
            try:
                img = await self._download_best(item)
                yield event.chain_result(
                    [Plain(self._caption(item)), Image.fromFileSystem(str(img))]
                )
            except PixivNowError as e:
                logger.error(f"PixivNow random 图片发送失败: {e}")
                yield event.plain_result(f"第 {idx} 张图片下载失败：{e}")

    @pixiv.command("rank")
    async def pixiv_rank(
        self,
        event: AstrMessageEvent,
        mode: str = "daily",
        content: str = "all",
        p: int = 1,
    ):
        """Pixiv 排行榜。

        Args:
            mode(string): daily/weekly/monthly/rookie/male/female 等。
            content(string): all/illust/ugoira/manga。
            p(int): 页码。
        """
        try:
            data = await self._request(
                "/ranking.php",
                {
                    "format": "json",
                    "mode": mode,
                    "content": content,
                    "p": max(p, 1),
                },
            )
        except PixivNowError as e:
            yield event.plain_result(str(e))
            return

        if not isinstance(data, dict) or not data.get("contents"):
            yield event.plain_result("排行榜为空，可能该模式需要登录或账号设置限制。")
            return

        date = data.get("date_range_text") or data.get("date") or ""
        items = data["contents"][:5]
        yield event.plain_result(
            f"Pixiv 排行榜（{mode} · {content}）{date}\n"
            + "\n".join(
                f"{i['rank']}. {i['title']} - {i.get('user_name', '')} (ID:{i['illust_id']})"
                for i in items
            )
        )
        for item in items:
            try:
                img = await self._download_best(item)
                yield event.chain_result([Image.fromFileSystem(str(img))])
            except PixivNowError as e:
                logger.error(f"PixivNow rank 图片发送失败: {e}")

    async def _do_search(
        self, keyword: str, page: int = 1, mode: str = "safe"
    ) -> list[dict]:
        """执行一次 Pixiv 搜索并返回清理后的作品列表（不含广告）。

        Args:
            keyword: 搜索关键词。
            page: 页码（1 起步）。
            mode: safe/all/r18。

        Returns:
            该页的 ArtworkInfo 列表。

        Raises:
            PixivNowError: 请求或解析失败。
        """
        data = await self._request(
            f"/ajax/search/artworks/{quote(keyword)}",
            {"p": max(page, 1), "mode": mode},
        )
        body = self._unwrap(data)
        if not isinstance(body, dict):
            return []
        raw = (body.get("illustManga") or {}).get("data") or []
        return [w for w in raw if w.get("id")]

    async def _show_search_page(
        self, event: AstrMessageEvent, keyword: str, page: int, mode: str
    ):
        """在事件流中发送某一页搜索结果（带拼图与分页控制说明）。

        Args:
            event: 当前消息事件。
            keyword: 搜索关键词。
            page: 页码。
            mode: 模式。

        Yields:
            MessageEventResult 列表。
        """
        try:
            works = await self._do_search(keyword, page, mode)
        except PixivNowError as e:
            yield event.plain_result(str(e))
            return

        if not works:
            yield event.plain_result(f"第 {page} 页没有搜索到插画。")
            return

        top = works[:SEARCH_PAGE_SIZE]
        # 记录会话（用于翻页/选择/退出）
        self._search_sessions[event.unified_msg_origin] = {
            "keyword": keyword,
            "mode": mode,
            "page": page,
            "works": top,
            "ts": asyncio.get_event_loop().time(),
        }

        try:
            grid = await self._make_grid(top, keyword=keyword, page=page, mode=mode)
            yield event.chain_result(
                [
                    Plain(
                        f"关键词「{keyword}」第 {page} 页 · 共 {len(top)} 个结果\n"
                        "数字 1-9 选图 | N 下一页 | P 上一页 | P数字 跳页 | E 或 0 退出"
                    ),
                    Image.fromFileSystem(str(grid)),
                ]
            )
        except PixivNowError as e:
            yield event.plain_result(f"结果获取/拼图生成失败：{e}")

    @pixiv.command("search")
    async def pixiv_search(
        self, event: AstrMessageEvent, keyword: str, p: int = 1, mode: str = ""
    ):
        """搜索插画。

        Args:
            keyword(string): 搜索关键词。
            p(int): 页码。
            mode(string): safe/all/r18。
        """
        if not keyword:
            yield event.plain_result("用法：/pixiv search <关键词> [页码] [mode]")
            return
        mode = mode or str(self.config.get("default_mode", "safe") or "safe")
        if not self._mode_allowed(mode):
            yield event.plain_result("R18 模式未启用，或模式参数非法。")
            return
        async for r in self._show_search_page(event, keyword, max(p, 1), mode):
            yield r

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_session_input(self, event: AstrMessageEvent):
        """监听搜索会话内的所有指令：选图/翻页/跳页/退出。"""
        msg = event.message_str.strip()
        umo = event.unified_msg_origin
        session = self._search_sessions.get(umo)
        if not session:
            return

        # 过期自动清除
        if asyncio.get_event_loop().time() - session.get("ts", 0) > SEARCH_SESSION_TTL:
            self._search_sessions.pop(umo, None)
            return

        # 退出
        if msg.lower() in ("e", "exit", "quit", "0"):
            self._search_sessions.pop(umo, None)
            yield event.plain_result("已退出搜索会话。")
            return

        # 跳页：P<数字>（如 P3 / p12）
        m = re.match(r"^[Pp](\d+)$", msg)
        if m:
            target = max(int(m.group(1)), 1)
            session["ts"] = asyncio.get_event_loop().time()
            async for r in self._show_search_page(
                event, session["keyword"], target, session.get("mode", "safe")
            ):
                yield r
            return

        # 翻页
        if msg.lower() in ("n", "next"):
            target = session.get("page", 1) + 1
            session["ts"] = asyncio.get_event_loop().time()
            async for r in self._show_search_page(
                event, session["keyword"], target, session.get("mode", "safe")
            ):
                yield r
            return
        if msg.lower() in ("p", "prev", "previous"):
            target = max(session.get("page", 1) - 1, 1)
            session["ts"] = asyncio.get_event_loop().time()
            async for r in self._show_search_page(
                event, session["keyword"], target, session.get("mode", "safe")
            ):
                yield r
            return

        # 选图下载原图
        if not msg.isdigit():
            return
        idx = int(msg) - 1
        works = session.get("works") or []
        if idx < 0 or idx >= len(works):
            yield event.plain_result(
                f"序号超出范围，请回复 1-{len(works)} 之间的数字（E/0 退出）。"
            )
            return
        work = works[idx]
        session["ts"] = asyncio.get_event_loop().time()
        try:
            detail = self._unwrap(
                await self._request(f"/ajax/illust/{work['id']}?full=1")
            )
            item = detail if isinstance(detail, dict) else work
            img = await self._download_best(item)
            yield event.chain_result(
                [
                    Plain(self._caption(item)),
                    Image.fromFileSystem(str(img)),
                ]
            )
        except PixivNowError as e:
            yield event.plain_result(f"下载失败：{e}")

    @pixiv.command("illust")
    async def pixiv_illust(self, event: AstrMessageEvent, id: str):
        """画作详情（含多页）。

        Args:
            id(string): 画作 ID。
        """
        if not id or not id.isdigit():
            yield event.plain_result("用法：/pixiv illust <画作ID>")
            return
        try:
            body = self._unwrap(await self._request(f"/ajax/illust/{id}?full=1"))
        except PixivNowError as e:
            yield event.plain_result(str(e))
            return

        if not isinstance(body, dict):
            yield event.plain_result("未找到该画作。")
            return

        max_pages = int(self.config.get("max_pages", 5) or 5)
        page_count = int(body.get("pageCount") or 1)

        text = self._caption(body)
        if page_count > 1:
            text += f"\n共 {page_count} 页，先展示前 {min(page_count, max_pages)} 页"
        yield event.plain_result(text)

        # 多页时优先展示各页
        if page_count > 1:
            try:
                pages = self._unwrap(await self._request(f"/ajax/illust/{id}/pages"))
                if isinstance(pages, list):
                    for pg in pages[:max_pages]:
                        try:
                            img = await self._download_best(pg)
                            yield event.chain_result([Image.fromFileSystem(str(img))])
                        except PixivNowError as e:
                            logger.error(f"PixivNow illust 分页发送失败: {e}")
                    return
            except PixivNowError as e:
                logger.error(f"获取分页失败: {e}")

        # 单页：发送主图
        try:
            img = await self._download_best(body)
            yield event.chain_result([Image.fromFileSystem(str(img))])
        except PixivNowError as e:
            yield event.plain_result(str(e))

    @pixiv.command("user")
    async def pixiv_user(self, event: AstrMessageEvent, id: str):
        """画师主页。

        Args:
            id(string): 画师用户 ID。
        """
        if not id or not id.isdigit():
            yield event.plain_result("用法：/pixiv user <画师ID>")
            return
        try:
            body = self._unwrap(await self._request(f"/ajax/user/{id}?full=1"))
        except PixivNowError as e:
            yield event.plain_result(str(e))
            return
        if not isinstance(body, dict) or not body.get("name"):
            yield event.plain_result("未找到该画师。")
            return

        # Pixiv /ajax/user/{id} 接口不返回 followers 字段（实测），
        # 使用 following（关注数）与 mypixivCount（MyPixiv 收藏数）。
        stats = f"关注：{body.get('following')}"
        mypixiv = body.get("mypixivCount")
        if mypixiv is not None:
            stats += f"  MyPixiv：{mypixiv}"
        text = (
            f"画师：{body.get('name')}\n"
            f"ID：{body.get('userId')}\n"
            f"{stats}\n"
            f"简介：{(body.get('comment') or '无').replace(chr(10), ' ')[:200]}\n"
            f"主页：https://www.pixiv.net/users/{body.get('userId')}"
        )
        yield event.plain_result(text)
        avatar = body.get("imageBig") or body.get("image")
        if avatar:
            try:
                img = await self._download(avatar)
                yield event.chain_result([Image.fromFileSystem(str(img))])
            except PixivNowError as e:
                logger.error(f"PixivNow user 头像发送失败: {e}")

    @pixiv.command("novel")
    async def pixiv_novel(self, event: AstrMessageEvent, id: str):
        """小说详情。

        Args:
            id(string): 小说 ID。
        """
        if not id or not id.isdigit():
            yield event.plain_result("用法：/pixiv novel <小说ID>")
            return
        try:
            body = self._unwrap(await self._request(f"/ajax/novel/{id}"))
        except PixivNowError as e:
            yield event.plain_result(str(e))
            return
        if not isinstance(body, dict) or not body.get("title"):
            yield event.plain_result("未找到该小说。")
            return

        content = body.get("content") or ""
        # Pixiv novel 详情实际返回 wordCount/characterCount 与 useWordCount，
        # 不存在 textCount（实测）。优先显示接口推荐的计数字段。
        if body.get("useWordCount"):
            count = body.get("wordCount")
        else:
            count = body.get("characterCount") or body.get("textCount")
        text = (
            f"标题：{body.get('title')}\n"
            f"作者：{body.get('userName')}\n"
            f"字数：{count if count is not None else '未知'}\n"
            f"链接：https://www.pixiv.net/novel/show.php?id={body.get('id')}\n\n"
            f"{content[:1000]}"
        )
        yield event.plain_result(text)

    @pixiv.command("seturl")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def pixiv_seturl(self, event: AstrMessageEvent, url: str):
        """设置 PixivNow 地址（管理员）。

        Args:
            url(string): 新的 PixivNow 服务地址。
        """
        if not url or not url.startswith(("http://", "https://")):
            yield event.plain_result("地址需以 http:// 或 https:// 开头。")
            return
        self.config["pixivnow_url"] = url.rstrip("/")
        self.config.save_config()
        yield event.plain_result(f"已设置 PixivNow 地址为：{url.rstrip('/')}")

    @pixiv.command("settoken")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def pixiv_settoken(self, event: AstrMessageEvent, token: str):
        """设置 Pixiv 登录 token（管理员）。

        Args:
            token(string): Pixiv 登录 Cookie PHPSESSID。
        """
        if not token:
            yield event.plain_result("用法：/pixiv settoken <PHPSESSID>")
            return
        self.config["token"] = token.strip()
        self.config.save_config()
        yield event.plain_result("已设置 Pixiv 登录 token。")

    async def terminate(self):
        """插件卸载/停用时清理临时文件。"""
        for p in self._temp_files:
            try:
                p.unlink(missing_ok=True)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"清理临时文件失败 {p}: {e}")
