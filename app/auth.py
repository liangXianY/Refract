"""账号体系：邮箱验证码注册 / 登录，密码登录，会话归属。

为什么改成账号
--------------
原来用 IP 派生 session_id 认人，问题一堆：换 Wi-Fi 就换身份（历史丢、长期记忆断）、
同一间办公室/同一个 NAT 后面的人互相串、手机和电脑不同步。用户看到的现象就是
「有时候进主页面、有时候进空白聊天页」——因为判断「有没有历史」靠的是本机
localStorage 里那个 IP 派生的 id，和服务端记的对不上。

账号之后：用户 → 会话列表（可多条）→ 每条会话的对话历史。刷新、换设备、
换网络都跟着账号走。

登录方式
--------
- **注册**：邮箱 + 验证码 + 密码。验证码证明邮箱是你的，密码是以后日常登录用的
- **登录**：邮箱 + 密码
- **验证码直接登录**：老账号、或忘了密码时用——邮箱能收信就能进，进去再改密码

密码为什么用 scrypt 而不是 bcrypt/argon2：标准库自带（hashlib.scrypt，
Python 3.6+），不用给这个项目再加一个 C 扩展依赖。参数 n=2**14 是
OWASP 给 scrypt 的下限建议。盐每账号随机 16 字节，和哈希一起存在同一列里。

存哪
----
SQLite（标准库 sqlite3，零依赖），`data/auth.db`：
- users          账号（唯一 identifier 就是邮箱）
- email_codes    验证码（只有最新一条有效，5 分钟）
- sessions       登录态（随机 token，30 天）
- conversations  会话（id 用短 hex，历史文件仍走 SessionHistory）

为什么不用 JWT：要能"退出登录即失效"，服务端可控。个人部署的会话量很小，
一张表足够。
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
import smtplib
import sqlite3
import threading
import time
from contextlib import contextmanager
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Iterator

CODE_TTL = 300          # 验证码 5 分钟有效
CODE_RESEND = 60        # 同一邮箱 60 秒内只能发一次
CODE_HOURLY = 5         # 同一邮箱每小时最多 5 封
CODE_TRIES = 5          # 同一个码最多试 5 次，错了作废
TOKEN_TTL = 30 * 86400  # 登录态 30 天

# 密码
PW_MIN = 8
PW_MAX = 64
# scrypt 成本参数：n 是 OWASP 建议下限，单账号哈希约 50~80ms（故意慢，抗爆破）
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1

COOKIE_NAME = "refract_auth"

_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    created_at    REAL NOT NULL,
    last_login    REAL,
    password_hash TEXT,
    password_at   REAL
);
CREATE TABLE IF NOT EXISTS email_codes (
    email       TEXT PRIMARY KEY,
    code        TEXT NOT NULL,
    expires_at  REAL NOT NULL,
    tries       INTEGER NOT NULL DEFAULT 0,
    sent_at     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS code_sends (
    email       TEXT NOT NULL,
    ts          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_send_email ON code_sends(email, ts);
CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_sess_user ON sessions(user_id);
"""


class AuthError(Exception):
    """带用户可读原因的鉴权/发码失败。路由层直接把它当 400 消息返回。"""


def normalize_email(raw: str) -> str:
    e = (raw or "").strip().lower()
    if not _EMAIL_RE.match(e):
        raise AuthError("邮箱格式不对")
    return e


