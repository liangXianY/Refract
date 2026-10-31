"""本地生成一个 demo 产物，用于视觉检查（不依赖服务、不消耗 token）。

用法：python tests/preview.py
产出：data/artifacts/demo.html
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.mock import mock_body, mock_brief  # noqa: E402
from app.pipeline.renderer import save_artifact  # noqa: E402

CASES = {
    "demo": ("给后端工程师讲清楚向量数据库到底解决了什么问题", "wechat"),
    "demo-short": ("反思机制在 Agent 循环里为什么必须分三档", "xiaohongshu"),
    "demo-long": ("从工程角度复盘我们为什么把动画渲染从服务端改到前端，以及这个决策背后的成本权衡", "video"),
}

if __name__ == "__main__":
    for name, (text, platform) in CASES.items():
        brief = mock_brief(text, platform)
        path = save_artifact(brief, mock_body(brief), name, settings.data_dir, "mock · 预览")
        print(f"{name:12s} {platform:12s} {path}")
