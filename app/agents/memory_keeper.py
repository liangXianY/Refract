"""MemoryKeeper：记忆的读写门面。

统一口径，不让每个 agent 各写各的——这是「记忆要一致」的前提。
"""

from __future__ import annotations

from ..pipeline.memory import MemoryStore


class MemoryKeeper:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def persona_profile(self) -> str:
        """给 Writer 的人设卡。"""
        return self.store.style_profile()

    def reflect_hint(self) -> str:
        """给 Reflector 的历史结构参考。"""
        return self.store.reflect_hint()

    def remember(self, key: str, value: str, kind: str = "general") -> None:
        self.store.set(key, value, kind)

    def recall(self, kind: str | None = None) -> list[dict]:
        return [
            {"key": i.key, "value": i.value, "kind": i.kind}
            for i in self.store.list_items(kind)
        ]

    def record_outcome(self, structure: str, score: float) -> None:
        """数据回流：结构 + 得分写进长期记忆。"""
        if structure:
            self.store.record_structure(structure, score)
