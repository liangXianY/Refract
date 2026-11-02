"""管理后台：账号视图 / 会话内容查看 / 按账号改配额。

跑的是真实 HTTP（FastAPI TestClient），不是直接调函数——接口鉴权、
状态码这些只有走 HTTP 才测得出来。
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["AUTH_DEV_CODE"] = "1"
os.environ["ADMIN_PASSWORD"] = "test-admin-pw"
os.environ["LLM_MOCK"] = "1"
os.environ.pop("SMTP_USER", None)
os.environ.pop("SMTP_PASS", None)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app, quota  # noqa: E402
from app.session_history import get_history  # noqa: E402

KEY = {"x-admin-key": "test-admin-pw"}
BAD = {"x-admin-key": "wrong"}


def test_admin_requires_key() -> None:
    c = TestClient(app)
    for path in ("/api/admin/users", "/api/admin/log", "/api/admin/conversation/x",
                 "/api/admin/user/x/conversations"):
        r = c.get(path, headers=BAD)
        assert r.status_code in (401, 403), f"{path} 没挡住错误口令：{r.status_code}"
    # 配了口令但不带 / 带错 → 401（403 是「压根没配管理员密码」那种情况）
    r = c.get("/api/admin/users")
    assert r.status_code == 401, f"不带口令也该被挡，实际 {r.status_code}"


def test_users_view_by_account() -> None:
    c = TestClient(app)
    code = c.post("/api/auth/send-code", json={"email": "boss@qq.com"}).json()["dev_code"]
    r = c.post("/api/auth/register", json={
        "email": "boss@qq.com", "code": code, "password": "helloworld123",
    })
    assert r.status_code == 200, r.text
    uid = r.json()["user"]["id"]

    users = c.get("/api/admin/users", headers=KEY).json()["users"]
    me = [u for u in users if u["user_id"] == uid]
    assert len(me) == 1, "注册了就该出现在后台，不管有没有产生用量"
    me = me[0]
    assert me["email"] == "boss@qq.com"
    assert me["registered"] is True and me["has_password"] is True
    assert me["limits"] == {"chat": 10, "video": 1, "article": 3}, "没单独设过就用全局默认"


def test_conversation_view() -> None:
    c = TestClient(app)
    code = c.post("/api/auth/send-code", json={"email": "talker@qq.com"}).json()["dev_code"]
    c.post("/api/auth/register", json={
        "email": "talker@qq.com", "code": code, "password": "helloworld123",
    })
    uid = c.get("/api/auth/me").json()["user"]["id"]

    cid = c.post("/api/conversations", json={"title": "写个标题"}).json()["conversation"]["id"]
    h = get_history()
    h.start_turn(cid, "帮我起个小红书标题")
    h.append_event(cid, {"type": "delta", "kind": "content", "text": "给你三个：…"})

    # 列表
    r = c.get(f"/api/admin/user/{uid}/conversations", headers=KEY)
    assert r.status_code == 200
    convs = r.json()["conversations"]
    assert convs and convs[0]["id"] == cid

    # 内容：管理员能跨用户看完整问答
    r = c.get(f"/api/admin/conversation/{cid}", headers=KEY)
    assert r.status_code == 200
    d = r.json()
    assert d["email"] == "talker@qq.com"
    blob = str(d["messages"])
    assert "帮我起个小红书标题" in blob and "给你三个" in blob, "问答内容都要能看到"

    # 不存在的会话
    assert c.get("/api/admin/conversation/c_nope", headers=KEY).status_code == 404

    # 普通用户不能拿管理员接口看别人的会话
    other = TestClient(app)
    code2 = other.post("/api/auth/send-code", json={"email": "other@qq.com"}).json()["dev_code"]
    other.post("/api/auth/verify", json={"email": "other@qq.com", "code": code2})
    assert other.get(f"/api/session/{cid}").status_code == 404, "别人的会话必须 404"


def test_set_quota_per_account() -> None:
    c = TestClient(app)
    code = c.post("/api/auth/send-code", json={"email": "quota@qq.com"}).json()["dev_code"]
    c.post("/api/auth/register", json={
        "email": "quota@qq.com", "code": code, "password": "helloworld123",
    })
    uid = c.get("/api/auth/me").json()["user"]["id"]

    # 单独给他 2 次对话
    r = c.post("/api/admin/quota", headers=KEY,
               json={"session_id": uid, "limits": {"chat": 2, "video": 9}})
    assert r.status_code == 200, r.text
    u = quota.get(uid)
    assert quota.limit_of(u, "chat") == 2, "自定义上限要真的生效"
    assert quota.limit_of(u, "video") == 9
    assert quota.limit_of(u, "article") == 3, "没给的项继续用全局默认"

    ok, _ = quota.can(uid, "chat")
    assert ok
    quota.consume(uid, "chat", "t1", real_ip="1.2.3.4")
    quota.consume(uid, "chat", "t2", real_ip="1.2.3.4")
    ok, reason = quota.can(uid, "chat")
    assert not ok and "2" in reason, "用到 2 次就该到顶"

    # 后台视图里能看出来是自定义的，且记下了真实 IP
    users = c.get("/api/admin/users", headers=KEY).json()["users"]
    me = next(x for x in users if x["user_id"] == uid)
    assert me["custom_limits"] is True
    assert me["last_ip"] == "1.2.3.4", "账号身份下也要能看到从哪个 IP 来的"

    # 传空 = 取消自定义，回到默认
    c.post("/api/admin/quota", headers=KEY, json={"session_id": uid, "limits": {}})
    assert quota.get(uid).limits is None
    assert quota.limit_of(quota.get(uid), "chat") == 10

    # 重置已用量（上限不动）
    quota.reset(uid)
    assert quota.get(uid).used["chat"] == 0
    assert quota.limit_of(quota.get(uid), "chat") == 10

    # 设不限
    c.post("/api/admin/unlimited", headers=KEY, json={"session_id": uid, "unlimited": True})
    assert quota.can(uid, "chat")[0] is True


def test_anonymous_usage_still_listed() -> None:
    """没注册就用了的（老数据 / 脚本调用）不能凭空从后台消失。"""
    c = TestClient(app)
    quota.consume("9.9.9.9", "chat", "legacy-task", real_ip="9.9.9.9")
    users = c.get("/api/admin/users", headers=KEY).json()["users"]
    anon = [u for u in users if not u["registered"] and u["last_ip"] == "9.9.9.9"]
    assert anon, "匿名用量记录要能在后台看到"
    assert anon[0]["email"] == "" and anon[0]["used"]["chat"] == 1


if __name__ == "__main__":
    test_admin_requires_key()
    test_users_view_by_account()
    test_conversation_view()
    test_set_quota_per_account()
    test_anonymous_usage_still_listed()
    print("ALL PASS 管理后台：账号视图 / 会话内容 / 按账号改额度 / 鉴权")
