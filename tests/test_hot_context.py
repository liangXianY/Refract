"""热点来源的契约测试。

之前踩过的坑：点热榜写文章时，只有标题进了 prompt，来源平台（百度/小红书…）
和热度值在点击那一刻就丢了。模型因此不知道这条是百度热搜的新闻时事，
还是小红书的生活方式话题——同一个标题该写成两篇不同的稿。

三条写作入口（/api/generate、对话的 write_article、lookup_hot）以前各写一份
逻辑，只有 lookup_hot 带了平台语境。这个脚本把「来源必须传下去」钉成契约：

1. 检索词必须带平台语境（裸搜标题会被同名无关内容带偏）
2. PLAN 的 prompt 里必须出现来源平台和热度
3. 接口和前端都得有对应的字段（少一个就静默丢失）
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

HTML = os.path.join(ROOT, "web", "index.html")
JS = os.path.join(ROOT, "web", "app.js")


def read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_query_carries_source_hint() -> None:
    """检索词 = 标题 + 平台语境词。

    「海鸥雨」裸搜能搜出契诃夫《海鸥》话剧——带上平台才搜得到这条热点本身。
    """
    from app.pipeline.research import build_query

    text = "结合「海鸥雨」这个热点写一篇"
    assert build_query(text, "baidu") == "海鸥雨 百度", build_query(text, "baidu")
    assert build_query(text, "rednote") == "海鸥雨 小红书"
    # 没有来源（手写选题）：原样返回，别凭空加词
    assert build_query(text, "") == "海鸥雨"
    assert build_query("随便写一篇", "") == "随便写一篇"


def test_context_block_has_source_and_score() -> None:
    """给 PLAN 的那段上下文：平台名、热度、调性都要在。"""
    from app.pipeline.research import Hotspot, context_block

    hot = Hotspot(source="baidu", title="A股再现涨近日", score="761.57w", rank=1)
    block = context_block(hot)
    assert "百度热搜" in block, block
    assert "761.57w" in block and "第 1 位" in block, block
    assert "新闻时事" in block, "来源调性要写进 prompt，否则模型不知道该用什么角度"

    # 手写选题（没点热榜）：不能凭空冒出来源
    assert context_block(Hotspot()) == ""
    assert context_block(None) == ""


def test_plan_prompt_includes_hotspot() -> None:
    """plan 的 user prompt 里要真有来源——这是整条链路的落点。"""
    from app.pipeline.brief import _user_prompt
    from app.pipeline.research import Hotspot

    hot = Hotspot(source="baidu", title="A股再现涨近日", score="761.57w", rank=1)
    with_hot = _user_prompt("结合“A股再现涨近日”这个热点写一篇", "wechat", 2500, "专业", hot)
    assert "百度热搜" in with_hot and "761.57w" in with_hot, with_hot

    without = _user_prompt("写一篇关于自律的文章", "wechat", 1200, "专业", None)
    assert "热点来源" not in without, "普通选题不该出现热点来源"


def test_generate_api_accepts_hot_fields() -> None:
    """/api/generate 要收得下来源字段，否则前端传了也白传。"""
    from app.main import GenerateIn

    fields = set(GenerateIn.model_fields)
    assert {"hot_source", "hot_title", "hot_score", "hot_rank"} <= fields, sorted(fields)

    # 不传也要能过（手写选题路径）：默认空
    g = GenerateIn(input="写一篇")
    assert g.hot_source == "" and g.hot_rank == 0


def test_frontend_sends_hot_fields() -> None:
    """前端点热榜要记住来源，提交时带上去。

    注意看的是「有没有传」，不是「有没有解构」——以前就是 renderHot 里只取了
    title，source 留在 hotSource 变量里没上传。
    """
    js = read(JS)
    assert "hotPick" in js, "前端没记录选中的热点来源"
    for f in ("hot_source", "hot_title", "hot_score", "hot_rank"):
        assert re.search(rf"{f}:\s*hot\s*\?", js), f"提交 payload 里缺 {f}"

    # 换平台要清掉旧的选择，不然会把小红书的来源贴到百度热搜的稿子上
    assert re.search(r"async function loadHot\(source\) \{\s*\n\s*hotSource = source;\s*\n\s*hotPick = null;", js), \
        "切换热榜平台时没清空已选热点"


def test_chat_write_article_infers_source() -> None:
    """对话路径：write_article 的 input 是模型自己写的，来源常丢，靠代码还原。"""
    from app.agents.supervisor import _infer_hotspot

    results = [
        {"tool": "hot_topics", "source": "baidu",
         "items": [{"rank": 1, "title": "A股再现涨近日", "score": "761.57w"},
                   {"rank": 2, "title": "乌镇峰会今年聊什么", "score": "500w"}]},
    ]
    hot = _infer_hotspot("结合「A股再现涨近日」写一篇公众号长文", results)
    assert hot.source == "baidu" and hot.score == "761.57w" and hot.rank == 1, hot

    # lookup_hot 的结果更直接，优先用它
    hot2 = _infer_hotspot("写篇文章", [
        {"tool": "lookup_hot", "source": "weibo", "hot_title": "某条", "score": "12w"},
    ])
    assert hot2.source == "weibo" and hot2.title == "某条", hot2

    # 跟热点无关的写作：不该硬贴来源
    assert not _infer_hotspot("写一篇关于自律的文章", results)
    assert not _infer_hotspot("随便写", [])


def test_chat_flow_passes_source_to_pipeline() -> None:
    """对话路径端到端：hot_topics → write_article，来源要真的进到 pipeline。

    这是最容易被漏掉的一条：write_article 的 input 是模型自己写的，
    实测它经常只写标题，来源得靠代码从本轮结果里还原出来补上。
    """
    import asyncio

    from app.agents import supervisor
    from app.pipeline import engine as engine_mod

    hot_items = [{"rank": 1, "title": "A股再现涨近日", "score": "761.57w", "link": ""},
                 {"rank": 2, "title": "乌镇峰会今年聊什么", "score": "500w", "link": ""}]

    async def fake_hot(args, llm, keeper, task_id, emit, results):
        return {"tool": "hot_topics", "summary": "百度热搜 top10", "source": "baidu",
                "items": hot_items, "material": "热榜素材……"}

    seen: dict = {}

    async def fake_pipeline(**kw):
        seen.update(kw)
        return {"task_id": "t", "body": "正文", "brief": {"topic": "A股", "title": "A股"},
                "rounds": [], "kept_round": 1, "word_count": 2, "tokens": 1,
                "elapsed_ms": 1, "model": "m", "mocked": True, "style": "auto",
                "structure": "", "final_score": 0.0}

    class FakeLLM:
        class cfg:
            mock = False

        def __init__(self, plans):
            self.plans = plans
            self.n = 0

        async def structured(self, **_kw):
            p = self.plans[min(self.n, len(self.plans) - 1)]
            self.n += 1
            return p

        async def stream(self, **_kw):
            yield "content", "好的。"

    class FakeKeeper:
        def persona_profile(self):
            return ""

        def reflect_hint(self):
            return ""

    async def _noop(_ev):
        return None

    orig_hot, orig_pipe = supervisor.TOOLS["hot_topics"], engine_mod.run_pipeline
    supervisor.TOOLS["hot_topics"] = {"title": "拉平台热榜", "runner": fake_hot}
    engine_mod.run_pipeline = fake_pipeline
    try:
        llm = FakeLLM([{
            "reply": "开工。", "summarize": False,
            "steps": [
                {"tool": "hot_topics", "args": {"source": "baidu"}, "title": "拉百度热搜", "why": "要看热点"},
                {"tool": "write_article",
                 "args": {"input": "结合「A股再现涨近日」写一篇公众号长文", "platform": "wechat", "words": "long"},
                 "title": "写文章", "why": "用户要成稿"},
            ],
        }])
        asyncio.run(supervisor.handle_chat(
            task_id="t1", text="百度热搜第一条给我写成公众号", llm=llm, keeper=FakeKeeper(),
            emit=_noop, session_id="s1",
        ))
    finally:
        supervisor.TOOLS["hot_topics"] = orig_hot
        engine_mod.run_pipeline = orig_pipe

    assert seen, "write_article 没走到 pipeline"
    assert seen.get("hot") and seen["hot"].source == "baidu", seen.get("hot")
    assert "百度热搜" in seen["user_input"], seen["user_input"][:200]
    assert "761.57w" in seen["user_input"], "热度值没传下去"


def test_lookup_hot_returns_source() -> None:
    """lookup_hot 要回传 source/score，write_article 才能接着用。"""
    src = read(os.path.join(ROOT, "app", "agents", "supervisor.py"))
    body = src.split('async def _run_lookup_hot', 1)[1].split('\nasync def', 1)[0]
    assert '"source": source' in body, "lookup_hot 没回传 source"
    assert '"score": score' in body, "lookup_hot 没回传热度值"


if __name__ == "__main__":
    test_query_carries_source_hint()
    test_context_block_has_source_and_score()
    test_plan_prompt_includes_hotspot()
    test_generate_api_accepts_hot_fields()
    test_frontend_sends_hot_fields()
    test_chat_write_article_infers_source()
    test_chat_flow_passes_source_to_pipeline()
    test_lookup_hot_returns_source()
    print("ALL PASS 热点来源：检索带语境 / prompt 有来源与热度 / 接口与前端字段齐 / 对话路径自动还原")
