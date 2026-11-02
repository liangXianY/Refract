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


def test_login_is_password_only() -> None:
    """登录只留「密码登录」；验证码只服务于注册和找回密码。

    这条是需求的红线：曾经有过「验证码登录」tab 和顶栏「设密码」入口，
    结果日常登录要等邮件、密码又能在登录后再补，心智混乱。
    """
    html = read(HTML)
    js = read(JS)

    tabs = set(re.findall(r'data-amtab="([a-z]+)"', html))
    assert tabs == {"pw", "reg"}, f"登录弹窗只该有「登录 / 注册」两个 tab，现在是 {sorted(tabs)}"

    # 三套模式都要在 AM.conf 里有配置：pw（登录）reg（注册）reset（找回密码）
    modes = set(re.findall(r"\n    ([a-z]+):\s*\{ title:", js))
    assert {"pw", "reg", "reset"} <= modes, f"AM.conf 缺模式：{sorted({'pw', 'reg', 'reset'} - modes)}"

    # 找回密码是从登录页跳过去的，必须有个入口按钮
    assert 'id="amAlt"' in html, "找不到「忘记密码？」的入口元素"
    assert "reset" in js.split("alt: [", 1)[1][:200], "忘记密码按钮没有指向 reset 模式"

    # 顶栏不再提供设/改密码，也没有验证码登录的残留
    assert "navPw" not in js and "navPw" not in html, "顶栏还在显示「设密码 / 改密码」"
    assert "/api/auth/verify" not in js, "前端还留着验证码登录接口"
    assert "amOld" not in html and "amOldRow" not in js, "「原密码」那一行该随设密码模式一起删掉"


def test_no_explanatory_fine_print() -> None:
    """弹窗里不要解释性小字：副标题、底部提示、kicker 都删掉了。

    用户的要求很直白——「这些小字不需要」。字段名（邮箱/密码/验证码）已经说明
    一切，再补一句「邮箱 + 密码，不发验证码」只是噪音。留着这条断言，防止以后
    又顺手加回来一段说明文字。
    """
    html = read(HTML)
    js = read(JS)
    for cls in ("am-kicker", "am-sub", "am-tip"):
        assert cls not in html, f"弹窗里又出现了 .{cls} 小字"
        assert cls not in js, f"app.js 又引用了 .{cls} 小字"
    for eid in ("amSub", "amTip"):
        assert eid not in html, f"弹窗里又出现了 #{eid} 小字"
        assert eid not in js, f"app.js 又引用了 #{eid}"


def test_auth_endpoints_match_backend() -> None:
    """前端调的每个 /api/auth/* 后端都得真有——改名了会静默 404。"""
    from app.main import app

    js = read(JS)
    used = set(re.findall(r"postAuth\('(/api/[^']+)'", js))
    assert used, "没从 app.js 解析出登录相关接口"
    routes = {getattr(r, "path", "") for r in app.routes}
    missing = sorted(p for p in used if p not in routes)
    assert not missing, f"前端调了后端没有的接口：{missing}"


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
    test_login_is_password_only()
    test_no_explanatory_fine_print()
    test_auth_endpoints_match_backend()
    test_admin_uses_real_endpoints()
    print("ALL PASS 前端契约：登录只有密码 / 注册与找回密码发码 / 无说明小字 / 接口都对得上")
