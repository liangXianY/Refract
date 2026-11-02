"""配图渲染的契约测试。

线上真出过一次：正文里明明白白写着一行
    ![跨应用用户画像数据拼接示意图](/img/6673e468ad0c/1.png)
图片文件其实好好的（48K、HTTP 200），但两个渲染器都只认 https?:// 开头的图，
于是整行被当普通段落输出，页面上就是一串字面文字；下载的 md 里是站内相对路径，
换台机器打开也是裂图。

这里钉住三件事：
1. 两个渲染器都认站内绝对路径（/img/...），也认 http(s)
2. 图没生成出来时的 prompt 占位渲染成占位卡，不是把提示词糊在正文里
3. 下载的 md 里图片是绝对 URL
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

JS = os.path.join(ROOT, "web", "app.js")

MD = """先给结论。

![跨应用用户画像数据拼接示意图](/img/abc123/1.png)

![联网搜到的现场图](https://example.com/a.jpg)

![手机麦克风与数据流示意图](prompt: 一部智能手机平放在木桌上，冷蓝色调)
"""


def read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_server_renderer_handles_site_relative_image() -> None:
    from app.pipeline.renderer import md_to_html

    html = md_to_html(MD)
    assert '<img src="/img/abc123/1.png"' in html, "站内配图没渲染成 img"
    assert "![跨应用用户画像" not in html, "图片语法被当成了普通文字"
    assert '<img src="https://example.com/a.jpg"' in html, "外链图正常渲染这条不能坏"
    # prompt 占位走占位卡，别把提示词当正文
    assert "imgslot" in html and "配图位" in html, "占位没渲染成占位卡"
    assert "![手机麦克风" not in html


def test_frontend_renderer_supports_both() -> None:
    """前端 renderMd 是流式正文用的，必须和后端同口径。"""
    js = read(JS)
    assert "mSlot" in js, "前端没处理 prompt 占位，正文里会露出一串提示词"
    assert "mdslot" in js, "占位卡样式没接上"
    # 正则里要有站内路径的分支
    m = re.search(r"const mImg = line\.trim\(\)\.match\((/.*?/)\);", js)
    assert m, "找不到图片行的正则"
    pattern = m.group(1)
    assert "https?" in pattern, "正则丢了外链分支"
    assert "\\/" in pattern or "|/" in pattern, f"正则不认站内路径：{pattern}"


def test_placeholder_styled_in_css() -> None:
    css = read(os.path.join(ROOT, "web", "styles.css"))
    assert ".md figure.mdslot" in css, "占位卡没样式，会跟正文糊在一起"


def test_downloaded_md_uses_absolute_image_url() -> None:
    """下载的 md 里的图必须是绝对 URL，否则换台机器就是裂图。"""
    from fastapi.testclient import TestClient

    from app import main as m
    from app.pipeline import runs

    tid = "mdimg001"
    record = {"task_id": tid, "kind": "article", "body": MD, "brief": {"topic": "测试配图"}}
    orig_get = runs.get
    # 只替身读记录，不往真实 data/runs 里写测试垃圾
    runs.get = lambda _dir, _tid: record if _tid == tid else None
    try:
        client = TestClient(m.app)
        r = client.get(f"/api/articles/{tid}/md")
    finally:
        runs.get = orig_get

    assert r.status_code == 200, r.text
    text = r.text
    assert "](http://testserver/img/abc123/1.png)" in text, text[:400]
    # 外链别被误改
    assert "](https://example.com/a.jpg)" in text


if __name__ == "__main__":
    test_server_renderer_handles_site_relative_image()
    test_frontend_renderer_supports_both()
    test_placeholder_styled_in_css()
    test_downloaded_md_uses_absolute_image_url()
    print("ALL PASS 配图渲染：站内路径 / 外链 / 占位卡 / 下载 md 绝对 URL")
