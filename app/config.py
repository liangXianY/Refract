"""全局配置。所有敏感信息走环境变量，不落文件、不入库。"""

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
    api_key: str
    base_url: str
    model: str
    data_dir: Path
    max_rounds: int

    @property
    def is_ready(self) -> bool:
        return self.mock or bool(self.api_key)


def load_settings() -> Settings:
    data_dir = Path(os.environ.get("DATA_DIR", ROOT / "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        mock=os.environ.get("LLM_MOCK", "1") not in ("0", "false", "False"),
        api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
        base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        model=os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"),
        data_dir=data_dir,
        max_rounds=int(os.environ.get("MAX_ROUNDS", "2")),
    )


settings = load_settings()
