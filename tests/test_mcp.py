"""MCP 服务端到端测试：起子进程连 stdio，列工具 + 调两个 tool。

不依赖外部客户端，验证协议层真的通。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "Scripts" / "python.exe"


async def main() -> None:
    env = dict(os.environ, LLM_MOCK="1", PYTHONIOENCODING="utf-8")
    params = StdioServerParameters(
        command=str(PY),
        args=[str(ROOT / "mcp_server.py")],
        env=env,
        cwd=str(ROOT),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(f"[init] server={init.server_info.name} version={init.server_info.version}")

            tools = await session.list_tools()
            print(f"[tools] {len(tools.tools)} 个：")
            for t in tools.tools:
                print(f"  - {t.name}")

            # 调 memory_write
            r = await session.call_tool(
                "memory_write",
                {"key": "persona", "value": "MCP 测试写入的人设", "kind": "persona"},
            )
            print(f"[call memory_write] {r.content[0].text}")

            # 调 memory_recall
            r = await session.call_tool("memory_recall", {})
            text = r.content[0].text
            print(f"[call memory_recall] {text[:160].replace(chr(10), ' | ')}")

            # 调 plan_video（会用到 LLM，mock 模式不花钱）
            r = await session.call_tool("plan_video", {"input": "介绍 Agent 是什么"})
            print(f"[call plan_video] {r.content[0].text[:200].replace(chr(10), ' | ')}")


if __name__ == "__main__":
    asyncio.run(main())
