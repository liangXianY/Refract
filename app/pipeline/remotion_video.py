"""视频方案 v2：video-shotcraft 式 Remotion 管线，替换整份 Canvas HTML 直写。

架构（对齐 video-shotcraft 的六阶段工作流里可自动化的部分）：

  1. 镜头映射   LLM 从 124 张配方卡索引里选卡 + 填参（文案/时长/SFX 表）
  2. 逐镜头实现  LLM 读选中卡的全文（动效核心/参数表/已知坑）写该镜头的 TSX 组件
  3. 工程组装   Python 确定性拼装完整 Remotion 工程（Root/Composition/Audio/lib）
  4. 闸门       逐镜头静态检查 + 工程级完整性检查，不过回炉一次，再不过落模板镜头
  5. 交付       工程目录 + 确定性合成的 SFX wav；不出 mp4，预览走 remotion studio

为什么换掉「整份 HTML 直写」
------------------------
旧方案（opus_video.py）让模型一口气吐几百行 Canvas 代码，创意上限高但质量方差大，
且画面/声音全靠模型自觉。新方案把「创意决策」（选哪张卡、什么节奏）和「工程骨架」
（时序、音轨、注册）分开：骨架由代码保证 100% 正确，模型只在卡参数约束下写单镜头，
每次输出小、闸门粒度细、可逐镜头回炉——这正是 video-shotcraft「配方卡+确定性渲染」
的思想：能用代码确定的绝不让模型自由发挥。

模型路由：本地测试走 deepseek-chat（一次一镜头，8k token 足够），
上线配了 CCTQ 凭据自动切 Opus——路由表在 models.py，本模块不关心。
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ..llm import LLMClient
from ..schemas import ContentBrief
from .director import VideoPlan
from . import recipes

FPS = 30
WIDTH, HEIGHT = 1920, 1080
SFX_KINDS = ("pop", "whoosh", "hit", "chord", "sparkle", "riser")

# ---------------------------------------------------------------- 提示词

MAPPING_SYSTEM = """你是宣传片分镜师，为一条产品介绍短片做「功能→镜头」的映射。

你会拿到：配方卡索引（每张卡有名字/一句话/适用场景/时长/能量）、内容简报、
已定的视觉方案（配色/总时长）。从中选出 5~8 张卡，按能量曲线排布：
开场低能量起步，中段推进，收尾给峰值然后安静落定。

硬规则：
- 卡名必须从索引里原样选，不许自创；同一张卡全片只当一次主角
- 每个镜头 3~7 秒（文字越多的镜头给越长），全部镜头时长之和约等于给定总时长（±20%）。
  **文字可读性优先**：screen_text 里的字，按「1 秒读 2-3 个字」估算停留时长，
  字没读完就转场是硬伤——宁可镜头少一点、每个镜头长一点，也不要赶节奏。
- screen_text 是这一镜屏幕上出现的短文字（≤20 字，从简报要点里提炼，禁止编造数据）
- sfx 是该镜头内的音效表：t 为镜头内秒数，type 只能是
  pop(登场)/whoosh(转场移动)/hit(强调)/chord(收尾)/sparkle(点缀)/riser(蓄势)，
  每镜 1~3 个，别滥用；镜头衔接处前一镜末尾或后一镜开头给一个 whoosh
- 最后一个镜头用 outro 类卡收尾落定

只输出 JSON：{"shots": [{"card": "卡名", "category": "类别", "duration_s": 4.5,
"narration": "这镜讲什么(20字内)", "screen_text": "屏幕文字",
"sfx": [{"t": 0.2, "type": "pop"}]}]}"""

SHOT_SYSTEM = """你是 Remotion 动效工程师，按配方卡实现一个镜头组件。

你会拿到：配方卡全文（动效核心/参数表/声音/已知坑——这些是硬约束，参数不得降档）、
这个镜头的文案与配色、组件契约。

组件契约（违反=废件）：
- 默认导出一个 React 函数组件，签名: ({ title, lines, accent, accent2, bg }: ShotProps)
- 只准 import: remotion（AbsoluteFill/useCurrentFrame/useVideoConfig/interpolate/spring/Easing）、
  react、../lib/refract（rand/easeOutCubic/easeInOutCubic/Caption）
- 确定性：禁 Math.random/Date.now/setTimeout/setInterval；随机一律 rand(seed)（lib 已给）
- 用 useCurrentFrame + interpolate/spring 驱动一切动画，fps=30，组件高度被卡时长约束
- 画面必须有真实中文文字：title 必须渲染，lines 可选择性渲染
- 严格使用给定配色（bg 底、accent 主强调、accent2 次强调），不自创颜色
- 遵守卡的节奏铁律：关键文字落定后 hold 满，批量入场靠运动不靠逐个发光，
  单点高质量光效可做；"已知坑"里点名的问题绝对不要再犯
