"""全局配置。所有敏感信息走环境变量，不落文件、不入库。

模型选择不在这里——见 models.py 的路由表。
这个文件只管「跑在什么模式、数据放哪」，不管「用哪个模型」。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """极简 .env 解析，避免为一个功能引入额外依赖。已存在的环境变量优先。"""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    mock: bool
    data_dir: Path
    max_rounds: int

    @property
    def is_ready(self) -> bool:
        """mock 模式永远就绪；真实模式要求文本链路上至少有一个配好凭据的端点。"""
        if self.mock:
            return True
        from . import config as _self

        return _self.registry.resolve("text") is not None

    @property
    def model(self) -> str:
        """当前文本主力模型名，用于健康检查与前端展示。"""
        from . import config as _self

        p = _self.registry.resolve("text") or _self.registry.candidate("text")
        return p.model if p else "(未配置)"


def load_settings() -> Settings:
    data_dir = Path(os.environ.get("DATA_DIR", ROOT / "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        mock=os.environ.get("LLM_MOCK", "1") not in ("0", "false", "False"),
        data_dir=data_dir,
        max_rounds=int(os.environ.get("MAX_ROUNDS", "2")),
    )


settings = load_settings()

# 路由表是进程级单例：多处代码共享同一份「哪个模型干哪类活」的状态
from .models import ModelRegistry  # noqa: E402

registry = ModelRegistry(data_dir=settings.data_dir).load()
