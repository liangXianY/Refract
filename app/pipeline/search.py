"""联网搜索：Tavily。

为什么选它：专为 LLM 设计，返回的是清洗后的正文（不用自己再去抓网页），
还有 images 字段能顺带搜图——「搜资料」和「搜配图」一个 API 全覆盖。
免费 1000 次/月，个人项目够用。

一个设计约束：搜索是外部依赖，挂了不能拖死主流程——
调用方（write_article / web_search 工具）拿不到结果就降级为无搜索写作，
和 reflect 的降级是同一个哲学：非关键路径不阻塞关键路径。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import httpx


class SearchError(RuntimeError):
    pass


@dataclass
class SearchResult:
    query: str
    items: list[dict] = field(default_factory=list)  # {title, url, content}
    images: list[dict] = field(default_factory=list)  # {url, description}


def _key() -> str:
    return os.environ.get("TAVILY_API_KEY", "")


def available() -> bool:
    return bool(_key())


async def search(
    query: str,
    max_results: int = 5,
    with_images: bool = False,
    timeout: float = 15.0,
) -> SearchResult:
    """搜一次。key 没配直接抛 SearchError，调用方决定怎么降级。"""
    key = _key()
    if not key:
        raise SearchError("TAVILY_API_KEY 未配置")
    query = (query or "").strip()
    if not query:
        raise SearchError("搜索词为空")

    payload = {
        "query": query[:400],
        "max_results": max(1, min(max_results, 10)),
        "search_depth": "basic",
        "include_answer": False,
        "include_images": with_images,
        "include_image_descriptions": with_images,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                "https://api.tavily.com/search",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload,
            )
    except httpx.RequestError as exc:
        raise SearchError(f"搜索网络错误：{type(exc).__name__}")
    if resp.status_code == 401:
        raise SearchError("Tavily key 无效（401）")
    if resp.status_code == 432:
        raise SearchError("Tavily 免费额度用完了")
    if resp.status_code >= 400:
        raise SearchError(f"Tavily 返回 {resp.status_code}")

    data = resp.json()
    items = [
        {"title": str(r.get("title") or "")[:120], "url": r.get("url") or "", "content": (r.get("content") or "")[:1500]}
        for r in (data.get("results") or [])[:max_results]
    ]
    images = [
        {"url": i.get("url") or "", "description": str(i.get("description") or "")[:120]}
        for i in (data.get("images") or [])
        if i.get("url")
    ]
    return SearchResult(query=query, items=items, images=images)


def to_material(res: SearchResult, limit: int = 3000) -> str:
    """搜索结果 → 喂给写作的素材。URL 必须保留：事实点要能溯源。"""
    lines = [f"搜索词：{res.query}", ""]
    for i, r in enumerate(res.items, 1):
        lines += [f"[{i}] {r['title']}", f"    来源：{r['url']}", f"    {r['content'][:600]}", ""]
    out = "\n".join(lines)
    return out[:limit]
