"""对话跨轮追问的端到端测试（假模型 + 假工具，不联网）。

复现线上那条：「给我看看微博热搜」→ 返回 10 条 →「详细讲一下第二条」→ 说找不到。
根因有两处，这里都要覆盖：
1. 上一轮的 10 条标题没有进上下文（只存了「微博 top10」一句摘要）
2. rank 从 0 数起，说第 2 条实际取的是第 3 条（甚至越界后报「缺 title」）
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agents import supervisor  # noqa: E402
from app.agents.session_context import SessionMemory  # noqa: E402

HOT = [{"rank": i + 1, "title": f"热点标题{i + 1}", "score": f"{i + 1}0w", "link": ""}
       for i in range(10)]


class FakeLLM:
    """只回放预先排好的计划，不真的推理。"""

    class cfg:
        mock = False

    def __init__(self, plans: list[dict]) -> None:
        self.plans = plans
        self.n = 0

    async def structured(self, **_kw):
        p = self.plans[min(self.n, len(self.plans) - 1)]
        self.n += 1
        return p

    async def stream(self, **kw):
        yield "content", "好的。"

    async def complete(self, **_kw):
        return "摘要", None


async def _noop(_ev):
    return None


def main() -> int:
    d = tempfile.mkdtemp()
    mem = SessionMemory()
    mem.configure(d)

    fired: list[str] = []

    async def fake_hot(args, llm, keeper, task_id, emit, results):
        return {"tool": "hot_topics", "summary": "微博 top10", "source": "weibo",
                "items": HOT, "material": "热榜素材……", "elapsed_ms": 10}

    orig_hot = supervisor.TOOLS["hot_topics"]
    supervisor.TOOLS["hot_topics"] = {"title": "拉平台热榜", "runner": fake_hot}
    try:
        # —— 第一轮：拉热榜 ——
        llm1 = FakeLLM([{"reply": "拉微博热搜。", "summarize": True,
                         "steps": [{"tool": "hot_topics", "args": {"source": "weibo"},
                                    "title": "拉微博热搜", "why": "用户要看热搜"}]}])
        r1 = asyncio.run(supervisor.handle_chat(
            task_id="t1", text="看看微博热搜", llm=llm1, keeper=None, emit=_noop,
            session_id="s1", context=mem.context("s1"), prior_results=mem.prior_results("s1"),
        ))
        mem.push("s1", "user", "看看微博热搜")
        mem.record_results("s1", r1["results"])
        mem.push("s1", "assistant", "微博 top10")

        ctx = mem.context("s1")
        assert "热点标题2" in ctx, "第二轮的上下文里必须有上一轮的条目"

        # —— 第二轮：只给 rank=2 ——
        import app.pipeline.search as search_mod

        async def fake_search(query: str, max_results: int = 5, **_kw):
            from app.pipeline.search import SearchResult

            fired.append(query)
            return SearchResult(query=query, items=[{"title": "详情", "url": "http://x", "content": "…"}])

        orig_search = search_mod.search
        search_mod.search = fake_search
        try:
            llm2 = FakeLLM([{"reply": "读第二条。", "summarize": True,
                             "steps": [{"tool": "lookup_hot", "args": {"rank": 2, "source": "weibo"},
                                        "title": "读第二条热点", "why": "用户追问第二条"}]}])
            r2 = asyncio.run(supervisor.handle_chat(
                task_id="t2", text="详细讲一下第二条", llm=llm2, keeper=None, emit=_noop,
                session_id="s1", context=mem.context("s1"), prior_results=mem.prior_results("s1"),
            ))
        finally:
            search_mod.search = orig_search
    finally:
        supervisor.TOOLS["hot_topics"] = orig_hot

    assert r2["failed"] == [], r2["failed"]
    assert fired and "热点标题2" in fired[0], f"应该搜第 2 条，实际：{fired}"
    print("PASS 跨轮追问：第二轮按 rank=2 定位到", fired[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
