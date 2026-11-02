"""平台热榜：60s API（github.com/vikiboss/60s）。

为什么是它：小红书没有公开的官方热榜接口，自己逆向协议要带登录态、
天天变。60s 把各平台热榜聚合成统一 REST 接口——/v2/rednote 就是
小红书热点，微博/知乎/抖音/B站/百度顺带全都有，一个依赖解决一串平台。

一个设计约束（和 search.py 同款哲学）：热榜是灵感素材，不是关键路径，
挂了就返回空列表让调用方降级，绝不能阻塞写作主流程。

公共实例（60s.viki.moe）有每日额度；用得狠就自部署：
    docker run -d --restart always -p 4399:4399 vikiboss/60s:latest
base_url 用环境变量 HOT_API_BASE 切换。
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field

import httpx

DEFAULT_BASE = "https://60s.viki.moe"
TIMEOUT = 15.0

# 公共实例有每日额度，偶发失败是常态（用户实测「点了报错，过会儿又好了」）。
# 两层兜底：失败重试一次，仍失败就拿旧缓存顶上并标记 stale——
# 热榜是灵感素材，宁可用五分钟前的榜单，也别让用户对着报错框干瞪眼。
_CACHE: dict[str, tuple[float, HotList]] = {}
_CACHE_TTL = 300.0     # 5 分钟内视为新鲜，直接返回，不打上游
_MAX_RETRY = 1         # 首次失败后再试一次，躲开瞬时抖动
_RETRY_DELAY = 0.6

# 暴露给规划层和前端的平台清单。key = 我们内部的 source 名
SOURCES = {
    "rednote": "小红书热点",
    "weibo": "微博热搜",
    "zhihu": "知乎热榜",
    "douyin": "抖音热点",
    "bilibili": "B站热门",
    "baidu": "百度热搜",
    "tieba": "百度贴吧",
}

# source → 60s 的实际端点路径（60s 里百度是多级路径，不是 /{source} 平铺）
ENDPOINTS = {
    "rednote": "rednote",
    "weibo": "weibo",
    "zhihu": "zhihu",
    "douyin": "douyin",
    "bilibili": "bilibili",
    "baidu": "baidu/hot",
    "tieba": "baidu/tieba",
}


class HotListError(RuntimeError):
    pass


@dataclass
class HotList:
    source: str
    source_label: str
    items: list[dict] = field(default_factory=list)  # {rank, title, score, link}
    # 这份数据是缓存兜底的（上游此刻拿不到），前端可提示「可能不是最新」
    stale: bool = False
    error: str = ""


def _base() -> str:
    return os.environ.get("HOT_API_BASE", DEFAULT_BASE).rstrip("/")


def available() -> bool:
    return bool(_base())


async def _fetch_once(source: str, limit: int) -> HotList:
    """真打一次上游。任何失败都抛 HotListError。"""
    url = f"{_base()}/v2/{ENDPOINTS[source]}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            payload = resp.json()
    except httpx.TimeoutException as exc:
        raise HotListError(f"{SOURCES[source]} 接口超时") from exc
    except httpx.HTTPStatusError as exc:
        # 上游把端点下线了（60s 的 /v2/bilibili 已 404「接口被吃掉了」）。
        # 注意 HTTPStatusError 和 RequestError 是兄弟类，不是父子——
        # 只捕 RequestError 会让 4xx/5xx 冒泡成 500，前端只能看到白屏。
        code = exc.response.status_code
        raise HotListError(f"{SOURCES[source]} 接口不可用（上游 HTTP {code}）") from exc
    except (httpx.RequestError, ValueError) as exc:
        raise HotListError(f"{SOURCES[source]} 接口异常：{exc}") from exc

    if payload.get("code") != 200:
        raise HotListError(f"{SOURCES[source]} 返回异常：{payload.get('message', 'unknown')}")

    items: list[dict] = []
    for i, raw in enumerate(payload.get("data") or []):
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        if not title:
            continue
        # score 优先用 score_desc（贴吧/百度给的是「185.54w」这种友好格式），
        # 没有再用原始 score / hot_value
        score = str(raw.get("score_desc") or raw.get("score") or raw.get("hot_value") or "")
        items.append(
            {
                "rank": raw.get("rank") or i + 1,
                "title": title[:80],
                "score": score,
                "link": raw.get("link") or raw.get("url") or "",
            }
        )
        if len(items) >= limit:
            break
    return HotList(source=source, source_label=SOURCES[source], items=items)


def _trim(hl: HotList, limit: int) -> HotList:
    """缓存里存的是当时拿到的条数，取用时按当前 limit 截一下。"""
    return HotList(
        source=hl.source, source_label=hl.source_label,
        items=hl.items[:limit], stale=hl.stale, error=hl.error,
    )


async def fetch_hot(source: str, limit: int = 10) -> HotList:
    """拉某个平台的热榜，三级兜底：新鲜缓存 → 上游（失败重试一次）→ 旧缓存。

    热榜是灵感素材不是关键路径。用户实测过「点了报错、过会儿又好了」——
    60s 公共实例有每日额度，偶发失败是常态，所以不能一失败就甩报错。
    宁可给一份五分钟前的榜单并标 stale，也别让用户对着错误提示干瞪眼。
    """
    if source not in SOURCES:
        raise HotListError(f"不支持的平台：{source}（可选：{'/'.join(SOURCES)}）")

    now = time.monotonic()
    cached = _CACHE.get(source)
    if cached and now - cached[0] < _CACHE_TTL:
        return _trim(cached[1], limit)

    last: HotListError | None = None
    for attempt in range(_MAX_RETRY + 1):
        try:
            hl = await _fetch_once(source, limit)
            _CACHE[source] = (time.monotonic(), hl)
            return hl
        except HotListError as exc:
            last = exc
            if attempt < _MAX_RETRY:
                await asyncio.sleep(_RETRY_DELAY)  # 躲开瞬时抖动

    if cached:
        hl = _trim(cached[1], limit)
        hl.stale = True
        hl.error = str(last)
        return hl
    raise last if last else HotListError(f"{SOURCES[source]} 拉取失败")


def to_material(hl: HotList) -> str:
    """拼成写作素材。link 是平台搜索/话题页，给 Writer 做话题定位和溯源用。"""
    lines = [f"【{hl.source_label}（实时，来源 60s API）】"]
    for it in hl.items:
        seg = f"{it['rank']}. {it['title']}"
        if it["score"]:
            seg += f"（热度 {it['score']}）"
        if it["link"]:
            seg += f"  → {it['link']}"
        lines.append(seg)
    return "\n".join(lines)
