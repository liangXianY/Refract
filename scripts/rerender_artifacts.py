"""把已生成的产物页按当前渲染器重画一遍。

/r/{id} 返回的是生成当时落盘的静态 HTML，所以渲染器修好之后老文章还是旧样子。
正文和 brief 都在 runs 记录里，直接重画即可，不用重新跑模型。

用法（服务器上，项目根目录）：.venv/bin/python scripts/rerender_artifacts.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings                     # noqa: E402
from app.pipeline import runs                       # noqa: E402
from app.pipeline.renderer import save_article     # noqa: E402
from app.schemas import ContentBrief                # noqa: E402


def main() -> None:
    ok = skipped = failed = 0
    for row in runs.list_runs(settings.data_dir, limit=500):
        if row.get("kind") != "article":
            continue
        tid = row.get("task_id") or ""
        rec = runs.get(settings.data_dir, tid) or {}
        body = rec.get("body") or ""
        brief_raw = rec.get("brief") or {}
        if not body or not brief_raw:
            skipped += 1
            continue
        try:
            brief = ContentBrief.model_validate(brief_raw)
        except Exception:
            skipped += 1     # 老记录字段不全，别为了修复把服务搞崩
            continue
        rounds = rec.get("rounds") or []
        meta = (f"{rec.get('model') or '?'} · 迭代 {len(rounds)} 轮 · "
                f"{rec.get('word_count') or len(body)} 字")
        try:
            save_article(brief, body, tid, settings.data_dir, meta)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  失败 {tid}: {exc}")
            failed += 1
    print(f"重画完成：{ok} 篇，跳过 {skipped}，失败 {failed}")


if __name__ == "__main__":
    main()
