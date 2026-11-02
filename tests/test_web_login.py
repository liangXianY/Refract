"""登录弹窗的 HTML / JS 契约检查。

前端没有单测框架，靠这个脚本兜住最常犯的两类错：
- 在 HTML 里加了个 tab，忘了在 app.js 的 AM.conf 里配它 → 点了没反应
- JS 里 $('amXxx') 引用的元素，HTML 里其实没有 → 运行时 null 报错

不启动浏览器、不发请求，纯文本比对，跑得飞快。
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, "web", "index.html")
JS = os.path.join(ROOT, "web", "app.js")


def read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_tabs_have_config() -> None:
    html = read(HTML)
    js = read(JS)
    tabs = set(re.findall(r'data-amtab="([a-z]+)"', html))
    assert tabs, "HTML 里没找到登录 tab"
    # AM.conf 里每个模式的写法是：  code:  { title: '...',
    modes = set(re.findall(r"\n    ([a-z]+):\s*\{ title:", js))
    assert modes, "没从 app.js 解析出登录模式配置"
    missing = tabs - modes
    assert not missing, f"这些 tab 在 app.js 里没有对应配置：{sorted(missing)}"


def test_am_elements_exist() -> None:
    html = read(HTML)
    js = read(JS)
    have = set(re.findall(r'id="([A-Za-z0-9_-]+)"', html))
    used = set(re.findall(r"\$\('(am[A-Za-z0-9_-]*)'\)", js))
    missing = sorted(used - have)
    assert not missing, f"app.js 引用了 HTML 里不存在的元素：{missing}"


def test_admin_uses_real_endpoints() -> None:
    """后台页面里 fetch 的每个 /api 地址，后端都得真有，不然点一下就是 404。"""
    from app.main import app

    html = read(os.path.join(ROOT, "web", "admin.html"))
    paths = set(re.findall(r"fetch\(`?(/[a-zA-Z0-9/_{}$.-]+)`?", html))
    routes = set()
    for r in app.routes:
        p = getattr(r, "path", "")
        if p.startswith("/api"):
            routes.add(p)

    def matches(tpl: str) -> bool:
        # 把模板里的 ${...} 当成任意一段
        pattern = re.sub(r"\$\{[^}]+\}", "[^/]+", tpl)
        return any(re.fullmatch(pattern, p) for p in routes)

    bad = sorted(p for p in paths if p.startswith("/api") and not matches(p))
    assert not bad, f"后台页面调了后端没有的接口：{bad}"


if __name__ == "__main__":
    test_tabs_have_config()
    test_am_elements_exist()
    test_admin_uses_real_endpoints()
    print("ALL PASS 前端契约：登录 tab / 弹窗元素 / 后台接口都存在")
