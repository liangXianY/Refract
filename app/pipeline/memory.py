"""记忆分层：让 Refract 越用越懂你。

三层，别混：
1. 会话记忆 —— 这次任务改了什么、否掉了什么。进程内（TaskState 已有 events）。
2. 项目记忆 —— 人设卡、禁用词、语气偏好、历史选题。SQLite，长期。
3. 长期偏好 —— 哪类标题/结构效果好。SQLite 表 + 回流字段，量级小不用向量库。

关键设计：记忆不是「存了就行」，要被用起来——
- 项目记忆的 persona / banned_words 进 Writer 的 prompt（管住风格不飘）
- 长期偏好的结构分进 Reflector 的评分参照（「这个结构过去表现好」加分）

存储选型：SQLite。服务器 1.9G 内存，不上向量库/Redis。
表就两张：memory（键值）、structure_stats（结构效果回流）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class MemoryItem:
    key: str
    value: str
    kind: str = "general"  # persona / banned / tone / topic / general
    updated_at: float = 0.0


_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'general',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

-- 长期偏好：每种内容结构的历史表现，用于 Reflector 加分
CREATE TABLE IF NOT EXISTS structure_stats (
    structure  TEXT PRIMARY KEY,   -- 结构标识，如 "问题-方案-证据-行动"
    runs       INTEGER NOT NULL DEFAULT 0,
    score_sum  REAL    NOT NULL DEFAULT 0,
    last_score REAL    NOT NULL DEFAULT 0,
    updated_at REAL    NOT NULL
);
"""


class MemoryStore:
    """SQLite 记忆库。单机单库，用一把锁串行化写操作。"""

    def __init__(self, db_path: Path | str) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def _init(self) -> None:
        with self._lock, self._conn() as c:
            c.executescript(_SCHEMA)

    # ---------- 项目记忆 ----------

    def set(self, key: str, value: str, kind: str = "general") -> None:
        now = time.time()
        with self._lock, self._conn() as c:
            c.execute(
                """INSERT INTO memory(key, value, kind, created_at, updated_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET
                     value=excluded.value, kind=excluded.kind, updated_at=excluded.updated_at""",
                (key, value, kind, now, now),
            )

    def get(self, key: str) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT value FROM memory WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def delete(self, key: str) -> bool:
        with self._lock, self._conn() as c:
            cur = c.execute("DELETE FROM memory WHERE key=?", (key,))
        return cur.rowcount > 0

    def list_items(self, kind: str | None = None) -> list[MemoryItem]:
        sql = "SELECT key, value, kind, updated_at FROM memory"
        args: tuple[Any, ...] = ()
        if kind:
            sql += " WHERE kind=?"
            args = (kind,)
        sql += " ORDER BY updated_at DESC"
        with self._conn() as c:
            rows = c.execute(sql, args).fetchall()
        return [
            MemoryItem(r["key"], r["value"], r["kind"], r["updated_at"]) for r in rows
        ]

    # ---------- 长期偏好（结构回流） ----------

    def record_structure(self, structure: str, score: float) -> None:
        """记录一次「这种结构 + 这次得分」，用于后续 Reflector 参考。"""
        now = time.time()
        with self._lock, self._conn() as c:
            c.execute(
                """INSERT INTO structure_stats(structure, runs, score_sum, last_score, updated_at)
                   VALUES(?,1,?,?,?)
                   ON CONFLICT(structure) DO UPDATE SET
                     runs=runs+1, score_sum=score_sum+excluded.score_sum,
                     last_score=excluded.last_score, updated_at=excluded.updated_at""",
                (structure, score, score, now),
            )

    def structure_avg(self, structure: str) -> float | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT runs, score_sum FROM structure_stats WHERE structure=?",
                (structure,),
            ).fetchone()
        if not row or not row["runs"]:
            return None
        return row["score_sum"] / row["runs"]

    def top_structures(self, limit: int = 5) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT structure, runs, score_sum, last_score,
                          score_sum/NULLIF(runs,0) AS avg
                   FROM structure_stats WHERE runs >= 1
                   ORDER BY avg DESC, runs DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- 给 Writer / Reflector 用的文本 ----------

    def style_profile(self) -> str:
        """把项目记忆拼成一段「人设卡」，直接进 Writer 的 prompt。

        这是记忆真正被用起来的地方：没有它，每篇稿子的风格都会飘。
        """
        persona = self.get("persona")
        tone = self.get("tone")
        banned = self.get("banned_words")
        audience = self.get("audience")

        parts: list[str] = []
        if persona:
            parts.append(f"人设：{persona}")
        if tone:
            parts.append(f"语气：{tone}")
        if audience:
            parts.append(f"读者：{audience}")
        if banned:
            words = [w.strip() for w in banned.replace("，", ",").split(",") if w.strip()]
            if words:
                parts.append("禁用词（一个都不要出现）：" + "、".join(words))

        if not parts:
            return ""
        return "\n\n【写作人设（来自项目记忆，必须遵守）】\n" + "\n".join(parts)

    def reflect_hint(self, structure: str | None = None) -> str:
        """给 Reflector 的参考：历史表现好的结构。"""
        tops = self.top_structures(limit=3)
        if not tops:
            return ""
        lines = [
            f"- {t['structure']}：平均 {t['avg']:.1f} 分（{t['runs']} 次）"
            for t in tops
        ]
        return "\n\n【历史表现较好的结构（评分时参考）】\n" + "\n".join(lines)


# 全局单例（由 main 初始化时注入路径）
_store: MemoryStore | None = None


def init_memory(db_path: Path | str) -> MemoryStore:
    global _store
    _store = MemoryStore(db_path)
    return _store


def get_memory() -> MemoryStore:
    if _store is None:
        raise RuntimeError("memory 未初始化，请先调用 init_memory()")
    return _store


# 供接口返回用的序列化
def dump_items(items: list[MemoryItem]) -> str:
    return json.dumps(
        [{"key": i.key, "value": i.value, "kind": i.kind, "updated_at": i.updated_at} for i in items],
        ensure_ascii=False,
    )
