"""Researcher：抓取取证。

职责单一——把 URL 变成可用的原材料（正文 + 元信息）。
不写稿、不判断内容好坏，那是 Writer 的事。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..pipeline.fetch import FetchError, FetchedPage, fetch_url, to_prompt_material


@dataclass
class ResearchResult:
    ok: bool
    material: str = ""
    title: str = ""
    site: str = ""
    word_count: int = 0
    url: str = ""
    error: str = ""


async def research(url: str, limit: int = 6000) -> ResearchResult:
    """抓一个链接，返回可喂给模型的原材料。失败时 ok=False 且 error 说清原因。"""
    try:
        page: FetchedPage = await fetch_url(url)
    except FetchError as exc:
        return ResearchResult(ok=False, error=str(exc), url=url)

    return ResearchResult(
        ok=True,
        material=to_prompt_material(page, limit=limit),
        title=page.title,
        site=page.site,
        word_count=page.word_count,
        url=page.url,
    )
