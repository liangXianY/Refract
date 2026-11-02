"""判分规则（lint）测试：只测确定性规则，不调模型。

这三条是线上误报最多的，改完要钉住：
1. 标题换个说法不该被判"漏节"
2. 结尾说了「评论区聊聊」就算有 CTA，不该要求逐字复述
3. 图片链接、第 N 节里的数字不是正文数据，不该进溯源检查
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pipeline.reflect import lint  # noqa: E402
from app.schemas import ContentBrief, Fact, Section  # noqa: E402


def _brief(**kw) -> ContentBrief:
    base = dict(
        topic="向量数据库为什么快",
        platform="wechat",
        word_limit=600,
        outline=[Section(heading="传统检索的瓶颈"), Section(heading="向量检索怎么解决")],
        facts=[Fact(claim="某基准测试快 3 倍", source="https://example.com/a")],
        cta="关注公众号，回复「向量」领取资料",
    )
    base.update(kw)
    return ContentBrief(**base)


BODY_OK = """# 向量数据库为什么快

## 一、传统检索的瓶颈

关键词匹配对不上同义表达，用户搜「怎么退款」找不到「退款流程说明」。这里不涉及任何数字。

## 二、向量检索是怎么解决的

把文本转成向量，按距离找近邻，语义相近就能命中。

![架构图](https://img.example.com/2024/11/1024x576.png)

到这里基本讲清楚了。

关注公众号，回复「向量」领取资料。
"""


def test_renamed_headings_not_missing() -> None:
    issues = [t for t, _ in lint(_brief(), BODY_OK)]
    assert not any("漏了" in t or "少了" in t for t in issues), issues


def test_cta_detected_by_tone() -> None:
    body = BODY_OK.replace("关注公众号，回复「向量」领取资料。", "有想法可以评论区聊聊。")
    issues = [t for t, _ in lint(_brief(), body)]
    assert not any("行动号召" in t for t in issues), issues


def test_image_url_digits_not_flagged() -> None:
    issues = [t for t, _ in lint(_brief(), BODY_OK)]
    assert not any("找不到出处" in t for t in issues), issues


def test_real_missing_section_is_blocking() -> None:
    body = BODY_OK.split("## 二、")[0]
    pairs = lint(_brief(), body)
    assert any(block for _, block in pairs), pairs


def test_short_body_is_blocking() -> None:
    pairs = lint(_brief(), "# 标题\n\n太短了。")
    assert any(block for _, block in pairs), pairs


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("ALL PASS")
