"""Supervisor：对话入口的主 Agent。三层，每层的成本和权力都不同。

第一层 规则（0 成本）：问候、问能力、感谢这类话，正则直接回话术。
        绝不进模型——「你好」换来一篇 4000 字文章，不是聪明，是失控。

第二层 规划（1 次调用）：把用户消息变成执行计划——用哪些工具、什么顺序。
        这是「自主」所在：模型看消息自己决定，不是路由表写死。
        约束写死在 prompt 里：没说要做内容生产就不许安排生产任务。

第三层 执行（N 次工具 + 1 次收尾）：按计划逐步跑，每步向前端推一张
        「正在调用什么工具」的卡片；有产出后流式生成最终答复。

为什么不是自由 ReAct 循环（想一步做一步）：
对话里的任务大多 1-3 步，先出完整计划的成本更低，而且前端能把
整个计划亮出来，用户在第一步就能纠正方向——走错了不用等它跑完。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..llm import LLMClient
from .memory_keeper import MemoryKeeper

Emitter = Callable[[dict[str, Any]], Awaitable[None]]


# ============================================================
# 第一层：规则层，零成本
# ============================================================

_GREETING = re.compile(r"^(你好|您好|hi|hello|hey|嗨|哈喽|在吗|在么|早上好|下午好|晚上好|早|晚上好呀)[\s!！.。~～]*$", re.I)
_THANKS = re.compile(r"^(谢谢|感谢|thx|thanks|多谢|辛苦了)[\s!！.。~～]*$", re.I)
_CAPABILITY = re.compile(r"(你能(干|做|干什么|做什么)|你会(什么|干|做)|(怎么|如何)(用|玩)|帮助|help|能干嘛|都有(什么|啥)(功能|能力))", re.I)
_IDENTITY = re.compile(r"(你是谁|你叫什么|介绍下?你自己|self.?intro)", re.I)

_GREETING_REPLIES = [
    "你好。丢给我一句话、一篇稿或一个链接都行——写公众号、出视频方案、看各平台热点都可以。",
    "嗨。想写点什么，还是先看看小红书、微博、GitHub 最近什么火？",
]
_THANKS_REPLIES = ["不客气。有下一个直接说。", "小事。下一个。"]


def small_talk(text: str) -> str | None:
    """命中闲聊就回话术，返回 None 表示不是闲聊。"""
    t = (text or "").strip()
    if not t:
        return "说点什么？一句话、一个链接，或者「看看 GitHub 热点」。"
    if _GREETING.match(t):
        import random

        return random.choice(_GREETING_REPLIES)
    if _THANKS.match(t):
        import random

        return random.choice(_THANKS_REPLIES)
    if _IDENTITY.search(t):
        return (
            "我是 Refract 的主 Agent。底下有几个干活的：Researcher 抓链接取材，"
            "Writer 走规划-写作-反思出稿，Director 管视频。你说需求，我来拆。"
        )
    if _CAPABILITY.search(t):
        return (
            "现在能干的：\n"
            "1. 写文章 —— 一句话或一个链接，走完规划、写作、自检迭代，产出能直接发的稿\n"
            "2. 解析链接 —— GitHub 仓库连 star 数和 README 一起拿，普通网页抽正文\n"
            "3. 热点榜单 —— 小红书/微博/知乎/抖音/B站实时热榜，GitHub 新星仓库，选一个直接成稿\n"
            "4. 视频方案 —— 给主题出三套拍法，选定后生成动画成片\n"
            "直接说需求就行。"
        )
    return None


# ============================================================
# 第二层：规划
# ============================================================

PLANNER_SYSTEM = """你是 Refract 的主 Agent，把用户消息变成执行计划。

可用工具（三个「取信息」工具要分清，别用错）：

【取信息三件套——本质不同，按用户意图选】
- hot_topics {"source": "...", "limit": 10}
    拉某个平台的「热榜/热搜」——返回的是**榜单**（按热度排序的标题列表 + 热度值）。
    用户要「看看某平台最近什么火、热榜、热搜、流行」时才用。source 可选：
    rednote=小红书、weibo=微博、zhihu=知乎、douyin=抖音、bilibili=B站、
    baidu=百度热搜、tieba=百度贴吧。
- lookup_hot {"title": "...", "source": "...", "rank": 2}
    针对榜单里的**某一条热点**，联网搜它的正文/报道，搞清楚「这条到底讲什么」。
    热榜只给标题没给内容，用户追问「XX 是怎么回事」时用这个，别用 web_search
    （裸搜标题会被同名无关内容带偏，比如「海鸥雨」裸搜会搜出契诃夫《海鸥》话剧）。
    用户说「第 2 条」「第二条讲讲」时，直接给 rank=2 最省事（榜单从 1 数起，
    来源取【已知信息】里那个榜单的平台）；也可以把标题原样填进 title。
