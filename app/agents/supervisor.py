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
    "你好。丢给我一句话、一篇稿或一个链接都行——写公众号、出视频方案、看 GitHub 热点都可以。",
    "嗨。想写点什么，还是先看看最近 GitHub 上有什么热的东西？",
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
            "3. GitHub 热点 —— 拉最近几天 star 涨得最快的新仓库，选一个直接成稿\n"
            "4. 视频方案 —— 给主题出三套拍法，选定后生成动画成片\n"
            "直接说需求就行。"
        )
    return None


# ============================================================
# 第二层：规划
# ============================================================

PLANNER_SYSTEM = """你是 Refract 的主 Agent，把用户消息变成执行计划。

可用工具：
- fetch_url {"url": "..."}          解析链接内容。GitHub 仓库会连 star 数、语言、README 一起返回；普通网页抽正文
- web_search {"query": "..."}       联网搜索，返回带来源链接的最新资料；写时效性/热点/对比类内容先用它取素材
- github_trending {"days": 7}       拉近 N 天 GitHub 上 star 涨得最快的新仓库
- write_article {"input": "...", "platform": "wechat|xiaohongshu|video", "words": "short|medium|long"}
                                    写一篇可发布的文案。走完整的规划-写作-自检迭代，耗时 1-2 分钟。正文会流式输出，不需要你转述
- video_plans {"input": "..."}      为主题出三套视频拍法，用户会挑一个
- memory_recall {}                  读取用户的人设偏好和历史表现

硬规则：
1. 闲聊、问候、问你能干什么 → steps 空数组，reply 里直接回答，要短，像人说话
2. 用户没提视频就不要安排视频，没提写稿就不要安排写稿。安排任务只看用户明说的
3. 「看看热点」「最近有什么火的」 → github_trending
4. 「分析/看看/读一下 + 链接」 → fetch_url
5. 「写一篇/出文案/发公众号」 → write_article，platform 按用户说的，默认 wechat；
   内容涉及时效信息（最近发生了什么/价格/版本/新闻）时，前面加一步 web_search
6. 明确要视频 → video_plans
7. 复合需求可以拆多步（先拉热点再写稿就是两步），但不超过 3 步，按依赖排序
8. write_article 的 input 要把用户的完整要求写清楚，不能只写四个字

只输出 JSON：
{
  "reply": "不需要工具时给用户的直接回复；有工具时给一句开工说明（10 字以内）",
  "steps": [
    {"tool": "github_trending", "args": {}, "title": "拉 GitHub 热点", "why": "用户想看最近什么火"}
  ]
}
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
    mocked: bool = False


class ToolError(RuntimeError):
    pass


def _fallback_plan(text: str) -> Plan:
    """规划模型不可用时的兜底：规则 intent 已经很准，直接映射成单步计划。"""
    from .intent import detect, extract_url

    it = detect(text)
    if it.kind in ("research", "fetch_and_article"):
        url = extract_url(text)
        steps = [Step("fetch_url", {"url": url}, "解析链接")]
        if it.kind == "fetch_and_article":
            steps.append(Step("write_article", {"input": text}, "写文章"))
        return Plan(reply="按规则直接安排。", steps=steps)
    if it.kind == "video":
        return Plan(reply="出视频方案。", steps=[Step("video_plans", {"input": text}, "出三套视频方案")])
    return Plan(reply="收到。", steps=[Step("write_article", {"input": text, "platform": "wechat"}, "写文章")])


async def make_plan(llm: LLMClient, text: str) -> Plan:
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

    try:
        data = await llm.structured(
            system=PLANNER_SYSTEM,
            user=f"用户消息：{text[:1500]}",
            max_tokens=1200,
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
    return Plan(reply=str(data.get("reply") or "")[:300], steps=steps)


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
        "material": to_material(res),  # 带 URL 来源，写作时事实点可溯源
    }


async def _run_write_article(args: dict, llm, keeper, task_id, emit, results) -> dict:
    from ..pipeline.engine import run_pipeline
    from ..pipeline.search import search

    input_text = str(args.get("input") or "")
    # 前序工具的素材自动注入：搜索结果 / 链接正文，都带来源
    extras = [r["material"] for r in results if r.get("material")]
    if extras:
        input_text = (
            input_text
            + "\n\n【检索素材（含来源链接，引用时必须标注）】\n"
            + "\n\n".join(extras)
        )

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
    )

    # 联网搜图配文：正文里有配图占位就搜一次真图，按序分配给占位
    body = result["body"]
    if not llm.cfg.mock and "](prompt:" in body:
        try:
            img_res = await search(str(args.get("input") or "")[:80], max_results=5, with_images=True)
            urls = iter([i["url"] for i in img_res.images])
            import re as _re
            body = _re.sub(
                r"!\[([^\]]*)\]\(prompt:[^)]*\)",
                lambda m: f"![{m.group(1)}]({u})" if (u := next(urls, None)) else m.group(0),
                body,
            )
        except Exception:
            pass  # 搜图失败保占位，不阻塞出稿

    result["body"] = body
    return {
        "tool": "write_article",
        "summary": f"{result['word_count']} 字 · {len(result['rounds'])} 轮 · 最终 {result.get('final_score')} 分",
        "result": result,
    }


async def _run_video_plans(args: dict, llm, keeper, task_id, emit, results) -> dict:
    from ..pipeline.director import recommend_plans
    from ..pipeline.brief import plan as plan_brief

    brief = await plan_brief(llm, str(args.get("input") or ""), "video", "medium")
    ps = await recommend_plans(llm, brief)
    return {
        "tool": "video_plans",
        "summary": f"《{brief.topic[:24]}》出了 {len(ps.plans)} 套拍法",
        "brief": brief.model_dump(),
        "plans": [p.model_dump() for p in ps.plans],
    }


async def _run_memory_recall(args: dict, llm, keeper, task_id, emit, results) -> dict:
    return {
        "tool": "memory_recall",
        "summary": f"{len(keeper.recall())} 条记忆",
        "items": [{"key": i.get("key"), "kind": i.get("kind")} for i in keeper.recall()][:10],
    }


TOOLS: dict[str, dict] = {
    "fetch_url": {"title": "解析链接", "runner": _run_fetch_url},
    "web_search": {"title": "联网搜索", "runner": _run_web_search},
    "github_trending": {"title": "拉 GitHub 热点", "runner": _run_github_trending},
    "write_article": {"title": "写文章（规划-写作-反思）", "runner": _run_write_article},
    "video_plans": {"title": "出视频方案", "runner": _run_video_plans},
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
) -> dict:
    """对话入口。返回结果 dict（给留痕用）。"""
    t0 = time.time()

    # --- 第一层：规则 ---
    small = small_talk(text)
    if small is not None:
        await emit({"type": "delta", "kind": "content", "text": small})
        await emit({"type": "done_reply"})
        return {"kind": "chitchat", "route": "rule", "reply": small, "elapsed_ms": int((time.time() - t0) * 1000)}

    # --- 第二层：规划 ---
    await emit({"type": "stage", "stage": "plan", "label": "主 Agent 规划任务"})
    plan = await make_plan(llm, text)
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
    results: list[dict] = []
    failed: list[str] = []
    for i, step in enumerate(plan.steps):
        spec = TOOLS.get(step.tool)
        if spec is None:
            failed.append(f"{step.tool}: 未知工具")
            continue
        await emit({"type": "step_start", "i": i, "tool": step.tool, "title": step.title or spec["title"]})
        ts = time.time()
        try:
            out = await spec["runner"](step.args, llm, keeper, task_id, emit, results)
            out["elapsed_ms"] = int((time.time() - ts) * 1000)
            out["title"] = step.title
            results.append(out)
            await emit({"type": "step_end", "i": i, "tool": step.tool, "status": "ok", "summary": out["summary"], "elapsed_ms": out["elapsed_ms"]})
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"
            failed.append(f"{step.tool}: {msg[:120]}")
            await emit({"type": "step_end", "i": i, "tool": step.tool, "status": "failed", "summary": msg[:160]})

    # --- 收尾：把工具产出整理成答复（流式） ---
    # 文章步已经在执行中流式输出过正文，这里只补产物卡；
    # 其余工具（热点/解析）没有正文输出，收尾生成完整答复。
    art = next((r for r in results if r["tool"] == "write_article"), None)
    if art:
        await emit(
            {"type": "artifact", "artifact": "article", "url": f"/r/{task_id}", "summary": art["summary"]}
        )

    plans_step = next((r for r in results if r["tool"] == "video_plans"), None)
    if plans_step:
        await emit({"type": "video_plans", **{"plans": plans_step["plans"], "brief": plans_step["brief"]}})

    # 需要文字答复的：热点/解析类，让模型基于结果说人话
    needs_summary = any(r["tool"] in ("github_trending", "fetch_url", "memory_recall") for r in results)
    if needs_summary:
        brief_lines = []
        for r in results:
            if r["tool"] == "github_trending":
                brief_lines += [
                    f"- {i['full_name']}（★{i['stars']} {i['language']}）：{(i['description'] or '')[:60]}"
                    for i in r["items"]
                ]
            elif r["tool"] == "fetch_url":
                brief_lines.append(f"- {r['summary']}\n  摘录：{r['material'][:600]}")
            elif r["tool"] == "memory_recall":
                brief_lines.append(f"- 记忆里有：{', '.join(i['key'] for i in r['items'])}")
        if failed:
            brief_lines.append(f"- 失败的步骤：{'; '.join(failed)}")

        gave = ""
        async for kind, chunk in llm.stream(
            system="你是 Refract 的助手。基于工具返回的结果，用中文给用户一个干脆的答复。"
            "列表就按要点列，别加套话开头（不要'好的''当然'）。控制在 200 字内。"
            "如果结果里有 GitHub 仓库，告诉用户可以直接说「用 xxx 写一篇」来成稿。",
            user="工具结果：\n" + "\n".join(brief_lines),
            max_tokens=600,
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
        "steps": [{"tool": r["tool"], "summary": r["summary"], "elapsed_ms": r["elapsed_ms"]} for r in results],
        "failed": failed,
        "elapsed_ms": int((time.time() - t0) * 1000),
        "results": results,  # 全量给上层：write_article 的 result 里带 body/brief，留痕要用
    }