- 组件根部用 <AbsoluteFill style={{background: bg}}> 包裹
- 代码 30~160 行，写完整，最后一个字符是组件闭合的 }

只输出 TSX 代码，不要解释、不要 Markdown 围栏。"""

SHOT_REPAIR = """上一版组件没通过静态检查，问题：
{failures}

重写整个组件，修正以上问题。仍然只输出 TSX 代码。"""


# ---------------------------------------------------------------- 数据结构

@dataclass
class ShotCue:
    t: float
    type: str


@dataclass
class Shot:
    idx: int                 # 从 1 起
    card: str                # 卡名；内置模板镜头为 ""
    category: str
    duration_s: float
    narration: str
    screen_text: str
    title: str
    lines: list[str] = field(default_factory=list)
    sfx: list[ShotCue] = field(default_factory=list)
    tsx: str = ""            # 组件源码
    used_fallback: bool = False
    gate_failures: list[str] = field(default_factory=list)


@dataclass
class GateResult:
    ok: bool
    failures: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


@dataclass
class RemotionResult:
    project_dir: Path
    shots: list[Shot]
    gate: GateResult
    provider_label: str
    model: str
    duration_s: float
    attempts: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------- 静态工程骨架

LIB_TSX = """// Refract 确定性动效库（与 Python 侧 SFX 合成器同源 mulberry32）
import React from 'react';

export function rand(seed: number): number {
  let t = (seed + 0x6d2b79f5) >>> 0;
  t = Math.imul(t ^ (t >>> 15), t | 1);
  t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
  return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
}

export const easeOutCubic = (p: number): number => 1 - Math.pow(1 - p, 3);
export const easeInOutCubic = (p: number): number =>
  p < 0.5 ? 4 * p * p * p : 1 - Math.pow(-2 * p + 2, 3) / 2;

export const Caption: React.FC<{
  text: string; accent?: string; color?: string; size?: number;
  y?: string | number; weight?: number;
}> = ({ text, accent, color, size = 64, y = '78%', weight = 700 }) => (
  <div style={{
    position: 'absolute', left: 0, right: 0, bottom: y, textAlign: 'center',
    fontFamily: '"PingFang SC", "Microsoft YaHei", system-ui, sans-serif',
    fontSize: size, fontWeight: weight, color: color || '#fff',
    textShadow: `0 2px 24px ${accent || '#22d3ee'}55`, letterSpacing: 2,
  }}>{text}</div>
);
"""

PACKAGE_JSON = {
    "name": "refract-promo",
    "private": True,
    "scripts": {"studio": "remotion studio", "upgrade": "remotion upgrade"},
    # Remotion 全家桶钉同一版本（官方要求）：4.0.0（2022 初版）的 Audio 类型
    # 定义有 bug（报 placeholder/onPointerEnterCapture 缺失），4.0.539 实测干净
    "dependencies": {
        "@remotion/cli": "4.0.539",
        "@remotion/player": "4.0.539",
        "react": "18.3.1",
        "react-dom": "18.3.1",
        "remotion": "4.0.539",
    },
    "devDependencies": {"typescript": "^5.5.0", "@types/react": "^18.3.3"},
}

TSCONFIG = {
    "compilerOptions": {
        "target": "ES2020", "module": "ESNext", "moduleResolution": "Bundler",
        "jsx": "react-jsx", "strict": True, "skipLibCheck": True,
        "noEmit": True, "esModuleInterop": True, "lib": ["DOM", "ES2020"],
    },
    "include": ["src"],
}

REMOTION_CONFIG = """import { Config } from '@remotion/cli/config';
Config.setVideoImageFormat('jpeg');
Config.setOverwriteOutput(true);
"""

INDEX_TS = """import { registerRoot } from 'remotion';
import { RemotionRoot } from './Root';

registerRoot(RemotionRoot);
"""

# 在线播放入口：用 @remotion/player 渲染干净播放器（自动播放、带控制条），
# 而不是 Remotion Studio（带时间线/图层的编辑器）。bundle 这个入口，
# 用户打开网页就是「像看视频一样」的成片，不是剪辑软件。
PLAYER_TSX = """import React from 'react';
import { registerRoot } from 'remotion';
import { Player } from '@remotion/player';
import { Promo, FPS, TOTAL_FRAMES } from './Root';

const PlayerPage: React.FC = () => (
  <div style={{
    width: '100%', height: '100vh', margin: 0, display: 'flex',
    alignItems: 'center', justifyContent: 'center', background: '#0a0a0a'
  }}>
    <Player
      component={Promo}
      durationInFrames={TOTAL_FRAMES}
      compositionWidth={1920}
      compositionHeight={1080}
      fps={FPS}
      controls
      loop
      autoPlay
      style={{ width: '100%', maxWidth: 1280 }}
    />
  </div>
);

