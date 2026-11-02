"""配色/对比度工具与闸门的单测（不调真实 API）。

覆盖线上真实翻车场景：浅色底风格（mono/line/geo/warm）下模型写白字 →
整片文字看不见。这层要保证「白字白底」在出片前就被拦住。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pipeline.remotion_video import (  # noqa: E402
    _FALLBACK_TSX_TMPL,
    _check_contrast,
    SHOT_SYSTEM,
    contrast_ratio,
    luminance,
    text_color_for,
)
from app.pipeline.styles import STYLES  # noqa: E402

ok = True


def check(name, cond, detail=""):
    global ok
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        ok = False


print("=== 1. 亮度计算 ===")
check("纯白亮度 ≈ 1.0", abs(luminance("#ffffff") - 1.0) < 0.01, f"{luminance('#ffffff'):.3f}")
check("纯黑亮度 ≈ 0", luminance("#000000") < 0.01, f"{luminance('#000000'):.3f}")
check("3 位简写支持", abs(luminance("#fff") - 1.0) < 0.01)

print("=== 2. 对比度计算 ===")
check("白对白 = 1:1", abs(contrast_ratio("#ffffff", "#ffffff") - 1.0) < 0.01)
check("白对黑 = 21:1", abs(contrast_ratio("#ffffff", "#000000") - 21.0) < 0.1,
      f"{contrast_ratio('#ffffff', '#000000'):.1f}")

print("=== 3. 文字色推导（所有风格都要够对比度）===")
for st in STYLES.values():
    text = text_color_for(st.bg)
    ratio = contrast_ratio(text, st.bg)
    check(f"{st.id:10s} bg={st.bg} → 文字 {text} 对比度 {ratio:.1f}:1", ratio >= 7.0)

print("=== 4. 闸门拦截「白字白底」===")
white_text_tsx = "const A = () => <div style={{ color: '#fff' }}>标题</div>;"
fails = _check_contrast(white_text_tsx, "#ffffff")
check("白底 + 白字 → 报错", len(fails) > 0, fails[0] if fails else "（没报！）")

fails2 = _check_contrast(white_text_tsx, "#05080c")
check("深底 + 白字 → 放行", len(fails2) == 0)

black_text_tsx = "const A = () => <div style={{ color: '#111' }}>标题</div>;"
check("深底 + 黑字 → 报错", len(_check_contrast(black_text_tsx, "#05080c")) > 0)
check("白底 + 黑字 → 放行", len(_check_contrast(black_text_tsx, "#ffffff")) == 0)

named = "const A = () => <div style={{ color: 'white' }}>标题</div>;"
check("white 关键字也拦", len(_check_contrast(named, "#fdf6ee")) > 0)

var_color = "const A = () => <div style={{ color: accent }}>标题</div>;"
check("用 accent 变量 → 不误报", len(_check_contrast(var_color, "#ffffff")) == 0)

print("=== 5. 兜底模板不再写死白字 ===")
check("模板已改成占位符", "@@TEXT@@" in _FALLBACK_TSX_TMPL)
check("模板里没有写死的 #fff", "color: '#fff'" not in _FALLBACK_TSX_TMPL)

for bg in ("#ffffff", "#f8f7f4", "#05080c"):
    rendered = _FALLBACK_TSX_TMPL.replace("@@N@@", "01").replace("@@TEXT@@", text_color_for(bg))
    import re
    m = re.search(r"color:\s*'(#[0-9a-fA-F]+)'", rendered)
    tc = m.group(1) if m else "?"
    check(f"模板在 bg={bg} 上文字色 {tc} 可见", contrast_ratio(tc, bg) >= 7.0)

print("  （以上为基础检查，端到端验证见下）")


# ---------------------------------------------------------------- 端到端：闸门在真实流程里生效

GOOD_TSX = """import React from 'react';
import { AbsoluteFill, interpolate, useCurrentFrame } from 'remotion';
import { easeOutCubic } from '../lib/refract';

export default function Shot01({ title, lines, accent, accent2, bg }: { title: string; lines: string[]; accent: string; accent2: string; bg: string }) {
  const frame = useCurrentFrame();
  const p = easeOutCubic(interpolate(frame, [0, 18], [0, 1], { extrapolateRight: 'clamp' }));
  return (
    <AbsoluteFill style={{ background: bg, justifyContent: 'center', alignItems: 'center', padding: 96 }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 32, alignItems: 'center' }}>
        <div style={{ fontSize: 88, fontWeight: 800, color: '#14161a', opacity: p }}>{title}</div>
        <div style={{ width: 200 * p, height: 6, background: accent2, borderRadius: 3 }} />
      </div>
    </AbsoluteFill>
  );
}
"""

BAD_TSX = GOOD_TSX.replace("color: '#14161a'", "color: '#ffffff'")  # 白字白底


class FakeLLM:
    """假模型：按顺序吐预设的 TSX，不联网、不计费。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0
        self.prompts = []

    async def complete(self, **kw):
        self.prompts.append(kw.get("user", ""))
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return reply, None


def _make_shot():
    from app.pipeline.remotion_video import Shot
    return Shot(idx=1, card="", category="", duration_s=4.0,
                narration="测试", screen_text="测试标题", title="测试标题")


