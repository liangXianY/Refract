"""链接抓取：URL → 结构化正文（Researcher 的核心能力）。

设计取舍：
1. 只用 httpx + trafilatura，不启无头浏览器。
   理由：服务器 1.9G 内存，Chromium 峰值 2-4G，一跑就 OOM。
   代价：JS 渲染的页面抓不到正文 —— 这时明确报错，绝不编造。
2. 抓不到就说抓不到。「失败」和「内容为空」是两种不同的错误，
   都要让前端能区分，否则模型会拿空内容硬编。
3. SSRF 防护：接口在公网，必须挡住内网地址（127./10./172.16./192.168./169.254.）
   和 file:// gopher:// 等非 http(s) 协议。
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx
import trafilatura


class FetchError(RuntimeError):
    """抓取失败。message 直接给用户看，要说清原因。"""


@dataclass
class FetchedPage:
    url: str
    title: str = ""
    text: str = ""
    author: str = ""
    published: str = ""
    site: str = ""
    word_count: int = 0
    truncated: bool = False


# 只放行 http/https，且拒绝内网、回环、链路本地
_BLOCKED_HOSTS = {"localhost", "metadata.google.internal"}


def _check_url(url: str) -> str:
    """校验 URL，返回规范化后的地址。不合法直接抛 FetchError。"""
    url = (url or "").strip()
    if not url:
        raise FetchError("链接为空")
    if len(url) > 2000:
        raise FetchError("链接过长（>2000 字符）")

    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise FetchError(f"只支持 http/https 链接，收到的是 {p.scheme or '无协议'}")

    host = (p.hostname or "").lower()
    if not host:
        raise FetchError("链接里没有有效域名")
    if host in _BLOCKED_HOSTS:
        raise FetchError("不允许访问本机地址")

    # IP 直连：挡掉私有网段和回环
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise FetchError("不允许访问内网地址")
    except ValueError:
        # 不是 IP，是域名，放行
        pass

    return url


_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

MAX_BYTES = 3 * 1024 * 1024  # 3MB 上限，防止抓到大文件撑爆内存
TIMEOUT = 15.0

# ---------------------------------------------------------- GitHub 走 API
# 为什么 GitHub 不走网页抓取：
#   1. github.com 主站在国内服务器直连经常超时，而 api.github.com 是通的
#   2. 就算抓到 HTML，trafilatura 抽出来的也是网页壳，拿不到 star 数、语言、topics
#   3. API 返回的是结构化数据，README 直出原文——这才是写介绍稿要的东西

_REPO_RE = re.compile(r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", re.I)


def _github_repo_url(url: str) -> tuple[str, str] | None:
    """是 github repo 链接就返回 (owner, repo)，不是返回 None。"""
    m = _REPO_RE.search(url)
    if not m or urlparse(url).hostname not in ("github.com", "www.github.com"):
        return None
    owner, repo = m.group(1), m.group(2)
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    return owner, repo


async def _github_fetch(url: str, owner: str, repo: str) -> FetchedPage:
    """GitHub repo → 结构化档案。未认证限流 60 次/小时，够用；配 GITHUB_TOKEN 可到 5000。"""
    api = "https://api.github.com"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "refract-content-agent",
        **({"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}"} if os.environ.get("GITHUB_TOKEN") else {}),
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=headers, follow_redirects=True) as client:
            meta_resp = await client.get(f"{api}/repos/{owner}/{repo}")
            if meta_resp.status_code == 404:
                raise FetchError(f"GitHub 上找不到 {owner}/{repo}（或它是私有仓库）")
            if meta_resp.status_code == 403:
                raise FetchError("GitHub API 限流了（未认证 60 次/小时），稍后再试或在 .env 配 GITHUB_TOKEN")
            if meta_resp.status_code >= 400:
                raise FetchError(f"GitHub API 返回 {meta_resp.status_code}")
            meta = meta_resp.json()

            readme = ""
            readme_resp = await client.get(
                f"{api}/repos/{owner}/{repo}/readme",
                headers={**headers, "Accept": "application/vnd.github.raw+json"},
            )
            if readme_resp.status_code == 200:
                readme = readme_resp.text
    except httpx.TimeoutException:
        raise FetchError("GitHub API 超时")
    except httpx.RequestError as exc:
        raise FetchError(f"GitHub API 网络错误：{type(exc).__name__}")

    # README 太长会稀释后面的写作信号：头 8000 字足够看清项目是什么、怎么用
    if len(readme) > 8000:
        readme = readme[:8000] + "\n\n（README 过长，已截断）"

    topics = "、".join(meta.get("topics") or [])
    license_ = ((meta.get("license") or {}).get("spdx_id") or "")
    lines = [
        f"项目：{meta.get('full_name') or f'{owner}/{repo}'}",
        f"简介：{meta.get('description') or '（无描述）'}",
        f"Stars：{meta.get('stargazers_count', '?')} | 语言：{meta.get('language') or '?'}"
        + (f" | 许可：{license_}" if license_ and license_ != "NOASSERTION" else ""),
    ]
    if topics:
        lines.append(f"标签：{topics}")
    if meta.get("homepage"):
        lines.append(f"主页：{meta['homepage']}")
    lines.append(f"最近更新：{(meta.get('updated_at') or '')[:10]}")
    if readme:
        lines += ["", "—— README 原文 ——", readme]

    text = "\n".join(lines).strip()
    return FetchedPage(
        url=url,
        title=f"{meta.get('full_name') or f'{owner}/{repo}'}：{(meta.get('description') or '')[:150]}",
        text=text,
        author=str(meta.get("owner", {}).get("login") or owner)[:80],
        published=(meta.get("updated_at") or "")[:10],
        site="GitHub",
        word_count=len(re.sub(r"\s", "", text)),
        truncated=len(readme) >= 8000,
    )


async def fetch_url(url: str) -> FetchedPage:
    """抓一个网页，抽出正文。失败抛 FetchError，说清原因。"""
    url = _check_url(url)

    # GitHub 是特例：走 API，不爬网页
    repo = _github_repo_url(url)
    if repo:
        return await _github_fetch(url, *repo)

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=TIMEOUT,
            headers={"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
        ) as client:
            resp = await client.get(url)
    except httpx.TimeoutException:
        raise FetchError("抓取超时（15 秒），对方站点可能很慢或屏蔽了请求")
    except httpx.TooManyRedirects:
        raise FetchError("重定向次数过多，链接可能有问题")
    except httpx.RequestError as exc:
        raise FetchError(f"网络错误：{type(exc).__name__}")

    if resp.status_code >= 400:
        raise FetchError(f"对方返回 HTTP {resp.status_code}（{resp.reason_phrase}）")

    content_type = (resp.headers.get("content-type") or "").lower()
    if "html" not in content_type and "text" not in content_type:
        raise FetchError(f"不是网页（Content-Type: {content_type or '未知'}）")

    raw = resp.content
    truncated = len(raw) > MAX_BYTES
    if truncated:
        raw = raw[:MAX_BYTES]

    try:
        html_text = raw.decode(resp.encoding or "utf-8", errors="replace")
    except (LookupError, UnicodeDecodeError):
        html_text = raw.decode("utf-8", errors="replace")

    # trafilatura 抽正文
    text = ""
    meta: dict = {}
    try:
        text = trafilatura.extract(
            html_text,
            include_comments=False,
            include_tables=True,
            favor_recall=True,
            url=url,
        ) or ""
        meta = trafilatura.extract_metadata(html_text) or {}
    except Exception as exc:  # 解析器内部错误不能让整个请求挂掉
        raise FetchError(f"正文解析失败：{type(exc).__name__}")

    title = getattr(meta, "title", "") or ""
    author = getattr(meta, "author", "") or ""
    date = getattr(meta, "date", "") or ""
    sitename = getattr(meta, "sitename", "") or ""

    # 兜底：从 <title> 抠标题
    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.S | re.I)
        if m:
            title = re.sub(r"\s+", " ", m.group(1)).strip()

    if not text.strip():
        raise FetchError(
            "抓到了网页，但抽不出正文。"
            "常见原因：页面内容靠 JS 渲染（本服务不启浏览器）、需要登录、或正文在图片里。"
        )

    text = text.strip()
    return FetchedPage(
        url=url,
        title=title[:200],
        text=text,
        author=author[:80],
        published=date[:40],
        site=sitename[:80],
        word_count=len(re.sub(r"\s", "", text)),
        truncated=truncated,
    )


def to_prompt_material(page: FetchedPage, limit: int = 6000) -> str:
    """把抓到的网页整理成喂给模型的原材料文本。"""
    body = page.text[:limit]
    head = []
    if page.title:
        head.append(f"标题：{page.title}")
    if page.site:
        head.append(f"来源：{page.site}")
    if page.published:
        head.append(f"发布：{page.published}")
    head.append(f"原文链接：{page.url}")
    cut = "（正文过长，已截断）" if len(page.text) > limit else ""
    return "\n".join(head) + "\n\n" + body + cut


# ---------------------------------------------------------- GitHub 热点
# github.com/trending 网页在国内服务器直连不通，而且它是 HTML 没有官方 API。
# 等价的官方接口是 Search API，但有个事实要说清楚：
# Search API 只能按「总 star 数」排序，给不出「近 7 天 star 增量」——
# 任何"用 Search API 复刻 trending"都是在做近似。两种近似各有偏向：
#   mode=new  近 N 天新建 + star 达标 → 抓「新秀」，老牌项目永远不上榜
#   mode=hot  高星 + 近 N 天有推送   → 抓「大牌在动」，新项目进不来
# 默认 new（体感最接近 trending）；换一批翻页，翻到头自动切另一个榜单。

async def github_trending(days: int = 7, limit: int = 10, mode: str = "new", page: int = 1) -> dict:
    """热点仓库。返回 {mode, page, has_more, items}。"""
    from datetime import datetime, timedelta, timezone

    days = max(1, min(days, 30))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    api = "https://api.github.com"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "refract-content-agent",
        **({"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}"} if os.environ.get("GITHUB_TOKEN") else {}),
    }
    if mode == "hot":
        q = f"stars:>3000 pushed:>{since}"
    else:
        mode = "new"
        q = f"created:>{since} stars:>200"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=headers) as client:
            resp = await client.get(
                f"{api}/search/repositories",
                params={
                    "q": q, "sort": "stars", "order": "desc",
                    "per_page": max(3, min(limit, 20)),
                    "page": max(1, min(page, 10)),
                },
            )
    except httpx.RequestError as exc:
        raise FetchError(f"GitHub Search API 网络错误：{type(exc).__name__}")
    if resp.status_code == 403:
        raise FetchError("GitHub API 限流了（未认证 60 次/小时），稍后再试")
    if resp.status_code >= 400:
        raise FetchError(f"GitHub Search API 返回 {resp.status_code}")

    data = resp.json()
    items = data.get("items") or []
    total = min(int(data.get("total_count") or 0), 1000)  # API 最多翻到 1000 条
    out = [
        {
            "full_name": i.get("full_name"),
            "description": i.get("description") or "",
            "stars": i.get("stargazers_count", 0),
            "language": i.get("language") or "",
            "url": i.get("html_url"),
            "topics": (i.get("topics") or [])[:5],
        }
        for i in items
    ]
    if not out:
        raise FetchError("这一页没有仓库了，再点一次会切换榜单")
    return {"mode": mode, "page": page, "has_more": page * limit < total, "items": out}
