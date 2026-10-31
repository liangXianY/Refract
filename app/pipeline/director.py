"""导演层：出片前先给三套方案（xilo-opus-video 的核心方法论）。

为什么不是「一句话直接生成视频」
--------------------------------
视频是一次性重投入：写错方向，几百行渲染代码全废，重跑一次几十秒到两分钟。
先花 2 秒出三套方案让用户挑，是整条链路里性价比最高的一步。
xilo 的做法就是「先推荐三套，再成片」，这里照搬。

方案要回答三个问题，缺一个用户就没法选：
  concept  这一版拍成什么样（画面语言，不是文案复述）
  why      为什么这篇内容适合这么拍（要跟内容特征挂钩，不能套话）
  motion   动起来靠什么（决定它是 MG 动画还是动态 PPT）

模型选择：这里用 route="text"（DeepSeek）。
理由：方案是结构化决策，不需要写代码，用 Opus 是浪费；
真正吃代码能力的是下一步「按方案写渲染代码」，那一步才切 Opus。
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from ..llm import LLMClient
from ..schemas import ContentBrief
from .styles import STYLES

SYSTEM = """你是 Motion Graphics 导演，为一个「内容生产 Agent」的演示短片做视觉方案。

你要给出 3 套**彼此差异明显**的方案，让客户挑。

硬要求：
1. 三套方案的视觉语言必须真的不同（信息密度、节奏、色彩、构图各走一路），
   不能是同一个东西换配色。
2. concept 写画面，不写文案。用「屏幕上发生什么」描述，不许写「讲述/介绍/展示」。
3. why 必须引用这篇内容的具体特征（题材、信息密度、是否有数据、受众），
   不许写「视觉冲击力强」这类套话。
4. motion 说清楚运动靠什么驱动：元素入场方式、持续运动、转场手法。
5. duration 控制在 18-30 秒。
6. style 只能从给定的风格库里选，或者填 custom（表示要单独定视觉）。