registerRoot(PlayerPage);
"""

README_TMPL = """# Refract 宣传片工程（video-shotcraft 式 Remotion 管线产出）

- 规格：{width}x{height} @ {fps}fps，总时长 {duration:.1f}s，{n} 个镜头
- 镜头卡映射：{cards}
- 音效：{sfx_n} 段（本地确定性合成，public/audio/sfx/）
- 本工程**不出 mp4**（按交付约定），预览用 Remotion Studio：

```
npm install
npm run studio      # 浏览器打开 http://localhost:3000，选 "Refract" 合成播放
```

## 镜头表

| # | 卡 | 时长 | 屏幕文字 |
|---|---|---|---|
{shot_rows}

## 确定性保证

随机一律走 src/lib/refract.ts 的 rand(seed)（mulberry32），同工程重放逐帧一致；
SFX wav 由 Python 按同源算法合成，画面与声音的时间轴都由 Root.tsx 统一编排。
"""

BAT_TMPL = """@echo off
cd /d %~dp0
if not exist node_modules (npm install)
npx remotion studio
"""


# ---------------------------------------------------------------- 闸门

_FORBIDDEN = re.compile(r"Math\.random|Date\.now|setInterval|setTimeout|fetch\(|require\(")
_IMPORT_OK = re.compile(
    r"^\s*import\s+(?:type\s+)?[^;]*from\s+['\"](remotion|react|\.\./lib/refract)['\"];?\s*$",
    re.M,
)
_IMPORT_ANY = re.compile(r"^\s*import\s+[^;]*from\s+['\"]([^'\"]+)['\"]", re.M)


def _braces_balanced(code: str) -> bool:
    for op, cl in (("{", "}"), ("(", ")"), ("[", "]")):
        # 粗粒度：字符串里也可能有括号，但两侧同源污染，工程上够用
        if code.count(op) != code.count(cl):
            return False
    return True


# lib/refract.tsx 里组件的真实 props。模型爱给它编不存在的属性（实测传过 opacity/
# fontSize/letterSpacing/texts 数组），tsc 会炸 TS2322。
_LIB_COMPONENT_PROPS = {
    "Caption": {"text", "accent", "color", "size", "y", "weight"},
}

_KEY_RE = re.compile(r"([A-Za-z_$][\w$]*)\s*:")


def _dup_style_keys(tsx: str) -> list[str]:
    """style={{...}} 内层对象的顶层重复 key（tsc TS1117，运行时后者覆盖前者）。"""
    dups: list[str] = []
    for m in re.finditer(r"style=\{\{", tsx):
        i = m.end()  # 内层对象的 {
        depth, j = 1, i
        while j < len(tsx) and depth:
            if tsx[j] == "{":
                depth += 1
            elif tsx[j] == "}":
                depth -= 1
            j += 1
        body, seen = tsx[i : j - 1], set()
        for km in _KEY_RE.finditer(body):
            key = km.group(1)
            if key in seen:
                dups.append(key)
            seen.add(key)
    return sorted(set(dups))


def _bad_component_props(tsx: str) -> list[str]:
    """lib 组件被传了不存在的 props（tsc TS2322）。"""
    bad: list[str] = []
    for comp, allowed in _LIB_COMPONENT_PROPS.items():
        for um in re.finditer(rf"<{comp}\b((?:[^>]|(?:\r?\n))*?)/?>", tsx):
            for pm in re.finditer(r"(\w+)\s*=", um.group(1)):
                if pm.group(1) not in allowed:
                    bad.append(f"{comp}.{pm.group(1)}")
    return sorted(set(bad))


_CONST_DEF = re.compile(r"\bconst\s+(\w+)\s*=\s*(-?\d+(?:\.\d+)?)\s*[;,]", re.M)
_INTERP_RANGE = re.compile(r"interpolate\(\s*[\w.]+\s*,\s*\[([^\]]*)\]")


def _eval_range_elem(expr: str, consts: dict[str, float]) -> float | None:
    """求值 inputRange 单元素；含未知标识符（如 durationInFrames）则放弃。"""
    s = expr.strip()
    for name, val in consts.items():
        s = re.sub(rf"\b{name}\b", repr(val), s)
    if not re.fullmatch(r"[-\d.\s+*/()]*", s) or not re.search(r"\d", s):
        return None
    try:
        return float(eval(s, {"__builtins__": {}}, {}))  # 表达式已白名单校验
    except Exception:
        return None


def _nonmonotonic_ranges(tsx: str) -> list[str]:
    """interpolate inputRange 静态求值验单调。实测炸点：模型写
    [0, 0.15*135, 0.88*135, BRAKE]，118.8 > BRAKE=100，运行时报
    inputRange must be strictly monotonically increasing，studio 黑屏。"""
    consts = {n: float(v) for n, v in _CONST_DEF.findall(tsx)}
    bad: list[str] = []
    for im in _INTERP_RANGE.finditer(tsx):
        vals = [_eval_range_elem(e, consts) for e in im.group(1).split(",")]
        for a, b in zip(vals, vals[1:]):
            if a is not None and b is not None and b <= a:
                bad.append(f"{a:g}->{b:g}")
                break
    return bad


def check_shot_tsx(tsx: str, shot: Shot) -> list[str]:
    failures: list[str] = []
    flat = tsx.strip()
    if len(flat) < 400:
        failures.append("太短")
    if not _braces_balanced(flat):
        failures.append("括号不配对（代码不完整）")
    if "export default" not in flat:
        failures.append("缺少 export default")
    if "useCurrentFrame" not in flat:
        failures.append("没有 useCurrentFrame（不是 Remotion 组件）")
    if _FORBIDDEN.search(flat):
        failures.append("含禁用的不确定/外部调用(Math.random/Date.now/定时器/fetch)")
    bad_imports = [m for m in _IMPORT_ANY.findall(flat)
                   if m not in ("remotion", "react", "../lib/refract")]
    if bad_imports:
        failures.append(f"引入了白名单外的模块: {bad_imports}")
    # 文案走 props（{title}）或字面量都算有字；两者皆无才是废件
    if not (re.search(r"[\u4e00-\u9fff]", flat) or "{title" in flat or "title}" in flat):
        failures.append("组件里没有任何文案")
    if shot.title and "{title" not in flat and shot.title not in flat:
        # 逐字渲染（wordmark 卡的做法）也算：title.split('') / Array.from(title)
        if not re.search(r"(title\.split|Array\.from\(title|\[\.\.\.title\])", flat):
            failures.append(f"没有渲染镜头文字「{shot.title[:10]}」")
    # 运行时炸点：用了 props 里没解构的变量（实测模型爱写 lines.map 却不解构 lines）
    m = re.search(r"function\s+\w+\s*\(\{([^}]*)\}", flat)
    if m:
        destructured = m.group(1)
        for prop in ("title", "accent", "accent2", "bg"):
            if prop not in destructured and re.search(rf"\b{prop}\b", flat[m.start():]):
                failures.append(f"props 未解构 {prop} 就使用（运行时会 ReferenceError）")
        if re.search(r"\blines\b", flat[m.start():]) and "lines" not in destructured:
            failures.append("使用了 lines 但未在 props 解构（运行时会 ReferenceError）")
    dups = _dup_style_keys(flat)
    if dups:
        failures.append(f"style 对象存在重复 key（tsc TS1117）: {dups}")
    bad_props = _bad_component_props(flat)
    if bad_props:
        failures.append(f"lib 组件传了不存在的 props（tsc TS2322）: {bad_props}")
    nonmono = _nonmonotonic_ranges(flat)
    if nonmono:
        failures.append(f"interpolate inputRange 不单调（运行时报错黑屏）: {nonmono}")
    return failures


# ---------------------------------------------------------------- 分镜映射

def _digest(brief: ContentBrief) -> str:
    sections = "；".join(s.heading for s in brief.outline[:6])
    facts = "；".join(f.claim for f in brief.facts[:4]) or "无明确数据点"
    return (
        f"主题：{brief.topic}\n切入角度：{brief.angle}\n钩子：{brief.hook}\n"
        f"章节：{sections}\n事实点：{facts}\nCTA：{brief.cta}"
    )


def _index_menu() -> str:
    rows = []
    for c in recipes.index():
        rows.append(
            f"- {c['name']} [{c['category']}] {c['duration']} {c['energy']}｜{c['one_liner']}｜适用: {c['use_for']}"
        )
    return "\n".join(rows)


def _fallback_shots(brief: ContentBrief, plan: VideoPlan) -> list[Shot]:
    """卡库缺失/映射失败时的确定性四段式：开场→章节→CTA。链路不能断。"""
    titles = [s.heading for s in brief.outline[:3]] or ["要点一", "要点二", "要点三"]
    spec = [
        ("", "opening", 3.0, "开场立题", brief.topic[:18], "pop", "whoosh"),
        ("", "data", 4.0, "讲亮点", titles[0], "hit", ""),
        ("", "effects", 4.0, "讲机制", titles[1] if len(titles) > 1 else titles[0], "whoosh", ""),
        ("", "outro", 3.5, "收尾 CTA", (brief.cta or "谢谢观看")[:16], "chord", ""),
    ]
    shots: list[Shot] = []
    for i, (card, cat, dur, narr, text, s1, s2) in enumerate(spec, 1):
        cues = [ShotCue(t=0.2, type=s1)]
        if s2:
            cues.append(ShotCue(t=dur - 0.5, type=s2))
        shots.append(Shot(idx=i, card=card, category=cat, duration_s=dur,
                          narration=narr, screen_text=text, title=text,
                          lines=[], sfx=cues))
    return shots


def _validate_mapping(data: dict, total: float) -> list[Shot]:
    shots_raw = data.get("shots") or []
    if not isinstance(shots_raw, list) or not shots_raw:
        raise ValueError("shots 为空")
    known = {c["name"]: c for c in recipes.index()}
    shots: list[Shot] = []
    used: set[str] = set()
    for i, raw in enumerate(shots_raw[:9], 1):
        card_name = str(raw.get("card") or "")
        if known and card_name not in known:
            continue  # 编造的卡名直接丢弃，宁缺毋滥
        if card_name in used:
            continue
        used.add(card_name)
        dur = max(3.0, min(7.0, float(raw.get("duration_s") or 4.5)))
        sfx = [ShotCue(t=max(0.0, min(dur, float(c.get("t") or 0))), type=c.get("type", "pop"))
               for c in (raw.get("sfx") or []) if isinstance(c, dict)]
        meta = known.get(card_name, {})
        text = str(raw.get("screen_text") or "")[:24]
        shots.append(Shot(idx=i, card=card_name, category=meta.get("category", ""),
                          duration_s=dur, narration=str(raw.get("narration") or "")[:24],
                          screen_text=text, title=text,
                          lines=[], sfx=sfx or [ShotCue(t=0.2, type="pop")]))
    if len(shots) < 3:
        raise ValueError(f"有效镜头不足: {len(shots)}")
    return shots


# ---------------------------------------------------------------- 逐镜头实现

_FALLBACK_TSX_TMPL = r"""import React from 'react';
import { AbsoluteFill, interpolate, useCurrentFrame } from 'remotion';
import { easeOutCubic, Caption } from '../lib/refract';

