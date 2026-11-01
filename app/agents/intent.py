"""意图识别：决定这次请求派给哪个 agent。

这是多 agent 编排的入口。判断靠规则 + 置信度，不无脑调模型——
理由和 brief 的 word_limit 一样：延迟、成本、可缓存。
规则能覆盖 90% 的场景，剩下的用 `low_confidence` 标记出来，由上层决定要不要追问。

意图类型：
- article  写文案（默认）
- video    出视频/分镜
- research 只抓取分析，不写
- fetch_and_article  带链接的写作（先抓后写）
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Intent:
    kind: str  # article | video | research | fetch_and_article
    confidence: float  # 0-1
    reason: str
    agent: str  # 派给哪个 agent
    needs_fetch: bool = False


# 关键词表。按「越具体越优先」排，先匹配到先返回。
_VIDEO_WORDS = ("视频", "短片", "动画", "分镜", "镜头", "宣传片", "成片", "mp4")
_RESEARCH_WORDS = ("分析一下", "总结一下这个链接", "抓取", "提取正文", "读一下这个页面", "讲讲", "怎么回事", "了解一下", "介绍一下")
# 写作词只用「明确要产出的动作词」。平台名（小红书/公众号）不算——用户说
# 「小红书热点」是想看热点，不是要写小红书文案，靠平台名判断会把「了解」误判成「写作」。
_ARTICLE_WORDS = ("文案", "文章", "写一篇", "写个", "写篇", "起稿", "口播稿", "推文", "写一篇文章", "出个稿")

_URL_RE = re.compile(r"https?://[^\s，。、）)]+", re.I)


def detect(input_text: str = "", url: str = "", hint: str = "") -> Intent:
    """判断意图。

    hint：前端显式指定的模式（如 platform=video），优先级高于关键词。
    """
    text = (input_text or "").strip()
    has_url = bool(url.strip()) or bool(_URL_RE.search(text))

    # 1. 显式 hint 最优先
    if hint == "video":
        return Intent("video", 1.0, "前端指定了视频模式", "director", has_url)
    if hint == "research":
        return Intent("research", 1.0, "前端指定了抓取模式", "researcher", True)

    # 2. 只有链接、没别的要求 → 抓取分析
    if has_url and not text:
        return Intent("research", 0.95, "只给了链接，按抓取分析处理", "researcher", True)

    # 3. 视频关键词
    video_hits = [w for w in _VIDEO_WORDS if w in text]
    if video_hits:
        return Intent(
            "video",
            0.9,
            f"命中视频关键词：{video_hits[0]}",
            "director",
            has_url,
        )

    # 4. 抓取关键词
    research_hits = [w for w in _RESEARCH_WORDS if w in text]
    if research_hits:
        return Intent(
            "research",
            0.85,
            f"命中抓取关键词：{research_hits[0]}",
            "researcher",
            True,
        )

    # 5. 带链接的写作
    if has_url:
        return Intent(
            "fetch_and_article",
            0.88,
            "给了链接 + 写作要求，先抓后写",
            "researcher→writer",
            True,
        )

    # 6. 写作关键词
    article_hits = [w for w in _ARTICLE_WORDS if w in text]
    if article_hits:
        return Intent(
            "article",
            0.85,
            f"命中写作关键词：{article_hits[0]}",
            "writer",
            False,
        )

    # 7. 兜底：不明意图。不再默认「写作」——问一句热点/好奇一句话，
    #    不该被派去生成一篇带反思迭代的文章。交给 research（只分析不产出），
    #    上层 planner 若判断需要才进一步安排写稿。
    return Intent(
        "research",
        0.4,
        "没有明确关键词，按「只了解不产出」处理（置信度低）",
        "researcher",
        False,
    )


def extract_url(text: str) -> str:
    """从一段文字里抠出第一个 URL。"""
    m = _URL_RE.search(text or "")
    return m.group(0) if m else ""
