"""真实链路探针：用 DeepSeek 跑一次 Remotion 管线，产出完整工程。

用法（需要 DEEPSEEK_API_KEY）：
    .venv/Scripts/python.exe tests/remotion_probe.py [主题]
产物：data/videos/probe-remotion/ 工程 + 控制台摘要
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.llm import LLMClient
from app.pipeline import remotion_video
from app.pipeline.director import VideoPlan
from tests.test_remotion import make_brief


async def main() -> None:
    if not settings.is_ready or settings.mock:
        print("未配置真实模型（检查 DEEPSEEK_API_KEY / LLM_MOCK=0）")
        return
    llm = LLMClient(settings, __import__("app.config", fromlist=["registry"]).registry)
    llm.provider("video")  # 早期失败：没配凭据直接抛
    provider = llm.provider("video")
    print(f"video 路由 → {provider.label} ({provider.model}) @ {provider.base_url}")

    brief = make_brief()
    plan = VideoPlan(
        id="B", name="深色 HUD 宣传片", style="hud", concept="流水线式产品介绍",
        duration=18, palette=["#22d3ee", "#4ade80"],
        beats=["开场立题", "输入归一化", "反思机制", "收尾 CTA"],
    )
    res = await remotion_video.generate(llm, brief, plan, settings.data_dir, "probe-rm")
    print(f"\n工程: {res.project_dir}")
    print(f"模型: {res.provider_label} / {res.model}  总时长: {res.duration_s:.1f}s")
    print(f"闸门: ok={res.gate.ok} fallbacks={res.gate.stats.get('fallbacks')}")
    for s in res.shots:
        flag = "⚠️模板" if s.used_fallback else "✅"
        print(f"  S{s.idx:02d} [{s.card or 'tpl'}] {s.duration_s:.1f}s 「{s.screen_text}」 {flag}")
        for f in s.gate_failures[:2]:
            print(f"      - {f}")
    print(f"音效: {len(res.gate.stats.get('sfx', []))} 段")
    remotion_video.write_info_page(res, "probe-rm", settings.data_dir, res.model)
    print("预览: cd", res.project_dir, "&& npm install && npm run studio")


if __name__ == "__main__":
    asyncio.run(main())
