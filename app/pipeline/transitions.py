"""转场库：镜头之间怎么切换。

设计（吸收 dashi 的转场配方 + mg-styles-15 的「元素驱动」铁律）：
1. 每个转场是纯函数 transition(ctx, W, H, t, start, style, extra) -> bool
   - t 是全局时间（秒），start 是转场开始时刻
   - 返回 True 表示转场结束，渲染器可以切到下一镜
2. 转场由「材料驱动」：纸会撕、屏幕会故障、线条会生长。不用 crossfade。
3. 铁律（dashi 盲测验证）：转场 0.3~0.7 秒，卡句界，全片至少 3 种、同种不连用 3 次。

颜色从 style 取，转场本身不写死颜色，保证同一转场适配不同风格。
"""

from __future__ import annotations

from typing import Any

from .styles import Style


def _seg(t: float, start: float, dur: float) -> float:
    """归一化进度 0~1。"""
    if dur <= 0:
        return 1.0 if t >= start else 0.0
    p = (t - start) / dur
    return 0.0 if p < 0 else (1.0 if p > 1 else p)


def _ease_in_out(p: float) -> float:
    return p * p * (3 - 2 * p)


def _ease_out(p: float) -> float:
    return 1 - (1 - p) ** 3


def crt_on(ctx: Any, W: int, H: int, t: float, start: float, style: Style, **_k: Any) -> bool:
    """CRT 开机：先压成一条亮线，再上下展开成满屏（黑底科技/霓虹风）。"""
    dur = 0.5
    p = _seg(t, start, dur)
    if p <= 0:
        return False
    ctx.save()
    # 用风格背景色盖住，模拟屏幕从一条线展开
    ctx.fillStyle = style.bg
    ctx.fillRect(0, 0, W, H)
    # 亮线在中间，向上下扩展
    h = max(2, H * _ease_out(p))
    y0 = (H - h) / 2
    # 亮线本体
    grad = ctx.createLinearGradient(0, y0, 0, y0 + h)
    grad.addColorStop(0, style.accent)
    grad.addColorStop(0.5, style.ink)
    grad.addColorStop(1, style.accent)
    ctx.fillStyle = grad
    ctx.fillRect(0, y0, W, h)
    ctx.restore()
    return p >= 1


def scan_wipe(ctx: Any, W: int, H: int, t: float, start: float, style: Style, **_k: Any) -> bool:
    """扫描线扫过：一条横线从上往下扫，扫过处露出下一镜（HUD 风）。"""
    dur = 0.6
    p = _seg(t, start, dur)
    if p <= 0:
        return False
    y = H * _ease_in_out(p)
    ctx.save()
    ctx.fillStyle = style.bg
    ctx.fillRect(0, 0, W, H)
    # 扫描线本体
    ctx.fillStyle = style.accent
    ctx.globalAlpha = 0.9
    ctx.fillRect(0, y - 1, W, 3)
    # 扫过的区域用背景色盖住（模拟旧画面被扫掉）
    ctx.globalAlpha = 1.0
    ctx.fillStyle = style.bg
    ctx.fillRect(0, 0, W, max(0, y - 3))
    ctx.restore()
    return p >= 1


def glitch(ctx: Any, W: int, H: int, t: float, start: float, style: Style, **_k: Any) -> bool:
    """故障撕裂：画面随机切条（霓虹/赛博风）。0.2 秒内，克制使用。"""
    dur = 0.35
    p = _seg(t, start, dur)
    if p <= 0:
        return False
    ctx.save()
    ctx.fillStyle = style.bg
    ctx.fillRect(0, 0, W, H)
    # 确定性伪随机（基于时间，不用 Math.random，保证纯函数可复现）
    seed = int(t * 30)
    import math
    for i in range(6):
        r = (math.sin(seed * 12.9898 + i * 78.233) * 43758.5453) % 1
        if r < 0.5:
            continue
        y = int((r * 100 % 1) * H)
        hh = 3 + int((math.sin(seed + i) * 43758.5453) % 1 * 18)
        shift = int((math.cos(seed + i * 2) * 43758.5453) % 1 * 40) - 20
        ctx.fillStyle = style.accent if i % 2 == 0 else style.ink
        ctx.globalAlpha = 0.7
        ctx.fillRect(shift, y, W, hh)
    ctx.globalAlpha = 1.0
    ctx.restore()
    return p >= 1


def stroke_grow(ctx: Any, W: int, H: int, t: float, start: float, style: Style, **_k: Any) -> bool:
    """描线生长：一条线从左到右画出（线稿风）。"""
    dur = 0.5
    p = _seg(t, start, dur)
    if p <= 0:
        return False
    ctx.save()
    ctx.fillStyle = style.bg
    ctx.fillRect(0, 0, W, H)
    y = H // 2
    ctx.strokeStyle = style.ink
    ctx.lineWidth = 2
    ctx.beginPath()
    ctx.moveTo(0, y)
    ctx.lineTo(W * _ease_out(p), y)
    ctx.stroke()
    ctx.restore()
    return p >= 1


def hard_cut(ctx: Any, W: int, H: int, t: float, start: float, style: Style, **_k: Any) -> bool:
    """硬切：直接切，无过渡（包豪斯/像素风）。"""
    dur = 0.1
    p = _seg(t, start, dur)
    if p <= 0:
        return False
    ctx.save()
    ctx.fillStyle = style.bg
    ctx.fillRect(0, 0, W, H)
    ctx.restore()
    return p >= 1


def wipe(ctx: Any, W: int, H: int, t: float, start: float, style: Style, **_k: Any) -> bool:
    """左到右色块擦除（像素风/扁平风）。"""
    dur = 0.4
    p = _seg(t, start, dur)
    if p <= 0:
        return False
    ctx.save()
    ctx.fillStyle = style.bg
    ctx.fillRect(0, 0, W, H)
    ctx.fillStyle = style.accent
    ctx.fillRect(0, 0, W * _ease_in_out(p), H)
    ctx.restore()
    return p >= 1


def slide_wipe(ctx: Any, W: int, H: int, t: float, start: float, style: Style, **_k: Any) -> bool:
    """版面推移：整块内容水平推走（报刊/杂志风）。"""
    dur = 0.5
    p = _seg(t, start, dur)
    if p <= 0:
        return False
    ctx.save()
    ctx.fillStyle = style.bg
    ctx.fillRect(0, 0, W, H)
    offset = int(W * _ease_in_out(p))
    # 旧画面被推走，露出下一镜的背景
    ctx.fillStyle = style.bg2
    ctx.fillRect(0, 0, offset, H)
    ctx.fillStyle = style.accent
    ctx.fillRect(offset - 2, 0, 2, H)
    ctx.restore()
    return p >= 1


# 转场注册表
TRANSITIONS: dict[str, Any] = {
    "crt_on": crt_on,
    "scan_wipe": scan_wipe,
    "glitch": glitch,
    "stroke_grow": stroke_grow,
    "hard_cut": hard_cut,
    "wipe": wipe,
    "slide_wipe": slide_wipe,
}


def get_transition(name: str) -> Any:
    return TRANSITIONS.get(name, hard_cut)


def pick_transition(style: Style, index: int) -> str:
    """按镜头序号从风格适配的转场里轮换选取，避免同种连用。

    规则（dashi）：全片至少 3 种、同种不连用 3 次。这里简单实现为：
    从风格的 transitions 列表里按 index 循环取，天然保证相邻不同（列表内不重复）。
    """
    if not style.transitions:
        return "hard_cut"
    return style.transitions[index % len(style.transitions)]
