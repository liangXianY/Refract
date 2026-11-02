"""Reflector：判断"够不够好、怎么修"。

设计上有两条硬规矩，也是面试时最该讲的两点：

1. 三层检查，前两层不花 token
   L1 契约校验：字段齐不齐、字数够不够、CTA 在不在
   L2 事实校验：正文里的每个数字能否在 facts 里找到出处
   L3 质量评分：模型按维度打分（唯一花钱的一层）

2. repair_action 由代码判定，不由模型给
   模型只负责"指出问题"，"该重试还是该重新规划"是成本决策，
   交给模型会得到一个永远说 refine 的复读机。
"""

from __future__ import annotations

import re

from .. import mock
from ..llm import LLMClient
from ..schemas import ContentBrief, Reflection

_SYSTEM = """你是严格的内容编辑。请以挑刺的视角审阅这篇稿件。

只输出 JSON：
{
  "scores": {"结构完整": 0-100, "事实可溯": 0-100, "语气匹配": 0-100, "开头吸引力": 0-100},
  "issues": ["具体问题，必须指出在哪一节、什么毛病，不要写'整体不错但可以更好'这种废话"]
}

打分标准：
- 结构完整：是否覆盖大纲每一节、每节有没有实际论点
- 事实可溯：数据是否有出处、有没有出现没有来源的具体数字
- 语气匹配：是否符合目标平台和给定语气
- 开头吸引力：前三句能不能让人继续读
issues 最多 4 条，按严重程度排序。"""

# 数字 + 单位，用来捞"疑似编造的数据"
_NUM = re.compile(r"\d+(?:\.\d+)?\s*(?:%|％|倍|万|千|亿|分钟|秒|小时|天|人|条|次|篇|元|GB|MB)")


def _plain(s: str) -> str:
    """去掉标点和空白，只留字，用于比对标题和 CTA（"01. 为什么" ≡ "为什么"）。"""
    return re.sub(r"[^\u4e00-\u9fff0-9a-zA-Z]", "", s or "").lower()


def _similar(a: str, b: str) -> float:
    """两段文字的相似度（0~1）。用标准库 difflib，不引分词器、不搞词表。"""
    from difflib import SequenceMatcher

    a, b = _plain(a), _plain(b)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


# 结尾出现这些词，就算做了行动号召（不同平台说法不一样，不要求逐字复述）
_CTA_WORDS = ("关注", "点赞", "转发", "留言", "评论", "收藏", "私信", "扫码",
              "点击", "试试", "评论区", "在看", "分享给", "加入", "报名", "咨询")


def lint(brief: ContentBrief, body: str) -> list[tuple[str, bool]]:
    """L1 + L2：确定性检查，成本为零。

    返回 (问题描述, 是否阻断)。
    分级很重要：第一版我把"数字找不到出处"当阻断，结果正文里
    "三步""5 分钟"这类常规表述全被命中，一次报 6 条，把 75 分的稿子
    直接打成 replan，白烧一轮成本。误报率高的规则只能做提示，不能做阻断。

    这一版把三条误报最高的规则改了：
    - 漏节：原来数 `## ` 的条数，模型换个标题写法就误判 → 改成标题模糊匹配
    - CTA：原来比 `cta[:8] not in body`，模型几乎不会逐字复述 → 改成看结尾语气
    - 数字溯源：原来把图片链接/URL 里的数字也当正文数据 → 先剥掉链接再扫
    """
    out: list[tuple[str, bool]] = []

    # L1 字数：偏离目标太多才是硬伤
    n = len(re.sub(r"\s", "", body))
    if brief.word_limit and n < brief.word_limit * 0.55:
        out.append((f"正文仅 {n} 字，明显低于目标 {brief.word_limit} 字", True))
    elif brief.word_limit and n > brief.word_limit * 1.8:
        out.append((f"正文 {n} 字，超出目标 {brief.word_limit} 字太多，需要精简", True))

    # L1 小节覆盖：按标题相似度判断，模型改个说法不算漏节
    headings = [h.strip() for h in re.findall(r"^#{2,3}\s+(.+)$", body, flags=re.M)]
    missing = [s.heading for s in brief.outline
               if not any(_similar(s.heading, h) >= 0.45 for h in headings)]
    if len(missing) >= 2:
        out.append((f"漏了 {len(missing)} 节：{'、'.join(missing[:2])}", True))
    elif missing:
        out.append((f"少了 1 节：{missing[0]}", False))

    # L1 CTA：只看结尾有没有号召动作，不要求复述原句
    if brief.cta:
        tail = body[-400:]
        if not any(w in tail for w in _CTA_WORDS) and _similar(brief.cta, tail) < 0.1:
            out.append(("结尾没有落到行动号召（读者不知道下一步做什么）", False))

    # L2 事实溯源：只提示，不阻断（误报率高）
    # 先剥掉图片/链接里的数字——图床文件名里的 2024、尺寸 1024 不是正文数据
    prose = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", body)
    prose = re.sub(r"https?://\S+", "", prose)
    prose = re.sub(r"第\s*[0-9一二三四五六七八九十]+\s*[节章步条]", "", prose)
    sourced = _plain(" ".join(f.claim + (f.source or "") for f in brief.facts))
    unsourced = [
        h.strip() for h in set(_NUM.findall(prose))
        if _plain(h).replace(" ", "") not in sourced.replace(" ", "")
    ]
    if unsourced:
        sample = "、".join(unsourced[:3])
        out.append((f"数据「{sample}」在事实清单里找不到出处，需确认是否编造", False))
    return out[:6]


