"""配方卡索引：从 video-shotcraft 仓库读取 124 张镜头卡的 frontmatter。

卡库是唯一事实源（references/shots/*/*.md），这里只做解析和两级的供给：
- index(): 全量卡的紧凑清单（名字/一句话/适用/时长/能量/标签）→ 喂给镜头映射 prompt
- card():  单卡全文 → 喂给逐镜头实现 prompt（卡上"已知坑/参数表"是实现时的硬约束）

仓库位置可配（RECIPE_DIR），默认项目同级 skills/video-shotcraft。
卡库缺失时 index() 返回空列表、管线退化为内置四段式分镜——链路不断，只是没有卡可抽。
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

from ..config import ROOT

_DEFAULT = ROOT.parent / "skills" / "video-shotcraft" / "references" / "shots"

_FRONT = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


def recipe_root() -> Path:
    env = os.environ.get("RECIPE_DIR")
    return Path(env) if env else _DEFAULT


@lru_cache(maxsize=1)
def _all_cards() -> dict[str, dict]:
    """{卡名: {category, path, frontmatter字段, body}}。首次调用扫盘后缓存。"""
    root = recipe_root()
    cards: dict[str, dict] = {}
    if not root.is_dir():
        return cards
    for md in sorted(root.glob("*/*.md")):
        text = md.read_text(encoding="utf-8", errors="replace")
        m = _FRONT.match(text)
        meta: dict = {}
        body = text
        if m:
            body = text[m.end():]
            for line in m.group(1).splitlines():
                key, _, val = line.partition(":")
                meta[key.strip()] = val.strip()
        name = meta.get("name") or md.stem
        cards[name] = {
            "name": name,
            "category": md.parent.name,
            "one_liner": meta.get("一句话", ""),
            "use_for": meta.get("适用", ""),
            "duration": meta.get("时长", ""),
            "energy": meta.get("能量", ""),
            "tags": [t.strip() for t in meta.get("标签", "").replace("，", ",").split(",") if t.strip()],
            "body": body,
        }
    return cards


def index() -> list[dict]:
    """紧凑索引（不含正文），供镜头映射 prompt 用。"""
    return [{k: c[k] for k in ("name", "category", "one_liner", "use_for", "duration", "energy", "tags")}
            for c in _all_cards().values()]


def card(name: str) -> dict | None:
    return _all_cards().get(name)


def card_text(name: str) -> str:
    c = _all_cards().get(name)
    if not c:
        return ""
    head = f"【{c['category']}/{c['name']}】{c['one_liner']}\n适用: {c['use_for']}\n时长: {c['duration']}  能量: {c['energy']}"
    return head + "\n\n" + c["body"][:6000]


def stats() -> dict:
    cards = _all_cards()
    cats: dict[str, int] = {}
    for c in cards.values():
        cats[c["category"]] = cats.get(c["category"], 0) + 1
    return {"total": len(cards), "categories": cats, "root": str(recipe_root())}
