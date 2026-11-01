"""Executor：按 brief 生成正文，流式返回。

反思给的 issues 会作为 hints 注入，这是"循环越转越聪明"的关键——
不带 history 的重试等于重新掷骰子。
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

from .. import mock
from ..llm import LLMClient
from ..schemas import ContentBrief
from .brief import brief_to_prompt

_SYSTEM = """你是公众号主笔，写作风格：信息密度高、不注水、有自己的判断。

硬要求：
1. 严格按给定大纲的节次和顺序写，每节用 "## " 二级标题。
2. 开篇第一句直接用钩子，不要加"在当今时代"这类引子。
3. 只用给定的事实点里、且标注了来源的数据。没有来源的说法，用"根据实践观察"这类模糊表述，不要编具体数字。
4. 不写"综上所述""总而言之"这类收尾套话，结尾直接给 CTA。
5. **字数是最硬的要求**：全文控制在 {word_limit} 字（允许 ±10%）。大纲共 {n_sections} 节，每节约 {per_section} 字。
   写超了就是不合格——多出来的内容挤占了读者的耐心，也拖慢了生成。
6. 输出 Markdown 正文，不要代码块包裹，不要输出标题之外的解释。

写作心法（动笔前在心里过一遍，不用写在稿子里）：
- 一篇文章只留一个锐利的观点——读者看完能记住的那句话是什么？想不清楚就先别写。
- 论证方式要换着来：故事、数据、类比、反问交替出现，通篇一种说理方式读者三段就走。
- 情感要有曲线：开头制造张力，中段"给信息→留新问题"交替推进，别从头到尾一个调。
- 埋 2-3 个金句（开头、转折、结尾各一），标准是能脱离上下文单独转发，且不是口号。
- 视角按段切换：故事段用第一人称拉近距离，分析段用第三人称保客观，建议段对读者说"你"。
- 结尾的互动钩子要让读者"有话想说"，开放式提问或争议立场都行——"求点赞关注"是最低级的。

排版增强（用了观感明显更好，但**按内容定，不要硬套**）：
- 表格只在「确实要并排对比多项同类信息」时才用（比如价格对比、参数对比、优缺点并列）。
  叙事、观点、故事类内容不要硬塞表格——一段话能说清的就用话。
  用表格用 Markdown 语法（| 语法，含 |---|---| 分隔行），别堆成大段文字
- 配图占位 3-5 个（每个大节一张很正常），插在概念首次出现的位置，格式一行：
  ![图片内容的描述性短语](prompt: 画面描述 + 风格关键词 + 色调 + 构图)
  **alt 必须写成可当搜索关键词的描述**（如"感知机示意图 神经元结构"），
  系统会拿它联网搜图；写"这里放什么图"这种空话等于没写。
  prompt 供后续画图生成用，要具体到能直接喂给画图模型。"""

_PLATFORM_STYLE = {
    "wechat": "长文，段落短（3-4 行），每节之间用小标题分隔。",
    "xiaohongshu": "像朋友分享，不像文章：300-800 字，分点清晰，小节用 emoji 做标记，"
                   "口语化短句每段不超过 2 行，正文里自然带 5-8 个话题标签（#格式），"
                   "开头两行直接给亮点或痛点。",
    "video": "口播稿，全篇可朗读，句子口语化，避免括号和表格，节奏快；"
             "前三句就是钩子，短句为主，结尾给明确的行动号召。",
}


async def compose_stream(
    llm: LLMClient,
    brief: ContentBrief,
    hints: list[str] | None = None,
    persona: str = "",
) -> AsyncIterator[tuple[str, str]]:
    """产出 (kind, text)，kind ∈ {reasoning, content}。

    persona：来自项目记忆的人设卡（人设/语气/读者/禁用词）。
    传进来就拼进 system prompt —— 这是「记忆真正被用起来」的地方，
    没有它每篇稿子的风格都会飘。
    """
    if llm.cfg.mock:
        body = mock.mock_body(brief)
        for piece in mock.mock_stream_chunks("先确认大纲节次和行为约束，再按节展开。", 8):
            yield "reasoning", piece
            await asyncio.sleep(0.02)
        for piece in mock.mock_stream_chunks(body, 10):
            yield "content", piece
            await asyncio.sleep(0.012)
        return

    n_sections = max(1, len(brief.outline))
    per_section = max(120, brief.word_limit // n_sections)
    system = _SYSTEM.format(word_limit=brief.word_limit, n_sections=n_sections, per_section=per_section)
    if persona:
        system = system + persona

    hint_block = ""
    if hints:
        hint_block = (
            "\n\n本次必须修复上一轮的问题（逐条落实到正文里）：\n"
            + "\n".join(f"- {h}" for h in hints)
            + f"\n\n注意：这是对已有稿件的**修订**，不是重写。篇幅仍控制在 {brief.word_limit} 字左右，"
            "只改动有问题的地方，不要借补充细节扩写——实测里最容易出的事就是修订轮字数翻三倍。"
        )

    user = (
        f"{brief_to_prompt(brief)}\n\n"
        f"平台风格：{_PLATFORM_STYLE.get(brief.platform, _PLATFORM_STYLE['wechat'])}"
        f"{hint_block}"
    )
    # 中文约 1 字 1.6 token，再给推理留 900 的余量；不设上限会让模型放开写
    budget = min(6000, int(brief.word_limit * 2.2) + 900)
    async for kind, text in llm.stream(system=system, user=user, max_tokens=budget):
        yield kind, text
