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
- 用单色强调 + 深色底做出层次；至少 3 层景深（背景网格 / 中景内容 / 前景浮层）

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


@dataclass
class GateResult:
    ok: bool
    failures: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


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
    """把可能被画到画布上的字捞出来，用于统计「画面里到底有多少字」。

    不认 HTML 正文：canvas 全屏铺开时，写在 <div> 里的字根本看不见——
    那正是「一句话都没说」的来源。

    两级提取。第一级直接找 fillText('...')。
    但模型常先封装一个 drawText() 再在里面调 fillText，这时第一级是空的，
    要退一步用整段 script（去掉注释）。

    为什么不精确解析字符串字面量：试过，纯粹的自写词法器在
    minified 代码上很容易错位，把中文串整段跳过。宁可粗一点，
    反正后面还有 phrase_cover 那道闸兜内容相关性。
    """
    direct = "".join(
        m[1]
        for m in re.findall(
            r"(?:fillText|strokeText)\s*\(\s*(['\"`])((?:\\.|(?!\1).)*?)\1", html, re.S
        )
    )
    if sum(1 for ch in direct if "\u4e00" <= ch <= "\u9fff") >= 20:
        return direct

    # 注意结尾的 |$：被截断的文件没有 </script>，仍然要把它已有的内容算进来，
    # 否则「文字 0 个」这个判断会在截断时永远成立，掩盖真实原因
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
        "",
        "要点（按顺序出现在画面上）：",
    ]
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
    max_attempts: int = 2,
) -> VideoResult:
    """让视频模型写整份 HTML；不过闸门就带着失败原因重修一次。"""
    user = _brief_block(brief, plan)
    html = ""
    gate = GateResult(ok=False, failures=["未生成"])
    p = llm.provider("video")
    attempts = 0
    history: list[dict] = []

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
        f"{res.provider_label} · {res.model} · 闸门 {'通过' if res.gate.ok else '未过'} "
        f"· 第 {res.attempts} 版</div>"
    )
    if not res.gate.ok:
        return html  # 残缺文件不注入，保持原样方便排查到底断在哪
    if "</body>" in html:
        return html.replace("</body>", badge + "\n</body>", 1)
    return html + badge


def save(html: str, task_id: str, data_dir: Path, meta: dict) -> Path:
    out = data_dir / "videos"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{task_id}.html"
    path.write_text(html, encoding="utf-8")
    (out / f"{task_id}.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path
