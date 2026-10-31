"""视频渲染器单元测试：不调 API，用 mock brief 直接渲染 4 种风格，验证输出。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pipeline.storyboard import storyboard
from app.pipeline.styles import STYLES, get_style, list_styles
from app.pipeline.transitions import pick_transition, get_transition
from app.pipeline.video import render_video
from app.schemas import ContentBrief, Fact, Section


def make_brief() -> ContentBrief:
    return ContentBrief(
        topic="从一句话到一条能发的视频",
        angle="内容生产流水线",
        hook="输入随便给，输出随便要。",
        platform="video",
        outline=[
            Section(heading="三类输入归一化", points=["一句话需求", "已有文案", "一个链接"]),
            Section(heading="Plan-Execute-Reflect", points=["规划", "执行", "反思"]),
            Section(heading="三种产物", points=["公众号长文", "动画视频"]),
        ],
        facts=[
            Fact(claim="召回率从 64% 提升到 83%", source="评测集实测"),
            Fact(claim="单篇产出从 40 分钟压到 3 分钟", source=None),
        ],
        cta="来 GitHub 看源码",
    )


def main() -> None:
    brief = make_brief()
    body = "## 测试正文\n\n这是一段正文。\n\n## 第二节\n\n更多内容。"

    print("=== 风格清单 ===")
    for s in list_styles():
        print(" ", s)

    print("\n=== 分镜 ===")
    shots = storyboard(brief, body)
    for s in shots:
        print(f"  {s.shot_id} [{s.task}] {s.screen_text} ({s.duration}s)")

    print("\n=== 转场映射 ===")
    for sid, style in STYLES.items():
        trans = [pick_transition(style, i) for i in range(len(shots))]
        print(f"  {sid}: {style.name} -> {trans}")

    out_dir = Path(__file__).resolve().parent.parent / "data" / "test_videos"
    out_dir.mkdir(parents=True, exist_ok=True)

    print("\n=== 渲染 4 种风格 ===")
    for sid in STYLES:
        html_str = render_video(brief, body, sid, "mock 测试")
        path = out_dir / f"{sid}.html"
        path.write_text(html_str, encoding="utf-8")
        print(f"  {sid}: {len(html_str)} 字节 -> {path.name}")

    # 未知风格回退
    fb = get_style("not_exist")
    print(f"\n未知风格回退到: {fb.name}")


if __name__ == "__main__":
    main()
