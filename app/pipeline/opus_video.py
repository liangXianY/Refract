"""成片层：让 Opus 直接写出整份动画 HTML，再用闸门兜住下限。

为什么把渲染代码交给模型
------------------------
模板渲染只能出「我已经想到的」画面。风格再多也是排列组合，
用户看两眼就腻——之前那版「PPT 翻页」的批评就是这么来的。
让强模型直接写 drawFrame，才可能有做新东西的空间。

为什么必须有闸门
----------------
之前实测过两种翻车：
  1. 模型吐了几百行纯几何动画，一个字都没有 —— 好看，但不是「介绍片」
  2. 模型把画面做成一页一页切 —— 本质还是动态 PPT
这两个问题靠 prompt 只能降低概率，不能消除。
所以生成完先跑三道自动检查，不过就打回重修一次，再不过就降级到模板。

**降级不是失败兜底，是质量下限的保证**：
宁可给一个 70 分的模板片，也不给一个 0 分的空白页。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..llm import LLMClient
from ..schemas import ContentBrief
from .director import VideoPlan

# ---------------------------------------------------------------- 提示词

SYSTEM = """你是 Motion Graphics 工程师，用 Canvas 2D 写一条中文演示短片。

输出：一份完整的单文件 HTML。不要 Markdown 围栏，不要解释，直接从 <!DOCTYPE html> 开始。

技术约束：
- 画布固定 1920x1080（16:9），页面里居中显示，四周留黑
- 只用 Canvas 2D + requestAnimationFrame；核心是一个纯函数 drawFrame(t)，t 为秒
- 时长按给定值，到末尾无缝循环
- 零外部资源：不许 <script src>、不许 CDN、不许 <img src>、不许 @import，
  字体只用系统栈（system-ui / "PingFang SC" / ui-monospace / serif）
- 用一个 <canvas> + 一个 <script>，不要引库
- **整份代码控制在 450 行以内，必须写完，最后一个字符是 </html>**。
  写不完宁可砍效果：一个能播的 400 行比一个播不了的 800 行有用得多
- 把总时长写到 window.__DUR = <给定时长>（数值即可），音效引擎按它排节拍

工艺规则（对齐 xilo-opus-video 的制作规范）：
- 随机数必须用固定种子的伪随机（如 function rnd(i){return (Math.sin(i*127.1+311.7)*43758.5453)%1;}），
  禁用 Math.random()——画面必须可复现，重放两次要一模一样
- 主动避开"没有任何限制时最容易出来的样子"：深色背景+居中大字+所有元素一起淡入淡出、
  角落里装饰性的标签和边框、无意义的发光。风格需要深色时（CRT/夜景/霓虹）照用，但排版要有自己的设计
- 文字一屏最多一句，每段至少停留 2.5 秒；每 2-4 秒画面上要有新东西出现
- 主体要大：核心元素至少占画面 1/3；镜头可以推拉跟随，不要全程固定机位把画面撑空
- 循环要无缝：最后一帧的状态（位置/透明度/光标）必须和第一帧完全一致
- 关键动作对齐节拍：把最重要的状态变化放在 t = DUR/8 的整数倍上——音效引擎在这些点发音，声画才会同步
- 弹簧缓动（可选，比 easeOut 高级）：spring(x,w,z) = 1 - exp(-z*w*x) * (cos(wd*x) + (z*w/wd)*sin(wd*x))，
  其中 wd = w*sqrt(1-z*z)。大字/定版用 w=10 z=0.9（沉稳），普通元素用 w=15 z=0.72（默认），贴纸用 w=18 z=0.45（回弹）

内容硬要求（不满足就算废片）：
- 屏幕上必须有真实中文文字。开场 3 秒内必须出现主标题，最后 2 秒仍要有字
- 至少 6 处不同的文字出现在不同时间点（标题、副标题、分条要点、结论、收尾）
- 禁止只画几何图形不写字

动效硬要求（不满足就是动态 PPT）：
- 不要分页：不要"一屏画完 → 淡出 → 下一屏"。用一个连续场景，元素按时间轴依次登场
- 所有位移/缩放/透明度用缓动（easeOutCubic / easeInOutQuint / 弹性），禁止线性
- 每个已登场的元素都要有持续的次级运动：呼吸、缓慢漂移、描边流动、光标闪烁、
  数字微抖。静止的画面等于死画面
