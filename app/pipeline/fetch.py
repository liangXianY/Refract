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


async def fetch_url(url: str) -> FetchedPage:
    """抓一个网页，抽出正文。失败抛 FetchError，说清原因。"""
    url = _check_url(url)

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