- web_search {"query": "..."}
    **泛化的网页搜索**——搜事实、概念、天气、新闻、价格、人物、事件。返回带来源链接的摘要。
    它跟「榜单」无关：用户问「XX 是什么/多少钱/怎么样」这种具体问题用它，
    而不是用户想看「某平台热榜」时用它。

【其余工具】
- today {}                          获取今天的真实日期（年月日+星期）。写/问时效性内容（天气、新闻、
                                    行情、节日）前**必须先调它**——你的训练数据是旧的，别拿记忆里的日期当今天
- fetch_url {"url": "..."}          解析一个链接的内容（网页抽正文 / GitHub 仓库信息）
- github_trending {"days": 7}       拉 GitHub 上 star 涨最快的新仓库（也是榜单，但专指 GitHub）
- write_article {"input": "...", "platform": "wechat|xiaohongshu|video", "words": "short|medium|long"}
                                    写一篇可发布的文案。走完整的规划-写作-自检迭代。正文流式输出，不用你转述
- video_plans {"input": "..."}      为主题直接生成一条动画视频（自主创作，网页直接播放）
- memory_recall {}                  读取用户的人设偏好和历史表现

关于【长期记忆】——用户消息最前面如果有一段「这位用户以前说过的」，那是系统从
以前会话里自动记住的（跨会话、跨网络都在），直接当事实引用，别问用户「要我记住吗」、
更别说自己没记忆。用户说「记一下 XX」照常回答「记下了」就行，系统会自动存。
**没有这一段就说明确实没存过任何东西**——此时不许说「你之前说过 / 按你定的调子 /
我记得你偏好」这类话，那是编的。没有依据就按通用建议答。

关于【已知信息】——用户消息前面如果带了这一段，说明之前几轮真的查过东西：
- 里面列出的热榜条目、搜过的词、生成的产物，都是**真实数据**，直接引用就行
- 用户说「第 N 条」「刚才那个」「上一篇」时，先在这一段里找对应对象，
  **不要重新拉一遍榜单/重搜一遍**（浪费额度，而且榜单会变，反而对不上）
- 找不到对应对象（比如已知信息里根本没有榜单）才考虑调工具；实在没有就问用户

决策流程（先判断任务属于哪一类，再定工具，不是套固定流程）：

【第一类：直接回答，不调工具】
- 闲聊、问候、问你能干什么 → steps 空数组，reply 直接答
- 「上一个问题/刚才说的/之前问的」回顾性 → steps 空数组，从「对话前文」里找上一句原文作答

【第二类：取信息再回答（简单问题，别搞复杂流程）】
- 用户想看「某平台热榜/热搜/最近什么火」 → hot_topics（这是拉榜单，不是搜网页）
- 用户追问榜单里某条「是什么/说的什么」 → lookup_hot 读详情
- 用户问具体事实/概念/新闻/价格（跟榜单无关） → web_search
- **问天气/新闻/行情等时效性问题 → 先 today 取今天日期，再 web_search 带正确日期搜**
  （否则会拿过时的日期去搜，答非所问）
- 给了链接要「读一下/分析」 → fetch_url
- 以上取到素材就行，别加多余步骤。取到后 summarize=true，说人话给用户。

【第三类：编排任务（要产出内容，才多步编排）】
- 明确要写文章/做视频 → 才安排 write_article / video_plans，必要时前面加取素材步骤
  （拿热榜写稿 → hot_topics；写时效性内容 → web_search；给链接写稿 → fetch_url）

- **写文章前先看信息全不全**。三件事必须清楚：写什么主题、多长（短/中/长）、发哪个平台
  （公众号 wechat / 小红书 xiaohongshu）。缺任何一项就问，别自己替用户定：
  steps 空数组，reply 一次性把缺的都问出来（例如「主题有了，还需要确认：篇幅短中长？发公众号还是小红书？」）。
  用户明确说了「随便/你定」才自己定。
- **写热点类文章必须先把内容查到**。你手上只有榜单标题，标题不等于内容：
  先从榜单里挑定那条 → lookup_hot 读正文；用户给了链接 → fetch_url 读网页；
  通用选题 → web_search 搜一轮。拿到素材再 write_article，别只拿标题就动笔。
- write_article 的 input 要把主题 + 素材要点一起写清楚，不能只写四个字。
- **热点稿的 input 必须写清来源平台和热度**，例如：
  「结合百度热搜第 1 条「A股再现涨近日」（热度 761.57w）写一篇公众号长文」。
  系统会自动补一段来源说明兜底，但你写清楚更稳——同一条热点在百度（新闻时事）
  和小红书（个人视角）是两篇不同的稿，来源决定角度和语气。

【没有工具能做到的，明说做不到】
- 如果用户要的东西，上面的工具一个都做不了（比如"帮我打电话""帮我订机票""控制我的手机"），
  就 steps 空数组，reply 里直接说「这个我做不了，我能做的是 xxx」，别硬编、别假装、别硬套一个不相关的工具。
  诚实比逞能重要。