- 至少一处强调动作：急停、回弹、爆闪或速度突变
- 转场用同一场景内的空间运动（推进、扫描线、元素汇聚、镜头推拉），不要白场切换
- 层次要分明（背景质感 / 中景内容 / 前景浮层），但背景质感**必须服从给定风格**：
  深色科技风才用网格、扫描线；纸面线稿风用留白和微纹理；像素风用色块。
  严禁把所有片子都画成同一种深底科技风——风格意向和配色是硬要求，不是建议

我会给你：标题、要点、时长、配色、节奏点。按节奏点排时间轴。
写完自己检查一遍：有没有字？有没有持续运动？有没有分页？

输出前最后一行确认：整个文件必须能被浏览器直接打开就播，不需要任何服务器。
"""

REPAIR_HINT = """上一版没通过自动检查，问题如下：
{failures}

请重写整份 HTML，重点修正上面的问题。特别注意：
- 文件必须写完，最后一个字符是 </html>。宁可少画两个效果，也不能写到一半断掉
- 代码要精简：CSS 写短、重复的绘制逻辑抽成函数、不要注释长段说明，整体控制在 450 行以内
- 文字必须真的画在画布上（ctx.fillText），不是写在 HTML 的 div 里靠 CSS 显示
- 每一段文字都要有自己的登场时间，不要所有字一次全出
- 元素登场后要继续动，不能静止
"""


# 硬伤 = 这片子根本没法看（截断/空白/不能播），必须降级模板；
# 软伤 = 内容质量不达标（字少/覆盖低），能用，标注出来就行。
# 为什么分：Opus 一次生成 5-10 分钟，为「字少了点」重烧一遍 16k token 不划算。
_HARD_MARKS = ("文件不完整", "太短", "没有 <canvas>", "没有动画循环", "外部资源", "空正文")


def hard_failures(failures: list[str]) -> list[str]:
    return [f for f in failures if any(m in f for m in _HARD_MARKS)]


@dataclass
class GateResult:
    ok: bool
    failures: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    @property
    def hard(self) -> list[str]:
        """硬伤子集：空列表 = 片子能播，质量问题照用。"""
        return hard_failures(self.failures)


@dataclass
class VideoResult:
    html: str
    plan: VideoPlan
    provider_label: str
    model: str
    gate: GateResult
    attempts: int
    degraded: bool = False
    # 每一版都留痕：复盘时最想知道的是「第一次为什么没过」，只留最后一次等于没留
    history: list[dict] = field(default_factory=list)
    # 每一版的完整 HTML——对比「被拦下的版本」和「过闸的版本」差在哪，全靠它
    attempt_htmls: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 闸门

_EXTERNAL = re.compile(r'(<script[^>]+src=|<link[^>]+href=["\']http|@import\s+url\()', re.I)
_HAS_RAF = re.compile(r"requestAnimationFrame|setInterval|setTimeout", re.I)
_HAS_CANVAS = re.compile(r"<canvas", re.I)
_HAS_FILLTEXT = re.compile(r"fillText|strokeText|innerText|textContent", re.I)


def _expected_phrases(brief: ContentBrief) -> list[str]:
    """从 brief 抽「应当出现在画面里」的短语。

    取标题冒号前的核心词 + 冒号后的解释，各自独立算命中：
    模型常把「反思：自己给自己打分」画成大字「反思」+ 小字说明，
    拿整句去匹配会误判成没写，白打回一次。
    """
    cands: list[str] = [brief.topic]
    for s in brief.outline:
        head = re.split(r"[：:，,。—]", s.heading)
        cands.append(head[0])  # 核心词
        if len(head) > 1:
            cands.append(head[1][:10])  # 解释句
    for s in brief.outline[:3]:
        cands += s.points[:2]
    if brief.hook:
        cands.append(brief.hook[:16])
    if brief.cta:
        cands.append(brief.cta[:16])

    seen: set[str] = set()
    out: list[str] = []
    for c in cands:
        c = re.sub(r"\s+", "", c)[:14]
        if len(c) < 3 or c in seen:
            continue
        seen.add(c)
        out.append(c)
    return out


def _drawn_text(html: str) -> str:
    """统计「可能被画到画布上的中文」的来源池。

    曾经的实现是两级择一：先找 fillText('...') 直接调用，凑够 20 个中文就
    提前返回——实测被它坑了：Opus 把文字集中在少数长字符串/数组变量里
    （fillText 只调 10 次、540 个中文），第一级只捞到 24 个就提前返回，
    闸门把一份 540 字的片子误判成「一个字都没有」。

    现在的口径：script 区去掉注释后全部内容。中文字符只可能出现在
    字符串字面量（要画的文案）或注释（已去掉）里，变量名不会是中文，
    所以「去注释的 script」就是文案全集，简单且不会少算。
    """
    # 结尾的 |$：被截断的文件没有 </script>，仍要把已有内容算进来
    scripts = "\n".join(re.findall(r"<script[^>]*>(.*?)(?:</script>|$)", html, re.S))
    scripts = re.sub(r"/\*.*?\*/", "", scripts, flags=re.S)  # 块注释
    scripts = re.sub(r"//[^\n]*", "", scripts)  # 行注释
    return scripts


def check(html: str, brief: ContentBrief, duration: int) -> GateResult:
    """四道闸：完整 / 能播 / 有字 / 在动。"""
    failures: list[str] = []
    flat = re.sub(r"\s+", "", html)

    # 闸零：文件完整性。
    # 实测最坑的一种翻车：模型写到一半被 max_tokens 截断，
    # 前面全是漂亮代码，浏览器打开就是一片黑，而且不报错。
    tail = html.rstrip()[-40:].lower()
    if "</html>" not in tail and "</script>" not in tail:
        failures.append(
            "文件不完整（跑到一半被截断了）：最后没有 </html> / </script>。"
            "请压缩代码体积，务必把整份写完"
        )
    if len(html) < 3500:
        failures.append(f"产物太短（{len(html)} 字符），不像完整文件")
    if not _HAS_CANVAS.search(html):
        failures.append("没有 <canvas>，无法逐帧渲染")
    if not _HAS_RAF.search(html):
        failures.append("没有动画循环（requestAnimationFrame / 定时器）")
    if _EXTERNAL.search(html):
        failures.append("引用了外部资源（脚本/样式/CDN），要求零外链、断网可播")
    if not _HAS_FILLTEXT.search(html):
        failures.append("没有在画布上写字的代码（fillText），很可能画面里一个字都没有")

    # 闸一：画面里到底有多少字。
    # 「一个字都没有」是被骂过的问题，直接量化：画布上的中文少于 40 字就算废片。
    drawn = _drawn_text(html)
    han = sum(1 for ch in drawn if "\u4e00" <= ch <= "\u9fff")
    if han < 40:
        failures.append(
            f"画布上的中文字只有 {han} 个（至少 40）。"
            "文字必须用 ctx.fillText 画到画布上，不能写在 HTML 的 div 里"
        )

    # 闸二：讲的还是不是这篇内容
    phrases = _expected_phrases(brief)
    hit = [p for p in phrases if p in flat]
    cover = len(hit) / len(phrases) if phrases else 1.0
    if cover < 0.34:
        failures.append(
            f"内容覆盖率只有 {cover:.0%}，画面偏离了正文。"
            f"必须出现：{'、'.join(phrases[:5])}"
        )

    # 闸三：时间轴要真的被用起来
    cues = len(re.findall(r"\b(t|time|elapsed)\b", html))
    if cues < 8:
        failures.append("时间轴相关代码过少，可能只是静态画面")

    stats = {
        "chars": len(html),
        "drawn_han": han,
        "phrase_cover": round(cover, 2),
        "phrases_hit": hit[:8],
        "phrases_expected": len(phrases),
    }
    return GateResult(ok=not failures, failures=failures, stats=stats)


# ---------------------------------------------------------------- 生成

def _brief_block(brief: ContentBrief, plan: VideoPlan) -> str:
    lines = [
        f"标题：{brief.topic}",
        f"一句话钩子：{brief.hook}",
        f"受众：{brief.audience}；语气：{brief.tone}",
        f"时长：{plan.duration} 秒",
        f"配色：{'、'.join(plan.palette) or '#05080c、#22d3ee、#f8fafc'}",
    ]
    # 风格库的具体参数必须传：之前只给 palette 三个色，Opus 全靠自己猜，
    # 六条片子写成了同一种深底科技风。styles.py 的定义才是风格的完整定义。
    from .styles import STYLES

    st = STYLES.get(plan.style)
    if st:
        lines.append(
            f"风格硬约束（{st.name}）：底色 {st.bg}（次 {st.bg2}）、主文字 {st.ink}、"
            f"次要文字 {st.dim}、强调色 {st.accent} 与 {st.accent2}、"
            f"{'等宽字体' if st.font == 'mono' else '无衬线字体'}、圆角风格 {st.radius}px。"
            f"背景/文字/强调色全部使用这些值，不要自作主张换色系。"
        )
    lines += ["", "要点（按顺序出现在画面上）："]
    for i, s in enumerate(brief.outline[:6], 1):
        pts = "／".join(s.points[:3])
        lines.append(f"  {i}. {s.heading}" + (f"  —— {pts}" if pts else ""))
    if brief.facts:
        lines.append("")
        lines.append("可用数据点：")
        for f in brief.facts[:4]:
            lines.append(f"  - {f.claim}")
    if brief.cta:
        lines.append("")
        lines.append(f"收尾句：{brief.cta}")
    lines += [
        "",
        f"创意方案：{plan.name} —— {plan.concept}",
        f"运动语言：{plan.motion}",
        "节奏点：",
    ]
    for b in plan.beats:
        lines.append(f"  · {b}")
    return "\n".join(lines)


async def generate(
    llm: LLMClient,
    brief: ContentBrief,
    plan: VideoPlan,
    *,
    max_attempts: int = 1,  # 实测重修要再烧 5-10 分钟的 Opus 额度；除硬伤外直接用第一版
) -> VideoResult:
    """让视频模型写整份 HTML；不过闸门就带着失败原因重修一次。"""
    user = _brief_block(brief, plan)
    html = ""
    gate = GateResult(ok=False, failures=["未生成"])
    p = llm.provider("video")
    attempts = 0
    history: list[dict] = []
    attempt_htmls: list[str] = []

    for attempt in range(max_attempts):
        attempts = attempt + 1
        prompt = user if attempt == 0 else user + "\n\n" + REPAIR_HINT.format(
            failures="\n".join(f"- {f}" for f in gate.failures)
        )
        html, p = await llm.complete(
            system=SYSTEM,
            user=prompt,
            max_tokens=p.max_tokens,  # 用 provider 自身上限，别手写一个更小的数把自己截断
            temperature=0.65 if attempt == 0 else 0.4,  # 重修时收敛，别又跑偏
            route="video",
        )
        if not html.strip():
            # 空产出重试没有意义：推理模型把 token 额度全花在思考上时，正文就是空的，
            # 这是端点/模型特性，不是「这次运气差」。直接降级，别再烧第二遍。
            gate = GateResult(
                ok=False,
                failures=["模型返回了空正文（推理把额度耗尽，或端点不支持长输出）。换非推理模型再试"],
                stats={"chars": 0},
            )
            history.append({"attempt": attempts, "chars": 0, "ok": False, "failures": gate.failures, "stats": gate.stats})
            attempt_htmls.append(html)
            break
        gate = check(html, brief, plan.duration)
        history.append(
            {
                "attempt": attempts,
                "chars": len(html),
                "ok": gate.ok,
                "failures": gate.failures,
                "stats": gate.stats,
            }
        )
        attempt_htmls.append(html)
        if gate.ok:
            break

    return VideoResult(
        html=html,
        plan=plan,
        provider_label=p.label,
        model=p.model,
        gate=gate,
        attempts=attempts,
        degraded=False,
        history=history,
        attempt_htmls=attempt_htmls,
    )


# 音效引擎：WebAudio 程序化合成，不用任何音频素材（版权干净、体积为零）。
# 浏览器禁止自动播放声音，所以默认静音，观众点一下才响。
# 节拍按 window.__DUR（Opus 被要求写入）八等分：每拍一个轻 tick，
# 每 6 拍一个 whoosh，最后一拍上扬和弦。__DUR 缺失时按 24s 兜底。
_AUDIO_JS = """
(function(){
  var DUR = (window.__DUR && window.__DUR > 5) ? window.__DUR : 24;
  var on = false, ac = null, timer = null, n = 0;
  function env(g, t, a, d, v){ g.gain.setValueAtTime(0.0001, t);
    g.gain.linearRampToValueAtTime(v, t + a);
    g.gain.exponentialRampToValueAtTime(0.0001, t + a + d); }
  function tick(t){ var o = ac.createOscillator(), g = ac.createGain();
    o.type = 'triangle'; o.frequency.value = 660 + (n % 3) * 180;
    env(g, t, 0.005, 0.1, 0.05); o.connect(g).connect(ac.destination);
    o.start(t); o.stop(t + 0.14); }
  function whoosh(t){ var len = Math.floor(ac.sampleRate * 0.45),
    b = ac.createBuffer(1, len, ac.sampleRate), d = b.getChannelData(0);
    for (var i = 0; i < len; i++) d[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / len, 1.6);
    var s = ac.createBufferSource(); s.buffer = b;
    var f = ac.createBiquadFilter(); f.type = 'bandpass'; f.Q.value = 1.2;
    f.frequency.setValueAtTime(280, t); f.frequency.exponentialRampToValueAtTime(2600, t + 0.38);
    var g = ac.createGain(); env(g, t, 0.02, 0.42, 0.09);
    s.connect(f).connect(g).connect(ac.destination); s.start(t); }
  function chord(t){ [261.6, 329.6, 392.0].forEach(function(fq, i){
    var o = ac.createOscillator(), g = ac.createGain(); o.type = 'sine';
    o.frequency.value = fq; env(g, t + i * 0.05, 0.03, 1.3, 0.045);
    o.connect(g).connect(ac.destination); o.start(t + i * 0.05); o.stop(t + 1.5); }); }
  function beat(){ var t = ac.currentTime + 0.02; n++;
    if (n === 1 || n % 6 === 4) whoosh(t);
    else if (n % 6 === 0) chord(t); else tick(t); }
  var b = document.createElement('button');
  b.textContent = '🔇 音效';
  b.style.cssText = 'position:fixed;right:14px;bottom:44px;z-index:9;font:500 12px ui-monospace,Menlo,monospace;color:#7b8b99;background:rgba(8,12,16,.72);border:1px solid #1e2a33;border-radius:6px;padding:5px 10px;cursor:pointer;backdrop-filter:blur(6px)';
  b.onclick = function(){
    if (!ac) ac = new (window.AudioContext || window.webkitAudioContext)();
    on = !on;
    if (on) { b.textContent = '🔊 音效中'; ac.resume(); beat();
      timer = setInterval(beat, DUR * 1000 / 8); }
    else { b.textContent = '🔇 音效'; ac.suspend(); clearInterval(timer); }
  };
  document.body.appendChild(b);
})();
"""


def wrap_meta(html: str, plan: VideoPlan, res: VideoResult) -> str:
    """在产物里注入一段可关闭的角标：哪个模型、过了几道闸。

    面试官打开链接第一眼看到"Opus 5.5 生成 · 闸门 3/3 通过"，
    比截图有说服力；也方便自己排查哪一版是谁写的。
    """
    badge = (
        '<div id="__refract_meta" style="position:fixed;left:14px;bottom:12px;z-index:9;'
        "font:500 12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;"
        "color:#7b8b99;background:rgba(8,12,16,.72);border:1px solid #1e2a33;"
        "border-radius:6px;padding:6px 10px;letter-spacing:.02em;pointer-events:none;"
        'backdrop-filter:blur(6px)">'
        f"{res.provider_label} · {res.model} · "
        + ("闸门通过" if res.gate.ok else "软伤标注：" + "；".join(res.gate.failures)[:60] + " · 第 1 版直接使用")
        + "</div>"
    )
    if res.gate.hard:
        return html  # 硬伤残缺文件不注入，保持原样方便排查到底断在哪
    audio = "<script>" + _AUDIO_JS + "</script>"
    if "</body>" in html:
        return html.replace("</body>", badge + audio + "\n</body>", 1)
    return html + badge + audio


def save(html: str, task_id: str, data_dir: Path, meta: dict) -> Path:
    out = data_dir / "videos"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{task_id}.html"
    path.write_text(html, encoding="utf-8")
    (out / f"{task_id}.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path
