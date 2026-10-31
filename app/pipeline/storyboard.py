"""分镜层：把文案正文拆成 Shot 表（镜头序列）。

这是「文案 → 视频」缺失的那一环（吸收 video-shot-planner + dashi 分镜字段）。
之前 renderer 直接从 brief 出动画页，跳过了分镜；现在先有分镜表，渲染器按镜出片。

每个 Shot 的字段：
- shot_id    镜头编号
- narration  旁白/字幕（这镜讲什么）
- screen_text 屏幕上显示的大字
- task       画面任务：封面/结构/论据/结尾（决定画什么）
- duration   时长（秒）
- highlight  核心词（字幕上色用，先留字段）
- transition_in 从前一镜进来的转场（由风格配置映射，见 pick_transition）

mock 模式下用规则拆分（不调 API，不花钱）：
- 封面 = 标题 + hook
- 结构 = 每个大纲节一张
- 论据 = 事实点
- 结尾 = CTA
真实模式下可以换 LLM 生成更精细的分镜，但先跑通规则版。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..schemas import ContentBrief


@dataclass
class Shot:
    shot_id: str
    narration: str = ""
    screen_text: str = ""
    task: str = "text"
    duration: float = 4.0
    highlight: list[str] = field(default_factory=list)
    transition_in: str = "hard_cut"


def storyboard(brief: ContentBrief, body: str = "") -> list[Shot]:
    """把 brief（+ 正文）拆成镜头序列。mock/规则版，不调 LLM。"""

    shots: list[Shot] = []
    i = 1

    # 镜头 1：封面
    shots.append(
        Shot(
            shot_id=f"S{i:02d}",
            narration=brief.hook,
            screen_text=brief.topic,
            task="cover",
            duration=4.0,
            highlight=[brief.topic],
        )
    )
    i += 1

    # 镜头 2..n：结构（每个大纲节一镜）
    for sec in brief.outline:
        points = " · ".join(sec.points[:2]) if sec.points else ""
        shots.append(
            Shot(
                shot_id=f"S{i:02d}",
                narration=points,
                screen_text=sec.heading,
                task="section",
                duration=4.0,
                highlight=[sec.heading],
            )
        )
        i += 1

    # 镜头：论据（有事实点就加，最多 3 条一镜）
    if brief.facts:
        facts_text = "  ".join(f.claim for f in brief.facts[:3])
        shots.append(
            Shot(
                shot_id=f"S{i:02d}",
                narration=facts_text,
                screen_text="哪些数据有出处",
                task="facts",
                duration=4.0,
                highlight=[f.claim for f in brief.facts[:2]],
            )
        )
        i += 1

    # 镜头：结尾
    shots.append(
        Shot(
            shot_id=f"S{i:02d}",
            narration=brief.cta or "感谢阅读",
            screen_text=brief.cta or "感谢阅读",
            task="ending",
            duration=3.5,
        )
    )

    return shots


def shots_to_data(brief: ContentBrief, shots: list[Shot]) -> dict:
    """把 brief + shots 打包成渲染器需要的 JSON 数据。"""
    return {
        "topic": brief.topic,
        "hook": brief.hook,
        "platformLabel": {
            "wechat": "公众号长文",
            "xiaohongshu": "小红书图文",
            "video": "视频口播稿",
        }.get(brief.platform, "内容"),
        "facts": [{"claim": f.claim, "source": f.source} for f in brief.facts],
        "cta": brief.cta,
        "shots": [
            {
                "shot_id": s.shot_id,
                "narration": s.narration,
                "screen_text": s.screen_text,
                "task": s.task,
                "duration": s.duration,
                "highlight": s.highlight,
            }
            for s in shots
        ],
    }
