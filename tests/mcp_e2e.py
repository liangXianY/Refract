"""MCP 端到端自检：用官方 client 连起本项目的 MCP server，
列工具 + 调几个工具，证明「能力已通过 MCP 暴露给外部 agent」真的成立。

用法（需真实 key，或只想看工具列表可以不带 key）：
    .venv/Scripts/python.exe tests/mcp_e2e.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> int:
    params = StdioServerParameters(
        command=str(ROOT / ".venv" / "Scripts" / "python.exe"),
        args=[str(ROOT / "mcp_server.py")],
        env=None,  # 继承当前环境（DEEPSEEK_API_KEY 等）
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print(f"[MCP] 连接成功，共 {len(names)} 个工具：")
            for n in names:
                print(f"  - {n}")

            # 只调不花钱、不依赖 key 的能力：热榜（60s 本地实例）
            if "hot_topics" in names:
                try:
                    r = await session.call_tool("hot_topics", {"source": "weibo", "limit": 3})
                    text = (r.content[0].text if r.content else "")[:300]
                    print("\n[hot_topics weibo] 返回：")
                    print(text)
                except Exception as exc:
                    print(f"\n[hot_topics] 调用失败（可能没配 HOT_API_BASE/key）：{type(exc).__name__}: {exc}")
            return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
