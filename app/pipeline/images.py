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


async def search_real_image(query: str, limit: int = 3) -> bytes | None:
    """用 Tavily 搜真实图片，返回第一张能下载的。

    对热点/新闻类文案，真实现场图比 AI 生成的更有说服力，还不花图像 API 的钱。
    代价是版权：新闻图有版权，对外正式发布前要自行确认授权，这里只做技术打通。
    """
    from .search import SearchError, search

    q = (query or "").strip()
    if not q:
        return None
    try:
        res = await search(q, max_results=3, with_images=True)
    except SearchError:
        return None
    for img in res.images[:limit]:
        data = await _download_image(str(img.get("url") or ""))
        if data:
            return data
    return None


async def _pick(desc: str, prompt: str) -> bytes | None:
    """按 IMAGE_SOURCE 决定这张图从哪来。

    auto（默认）：先用 Tavily 搜真实图，搜不到再 AI 生成
    tavily      ：只用真实图（完全不花钱）
    ai          ：只用 AI 生成（无版权风险）
    """
    src = _source()
    if src in ("auto", "tavily"):
        data = await search_real_image(desc)
        if data:
            return data
        if src == "tavily":
            return None
    if src in ("auto", "ai") and os.environ.get("IMAGE_API_KEY", "").strip():
        return await generate_one(prompt)
    return None


async def fill_placeholders(
    body: str, out_dir: Path, url_prefix: str = "/img"
) -> tuple[str, int]:
    """把正文里的 `![说明](prompt: xxx)` 替换成真图。

    返回 (新正文, 成功张数)。未配置 key 或全部失败时原样返回，正文不受影响。
    """
    if not enabled() or not body:
        return body, 0

    total = len(_PLACEHOLDER.findall(body))
    if not total:
        return body, 0

    hits = list(_PLACEHOLDER.finditer(body))[:MAX_IMAGES]
    if not hits:
        return body, 0

    out_dir.mkdir(parents=True, exist_ok=True)
    # group(1) 是图的说明（短，像新闻标题）→ 拿去搜真实图；
    # group(2) 是生图 prompt（长，描述画面）→ 拿去 AI 生成。
    results = await asyncio.gather(
        *(_pick(m.group(1).strip(), m.group(2).strip()) for m in hits)
    )

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

    new_body = _PLACEHOLDER.sub(repl, body, count=MAX_IMAGES)
    return new_body, seq["n"]
