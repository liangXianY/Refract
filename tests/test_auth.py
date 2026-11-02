"""账号体系：验证码、登录态、会话归属。

不碰网络（没配 SMTP 时走 AUTH_DEV_CODE，验证码直接返回），只测逻辑与隔离。
"""

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.pop("SMTP_USER", None)
os.environ.pop("SMTP_PASS", None)
os.environ["AUTH_DEV_CODE"] = "1"

from app import auth as auth_mod  # noqa: E402
from app.auth import AuthError, AuthStore  # noqa: E402


def fresh() -> AuthStore:
    return AuthStore(Path(tempfile.mkdtemp()))


def test_email_format() -> None:
    for bad in ("", "abc", "a@b", "a b@c.com", "@qq.com"):
        try:
            auth_mod.normalize_email(bad)
        except AuthError:
            continue
        raise AssertionError(f"这个邮箱应该被判非法：{bad!r}")
    assert auth_mod.normalize_email("  XiaoLiang@QQ.com ") == "xiaoliang@qq.com"


def test_code_flow() -> None:
    st = fresh()
    out = st.issue_code("xiaoliang@qq.com")
    code = out["dev_code"]
    assert len(code) == 6 and code.isdigit()

    # 60 秒内重发要被拒
    try:
        st.issue_code("xiaoliang@qq.com")
    except AuthError as exc:
        assert "秒后再试" in str(exc)
    else:
        raise AssertionError("重发应该被限流")

    # 错码：要报错并计次，不能放行
    try:
        st.verify("xiaoliang@qq.com", "000000" if code != "000000" else "111111")
    except AuthError as exc:
        assert "不对" in str(exc)
    else:
        raise AssertionError("错码不该通过")

    token, user = st.verify("xiaoliang@qq.com", code)
    assert token and user["email"] == "xiaoliang@qq.com" and user["created"] is True

    # 验证码一次性
    try:
        st.verify("xiaoliang@qq.com", code)
    except AuthError as exc:
        assert "先点" in str(exc) or "过期" in str(exc)
    else:
        raise AssertionError("验证码只能用一次")

    # 登录态
    me = st.user_by_token(token)
    assert me and me["email"] == "xiaoliang@qq.com"
    st.logout(token)
    assert st.user_by_token(token) is None, "退出后 token 必须立刻失效"

    # 同一个邮箱再登录 = 同一个账号，不是新注册
    out2 = st.issue_code("xiaoliang@qq.com")
    # 手动清掉限流记录，模拟「过了一分钟」
    with st._db() as c:
        c.execute("DELETE FROM code_sends WHERE email=?", ("xiaoliang@qq.com",))
        c.execute("DELETE FROM email_codes WHERE email=?", ("xiaoliang@qq.com",))
    out2 = st.issue_code("xiaoliang@qq.com")
    _, user2 = st.verify("xiaoliang@qq.com", out2["dev_code"])
    assert user2["created"] is False and user2["id"] == user["id"], "老邮箱不该变成新账号"


def test_conversations_isolated() -> None:
    st = fresh()
    a_token, a = st.verify("a@qq.com", st.issue_code("a@qq.com")["dev_code"])
    b_token, b = st.verify("b@qq.com", st.issue_code("b@qq.com")["dev_code"])

    c1 = st.create_conversation(a["id"])
    st.touch_conversation(c1["id"], "小红书怎么写")
    c2 = st.create_conversation(a["id"])
    st.touch_conversation(c2["id"], "RAG 科普")

    mine = st.list_conversations(a["id"])
    assert len(mine) == 2 and mine[0]["id"] == c2["id"], "要按最近活跃倒序"
    assert mine[0]["title"] == "RAG 科普"

    assert st.list_conversations(b["id"]) == [], "别人的会话一条都不能出现"

    # 删别人的会话：必须失败
    assert st.delete_conversation(c1["id"], b["id"]) is False
    assert len(st.list_conversations(a["id"])) == 2
    assert st.delete_conversation(c1["id"], a["id"]) is True
    assert len(st.list_conversations(a["id"])) == 1

    # 标题只取第一次（后续 touch 不改标题）
    st.touch_conversation(c2["id"], "换个标题试试")
    assert st.list_conversations(a["id"])[0]["title"] == "RAG 科普"


if __name__ == "__main__":
    test_email_format()
    test_code_flow()
    test_conversations_isolated()
    print("ALL PASS 账号体系：验证码限流 / 一次性 / 账号复用 / 会话隔离")