def _smtp_conf() -> dict:
    """每次现读环境变量：改 .env 重启即生效，不用重启两次。"""
    return {
        "host": (os.environ.get("SMTP_HOST") or "smtp.qq.com").strip(),
        "port": int(os.environ.get("SMTP_PORT") or 465),
        "user": (os.environ.get("SMTP_USER") or "").strip(),
        "password": (os.environ.get("SMTP_PASS") or "").strip(),
        "sender": (os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER") or "").strip(),
    }


def mail_configured() -> bool:
    c = _smtp_conf()
    return bool(c["user"] and c["password"])


# ---------------- 密码 ----------------


def check_password_rules(raw: str) -> str:
    """校验密码格式，返回清洗后的密码；不合规则抛 AuthError。"""
    pw = (raw or "").strip()
    if len(pw) < PW_MIN:
        raise AuthError(f"密码至少 {PW_MIN} 位")
    if len(pw) > PW_MAX:
        raise AuthError(f"密码最多 {PW_MAX} 位")
    return pw


def hash_password(raw: str) -> str:
    """scrypt + 随机盐。存的格式：scrypt$n$r$p$<salt_b64>$<hash_b64>。

    把参数也存进去，将来想提高成本时能认出旧哈希、按需重算。
    """
    pw = check_password_rules(raw)
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(
        pw.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32
    )
    return "scrypt$%d$%d$%d$%s$%s" % (
        _SCRYPT_N,
        _SCRYPT_R,
        _SCRYPT_P,
        base64.b64encode(salt).decode(),
        base64.b64encode(dk).decode(),
    )


def verify_password(raw: str, stored: str | None) -> bool:
    """校验密码。stored 为空（没设过密码）恒 False。"""
    if not stored or not raw:
        return False
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return False
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = base64.b64decode(parts[4])
        want = base64.b64decode(parts[5])
    except (ValueError, TypeError):
        return False
    try:
        got = hashlib.scrypt(raw.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(want))
    except ValueError:
        return False
    return secrets.compare_digest(got, want)


class AuthStore:
    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / "auth.db"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._db() as c:
            c.executescript(_SCHEMA)
            self._migrate(c)

    def _migrate(self, c: sqlite3.Connection) -> None:
        """给老库补列。CREATE TABLE IF NOT EXISTS 对已存在的表什么都不做，
        加字段只能靠 ALTER——上线前库里已经有账号，不能重建。"""
        for table, cols in (
            ("users", (("password_hash", "TEXT"), ("password_at", "REAL"))),
            ("sessions", (("via", "TEXT"),)),
        ):
            have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
            for col, decl in cols:
                if col not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    def _conn(self) -> sqlite3.Connection:
        # 每次新开连接：发信、抽事实都在线程池里跑，共享连接会踩 "SQLite objects
        # created in a thread can only be used in that same thread"
        c = sqlite3.connect(self._path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        """开连接 → 事务 → 一定关。sqlite3 的 `with conn` 只提交不关连接，
        每次请求攒一个没关的句柄，跑两天就 "too many open files"。"""
        c = self._conn()
        try:
            with c:
                yield c
        finally:
            c.close()

    # ---------------- 验证码 ----------------

    def issue_code(self, raw_email: str, ip: str = "") -> dict:
        """生成并发送验证码。返回 {email, ttl, dev_code?}。"""
        email = normalize_email(raw_email)
        now = time.time()
        with self._lock, self._db() as c:
            row = c.execute("SELECT sent_at FROM email_codes WHERE email=?", (email,)).fetchone()
            if row and now - row["sent_at"] < CODE_RESEND:
                wait = int(CODE_RESEND - (now - row["sent_at"])) + 1
                raise AuthError(f"刚发过，{wait} 秒后再试")
            # 每小时上限：email_codes 是覆盖写（一个邮箱一行），数不出次数，
            # 所以单独记一张流水表 code_sends
            hour_ago = now - 3600
            recent = c.execute(
                "SELECT COUNT(*) n FROM code_sends WHERE email=? AND ts>?", (email, hour_ago)
            ).fetchone()["n"]
            if recent >= CODE_HOURLY:
                raise AuthError("这个邮箱一小时内请求太多次了，过会儿再试")
            c.execute("INSERT INTO code_sends(email, ts) VALUES(?,?)", (email, now))
            c.execute("DELETE FROM code_sends WHERE ts < ?", (now - 7 * 86400,))

            code = f"{secrets.randbelow(1_000_000):06d}"
            c.execute(
                "INSERT INTO email_codes(email, code, expires_at, tries, sent_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(email) DO UPDATE SET code=excluded.code, expires_at=excluded.expires_at, "
                "tries=0, sent_at=excluded.sent_at",
                (email, code, now + CODE_TTL, 0, now),
            )
            # 顺手清掉过期的旧码，表不会无限长
            c.execute("DELETE FROM email_codes WHERE expires_at < ?", (now - 86400,))

        if not mail_configured():
            # 本地开发/没配 SMTP：不静默失败，也不假装发出去了
            if os.environ.get("AUTH_DEV_CODE", "0") in ("1", "true"):
                return {"email": email, "ttl": CODE_TTL, "dev_code": code}
            raise AuthError("服务器还没配发件邮箱（SMTP_USER / SMTP_PASS），暂时发不了验证码")

        self._send(email, code)
        return {"email": email, "ttl": CODE_TTL}

    def _send(self, email: str, code: str) -> None:
        conf = _smtp_conf()
        msg = EmailMessage()
        msg["Subject"] = f"Refract 登录验证码：{code}"
        msg["From"] = formataddr(("Refract", conf["sender"]))
        msg["To"] = email
        msg.set_content(
            f"你的 Refract 登录验证码是：{code}\n\n"
            f"{CODE_TTL // 60} 分钟内有效，只能用一次。\n"
            f"不是你本人操作的话，忽略这封邮件就行。\n"
        )
        msg.add_alternative(
            f"""<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;
color:#14161a;line-height:1.6;max-width:420px">
  <p style="font-size:13px;letter-spacing:.14em;color:#6b7178;margin:0 0 6px">REFRACT</p>
  <p style="margin:0 0 18px;font-size:15px">登录验证码</p>
  <p style="font:700 34px/1.2 ui-monospace,Menlo,monospace;letter-spacing:.28em;
margin:0 0 18px;color:#0f8a5f">{code}</p>
  <p style="margin:0 0 6px;font-size:13px;color:#5a6068">{CODE_TTL // 60} 分钟内有效，只能用一次。</p>
  <p style="margin:0;font-size:12px;color:#8b9096">不是你本人操作的话，忽略这封邮件就行。</p>
</div>""",
            subtype="html",
        )
        try:
            with smtplib.SMTP_SSL(conf["host"], conf["port"], timeout=20) as s:
                s.login(conf["user"], conf["password"])
                s.send_message(msg)
        except smtplib.SMTPAuthenticationError as exc:
            raise AuthError(
                "发件邮箱认证失败（检查 SMTP_USER 是否就是授权码对应的邮箱、SMTP_PASS 是否为授权码）"
            ) from exc
        except (OSError, smtplib.SMTPException) as exc:
            raise AuthError(f"邮件发送失败：{type(exc).__name__}") from exc

    # ---------------- 登录 ----------------

    def verify(self, raw_email: str, code: str) -> tuple[str, dict]:
        """验证码直接登录（邮箱第一次登录即注册）。返回 (token, user)。"""
        email = normalize_email(raw_email)
        code = (code or "").strip()
        if not re.fullmatch(r"\d{6}", code):
            raise AuthError("验证码是 6 位数字")

        now = time.time()
        with self._lock, self._db() as c:
            uid, user = self._redeem_code(c, email, code, now)
            token = self._issue_token(c, uid, now)
        return token, user

    def register(self, raw_email: str, code: str, password: str) -> tuple[str, dict]:
        """邮箱 + 验证码 + 密码注册。返回 (token, user)。"""
        email = normalize_email(raw_email)
        # 先查密码格式：别等验证码都验过了才告诉用户密码太短
        pw_hash = hash_password(password)
        code = (code or "").strip()
        if not re.fullmatch(r"\d{6}", code):
            raise AuthError("验证码是 6 位数字")

        now = time.time()
        with self._lock, self._db() as c:
            uid, user = self._redeem_code(c, email, code, now)
            row = c.execute("SELECT password_hash FROM users WHERE id=?", (uid,)).fetchone()
            if row and row["password_hash"]:
                raise AuthError("这个邮箱已经注册过了，直接用密码登录")
            c.execute(
                "UPDATE users SET password_hash=?, password_at=? WHERE id=?", (pw_hash, now, uid)
            )
            token = self._issue_token(c, uid, now)
        return token, user

    def login(self, raw_email: str, password: str) -> tuple[str, dict]:
        """邮箱 + 密码登录。返回 (token, user)。"""
        email = normalize_email(raw_email)
        if not password:
            raise AuthError("请输入密码")

        now = time.time()
        with self._lock, self._db() as c:
            row = c.execute(
                "SELECT id, email, password_hash FROM users WHERE email=?", (email,)
            ).fetchone()
            if row is None:
                raise AuthError("这个邮箱还没注册")
            if not row["password_hash"]:
                raise AuthError("这个账号还没设密码，先用验证码登录，进去再设一个")
            if not verify_password(password, row["password_hash"]):
                raise AuthError("密码不对")

            uid = row["id"]
            c.execute("UPDATE users SET last_login=? WHERE id=?", (now, uid))
            token = self._issue_token(c, uid, now, via="password")
        return token, {"id": uid, "email": email, "created": False}

    def set_password(self, user_id: str, new_raw: str, old_raw: str = "",
                     token: str = "") -> None:
        """设/改密码。

        安全上的两头堵：
        - 密码登录进来的会话，改密码必须先过原密码——不然别人拿到一台已登录的
          浏览器就能把账号改密码抢走
        - 验证码登录进来的会话（via=code）可以免原密码直接重设——邮箱能收信
          就证明是本人，这也是「忘了密码」的唯一恢复路径
        """
        new_hash = hash_password(new_raw)
        with self._lock, self._db() as c:
            row = c.execute("SELECT password_hash FROM users WHERE id=?", (user_id,)).fetchone()
            if row is None:
                raise AuthError("账号不存在")
            if row["password_hash"]:
                via = ""
                if token:
                    s = c.execute("SELECT via FROM sessions WHERE token=?", (token,)).fetchone()
                    via = (s["via"] if s else "") or ""
                if via != "code" and not verify_password(old_raw or "", row["password_hash"]):
                    raise AuthError("原密码不对")
            c.execute(
                "UPDATE users SET password_hash=?, password_at=? WHERE id=?",
                (new_hash, time.time(), user_id),
            )

    def _redeem_code(self, c: sqlite3.Connection, email: str, code: str,
                     now: float) -> tuple[str, dict]:
        """校验并消耗一个验证码，返回 (user_id, user)。账号不存在就顺手建。

        调用方必须持锁、在事务里。验证码在这里删掉——一次性，用完即废。
        """
        row = c.execute(
            "SELECT code, expires_at, tries FROM email_codes WHERE email=?", (email,)
        ).fetchone()
        if not row:
            raise AuthError("还没发过验证码，先点「发送验证码」")
        if row["expires_at"] < now:
            c.execute("DELETE FROM email_codes WHERE email=?", (email,))
            raise AuthError("验证码过期了，重新发一个")
        if row["tries"] >= CODE_TRIES:
            c.execute("DELETE FROM email_codes WHERE email=?", (email,))
            raise AuthError("试错次数太多，重新发一个")
        if not secrets.compare_digest(row["code"], code):
            c.execute("UPDATE email_codes SET tries=tries+1 WHERE email=?", (email,))
            left = CODE_TRIES - row["tries"] - 1
            raise AuthError(f"验证码不对，还能试 {max(left, 0)} 次")

        c.execute("DELETE FROM email_codes WHERE email=?", (email,))
        u = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if u is None:
            uid = "u_" + secrets.token_hex(8)
            c.execute(
                "INSERT INTO users(id, email, created_at, last_login) VALUES(?,?,?,?)",
                (uid, email, now, now),
            )
            user = {"id": uid, "email": email, "created": True}
        else:
            uid = u["id"]
            c.execute("UPDATE users SET last_login=? WHERE id=?", (now, uid))
            user = {"id": uid, "email": email, "created": False}
        return uid, user

    def _issue_token(self, c: sqlite3.Connection, uid: str, now: float,
                     via: str = "code") -> str:
        """发一个登录态 token，顺手清过期的。调用方持锁、在事务里。

        via 记这个会话是怎么进来的：'code'（验证码，能免原密码改密码）或
        'password'（密码登录，改密码要先验原密码）。
        """
        token = secrets.token_urlsafe(32)
        c.execute(
            "INSERT INTO sessions(token, user_id, created_at, expires_at, via) "
            "VALUES(?,?,?,?,?)",
            (token, uid, now, now + TOKEN_TTL, via),
        )
        c.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        return token

    def user_by_token(self, token: str) -> dict | None:
        if not token:
            return None
        with self._db() as c:
            row = c.execute(
                "SELECT u.id, u.email, u.created_at, u.last_login, "
                "(u.password_hash IS NOT NULL) has_password FROM sessions s "
                "JOIN users u ON u.id = s.user_id WHERE s.token=? AND s.expires_at>?",
                (token, time.time()),
            ).fetchone()
        return dict(row) if row else None

    def logout(self, token: str) -> None:
        if not token:
            return
        with self._lock, self._db() as c:
            c.execute("DELETE FROM sessions WHERE token=?", (token,))

    def user_by_id(self, uid: str) -> dict | None:
        with self._db() as c:
            row = c.execute(
                "SELECT id, email, created_at, last_login, "
                "(password_hash IS NOT NULL) has_password FROM users WHERE id=?",
                (uid,),
            ).fetchone()
        return dict(row) if row else None

    # ---------------- 会话 ----------------

    def create_conversation(self, user_id: str, title: str = "") -> dict:
        cid = "c_" + secrets.token_hex(8)
        now = time.time()
        with self._lock, self._db() as c:
            c.execute(
                "INSERT INTO conversations(id, user_id, title, created_at, updated_at) VALUES(?,?,?,?,?)",
                (cid, user_id, (title or "")[:60], now, now),
            )
        return {"id": cid, "title": title or "", "created_at": now, "updated_at": now}

    def touch_conversation(self, cid: str, title: str = "") -> None:
        """更新活跃时间；标题只在还没有标题时从第一句话里取。"""
        now = time.time()
        with self._lock, self._db() as c:
            if title:
                c.execute(
                    "UPDATE conversations SET updated_at=?, "
                    "title=CASE WHEN title='' THEN ? ELSE title END WHERE id=?",
                    (now, title[:60], cid),
                )
            else:
                c.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, cid))

    def list_conversations(self, user_id: str, limit: int = 50) -> list[dict]:
        with self._db() as c:
            rows = c.execute(
                "SELECT id, title, created_at, updated_at FROM conversations "
                "WHERE user_id=? ORDER BY updated_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_conversation(self, cid: str) -> dict | None:
        with self._db() as c:
            row = c.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
        return dict(row) if row else None

    def delete_conversation(self, cid: str, user_id: str) -> bool:
        with self._lock, self._db() as c:
            cur = c.execute("DELETE FROM conversations WHERE id=? AND user_id=?", (cid, user_id))
            return cur.rowcount > 0

    def all_users(self) -> list[dict]:
        with self._db() as c:
            rows = c.execute(
                "SELECT u.id, u.email, u.created_at, u.last_login, "
                "(password_hash IS NOT NULL) has_password, "
                "(SELECT COUNT(*) FROM conversations v WHERE v.user_id=u.id) n "
                "FROM users u ORDER BY u.last_login DESC"
            ).fetchall()
        return [dict(r) for r in rows]


_singleton: AuthStore | None = None


def get_auth(data_dir: Path | None = None) -> AuthStore:
    global _singleton
    if _singleton is None:
        if data_dir is None:
            from .config import settings

            data_dir = settings.data_dir
        _singleton = AuthStore(data_dir)
    return _singleton
