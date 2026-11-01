"""模型注册表：同一套代码里按「用途」路由到不同厂商、不同协议。

为什么需要这一层
----------------
不是所有活都该交给同一个模型：

- 文案 / 分镜方案 / 反思评分：调用频次高、单次输出短、要快 → DeepSeek flash
- 视频成片代码：一次要吐几百行 Canvas 动画，还要理解视觉意图 → Opus 5.5

把它们塞进一个 client 的后果是：换模型要改代码，改完还要重新部署。
所以这里把「哪个模型干哪类活」变成一份可运行时修改的路由表。

两套协议
--------
DeepSeek 走 OpenAI 兼容的 /chat/completions（Bearer 鉴权）。
Claude 走 Anthropic 的 /v1/messages（x-api-key 或 Bearer，system 是顶层字段，
内容块是 content blocks，流式事件是 content_block_delta）。

差异不小，但对外只暴露 `route` 一个字符串，协议细节封在 llm.py 的 transport 里。

路由表落在 data/models.json：进程重启后仍在，不需要改环境变量重部署。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Provider:
    """一个可调用的模型端点。"""

    key: str
    label: str
    protocol: str  # openai | anthropic
    base_url: str
    api_key: str
    model: str
    max_tokens: int = 8000

    @property
    def ready(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    def public(self) -> dict:
        """给前端看的字段：绝不外泄 api_key。"""
        return {
            "key": self.key,
            "label": self.label,
            "protocol": self.protocol,
            "model": self.model,
            "base_url": self.base_url,
            "ready": self.ready,
        }


def _env(*names: str, default: str = "") -> str:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return default


def build_providers() -> dict[str, Provider]:
    """从环境变量组装候选端点。

    中转站按 new-api 部署，OpenAI 与 Anthropic 两套端点同域：
      OpenAI   → {base}/v1/chat/completions
      Anthropic→ {base}/v1/messages
    所以 base_url 统一存域名，由 transport 拼路径，少一个配置项就少一个坑。
    """
    ds_key = _env("DEEPSEEK_API_KEY")
    cctq_base = _env("CCTQ_BASE_URL", "ANTHROPIC_BASE_URL", default="https://www.cctq.ai")
    cctq_key = _env("CCTQ_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")

    return {
        # 文本主力：快、便宜、中文稳
        "deepseek": Provider(
            key="deepseek",
            label="DeepSeek",
            protocol="openai",
            base_url=_env("DEEPSEEK_BASE_URL", default="https://api.deepseek.com"),
            api_key=ds_key,
            model=_env("DEEPSEEK_MODEL", default="deepseek-flash"),
            max_tokens=8000,
        ),
        # 代码/长输出主力：deepseek-chat 是非推理模型，不会把额度全烧在思考上，
        # 适合一次性吐几千行 HTML 的成片代码。flash 是推理模型，长输出必被截断。
        "deepseek-chat": Provider(
            key="deepseek-chat",
            label="DeepSeek（代码）",
            protocol="openai",
            base_url=_env("DEEPSEEK_BASE_URL", default="https://api.deepseek.com"),
            api_key=ds_key,
            model=_env("DEEPSEEK_CHAT_MODEL", default="deepseek-chat"),
            max_tokens=8000,
        ),
        # 代码/视觉主力（可选）：中转站 Opus，走 Anthropic 协议。默认视频链路不走它，
        # 需要更强视觉时把 video 路由切回 cctq 即可。
        "cctq": Provider(
            key="cctq",
            label="Opus 5.5（中转）",
            protocol="anthropic",
            base_url=cctq_base,
            api_key=cctq_key,
            model=_env("CCTQ_MODEL", "CLAUDE_OPUS_MODEL", default="claude-opus-5-5"),
            max_tokens=16000,
        ),
        # 兜底：DeepSeek 官方也提供 Anthropic 兼容端点，
        # 没配中转站时仍能跑通视频链路（模型能力弱一些，但协议一致）
        "deepseek-anthropic": Provider(
            key="deepseek-anthropic",
            label="DeepSeek（Anthropic 兼容）",
            protocol="anthropic",
            base_url=_env("DEEPSEEK_ANTHROPIC_BASE_URL", default="https://api.deepseek.com/anthropic"),
            api_key=ds_key,
            model=_env("DEEPSEEK_ANTHROPIC_MODEL", "DEEPSEEK_MODEL", default="deepseek-flash"),
            max_tokens=8000,
        ),
    }


# 用途 → 首选 provider key，按顺序降级
DEFAULT_ROUTES: dict[str, list[str]] = {
    "text": ["deepseek"],
    # 视频成片 + custom 风格设计默认走 Opus（视觉/代码理解强）。
    # deepseek-chat 兜底：本地测试没配 CCTQ 凭据时自动落到它，省钱跑通链路。
    "video": ["cctq", "deepseek-chat"],
}


@dataclass
class ModelRegistry:
    """路由表 + 运行时切换。"""

    data_dir: Path
    providers: dict[str, Provider] = field(default_factory=dict)
    routes: dict[str, list[str]] = field(default_factory=dict)

    @property
    def _state_file(self) -> Path:
        return self.data_dir / "models.json"

    def load(self) -> "ModelRegistry":
        self.providers = build_providers()
        self.routes = {k: list(v) for k, v in DEFAULT_ROUTES.items()}
        path = self._state_file
        if path.exists():
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                for route, chain in (saved.get("routes") or {}).items():
                    # 只接受仍然存在的 provider，避免配置残留指向已删除的端点
                    valid = [p for p in chain if p in self.providers]
                    if valid:
                        self.routes[route] = valid
            except (json.JSONDecodeError, AttributeError):
                pass  # 配置坏了就用默认值，不能让一个坏文件拖垮启动
        return self

    def save(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._state_file.write_text(
            json.dumps({"routes": self.routes}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ------------------------------------------------------------ 查询

    def resolve(self, route: str) -> Provider | None:
        """按用途取第一个「凭据齐全」的 provider；全没配就返回 None。"""
        for key in self.routes.get(route, []):
            p = self.providers.get(key)
            if p and p.ready:
                return p
        return None

    def candidate(self, route: str) -> Provider | None:
        """不看凭据，取首选。用于前端展示「理论上会用哪个模型」。"""
        chain = self.routes.get(route) or []
        return self.providers.get(chain[0]) if chain else None

    def describe(self, route: str) -> dict:
        chosen = self.resolve(route)
        cand = self.candidate(route)
        return {
            "route": route,
            "active": chosen.public() if chosen else None,
            "configured": cand.public() if cand else None,
            "chain": self.routes.get(route, []),
            "fallback": chosen is not None and cand is not None and chosen.key != cand.key,
        }

    def snapshot(self) -> dict:
        return {
            "providers": [p.public() for p in self.providers.values()],
            "routes": {r: self.describe(r) for r in self.routes},
        }

    # ------------------------------------------------------------ 切换

    def set_route(self, route: str, chain: list[str]) -> None:
        if route not in DEFAULT_ROUTES:
            raise ValueError(f"未知用途：{route}")
        unknown = [k for k in chain if k not in self.providers]
        if unknown:
            raise ValueError(f"未知 provider：{unknown}")
        if not chain:
            raise ValueError("路由链不能为空")
        self.routes[route] = chain
        self.save()
