import asyncio
import copy
import hashlib
import os
import random
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

import httpx
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
import astrbot.api.message_components as Comp
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr
from .pixiv_grid_renderer import PixivGridRenderer

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

# 回复作品时可通过配置勾选展示的信息字段（key -> 中文名，用于提示）
CAPTION_FIELD_NAMES: dict[str, str] = {
    "title": "标题",
    "artist": "画师",
    "id": "作品ID",
    "bookmark": "收藏数",
    "like": "喜欢数",
    "tags": "标签",
    "link": "作品链接",
}
# 所有合法字段（与 _conf_schema.json 中 caption_fields 的 options 保持一致）
ALL_CAPTION_FIELDS: tuple[str, ...] = tuple(CAPTION_FIELD_NAMES)


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
        self._http_client: httpx.AsyncClient | None = None
        self._request_cache: dict[str, tuple[float, dict | list]] = {}
        self._byte_cache: dict[str, tuple[float, bytes]] = {}
        self._path_cache: dict[str, tuple[float, Path]] = {}
        self._render_cache: dict[str, tuple[float, Path]] = {}
        self._inflight_requests: dict[str, asyncio.Task] = {}
        self._inflight_bytes: dict[str, asyncio.Task] = {}
        self._network_semaphore = asyncio.Semaphore(
            min(max(int(self.config.get("max_concurrent_requests", 6) or 6), 1), 16)
        )

    @staticmethod
    def _is_onebot(event: AstrMessageEvent) -> bool:
        """仅 OneBot v11（aiocqhttp）支持合并转发节点。"""
        try:
            return "aiocqhttp" in event.get_platform_name().lower()
        except Exception:
            return False

    @staticmethod
    def _consume_event(event: AstrMessageEvent) -> None:
        """消费插件指令：禁止默认 LLM 调用并终止后续事件传播。"""
        event.should_call_llm(False)
        event.stop_event()

    # ── 基础工具 ──────────────────────────────────────────────────

    def _cache_ttl(self, kind: str) -> int:
        defaults = {"api": 180, "image": 300, "render": 180}
        key = {"api": "api_cache_ttl", "image": "image_cache_ttl", "render": "render_cache_ttl"}[kind]
        return min(max(int(self.config.get(key, defaults[kind]) or defaults[kind]), 0), 3600)

    def _prune_cache(self, cache: dict, max_items: int) -> None:
        now = time.monotonic()
        for key, (expires, _) in list(cache.items()):
            if expires <= now:
                cache.pop(key, None)
        if len(cache) > max_items:
            for key, _ in sorted(cache.items(), key=lambda pair: pair[1][0])[: len(cache) - max_items]:
                cache.pop(key, None)

    @staticmethod
    def _cache_get(cache: dict, key: str):
        value = cache.get(key)
        if not value:
            return None
        expires, data = value
        if expires <= time.monotonic():
            cache.pop(key, None)
            return None
        return data

    @staticmethod
    def _cache_put(cache: dict, key: str, value, ttl: int) -> None:
        if ttl > 0:
            cache[key] = (time.monotonic() + ttl, value)

    async def _client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            timeout = max(int(self.config.get("download_timeout", 20) or 20), 5)
            keepalive = bool(self.config.get("http_keepalive_enabled", False))
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(timeout),
                follow_redirects=True,
                transport=httpx.AsyncHTTPTransport(retries=1),
                limits=httpx.Limits(
                    max_connections=12,
                    max_keepalive_connections=6 if keepalive else 0,
                    keepalive_expiry=10.0 if keepalive else 0.0,
                ),
                headers=self._headers(),
            )
        return self._http_client

    def _clear_runtime_caches(self) -> None:
        self._request_cache.clear()
        self._byte_cache.clear()
        self._path_cache.clear()
        self._render_cache.clear()

    async def _reset_client(self) -> None:
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()
        self._http_client = None
        self._clear_runtime_caches()

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
            ("thumb", "small", "regular")
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

    async def _request(
        self,
        path: str,
        params: dict | None = None,
        *,
        use_cache: bool = True,
    ) -> dict | list:
        """请求 PixivNow 接口并返回解析后的 JSON。"""
        url = self._base_url() + path
        param_items = tuple(sorted((str(k), str(v)) for k, v in (params or {}).items()))
        cache_key = f"GET:{url}:{param_items}"
        if use_cache:
            cached = self._cache_get(self._request_cache, cache_key)
            if cached is not None:
                return copy.deepcopy(cached)

        async def fetch():
            try:
                async with self._network_semaphore:
                    resp = await (await self._client()).get(url, params=params)
            except httpx.HTTPError as e:
                raise PixivNowError(f"请求 PixivNow 失败: {e}") from e
            if resp.status_code != 200:
                raise PixivNowError(f"PixivNow 返回 HTTP {resp.status_code}（{url}）")
            try:
                data = resp.json()
            except ValueError as e:
                raise PixivNowError("PixivNow 返回的不是有效 JSON") from e
            if use_cache:
                self._cache_put(self._request_cache, cache_key, data, self._cache_ttl("api"))
                self._prune_cache(self._request_cache, 128)
            return data

        inflight_key = cache_key if use_cache else f"{cache_key}:{time.monotonic_ns()}"
        task = self._inflight_requests.get(inflight_key)
        if task is None:
            task = asyncio.create_task(fetch())
            self._inflight_requests[inflight_key] = task
        try:
            return copy.deepcopy(await task)
        finally:
            if self._inflight_requests.get(inflight_key) is task:
                self._inflight_requests.pop(inflight_key, None)

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
        cached = self._cache_get(self._byte_cache, target)
        if cached is not None:
            return cached

        async def fetch():
            try:
                async with self._network_semaphore:
                    resp = await (await self._client()).get(target)
            except httpx.HTTPError as e:
                raise PixivNowError(f"图片下载失败: {e}") from e
            if resp.status_code != 200:
                raise PixivNowError(f"图片下载失败 HTTP {resp.status_code}")
            content = resp.content
            max_bytes = min(
                max(int(self.config.get("max_memory_image_mb", 6) or 6), 0), 32
            ) * 1024 * 1024
            if max_bytes and len(content) <= max_bytes:
                self._cache_put(self._byte_cache, target, content, self._cache_ttl("image"))
                self._prune_cache(self._byte_cache, 64)
            return content

        task = self._inflight_bytes.get(target)
        if task is None:
            task = asyncio.create_task(fetch())
            self._inflight_bytes[target] = task
        try:
            return await task
        finally:
            if self._inflight_bytes.get(target) is task:
                self._inflight_bytes.pop(target, None)

    async def _download(self, url: str) -> Path:
        """下载图片到临时文件并返回其路径。

        Args:
            url: 图片 URL（自动经 PixivNow 代理解析）。

        Returns:
            临时文件路径。

        Raises:
            PixivNowError: 下载失败时。
        """
        target = self._resolve_url(url)
        cached_path = self._cache_get(self._path_cache, target)
        if cached_path is not None and cached_path.exists():
            return cached_path
        content = await self._fetch_bytes(url)
        suffix = Path(url.split("?")[0]).suffix or ".jpg"
        fd, path = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        p = Path(path)
        self._temp_files.append(p)
        ttl = max(self._cache_ttl("image"), 120)
        self._cache_put(self._path_cache, target, p, ttl)
        self._prune_cache(self._path_cache, 64)
        self._schedule_cleanup(p, ttl + 30)
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

    def _renderer(self) -> PixivGridRenderer:
        """按当前配置创建统一主题渲染器。"""
        theme = str(self.config.get("render_theme", "dark") or "dark").lower()
        font_path = str(self.config.get("render_font_path", "") or "").strip()
        return PixivGridRenderer(theme=theme, font_path=font_path or None)

    def _save_canvas(self, canvas) -> Path:
        """保存渲染结果到延迟清理的临时 PNG。"""
        fd, path = tempfile.mkstemp(suffix=".png")
        with os.fdopen(fd, "wb") as file:
            canvas.save(file, format="PNG", optimize=True)
        result = Path(path)
        self._temp_files.append(result)
        self._schedule_cleanup(result, max(self._cache_ttl("render"), 120) + 30)
        return result

    @staticmethod
    def _parse_tail_options(
        raw: str,
        *,
        default_count: int | None = None,
        default_page: int | None = None,
        default_mode: str = "safe",
    ) -> tuple[str, int | None, int | None, str]:
        """解析“主体文本 [数字] [模式]”，主体可包含空格。"""
        tokens = str(raw).strip().split()
        mode = default_mode
        if tokens and tokens[-1].lower() in ("safe", "all", "r18"):
            mode = tokens.pop().lower()
        number: int | None = None
        if tokens and tokens[-1].isdigit():
            number = int(tokens.pop())
        count = number if default_count is not None and number is not None else default_count
        page = number if default_page is not None and number is not None else default_page
        return " ".join(tokens).strip(), count, page, mode

    async def _send_card_separately(
        self, event: AstrMessageEvent, path: Path
    ) -> bool:
        """像 rika_share 一样主动单独发送信息卡，避免引用回复污染图片。"""
        try:
            sent = await self.context.send_message(
                event.unified_msg_origin,
                MessageChain().file_image(str(path)),
            )
            if sent:
                logger.info(f"PixivNow 信息卡已单独发送: {path.name}")
                return True
            logger.warning(f"PixivNow 信息卡主动发送未找到匹配平台会话: {path.name}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"PixivNow 信息卡主动发送异常，将回退事件回复: {e}")
        return False

    async def _send_image_collection(
        self,
        event: AstrMessageEvent,
        paths: list[Path],
        header: str = "",
    ):
        """发送原图集合：OneBot 合并转发，其他平台使用普通图片消息链。"""
        if not paths:
            return
        if self._is_onebot(event) and len(paths) > 1:
            sender_name = event.get_sender_name()
            sender_id = event.get_sender_id()
            nodes = Comp.Nodes([])
            if header:
                nodes.nodes.append(
                    Comp.Node(uin=sender_id, name=sender_name, content=[Comp.Plain(header)])
                )
            for path in paths:
                nodes.nodes.append(
                    Comp.Node(
                        uin=sender_id,
                        name=sender_name,
                        content=[Comp.Image.fromFileSystem(str(path))],
                    )
                )
            yield event.chain_result([nodes])
            return

        parts: list = []
        if header:
            parts.append(Comp.Plain(header))
        parts.extend(Comp.Image.fromFileSystem(str(path)) for path in paths)
        yield event.chain_result(parts)

    async def _make_detail_card(
        self,
        kind: str,
        data: dict,
        media_url: str = "",
        media_bytes: bytes | None = None,
    ) -> Path:
        """下载详情卡媒体并调用对应渲染布局。"""
        detail_id = str(data.get("id") or data.get("userId") or "")
        render_key = hashlib.sha1(
            f"detail|{kind}|{detail_id}|{data.get('title') or data.get('name')}|{self.config.get('render_theme')}|{self.config.get('render_font_path')}".encode()
        ).hexdigest()
        cached = self._cache_get(self._render_cache, render_key)
        if cached is not None and cached.exists():
            return cached

        media: bytes | None = media_bytes
        if media is None and kind == "illust":
            media = await self._fetch_thumb_bytes(data)
        elif media is None and media_url:
            try:
                media = await self._fetch_bytes(media_url)
            except PixivNowError as e:
                logger.warning(f"PixivNow {kind} 卡片媒体下载失败，使用占位图: {e}")
        renderer = self._renderer()
        render_method = {
            "illust": renderer.render_illust_detail,
            "user": renderer.render_user_detail,
            "novel": renderer.render_novel_detail,
        }[kind]
        canvas = await asyncio.to_thread(render_method, data, media)
        result = self._save_canvas(canvas)
        self._cache_put(self._render_cache, render_key, result, self._cache_ttl("render"))
        self._prune_cache(self._render_cache, 32)
        return result

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

    def _caption_fields(self) -> list[str]:
        """读取配置，返回回复作品时应展示的信息字段列表。

        返回配置项 caption_fields 中勾选且合法的字段；若从未配置
        （如旧版插件升级后未保存过配置），则默认展示全部字段。
        """
        raw = self.config.get("caption_fields")
        if raw is None:
            return list(ALL_CAPTION_FIELDS)
        if not isinstance(raw, list):
            return list(ALL_CAPTION_FIELDS)
        return [f for f in raw if f in ALL_CAPTION_FIELDS]

    def _caption(self, item: dict, *, extra: str = "") -> str:
        """根据配置勾选的字段构造作品文本说明。

        仅展示配置项 caption_fields 中勾选的信息（标题、画师、ID、
        收藏、喜欢、标签、链接等）；全部取消勾选时返回空字符串，
        即只发送图片不带文字。

        Args:
            item: 作品对象。
            extra: 附加说明行（如多页提示），始终展示。

        Returns:
            组装好的文本说明。
        """
        fields = self._caption_fields()
        lines: list[str] = []
        if "title" in fields and item.get("title"):
            lines.append(f"标题：{item['title']}")
        if "artist" in fields and item.get("userName"):
            lines.append(f"画师：{item['userName']}")
        if "id" in fields and item.get("id"):
            lines.append(f"ID：{item['id']}")
        if "bookmark" in fields and item.get("bookmarkCount") is not None:
            lines.append(f"收藏：{item['bookmarkCount']}")
        if "like" in fields and item.get("likeCount") is not None:
            lines.append(f"喜欢：{item['likeCount']}")
        if "tags" in fields and item.get("tags"):
            tags = self._extract_tags(item["tags"])
            if tags:
                max_tags = max(int(self.config.get("max_tags", 10) or 10), 1)
                shown = tags[:max_tags]
                suffix = f" 等 {len(tags)} 个标签" if len(tags) > max_tags else ""
                lines.append("标签：" + " ".join(shown) + suffix)
        if "link" in fields and item.get("id"):
            lines.append(f"链接：https://www.pixiv.net/artworks/{item['id']}")
        if extra:
            lines.append(extra)
        return "\n".join(lines)

    @staticmethod
    def _extract_tags(tags) -> list[str]:
        """把 Pixiv 返回的 tags 字段统一为字符串列表。

        兼容两种结构：{"tags": [{"tag": "xxx"}, ...]} 与 ["xxx", ...]。
        """
        if isinstance(tags, dict):
            tags = tags.get("tags") or []
        if not isinstance(tags, list):
            return []
        out: list[str] = []
        for t in tags:
            if isinstance(t, dict):
                t = t.get("tag") or ""
            if isinstance(t, str) and t:
                out.append(t)
        return out

    def _caption_chain(self, item: dict, img: Path, *, extra: str = "") -> list:
        """组装作品回复消息链：文字说明（按配置勾选，可为空）+ 图片。

        Args:
            item: 作品对象。
            img: 已下载的图片临时文件路径。
            extra: 附加说明行。

        Returns:
            可直接用于 chain_result 的消息组件列表。
        """
        caption = self._caption(item, extra=extra)
        chain: list = []
        if caption:
            chain.append(Plain(caption))
        chain.append(Image.fromFileSystem(str(img)))
        return chain

    def _search_selection_result(
        self,
        event: AstrMessageEvent,
        item: dict,
        img: Path,
    ):
        """构造搜索选图回复；OneBot 将文字与原图拆为合并转发节点。"""
        caption = self._caption(item)
        if not self._is_onebot(event):
            return event.chain_result(self._caption_chain(item, img))

        sender_name = event.get_sender_name()
        sender_id = event.get_sender_id()
        nodes = Comp.Nodes([])
        if caption:
            nodes.nodes.append(
                Comp.Node(
                    uin=sender_id,
                    name=sender_name,
                    content=[Comp.Plain(caption)],
                )
            )
        nodes.nodes.append(
            Comp.Node(
                uin=sender_id,
                name=sender_name,
                content=[Comp.Image.fromFileSystem(str(img))],
            )
        )
        return event.chain_result([nodes])

    def _needs_illust_detail(self, item: dict) -> bool:
        """仅在配置字段确实缺失时补查完整作品详情。"""
        if not self.config.get("fetch_detailed_metadata", False):
            return False
        fields = self._caption_fields()
        required = {
            "bookmark": "bookmarkCount",
            "like": "likeCount",
            "tags": "tags",
        }
        return any(field in fields and item.get(key) is None for field, key in required.items())

    async def _enrich_illust(self, item: dict) -> dict:
        if not self._needs_illust_detail(item) or not item.get("id"):
            return item
        detail = self._unwrap(await self._request(f"/ajax/illust/{item['id']}?full=1"))
        return detail if isinstance(detail, dict) else item

    def _artwork_collection_result(
        self,
        event: AstrMessageEvent,
        artworks: list[tuple[dict, Path]],
        header: str = "",
    ):
        """统一发送一组作品；OneBot 多图使用合并转发，减少消息条数。"""
        if self._is_onebot(event) and len(artworks) > 1:
            sender_name = event.get_sender_name()
            sender_id = event.get_sender_id()
            nodes = Comp.Nodes([])
            if header:
                nodes.nodes.append(Comp.Node(uin=sender_id, name=sender_name, content=[Comp.Plain(header)]))
            for item, path in artworks:
                content: list = []
                caption = self._caption(item)
                if caption:
                    content.append(Comp.Plain(caption))
                content.append(Comp.Image.fromFileSystem(str(path)))
                nodes.nodes.append(Comp.Node(uin=sender_id, name=sender_name, content=content))
            return event.chain_result([nodes])

        chain: list = []
        if header and len(artworks) > 1:
            chain.append(Comp.Plain(header))
        for item, path in artworks:
            chain.extend(self._caption_chain(item, path))
        return event.chain_result(chain)

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
        item_ids = ",".join(str(item.get("id") or "") for item in items)
        render_key = hashlib.sha1(
            f"grid|{keyword}|{page}|{mode}|{columns}|{item_ids}|{self.config.get('render_theme')}|{self.config.get('render_font_path')}".encode()
        ).hexdigest()
        cached = self._cache_get(self._render_cache, render_key)
        if cached is not None and cached.exists():
            return cached

        # 并发下载所有缩略图
        thumbs_data = await asyncio.gather(
            *(self._fetch_thumb_bytes(it) for it in items)
        )
        theme = str(self.config.get("render_theme", "dark") or "dark").lower()
        font_path = str(self.config.get("render_font_path", "") or "").strip()
        renderer = PixivGridRenderer(
            theme=theme,
            columns=columns,
            font_path=font_path or None,
        )
        canvas = await asyncio.to_thread(
            renderer.render, items, thumbs_data, keyword, page, mode
        )

        fd, path = tempfile.mkstemp(suffix=".png")
        with os.fdopen(fd, "wb") as f:
            canvas.save(f, format="PNG")
        p = Path(path)
        self._temp_files.append(p)
        ttl = max(self._cache_ttl("render"), 120)
        self._cache_put(self._render_cache, render_key, p, ttl)
        self._prune_cache(self._render_cache, 32)
        self._schedule_cleanup(p, ttl + 30)
        return p

    async def _make_rank_card(
        self,
        items: list[dict],
        *,
        mode: str,
        content: str,
        page: int,
        date: str,
    ) -> Path:
        """下载排行榜缩略图并渲染为单张主题海报。"""
        item_ids = ",".join(str(item.get("illust_id") or item.get("id") or "") for item in items)
        render_key = hashlib.sha1(
            f"rank|{mode}|{content}|{page}|{date}|{item_ids}|{self.config.get('render_theme')}|{self.config.get('render_font_path')}".encode()
        ).hexdigest()
        cached = self._cache_get(self._render_cache, render_key)
        if cached is not None and cached.exists():
            return cached
        thumbs = await asyncio.gather(*(self._fetch_thumb_bytes(item) for item in items))
        renderer = self._renderer()
        canvas = await asyncio.to_thread(
            renderer.render_ranking,
            items,
            thumbs,
            mode,
            content,
            page,
            date,
        )
        fd, path = tempfile.mkstemp(suffix=".png")
        with os.fdopen(fd, "wb") as file:
            canvas.save(file, format="PNG", optimize=True)
        result = Path(path)
        self._temp_files.append(result)
        ttl = max(self._cache_ttl("render"), 120)
        self._cache_put(self._render_cache, render_key, result, ttl)
        self._prune_cache(self._render_cache, 32)
        self._schedule_cleanup(result, ttl + 30)
        return result

    # ── 命令组 ────────────────────────────────────────────────────

    @filter.command_group("pixiv", alias={"pix"})
    def pixiv(self):
        """PixivNow 插画查询命令组。"""

    @pixiv.command("help", alias={"h"})
    async def pixiv_help(self, event: AstrMessageEvent):
        """显示帮助信息。"""
        self._consume_event(event)
        yield event.plain_result(
            "PixivNow 指令\n\n"
            "发现作品\n"
            "/pixiv r [数量] [模式]  随机插画\n"
            "/pixiv rk <关键词> [数量] [模式]  关键词随机\n"
            "/pixiv s <关键词> [页码] [模式]  搜索并进入选图会话\n"
            "/pixiv top [模式] [类型] [页码]  排行榜\n\n"
            "查看详情\n"
            "/pixiv i <作品ID>  作品详情与原图\n"
            "/pixiv u <画师ID>  画师资料\n"
            "/pixiv n <小说ID>  小说摘要\n\n"
            "  搜索后会话内：\n"
            "    1-9  下载对应原图\n"
            "    N    下一页\n"
            "    P    上一页\n"
            "    P数字 跳转到指定页\n"
            "    E/0  退出搜索会话\n"
            f"  无操作 {SEARCH_SESSION_TTL} 秒自动退出\n"
            "\n完整名称 random / random_keyword / search / rank / illust / user / novel 仍兼容\n"
            "/pixiv seturl <地址>  设置 PixivNow 地址（管理员）\n"
            "/pixiv settoken <token>  设置 Pixiv 登录 token（管理员）\n"
            "mode 可选：safe / all / r18\n"
            "可在管理面板配置回复附带的信息字段（标题/画师/标签/链接等）"
        )

    @pixiv.command("random", alias={"r"})
    async def pixiv_random(self, event: AstrMessageEvent, n: int = 0, mode: str = ""):
        """随机插画。

        Args:
            n(int): 张数，0 表示使用默认。
            mode(string): safe/all/r18。
        """
        self._consume_event(event)
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
                use_cache=False,
            )
        except PixivNowError as e:
            yield event.plain_result(str(e))
            return

        works = data if isinstance(data, list) else []
        if not works:
            yield event.plain_result("没有获取到插画，可能该模式无可展示内容。")
            return

        selected = works[:count]
        downloads = await asyncio.gather(
            *(self._download_best(item) for item in selected),
            return_exceptions=True,
        )
        ready: list[tuple[dict, Path]] = []
        for index, (item, result) in enumerate(zip(selected, downloads), 1):
            if isinstance(result, Path):
                ready.append((item, result))
            else:
                logger.error(f"PixivNow random 第 {index} 张下载失败: {result}")
        if ready:
            yield self._artwork_collection_result(event, ready, f"Pixiv 随机插画 · {mode.upper()}")
        elif downloads:
            yield event.plain_result("随机插画下载失败，请稍后重试。")

    @pixiv.command("random_keyword", alias={"rk", "krandom"})
    async def pixiv_random_keyword(
        self,
        event: AstrMessageEvent,
        query: GreedyStr,
    ):
        """从指定关键词的搜索结果中随机抽取插画。

        Args:
            query(string): 关键词以及可选的数量、模式，例如 原神 风景 3 safe。
        """
        self._consume_event(event)
        tokens = str(query).strip().split()
        if not tokens:
            yield event.plain_result(
                "用法：/pixiv random_keyword <关键词> [数量] [mode]"
            )
            return

        mode = str(self.config.get("default_mode", "safe") or "safe")
        if tokens and tokens[-1].lower() in ("safe", "all", "r18"):
            mode = tokens.pop().lower()

        count = int(self.config.get("default_count", 1) or 1)
        if tokens and tokens[-1].isdigit():
            count = int(tokens.pop())
        count = min(max(count, 1), 10)

        keyword = " ".join(tokens).strip()
        if not keyword:
            yield event.plain_result("关键词不能为空。")
            return
        if not self._mode_allowed(mode):
            yield event.plain_result("R18 模式未启用，或模式参数非法。")
            return

        page_count = min(
            max(int(self.config.get("keyword_random_pages", 3) or 3), 1),
            10,
        )
        works: list[dict] = []
        seen: set[str] = set()
        last_error: PixivNowError | None = None
        # 渐进获取：候选数量足够后立即停止，避免固定扫满所有页面。
        candidate_target = max(count * 4, 12)
        for page in range(1, page_count + 1):
            try:
                result = await self._do_search(keyword, page, mode)
            except PixivNowError as e:
                last_error = e
                continue
            for work in result:
                work_id = str(work.get("id") or "")
                if work_id and work_id not in seen:
                    seen.add(work_id)
                    works.append(work)
            if len(works) >= candidate_target:
                break

        if not works:
            if last_error:
                yield event.plain_result(str(last_error))
            else:
                yield event.plain_result(f"没有搜索到与「{keyword}」相关的插画。")
            return

        selected = random.sample(works, min(count, len(works)))

        async def prepare(work: dict):
            item = await self._enrich_illust(work)
            return item, await self._download_best(item)

        prepared = await asyncio.gather(*(prepare(work) for work in selected), return_exceptions=True)
        ready: list[tuple[dict, Path]] = []
        for index, result in enumerate(prepared, 1):
            if isinstance(result, tuple):
                ready.append(result)
            else:
                logger.error(f"PixivNow 关键词随机第 {index} 张发送失败: {result}")
        if ready:
            yield self._artwork_collection_result(
                event,
                ready,
                f"关键词随机 · {keyword} · {mode.upper()}",
            )
        else:
            yield event.plain_result("候选作品下载失败，请稍后重试。")

    @pixiv.command("rank", alias={"top"})
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
        self._consume_event(event)
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
        try:
            card = await self._make_rank_card(
                items,
                mode=mode,
                content=content,
                page=max(p, 1),
                date=str(date),
            )
            if not await self._send_card_separately(event, card):
                yield event.chain_result([Image.fromFileSystem(str(card))])
        except Exception as e:  # noqa: BLE001
            logger.error(f"PixivNow rank 排行榜渲染失败: {e}")
            yield event.plain_result(
                f"Pixiv 排行榜（{mode} · {content}）{date}\n"
                + "\n".join(
                    f"{i['rank']}. {i['title']} - {i.get('user_name', '')} (ID:{i['illust_id']})"
                    for i in items
                )
            )

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
            if not await self._send_card_separately(event, grid):
                yield event.chain_result([Image.fromFileSystem(str(grid))])
        except PixivNowError as e:
            yield event.plain_result(f"结果获取/拼图生成失败：{e}")

    @pixiv.command("search", alias={"s"})
    async def pixiv_search(
        self, event: AstrMessageEvent, query: GreedyStr
    ):
        """搜索插画。

        Args:
            query(string): 关键词以及可选页码、模式。
        """
        self._consume_event(event)
        keyword, _, p, mode = self._parse_tail_options(
            str(query),
            default_page=1,
            default_mode=str(self.config.get("default_mode", "safe") or "safe"),
        )
        if not keyword:
            yield event.plain_result("用法：/pixiv search <关键词> [页码] [mode]")
            return
        if not self._mode_allowed(mode):
            yield event.plain_result("R18 模式未启用，或模式参数非法。")
            return
        async for r in self._show_search_page(event, keyword, max(p or 1, 1), mode):
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
            self._consume_event(event)
            self._search_sessions.pop(umo, None)
            yield event.plain_result("已退出搜索会话。")
            return

        # 跳页：P<数字>（如 P3 / p12）
        m = re.match(r"^[Pp](\d+)$", msg)
        if m:
            self._consume_event(event)
            target = max(int(m.group(1)), 1)
            session["ts"] = asyncio.get_event_loop().time()
            async for r in self._show_search_page(
                event, session["keyword"], target, session.get("mode", "safe")
            ):
                yield r
            return

        # 翻页
        if msg.lower() in ("n", "next"):
            self._consume_event(event)
            target = session.get("page", 1) + 1
            session["ts"] = asyncio.get_event_loop().time()
            async for r in self._show_search_page(
                event, session["keyword"], target, session.get("mode", "safe")
            ):
                yield r
            return
        if msg.lower() in ("p", "prev", "previous"):
            self._consume_event(event)
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
        self._consume_event(event)
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
            item = await self._enrich_illust(work)
            img = await self._download_best(item)
            yield self._search_selection_result(event, item, img)
        except PixivNowError as e:
            yield event.plain_result(f"下载失败：{e}")

    @pixiv.command("illust", alias={"i"})
    async def pixiv_illust(self, event: AstrMessageEvent, id: str):
        """画作详情（含多页）。

        Args:
            id(string): 画作 ID。
        """
        self._consume_event(event)
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
        paths: list[Path] = []
        try:
            if page_count > 1:
                pages = self._unwrap(await self._request(f"/ajax/illust/{id}/pages"))
                if isinstance(pages, list):
                    downloads = await asyncio.gather(
                        *(self._download_best(pg) for pg in pages[:max_pages]),
                        return_exceptions=True,
                    )
                    paths = [result for result in downloads if isinstance(result, Path)]
            else:
                paths = [await self._download_best(body)]
        except PixivNowError as e:
            logger.warning(f"作品原图下载失败，仍尝试发送信息卡: {e}")

        media_bytes: bytes | None = None
        if paths:
            try:
                media_bytes = await asyncio.to_thread(paths[0].read_bytes)
            except OSError:
                pass
        try:
            card = await self._make_detail_card("illust", body, media_bytes=media_bytes)
            if not await self._send_card_separately(event, card):
                yield event.chain_result([Image.fromFileSystem(str(card))])
        except Exception as e:  # noqa: BLE001
            logger.error(f"PixivNow illust 信息卡渲染失败: {e}")
            text = self._caption(body)
            if page_count > 1:
                text += f"\n共 {page_count} 页，展示前 {len(paths)} 页"
            if text:
                yield event.plain_result(text)

        if paths:
            header = f"作品原图 · 共 {page_count} 页，展示前 {len(paths)} 页" if page_count > 1 else ""
            async for result in self._send_image_collection(event, paths, header):
                yield result
        else:
            yield event.plain_result("作品信息已获取，但原图下载失败。")

    @pixiv.command("user", alias={"u"})
    async def pixiv_user(self, event: AstrMessageEvent, id: str):
        """画师主页。

        Args:
            id(string): 画师用户 ID。
        """
        self._consume_event(event)
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

        avatar = body.get("imageBig") or body.get("image")
        try:
            card = await self._make_detail_card("user", body, str(avatar or ""))
            if not await self._send_card_separately(event, card):
                yield event.chain_result([Image.fromFileSystem(str(card))])
        except Exception as e:  # noqa: BLE001
            logger.error(f"PixivNow user 信息卡渲染失败: {e}")
            stats = f"关注：{body.get('following')}"
            if body.get("mypixivCount") is not None:
                stats += f"  MyPixiv：{body.get('mypixivCount')}"
            yield event.plain_result(
                f"画师：{body.get('name')}\nID：{body.get('userId')}\n{stats}\n"
                f"简介：{(body.get('comment') or '无').replace(chr(10), ' ')[:200]}\n"
                f"主页：https://www.pixiv.net/users/{body.get('userId')}"
            )

    @pixiv.command("novel", alias={"n"})
    async def pixiv_novel(self, event: AstrMessageEvent, id: str):
        """小说详情。

        Args:
            id(string): 小说 ID。
        """
        self._consume_event(event)
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

        cover = body.get("coverUrl") or ""
        try:
            card = await self._make_detail_card("novel", body, str(cover))
            if not await self._send_card_separately(event, card):
                yield event.chain_result([Image.fromFileSystem(str(card))])
        except Exception as e:  # noqa: BLE001
            logger.error(f"PixivNow novel 信息卡渲染失败: {e}")
            count = (
                body.get("wordCount")
                if body.get("useWordCount")
                else body.get("characterCount") or body.get("textCount")
            )
            yield event.plain_result(
                f"标题：{body.get('title')}\n作者：{body.get('userName')}\n"
                f"字数：{count if count is not None else '未知'}\n"
                f"链接：https://www.pixiv.net/novel/show.php?id={body.get('id')}\n\n"
                f"{str(body.get('content') or '')[:1000]}"
            )

    @pixiv.command("seturl")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def pixiv_seturl(self, event: AstrMessageEvent, url: str):
        """设置 PixivNow 地址（管理员）。

        Args:
            url(string): 新的 PixivNow 服务地址。
        """
        self._consume_event(event)
        if not url or not url.startswith(("http://", "https://")):
            yield event.plain_result("地址需以 http:// 或 https:// 开头。")
            return
        self.config["pixivnow_url"] = url.rstrip("/")
        self.config.save_config()
        await self._reset_client()
        yield event.plain_result(f"已设置 PixivNow 地址为：{url.rstrip('/')}")

    @pixiv.command("settoken")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def pixiv_settoken(self, event: AstrMessageEvent, token: str):
        """设置 Pixiv 登录 token（管理员）。

        Args:
            token(string): Pixiv 登录 Cookie PHPSESSID。
        """
        self._consume_event(event)
        if not token:
            yield event.plain_result("用法：/pixiv settoken <PHPSESSID>")
            return
        self.config["token"] = token.strip()
        self.config.save_config()
        await self._reset_client()
        yield event.plain_result("已设置 Pixiv 登录 token。")

    async def terminate(self):
        """插件卸载/停用时清理临时文件。"""
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()
        for p in self._temp_files:
            try:
                p.unlink(missing_ok=True)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"清理临时文件失败 {p}: {e}")
