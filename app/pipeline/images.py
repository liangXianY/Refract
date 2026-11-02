"""文章配图：把 compose 规划好的图位变成真图片。

两条来源，用 IMAGE_SOURCE 选：
    tavily   用 Tavily 搜真实图片——复用已有 TAVILY_API_KEY，不额外花钱，
             热点/新闻类文案的现场图比 AI 生成的更有说服力。代价是版权：
             新闻图有版权，对外发布前需自行确认授权。
    ai       用图像 API 生成——无版权风险、贴合文案，但按张计费。
    auto     默认。先搜真实图，搜不到再 AI 生成（两边都配了 key 时的最优解）。
    其它值   等同关闭，正文保留「🖼 配图位」占位。

三个设计取舍，都是踩出来的：

1. **拿到就立刻下载存本地**。图床返回的是临时 URL（硅基流动只 1 小时），
   外链直接写进产物的话，用户第二天打开就是一片裂图。

2. **并行取图**。三张串行要 40-60 秒，并行压到 15-20 秒——文章本来就要跑三分钟，
   配图不该再拖一截。

3. **任何一张失败就保留占位**。配图是加分项不是关键路径，
   不能因为一张图挂了把整篇文章搞失败。

图像生成部分按硅基流动的格式写（OpenAI 兼容），换别家改 IMAGE_BASE_URL + IMAGE_MODEL：
    POST {base}/v1/images/generations
    {"model": ..., "prompt": ..., "image_size": "1024x576", "batch_size": 1}
    → {"images": [{"url": "..."}]}
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import httpx

DEFAULT_BASE = "https://api.siliconflow.cn"
# Kolors 对中文提示词理解好，价格低；想更快可以换 black-forest-labs/FLUX.1-schnell
DEFAULT_MODEL = "Kwai-Kolors/Kolors"
TIMEOUT = 90.0
MAX_IMAGES = 3          # 每篇最多几张，控成本
IMAGE_SIZE = "1024x576"  # 16:9，适配公众号头图与正文横图

_PLACEHOLDER = re.compile(r"!\[([^\]]*)\]\(prompt:\s*([^)]*)\)")


def enabled() -> bool:
    """有没有任何一条能出图的路子。

    真实图走 Tavily（复用既有 TAVILY_API_KEY，不额外花钱），
    AI 生成走图像 API（IMAGE_API_KEY）。两者都没配就整段跳过。
    """
    src = (os.environ.get("IMAGE_SOURCE", "auto") or "auto").strip().lower()
    if src == "tavily":
        return bool(os.environ.get("TAVILY_API_KEY", "").strip())
    if src == "ai":
        return bool(os.environ.get("IMAGE_API_KEY", "").strip())
    return bool(os.environ.get("TAVILY_API_KEY", "").strip()
                or os.environ.get("IMAGE_API_KEY", "").strip())


def _source() -> str:
    """auto（默认，真实图优先）/ tavily（只用真实图）/ ai（只用 AI 生成）/ off。"""
    return (os.environ.get("IMAGE_SOURCE", "auto") or "auto").strip().lower()



def _cfg() -> tuple[str, str, str]:
    return (
        os.environ.get("IMAGE_API_KEY", "").strip(),
        os.environ.get("IMAGE_BASE_URL", DEFAULT_BASE).rstrip("/"),
        os.environ.get("IMAGE_MODEL", DEFAULT_MODEL).strip(),
    )


async def generate_one(prompt: str) -> bytes | None:
    """生成一张图并下载成 bytes。失败返回 None，不抛异常（调用方按张降级）。"""
    key, base, model = _cfg()
    if not key:
        return None
    payload = {
        "model": model,
        "prompt": prompt[:800],
        "image_size": IMAGE_SIZE,
        "batch_size": 1,
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(
                f"{base}/v1/images/generations",
                json=payload,
                headers={"Authorization": f"Bearer {key}"},
            )
            if resp.status_code >= 400:
                return None
            data = resp.json()
            imgs = data.get("images") or []
            url = (imgs[0] or {}).get("url") if imgs else ""
            if not url:
                return None
            # 临时 URL，立刻拉下来存本地，别把外链写进产物
            got = await client.get(url)
            if got.status_code >= 400 or not got.content:
                return None
            return got.content
    except (httpx.HTTPError, ValueError, AttributeError, TypeError, IndexError):
        return None


_MIN_BYTES = 8 * 1024   # 小于这个的多半是图标、占位图或加载失败的空响应


async def _download_image(url: str) -> bytes | None:
    """下载图片，并确认它真的是一张能用的图（不是 404 页、图表、超小图标）。"""
    if not url or not url.startswith("http"):
        return None
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code >= 400:
                return None
            ctype = (r.headers.get("content-type") or "").lower()
            if ctype and not ctype.startswith("image/"):
                return None          # 有些站会把图片链接重定向到 HTML 页
            if len(r.content) < _MIN_BYTES:
                return None
            return r.content
    except (httpx.HTTPError, ValueError):
        return None


async def search_real_images(query: str, limit: int = 4) -> list[tuple[bytes, str]]:
    """用 Tavily 搜真实图片，返回 [(图片二进制, 图片描述)]。

    对热点/新闻类文案，真实现场图比 AI 生成的更有说服力，还不花图像 API 的钱。
    代价是版权：新闻图有版权，对外正式发布前要自行确认授权，这里只做技术打通。

    返回描述是为了「相关性判断」：Tavily 给的 description 说明了图里是什么，
    没有它就只能盲选第一张，经常搜出无关的 logo、表情包、广告图。
    """
    from .search import SearchError, search

    q = (query or "").strip()
    if not q:
        return []
    try:
        res = await search(q, max_results=3, with_images=True)
    except SearchError:
        return []
    out: list[tuple[bytes, str]] = []
    for img in res.images[:limit]:
        data = await _download_image(str(img.get("url") or ""))
        if data:
            out.append((data, str(img.get("description") or "").strip()))
    return out


async def search_real_image(query: str, limit: int = 3) -> bytes | None:
    """只要一张：兼容老调用方。"""
    pool = await search_real_images(query, limit=limit)
    return pool[0][0] if pool else None


_IMAGE_PICK_SYSTEM = """你在给一篇中文文章选配图。给你图位说明和几张候选图（带图片描述），
挑出唯一一张和说明真的相关、适合放进文章的；都不合适就返回 -1。
只输出 JSON：{"pick": 数字}"""


async def _choose(llm, desc: str, topic: str, pool: list[tuple[bytes, str]]) -> bytes | None:
    """从候选图里选一张。单张直接返回；多张交模型判断相关性。

    为什么交模型：图和文字的相关性没法用词面相似度算（同义词、示意图、
    图标和实物都会误判）。模型看一眼描述就能判断，成本是一次几十 token 的调用，
    比往文章里塞一张不相关的图便宜得多。
    """
    if not pool:
        return None
    if len(pool) == 1 or llm is None or getattr(llm.cfg, "mock", False):
        return pool[0][0]
    menu = "\n".join(
        f"{i}. {d or '（无描述）'}" for i, (_data, d) in enumerate(pool)
    )
    try:
        data = await llm.structured(
            system=_IMAGE_PICK_SYSTEM,
            user=f"文章主题：{topic or '（未提供）'}\n图位说明：{desc}\n\n候选图：\n{menu}",
            max_tokens=800,
            temperature=0.1,
            route="text",
            validate=lambda d: None if isinstance(d.get("pick"), int) else (_ for _ in ()).throw(ValueError("pick")),
        )
        idx = int(data.get("pick", 0))
    except Exception:
        return pool[0][0]  # 判断失败就退回第一张，不卡住配图
    if idx < 0 or idx >= len(pool):
        return None  # 模型明确说都不合适 → 这篇就不放这张图
    return pool[idx][0]


async def _pick(desc: str, prompt: str, *, llm=None, topic: str = "") -> bytes | None:
    """按 IMAGE_SOURCE 决定这张图从哪来。

    auto（默认）：先用 Tavily 搜真实图，搜不到再 AI 生成
    tavily      ：只用真实图（完全不花钱）
    ai          ：只用 AI 生成（无版权风险）
    """
    src = _source()
    if src in ("auto", "tavily"):
        pool = await search_real_images(desc)
        data = await _choose(llm, desc, topic, pool)
        if data:
            return data
        if src == "tavily":
            return None
    if src in ("auto", "ai") and os.environ.get("IMAGE_API_KEY", "").strip():
        return await generate_one(prompt)
    return None


async def fill_placeholders(
    body: str, out_dir: Path, url_prefix: str = "/img", *,
    top_up: bool = True, topic: str = "", max_total: int = 3, llm=None,
) -> tuple[str, int]:
    """把正文里的 `![说明](prompt: xxx)` 替换成真图；图不够时主动补。

    返回 (新正文, 成功张数)。未配置任何图源或全部失败时原样返回。

    top_up=True 时，如果最终图数仍少于 max_total，会再搜一次图插到中后段小节后——
    文章板块和对话板块都踩过同一个坑：模型不是每次都按引导插占位，
    结果正文一个图都没有。不能指望模型记得，得由代码兜。
    """
    if not enabled() or not body:
        return body, 0

    total_slots = len(_PLACEHOLDER.findall(body))
    hits = list(_PLACEHOLDER.finditer(body))[:MAX_IMAGES]

    out_dir.mkdir(parents=True, exist_ok=True)
    # group(1) 是图的说明（短，像新闻标题）→ 拿去搜真实图；
    # group(2) 是生图 prompt（长，描述画面）→ 拿去 AI 生成。
    results = await asyncio.gather(
        *(_pick(m.group(1).strip(), m.group(2).strip(), llm=llm, topic=topic) for m in hits)
    ) if hits else []

    it = iter(results)
    seq = {"n": 0}

    def repl(m: re.Match[str]) -> str:
        data = next(it, None)
        if not data:
            return m.group(0)          # 这张失败，保留原占位
        seq["n"] += 1
        name = f"{seq['n']}.png"
        (out_dir / name).write_bytes(data)
        return f"![{m.group(1)}]({url_prefix}/{out_dir.name}/{name})"

    new_body = _PLACEHOLDER.sub(repl, body, count=MAX_IMAGES) if total_slots else body

    # 补图：正文图不足 max_total 时，再取几张塞进中后段小节
    if top_up and seq["n"] < max_total and "## " in new_body:
        need = max_total - new_body.count("![")
        if need > 0:
            q = (topic or _first_alt(body) or "").strip()
            extra = await _search_pool(q, need)
            if extra:
                new_body = _insert_after_sections(
                    new_body, extra, out_dir, url_prefix, seq, max_total, topic=q
                )

    return new_body, seq["n"]


def _first_alt(body: str) -> str:
    """正文里第一个图注，用来当补图的搜索词（没有就用空串）。"""
    m = re.search(r"!\[([^\]]+)\]", body)
    return (m.group(1) if m else "").strip()[:60]


async def _search_pool(query: str, need: int) -> list[bytes]:
    """搜一批真实图备用（补图用）。搜不到就返回空，不抛异常。"""
    q = (query or "").strip()
    if not q or _source() not in ("auto", "tavily"):
        return []
    from .search import SearchError, search

    try:
        res = await search(q, max_results=6, with_images=True)
    except SearchError:
        return []
    out: list[bytes] = []
    for img in res.images:
        if len(out) >= need:
            break
        data = await _download_image(str(img.get("url") or ""))
        if data:
            out.append(data)
    return out


def _insert_after_sections(
    body: str, blobs: list[bytes], out_dir: Path, url_prefix: str,
    seq: dict, max_total: int, topic: str = "",
) -> str:
    """把图片插到中后段小节标题之后，最多凑到 max_total 张。"""
    alt = (topic or "配图")[:40]
    for i, data in enumerate(blobs):
        if body.count("![") >= max_total:
            break
        heads = list(re.finditer(r"^## .+$", body, re.M))
        if not heads:
            break
        # 从第 2 个小节往后逐个插，避免几张图都挤在同一处
        at = heads[min(len(heads) - 1, 1 + i)].end()
        seq["n"] += 1
        name = f"{seq['n']}.png"
        (out_dir / name).write_bytes(data)
        body = body[:at] + f"\n\n![{alt}]({url_prefix}/{out_dir.name}/{name})\n" + body[at:]
    return body