// 模板镜头（闸门未过自动落模板，保证工程必可播）
export default function Shot@@N@@({ title, accent, accent2, bg }: { title: string; accent: string; accent2: string; bg: string }) {
  const frame = useCurrentFrame();
  const p = easeOutCubic(interpolate(frame, [0, 18], [0, 1], { extrapolateRight: 'clamp' }));
  const breathe = Math.sin(frame / 18) * 0.012;
  return (
    <AbsoluteFill style={{ background: bg, justifyContent: 'center', alignItems: 'center' }}>
      <div style={{
        fontSize: 96, fontWeight: 800, color: '#fff', letterSpacing: 4,
        transform: `scale(${0.86 + 0.14 * p + breathe})`,
        opacity: p, textShadow: `0 4px 40px ${accent}66`,
        fontFamily: '"PingFang SC", "Microsoft YaHei", system-ui, sans-serif',
      }}>{title}</div>
      <div style={{
        marginTop: 28, width: 220 * p, height: 6, background: accent2, borderRadius: 3,
      }} />
    </AbsoluteFill>
  );
}
"""


async def _impl_shot(llm: LLMClient, shot: Shot, plan: VideoPlan, brief: ContentBrief,
                     history: list[dict]) -> Shot:
    from .styles import get_style

    st = get_style(plan.style)
    palette = plan.palette or [st.accent, st.accent2]
    bg = st.bg
    props_block = (
        f"镜头文字(title): {shot.title}\n"
        f"辅助要点(lines): {'、'.join(shot.lines) or '无'}\n"
        f"配色: bg={bg} accent={palette[0]} accent2={palette[1] if len(palette) > 1 else palette[0]}\n"
        f"镜头时长: {shot.duration_s:.1f}s（fps=30，即 {int(shot.duration_s * FPS)} 帧）\n"
        f"叙事任务: {shot.narration}"
    )
    card_block = recipes.card_text(shot.card) if shot.card else "（无卡：做干净的单焦点定版镜头）"
    user = f"【配方卡】\n{card_block}\n\n【本镜头参数】\n{props_block}\n\n【内容简报摘要】\n{_digest(brief)[:500]}"

    tsx, _prov = await llm.complete(system=SHOT_SYSTEM, user=user, max_tokens=4000, route="video")
    failures = check_shot_tsx(tsx, shot)
    history.append({"shot": shot.idx, "card": shot.card, "attempt": 1,
                    "ok": not failures, "failures": failures, "chars": len(tsx)})
    if failures:
        tsx2, _p2 = await llm.complete(
            system=SHOT_SYSTEM, user=user + "\n\n" + SHOT_REPAIR.format(failures="；".join(failures)),
            max_tokens=4000, route="video")
        failures2 = check_shot_tsx(tsx2, shot)
        history.append({"shot": shot.idx, "card": shot.card, "attempt": 2,
                        "ok": not failures2, "failures": failures2, "chars": len(tsx2)})
        if not failures2:
            shot.tsx, shot.gate_failures = tsx2, []
            return shot
        shot.tsx = (_FALLBACK_TSX_TMPL.replace("@@N@@", f"{shot.idx:02d}"))
        shot.used_fallback = True
        shot.gate_failures = failures2
        return shot

    shot.tsx, shot.gate_failures = tsx, []
    return shot


# ---------------------------------------------------------------- 工程组装

def _root_tsx(shots: list[Shot], sfx_global: list[dict],
              accent: str, accent2: str, bg: str) -> str:
    imports = [f"import Shot{s.idx:02d} from './shots/Shot{s.idx:02d}';" for s in shots]
    seqs, audio = [], []
    frame_cursor = 0
    for s in shots:
        from_f = frame_cursor
        dur_f = int(round(s.duration_s * FPS))
        title_js = json.dumps(s.title, ensure_ascii=False)
        seqs.append(
            f"      <Sequence from={{{from_f}}} durationInFrames={{{dur_f}}} name=\"S{s.idx:02d} {s.card or 'tpl'}\">\n"
            f"        <Shot{s.idx:02d} title={title_js} accent={{ACCENT}} accent2={{ACCENT2}} bg={{BG}} />\n"
            f"      </Sequence>"
        )
        frame_cursor += dur_f
    for cue in sfx_global:
        audio.append(
            f"      <Sequence from={{{int(round(cue['t'] * FPS))}}} durationInFrames={{90}}>\n"
            f"        <Audio src={{staticFile('audio/sfx/{cue['file']}')}} volume={{0.7}} />\n"
            f"      </Sequence>"
        )
    total = frame_cursor
    return f"""import React from 'react';
