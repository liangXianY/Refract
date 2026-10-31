"""Writer：写文案。

两档用法：
- write_once()：一轮出稿，快，适合 MCP 的单次调用和简单场景。
- 完整的多轮 Plan-Execute-Reflect 在 pipeline/engine.py，那是主链路，
  Writer 在这里不重复实现，只是把「写」这个动作暴露成清晰的一步。
"""

from __future__ import annotations

from ..llm import LLMClient
from ..schemas import ContentBrief
from ..pipeline.brief import plan
from ..pipeline.compose import compose_stream


async def write_once(
    llm: LLMClient,
    user_input: str,
    platform: str = "wechat",
    words: str = "medium",
    persona: str = "",
) -> tuple[ContentBrief, str]:
    """一轮出稿：plan → compose。返回 (brief, 正文)。"""
    brief = await plan(llm, user_input, platform, words)
    parts: list[str] = []
    async for kind, text in compose_stream(llm, brief, None, persona=persona):
        if kind == "content":
            parts.append(text)
    return brief, "".join(parts)
