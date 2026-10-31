"""Mock 数据源。

只在 LLM_MOCK=1 时使用。设计原则：**结构和真实调用完全一致**，
这样从 mock 切到真模型只需要改一个环境变量，调用方代码一行不动。

不是随便 return 个字符串 —— 它会根据用户输入拼出不同主题的 brief，
让前端在 mock 阶段就能看出真实的排版和长度。
"""

from __future__ import annotations

import re

from .schemas import ContentBrief, Fact, Reflection, Section

_PLATFORM_HINT = {
    "wechat": "公众号长文",
    "xiaohongshu": "小红书图文",
    "video": "视频口播稿",
}

# 和 pipeline/brief.py 的 PLATFORM_SPEC 保持一致
_WORD_LIMIT = {"wechat": 1200, "xiaohongshu": 550, "video": 800}


def mock_brief(user_input: str, platform: str = "wechat", word_limit: int | None = None) -> ContentBrief:
    # 抓取来的素材带元信息头（标题：/来源：/发布：/原文链接：），
    # 直接拿第一行当主题会变成「标题：GitHub - xxx」。跳过元信息找真正的主题。
    topic = ""
    for line in (user_input or "").splitlines():
        line = line.strip()
        if not line or line.startswith(("标题：", "来源：", "发布：", "原文链接：", "<<<", ">>>")):
            continue
        topic = re.sub(r"\s+", " ", line)[:40]
        break
    topic = topic or "内容生产 Agent"
    kind = _PLATFORM_HINT.get(platform, "公众号长文")
    return ContentBrief(
        topic=topic,
        angle="从一个具体的工作场景切入，讲清楚它解决了谁的什么问题",
        audience="对 AI 应用落地感兴趣的技术与业务同学",
        platform=platform,  # type: ignore[arg-type]
        tone="专业、克制、有观点，不堆术语",
        word_limit=word_limit or _WORD_LIMIT.get(platform, 1200),
        hook=f"先说结论：{topic} 的价值不在模型本身，而在它替掉的那段重复劳动。",
        outline=[
            Section(
                heading="问题：为什么现在这套流程跑不动",
                points=["人工环节占比过高，产出速度被卡在审核上", "每次都是重新起稿，经验沉淀不下来"],
            ),
            Section(
                heading="拆解：把一件事拆成可复用的四步",
                points=["统一输入表征，让下游只认一个结构", "执行与校验分离，失败可定位到具体环节"],
            ),
            Section(
                heading="关键设计：反思机制为什么要分三档",
                points=["同参数重试只能解决偶发失败", "参数级修补和重新规划的成本差十倍"],
            ),
            Section(
                heading="结果与反思",
                points=[f"按 {kind} 形态产出，单篇耗时从 40 分钟降到 3 分钟", "仍存在的边界：事实核验依赖外部信源"],
            ),
        ],
        facts=[
            Fact(claim="单篇初稿耗时由约 40 分钟降至 3 分钟内", source="本地实测（mock 阶段为示例值）"),
            Fact(claim="反思分三档后，无效重跑次数明显下降", source=None),
        ],
        cta="如果你也在做类似的事，欢迎交流踩过的坑。",
    )


def mock_body(brief: ContentBrief) -> str:
    parts: list[str] = []
    parts.append(f"# {brief.topic}\n")
    parts.append(f"> {brief.hook}\n")
    for idx, sec in enumerate(brief.outline, start=1):
        parts.append(f"\n## {idx}. {sec.heading}\n")
        for p in sec.points:
            parts.append(f"- {p}")
            parts.append(
                f"\n  {p}。这一段展开讲实际操作时踩到的细节：先看输入是否稳定，"
                f"再看失败能不能被定位，最后才谈效果提升。"
            )
        parts.append("")
    parts.append(f"\n## 写在最后\n\n{brief.cta}\n")
    return "\n".join(parts).strip()


def mock_reflection(brief: ContentBrief, body: str, round_no: int = 1) -> Reflection:
    """round=1 故意给一个不达标的结果，好让前端能看到 refine 分支真的走了一次。"""
    verified = sum(1 for f in brief.facts if f.verified)
    total_facts = max(1, len(brief.facts))
    coverage = int(verified / total_facts * 100)

    if round_no == 1:
        scores = {"结构完整": 78, "事实可溯": coverage if coverage else 55, "语气匹配": 82, "开头吸引力": 71}
        r = Reflection(
            scores=scores,
            issues=[
                "第 2 节后劲不足，缺少一个具体数字支撑观点",
                "有一条事实点没有标注来源，存在编造风险",
            ],
            repair_action="refine",
            round=round_no,
        )
    else:
        scores = {"结构完整": 88, "事实可溯": 90, "语气匹配": 86, "开头吸引力": 84}
        r = Reflection(scores=scores, issues=["仍可加强结尾的行动号召，但不影响发布"], repair_action="pass", round=round_no)
    r.recompute_total()
    return r


def mock_stream_chunks(text: str, size: int = 6) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]