import {{ AbsoluteFill, Audio, Composition, Sequence, staticFile }} from 'remotion';
{chr(10).join(imports)}

// 骨架由 Refract 管线确定性生成：时序/音轨/注册在这里，镜头实现在 src/shots/
const ACCENT = '{accent}';
const ACCENT2 = '{accent2}';
const BG = '{bg}';
const FPS = {FPS};

const Promo: React.FC = () => {{
  return (
    <AbsoluteFill style={{{{ background: BG }}}}>
{chr(10).join(seqs)}
{chr(10).join(audio)}
    </AbsoluteFill>
  );
}};

const RemotionRoot: React.FC = () => (
  <Composition
    id="Refract"
    component={{Promo}}
    durationInFrames={{{total}}}
    fps={{FPS}}
    width={{{WIDTH}}}
    height={{{HEIGHT}}}
  />
);

export {{ RemotionRoot, Promo, FPS }};
export const TOTAL_FRAMES = {total};
"""


def _assemble(project: Path, shots: list[Shot], sfx_global: list[dict],
              plan: VideoPlan, total_s: float) -> None:
    (project / "src" / "shots").mkdir(parents=True, exist_ok=True)
    (project / "public" / "audio" / "sfx").mkdir(parents=True, exist_ok=True)
    (project / "src" / "index.ts").write_text(INDEX_TS, encoding="utf-8")
    (project / "src" / "lib").mkdir(parents=True, exist_ok=True)
    # 注意必须是 .tsx：lib 里含 JSX（Caption 组件），.ts 后缀 tsc 直接报错
    (project / "src" / "lib" / "refract.tsx").write_text(LIB_TSX, encoding="utf-8")
    for s in shots:
        (project / "src" / "shots" / f"Shot{s.idx:02d}.tsx").write_text(s.tsx, encoding="utf-8")

    palette = plan.palette or ["#22d3ee", "#f59e0b"]
    from .styles import get_style

    st = get_style(plan.style)
    bg = st.bg
    root = _root_tsx(shots, sfx_global, palette[0],
                     palette[1] if len(palette) > 1 else palette[0], bg)
    (project / "src" / "Root.tsx").write_text(root, encoding="utf-8")
    # 在线播放入口：bundle 这个文件得到干净播放器页（而非 Studio）
    (project / "src" / "player.tsx").write_text(PLAYER_TSX, encoding="utf-8")

    (project / "package.json").write_text(json.dumps(PACKAGE_JSON, ensure_ascii=False, indent=2), encoding="utf-8")
    (project / "tsconfig.json").write_text(json.dumps(TSCONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
    (project / "remotion.config.ts").write_text(REMOTION_CONFIG, encoding="utf-8")
    (project / "预览.bat").write_text(BAT_TMPL, encoding="utf-8")

    shot_rows = "\n".join(
        f"| {s.idx} | {s.card or '内置模板'} | {s.duration_s:.1f}s | {s.screen_text} |" for s in shots)
    cards = "、".join(s.card or "模板" for s in shots)
    (project / "README.md").write_text(
        README_TMPL.format(width=WIDTH, height=HEIGHT, fps=FPS, duration=total_s,
                           n=len(shots), cards=cards, sfx_n=len(sfx_global), shot_rows=shot_rows),
        encoding="utf-8")


def check_project(project: Path, shots: list[Shot]) -> list[str]:
    failures: list[str] = []
    for s in shots:
        p = project / "src" / "shots" / f"Shot{s.idx:02d}.tsx"
        if not p.exists() or p.stat().st_size < 300:
            failures.append(f"Shot{s.idx:02d} 文件缺失或过小")
    root = (project / "src" / "Root.tsx").read_text(encoding="utf-8")
    if not (project / "src" / "lib" / "refract.tsx").exists():
        failures.append("lib/refract.tsx 缺失（组件 import 会挂）")
    if "registerRoot" not in (project / "src" / "index.ts").read_text(encoding="utf-8"):
        failures.append("index.ts 未注册 registerRoot")
    for s in shots:
        if f"Shot{s.idx:02d}" not in root:
            failures.append(f"Root.tsx 没挂载 Shot{s.idx:02d}")
    if "<Audio" not in root:
        failures.append("Root.tsx 没有音轨（要求带音效）")
    # JSX 属性值必须带花括号：from=6 是非法 JSX（曾真实产出过，必须闸住）
    if re.search(r"<Sequence\s+[^>]*from=\d", root):
        failures.append("Root.tsx 存在非法 JSX 属性（Sequence from 缺花括号）")
    return failures


# ---------------------------------------------------------------- 工程页（替代 /v/ 直出的 HTML 片）

INFO_TMPL = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>Refract · Remotion 工程 {tid}</title>
<style>
body{{margin:0;font:15px/1.7 system-ui,"PingFang SC",sans-serif;background:#f7f7f5;color:#1a1a1a}}
main{{max-width:860px;margin:0 auto;padding:48px 24px}}
h1{{font-size:22px}} code{{background:#eee;padding:2px 7px;border-radius:5px;font-size:13px}}
pre{{background:#14171c;color:#d7e2e8;padding:16px 20px;border-radius:10px;overflow:auto}}
table{{border-collapse:collapse;width:100%}} td,th{{border-bottom:1px solid #e2e2de;padding:8px 10px;text-align:left}}
.tag{{display:inline-block;background:{accent}18;color:{accent};border:1px solid {accent}44;border-radius:99px;padding:1px 10px;font-size:12px;margin-right:6px}}
</style></head><body><main>
<h1>Refract 宣传片工程 <span class="tag">{model}</span><span class="tag">{n} 镜头</span><span class="tag">{dur:.1f}s</span></h1>
<p>按 <b>video-shotcraft</b> 方案产出：LLM 选配方卡 + 逐镜头实现，工程骨架与音轨由代码确定性拼装。
本产物<b>不渲染 mp4</b>，用 Remotion Studio 预览：</p>
<pre>cd {project}
npm install
npm run studio   # http://localhost:3000</pre>
<h3>镜头表</h3>
<table><tr><th>#</th><th>配方卡</th><th>时长</th><th>屏幕文字</th><th>实现</th></tr>
{rows}
</table>
<h3>音效表（本地合成 wav）</h3>
<table><tr><th>全局时间</th><th>音色</th><th>文件</th></tr>
{sfx_rows}
</table>
<p style="color:#888">工程目录：{project}</p>
</main></body></html>
"""