def _make_plan(style_id):
    from types import SimpleNamespace
    return SimpleNamespace(style=style_id, palette=["#e03c31", "#1f3a5f"])


def _make_brief():
    from types import SimpleNamespace
    return SimpleNamespace(topic="测试主题", angle="角度", hook="钩子",
                           outline=[], facts=[], cta="行动")


print("=== 6. 端到端：白字白底会被拦下并重试 ===")
import asyncio  # noqa: E402
from app.pipeline.remotion_video import _impl_shot  # noqa: E402

# mono 风格是纯白底 (#ffffff)
llm1 = FakeLLM([BAD_TSX, GOOD_TSX])
shot1 = asyncio.run(_impl_shot(llm1, _make_shot(), _make_plan("mono"), _make_brief(), []))
check("第一次白字白底被拦 → 触发了重试", llm1.calls == 2, f"模型被调用 {llm1.calls} 次")
check("重试合格 → 用了重试版本，没落模板", not shot1.used_fallback)
check("最终代码里是深色字", "#14161a" in shot1.tsx)

print("=== 7. 两次都白字白底 → 落模板，模板文字可见 ===")
llm2 = FakeLLM([BAD_TSX, BAD_TSX])
shot2 = asyncio.run(_impl_shot(llm2, _make_shot(), _make_plan("mono"), _make_brief(), []))
check("两次都被拦 → 落模板", shot2.used_fallback)
check("模板不再用白字", "color: '#ffffff'" not in shot2.tsx and "'#fff'" not in shot2.tsx)
m2 = __import__("re").search(r"color:\s*'(#[0-9a-fA-F]+)'", shot2.tsx)
tpl_color = m2.group(1) if m2 else "?"
check(f"模板文字色 {tpl_color} 在白底上可见",
      contrast_ratio(tpl_color, "#ffffff") >= 7.0 if tpl_color != "?" else False)

print("=== 8. 深色风格不该被误杀 ===")
dark_ok = GOOD_TSX.replace("color: '#14161a'", "color: '#f7f9fb'")
llm3 = FakeLLM([dark_ok])
shot3 = asyncio.run(_impl_shot(llm3, _make_shot(), _make_plan("hud"), _make_brief(), []))
check("hud(深底) + 浅色字 → 一次过", llm3.calls == 1, f"模型被调用 {llm3.calls} 次")
check("没落模板", not shot3.used_fallback)

print("=== 9. 提示词真的把文字色和排版规矩喂进去了 ===")
prompt = llm1.prompts[0] if llm1.prompts else ""
check("参数块里带了「正文字色」", "正文字色" in prompt)
check("提示词禁止绝对定位叠字", "position:'absolute'" in SHOT_SYSTEM or "position: 'absolute'" in SHOT_SYSTEM or "absolute" in SHOT_SYSTEM)
check("提示词要求用给定文字色", "正文字色" in SHOT_SYSTEM)

print("=== 10. 布局闸门：绝对定位叠字 ===")
from app.pipeline.remotion_video import _check_layout  # noqa: E402

flex_ok = ("const A = () => <AbsoluteFill style={{ justifyContent: 'center' }}>"
           "<div style={{ display: 'flex', flexDirection: 'column', gap: 24 }}>"
           "<span>{title}</span></div></AbsoluteFill>;")
check("flex 纵向流式 → 放行", len(_check_layout(flex_ok)) == 0)

one_abs = ("const A = () => <AbsoluteFill>"
           "<div style={{ position: 'absolute', top: 0, left: 0, width: 100, height: 2, background: accent }} />"
           "<div style={{ display: 'flex' }}>{title}</div></AbsoluteFill>;")
check("单个 absolute 装饰层 → 放行", len(_check_layout(one_abs)) == 0)

bad_layout = """
const A = () => <>
  <div style={{ position: 'absolute', top: 100 }}>第一行文字</div>
  <div style={{ position: 'absolute', top: 200 }}>第二行文字</div>
  <div style={{ position: 'absolute', top: 200 }}>第三行文字</div>
  <div style={{ position: 'absolute', top: 300 }}>{title}</div>
</>;
"""
fails_layout = _check_layout(bad_layout)
check("4 个绝对定位文字块 → 判叠字", len(fails_layout) > 0,
      fails_layout[0][:60] if fails_layout else "（没报！）")

print("=== 11. 布局闸门接进了 check_shot_tsx ===")
from app.pipeline.remotion_video import check_shot_tsx  # noqa: E402

bad_shot = _make_shot()
all_fails = check_shot_tsx(bad_layout, bad_shot)
check("叠字代码整体判废", any("叠字" in f or "绝对定位" in f for f in all_fails),
      next((f for f in all_fails if "叠字" in f or "绝对定位" in f), "（没进 check_shot_tsx！）"))

good_fails = check_shot_tsx(GOOD_TSX, bad_shot)
check("合格代码不受影响", len(good_fails) == 0, good_fails)

print()
print("端到端全部通过 ✅" if ok else "仍有失败项 ❌")
sys.exit(0 if ok else 1)
