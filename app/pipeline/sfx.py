"""SFX 本地合成器：按音效表确定性生成 wav 文件。

为什么本地合成而不是直接接 Freesound
----------------------------------
video-shotcraft 的标准做法是 Freesound 按需下载（CC0），但那要外网 + API token，
服务器上不可控。这里先用纯 stdlib（wave + math）合成五类基础音色，
保证「离线、零依赖、确定性」（同 seed 同波形，重跑两次文件一致）；
Freesound 升级路径保留：音效表结构与之兼容，后续换源只动 render 一个函数。

音色词汇与 video-shotcraft 的声音句式对齐（references/aesthetic-rules.md）：
riser → impact(hit) → sparkle 的电影系节奏，不用游戏 UI tap。
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

SR = 44100

# 每类音色的 (时长秒, 是否循环用噪声底)
_KINDS = {
    "pop": 0.09,
    "whoosh": 0.38,
    "hit": 0.16,
    "chord": 0.65,
    "sparkle": 0.5,
    "riser": 0.8,
}


def _mulberry32(seed: int):
    """确定性伪随机（与 Remotion 侧 helpers 同一算法，画面与声音同源）。"""

    def rnd() -> float:
        nonlocal seed
        seed = (seed + 0x6D2B79F5) & 0xFFFFFFFF
        t = seed
        t = (t ^ (t >> 15)) * (t | 1) & 0xFFFFFFFF
        t = (t ^ (t + ((t ^ (t >> 7)) * (t | 61) & 0xFFFFFFFF))) & 0xFFFFFFFF
        return ((t ^ (t >> 14)) & 0xFFFFFFFF) / 4294967296.0

    return rnd


def _env_exp(i: int, n: int, k: float = 6.0) -> float:
    """指数衰减包络。"""
    return math.exp(-k * i / n)


def _samples(kind: str, seed: int) -> list[float]:
    rnd = _mulberry32(seed)
    n = int(SR * _KINDS[kind])
    out = [0.0] * n

    if kind == "pop":
        # 短促下扫正弦：元素登场
        f0, f1 = 620.0, 210.0
        ph = 0.0
        for i in range(n):
            t = i / n
            f = f0 + (f1 - f0) * t
            ph += 2 * math.pi * f / SR
            out[i] = 0.5 * math.sin(ph) * _env_exp(i, n, 7.0)

    elif kind == "whoosh":
        # 带通感噪声扫：转场/大幅移动（一阶低通近似，截止随包络走）
        prev = 0.0
        for i in range(n):
            t = i / n
            noise = rnd() * 2 - 1
            alpha = 0.04 + 0.22 * math.sin(math.pi * t)  # 中段最亮
            prev += alpha * (noise - prev)
            amp = math.sin(math.pi * t) ** 1.5  # 起音-收音都缓
            out[i] = 0.6 * prev * amp

    elif kind == "hit":
        # 低频冲击 + 噪声瞬态：强调落点
        ph = 0.0
        for i in range(n):
            t = i / n
            f = 150.0 * (1 - 0.4 * t)
            ph += 2 * math.pi * f / SR
            body = math.sin(ph) * _env_exp(i, n, 5.0)
            click = (rnd() * 2 - 1) * _env_exp(i, n, 30.0) * 0.4
            out[i] = 0.75 * body + click

    elif kind == "chord":
        # 大三和弦软铺：收尾
        freqs = (523.25, 659.25, 783.99)
        for j, f in enumerate(freqs):
            ph = 2 * math.pi * f * (j * 0.012)  # 轻微错拍起音
            for i in range(n):
                t = i / n
                atk = min(1.0, i / (SR * 0.02))
                out[i] += 0.18 * math.sin(ph + 2 * math.pi * f * i / SR) * atk * _env_exp(i, n, 3.5)

    elif kind == "sparkle":
        # 上行短音列：亮点点缀
        steps = 5
        for j in range(steps):
            f = 880 * (2 ** (j / steps))
            s0 = int(n * j / steps)
            sl = int(SR * 0.07)
            ph = 0.0
            for i in range(min(sl, n - s0)):
                ph += 2 * math.pi * f / SR
                out[s0 + i] += 0.22 * math.sin(ph) * _env_exp(i, sl, 5.0)

    elif kind == "riser":
        # 上行扫 + 渐强：段前蓄势
        prev = 0.0
        ph = 0.0
        for i in range(n):
            t = i / n
            f = 320 + 900 * t * t
            ph += 2 * math.pi * f / SR
            noise = rnd() * 2 - 1
            prev += (0.05 + 0.15 * t) * (noise - prev)
            amp = t * t  # 渐强
            out[i] = (0.3 * math.sin(ph) + 0.35 * prev) * amp

    else:  # pragma: no cover - 上游枚举校验兜底
        raise ValueError(f"未知音色: {kind}")

    # 软限幅，防爆音
    return [max(-0.92, min(0.92, s)) for s in out]


def synthesize(kind: str, seed: int = 1) -> bytes:
    """合成一段 wav 字节。同 (kind, seed) 恒等输出。"""
    if kind not in _KINDS:
        raise ValueError(f"未知音色: {kind}（可用: {sorted(_KINDS)}）")
    samples = _samples(kind, seed)
    frames = b"".join(struct.pack("<h", int(s * 32767)) for s in samples)
    import io

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(frames)
    return buf.getvalue()


def render_sfx(cues: list[dict], out_dir: Path) -> list[dict]:
    """按音效表落盘。cues: [{t, type}] → [{t, type, file}]。

    同类型多次出现时按出现序号派生 seed，保证同片内不同 whoosh 波形有差异
    但整个工程重跑结果一致。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    counters: dict[str, int] = {}
    result: list[dict] = []
    for idx, cue in enumerate(cues):
        kind = cue.get("type", "pop")
        if kind not in _KINDS:
            kind = "pop"
        seq = counters.get(kind, 0)
        counters[kind] = seq + 1
        fname = f"{kind}-{seq + 1}.wav"
        (out_dir / fname).write_bytes(synthesize(kind, seed=1000 + idx * 17 + seq))
        result.append({"t": round(float(cue.get("t", 0.0)), 2), "type": kind, "file": fname})
    return result