只输出 JSON：
{
  "plans": [
    {
      "id": "A",
      "name": "方案名（4-8 字，有画面感）",
      "style": "风格库 key 或 custom",
      "concept": "一句话说清屏幕上发生什么（40-70 字）",
      "why": "为什么适合这篇内容（30-60 字，必须引用内容特征）",
      "duration": 22,
      "beats": ["0-3s 开场钩子写什么", "3-9s 主体一", "9-16s 主体二", "16-22s 收尾"],
      "palette": ["#0b0f17", "#22d3ee", "#f8fafc"],
      "motion": "运动语言（30-50 字）"
    }
  ]
}
"""


class VideoPlan(BaseModel):
    id: str = "A"
    name: str = Field(default="方案", max_length=40)
    style: str = "hud"
    concept: str = ""
    why: str = ""
    duration: int = 22
    beats: list[str] = Field(default_factory=list)
    palette: list[str] = Field(default_factory=list)
    motion: str = ""


class PlanSet(BaseModel):
    plans: list[VideoPlan] = Field(default_factory=list)
    model: str = ""
    mocked: bool = False


def _style_menu() -> str:
    """把风格库压成菜单：模型只能选这些 key。"""
    return "\n".join(f"- {s.id}（{s.name}）：底色 {s.bg}，主色 {s.accent}/{s.accent2}，{s.font} 字体" for s in STYLES.values())


def _content_digest(brief: ContentBrief, body: str) -> str:
    sections = "；".join(s.heading for s in brief.outline[:6])
    facts = "；".join(f.claim for f in brief.facts[:4]) or "无明确数据点"
    head = re.sub(r"\s+", " ", body[:600])
    return (
        f"标题：{brief.topic}\n"
        f"切入角度：{brief.angle}\n"
        f"受众：{brief.audience}\n"
        f"语气：{brief.tone}\n"
        f"钩子：{brief.hook}\n"
        f"大纲：{sections}\n"
        f"数据点：{facts}\n"
        f"收尾：{brief.cta}\n"
        f"正文开头：{head}"
    )


def _mock_plans(brief: ContentBrief) -> list[VideoPlan]:
    """不调模型的兜底：三套风格差异明显的模板方案。

    mock 不是「假数据糊弄」，而是「没有模型也能验证整条链路」。
    这三套的差异和真模型给出的应该是同一个量级。
    """
    topic = brief.topic[:16]
    return [
        VideoPlan(
            id="A",
            name="管道解剖",
            style="hud",
            concept=f"屏幕是一台运转中的机器：三条发光管道横贯画面，{topic}的每个环节在管道里被推送、校验、点亮。",
            why="内容有明确流程和大纲分节，适合用机器剖面的方式把抽象结构变成可见的空间关系。",
            duration=22,
            beats=[
                f"0-3s 冷启动扫描线扫过全屏，{topic}标题被逐字点亮",
                "3-9s 三条管道依次通电，标签从管道里浮起",
                "9-16s 数据点在管道中穿行，命中节点时爆出一圈光晕",
                "16-22s 管道汇流成一条，收尾字从光里浮出",
            ],
            palette=["#05080c", "#22d3ee", "#4ade80"],
            motion="管道内流体持续滚动（次级运动），节点脉冲呼吸，命中时 0.2s 急停 + 光晕扩散",
        ),
        VideoPlan(
            id="B",
            name="纸上推演",
            style="line",
            concept=f"白纸上手绘的示意图逐笔生长：{topic}被画成一张思维导图，线条先出，字后落在线上。",
            why="受众是普通读者、语气不生硬，线稿比 HUD 更亲和，读起来像有人在你面前边讲边画。",
            duration=20,
            beats=[
                "0-2s 纸面微纹理浮现，一个手绘圆点在中央落下",
                f"2-7s 从圆点引出三条线，{topic}写在线端",
                "7-15s 分支逐条生长，每条线末端落一个关键词",
                "15-20s 所有线条轻微回弹，收尾句手写体出现",
            ],
            palette=["#f8f7f4", "#1a1a1a", "#e8573f"],
            motion="线条用 stroke-dashoffset 生长，笔触有 1.5° 抖动模拟手绘，节点落位后轻微回弹",
        ),
        VideoPlan(
            id="C",
            name="数据鼓点",
            style="tech",
            concept=f"画面跟着节奏走：{topic}的关键数字逐个砸在网格上，网格随鼓点缩放，数字滚到位时整屏闪一下。",
            why="内容里有可量化的点，数字是最快建立可信度的元素，节奏感能撑住短视频的完播率。",
            duration=18,
            beats=[
                "0-3s 网格从中心扩散铺满，第一个数字从 0 滚到位",
                "3-10s 三个数据点依次砸落，每次落点网格缩放一次",
                "10-15s 数据并排对比，柱状从底部生长",
                "15-18s 所有元素收束成一个结论数字",
            ],
            palette=["#0a0e14", "#f59e0b", "#38bdf8"],
            motion="数字滚动用缓出曲线，落点用 8px 回弹，网格缩放与落点同帧触发（节拍对齐）",
        ),
    ]


def _validate(data: dict) -> None:
    plans = data.get("plans")
    if not isinstance(plans, list) or len(plans) < 2:
        raise ValueError("plans 至少 2 个")
    for p in plans:
        if not p.get("concept"):
            raise ValueError("方案缺 concept")


async def recommend_plans(
    llm: LLMClient,
    brief: ContentBrief,
    body: str = "",
    top_k: int = 3,
) -> PlanSet:
    """出三套方案。mock 模式走规则模板，真实模式走 DeepSeek。"""
    if llm.cfg.mock:
        return PlanSet(plans=_mock_plans(brief)[:top_k], model="mock", mocked=True)

    data = await llm.structured(
        system=SYSTEM,
        user=(
            f"内容摘要：\n{_content_digest(brief, body)}\n\n"
            f"可选风格库：\n{_style_menu()}\n\n"
            f"给 {top_k} 套方案。"
        ),
        max_tokens=2200,
        temperature=0.8,
        validate=_validate,
        route="text",
    )

    plans: list[VideoPlan] = []
    for i, raw in enumerate(data.get("plans", [])[:top_k]):
        style = str(raw.get("style") or "hud")
        if style not in STYLES:
            style = "custom"
        plans.append(
            VideoPlan(
                id=str(raw.get("id") or chr(65 + i)),
                name=str(raw.get("name") or f"方案{chr(65 + i)}")[:40],
                style=style,
                concept=str(raw.get("concept") or "")[:200],
                why=str(raw.get("why") or "")[:200],
                duration=max(12, min(40, int(raw.get("duration") or 22))),
                beats=[str(b)[:120] for b in (raw.get("beats") or [])][:6],
                palette=[str(c) for c in (raw.get("palette") or [])][:4],
                motion=str(raw.get("motion") or "")[:200],
            )
        )
    if not plans:
        return PlanSet(plans=_mock_plans(brief), model="fallback", mocked=True)
    return PlanSet(plans=plans, model=llm.provider("text").model, mocked=False)
