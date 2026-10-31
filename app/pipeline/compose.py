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
6. 输出 Markdown 正文，不要代码块包裹，不要输出标题之外的解释。"""

_PLATFORM_STYLE = {
    "wechat": "长文，段落短（3-4 行），每节之间用小标题分隔。",
    "xiaohongshu": "短句为主，多用换行断句，每段不超过 2 行，避免长段。",
    "video": "口播稿，全篇可朗读，句子口语化，避免括号和表格，节奏快。",
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
