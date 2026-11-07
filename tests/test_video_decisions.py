"""视频决策透出测试（假模型，不联网、不装依赖）。

用户的要求：「用户不干涉，但我要能看到每一次模型的决策，比如剧本是什么样的」。
所以 generate 必须把这几步推成 decision 事件：
视觉方案 → 剧本（每镜的卡/时长/屏幕文字）→ 每镜开始/结束 → 工程闸门。
这个测试就是钉住这条事件序列，改坏了立刻红。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pipeline import remotion_video  # noqa: E402
from app.schemas import ContentBrief, Section  # noqa: E402

TSX = """import React from 'react';
import { AbsoluteFill, interpolate, useCurrentFrame } from 'remotion';

export default function ShotScene({ title, accent, accent2, bg }: { title: string; accent: string; accent2: string; bg: string }) {
  const frame = useCurrentFrame();
  const p = interpolate(frame, [0, 20], [0, 1], { extrapolateRight: 'clamp' });
  return (
    <AbsoluteFill style={{ background: bg, display: 'flex', flexDirection: 'column', justifyContent: 'center', alignItems: 'center', gap: 24, padding: 96 }}>
      <div style={{ fontSize: 88, fontWeight: 700, color: '#14161a', opacity: p }}>{title}</div>
      <div style={{ width: 220 * p, height: 6, background: accent2 }} />
      <div style={{ fontSize: 30, color: '#3a4048' }}>{accent}</div>
    </AbsoluteFill>
  );
}
"""


class FakeProvider:
    label = "假模型"
    model = "fake-1"


class FakeLLM:
    class cfg:
        mock = False

    def provider(self, _route: str):
        return FakeProvider()

    async def structured(self, **_kw):
        return {
            "shots": [
                {"card": "opening-title", "category": "opening", "duration_s": 4.0,
                 "narration": "开场点名主题", "screen_text": "开场", "sfx": [{"t": 0.2, "type": "pop"}]},
                {"card": "feature-grid", "category": "features", "duration_s": 4.5,
                 "narration": "讲清三个能力", "screen_text": "三个能力", "sfx": [{"t": 0.3, "type": "hit"}]},
                {"card": "outro-logo", "category": "outro", "duration_s": 3.5,
                 "narration": "收尾落定", "screen_text": "下次见", "sfx": [{"t": 0.2, "type": "chord"}]},
            ]
        }

    async def complete(self, **_kw):
        return TSX, FakeProvider()


def test_decision_events() -> None:
    events: list[dict] = []

    async def emit(ev):
        events.append(ev)

    brief = ContentBrief(
        topic="把动画渲染搬回前端",
        platform="video",
        word_limit=300,
        outline=[Section(heading="问题"), Section(heading="做法")],
    )
    from app.pipeline.director import VideoPlan

    plan = VideoPlan(id="A", name="界面为主角", style="mono",   # 浅底，TSX 里的深色文字才合规
                     concept="以产品界面为主角", why="产品本身最有说服力",
                     duration=12, beats=[], palette=["#14161a", "#5a6068"], motion="")

    d = Path(tempfile.mkdtemp())
    # 卡库是运行时数据（skills/video-shotcraft），测试机上不一定在；
    # 让它当空库，随便写的卡名都能通过映射校验，测的是决策事件而不是卡库
    orig_index, orig_card = remotion_video.recipes.index, remotion_video.recipes.card_text
    remotion_video.recipes.index = lambda: []
    remotion_video.recipes.card_text = lambda _n: "（测试卡：单焦点定版）"
    try:
        res = asyncio.run(remotion_video.generate(FakeLLM(), brief, plan, d, "t-decision", emit=emit))
    finally:
        remotion_video.recipes.index, remotion_video.recipes.card_text = orig_index, orig_card

    stages = [e["stage"] for e in events if e.get("type") == "decision"]
    assert stages[0] == "plan", stages
    assert "script" in stages, stages
    assert stages.count("shot_start") == len(res.shots) == 3, stages
    assert stages.count("shot_done") == 3, stages
    assert stages[-1] == "gate", stages

    script = next(e for e in events if e.get("stage") == "script")
    assert len(script["shots"]) == 3
    assert script["shots"][1]["screen_text"] == "三个能力", script
    assert "字幕" in script["label"], script["label"]

    assert res.gate.ok, res.gate.failures          # 假 TSX 是合法的，不该落模板
    assert not any(s.used_fallback for s in res.shots)


if __name__ == "__main__":
    test_decision_events()
    print("PASS 视频决策事件：plan → script → shot_start×3 → shot_done×3 → gate")
