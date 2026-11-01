"""会话历史持久化：同一 IP 刷新后能看到完整对话（含过程卡）。

设计：
- 按 session_id（IP 派生）存到 data/sessions/{session_id}.json
- 每轮对话存「用户消息 + 这一轮的全部 SSE 事件序列」
- 重放 = 把事件序列原样喂回前端的 onEvent，前端不用知道「这是历史还是实时」

为什么不复用 runs/：runs 是「复盘用的技术留痕」（含 internal 字段、每步耗时、
失败原因），偏调试；这里要的是「对话体验的可恢复」，存的是用户能看见的东西。
两者关注点不同，分开存。

事件里可能含大字段（material 全文、视频 plans），所以只挑「前端会渲染的」
事件类型存，且裁剪大字段，控制体积。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

# 前端会渲染、值得回放的事件类型（其余如 ping/note/internal 跳过）
KEEP_TYPES = {
    "plan", "step_start", "tool_detail", "step_end", "delta", "stage",
    "reflection", "artifact", "video_plans", "done_reply", "error",
}

# 单个事件里要裁剪的长字段，避免 material 全文把 JSON 撑爆
TRUNC_FIELDS = {"material": 800, "lines": 20}


def _trim_event(ev: dict) -> dict:
    out = {}
    for k, v in ev.items():
        if k == "material" and isinstance(v, str):
            out[k] = v[:TRUNC_FIELDS["material"]]
        elif k == "lines" and isinstance(v, list):
            out[k] = v[:TRUNC_FIELDS["lines"]]
        elif k in ("brief", "plans") and isinstance(v, (dict, list)):
            out[k] = v  # 产物结构要完整保留，前端渲染方案卡/正文要用
        else:
            out[k] = v
    return out


class SessionHistory:
    def __init__(self, data_dir: Path) -> None:
        self._dir = data_dir / "sessions"
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        return self._dir / f"{session_id}.json"

    def start_turn(self, session_id: str, user_text: str, task_id: str = "") -> None:
        """开一轮：记用户消息（一条独立 message）。task_id 用于流水反查这一轮。"""
        data = self._load(session_id)
        data["messages"].append({"role": "user", "text": user_text, "events": [], "task_id": task_id})
        # 同时开一条空白的 ai 消息槽位，事件往里填
        data["messages"].append({"role": "assistant", "text": "", "events": [], "task_id": task_id})
        self._save(session_id, data)

    def append_event(self, session_id: str, ev: dict) -> None:
        """记一个事件到当前轮的最后一条（assistant）消息。"""
        if ev.get("type") not in KEEP_TYPES:
            return
        data = self._load(session_id)
        if not data["messages"]:
            return
        # 找最后一条 assistant 消息
        for m in reversed(data["messages"]):
            if m.get("role") == "assistant":
                m["events"].append(_trim_event(ev))
                break
        self._save(session_id, data)

    def history(self, session_id: str) -> list[dict]:
        return self._load(session_id).get("messages", [])

    def qa_history(self, session_id: str) -> list[dict]:
        """管理员视角的「纯问答」：只要用户问题 + AI 正文，不要过程事件。

        AI 正文从 delta 事件累积，其余（plan/step/tool/stage）全部丢弃。
        """
        out: list[dict] = []
        pending_user = None
        for m in self.history(session_id):
            if m.get("role") == "user":
                pending_user = m.get("text", "")
            elif m.get("role") == "assistant":
                body = "".join(
                    ev.get("text", "")
                    for ev in m.get("events", [])
                    if ev.get("type") == "delta" and ev.get("kind") == "content"
                )
                out.append({"q": pending_user or "", "a": body})
                pending_user = None
        return out

    def turn_qa(self, task_id: str) -> dict | None:
        """按 task_id 反查某一轮的纯问答（流水表点「查看对话」用）。"""
        for m in self.history_by_task(task_id):
            if m.get("role") == "user":
                q = m.get("text", "")
            elif m.get("role") == "assistant":
                body = "".join(
                    ev.get("text", "")
                    for ev in m.get("events", [])
                    if ev.get("type") == "delta" and ev.get("kind") == "content"
                )
                return {"q": q, "a": body}
        return None

    def history_by_task(self, task_id: str) -> list[dict]:
        """遍历所有 session 找 task_id 对应的那两条消息（user + assistant）。

        按 task_id 反查需要扫全部 session 文件——数据量小（个人部署）可接受。
        """
        if not task_id or not self._dir.exists():
            return []
        for p in self._dir.glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, TypeError):
                continue
            msgs = data.get("messages", [])
            for i, m in enumerate(msgs):
                if m.get("task_id") == task_id:
                    return msgs[i:i + 2]
        return []

    def _load(self, session_id: str) -> dict:
        p = self._path(session_id)
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, TypeError):
                pass
        return {"messages": []}

    def _save(self, session_id: str, data: dict) -> None:
        p = self._path(session_id)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# 模块级单例（和 quota 同款理由：main 和别处要共享同一个实例）
_history_singleton: SessionHistory | None = None


def get_history(data_dir: Path | None = None) -> SessionHistory:
    global _history_singleton
    if _history_singleton is None:
        if data_dir is None:
            from .config import settings

            data_dir = settings.data_dir
        _history_singleton = SessionHistory(data_dir)
    return _history_singleton
