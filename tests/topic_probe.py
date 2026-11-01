"""按主题跑一次完整真实链路：brief → 三方案 → 选第一方案 → Remotion 工程。

用法（需要 DEEPSEEK_API_KEY）：
    .venv/Scripts/python.exe tests/topic_probe.py "大模型的发展史"
产物：data/videos/topic-probe/ 工程 + 控制台摘要
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings, registry
from app.llm import LLMClient


async def main() -> None:
    topic = sys.argv[1] if len(sys.argv) > 1 else "大模型的发展史"
    if not settings.is_ready or settings.mock:
        print("未配置真实模型（检查 DEEPSEEK_API_KEY / LLM_MOCK=0）")
        return
    from app.pipeline import remotion_video
    from app.pipeline.brief import plan as brief_plan
    from app.pipeline.director import recommend_plans

    llm = LLMClient(settings, registry)
    provider = llm.provider("video")
    print(f"video 路由 → {provider.label} ({provider.model}) @ {provider.base_url}")

    t0 = time.time()
    print(f"\n[1/3] 整理 brief：{topic}")
    brief = await brief_plan(llm, topic, platform="video", words="medium")
    print(f"  topic={brief.topic} | angle={brief.angle} | hook={brief.hook}")
    for s in brief.outline:
        print(f"  - {s.heading}: {'、'.join(s.points)[:60]}")

    print("\n[2/3] 出三套方案")
    ps = await recommend_plans(llm, brief, opus_custom=False)
    for i, p in enumerate(ps.plans):
        print(f"  方案{chr(65 + i)} [{p.style}] {p.name} | {p.duration}s | {p.concept[:50]}")
    chosen = ps.plans[0]
    print(f"  → 选用方案{chosen.id}: {chosen.name}")

    print("\n[3/3] Remotion 逐镜头生成")
    res = await remotion_video.generate(llm, brief, chosen, settings.data_dir, "topic-probe")
    print(f"\n工程: {res.project_dir}")
    print(f"模型: {res.provider_label} / {res.model}  总时长: {res.duration_s:.1f}s")
    print(f"闸门: ok={res.gate.ok} fallbacks={res.gate.stats.get('fallbacks')}")
    for s in res.shots:
        flag = "⚠️模板" if s.used_fallback else "✅"
        print(f"  S{s.idx:02d} [{s.card or 'tpl'}] {s.duration_s:.1f}s 「{s.screen_text}」 {flag}")
        for f in s.gate_failures[:2]:
            print(f"      - {f}")
    print(f"音效: {len(res.gate.stats.get('sfx', []))} 段")
    remotion_video.write_info_page(res, "topic-probe", settings.data_dir, res.model)
    print(f"\n总耗时 {time.time() - t0:.0f}s")
    print("预览: cd", res.project_dir, "&& npm install && npm run studio")


if __name__ == "__main__":
    asyncio.run(main())
