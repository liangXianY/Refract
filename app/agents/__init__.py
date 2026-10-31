"""多 Agent 层。

拆分依据是业务边界，不是代码行数：
- intent      意图识别（决定派谁）
- orchestrator 编排中枢
- researcher  抓取取证
- writer      写文案
- director    出分镜/视频
- memory_keeper 记忆读写

各 agent 共享 ContentBrief 这个中心抽象，不各自理解一套。
"""

from __future__ import annotations

from .intent import Intent, detect, extract_url
from .memory_keeper import MemoryKeeper
from .orchestrator import OrchestrationResult, route, run

__all__ = [
    "Intent",
    "detect",
    "extract_url",
    "MemoryKeeper",
    "OrchestrationResult",
    "route",
    "run",
]
