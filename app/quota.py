"""轻量配额 + 使用流水。

设计：
- 键（key）是「身份」：登录后是账号 ID（`u_xxx`），没有账号的调用方仍可用 IP
  （后台/脚本）。账号 ID 直接当键用，IP 则哈希成匿名 ID——这样老数据不用迁移
- 配额：每个账号默认 10 轮对话 + 1 个视频 + 3 篇文章，用完成止。管理员可以
  单独给某个账号改上限（`limits`）、重置已用量、或设为不限
- 每次请求记录到 usage.json：IP、时间、动作（chat/video/article）、产物链接，
  管理员后台可查

注意 `ip` 字段的历史包袱：早期这个位置存的是身份键本身（账号 ID 或 IP 哈希），
后台里显示的「IP」其实是那串 ID。真实 IP 单独记在 `last_ip`。

为什么存 JSON 而不是 sqlite：数据量小（个人部署），JSON 可读可手改，
和项目里 runs/、models.json 的存法一致，少一个依赖。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


# 永久配额上限（用完成止，管理员重置或设不限）
QUOTA = {
    "chat": 10,      # 对话轮次
    "video": 1,      # 生成视频
    "article": 3,    # 生成文章
}

# 动作 → 中文名，给后台看
ACTION_LABEL = {"chat": "对话", "video": "视频", "article": "文章"}


def derive_session_id(ip: str) -> str:
    """IP → 稳定 session_id。加盐防止裸 IP 可逆（日志/后台里不直接曝 IP）。"""
    return hashlib.sha256(f"refract:{ip}".encode()).hexdigest()[:24]


@dataclass
class UserUsage:
    ip: str
    session_id: str
    used: dict = field(default_factory=lambda: {"chat": 0, "video": 0, "article": 0})
    unlimited: bool = False
    first_seen: str = ""
    last_seen: str = ""
    limits: dict | None = None   # 单独给这个账号设的上限；None = 用全局 QUOTA
    last_ip: str = ""            # 真实 IP（账号身份下也能看出从哪来）


class QuotaStore:
    """配额 + 访问记录。进程内缓存，每次变更落盘。"""

    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / "usage.json"
        self._users: dict[str, UserUsage] = {}
        self._log: list[dict] = []  # 访问流水：{ip, session_id, action, ts, artifact}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            for u in data.get("users", []):
                # 老数据里没有 limits/last_ip 这两个键，用 dataclass 的默认值补齐
                self._users[u["session_id"]] = UserUsage(**u)
            self._log = data.get("log", [])
        except (json.JSONDecodeError, TypeError):
            pass

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "users": [vars(u) for u in self._users.values()],
            "log": self._log,
        }
        self._path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _sid(self, key: str) -> str:
        """账号 ID 直接用；IP 哈希成匿名 ID（避免日志里裸 IP，也让老数据继续有效）。"""
        return key if key.startswith("u_") else derive_session_id(key)

    def get(self, ip: str) -> UserUsage:
        sid = self._sid(ip)
        u = self._users.get(sid)
        if u is None:
            now = datetime.now().isoformat(timespec="seconds")
            u = UserUsage(ip=ip, session_id=sid, first_seen=now, last_seen=now)
            self._users[sid] = u
            self._save()
        return u

    def limit_of(self, u: UserUsage, action: str) -> int:
        """这个账号在这个动作上的上限：单独设过就用单独的，否则用全局默认。"""
        lim = (u.limits or {}).get(action)
        if lim is None:
            lim = QUOTA.get(action, 10)
        try:
            return max(0, int(lim))
        except (TypeError, ValueError):
            return QUOTA.get(action, 10)

    def can(self, ip: str, action: str) -> tuple[bool, str]:
        """能不能执行这个动作。返回 (ok, 原因)。"""
        u = self.get(ip)
        if u.unlimited:
            return True, ""
        limit = self.limit_of(u, action)
        if u.used.get(action, 0) >= limit:
            return False, f"已达上限（{limit}），请联系管理员重置或开通不限"
        return True, ""

    def consume(self, ip: str, action: str, artifact: str = "", real_ip: str = "") -> None:
        u = self.get(ip)
        if not u.unlimited:
            u.used[action] = u.used.get(action, 0) + 1
        u.last_seen = datetime.now().isoformat(timespec="seconds")
        if real_ip:
            u.last_ip = real_ip
        self._log.append({
            "ip": real_ip or ip,
            "session_id": u.session_id,
            "action": action,
            "label": ACTION_LABEL.get(action, action),
            "ts": datetime.now().isoformat(timespec="seconds"),
            "artifact": artifact,
        })
        # 流水只留最近 500 条，防止无限膨胀
        if len(self._log) > 500:
            self._log = self._log[-500:]
        self._save()

    def reset(self, session_id: str) -> bool:
        u = self._users.get(session_id)
        if u is None:
            return False
        u.used = {"chat": 0, "video": 0, "article": 0}
        self._save()
        return True

    def set_unlimited(self, session_id: str, flag: bool) -> bool:
        u = self._users.get(session_id)
        if u is None:
            return False
        u.unlimited = flag
        self._save()
        return True

    def set_limits(self, session_id: str, limits: dict | None) -> bool:
        """单独给某个账号设上限。传空/None = 取消自定义，回到全局默认。

        只认 chat/video/article 三个键，值取整夹到 [0, 100000]——后台是手填的，
        别让一个手滑的负数把账号锁死。
        """
        u = self._users.get(session_id)
        if u is None:
            return False
        clean: dict[str, int] = {}
        for k in ("chat", "video", "article"):
            v = (limits or {}).get(k)
            if v is None or v == "":
                continue
            try:
                clean[k] = max(0, min(int(v), 100000))
            except (TypeError, ValueError):
                continue
        u.limits = clean or None
        self._save()
        return True

    def users(self) -> list[dict]:
        out = []
        for u in self._users.values():
            out.append({
                "session_id": u.session_id,
                "ip": u.ip,
                "last_ip": u.last_ip,
                "used": dict(u.used),
                "unlimited": u.unlimited,
                "first_seen": u.first_seen,
                "last_seen": u.last_seen,
                "limits": {k: self.limit_of(u, k) for k in QUOTA},
                "custom_limits": bool(u.limits),
            })
        out.sort(key=lambda x: x["last_seen"] or "", reverse=True)
        return out

    def log(self, limit: int = 100) -> list[dict]:
        return list(reversed(self._log[-limit:]))


# 模块级单例：main.py 和 supervisor.py 都要用同一个实例，
# 否则配额会各记各的账（main 记了 chat，supervisor 记 article，数据对不上）
_quota_singleton: QuotaStore | None = None


def get_quota(data_dir: Path | None = None) -> QuotaStore:
    global _quota_singleton
    if _quota_singleton is None:
        if data_dir is None:
            from .config import settings

            data_dir = settings.data_dir
        _quota_singleton = QuotaStore(data_dir)
    return _quota_singleton
