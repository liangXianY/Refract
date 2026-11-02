"""会话状态：结构化事实 + 滚动窗口 + 早期摘要。

三类东西，混在一起存就会互相污染，所以分开：

1. **结构化事实（facts）**——工具真正查到的数据：热榜 10 条（带排名/热度/链接）、
   搜过哪些词命中哪些网页、写过哪篇文章。这些是「硬数据」，要原样留给下一轮，
   不能压成一句话摘要。
   踩过的坑：之前只存「微博 top10」这种摘要，用户追问「详细讲一下第二条」，
   planner 手上没有任何条目，只能说找不到。

2. **最近 5 轮原文**——用户和助手各一句，供理解「刚才那个」「上一篇」的指代。

3. **更早对话的摘要**——模型压缩，滚动累积，不无限膨胀。

持久化到 data/sessions/{sid}.ctx.json：进程重启、服务重发版都不丢。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

# 滑动窗口：最近 10 条消息（= 5 轮用户+AI）原样保留
MAX_RECENT_MSGS = 10
# 搜索/产物类事实只留最近几条，避免上下文无限长
MAX_SEARCHES = 3
MAX_PAGES = 3
MAX_OUTPUTS = 2


@dataclass
class Turn:
    role: str  # user | assistant
    summary: str
    ts: float = field(default_factory=time.time)


def _empty_facts() -> dict:
    return {
        "hot": None,        # 最近一次热榜：{source, label, items, turn, ts}
        "searches": [],     # [{query, items:[{title,url}], turn, ts}]
        "pages": [],        # [{url, title, words, turn, ts}]
        "articles": [],     # [{task_id, title, words, url, turn, ts}]
        "videos": [],       # [{task_id, topic, url, turn, ts}]
    }


@dataclass
class Session:
    session_id: str
    recent: list[Turn] = field(default_factory=list)
    old_summary: str = ""
    facts: dict = field(default_factory=_empty_facts)
    turn: int = 0
    updated_at: float = field(default_factory=time.time)


class SessionMemory:
    """进程级会话上下文。单机单进程够用；多进程部署要换 Redis。"""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._dir: Path | None = None

    # ---------- 持久化 ----------
    def configure(self, data_dir: str | Path) -> None:
        self._dir = Path(data_dir) / "sessions"
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path | None:
        if self._dir is None:
            return None
        safe = "".join(c for c in session_id if c.isalnum() or c in "-_")
        return self._dir / f"{safe}.ctx.json" if safe else None

    def _get(self, session_id: str) -> Session | None:
        if not session_id:
            return None
        s = self._sessions.get(session_id)
        if s is not None:
            return s
        # 冷启动/重启后从磁盘捞回来
        p = self._path(session_id)
        if p and p.exists():
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                s = Session(
                    session_id=session_id,
                    old_summary=d.get("old_summary", ""),
                    facts={**_empty_facts(), **(d.get("facts") or {})},
                    turn=int(d.get("turn") or 0),
                    updated_at=float(d.get("updated_at") or time.time()),
                )
                self._sessions[session_id] = s
                return s
            except Exception:
                pass
        return None

    def _save(self, s: Session) -> None:
        p = self._path(s.session_id)
        if not p:
            return
        try:
            p.write_text(
                json.dumps(
                    {
                        "old_summary": s.old_summary,
                        "facts": s.facts,
                        "turn": s.turn,
                        "updated_at": s.updated_at,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass  # 落盘失败不影响本次对话

    # ---------- 对话轮次 ----------
    def push(self, session_id: str, role: str, summary: str) -> None:
        if not session_id:
            return
        s = self._get(session_id)
        if s is None:
            s = Session(session_id=session_id)
            self._sessions[session_id] = s
        summary = (summary or "").strip()
        if not summary:
            return
        s.recent.append(Turn(role=role, summary=summary))
        if role == "user":
            s.turn += 1
        s.updated_at = time.time()
        self._save(s)

    def overflow(self, session_id: str) -> list[Turn]:
        """超出窗口的旧消息，返回并从 recent 里移除（调用方拿去喂摘要模型）。"""
        s = self._get(session_id)
        if not s or len(s.recent) <= MAX_RECENT_MSGS:
            return []
        cut = len(s.recent) - MAX_RECENT_MSGS
        overflow = s.recent[:cut]
        s.recent = s.recent[cut:]
        return overflow

    def set_summary(self, session_id: str, summary: str) -> None:
        s = self._get(session_id)
        if s:
            s.old_summary = (summary or "").strip()
            self._save(s)

    def get_summary(self, session_id: str) -> str:
        s = self._get(session_id)
        return s.old_summary if s else ""

    # ---------- 结构化事实 ----------
    def record_results(self, session_id: str, results: list[dict]) -> None:
        """把本轮工具产出里的硬数据抽出来存好，供后续轮次直接引用。"""
        if not session_id or not results:
            return
        s = self._get(session_id)
        if s is None:
            s = Session(session_id=session_id)
            self._sessions[session_id] = s
        f = s.facts
        now = time.time()
        for r in results:
            tool = r.get("tool")
            if tool == "hot_topics" and r.get("items"):
                f["hot"] = {
                    "source": r.get("source") or "",
                    "label": (r.get("summary") or "").split(" top")[0],
                    "items": [
                        {
                            "rank": it.get("rank") or i + 1,
                            "title": it.get("title", ""),
                            "score": it.get("score", ""),
                            "link": it.get("link") or it.get("url") or "",
                        }
                        for i, it in enumerate(r["items"][:20])
                    ],
                    "turn": s.turn,
                    "ts": now,
                }
            elif tool in ("web_search", "lookup_hot") and r.get("query"):
                f["searches"].append(
                    {
                        "query": r["query"],
                        "items": [{"title": it.get("title", ""), "url": it.get("url", "")}
                                  for it in (r.get("hits") or [])][:6],
                        "turn": s.turn,
                        "ts": now,
                    }
                )
                f["searches"] = f["searches"][-MAX_SEARCHES:]
            elif tool == "fetch_url" and r.get("url"):
                f["pages"].append(
                    {"url": r["url"], "title": r.get("title", ""),
                     "words": r.get("words", 0), "turn": s.turn, "ts": now}
                )
                f["pages"] = f["pages"][-MAX_PAGES:]
            elif tool == "write_article" and r.get("result"):
                res = r["result"]
                brief = res.get("brief") or {}
                f["articles"].append(
                    {
                        "task_id": r.get("task_id", ""),
                        "title": brief.get("title") or brief.get("topic") or res.get("title") or "",
                        "words": res.get("word_count", 0),
                        "url": f"/r/{r.get('task_id', '')}",
                        "turn": s.turn,
                        "ts": now,
                    }
                )
                f["articles"] = f["articles"][-MAX_OUTPUTS:]
            elif tool == "video_plans" and r.get("video_url"):
                f["videos"].append(
                    {"task_id": r.get("task_id", ""), "topic": r.get("topic", ""),
                     "url": r["video_url"], "turn": s.turn, "ts": now}
                )
                f["videos"] = f["videos"][-MAX_OUTPUTS:]
        s.updated_at = now
        self._save(s)

    def prior_results(self, session_id: str) -> list[dict]:
        """把上几轮的事实还原成「伪工具结果」，塞进本轮 results 开头。

        两个用处：
        - lookup_hot 靠它按 rank 找到上一轮那条热点（用户说「讲第二条」）
        - 收尾/写作能引用上一轮的产物，不用重拉
        只带条目不带 material：旧素材不能当新稿的事实依据。
        """
        s = self._get(session_id)
        if not s:
            return []
        out: list[dict] = []
        hot = s.facts.get("hot")
        if hot and hot.get("items"):
            out.append(
                {
                    "tool": "hot_topics",
                    "summary": f"{hot.get('label') or hot.get('source')} top{len(hot['items'])}（上一轮）",
                    "source": hot.get("source", ""),
                    "items": hot["items"],
                    "material": "",
                    "elapsed_ms": 0,
                    "prior": True,
                }
            )
        return out

    # ---------- 拼给 planner 的上下文 ----------
    def context(self, session_id: str) -> str:
        s = self._get(session_id)
        if not s:
            return ""
        blocks: list[str] = []

        # 1) 对话前文：早期摘要 + 最近 5 轮原文
        lines = ["【对话前文（供你理解指代）】"]
        if s.old_summary:
            lines.append(f"· 更早的对话摘要：{s.old_summary}")
        for t in s.recent:
            who = "用户" if t.role == "user" else "你(助手)"
            lines.append(f"- {who}：{t.summary}")
        blocks.append("\n".join(lines))

        # 2) 已知信息：硬数据原样摆出来，模型自己判断要不要复用
        facts = s.facts
        flines: list[str] = []
        hot = facts.get("hot")
        if hot and hot.get("items"):
            ago = max(0, s.turn - int(hot.get("turn") or s.turn))
            label = hot.get("label") or hot.get("source") or "热榜"
            flines.append(
                f"· 「{label}」热榜 {len(hot['items'])} 条"
                + (f"（{ago} 轮前拉的）" if ago else "（刚拉的）")
                + "，用户说「第 N 条」就是指下面第 N 条："
            )
            for it in hot["items"][:12]:
                score = f"（{it['score']}）" if it.get("score") else ""
                flines.append(f"    {it.get('rank')}. {it.get('title','')}{score}")
            flines.append(
                f"    要读某条详情：lookup_hot(source={hot.get('source') or ''}, rank=N)；"
                "要拿某条写稿：把它的标题原样放进 write_article 的 input，"
                f"并写明它来自「{label}」和上面的热度。"
            )
        for sr in facts.get("searches") or []:
            hits = "；".join(f"《{i['title'][:28]}》" for i in sr.get("items") or [])
            ago = max(0, s.turn - int(sr.get("turn") or s.turn))
            flines.append(
                f"· {ago} 轮前搜过「{sr.get('query','')}」"
                + (f"，命中：{hits}" if hits else "（没返回结果）")
            )
        for pg in facts.get("pages") or []:
            flines.append(f"· 读过网页《{pg.get('title','')}》 {pg.get('url','')}")
        for ar in facts.get("articles") or []:
            flines.append(f"· 已生成文章《{str(ar.get('title',''))[:30]}》 {ar.get('words',0)} 字 {ar.get('url','')}")
        for vd in facts.get("videos") or []:
            flines.append(f"· 已生成视频《{str(vd.get('topic',''))[:30]}》 {vd.get('url','')}")
        if flines:
            blocks.append(
                "【已知信息（之前查到的真实数据，能直接用，别重新去拉一遍）】\n"
                + "\n".join(flines)
            )
        return "\n\n".join(blocks)

    def forget(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        p = self._path(session_id)
        if p and p.exists():
            try:
                p.unlink()
            except Exception:
                pass


# 进程级单例
session_memory = SessionMemory()