工具选择细则（热点 vs 搜索，务必分清）：
- 「看看热点」「最近有什么火的」「XX热榜/热搜」 → hot_topics（拉榜单，不是 web_search）
- 追问某条热点「是什么/说的什么」 → lookup_hot（带 source），别裸 web_search
- 「XX 是什么/多少钱/怎么样/为什么」这类具体问题 → web_search（跟榜单无关）
- 「结合热点写小红书/公众号」 → hot_topics → lookup_hot 读正文 → write_article（三步，缺一不可）
- 「分析/读一下 + 链接」 → fetch_url
- 复合任务最多 3 步，按依赖排序
- write_article 的 input 要写清楚完整要求，不能只写四个字

只输出 JSON：
{
  "reply": "不需要工具时的直接回复；有工具时给一句开工说明（10 字以内）；做不到时直接说做不到",
  "summarize": true,
  "steps": [
    {"tool": "web_search", "args": {"query": "..."}, "title": "联网查询", "why": "用户问了事实性问题"}
  ]
}

关于 summarize 字段：
- 第二类（联网查再回答）→ 一定 true，你要把搜索结果说人话
- 第三类里 write_article/video_plans 自己会输出正文/视频 → false，避免重复
- 做不到/闲聊 → 无所谓（没工具执行，用不上）
"""


@dataclass
class Step:
    tool: str
    args: dict = field(default_factory=dict)
    title: str = ""
    why: str = ""


@dataclass
class Plan:
    reply: str = ""
    steps: list[Step] = field(default_factory=list)
    summarize: bool | None = None  # None=未指定(按默认规则)，True/False=planner 显式控制
    mocked: bool = False


class ToolError(RuntimeError):
    pass


def _fallback_plan(text: str) -> Plan:
    """规划模型不可用时的兜底：规则 intent 已经很准，直接映射成单步计划。"""
    from .intent import detect, extract_url

    it = detect(text)
    if it.kind == "research":
        # 只了解不产出：有链接抓链接，没链接就联网搜一把讲清楚，不写稿。
        # 这类「问个问题要答案」的场景，搜完必须总结回复
        url = extract_url(text)
        if url:
            steps = [Step("fetch_url", {"url": url}, "解析链接")]
        else:
            steps = [Step("web_search", {"query": text[:100]}, "联网了解")]
        return Plan(reply="帮你查一下。", steps=steps, summarize=True)
    if it.kind == "fetch_and_article":
        url = extract_url(text)
        steps = [Step("fetch_url", {"url": url}, "解析链接")]
        steps.append(Step("write_article", {"input": text}, "写文章"))
        return Plan(reply="按规则直接安排。", steps=steps)
    if it.kind == "video":
        return Plan(reply="生成动画。", steps=[Step("video_plans", {"input": text}, "生成动画视频")])
    # article（明确写了要产出）才走写稿
    return Plan(reply="收到。", steps=[Step("write_article", {"input": text, "platform": "wechat"}, "写文章")])


async def make_plan(llm: LLMClient, text: str, context: str = "") -> Plan:
    if llm.cfg.mock:
        # mock 模式也走规则版，保证链路可测且零成本
        p = _fallback_plan(text)
        p.mocked = True
        return p

    def _validate(d: dict) -> None:
        if not isinstance(d.get("steps"), list):
            raise ValueError("steps 必须是数组")
        known = {t for t in TOOLS}
        for s in d["steps"]:
            if s.get("tool") not in known:
                raise ValueError(f"未知工具：{s.get('tool')}")

    user_prompt = f"用户消息：{text[:1500]}"
    if context:
        user_prompt = f"{context}\n\n{user_prompt}"

    try:
        data = await llm.structured(
            system=PLANNER_SYSTEM,
            user=user_prompt,
            max_tokens=3000,  # 推理模型的思考 token 共用额度，1200 会被吃光
            temperature=0.2,
            validate=_validate,
            route="text",
        )
    except Exception:
        return _fallback_plan(text)

    steps = [
        Step(
            tool=s["tool"],
            args=s.get("args") or {},
            title=str(s.get("title") or s["tool"])[:40],
            why=str(s.get("why") or "")[:80],
        )
        for s in data.get("steps", [])[:3]
    ]
    return Plan(
        reply=str(data.get("reply") or "")[:300],
        steps=steps,
        summarize=data.get("summarize") if isinstance(data.get("summarize"), bool) else None,
    )


# ============================================================
# 第三层：工具执行
# ============================================================

# 每个 runner 接 (args, llm, keeper, task_id, emit)，返回给收尾层的结果 dict。
# 工具不直接碰 HTTP 层——产物保存、留痕是 main 的事，这里只产出数据。


async def _run_fetch_url(args: dict, llm, keeper, task_id, emit, results) -> dict:
    from ..pipeline.fetch import FetchError, fetch_url, to_prompt_material

    url = str(args.get("url") or "").strip()
    if not url:
        raise ToolError("缺 url")
    page = await fetch_url(url)
    await emit({"type": "tool_detail", "lines": [
        f"标题：{page.title[:60]}",
        f"来源：{page.site} · {page.word_count} 字",
    ]})
    return {
        "tool": "fetch_url",
        "summary": f"《{page.title[:50]}》 {page.site} · {page.word_count} 字",
        "url": url,
        "title": page.title[:80],
        "words": page.word_count,
        "material": to_prompt_material(page, 5000),
    }


async def _run_github_trending(args: dict, llm, keeper, task_id, emit, results) -> dict:
    from ..pipeline.fetch import FetchError, github_trending

    items = await github_trending(days=int(args.get("days") or 7), limit=int(args.get("limit") or 8))
    await emit({"type": "tool_detail", "lines": [
        f"{i['full_name']} ★{i['stars']} {i['language']}".strip() for i in items[:8]
    ]})
    return {
        "tool": "github_trending",
        "summary": f"近 {args.get('days', 7)} 天热点 top{len(items)}",
        "items": items,
    }


async def _run_web_search(args: dict, llm, keeper, task_id, emit, results) -> dict:
    from ..pipeline.search import SearchError, search, to_material

    query = str(args.get("query") or "").strip()
    if not query:
        raise ToolError("缺 query")
    res = await search(query, max_results=int(args.get("max_results") or 5))
    await emit({"type": "tool_detail", "lines": [
        f"{r['title'][:44]} — {r['url'][:44]}" for r in res.items[:5]
    ]})
    return {
        "tool": "web_search",
        "summary": f"「{query[:24]}」搜到 {len(res.items)} 条资料",
        "query": query,
        "hits": [{"title": r["title"], "url": r["url"]} for r in res.items],
        "material": to_material(res),  # 带 URL 来源，写作时事实点可溯源
    }


async def _run_hot_topics(args: dict, llm, keeper, task_id, emit, results) -> dict:
    from ..pipeline.hotlist import SOURCES, HotListError, fetch_hot, to_material

    source = str(args.get("source") or "rednote").strip().lower()
    # 模型有时给中文名，兜底映射一下
    alias = {"小红书": "rednote", "微博": "weibo", "知乎": "zhihu", "抖音": "douyin",
             "b站": "bilibili", "哔哩哔哩": "bilibili", "百度": "baidu",
             "贴吧": "tieba", "百度贴吧": "tieba"}
    source = alias.get(source, source)
    try:
        hl = await fetch_hot(source, limit=int(args.get("limit") or 10))
    except HotListError as exc:
        # 热榜是灵感素材不是关键路径：拿不到就明说，别让整步计划崩
        await emit({"type": "tool_detail", "lines": [f"热榜接口没拿到数据：{exc}"]})
        return {
            "tool": "hot_topics",
            "summary": f"{SOURCES.get(source, source)}拉取失败",
            "material": "",
        }
    await emit({"type": "tool_detail", "lines": [
        f"{i['rank']}. {i['title']}" + (f"（{i['score']}）" if i["score"] else "")
        for i in hl.items[:10]
    ]})
    return {
        "tool": "hot_topics",
        "summary": f"{hl.source_label} top{len(hl.items)}",
        "source": hl.source,
        "items": hl.items,
        "material": to_material(hl),  # 自动流入 write_article
    }


async def _run_lookup_hot(args: dict, llm, keeper, task_id, emit, results) -> dict:
    """读某条热点的详情。热榜只给标题，要弄清「这条到底讲什么」必须联网读报道。

    title 可以由模型直接给，也可以只给 rank——rank 指向之前拉过的榜单
    （本轮 results 里的，或上一轮存进会话状态的），用户说「第二条讲讲」就走这条。
    """
    from ..pipeline.search import SearchError, search, to_material

    title = str(args.get("title") or "").strip()
    source = str(args.get("source") or "").strip()
    rank = args.get("rank", args.get("index"))

    score = ""
    if not title and rank is not None:
        # rank 从 1 数起：用户说的「第二条」就是 items[1]
        prev = next((r for r in results if r.get("tool") == "hot_topics" and r.get("items")), None)
        if prev:
            try:
                item = prev["items"][int(rank) - 1]
                title = item["title"]
                score = str(item.get("score") or "")
                source = source or prev.get("source", "")
            except (IndexError, KeyError, TypeError, ValueError):
                title = ""
    if not title:
        raise ToolError("缺 title（要读哪条热点；也可以给 rank=第几条）")

    # 裸搜标题容易被无关内容带偏（「海鸥雨」裸搜会搜出契诃夫《海鸥》话剧），
    # 带上平台语境，让搜索引擎知道这是「某平台的某条热点」。
    # 语境词和另外两条写作入口共用一份，别在这里另写一张表。
    from ..pipeline.research import build_query

    query = build_query(f"「{title}」", source)
    try:
        res = await search(query, max_results=6)
    except SearchError as exc:
        await emit({"type": "tool_detail", "lines": [f"搜索热点详情失败：{exc}"]})
        return {"tool": "lookup_hot", "summary": f"「{title[:20]}」详情搜索失败",
                "query": query, "material": "", "found": False}

    if not res.items:
        await emit({"type": "tool_detail", "lines": [f"搜「{query[:30]}」没有任何返回"]})
        return {"tool": "lookup_hot", "summary": f"「{title[:20]}」没搜到详情",
                "query": query, "material": "", "found": False}

    await emit({"type": "tool_detail", "lines": [
        f"{r['title'][:46]} — {r['url'][:40]}" for r in res.items[:6]
    ]})
    return {
        "tool": "lookup_hot",
        "summary": f"「{title[:20]}」详情（{len(res.items)} 篇）",
        "hot_title": title,   # 收尾时要说清「用户问的是这条」，别和搜索结果的序号混了
        # 来源和热度一并返回：后面 write_article 要靠它把「这条来自哪、有多热」
        # 一起交给写作，不然模型只知道一个标题
        "source": source,
        "score": score,
        "query": query,
        "hits": [{"title": r["title"], "url": r["url"]} for r in res.items],
        "material": to_material(res),
        "found": True,
    }



def brief_img_hint(args: dict, body: str) -> str:
    """给搜图的短提示：优先用用户输入的前 20 字。"""
    return (str(args.get("input") or "")[:20] or body[:20]).replace("\n", " ")


def _infer_hotspot(input_text: str, results: list[dict]) -> Hotspot:
    """从本轮已跑过的工具结果里，还原「这条热点来自哪个平台、热度多少」。

    为什么要推断而不是指望模型：write_article 的 input 是模型自己写的，
    实测它经常只写标题（「结合 A股再现涨近日 写一篇」），平台来源就被丢掉了。
    而 hot_topics / lookup_hot 的结果就在眼前，代码还原比模型自觉可靠。
    """
    from ..pipeline.research import Hotspot

    # 1. 优先看 lookup_hot：它就是「某条热点的详情」，source/title 最直接
    for r in reversed(results):
        if r.get("tool") == "lookup_hot" and r.get("source"):
            return Hotspot(
                source=str(r.get("source") or ""),
                title=str(r.get("hot_title") or ""),
                score=str(r.get("score") or ""),
            )

    # 2. 否则看榜单：在 items 里找那条标题出现在写作要求里的
    for r in reversed(results):
        if r.get("tool") != "hot_topics" or not r.get("items"):
            continue
        for i, item in enumerate(r["items"], 1):
            t = str(item.get("title") or "")
            if t and t in input_text:
                return Hotspot(
                    source=str(r.get("source") or ""),
                    title=t,
                    score=str(item.get("score") or ""),
                    rank=i,
                )
    return Hotspot()


async def _run_write_article(args: dict, llm, keeper, task_id, emit, results) -> dict:
    from ..pipeline.engine import run_pipeline
    from ..pipeline.research import build_query, context_block
    from ..pipeline.search import search, to_material

    input_text = str(args.get("input") or "")
    # 热点来源：模型写的 input 里经常只有标题，这里从本轮结果里还原出来，
    # 等素材拼完再补进 input（先补会把来源说明也当成检索词的一部分）。
    hot = _infer_hotspot(input_text, results)

    # 前序工具的素材自动注入：搜索结果 / 链接正文，都带来源
    extras = [r["material"] for r in results if r.get("material")]
    if not extras and not llm.cfg.mock:
        # 用户直接说「写一篇关于 XX」时没有前序素材，得自己搜一次。
        # 搜到的东西一律给模型，相关与否让模型自己判断——代码算相似度
        # 判不准（同义词、缩写、跨语言都算不准），模型看标题和来源更靠谱。
        # 检索词带平台语境：裸搜标题会被同名无关内容带偏。
        q = build_query(input_text, hot.source)
        if q:
            try:
                await emit({"type": "stage", "stage": "fetch",
                            "label": f"检索「{q[:18]}」相关资料"})
                sr = await search(q, max_results=5)
                if sr.items:
                    extras = [to_material(sr)]
                    await emit({"type": "note",
                                "text": f"已检索到 {len(sr.items)} 篇资料，写稿时参考"})
                else:
                    await emit({"type": "note",
                                "text": "没检索到资料，基于公开常识写，别编细节"})
            except Exception:
                pass  # 检索是增强不是关键路径，失败照常写
    if extras:
        input_text = (
            input_text
            + "\n\n【检索素材（含来源链接，引用时必须标注）】\n"
            + "\n\n".join(extras)
        )
    if hot:
        # 来源放在素材之后：这两段都是「事实」，写作指令本身在最前面
        input_text = f"{input_text}\n\n{context_block(hot)}"

    result = await run_pipeline(
        task_id=task_id,
        user_input=input_text,
        platform=str(args.get("platform") or "wechat"),
        llm=llm,
        words=str(args.get("words") or "medium"),
        style="auto",
        emit=emit,  # stage/delta/reflection 直接透传，前端渲染成卡片
        persona=keeper.persona_profile(),
        history_hint=keeper.reflect_hint(),
        hot=hot,
    )

    # 配图和文章板块走同一套（images.fill_placeholders）：
    # 占位→真图、图不够→补到 3 张、多张候选→模型判断相关性。
    # 之前这里另写了一份「用外链不落盘」的版本，两边行为迟早漂。
    body = result["body"]
    try:
        from ..config import settings as _settings
        from ..pipeline import images as _images

        if _images.enabled() and not llm.cfg.mock:
            body2, n_ok = await _images.fill_placeholders(
                body, _settings.data_dir / "images" / task_id,
                topic=(result.get("brief") or {}).get("topic", ""), llm=llm,
            )
            if n_ok:
                body = body2
                await emit({"type": "note", "text": f"配图 {n_ok} 张（相关性已过滤）"})
    except Exception:
        pass  # 配图是锦上添花，失败保持原稿

    result["body"] = body
    return {
        "tool": "write_article",
        "summary": f"{result['word_count']} 字 · {len(result['rounds'])} 轮 · 最终 {result.get('final_score')} 分",
        "task_id": task_id,
        "topic": (result.get("brief") or {}).get("title", ""),
        "platform": str(args.get("platform") or "wechat"),
        "result": result,
    }


async def _run_video_plans(args: dict, llm, keeper, task_id, emit, results) -> dict:
    """对话里做视频：直接生成（自主创作），不再出方案让用户选。

    和视频模块一个思路——Agent 从内容自动定视觉方向，走配方卡管线直接出片。
    返回 video_url，前端直接嵌播放器。
    """
    from ..config import settings
    from ..pipeline import remotion_video
    from ..pipeline.director import VideoPlan, recommend_plans
    from ..pipeline.brief import plan as plan_brief

    brief = await plan_brief(llm, str(args.get("input") or ""), "video", "medium")
    await emit({
        "type": "decision", "stage": "brief",
        "label": f"内容简报：{brief.topic}",
        "lines": [f"角度：{brief.angle or '（未定）'}", f"受众：{brief.audience}",
                  f"事实点：{len(brief.facts)} 条 · 大纲 {len(brief.outline)} 节"],
    })

    # 自动定视觉方向（取推荐首套），失败用默认方案兜底
    plan_obj: VideoPlan | None = None
    if not llm.cfg.mock:
        try:
            ps = await recommend_plans(llm, brief, opus_custom=False)
            if ps.plans:
                plan_obj = ps.plans[0]
        except Exception:
            plan_obj = None
    if plan_obj is None:
        plan_obj = VideoPlan(
            id="A", name="自动方案", style="hud",
            concept="以产品界面为主角，几何动效推进", why="自主创作默认方向",
            duration=28, beats=[], palette=["#0b0f17", "#22d3ee", "#f8fafc"], motion="",
        )

    await emit({"type": "stage", "stage": "render", "label": "选配方卡 · 逐镜头生成动画"})
    res = await remotion_video.generate(llm, brief, plan_obj, settings.data_dir, task_id, emit=emit)

    # 首选模型不可用、已降级——立刻告诉用户，别让他纳闷画面质量怎么变了
    if res.fallback:
        await emit({"type": "fallback", **res.fallback})

    # 在线预览：bundle 成网页可直接播放
    build_dir = await remotion_video.bundle_project(res.project_dir)

    return {
        "tool": "video_plans",
        "summary": f"《{brief.topic[:24]}》{len(res.shots)} 镜头 · {res.duration_s:.1f}s",
        "task_id": task_id,
        "topic": brief.topic,
        "video_url": f"/v/{task_id}" if build_dir else None,
        "project_dir": str(res.project_dir),
        "model": res.model,
        "fallback": res.fallback,
        "degraded": not res.gate.ok,
    }


async def _run_memory_recall(args: dict, llm, keeper, task_id, emit, results) -> dict:
    return {
        "tool": "memory_recall",
        "summary": f"{len(keeper.recall())} 条记忆",
        "items": [{"key": i.get("key"), "kind": i.get("kind")} for i in keeper.recall()][:10],
    }


async def _run_today(args: dict, llm, keeper, task_id, emit, results) -> dict:
    """返回今天的日期。写时效性内容（天气/新闻/行情）前先取它，
    否则模型可能用训练时的旧日期，把过时信息当今天。"""
    from datetime import datetime

    now = datetime.now()
    return {
        "tool": "today",
        "summary": f"今天是 {now.strftime('%Y-%m-%d')}（{['周一','周二','周三','周四','周五','周六','周日'][now.weekday()]}）",
        "material": f"今天日期：{now.strftime('%Y年%m月%d日')} {['周一','周二','周三','周四','周五','周六','周日'][now.weekday()]}",
    }


TOOLS: dict[str, dict] = {
    "today": {"title": "获取今天日期", "runner": _run_today},
    "fetch_url": {"title": "解析链接", "runner": _run_fetch_url},
    "web_search": {"title": "联网搜索", "runner": _run_web_search},
    "hot_topics": {"title": "拉平台热榜", "runner": _run_hot_topics},
    "lookup_hot": {"title": "读热点详情", "runner": _run_lookup_hot},
    "github_trending": {"title": "拉 GitHub 热点", "runner": _run_github_trending},
    "write_article": {"title": "写文章（规划-写作-反思）", "runner": _run_write_article},
    "video_plans": {"title": "生成动画视频", "runner": _run_video_plans},
    "memory_recall": {"title": "读记忆", "runner": _run_memory_recall},
}


# ============================================================
# 主流程
# ============================================================

async def handle_chat(
    *,
    task_id: str,
    text: str,
    llm: LLMClient,
    keeper: MemoryKeeper,
    emit: Emitter,
    session_id: str = "",
    context: str = "",
    ip: str = "",
    prior_results: list[dict] | None = None,
) -> dict:
    """对话入口。返回结果 dict（给留痕用）。

    context：对话前文 + 已知信息（热榜条目、搜过的词、产物链接），
    由 session_memory.context() 拼好传进来。用户追问「第二条」时，
    planner 手里有那 10 条标题，才不会说找不到。

    prior_results：上一轮的事实还原成的伪工具结果，塞进 results 开头。
    这样 lookup_hot 只给 rank 也能定位到具体那条热点。
    """
    t0 = time.time()

    # --- 第一层：规则 ---
    small = small_talk(text)
    if small is not None:
        await emit({"type": "delta", "kind": "content", "text": small})
        await emit({"type": "done_reply"})
        return {"kind": "chitchat", "route": "rule", "reply": small, "elapsed_ms": int((time.time() - t0) * 1000)}

    # --- 第二层：规划 ---
    await emit({"type": "stage", "stage": "plan", "label": "主 Agent 规划任务"})
    plan = await make_plan(llm, text, context)
    await emit(
        {
            "type": "plan",
            "steps": [{"tool": s.tool, "title": s.title, "why": s.why} for s in plan.steps],
            "mocked": plan.mocked,
        }
    )

    if not plan.steps:
        # 无工具：reply 就是答复。长句直接给，短句也够用
        reply = plan.reply or "收到。"
        await emit({"type": "delta", "kind": "content", "text": reply})
        await emit({"type": "done_reply"})
        return {"kind": "reply", "route": "planner", "reply": reply, "elapsed_ms": int((time.time() - t0) * 1000)}

    # --- 第三层：执行 ---
    # 开头先垫上一轮的事实（热榜条目等），让 lookup_hot 能按 rank 找到那条；
    # 它们标了 prior=True，收尾时不参与总结，也不会被当成新素材。
    results: list[dict] = [dict(r, prior=True) for r in (prior_results or [])]
    failed: list[str] = []
    for i, step in enumerate(plan.steps):
        spec = TOOLS.get(step.tool)
        if spec is None:
            failed.append(f"{step.tool}: 未知工具")
            continue
        # 配额预检查：对话里「写文章」和文章板块共用 article 配额，
        # 不能让用户靠 10 轮对话无限白嫖生成文章（视频成片走 /api/video/render，已单独计）
        if step.tool == "write_article" and ip:
            from ..quota import get_quota

            _q = get_quota()
            _ok, _reason = _q.can(ip, "article")
            if not _ok:
                failed.append(f"write_article: {_reason}")
                await emit({"type": "step_end", "i": i, "tool": step.tool, "status": "failed", "summary": _reason})
                continue
        await emit({"type": "step_start", "i": i, "tool": step.tool, "title": step.title or spec["title"]})
        ts = time.time()
        try:
            out = await spec["runner"](step.args, llm, keeper, task_id, emit, results)
            out["elapsed_ms"] = int((time.time() - ts) * 1000)
            out["title"] = step.title
            results.append(out)
            # 文章真的写出来了，才扣 article 配额（预检查只拦，不扣；这里落账）
            if step.tool == "write_article" and ip:
                from ..quota import get_quota

                get_quota().consume(ip, "article", f"/r/{task_id}")
            await emit({"type": "step_end", "i": i, "tool": step.tool, "status": "ok", "summary": out["summary"], "elapsed_ms": out["elapsed_ms"]})
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"
            failed.append(f"{step.tool}: {msg[:120]}")
            await emit({"type": "step_end", "i": i, "tool": step.tool, "status": "failed", "summary": msg[:160]})

    # --- 收尾：把工具产出整理成答复（流式） ---
    # 只收本轮跑出来的（fresh）——上一轮的热榜/搜索只是给模型做指代用，
    # 不该重复列一遍，更不能当成这一轮的结论。
    fresh = [r for r in results if not r.get("prior")]
    # 文章步已经在执行中流式输出过正文，这里只补产物卡；
    # 其余工具（热点/解析）没有正文输出，收尾生成完整答复。
    art = next((r for r in fresh if r["tool"] == "write_article"), None)
    if art:
        await emit(
            {"type": "artifact", "artifact": "article", "url": f"/r/{task_id}", "summary": art["summary"]}
        )

    plans_step = next((r for r in fresh if r["tool"] == "video_plans"), None)
    if plans_step:
        # 直接生成完成：emit video 事件，前端直接嵌播放器
        await emit({
            "type": "video",
            "url": plans_step.get("video_url") or f"/v/{task_id}",
            "degraded": plans_step.get("degraded", False),
            "model": plans_step.get("model", ""),
            "fallback": plans_step.get("fallback"),
            "summary": plans_step.get("summary", ""),
        })

    # 是否生成文字答复：默认「有内容就总结」（泛化，不依赖模型输出字段），
    # planner 明确说 summarize=false 时才跳过（写稿/出视频这种工具自己输出的场景）。
    material_results = [
        r for r in fresh
        # lookup_hot 即使没搜到也要留下：得让模型知道「查过了、确实没有」，
        # 否则它会当没发生过，转而凭标题瞎编
        if r.get("material") or r.get("items") or r.get("tool") == "lookup_hot"
    ]
    if material_results and plan.summarize is not False:
        brief_lines = []
        for r in material_results:
            tool = r["tool"]
            if tool == "hot_topics" and r.get("items"):
                brief_lines += [f"- {i['rank']}. {i['title']}" + (f"（{i['score']}）" if i.get("score") else "") for i in r["items"]]
            elif tool == "github_trending" and r.get("items"):
                brief_lines += [f"- {i['full_name']}（★{i['stars']} {i['language']}）：{(i.get('description') or '')[:60]}" for i in r["items"]]
            elif tool == "lookup_hot":
                if r.get("material"):
                    # 点明「问的是这条」——否则模型会把用户说的「第2条」当成
                    # 下面搜索结果里的 [2]，答成另一件事（线上真发生过）
                    brief_lines.append(
                        f"【热点详情】用户要了解的热点是「{r.get('hot_title') or ''}」"
                        f"（下面的 [1][2]... 是搜到的网页编号，不是用户说的第几条）：\n{r['material']}"
                    )
                else:
                    brief_lines.append(
                        "【热点详情】没搜到可靠信息——搜索返回的内容和这条标题对不上，"
                        "无法确认它具体讲什么。回答时如实说明查不到，"
                        "不要凭标题猜测或编造细节。"
                    )
            elif tool == "fetch_url":
                brief_lines.append(f"- {r['summary']}\n  摘录：{r['material'][:600]}")
            elif tool == "memory_recall" and r.get("items"):
                brief_lines.append(f"- 记忆里有：{', '.join(i['key'] for i in r['items'])}")
            elif r.get("material"):
                # 兜底：任何带 material 的工具（web_search 等）直接把素材喂给模型
                brief_lines.append(r["material"])
        if failed:
            brief_lines.append(f"- 失败的步骤：{'; '.join(failed)}")

        gave = ""
        async for kind, chunk in llm.stream(
            system="你是 Refract 的助手。基于工具返回的结果，用中文给用户一个干脆的答复。"
            "用户问什么就答什么（问天气就报天气、问新闻就说新闻、问对比就列对比），"
            "直接回答，别讲流程。列表类就按要点列，别加套话开头（不要'好的''当然'）。"
            "搜索结果里可能混着不相干的内容——先看标题和来源判断是不是同一件事，"
            "对不上就别引用，直接说「没查到可靠信息」，别硬凑。"
            "控制在 400 字内。如果结果里有 GitHub 仓库或热点，可以提示用户「要写成稿就说一声」。",
            user=f"用户问的是：\n{text}\n\n工具结果：\n" + "\n\n".join(brief_lines),
            max_tokens=2400,  # deepseek-flash 是推理模型，reasoning 会和正文抢额度，900 会把答复拦腰截断
            temperature=0.4,
            route="text",
        ):
            if kind == "content":
                gave += chunk
                await emit({"type": "delta", "kind": "content", "text": chunk})
        if not gave.strip():
            # 模型没给就退到原始数据
            await emit({"type": "delta", "kind": "content", "text": "\n".join(brief_lines)})

    await emit({"type": "done_reply"})
    return {
        "kind": "task",
        "route": "planner",
        "steps": [{"tool": r["tool"], "summary": r["summary"], "elapsed_ms": r["elapsed_ms"]} for r in fresh],
        "failed": failed,
        "elapsed_ms": int((time.time() - t0) * 1000),
        "results": fresh,  # 全量给上层：write_article 的 result 里带 body/brief，留痕要用
    }
