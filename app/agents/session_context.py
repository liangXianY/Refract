"""会话上下文：滚动窗口 + 摘要（rolling summary）。

策略（对齐需求）：
- 最近 5 轮对话（= 用户 + AI 共 10 条消息）原样保留，供 planner 理解指代
- 更早的消息不再逐条存，而是由模型压缩成一段摘要（滚动累积）
- planner 看到的是「早期摘要」+「最近 5 轮原文」，既不会无限膨胀，又保留近期细节

为什么用摘要而不是直接丢：超过 5 轮的对话里可能藏着用户的关键偏好、
指代对象（"就是上次说的那个"），直接丢会让 planner 失忆。摘要把精华留下。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# 滑动窗口：最近 10 条消息（= 5 轮用户+AI）原样保留
MAX_RECENT_MSGS = 10


@dataclass
class Turn:
    role: str  # user | assistant
    summary: str
    ts: float = field(default_factory=time.time)


@dataclass
class Session:
    session_id: str
    recent: list[Turn] = field(default_factory=list)  # 最近 N 条原文
    old_summary: str = ""  # 更早对话的摘要（模型生成，滚动累积）
    updated_at: float = field(default_factory=time.time)


class SessionMemory:
    """进程级会话上下文。单机单进程够用；多进程部署要换 Redis。"""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def push(self, session_id: str, role: str, summary: str) -> None:
        if not session_id:
            return
        s = self._sessions.get(session_id)
        if s is None:
            s = Session(session_id=session_id)
            self._sessions[session_id] = s
        summary = (summary or "").strip()
        if not summary:
            return
        s.recent.append(Turn(role=role, summary=summary))
        s.updated_at = time.time()

    def overflow(self, session_id: str) -> list[Turn]:
        """超出窗口的旧消息，返回并从 recent 里移除（调用方拿去喂摘要模型）。"""
        s = self._sessions.get(session_id)
        if not s:
            return []
        if len(s.recent) <= MAX_RECENT_MSGS:
            return []
        # 移除最老的，直到剩 MAX_RECENT_MSGS 条
        overflow = s.recent[: len(s.recent) - MAX_RECENT_MSGS]
        s.recent = s.recent[len(s.recent) - MAX_RECENT_MSGS:]
        return overflow

    def set_summary(self, session_id: str, summary: str) -> None:
        """用模型生成的摘要替换 old_summary（滚动累积）。"""
        s = self._sessions.get(session_id)
        if s:
            s.old_summary = (summary or "").strip()

    def get_summary(self, session_id: str) -> str:
        s = self._sessions.get(session_id)
        return s.old_summary if s else ""

    def context(self, session_id: str) -> str:
        """拼成给 planner 的「前文」：早期摘要 + 最近 5 轮原文。"""
        s = self._sessions.get(session_id)
        if not s:
            return ""
        lines = ["【对话前文（供你理解指代）】"]
        if s.old_summary:
            lines.append(f"· 更早的对话摘要：{s.old_summary}")
        for t in s.recent:
            who = "用户" if t.role == "user" else "你(助手)"
            lines.append(f"- {who}：{t.summary}")
        return "\n".join(lines)

    def forget(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)


# 进程级单例
session_memory = SessionMemory()
