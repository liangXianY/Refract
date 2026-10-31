"""视频链路的真实模型探针。

用法：
    python -m tests.video_probe            # 用当前 video 路由
    python -m tests.video_probe cctq       # 强制指定 provider

它做三件事，每件都打时间：
  1. 出方案（text 路由，便宜）
  2. 让 video 路由写整份 HTML
  3. 跑闸门，把失败原因打出来

产物落到 .tmp/probe-*.html，可以直接用浏览器打开看。
不写进 data/，避免污染正式产物目录。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

from app.config import registry, settings
from app.llm import LLMClient
from app.pipeline.director import recommend_plans
from app.pipeline import opus_video
from app.schemas import ContentBrief, Fact, Section

OUT = Path(__file__).parent.parent / ".tmp"

BRIEF = ContentBrief(
    topic="Refract：把一段输入变成能发的稿",
    angle="从一条链接到一篇能直接发的稿，中间发生了什么",
    audience="对 AI 应用落地感兴趣的技术同学",
    platform="video",
    tone="干脆、有细节",
    hook="你丢一条链接进去，它先拆结构，再写，再自己挑毛病重写。",
    outline=[
        Section(heading="抓取：链接进来先拆干净", points=["正文抽取", "标题作者时间"]),
        Section(heading="规划：先定结构再动笔", points=["大纲四节", "字数按平台定"]),
        Section(heading="反思：自己给自己打分", points=["契约校验零成本", "不过线就打回重写"]),
        Section(heading="产物：稿和片子一起出", points=["文章直接可发", "动画另存一份"]),
    ],
    facts=[Fact(claim="反思一轮平均提分 12.6", source="内部测试 30 篇")],
    cta="Refract，输入到产物只隔一次点击",
)


async def main() -> None:
    force = sys.argv[1] if len(sys.argv) > 1 else ""
    if force:
        registry.routes["video"] = [force]
    print("mock =", settings.mock)
    print("video 路由 =", [ (k, registry.providers[k].ready) for k in registry.routes["video"] ])
    p = registry.resolve("video")
    print("实际使用 =", p.label if p else "None", "|", p.model if p else "")

    llm = LLMClient(settings, registry)

    t0 = time.time()
    ps = await recommend_plans(llm, BRIEF)
    print(f"\n[1] 方案 {time.time()-t0:.1f}s  模型={ps.model}")
    for x in ps.plans:
        print(f"    {x.id} {x.name} / {x.style} / {x.duration}s — {x.concept[:50]}")

    plan = ps.plans[0]
    t1 = time.time()
    print(f"\n[2] 生成中… 用 {plan.name}")
    res = await opus_video.generate(llm, BRIEF, plan)
    print(f"    {time.time()-t1:.1f}s  模型={res.model}  第 {res.attempts} 版  字符={len(res.html)}")

    print("\n[3] 闸门")
    print("    ok =", res.gate.ok)
    print("    stats =", res.gate.stats)
    for f in res.gate.failures:
        print("    ✗", f)

    OUT.mkdir(exist_ok=True)
    path = OUT / f"probe-{int(time.time())}.html"
    path.write_text(opus_video.wrap_meta(res.html, plan, res), encoding="utf-8")
    print("\n产物:", path)
    await llm.aclose()


if __name__ == "__main__":
    asyncio.run(main())
