"""账号体系：验证码、密码、登录态、会话归属。

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


def test_password_hash() -> None:
    h = auth_mod.hash_password("helloworld123")
    assert h.startswith("scrypt$"), "哈希串要自带算法标记，将来才好换算法"
    assert auth_mod.verify_password("helloworld123", h)
    assert not auth_mod.verify_password("helloworld124", h)
    assert not auth_mod.verify_password("helloworld123", None), "没设过密码的账号不能登"
    assert not auth_mod.verify_password("", h)
    # 两个相同密码的哈希不能一样（盐是随机的）
    assert h != auth_mod.hash_password("helloworld123")

    for bad in ("1234567", "", "   "):
        try:
            auth_mod.hash_password(bad)
        except AuthError:
            continue
        raise AssertionError(f"这个密码应该被拒：{bad!r}")


def test_register_and_login() -> None:
    st = fresh()
    code = st.issue_code("new@qq.com")["dev_code"]
    token, user = st.register("new@qq.com", code, "helloworld123")
    assert user["created"] is True
    assert st.user_by_token(token)["email"] == "new@qq.com"
    assert st.user_by_id(user["id"])["has_password"] == 1

    # 密码登录：对
    t2, u2 = st.login("new@qq.com", "helloworld123")
    assert u2["id"] == user["id"] and st.user_by_token(t2)

    # 密码登录：错
    try:
        st.login("new@qq.com", "wrongpassword")
    except AuthError as exc:
        assert "不对" in str(exc)
    else:
        raise AssertionError("错误密码不该通过")

    # 没注册的邮箱
    try:
        st.login("nobody@qq.com", "helloworld123")
    except AuthError as exc:
        assert "还没注册" in str(exc)
    else:
        raise AssertionError("没注册的邮箱不该登进来")

    # 同一个邮箱重复注册：要挡住（已经有密码了）
    code2 = st.issue_code("new@qq.com")["dev_code"]
    try:
        st.register("new@qq.com", code2, "anotherpw456")
    except AuthError as exc:
        assert "已经注册" in str(exc)
    else:
        raise AssertionError("重复注册不该通过")


def test_set_password() -> None:
    st = fresh()
    # 验证码进来的老账号（没密码）：直接设，不用原密码
    code_token, user = st.verify("old@qq.com", st.issue_code("old@qq.com")["dev_code"])
    uid = user["id"]
    assert st.user_by_id(uid)["has_password"] == 0
    st.set_password(uid, "newpassword1", token=code_token)
    assert st.login("old@qq.com", "newpassword1")

    # 之后是「密码登录」的会话：改密码必须先过原密码，否则拿到浏览器就能抢号
    pw_token, _ = st.login("old@qq.com", "newpassword1")
    try:
        st.set_password(uid, "anotherpw999", old_raw="瞎猜的", token=pw_token)
    except AuthError as exc:
        assert "原密码不对" in str(exc)
    else:
        raise AssertionError("不验原密码就改密码太危险")
    st.set_password(uid, "anotherpw999", old_raw="newpassword1", token=pw_token)
    assert st.login("old@qq.com", "anotherpw999")

    # 但「验证码登录」的会话能免原密码重设——这是忘了密码的唯一恢复路径
    code_token2, _ = st.verify("old@qq.com", st.issue_code("old@qq.com")["dev_code"])
    st.set_password(uid, "recoveredpw01", token=code_token2)
    assert st.login("old@qq.com", "recoveredpw01")


def test_code_login_still_works_with_password() -> None:
    """设了密码之后，验证码登录仍然要能用——这是找回密码的唯一入口。"""
    st = fresh()
    st.register("both@qq.com", st.issue_code("both@qq.com")["dev_code"], "helloworld123")
    code = st.issue_code("both@qq.com")["dev_code"]
    token, user = st.verify("both@qq.com", code)
    assert user["created"] is False and token
    # 用验证码进来后把密码改掉，旧密码立刻失效
    st.set_password(user["id"], "brandnewpw01", token=token)
    try:
        st.login("both@qq.com", "helloworld123")
    except AuthError:
        pass
    else:
        raise AssertionError("改完密码旧密码还能登，等于没改")


if __name__ == "__main__":
    test_email_format()
    test_code_flow()
    test_conversations_isolated()
    test_password_hash()
    test_register_and_login()
    test_set_password()
    test_code_login_still_works_with_password()
    print("ALL PASS 账号体系：验证码限流 / 一次性 / 密码注册登录 / 改密 / 会话隔离")
