"""Refract MCP 服务：把项目能力暴露给外部 AI 客户端。

为什么是 MCP 而不是再加一套 HTTP 接口：
MCP 让 Claude Desktop / Cursor / 其他 agent 能直接调这些能力，不用开网页点按钮。
这是「项目可被外部 agent 复用」的证明，也是简历上能讲的一块。

设计：
- 独立入口，不动现有 FastAPI。`python mcp_server.py` 起 stdio 服务。
- tool 按「能力面」切（抓取/写作/视频/记忆），不按内部函数切。
- 直接 import app 里的函数，不走 HTTP，少一层开销。

用法（Claude Desktop 配置示例）：
{
  "mcpServers": {
    "refract": {
      "command": "<项目>/.venv/Scripts/python.exe",
      "args": ["<项目>/mcp_server.py"],
      "env": {"DEEPSEEK_API_KEY": "..."}
    }
  }
}
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# 让 mcp_server.py 能 import app（无论从哪个目录启动）
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp.server.mcpserver import MCPServer  # noqa: E402

from app.config import settings  # noqa: E402
from app.llm import LLMClient  # noqa: E402
from app.pipeline.fetch import FetchError, fetch_url, to_prompt_material  # noqa: E402
from app.pipeline.memory import init_memory  # noqa: E402
from app.pipeline.renderer import render_artifact  # noqa: E402
from app.pipeline.storyboard import shots_to_data, storyboard  # noqa: E402
from app.pipeline.video import render_video  # noqa: E402
from app.schemas import ContentBrief  # noqa: E402

mcp = MCPServer(
    name="refract",
    instructions=(
        "Refract 是内容生产流水线：给一个链接或一句话，产出公众号文案、分镜、动画视频页。"
        "能力分四组：① 取素材——fetch_url 抓网页、hot_topics 拉平台热榜、web_search 联网搜、"
        "lookup_hot 读热点详情；② 写稿——compose_article；③ 视频——plan_video 出分镜、"
        "render_video 出片；④ 记忆——memory_write 存、memory_recall 读。"
    ),
)

from app.config import registry  # noqa: E402
llm = LLMClient(settings, registry)
memory = init_memory(settings.data_dir / "memory.db")


# ---------------- 1. 抓取 ----------------

@mcp.tool()
async def fetch_url_tool(url: str) -> str:
    """抓一个网页，抽正文。返回标题、字数、正文前 1500 字。

    抓不到会明确说原因（JS 渲染 / 需要登录 / 超时），不会编造内容。
    """
    try:
        page = await fetch_url(url)
    except FetchError as exc:
        return f"抓取失败：{exc}"
    body = to_prompt_material(page, limit=1500)
    return f"抓到了 {page.word_count} 字。\n\n{body}"


# ---------------- 1b. 平台热榜 + 联网搜索 + 热点详情 ----------------

@mcp.tool()
async def hot_topics(source: str = "rednote", limit: int = 10) -> str:
    """拉平台实时热榜（小红书/微博/知乎/抖音/B站/百度）。

    source: rednote | weibo | zhihu | douyin | bilibili | baidu
    返回排名、标题、热度。这是「了解最近什么火」的入口，只给标题热度，
    想深入某条热点用 lookup_hot 读详情。
    """
    from app.pipeline.hotlist import SOURCES, HotListError, fetch_hot

    try:
        hl = await fetch_hot(source, limit=limit)
    except HotListError as exc:
        return f"热榜获取失败：{exc}"
    lines = [f"【{hl.source_label}】实时热榜："]
    for it in hl.items:
        seg = f"{it['rank']}. {it['title']}"
        if it["score"]:
            seg += f"（热度 {it['score']}）"
        lines.append(seg)
    return "\n".join(lines)


@mcp.tool()
async def web_search(query: str, max_results: int = 5) -> str:
    """联网搜索，返回带来源链接的最新资料。

    适合写时效性/热点/对比类内容时先取素材，也适合「查一下某件事」。
    """
    from app.pipeline.search import SearchError, search

    try:
        res = await search(query, max_results=max_results)
    except SearchError as exc:
        return f"搜索失败：{exc}"
    lines = [f"搜索「{query}」共 {len(res.items)} 条：", ""]
    for i, r in enumerate(res.items, 1):
        lines.append(f"[{i}] {r['title']}")
        lines.append(f"    来源：{r['url']}")
        lines.append(f"    {r['content'][:300]}")
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
async def lookup_hot(title: str) -> str:
    """针对某条热点标题搜正文，理解这条热点到底讲什么。

    热榜只给标题，要写稿或深入解释某条热点，先用这个读详情。
    """
    from app.pipeline.search import SearchError, search

    try:
        res = await search(title, max_results=5)
    except SearchError as exc:
        return f"详情搜索失败：{exc}"
    lines = [f"「{title}」相关报道 {len(res.items)} 篇：", ""]
    for i, r in enumerate(res.items, 1):
        lines.append(f"[{i}] {r['title']}（{r['url']}）")
        lines.append(f"    {r['content'][:400]}")
        lines.append("")
    return "\n".join(lines)


# ---------------- 2. 写文案 ----------------

@mcp.tool()
async def compose_article(
    input: str = "",
    url: str = "",
    platform: str = "wechat",
    words: str = "medium",
) -> str:
    """写一篇公众号/小红书文章。

    input 和 url 至少给一个。给了 url 会先抓正文当素材。
    platform: wechat | xiaohongshu
    words: short | medium | long
    """
    if not input.strip() and not url.strip():
        return "错误：input 和 url 至少给一个"

    from app.pipeline.brief import plan
    from app.pipeline.compose import compose_stream

    material = input
    if url.strip():
        try:
            page = await fetch_url(url)
        except FetchError as exc:
            return f"链接抓取失败：{exc}"
        fetched = to_prompt_material(page)
        material = f"{input}\n\n链接原文：\n{fetched}" if input.strip() else fetched

    brief = await plan(llm, material, platform, words)
    parts: list[str] = []
    async for kind, text in compose_stream(llm, brief, None, persona=memory.style_profile()):
        if kind == "content":
            parts.append(text)
    body = "".join(parts)
    return f"# {brief.topic}\n\n{body}"


# ---------------- 3. 分镜 ----------------

@mcp.tool()
async def plan_video(input: str = "", platform: str = "video") -> str:
    """把一段需求拆成分镜表（镜头/画面/时长）。不渲染，只出方案。"""
    from app.pipeline.brief import plan

    brief = await plan(llm, input or "内容介绍", platform, "medium")
    shots = storyboard(brief, "")
    lines = [f"主题：{brief.topic}", f"共 {len(shots)} 个镜头：", ""]
    for s in shots:
        lines.append(f"{s.shot_id} [{s.task}] {s.screen_text}（{s.duration}s）")
        if s.narration:
            lines.append(f"    画面：{s.narration[:60]}")
    return "\n".join(lines)


# ---------------- 4. 出视频页 ----------------

@mcp.tool()
async def render_video_page(input: str = "", style: str = "hud") -> str:
    """生成一个可播放的动画视频页（HTML 路径）。

    style: hud | line | pixel | geo
    返回：产物文件路径，可直接在浏览器打开。
    """
    import uuid

    from app.pipeline.brief import plan

    brief = await plan(llm, input or "内容介绍", "video", "medium")
    aid = uuid.uuid4().hex[:12]
    style_id = style if style in ("hud", "line", "pixel", "geo") else "hud"
    path = Path(settings.data_dir) / "videos" / f"{aid}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_video(brief, "", style_id, f"{style_id} 风格 · MCP 生成"),
        encoding="utf-8",
    )
    return f"已生成：{path}\n风格：{style_id}\n用浏览器打开即可播放。"


# ---------------- 5. 记忆写入 ----------------

@mcp.tool()
def memory_write(key: str, value: str, kind: str = "general") -> str:
    """写一条项目记忆。

    kind: persona（人设）/ banned（禁用词，逗号分隔）/ tone（语气）/ audience（读者）/ topic（选题）
    这些会在下次写稿时自动进 prompt。
    """
    memory.set(key, value, kind)
    return f"已记住 {key}（{kind}）= {value[:50]}"


# ---------------- 6. 记忆读取 ----------------

@mcp.tool()
def memory_recall(kind: str = "") -> str:
    """读项目记忆。不传 kind 返回全部 + 人设卡文本。"""
    items = memory.list_items(kind or None)
    if not items:
        return "记忆库是空的。用 memory_write 存点东西（人设、禁用词、语气）。"
    lines = [f"共 {len(items)} 条："]
    for i in items:
        lines.append(f"- [{i.kind}] {i.key}: {i.value[:80]}")
    profile = memory.style_profile()
    if profile:
        lines.append(profile)
    return "\n".join(lines)


# ---------------- 7. 结构表现（长期记忆） ----------------

@mcp.tool()
def memory_structures() -> str:
    """看历史表现最好的内容结构（数据回流结果）。"""
    tops = memory.top_structures()
    if not tops:
        return "还没有结构表现记录。生成几篇稿子后会自动积累。"
    return "\n".join(
        f"- {t['structure']}：平均 {t['avg']:.1f} 分（{t['runs']} 次）" for t in tops
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
