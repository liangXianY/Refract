"""会话面板的高度链契约检查。

踩过的坑（2026-11-02）：`.chatlayout` 只写了 `grid-template-columns`，隐式行是 auto，
一个长一点的回答就能把行撑到 2000px+，`.chatwrap` 的 `height:100%` 跟着变成内容高度，
`.chatlog` 于是根本不滚——整块被 `body[data-view=workbench]{overflow:hidden}` 裁掉，
第二条消息只露半截、输入框直接消失。

这类错误纯文本也能兜住：高度链上每一环必须显式写成「可以收缩」。不启浏览器，跑得飞快。
"""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS = os.path.join(ROOT, "web", "styles.css")
JS = os.path.join(ROOT, "web", "app.js")


def read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def load_css() -> str:
    # 注释会和后面的选择器粘在一起，"/* 会话 */.chatwrap" 就匹配不上了，先剥掉
    return re.sub(r"/\*.*?\*/", "", read(CSS), flags=re.S)


def rule_bodies(css: str, selector: str) -> list[str]:
    """取出某个选择器（整条，按逗号分组里精确匹配）对应的所有声明块。"""
    out = []
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        sels = [s.strip() for s in m.group(1).split(",")]
        if selector in sels:
            out.append(m.group(2))
    return out


def test_chat_grid_row_is_bounded() -> None:
    css = load_css()
    bodies = rule_bodies(css, ".chatlayout")
    assert bodies, "styles.css 里找不到 .chatlayout 规则"
    joined = " ".join(bodies)
    rows = re.search(r"grid-template-rows\s*:\s*([^;]+)", joined)
    assert rows, (
        ".chatlayout 没写 grid-template-rows —— 隐式行是 auto，会被消息内容撑高，"
        "导致会话区不滚动、输入框被挤出视口"
    )
    track = rows.group(1).strip()
    assert "minmax(0" in track or track.startswith("1fr") or "1fr" == track, (
        f".chatlayout 的行轨道必须是可收缩的 minmax(0,1fr)（现在是 {track}）"
    )


def test_chatwrap_and_chatlog_can_shrink() -> None:
    css = load_css()
    wrap = " ".join(rule_bodies(css, ".chatwrap"))
    log = " ".join(rule_bodies(css, ".chatlog"))
    assert re.search(r"min-height\s*:\s*0", wrap), ".chatwrap 缺 min-height:0，flex 子项撑不回去"
    assert re.search(r"min-height\s*:\s*0", log), ".chatlog 缺 min-height:0，内容会把自身撑高而不是滚动"
    assert re.search(r"overflow-y\s*:\s*auto", log), ".chatlog 必须 overflow-y:auto，否则长会话没有滚动条"


def test_mobile_panels_allow_body_scroll() -> None:
    """手机端文章/视频面板按自然流式排布，body 必须放开滚动，否则下半屏永远是死的。"""
    css = read(CSS)
    assert 'data-panel="article"' in css and 'data-panel="video"' in css, (
        "styles.css 缺少手机端 data-panel=article/video 的 body 滚动规则"
    )
    js = read(JS)
    assert "dataset.panel" in js, "app.js 没把当前面板写进 body[data-panel]，上面的 CSS 规则不会生效"


if __name__ == "__main__":
    test_chat_grid_row_is_bounded()
    test_chatwrap_and_chatlog_can_shrink()
    test_mobile_panels_allow_body_scroll()
    print("ALL PASS 会话布局：grid 行可收缩 / chatlog 能滚 / 手机端 body 能滚")
