"""任务留痕：每次生成落一份 JSON，供复盘用。

为什么单独存一份，而不是从产物里读
----------------------------------
产物页是给眼睛看的，复盘要的是「这次为什么成/为什么败」：
哪套方案、哪个模型、第几版过的闸、没过的那几版各踩了什么。
这些信息生成完就丢了——任务状态在进程内存里，重启即失。

存成 `data/runs/{task_id}.json`，一任务一文件：
- 不用数据库，服务器内存紧张，读文件就够
- 出问题时直接把单个 JSON 发给别人，不带别的上下文也能看懂
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def _dir(data_dir: Path) -> Path:
    d = data_dir / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save(data_dir: Path, record: dict) -> Path:
    task_id = record.get("task_id") or "unknown"
    record.setdefault("finished_at", datetime.now().isoformat(timespec="seconds"))
    path = _dir(data_dir) / f"{task_id}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def list_runs(data_dir: Path, limit: int = 30) -> list[dict]:
    """按时间倒序列出，只回摘要字段——完整记录可能几十 KB，列表不该背这个包袱。"""
    files = sorted(_dir(data_dir).glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[dict] = []
    for f in files[:limit]:
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        out.append(
            {
                "task_id": r.get("task_id"),
                "kind": r.get("kind"),
                "finished_at": r.get("finished_at"),
                "elapsed_ms": r.get("elapsed_ms"),
                "title": (r.get("brief") or {}).get("topic"),
                "plan": (r.get("plan") or {}).get("name"),
                "provider": r.get("provider"),
                "model": r.get("model"),
                "degraded": r.get("degraded"),
                "gate_ok": (r.get("gate") or {}).get("ok"),
                "word_count": r.get("word_count"),
                "final_score": r.get("final_score"),
                "artifact": r.get("artifact"),
            }
        )
    return out


def get(data_dir: Path, task_id: str) -> dict | None:
    if not task_id.isalnum():
        return None
    path = _dir(data_dir) / f"{task_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
