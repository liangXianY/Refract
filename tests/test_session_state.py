"""会话状态 + 跨轮追问的测试（不联网、不调模型）。

覆盖线上踩过的两个坑：
1. 上一轮拉过热榜，这一轮问「详细讲一下第二条」→ 说找不到
   （摘要里只有「微博 top10」，没有条目；且 rank 从 0 开始数错了位）
2. 服务重启后上下文全丢（原来只存进程内存）
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agents.session_context import SessionMemory  # noqa: E402

HOT = [
    {"rank": i + 1, "title": f"第{i + 1}条热点标题", "score": f"{i + 1}00w", "link": ""}
    for i in range(10)
]


def _hot_result() -> dict:
    return {
        "tool": "hot_topics",
        "summary": "微博 top10",
        "source": "weibo",
        "items": HOT,
        "material": "……",
        "elapsed_ms": 120,
    }


def test_context_carries_items() -> None:
    d = tempfile.mkdtemp()
    m = SessionMemory()
    m.configure(d)
    m.push("s1", "user", "微博最近什么火")
    m.record_results("s1", [_hot_result()])
    m.push("s1", "assistant", "微博 top10")

    ctx = m.context("s1")
    assert "微博 top10" in ctx, ctx
    assert "3. 第3条热点标题" in ctx, "热榜条目必须原样进上下文，不能只剩一句摘要"
    assert "lookup_hot" in ctx

    prior = m.prior_results("s1")
    assert prior and prior[0]["tool"] == "hot_topics"
    assert prior[0]["items"][1]["title"] == "第2条热点标题"
    assert prior[0]["material"] == "", "旧素材不能当新稿的事实依据"


def test_rank_resolves_across_turns() -> None:
    """上一轮拉过榜单，这一轮只给 rank=2，要能定位到第 2 条。"""
    from app.agents import supervisor

    d = tempfile.mkdtemp()
    m = SessionMemory()
    m.configure(d)
    m.push("s2", "user", "看看微博热搜")
    m.record_results("s2", [_hot_result()])
    prior = m.prior_results("s2")

    seen: list[str] = []

    async def fake_search(query: str, max_results: int = 5, **_kw):
        from app.pipeline.search import SearchResult

        seen.append(query)
        return SearchResult(query=query, items=[{"title": "命中", "url": "http://x", "content": "…"}])

    async def noop(ev):
        return None

    import app.pipeline.search as search_mod

    orig = search_mod.search
    search_mod.search = fake_search  # type: ignore[assignment]
    try:
        out = asyncio.run(
            supervisor._run_lookup_hot({"rank": 2}, None, None, "t1", noop, prior)
        )
    finally:
        search_mod.search = orig

    assert out["found"] is True
    assert "第2条热点标题" in seen[0], f"rank=2 应该指向第 2 条，实际搜的是：{seen[0]}"


def test_persist_across_restart() -> None:
    d = tempfile.mkdtemp()
    m = SessionMemory()
    m.configure(d)
    m.push("s3", "user", "小红书热榜")
    m.record_results("s3", [_hot_result()])

    m2 = SessionMemory()  # 模拟进程重启
    m2.configure(d)
    ctx = m2.context("s3")
    assert "第2条热点标题" in ctx, "重启后热榜条目应该还在"
    assert m2.prior_results("s3"), "重启后仍要能按 rank 定位"


def test_write_article_records_output() -> None:
    d = tempfile.mkdtemp()
    m = SessionMemory()
    m.configure(d)
    m.push("s4", "user", "写一篇")
    m.record_results(
        "s4",
        [{
            "tool": "write_article",
            "summary": "1200 字",
            "task_id": "abc123",
            "result": {"brief": {"title": "测试标题"}, "word_count": 1200},
        }],
    )
    ctx = m.context("s4")
    assert "测试标题" in ctx
    assert "/r/abc123" in ctx


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("ALL PASS")
