"""风格库：把「视频风格」从枚举升级为配置对象。

之前 style 是 main.py 里的 6 个枚举字符串，但 renderer 根本不读它，等于摆设。
现在每个风格是一个 dict：配色 + 字体 + 质感 + 它适配的转场（转场函数在 transitions.py）。

设计原则（吸收 mg-styles-15 / dashi / lemo 的方法论）：
1. 风格 = 数据，不是 prompt。加风格只加一个 dict，不改渲染逻辑。
2. 风格与转场是映射关系：每个风格声明它用哪几种转场。
3. 只做代码能渲染的（几何/排版/像素/HUD 类），材质类（油画/水彩）不进，
   那需要 AI 生成底图，纯 canvas 画不出笔触。

字段说明：
- bg / bg2 : 背景色（主/次，用于渐变或分层）
- ink      : 主文字色
- dim      : 次要文字色
- accent   : 强调色
- accent2  : 第二强调色
- font     : "mono" | "sans"
- radius   : 圆角风格（0=锐利, 中, 大）
- transitions : 该风格适配的转场名（对应 transitions.py 的 TRANSITIONS）
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Style:
    id: str
    name: str
    bg: str
    bg2: str
    ink: str
    dim: str
    accent: str
    accent2: str
    font: str
    radius: int
    transitions: tuple[str, ...] = field(default_factory=tuple)


STYLES: dict[str, Style] = {
    "hud": Style(
        id="hud",
        name="赛博 HUD",
        bg="#05080c",
        bg2="#0a1119",
        ink="#e8f6f2",
        dim="#3d5866",
        accent="#22d3ee",
        accent2="#4ade80",
        font="mono",
        radius=6,
        transitions=("crt_on", "scan_wipe", "glitch"),
    ),
    "line": Style(
        id="line",
        name="线稿",
        bg="#f8f7f4",
        bg2="#eeede8",
        ink="#16130f",
        dim="#8a857c",
        accent="#c8452b",
        accent2="#16130f",
        font="sans",
        radius=4,
        transitions=("stroke_grow", "hard_cut"),
    ),
    "pixel": Style(
        id="pixel",
        name="像素 8-bit",
        bg="#1a1c2c",
        bg2="#232842",
        ink="#f4f4f4",
        dim="#566c86",
        accent="#ffcd75",
        accent2="#73eff7",
        font="mono",
        radius=0,
        transitions=("hard_cut", "wipe"),
    ),
    "geo": Style(
        id="geo",
        name="几何构成",
        bg="#f1e9da",
        bg2="#e6dcc8",
        ink="#16130f",
        dim="#6f6a60",
        accent="#e03c31",
        accent2="#1e4fa3",
        font="sans",
        radius=0,
        transitions=("hard_cut", "slide_wipe"),
    ),
    "editorial": Style(
        id="editorial",
        name="杂志编辑",
        bg="#faf7f2",
        bg2="#f1ece3",
        ink="#191714",
        dim="#8a8378",
        accent="#c2492b",
        accent2="#1f4e5f",
        font="sans",
        radius=2,
        transitions=("hard_cut", "slide_wipe"),
    ),
    "blueprint": Style(
        id="blueprint",
        name="工程蓝图",
        bg="#0d3b66",
        bg2="#0a2f52",
        ink="#eaf2fa",
        dim="#7fa3c4",
        accent="#ffd166",
        accent2="#8ecae6",
        font="mono",
        radius=0,
        transitions=("scan_wipe", "hard_cut"),
    ),
    "warm": Style(
        id="warm",
        name="暖调生活",
        bg="#fdf6ee",
        bg2="#f9ead6",
        ink="#4a3728",
        dim="#a08c76",
        accent="#e07a5f",
        accent2="#81b29a",
        font="sans",
        radius=10,
        transitions=("slide_wipe", "hard_cut"),
    ),
    "mono": Style(
        id="mono",
        name="黑白极简",
        bg="#ffffff",
        bg2="#f2f2f2",
        ink="#000000",
        dim="#999999",
        accent="#000000",
        accent2="#d1453b",
        font="sans",
        radius=0,
        transitions=("hard_cut",),
    ),
    # 占位：后续可继续加（霓虹/扁平等），只加 dict 不改逻辑
}

DEFAULT_STYLE = "hud"


def get_style(style_id: str | None) -> Style:
    """style_id 为空或未知时回退到默认风格。"""
    if style_id in STYLES:
        return STYLES[style_id]
    return STYLES[DEFAULT_STYLE]


def list_styles() -> list[dict[str, object]]:
    """给前端/接口用的风格清单（风格卡）。"""
    return [
        {"id": s.id, "name": s.name, "accent": s.accent, "bg": s.bg}
        for s in STYLES.values()
    ]