def write_info_page(res: RemotionResult, task_id: str, data_dir: Path, model: str) -> Path:
    rows = "\n".join(
        f"<tr><td>{s.idx}</td><td>{s.card or '内置模板'}</td><td>{s.duration_s:.1f}s</td>"
        f"<td>{s.screen_text}</td><td>{'✅' if not s.used_fallback else '⚠️ 模板兜底'}</td></tr>"
        for s in res.shots)
    sfx_rows = "\n".join(
        f"<tr><td>{c['t']:.2f}s</td><td>{c['type']}</td><td><code>{c['file']}</code></td></tr>"
        for c in res.gate.stats.get("sfx", []))
    palette = "#22d3ee"
    html = INFO_TMPL.format(tid=task_id, model=model, n=len(res.shots), dur=res.duration_s,
                            project=res.project_dir, rows=rows, sfx_rows=sfx_rows, accent=palette)
    out = data_dir / "videos" / f"{task_id}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out


async def bundle_project(project_dir: Path) -> Path | None:
    """把 Remotion 工程 bundle 成浏览器可直接播放的静态目录（build/）。

    播放发生在用户浏览器（@remotion/player），服务器不出 mp4，避开
    headless Chrome 1.5G 内存的坑。bundle 是一次性构建，产出的
    build/index.html 自带播放器，/v/{task_id} 路由托管它即可在线预览。

    返回 build 目录；任何失败（缺 Node / 依赖装不上 / 内存不够）返回 None，
    调用方降级回「信息页」——视频功能不能因为预览构建失败而整体报错。
    """
    import asyncio
    import shutil as _shutil

    if not (project_dir / "package.json").exists():
        return None
    node = _shutil.which("node")
    npm = _shutil.which("npm")
    if not node or not npm:
        return None

    build = project_dir / "build"
    # 已有 build 且是完整的（有 index.html + bundle.js）就直接复用，跳过重构建
    if (build / "index.html").exists() and (build / "bundle.js").exists():
        return build

    try:
        # 1) 装依赖（node_modules 不存在才装，避免每次重装一两分钟）。
        #    第一次要拉 remotion 全家桶（含 compositor 二进制，几百 MB），
        #    小内存服务器上可能 4-6 分钟，超时放宽到 600s。
        if not (project_dir / "node_modules").exists():
            proc = await asyncio.create_subprocess_exec(
                npm, "install", "--no-audit", "--no-fund",
                cwd=str(project_dir),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, err = await asyncio.wait_for(proc.communicate(), timeout=600)
            if proc.returncode != 0:
                return None

        # 2) bundle（产出 build/）。走 player 入口（干净播放器页），不是 Studio。
        #    cli 的 bin 入口是 remotion-cli.js。
        #    注意：Windows 下 node + 绝对路径 + cwd 会把路径重复拼接（MODULE_NOT_FOUND），
        #    所以这里传相对路径，靠 cwd 定位
        cli_rel = Path("node_modules") / "@remotion" / "cli" / "remotion-cli.js"
        if not (project_dir / cli_rel).exists():
            return None
        proc = await asyncio.create_subprocess_exec(
            node, str(cli_rel), "bundle", "src/player.tsx",
            cwd=str(project_dir),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await asyncio.wait_for(proc.communicate(), timeout=600)
        if proc.returncode != 0:
            return None
    except (asyncio.TimeoutError, OSError):
        return None

    if (build / "index.html").exists():
        return build
    return None


# ---------------------------------------------------------------- 主入口

async def generate(llm: LLMClient, brief: ContentBrief, plan: VideoPlan,
                   data_dir: Path, task_id: str) -> RemotionResult:
    from .sfx import render_sfx

    history: list[dict] = []
    provider = llm.provider("video")

    # 1) 镜头映射
    menu = _index_menu()
    total = float(plan.duration or 20)
    map_user = (
        f"【配方卡索引（{len(recipes.index())} 张）】\n{menu or '（卡库缺失，按 opening→data→effects→outro 自由设计）'}\n\n"
        f"【视觉方案】{plan.name}：{plan.concept}｜总时长 {total}s\n"
        f"【内容简报】\n{_digest(brief)}"
    )
    try:
        mapping = await llm.structured(system=MAPPING_SYSTEM, user=map_user,
                                       max_tokens=2500, route="video")
        shots = _validate_mapping(mapping, total)
    except Exception:
        shots = _fallback_shots(brief, plan)
        history.append({"stage": "mapping", "fallback": True})

    # 2) 逐镜头实现
    for s in shots:
        await _impl_shot(llm, s, plan, brief, history)

    # 3) 组装 + SFX（同 task_id 重跑时只清生成产物，保留 node_modules——
    #    整目录 rmtree 会把依赖一起删，重装要一两分钟）
    project = data_dir / "videos" / f"{task_id}-remotion"
    project.mkdir(parents=True, exist_ok=True)
    for stale in ("src", "public", "README.md", "tsconfig.json",
                  "package.json", "remotion.config.ts", "预览.bat"):
        target = project / stale
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            target.unlink(missing_ok=True)
    sfx_global: list[dict] = []
    frame_cursor = 0.0
    for s in shots:
        for cue in s.sfx:
            sfx_global.append({"t": round(frame_cursor + cue.t, 2), "type": cue.type})
        frame_cursor += s.duration_s
    sfx_files = render_sfx(sfx_global, project / "public" / "audio" / "sfx")
    _assemble(project, shots, sfx_files, plan, frame_cursor)

    # 4) 工程级闸门
    proj_failures = check_project(project, shots)
    shot_soft = [f"Shot{s.idx:02d} 落模板: {'；'.join(s.gate_failures[:2])}"
                 for s in shots if s.used_fallback]
    gate = GateResult(ok=not proj_failures,
                      failures=proj_failures + shot_soft,
                      stats={"shots": len(shots), "sfx": sfx_files,
                             "fallbacks": sum(1 for s in shots if s.used_fallback),
                             "duration_s": round(frame_cursor, 2)})

    res = RemotionResult(project_dir=project, shots=shots, gate=gate,
                         provider_label=provider.label, model=provider.model,
                         duration_s=frame_cursor, attempts=history)
    return res
