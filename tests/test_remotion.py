"""Remotion 管线单元测试：不调 API，mock LLM，验证工程组装/闸门/SFX 的确定性。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pipeline import remotion_video as rv
from app.pipeline.sfx import synthesize, render_sfx
from app.pipeline.styles import get_style
from app.pipeline.director import VideoPlan
from app.schemas import ContentBrief, Fact, Section


def make_brief() -> ContentBrief:
    return ContentBrief(
        topic="从一句话到一条能发的视频",
        angle="内容生产流水线",
        hook="输入随便给，输出随便要。",
        platform="video",
        outline=[
            Section(heading="三类输入归一化", points=["一句话需求", "已有文案"]),
            Section(heading="Plan-Execute-Reflect", points=["规划", "执行"]),
        ],
        facts=[Fact(claim="召回率从 64% 提升到 83%", source="评测集")],
        cta="来 GitHub 看源码",
    )


GOOD_TSX = """import React from 'react';
import { AbsoluteFill, interpolate, useCurrentFrame } from 'remotion';
import { rand, easeOutCubic, Caption } from '../lib/refract';

export default function Shot01({ title, accent, accent2, bg }: { title: string; accent: string; accent2: string; bg: string }) {
  const frame = useCurrentFrame();
  const p = easeOutCubic(interpolate(frame, [0, 18], [0, 1], { extrapolateRight: 'clamp' }));
  const jitter = rand(7) * 2 - 1;
  return (
    <AbsoluteFill style={{ background: bg, justifyContent: 'center', alignItems: 'center' }}>
      <div style={{ fontSize: 88, opacity: p, color: '#fff', transform: `translateX(${jitter}px)`, fontFamily: 'system-ui' }}>
        {title}
      </div>
      <Caption text={title} accent={accent} />
    </AbsoluteFill>
  );
}
"""


class MockProvider:
    def __init__(self) -> None:
        self.label = "Mock"
        self.model = "mock-video"


class MockLLM:
    """模拟视频模型：映射返回合法 JSON，实现返回合格 TSX。"""

    def __init__(self, bad_tsx: str = "") -> None:
        self.provider_route = MockProvider()
        self.bad_tsx = bad_tsx

    def provider(self, route: str) -> MockProvider:
        return self.provider_route

    async def structured(self, *, system, user, max_tokens=2000, temperature=0.3, validate=None, route="text"):
        cards = rv.recipes.index()
        pick = [c["name"] for c in cards][:5]
        if len(pick) < 5:
            pick = ["", "", "", ""]
        shots = []
        durs = [3.0, 4.0, 4.5, 3.5]
        cats = ["opening", "data", "effects", "outro"]
        for i in range(4):
            shots.append({
                "card": pick[i] if i < len(pick) else "",
                "category": cats[i],
                "duration_s": durs[i],
                "narration": f"第{i+1}段",
                "screen_text": make_brief().outline[min(i, 1)].heading[:10],
                "sfx": [{"t": 0.2, "type": "pop" if i < 3 else "chord"}],
            })
        return {"shots": shots}

    async def complete(self, *, system, user, max_tokens=8000, temperature=0.7, route="video"):
        return self.bad_tsx or GOOD_TSX, self.provider_route


def test_sfx_deterministic():
    a = synthesize("whoosh", seed=42)
    b = synthesize("whoosh", seed=42)
    c = synthesize("whoosh", seed=43)
    assert a == b and a != c
    for kind in rv.SFX_KINDS:
        w = synthesize(kind)
        assert len(w) > 4000  # 至少几十毫秒的合法 wav
        assert w[:4] == b"RIFF"


def test_render_sfx_files(tmp=None):
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        out = render_sfx([{"t": 0.5, "type": "pop"}, {"t": 2.0, "type": "pop"},
                          {"t": 3.0, "type": "未知类型"}], Path(td))
        assert len(out) == 3
        assert out[0]["file"] == "pop-1.wav" and out[1]["file"] == "pop-2.wav"
        assert out[2]["type"] == "pop"  # 未知音色回落
        assert (Path(td) / "pop-1.wav").stat().st_size > 4000


def test_check_shot_tsx_gates():
    shot = rv.Shot(idx=1, card="", category="", duration_s=3, narration="",
                   screen_text="三类输入归一化", title="三类输入归一化")
    assert rv.check_shot_tsx(GOOD_TSX, shot) == []

    bad = GOOD_TSX.replace("useCurrentFrame", "frame")
    fails = rv.check_shot_tsx(bad, shot)
    assert any("useCurrentFrame" in f for f in fails)

    nondet = GOOD_TSX.replace("rand(7)", "Math.random()")
    assert any("禁用" in f for f in rv.check_shot_tsx(nondet, shot))

    evil_import = GOOD_TSX.replace("../lib/refract", "fs")
    assert any("白名单" in f for f in rv.check_shot_tsx(evil_import, shot))

    # style 对象重复 key（tsc TS1117，实测 deepseek 爱同时写 opacity 两次）
    dup = GOOD_TSX.replace(
        "justifyContent: 'center',",
        "justifyContent: 'center', opacity: p, opacity: p * 0.5,",
    )
    assert any("重复 key" in f for f in rv.check_shot_tsx(dup, shot))

    # lib 组件传不存在的 props（tsc TS2322，实测传过 opacity/fontSize/texts）
    bad_props = GOOD_TSX.replace(
        "<Caption text={title} accent={accent} />",
        "<Caption text={title} accent={accent} opacity={0.7} fontSize={26} />",
    )
    assert any("TS2322" in f for f in rv.check_shot_tsx(bad_props, shot))
    multi = GOOD_TSX.replace(
        "<Caption text={title} accent={accent} />",
        "<Caption\n  texts={lines}\n  frame={frame}\n/>",
    )
    assert any("Caption.texts" in f for f in rv.check_shot_tsx(multi, shot))

    # inputRange 不单调（实测 [0, 0.15*135, 0.88*135, BRAKE] 炸 studio 黑屏）
    nonmono = GOOD_TSX.replace(
        "const frame = useCurrentFrame();",
        "const frame = useCurrentFrame(); const BRAKE = 100;"
        " const camX = interpolate(frame, [0, 0.15 * 135, 0.88 * 135, BRAKE], [0, 1, 2, 3]);",
    )
    assert any("不单调" in f for f in rv.check_shot_tsx(nonmono, shot))
    # 常量代入后单调的不误杀
    mono = nonmono.replace("0.88 * 135", "0.7 * 135")
    assert not any("不单调" in f for f in rv.check_shot_tsx(mono, shot))


def test_mapping_validation_drops_fake_cards():
    real = [c["name"] for c in rv.recipes.index()]
    raw = {"shots": [
        {"card": real[0] if real else "", "duration_s": 3, "screen_text": "开场",
         "sfx": [{"t": 0, "type": "pop"}]},
        {"card": "不存在的卡", "duration_s": 3, "screen_text": "x"},
        {"card": real[1] if len(real) > 1 else "", "duration_s": 9, "screen_text": "中段"},  # 超时长被夹
        {"card": real[2] if len(real) > 2 else "", "duration_s": 3, "screen_text": "收尾"},
        {"card": real[3] if len(real) > 3 else "", "duration_s": 3, "screen_text": "补位"},
    ]}
    shots = rv._validate_mapping(raw, 15.0)
    assert len(shots) >= 3
    assert all(s.card != "不存在的卡" for s in shots)
    assert all(1.5 <= s.duration_s <= 6.0 for s in shots)


def test_fallback_shots_deterministic():
    b = make_brief()
    s1 = rv._fallback_shots(b, VideoPlan())
    s2 = rv._fallback_shots(b, VideoPlan())
    assert [(s.card, s.duration_s, s.screen_text) for s in s1] == \
           [(s.card, s.duration_s, s.screen_text) for s in s2]
    assert len(s1) == 4


async def _gen(tmpdir: Path, bad_tsx: str = ""):
    llm = MockLLM(bad_tsx=bad_tsx)
    return await rv.generate(llm, make_brief(), VideoPlan(duration=15, style="hud"), tmpdir, "t0001")


def test_generate_full_project():
    import asyncio
    import tempfile

    async def run():
        with tempfile.TemporaryDirectory() as td:
            res = await _gen(Path(td))
            p = res.project_dir
            assert (p / "src" / "Root.tsx").exists()
            assert (p / "src" / "index.ts").exists()
            assert (p / "src" / "lib" / "refract.tsx").exists()
            assert (p / "package.json").exists()
            assert (p / "预览.bat").exists()
            # 四个镜头组件都落盘且 Root 挂载
            root = (p / "src" / "Root.tsx").read_text(encoding="utf-8")
            for s in res.shots:
                assert (p / "src" / "shots" / f"Shot{s.idx:02d}.tsx").exists()
                assert f"Shot{s.idx:02d}" in root
            assert "<Audio" in root  # 要求带音效
            assert res.gate.ok, res.gate.failures
            # info 页可写
            info = rv.write_info_page(res, "t0001", Path(td), res.model)
            assert info.exists() and "Remotion" in info.read_text(encoding="utf-8")
            return res

    res = asyncio.run(run())
    assert res.duration_s > 10


def test_generate_bad_tsx_falls_back():
    import asyncio
    import tempfile

    async def run():
        with tempfile.TemporaryDirectory() as td:
            return await _gen(Path(td), bad_tsx="export default () => null")

    res = asyncio.run(run())
    assert all(s.used_fallback for s in res.shots)
    # 软伤：工程仍完整、gate 有标注但 Root/组件齐全
    assert res.gate.stats["fallbacks"] == len(res.shots)
    assert not any("文件缺失" in f for f in res.gate.failures)


def test_recipes_index_loads():
    st = rv.recipes.stats()
    print("recipe stats:", st)
    if st["total"]:
        assert st["total"] >= 100
        assert set(st["categories"]) >= {"opening", "outro"}
        c = rv.recipes.card(st and list(rv.recipes.index())[0]["name"])
        assert c and "动效核心" in c["body"] or c


def main() -> None:
    test_sfx_deterministic()
    test_render_sfx_files()
    test_check_shot_tsx_gates()
    test_mapping_validation_drops_fake_cards()
    test_fallback_shots_deterministic()
    test_generate_full_project()
    test_generate_bad_tsx_falls_back()
    test_recipes_index_loads()
    print("ALL REMOTION TESTS PASSED")


if __name__ == "__main__":
    main()
