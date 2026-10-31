"""Director：把文案变成视频/分镜。

职责：文案 → 分镜表 → 渲染参数。
不写文案（那是 Writer），只管「怎么拍」。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..llm import LLMClient
from ..pipeline.brief import plan
from ..pipeline.storyboard import Shot, storyboard
from ..schemas import ContentBrief


@dataclass
class ShotPlan:
    brief: ContentBrief
    shots: list[Shot]

    def summary(self) -> str:
        lines = [f"主题：{self.brief.topic}", f"共 {len(self.shots)} 个镜头："]
        for s in self.shots:
            lines.append(f"{s.shot_id} [{s.task}] {s.screen_text}（{s.duration}s）")
        return "\n".join(lines)


async def plan_shots(
    llm: LLMClient,
    user_input: str,
    platform: str = "video",
) -> ShotPlan:
    """把需求拆成分镜表。"""
    brief = await plan(llm, user_input, platform, "medium")
    shots = storyboard(brief, "")
    return ShotPlan(brief=brief, shots=shots)
