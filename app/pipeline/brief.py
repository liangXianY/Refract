"""Planner：把任意输入归一化成 ContentBrief。

这一步是整个系统的入口，也是最该被质疑的地方——
面试官会问"为什么不让下游各自解析输入"。
答：输入形态是 3 种、产物是 3 种，不归一化就是 9 条链路。
"""

from __future__ import annotations

from .. import mock
from ..llm import LLMClient
from ..schemas import ContentBrief

SYSTEM = """你是资深内容策划。任务：把用户的任意输入，转成一份结构化的选题方案。

规则：
1. 只输出 JSON，不要任何解释文字、不要 markdown 代码块。
2. outline 的每一节必须有明确论点，不要写"背景介绍"这种空标题。
3. facts 只放可以在文中当论据的具体说法；找不到可靠出处的，source 字段留 null。
   绝对不要为了凑数编造数据——source 为 null 会在前端被标黄，这是刻意设计的暴露。
4. hook 必须是能让人停下来的第一句，不要写"随着 AI 的发展"这类套话。
5. word_limit 和 platform 由系统给定，原样回填，不要自己发挥。

输出 JSON 结构：
{
  "topic": "主题，40 字内",
  "angle": "切入角度，一句话",
  "audience": "目标读者",
  "platform": "原样回填系统给定的平台",
  "tone": "语气描述",
  "word_limit": 0,
  "hook": "开头第一句",
  "outline": [{"heading": "小节标题", "points": ["论点1", "论点2"]}],
  "facts": [{"claim": "具体说法", "source": "出处或 null"}],
  "cta": "结尾行动号召"
}"""


def _user_prompt(user_input: str, platform: str, word_limit: int, tone: str) -> str:
    return (
        f"目标平台：{platform}\n"
        f"目标篇幅：{word_limit} 字（反映到 word_limit 字段）\n"
        f"期望语气：{tone}\n"
        f"用户输入：\n<<<\n{user_input[:4000]}\n>>>\n\n"
        f"请生成选题方案 JSON。outline 给 4-6 节，每节 2-3 个论点。"
    )


# 平台规格由代码决定，不交给模型。
# 实测过：让模型自己填 word_limit，它会直接给上限 6000，
# 结果单轮写了 11670 字，耗时从 30 秒涨到 115 秒，反思还把稿子打成 replan。
# 篇幅是产品决策，不是创作决策。
PLATFORM_SPEC: dict[str, dict[str, object]] = {
    "wechat": {"word_limit": 1200, "tone": "专业、克制、有观点，不堆术语"},
    "xiaohongshu": {"word_limit": 550, "tone": "轻松口语、有个人感、不端着"},
    "video": {"word_limit": 800, "tone": "口语化、节奏快、能直接念"},
}


async def plan(llm: LLMClient, user_input: str, platform: str = "wechat") -> ContentBrief:
    spec = PLATFORM_SPEC.get(platform, PLATFORM_SPEC["wechat"])

    if llm.cfg.mock:
        return mock.mock_brief(user_input, platform)

    def _validate(raw: dict) -> None:
        ContentBrief.model_validate({**raw, "platform": platform})

    raw = await llm.structured(
        system=SYSTEM,
        user=_user_prompt(user_input, platform, int(spec["word_limit"]), str(spec["tone"])),  # type: ignore[arg-type]
        max_tokens=4000,
        validate=_validate,
    )
    raw.setdefault("platform", platform)
    brief = ContentBrief.model_validate(raw)

    # 模型在这三个字段上没有决定权：写什么平台、多少字、什么调性，是产品侧定的
    brief.platform = platform  # type: ignore[assignment]
    brief.word_limit = int(spec["word_limit"])  # type: ignore[arg-type]
    if not brief.tone:
        brief.tone = str(spec["tone"])
    return brief


def brief_to_prompt(brief: ContentBrief) -> str:
    """把 brief 序列化回给执行器看的紧凑文本。别整段 json.dumps，模型读起来更费 token。"""
    lines = [
        f"主题：{brief.topic}",
        f"切入角度：{brief.angle}",
        f"目标读者：{brief.audience}",
        f"语气：{brief.tone}",
        f"篇幅：约 {brief.word_limit} 字",
        f"开头钩子（可优化但保留核心意思）：{brief.hook}",
        "",
        "大纲：",
    ]
    for idx, sec in enumerate(brief.outline, start=1):
        lines.append(f"{idx}. {sec.heading}")
        for p in sec.points:
            lines.append(f"   - {p}")
    if brief.facts:
        lines.append("")
        lines.append("可用事实点（有 source 的才可以当数据引用，source 为 null 的不要写成确定数据）：")
        for f in brief.facts:
            lines.append(f"   - {f.claim}" + (f"（来源：{f.source}）" if f.source else "（无出处，慎用）"))
    if brief.cta:
        lines += ["", f"结尾 CTA：{brief.cta}"]
    return "\n".join(lines)
