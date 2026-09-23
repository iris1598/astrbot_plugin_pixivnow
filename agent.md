# agent.md — PixivNow 插件 · AI 开发指南

> 本文件面向在本仓库工作的 AI / 开发者：**先读这里再动手**，用于快速定位代码、判断改哪里、改完怎么验证。
> ⚠️ **重要改动后必须同步更新本文件**，规则见最后一节「维护要求」。

---

## 0. 一句话概览

AstrBot 插件：把聊天指令转发给**自托管的 PixivNow 服务**（本插件不直连 Pixiv），拿到作品数据后下载图片、渲染主题信息卡、按平台能力发送。

| 项 | 值 |
| :--- | :--- |
| 框架 | AstrBot（`metadata.yaml: astrbot_version: ">=4.9.2"`），Python 3.10+ |
| 插件身份 | `metadata.yaml`: `name: astrbot_plugin_pixivnow` / `author: astrbot_plugin_pixivnow` / `display_name: PixivNow` |
| 版本号 | **两处必须逐字相同**：`metadata.yaml: version`（AstrBot 与市场身份校验的唯一依据）、`README.md` 顶部「版本：」一行。本插件**没有** `@register(...)`，所以 `main.py` 里不存在版本号 |
| 插件目录名 | `astrbot_plugin_pixivnow`（**不要改**） |
| 入口 | `main.py` → 插件类 `PixivNowPlugin(Star)`（无 `@register`，靠 `Star` 子类 + `*Plugin` 命名被框架发现） |
| 实现主体 | `main.py`（全部逻辑）+ `pixiv_grid_renderer.py`（纯 Pillow 渲染） |
| 配置维护入口 | 原生配置面板（本插件**不**隐藏配置项，`_conf_schema.json` 全部可见） |
| 变更日志 | 仓库**暂无** `CHANGELOG.md`；`agent.md` 只描述**当前状态** |
| 上游 | PixivNow 服务：[features-pixiv/pixiv-now](https://github.com/features-pixiv/pixiv-now)；卡片视觉语言参考 [astrbot_plugin_rika_share](https://github.com/iris1598/astrbot_plugin_rika_share) |
| 依赖 | `httpx>=0.24`、`Pillow>=9.0`（见 `requirements.txt`） |

> 已知偏差：无。（`metadata.yaml` 与 `README.md` 的版本号当前都是 `2.2.2`，改动版本时记得两处一起改。）

---

## 1. 铁律（改代码前必读）

1. **所有 Handler 必须写在 `main.py`。**
   AstrBot 用 `handler.handler_module_path == 插件主模块` **精确匹配**来归属 Handler（框架源码 `<AstrBot>/astrbot/core/star/register/star_handler.py` 的 `get_handler_or_create` 里 `handler_module_path=handler.__module__`，不在本仓库）。
   本插件全部指令与监听器都在 `main.py` 里用装饰器定义，**不要**把带 `@filter.*` / `@pixiv.command` 的函数挪到其他模块——挪走即静默失效。渲染、下载、缓存等**纯逻辑**可以放别的模块（如 `pixiv_grid_renderer.py` 就没有任何 Handler）。
   数量基线：**12 个 Handler** = 1 个指令组（`@filter.command_group("pixiv")`）+ 10 个子指令（`@pixiv.command`）+ 1 个消息监听（`on_session_input`，`@filter.event_message_type(ALL)`）。新增/删除指令时同步更新第 7 节基线。
2. **插件类名必须以 `Plugin` 结尾（或名为 `Main`）。**
   框架按 `name.lower().endswith("plugin") or name.lower() == "main"` 找插件类。现用名 `PixivNowPlugin`。
3. **不要改插件身份标识**：`metadata.yaml` 的 `name`、`author`、目录名。
   `author` / `name` 还有一层作用：插件市场的 `plugin_id = author + "/" + name`，且市场记录必须与包内 `metadata.yaml` **逐字相等**，所以 author 用「稳定的账号名」而不是带说明的长句子。
4. **日志器必须且只能从 `astrbot.api` 导入**：`from astrbot.api import logger`。
   插件里**不得** `import logging`，也不得使用内置日志模块的 `Logger` / `Handler` / `Formatter` / `getLogger`——这是上架审核的硬性规则。详见 5.9。
5. **不要执行 git 远端操作**（`pull` / `push`）。`status` / `diff` / `show` 可以随意用来比对查看；是否提交由人决定。
6. **行为敏感点**：错误文案、日志文案、消息条数与顺序、缓存默认值都算「可观察行为」，非必要不要改。
7. 新增三方依赖要写进 `requirements.txt`。`Pillow` 是渲染的硬依赖（缺失时本插件无法工作，没有降级分支）。

---

## 2. 目录结构

```text
astrbot_plugin_pixivnow/
├── main.py                  # 插件入口：插件类 + 全部 Handler + 网络/缓存/下载/发送/自动撤回/LLM 工具
├── pixiv_grid_renderer.py   # 渲染器：搜索网格 / 详情卡 / 排行榜海报（纯 Pillow，无网络、无 Handler）
├── metadata.yaml            # 插件元数据（name/author/display_name/version/repo/astrbot_version）
├── _conf_schema.json        # 插件配置 schema（4 组、24 项，全部在原生面板可见）
├── requirements.txt         # 依赖：httpx、Pillow
├── agent.md                 # ← 本文件（开发指南）
├── docs/previews/           # README 用的 10 张预览图（5 版式 × dark/light）
└── scripts/
    ├── dev_logger.py        # 开发脚本用的最小日志桩（不依赖内置日志模块，见 5.9）
    └── preview_themes.py    # 本地重新生成 docs/previews/ 预览图
```

**命名与归档约定**

- 保持「单文件 + 一个渲染器」的形态：`main.py` 是唯一入口且承载全部 Handler；新增功能优先作为 `PixivNowPlugin` 的方法，**不要**拆出一个 `utils/` 包（本插件规模不需要）。
- 渲染相关的改动一律落 `pixiv_grid_renderer.py`；它必须保持**可脱离 AstrBot 导入**（只依赖 PIL + `astrbot.api.logger`），因为 `scripts/preview_themes.py` 要独立跑它。
- 预览图由脚本产出，不要手工替换 `docs/previews/*.png`。

---

## 3. 运行期数据与临时文件

本插件**不使用插件数据目录**（没有 `StarTools.get_data_dir` / `data/plugin_data/...`），所有落盘都是**系统临时目录**里的图片，进程结束即可丢弃：

| 产物 | 位置 | 由谁写入 | 生命周期 |
| :--- | :--- | :--- | :--- |
| 下载的原图 / 缩略图 | `tempfile.mkstemp()` 的临时文件 | `_download` | 记录进 `_temp_files`；`_schedule_cleanup(path, ttl+30)` 延迟删除；`terminate()` 兜底全删 |
| 渲染卡片 PNG | `tempfile.mkstemp(suffix=".png")` | `_save_canvas` / `_make_grid` / `_make_rank_card` | 同上 |

**内存态**（重启即失效，无持久化）：

- 四级缓存：`_request_cache`（API JSON）、`_byte_cache`（图片字节）、`_path_cache`（临时文件路径）、`_render_cache`（渲染结果路径）。
- 去重/并发：`_inflight_requests`、`_inflight_bytes`（同键请求合并）、`_network_semaphore`（并发上限）。
- 状态：`_search_sessions`（搜索选图会话）、`_temp_files`、`_recall_locks`、`_http_client`。

清理点：`_reset_client()` → `_clear_runtime_caches()`（清空四级缓存）；`/pixiv seturl`、`/pixiv settoken` 会调用；`terminate()` 关客户端 + 删临时文件。
**新增任何内存缓存都要挂到这两处清理点**（`_clear_runtime_caches` 与 `terminate`）。

---

## 4. 核心数据流（一条指令的完整链路）

```
消息到达
 └─ AstrBot 匹配 Handler
     ├─ /pixiv <子指令> …       @pixiv.command（10 个子指令）
     └─ on_session_input        @filter.event_message_type(ALL)，所有消息都进，第一步就早退（无会话直接 return）
         ↓
 指令方法第一步一律 self._consume_event(event)
    → event.should_call_llm(False)（不触发 LLM）
    → event.stop_event()（终止后续 Handler）
    → _attach_recall(event)（OneBot 下包装 event.send 以便自动撤回，见 5.5）
         ↓
 业务逻辑（以 illust / search / rank 为例）
    1. self._base_url() + self._request(path, params)   ← 统一接口访问，带 API 缓存 + inflight 去重
    2. self._unwrap(data)                                 ← 解开 {error, message, body} 信封
    3. 取图：_enrich_illust → _download_best → _download/_fetch_bytes（候选链回退，见 5.3）
    4. 渲染：_make_grid / _make_rank_card / _make_detail_card（asyncio.to_thread 调 Pillow）
    5. 发送（见 5.4）：
       · 卡片：_send_card_separately（主动单独发，避免引用回复污染图片）
               失败则回退 event.chain_result([Image...])
       · 图集：_send_image_collection / _artwork_collection_result
               OneBot 多图 → Comp.Nodes 合并转发；其他平台 → 图片消息链
    6. 异常：PixivNowError → yield event.plain_result(str(e))
```

**发送方式选择**：卡片走**主动发送**（`context.send_message(umo, MessageChain().file_image(...))`），原因是引用回复会把 `@用户` 文本混进消息、污染图片 markdown；正文图集则走 `event.chain_result`。

---

## 5. 关键接口契约

### 5.1 配置读取（`_cfg` / `_set_cfg`）

- 读：`self._cfg(key, default)` 按**固定顺序**在四个分组里找：`("基础配置", "高级配置", "LLM 工具", "额外设置")`，命中即返回。
- 写：`self._set_cfg(key, value, group="基础配置")` 写进指定分组（注意：只写内存里的 `self.config`，**必须**再 `self.config.save_config()` 才落盘）。
- **配置项的唯一来源是 `_conf_schema.json`**（4 组 / 24 项）。AstrBot 加载插件时先按文件里的 schema 做 `check_config_integrity`，会**剔除 schema 之外的键**，之后才实例化插件类。所以：
  1. 新增配置项必须同时改 `_conf_schema.json`（写清 `type` / `default` / `hint`，需要的话加 `options`+`labels`）；
  2. 若新增了**分组**，必须同步加进 `_cfg` 的分组元组，否则该项永远读不到；
  3. 同步更新 `README.md` 的配置表。
- `caption_fields` 有特殊默认：`_cfg` 返回 `None`（从未配置）时按「全选」处理（`ALL_CAPTION_FIELDS`），见 `_caption_fields()`。
- 分组清单：`基础配置`(11) / `高级配置`(7) / `LLM 工具`(4) / `额外设置`(2)。

### 5.2 四级缓存与去重

| 缓存 | 键 | 值 | TTL 来源 | 上限 |
| :--- | :--- | :--- | :--- | :--- |
| `_request_cache` | `GET:<url>:<sorted params>` | API JSON | `api_cache_ttl`（默认 180） | 128 |
| `_byte_cache` | 解析后的图片 URL | `bytes` | `image_cache_ttl`（默认 300） | 64 |
| `_path_cache` | 解析后的图片 URL | `Path` | `max(image_ttl, 120)` | 64 |
| `_render_cache` | 渲染参数 sha1 | `Path` | `max(render_cache_ttl, 120)`（默认 180） | 32 |

- TTL 由 `_cache_ttl(kind)` 统一读取并 clamp 到 `0..3600`；`_cache_put` 在 `ttl<=0` 时**什么都不做**（=关闭该级缓存）。
- **文件型缓存有 120 秒下限**：`_path_cache`（`_download`）与 `_render_cache`（`_make_grid` / `_make_rank_card` / `_make_detail_card`）的条目 TTL 一律取 `max(配置值, 120)`，且临时文件的删除延迟是 `ttl + 30`——即**缓存条目总是比它指向的文件先过期**，调用方不会取到已被删掉的文件。纯内存的 `_request_cache` / `_byte_cache` 不设下限。
- 条目结构是 `(expires_monotonic, value)`；`_cache_get` 惰性过期；`_prune_cache(cache, max_items)` 先删过期、再按到期时间留新删旧。
- `_request` / `_fetch_bytes` 都做了 **inflight 去重**：同键并发只发一次请求。`use_cache=False`（如 `random`）时用唯一键，**不去重也不写缓存**——这是有意的，随机接口不能被缓存。
- `_request` 返回 `copy.deepcopy`（防止调用方改坏缓存对象）；`_byte_cache` 命中直接返回同一份 bytes。
- `_path_cache` 命中前还会 `exists()` 校验，文件被回收就重新下载。

### 5.3 图片 URL 候选链（`_url_candidates` → `_download_best`）

PixivNow 服务端构造原图 URL 只能猜扩展名，**原图可能 404**（如原图是 PNG 而 `_p0.jpg` 不存在）。因此下载一律走候选链依次尝试：

- 顺序：默认 `original → regular → small → thumb`；`prefer_thumb=True`（拼图缩略图）时反过来 `thumb → small → regular`。
- 末尾追加由 `item["url"]` 推导的 regular 与原值；整体去重。
- 辅助变换：`_resolve_url`（`/-/`、`/~/`、`i.pximg.net→/-/`、`s.pximg.net→/~/`、绝对地址直通）、`_to_regular`（`/c/<size>/`→`/`、`/custom-thumb/`→`/img-master/`、`_(square1200|custom1200)`→`_master1200`）。
- `_download_best` 逐个尝试，全部失败才抛 `PixivNowError`；单次失败会 `logger.warning` 后继续下一档。`_fetch_thumb_bytes` 是它的「失败返回 None」版本，供拼图与详情卡封面使用。

### 5.4 消息发送

| 场景 | 走法 | 位置 |
| :--- | :--- | :--- |
| 指令拦截 | `should_call_llm(False)` + `stop_event()` + 挂撤回 | `_consume_event` |
| 单独发信息卡 | 主动 `context.send_message(umo, MessageChain().file_image(...))`，失败回退 `chain_result` | `_send_card_separately` |
| 多图/图集 | OneBot（`aiocqhttp`）且 >1 张 → `Comp.Nodes` 合并转发；否则图片消息链 | `_send_image_collection` / `_artwork_collection_result` |
| 搜索选图 | OneBot 把「文字 + 原图」拆成两个转发节点，其他平台一条链 | `_search_selection_result` |

- `_is_onebot(event)` 判据是 `event.get_platform_name()` 含 `aiocqhttp`（异常时返回 False）。**只有 OneBot v11 支持合并转发**。
- 每次发送都经过 `_send_with_recall(...)`，未开自动撤回时它只做一次直接透传。

### 5.5 自动撤回（仅 OneBot v11）

`_send_with_recall` 通过**临时替换共享 OneBot 客户端的 `call_action`** 来捕获 `message_id`，两个约束缺一不可（否则会误撤回机器人发出的其他消息）：

1. **每个 bot 一把锁**（`_recall_locks[id(bot)]`）串行化补丁窗口——并发覆盖会导致包装函数残留，之后所有消息都被捕获；
2. **contextvars 标记**（`_recall_capture_token`）区分「本任务正在发送」与「其他任务并发发送」，窗口内其他任务的消息只透传不捕获。

捕获的是 `ONE_BOT_SEND_ACTIONS` 里的发送动作；`_schedule_recall` 延迟 `auto_recall_delay`（clamp `3..3600`）后 `delete_msg`。`_attach_recall` 包装 `event.send`，用 `_pixiv_recall_wrapped` 标志保证幂等。

### 5.6 LLM 工具

- `PixivKeywordRandomTool`（`FunctionTool[AstrAgentContext]`）持有 `plugin` 实例以便回调用插件逻辑；`__init__` 里按 `llm_tool_enabled` 注册（`context.add_llm_tools(...)`）→ **改动开关需重启 AstrBot**。
- 实现 `_pixiv_keyword_random_tool_impl`：**固定 safe 模式**，取 1 张，发图后按 `llm_tool_reply_mode` 决定回传给 LLM 的文本（`with_description` / `image_only`）。
- `_describe_image` 优先用 `llm_tool_vision_provider` 指定的提供商，留空则用当前会话聊天模型；模型不支持图像或调用失败时返回空串（**不中断**工具执行）。

### 5.7 渲染器（`pixiv_grid_renderer.py`）

- 构造：`PixivGridRenderer(theme="dark", columns=3, font_path=None)`；`theme` 不在 `THEMES` 时回落 `dark`。
- 版式方法：`render`（3×3 搜索网格）、`render_ranking`（前三名主卡 + 横向榜单）、`render_illust_detail` / `render_user_detail` / `render_novel_detail`。前两个由 `main` 直接调，后三个经 `render_method` 字典分发（见 `_make_detail_card`）。
- 全部为**同步 Pillow 代码**，`main.py` 用 `asyncio.to_thread` 调用，别在里面做网络请求。
- 字体：`_discover_fonts` 按平台候选表 + 常见字体目录查找中日韩字体，Linux 再退 `fc-match`；找不到只 `logger.warning` 并用 `ImageFont.load_default()`（文字可能显示方框）。
- 品牌色 `ACCENT`、主题色板 `THEMES` / `GridTheme`、字体候选表都在文件顶部，改视觉只动这些常量（新增主题要同时在 `_conf_schema.json` 的 `render_theme.options` 与 `THEMES` 里加）。

### 5.8 搜索会话

- `/pixiv s <关键词> [页码] [模式]` → `_show_search_page` 取前 `SEARCH_PAGE_SIZE`(9) 个作品、存 `_search_sessions[umo] = {keyword, mode, page, works, ts}`、发拼图。
- `on_session_input` 处理会话内输入：`1-9` 选图下载原图、`N/next` 翻页、`P/prev` 上一页、`P<数字>` 跳页、`E/exit/quit/0` 退出；`SEARCH_SESSION_TTL`(120s) 无操作自动过期。选图成功/失败都会 `pop` 会话（`finally` 里）。
- 会话按 `event.unified_msg_origin` 隔离（一个会话一条）。时间戳用 `asyncio.get_event_loop().time()`。

### 5.9 日志规范（审核要求）

**日志器必须且只能从 `astrbot.api` 导入**：`from astrbot.api import logger`。
插件里一律用它，**不得** `import logging`，也不得使用内置日志模块的 `Logger` / `Handler` / `Formatter` / `getLogger`。

- `astrbot.api.logger` 是 `_PluginContextLogger` 代理：**按调用方模块**解析出本插件的专用日志器（`astrbot.plugin.<插件名>`，与 `from astrbot.api import logger` 写在哪无关）。直接用 `logger.info(...)` / `logger.exception(...)` 即可，**不要自己取 logger 对象**。
- **不要往 logger 上挂处理器、也不要改它的级别**：既越过审核规则，又会波及全局日志。
- **开发脚本**（`scripts/`）要能在没有 AstrBot 的环境下独立运行：`scripts/preview_themes.py` 在导入渲染器前把 `scripts/dev_logger.py` 的 `StubLogger` 塞进 `sys.modules["astrbot.api"]`，同样不引入内置日志模块。新增独立脚本请沿用这个写法。
- 提交前自查（第 7 节验证清单里也有）：

```bash
# 1) 是否引入内置日志模块（注意要连 from logging import … 一起抓）
grep -rnE "(import|from)[[:space:]]+logging|logging\.[A-Za-z]|getLogger|basicConfig|(File|Stream)Handler" --include=*.py .
# 2) 是否改动了框架 logger 的状态
grep -rnE "logger\.(setLevel|addHandler|removeHandler|handlers|propagate|filters)" --include=*.py .
# 两条都应当没有任何输出。第 1 条刻意不带 Formatter——否则会命中 argparse 的
# RawDescriptionHelpFormatter（内置日志模块的 Formatter 一定伴随上面几个词出现，不漏检）。
```

---

## 6. 「我要做 X，改哪里」速查表

| 任务 | 改动位置 | 注意 |
| :--- | :--- | :--- |
| 新增指令 | `main.py`（`@pixiv.command`）+ 逻辑写成插件方法 | Handler 必须在 `main.py`（铁律 1）；管理指令加 `@filter.permission_type(ADMIN)`；记得 `self._consume_event(event)` |
| 新增配置项 | `_conf_schema.json` + `_cfg` 的分组元组（若新增分组）+ `README.md` 配置表 | 见 5.1；schema 里没有的键会被 AstrBot 剔除 |
| 改默认内容模式 / 张数 / 标签数 | `_conf_schema.json` 的 `default` | 读取处都有 `or 默认值` 兜底，两处一起看 |
| 调接口路径 / 参数 | `main.py` 里各指令发的 `self._request(...)` | 一律经 `_request` 才有缓存与去重；返回要 `_unwrap` 解信封 |
| 调图片清晰度 / 候选顺序 | `_url_candidates` / `_to_regular` / `_resolve_url` | 见 5.3；原图 404 是预期情况，别删回退链 |
| 调缓存时间 / 上限 | `_conf_schema.json` 的 `*_cache_ttl` + `_cache_ttl` / `_prune_cache` 的 `max_items` | 见 5.2；纯内存缓存 `ttl<=0` 即关闭，文件型缓存（path / render）有 120 秒下限 |
| 调下载超时 / 并发 | `download_timeout` / `max_concurrent_requests`（`_client` 与 `_network_semaphore`） | 并发上限 clamp 到 `1..16` |
| 调消息发送形态 | `_send_card_separately` / `_send_image_collection` / `_artwork_collection_result` / `_search_selection_result` | 见 5.4；OneBot 才走合并转发 |
| 调自动撤回 | `_recall_delay` / `_send_with_recall` / `_attach_recall` / `_schedule_recall` | 见 5.5；锁 + contextvars 两个约束不能拆 |
| 调 LLM 工具 | `PixivKeywordRandomTool` + `_pixiv_keyword_random_tool_impl` + `_describe_image` | 见 5.6；开关改动需重启 |
| 调卡片视觉 / 版式 / 配色 / 字体 | `pixiv_grid_renderer.py`（`ACCENT` / `THEMES` / 各 `render_*`） | 见 5.7；新增主题同步 schema options 与 `THEMES` |
| 调搜索会话行为 | `_show_search_page` + `on_session_input` + `SEARCH_SESSION_TTL` / `SEARCH_PAGE_SIZE` | 见 5.8；退出/过期要 `pop` 会话 |
| 调详情卡字段（收藏/喜欢/标签…） | `CAPTION_FIELD_NAMES` + `_caption` + `_needs_illust_detail` + `_enrich_illust` | 三处字段名要一致；schema 的 `caption_fields.options` 也要同步 |
| 重新生成预览图 | `scripts/preview_themes.py` | 产出 `docs/previews/*.png`（5 版式 × 2 主题） |
| 加日志 / 改日志方式 | 只用 `from astrbot.api import logger`；开发脚本用 `scripts/dev_logger.py::StubLogger` | **不得引入内置日志模块**，见 5.9 |
| 改 README / 元数据 | `README.md`、`metadata.yaml` | 版本号两处要一致（见第 0 节） |

---

## 7. 验证清单（改完必跑）

```bash
# <PY> = 能 `import astrbot` 的 Python 解释器，即 AstrBot 运行环境所使用的解释器
#        （下文命令在插件根目录执行）

# 1) 静态检查：未定义名 / 未使用导入（需 ruff）
<PY> -m ruff check --select F,E9 --no-cache --exclude __pycache__ .

# 2) 语法编译
<PY> -m compileall -q .

# 3) 日志器来源自查（审核要求：只能用 astrbot.api 的 logger，见 5.9；两条都应无输出）
grep -rnE "(import|from)[[:space:]]+logging|logging\.[A-Za-z]|getLogger|basicConfig|(File|Stream)Handler" --include=*.py .
grep -rnE "logger\.(setLevel|addHandler|removeHandler|handlers|propagate|filters)" --include=*.py .

# 4) 渲染回归 + 预览图重生成（5 版式 × 2 主题 = 10 张，末行应列出 10 个路径）
<PY> scripts/preview_themes.py     # 可脱离 AstrBot 直接跑（自带 astrbot.api 日志桩）
```

**导入 + Handler 冒烟脚本**（放临时目录，不提交；在 AstrBot 仓库根目录执行）：

```python
# 目的：确认模块可导入、12 个 Handler 已被框架接收
import importlib, pkgutil, sys
from pathlib import Path
import shutil

SRC = Path("<插件目录>")                                  # astrbot_plugin_pixivnow 目录
STAGE = Path("<临时目录>/astrbot_plugin_pixivnow")        # 当命名空间包导入
if STAGE.exists():
    shutil.rmtree(STAGE.parent)
STAGE.mkdir(parents=True)
for name in ("main.py", "pixiv_grid_renderer.py"):
    shutil.copy2(SRC / name, STAGE / name)
sys.path.insert(0, str(STAGE.parent))

import astrbot_plugin_pixivnow as pkg

bad = []
names = [pkg.__name__ + ".main"]
names += [m.name for m in pkgutil.walk_packages(pkg.__path__, pkg.__name__ + ".")]
for n in sorted(set(names)):
    try:
        importlib.import_module(n)
    except Exception as e:                            # noqa: BLE001
        bad.append((n, repr(e)))

from astrbot.core.star.star_handler import star_handlers_registry

handlers = star_handlers_registry.get_handlers_by_module_name(
    "astrbot_plugin_pixivnow.main"
)
print("imports_failed :", bad)
print("handlers       :", len(handlers))               # 应为 12
assert not bad and len(handlers) == 12
```

期望基线（2026-09-23 实测）：模块导入 **2/2**（`main.py` + `pixiv_grid_renderer.py`，无 `__init__.py`，靠命名空间包导入）、Handler **12 个**（指令组 1 + 子指令 10 + 消息监听 1）、配置项 **24 项 / 4 组**、预览图 **10 张**、日志自查两条均无输出。

**日志路由回归**（改渲染器日志或升级 AstrBot 后值得跑一次）：把 `astrbot_plugin_pixivnow.main` 写进 `astrbot.core.star.star_map`（`StarMetadata(name="astrbot_plugin_pixivnow", module_path="astrbot_plugin_pixivnow.main", ...)`），再调一次渲染器的告警路径（如 `_discover_fonts("/不存在")`），确认输出前缀是 `astrbot_plugin_pixivnow` 而不是全局 `astrbot`。

**真机冒烟**（需要真实的 PixivNow 服务）：配置 `pixivnow_url` 后依次跑 `/pixiv r`、`/pixiv rk 关键词`、`/pixiv s 关键词`（含翻页 / 选图 / 退出）、`/pixiv top`、`/pixiv i <id>`、`/pixiv u <id>`、`/pixiv n <id>`，并单独验证 OneBot 平台的多图合并转发与自动撤回。

---

## 8. 已知约束与坑

- **Handler 归属**：见铁律 1，这是最容易踩的坑（`__module__` 不是插件主模块 → 框架直接丢弃该 Handler，静默不生效）。所有带装饰器的函数留在 `main.py`。
- **`on_session_input` 对所有消息生效**：`@filter.event_message_type(ALL)` 意味着每条消息都会进它，所以**第一件事**就是查 `_search_sessions` 并早退。别在它前面加任何重活。
- **指令组受 `wake_prefix` 制约**：`@pixiv.command` 的匹配依赖唤醒前缀与指令组树，改 `command_group` 的名字 / 别名会同时影响所有子指令与帮助。
- **`_cfg` 是「按分组顺序找」而非「按全库找」**：同一个键若在多个分组里出现，先命中先返回；新分组不加进 `_cfg` 的分组元组就永远读不到（见 5.1）。
- **schema 之外的配置键会被剔除**：不要在 `__init__` 里往 `config.schema` 注入键——下次重载时 `check_config_integrity` 会把用户的值一起删掉，静默丢失。
- **`_set_cfg` 之后必须 `save_config()`**：只改内存不落盘，重启即丢（`seturl` / `settoken` 都成对调用了）。
- **`_temp_files` 只增不减**：临时文件会被 `_schedule_cleanup` 延迟删除，但路径对象一直留在列表里直到 `terminate()`。长时间运行时该列表会缓慢增长——如果做优化，记得同时维护「文件已删」与「列表条目」两件事。
- **文件型缓存的 TTL 有 120 秒下限，`0` 关不干净**：`_path_cache` / `_render_cache` 的条目 TTL 一律 `max(配置值, 120)`（`_request_cache` / `_byte_cache` 是纯内存缓存，不设下限）。所以把 `image_cache_ttl` / `render_cache_ttl` 设为 `0` 后，这两级仍会缓存 120 秒——这是**现状语义**，不是 bug：下限存在的意义是让「缓存条目比它指向的临时文件先过期」（文件在 `ttl + 30` 才删）。真要彻底关闭文件型缓存，得同时改缓存写入与文件清理两处。
- **随机接口不能被缓存**：`random` 用 `use_cache=False`（唯一键、不写缓存）——去掉它会让随机结果固定不变。
- **原图 404 是预期情况**：PixivNow 只能猜扩展名，所以 `_url_candidates` 的逐档回退不能删；`_download_best` 全失败才抛错。
- **渲染是同步阻塞代码**：一律经 `asyncio.to_thread` 调用（`_make_grid` / `_make_rank_card` / `_make_detail_card` 都这样做了），不要在事件循环里直接调 `renderer.render`。
- **Pillow 是硬依赖**：`import astrbot.api` 之外，本插件缺失 Pillow 直接不可用（没有像 rika 那样「Pillow 缺失 → 关闭渲染」的降级分支）。要加降级得连渲染调用点一起改。
- **合并转发的内存放大**：OneBot 会把节点内图片整体 base64，峰值约为原图总量数倍；目前是「多图一次性成节点」的实现，若要处理超大图集需自行加拆批。
- **自动撤回的双重约束不能拆**：见 5.5。去掉 per-bot 锁会残留包装函数（之后所有消息被误撤回）；去掉 contextvars 标记会误捕获其他任务的发送。
- **自动撤回仅 OneBot v11**：`_is_onebot` 判 `aiocqhttp`；其他平台不会有任何撤回行为（也不该有）。
- **`_send_card_separately` 可能「未找到会话」**：主动发送依赖 `umo` 能映射到平台会话，失败会 `logger.warning` 并返回 False → 调用方回退 `chain_result`。这条回退分支要保留。
- **搜索会话按 UMO 覆盖**：同一会话再次 `/pixiv s` 会直接覆盖上一个会话，属预期。
- **不得使用内置日志模块**：日志器只能来自 `astrbot.api`（见 5.9）。独立脚本用 `scripts/dev_logger.py` 的桩。
- **版本号两处一致**：`metadata.yaml` 与 `README.md`（本插件没有 `@register`，别去 `main.py` 找版本号）。
- **预览图不要手工改**：由 `scripts/preview_themes.py` 产出，手工替换会与脚本输出脱节。

---

## 9. 维护要求（防止本文件过时）

### 9.1 何时必须更新本文件

出现以下任一种「重要改动」时，**在同一次改动里**更新 `agent.md`：

1. 目录 / 文件新增、删除、改名、移动（→ 更新第 2 节结构树 + 第 6 节速查表）
2. 新增 / 删除指令、消息监听器，或权限变化（→ 第 1 节、第 4 节，并更新第 7 节 Handler 基线）
3. `PixivNowPlugin` 公开行为变化：主流程、发送顺序、发送形态（→ 第 4 节、5.4）
4. 配置项增删改名、默认值变化、分组变化（→ 第 0 节、5.1、第 6 节）
5. 缓存层级 / TTL 语义 / 去重机制变化（→ 5.2）
6. 图片 URL 候选链、清晰度回退规则变化（→ 5.3）
7. 自动撤回机制变化（→ 5.5、第 8 节）
8. LLM 工具的参数、返回方式、视觉转述行为变化（→ 5.6）
9. 渲染器版式 / 主题 / 字体探测变化，或新增 `render_*` 方法（→ 5.7、第 6 节）
10. 搜索会话的输入约定、TTL、页面尺寸变化（→ 5.8）
11. 日志的实现方式变化：日志器来源、是否拦截/改动框架日志、独立脚本的桩做法（→ 5.9、第 7 节、第 8 节）
12. 依赖 / 运行环境要求变化（→ 第 0 节、第 8 节）
13. 验证基线数字变化（→ 第 7 节「期望基线」）

### 9.2 更新方式

- 直接改对应小节，**不要只在文末追加**；顺手检查第 6 节速查表是否仍指向存在的文件。
- 自检：文中出现的每个路径都存在；命令可复制执行；第 7 节基线数字与实测一致。
- **只写与仓库内容有关的通用信息**：不要记录任何本机绝对路径、用户名、账号、令牌等隐私或环境专属内容。