def decide(total: float, blocking: int, round_no: int, max_rounds: int, prev_total: float | None = None) -> str:
    """修复档位的判据。pass=达标，refine=可修，replan=方案层面问题，stop=轮数用尽。

    阈值是实测校准出来的，不是拍的：
    第一版按 62/82 设，真实模型把正常稿子打成 replan，每篇多付一次规划成本，
    总耗时从 30 秒涨到 115 秒。现在 55/78。
    blocking 只数"硬伤"（字数离谱、大面积漏节），提示类问题不参与决策。

    stop 和 pass 必须分开：轮数用尽时分数不够也得停（成本决策），
    但不能标成 PASS——56 分显示 PASS 是在骗用户。停了就是停了，
    交付哪一版由引擎取历史最高分，和这个档位无关。
    """
    if round_no >= max_rounds:
        return "stop"
    if total < 55 or blocking >= 2:
        return "replan"
    if blocking >= 1 or total < 78:
        return "refine"
    return "pass"


async def reflect(
    llm: LLMClient,
    brief: ContentBrief,
    body: str,
    round_no: int = 1,
    max_rounds: int = 2,
    history_hint: str = "",
    prev_total: float | None = None,  # 上一轮总分：日志里能看出「修订是进步还是退步」
) -> Reflection:
    """反思。

    history_hint：来自长期记忆的「历史表现较好的结构」。
    它让评分有参照系——同一个结构过去 6 篇都拿高分，这次写得顺就不该压分；
    反过来，这个结构历史上总出问题，就该更挑刺。
    """
    if llm.cfg.mock:
        r = mock.mock_reflection(brief, body, round_no)
        lints = lint(brief, body)
        blocking = sum(1 for _, b in lints if b)
        r.issues = ([t for t, _ in lints] + r.issues)[:5]
        r.recompute_total()
        r.repair_action = decide(r.total, blocking, round_no, max_rounds, prev_total)  # type: ignore[assignment]
        return r

    try:
        raw = await llm.structured(
            system=_SYSTEM,
            user=(
                f"目标平台 {brief.platform}，语气 {brief.tone}，目标篇幅 {brief.word_limit} 字。"
                f"{history_hint}\n\n以下是待审稿件：\n<<<\n{body[:12000]}\n>>>"
            ),
            max_tokens=6000,  # 推理模型的思考 token 共用额度
        )
    except Exception as exc:
        # 反思是「非关键路径」。它挂了，不能让已经写好的一整篇稿子作废——
        # 降级成纯规则校验，把失败原因如实告诉用户，然后继续。
        lints = lint(brief, body)
        blocking = sum(1 for _, b in lints if b)
        r = Reflection(
            scores={},
            issues=[f"评分模型不可用（{type(exc).__name__}），已降级为规则校验"]
            + [t for t, _ in lints],
            round=round_no,
        )
        r.total = 62.0 if blocking else 76.0
        r.repair_action = decide(r.total, blocking, round_no, max_rounds, prev_total)  # type: ignore[assignment]
        return r
    scores = {}
    for k, v in (raw.get("scores") or {}).items():
        try:
            scores[str(k)] = max(0, min(100, int(v)))
        except (TypeError, ValueError):
            continue
    issues = [str(i) for i in (raw.get("issues") or [])][:4]

    r = Reflection(scores=scores, issues=issues, round=round_no)
    r.recompute_total()

    # 确定性检查优先：模型看不见的问题（字数离谱、漏节），代码能抓到
    lints = lint(brief, body)
    blocking = sum(1 for _, b in lints if b)
    if lints:
        r.issues = ([t for t, _ in lints] + r.issues)[:6]
        # 有硬伤时压一下分数，避免模型给高分后直接 pass
        if blocking and r.total >= 82:
            r.total = 79.0
    r.repair_action = decide(r.total, blocking, round_no, max_rounds, prev_total)  # type: ignore[assignment]
    return r
