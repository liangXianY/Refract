"""取材：热点来源语境 + 检索词。

为什么单独拎一个文件
--------------------
「写热点」有三条入口，以前各写一份逻辑：

1. 工作台左栏：点热榜 → 生成文章（/api/generate）
2. 对话里的 write_article 工具
3. 对话里的 lookup_hot（读某条热点的正文）

结果只有第 3 条带上了平台语境（搜「标题 + 百度」），前两条模型拿到的是
裸标题。两件事因此同时发生：一是模型不知道这条来自百度热搜还是小红书，
二是裸搜标题会被同名无关内容带偏（「海鸥雨」裸搜能搜出契诃夫《海鸥》）。

现在三条入口共用这里的函数，来源信息和检索词口径统一。

一个关键的认知：**来源平台不是备注，是选题约束。** 百度热搜的读者是来
搞清楚「发生了什么」的，小红书热点的读者是想看「这跟我有什么关系」，
同一个标题在两个平台该写成两篇稿。所以下面的 SOURCE_TONE 不是标签，
是直接写进 prompt 的写作要求。
"""

from __future__ import annotations

from dataclasses import dataclass

from .search import extract_query

# 搜索语境词：拼进检索词，让搜索引擎知道「这是某平台的某条热点」。
# 注意要短、要是平台自称的词（百度就搜「百度」，不搜「百度热搜」——
# 后者会把结果限死在榜单页面本身，搜不到这条的正文报道）。
SOURCE_HINT: dict[str, str] = {
    "rednote": "小红书",
    "weibo": "微博",
    "zhihu": "知乎",
    "douyin": "抖音",
    "bilibili": "B站",
    "baidu": "百度",
    "tieba": "百度贴吧",
}

# 各平台热榜的调性。原样写进 PLAN 的 prompt，用来定角度和语气。
SOURCE_TONE: dict[str, str] = {
    "baidu": "百度热搜：新闻时事场。读者是来搞清楚「发生了什么、跟我有什么关系」的，"
             "要事实清楚、有信息增量，别写成情绪文，也别写成公关稿",
    "tieba": "百度贴吧：社区讨论场。口语、有站队和吐槽的烟火气，可以带梗，别端着",
    "rednote": "小红书热点：种草/生活方式语境。要有「这跟我有什么关系」的个人视角，"
               "口语、有画面感，别写成新闻通稿",
    "weibo": "微博热搜：公共舆论场。要抓争议点和情绪点，节奏快，有明确态度",
    "zhihu": "知乎热榜：观点/解释场。要有分析和论据，能把「为什么」讲透，别只讲「是什么」",
    "douyin": "抖音热点：短视频语境。开头三秒必须抓住人，口语、节奏快、能直接念",
    "bilibili": "B站热门：年轻/兴趣社区语境。可以玩梗、可以有圈层黑话，但事实要准",
}


@dataclass
class Hotspot:
    """一条被选中来写作的热点。来源 + 热度都要传给写作，不只是标题。"""

    source: str = ""     # baidu / weibo / ...（空 = 不是从热榜点的）
    title: str = ""
    score: str = ""      # 榜单给的热度值，形如「761.57w」
    rank: int = 0        # 榜单第几位，1 起

    def __bool__(self) -> bool:
        return bool(self.source or self.title)


def label(source: str) -> str:
    """平台 key → 中文名。查不到就原样返回，别静默丢成空串。"""
    from .hotlist import SOURCES

    return SOURCES.get(source, source or "")


def build_query(text: str, source: str = "") -> str:
    """检索词 = 标题（或原句） + 平台语境。

    标题优先取「」里的（点热榜时前端拼的句式就是这个形态），取不到就用原句。
    """
    q = extract_query(text)
    hint = SOURCE_HINT.get(source or "", "")
    return f"{q} {hint}".strip() if hint else q


def context_block(hot: Hotspot | None) -> str:
    """给 PLAN 阶段的一句话上下文。没有热点就返回空串（手写选题不该凭空冒出来源）。"""
    if not hot:
        return ""
    name = label(hot.source)
    tone = SOURCE_TONE.get(hot.source, "")
    lines = []
    if name:
        lines.append(f"热点来源：{name}")
    if hot.title:
        lines.append(f"热点标题：{hot.title}")
    if hot.score:
        lines.append(f"榜单热度：{hot.score}" + (f"（第 {hot.rank} 位）" if hot.rank else ""))
    if tone:
        lines.append(f"来源调性：{tone}")
    if lines:
        lines.append("选题角度和语气要贴这个来源平台的调性；这条是榜上公认的热度，"
                     "不要写成与热度无关的个人随笔。")
    return "\n".join(lines)
