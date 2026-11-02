"""文章配图：把 compose 阶段规划好的 prompt 变成真图。

三个设计取舍，都是踩出来的：

1. **生成后立刻下载存本地**。多数图像 API 返回临时 URL（硅基流动只有 1 小时），
   直接引用的话，用户第二天打开文章就是一片裂图。

2. **并行生成**。三张图串行要 40-60 秒，并行压到 15-20 秒——文章本来就要跑三分钟，
   配图不该再拖一截。

3. **任何一张失败就保留占位**。配图是加分项不是关键路径，
   不能因为一张图挂了把整篇文章搞失败。

没配 IMAGE_API_KEY 时整个模块自动跳过，正文保持「🖼 配图位」占位——
和接入前的行为完全一致，不会因为没配 key 就报错。

接口按硅基流动的格式写（OpenAI 兼容），换别家改 IMAGE_BASE_URL + IMAGE_MODEL 即可：
    POST {base}/v1/images/generations
    {"model": ..., "prompt": ..., "image_size": "1024x576", "batch_size": 1}
    → {"images": [{"url": "..."}]}
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import httpx

DEFAULT_BASE = "https://api.siliconflow.cn"
# Kolors 对中文提示词理解好，价格低；想更快可以换 black-forest-labs/FLUX.1-schnell
DEFAULT_MODEL = "Kwai-Kolors/Kolors"
TIMEOUT = 90.0
MAX_IMAGES = 3          # 每篇最多几张，控成本
IMAGE_SIZE = "1024x576"  # 16:9，适配公众号头图与正文横图

_PLACEHOLDER = re.compile(r"!\[([^\]]*)\]\(prompt:\s*([^)]*)\)")


def enabled() -> bool:
    return bool(os.environ.get("IMAGE_API_KEY", "").strip())


def _cfg() -> tuple[str, str, str]:
    return (
        os.environ.get("IMAGE_API_KEY", "").strip(),
        os.environ.get("IMAGE_BASE_URL", DEFAULT_BASE).rstrip("/"),
        os.environ.get("IMAGE_MODEL", DEFAULT_MODEL).strip(),
    )


async def generate_one(prompt: str) -> bytes | None:
    """生成一张图并下载成 bytes。失败返回 None，不抛异常（调用方按张降级）。"""
    key, base, model = _cfg()
    if not key:
        return None
    payload = {
        "model": model,
        "prompt": prompt[:800],
        "image_size": IMAGE_SIZE,
        "batch_size": 1,
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(
                f"{base}/v1/images/generations",
                json=payload,
                headers={"Authorization": f"Bearer {key}"},
            )
            if resp.status_code >= 400:
                return None
            data = resp.json()
            imgs = data.get("images") or []
            url = (imgs[0] or {}).get("url") if imgs else ""
            if not url:
                return None
            # 临时 URL，立刻拉下来存本地，别把外链写进产物
            got = await client.get(url)
            if got.status_code >= 400 or not got.content:
                return None
            return got.content
    except (httpx.HTTPError, ValueError, AttributeError, TypeError, IndexError):
        return None


async def fill_placeholders(
    body: str, out_dir: Path, url_prefix: str = "/img"
) -> tuple[str, int]:
    """把正文里的 `![说明](prompt: xxx)` 替换成真图。

    返回 (新正文, 成功张数)。未配置 key 或全部失败时原样返回，正文不受影响。
    """
    if not enabled() or not body:
        return body, 0

    total = len(_PLACEHOLDER.findall(body))
    if not total:
        return body, 0

    # 只取前 N 个 prompt 去生成（控成本），按出现顺序
    prompts = [m.group(2).strip() for m in _PLACEHOLDER.finditer(body)][:MAX_IMAGES]
    if not prompts:
        return body, 0

    out_dir.mkdir(parents=True, exist_ok=True)
    results = await asyncio.gather(*(generate_one(p) for p in prompts))

    it = iter(results)
    seq = {"n": 0}

    def repl(m: re.Match[str]) -> str:
        data = next(it, None)
        if not data:
            return m.group(0)          # 这张失败，保留原占位
        seq["n"] += 1
        name = f"{seq['n']}.png"
        (out_dir / name).write_bytes(data)
        return f"![{m.group(1)}]({url_prefix}/{out_dir.name}/{name})"

    new_body = _PLACEHOLDER.sub(repl, body, count=MAX_IMAGES)
    return new_body, seq["n"]
