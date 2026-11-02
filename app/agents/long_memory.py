"""长期语义记忆：mem0 适配层（可选，关掉就是零依赖）。

为什么要引现成模块
------------------
「跨会话记住这个用户是谁、偏好什么」是标准需求，mem0（GitHub 4w+ star）已经把
「抽取事实 → 去重矛盾 → 向量检索 → 按相关度回填 prompt」这套做完了，自己再写一遍
必然漏边界情况。所以我们只写适配层，不写记忆算法。

为什么默认不开
--------------
mem0 必须要一个 **embedding（向量化）模型**，而 DeepSeek 只提供对话接口、没有
embedding 接口。三条路都不便宜：
- 硅基流动 bge-m3：免费，但要一个 key（MEM0_EMBED_KEY）
- fastembed 本地 ONNX：实测这个网络下模型下载 10 分钟没下来（HF 直连超时，
  走 hf-mirror 也慢），1.9G 内存的小机器再挂一个 onnxruntime 也不划算
- 本地 sentence-transformers：要 torch，2G 内存直接判死
所以默认走项目自带的 SQLite 人设记忆（pipeline/memory.py），配好 key 再把
MEM0_ENABLED=1 打开。

它管什么、不管什么
------------------
- 管：跨会话的**用户事实与偏好**（"他做自媒体，写小红书要口语化"、"上次说过不做长文"）
- 不管：本轮**工作记忆**（热榜那 10 条、刚才搜到的网页）——那是结构化数据，
  放 mem0 会被抽成一句事实，条目和链接全丢，还不如直接存 JSON（见 session_context.py）

配置（.env）
------------
    MEM0_ENABLED=1
    MEM0_EMBED_KEY=sk-xxx              # 硅基流动注册即有免费额度
    MEM0_EMBED_BASE=https://api.siliconflow.cn/v1
    MEM0_EMBED_MODEL=BAAI/bge-m3
"""

from __future__ import annotations

import os
import threading
from typing import Any

_LOCK = threading.Lock()
_CLIENT: Any = None
_TRIED = False
_LAST_ERR = ""  # 初始化失败的原因，给 /api/health 报出去，免得黑盒排查


def _env(k: str, d: str = "") -> str:
    return (os.environ.get(k) or d).strip()


def enabled() -> bool:
    """配齐了才开。缺 key 直接当没这个功能，绝不半开状态。"""
    return _env("MEM0_ENABLED") in ("1", "true", "yes") and bool(_env("MEM0_EMBED_KEY"))


def _client():
    """懒加载单例。mem0 首次初始化要连向量库，放在请求里做会拖慢首轮。"""
    global _CLIENT, _TRIED, _LAST_ERR
    if _CLIENT is not None or _TRIED:
        return _CLIENT
    with _LOCK:
        if _CLIENT is not None or _TRIED:
            return _CLIENT
        _TRIED = True
        if not enabled():
            return None
        try:
            # mem0 默认往 PostHog 发匿名统计，服务器不必要的外连一律关掉
            os.environ.setdefault("MEM0_TELEMETRY", "False")
            from mem0 import Memory  # 可选依赖：pip install mem0ai

            data_dir = _env("DATA_DIR", "./data")
            cfg = {
                # 事实抽取用 DeepSeek（项目已有的 key，不额外花钱）
                "llm": {
                    "provider": "deepseek",
                    "config": {
                        # 抽事实是小事，用最便宜的：优先项目已在用的 DEEPSEEK_MODEL
                        # （服务器上是 deepseek-flash），没有才退回 deepseek-chat
                        "model": (_env("MEM0_LLM_MODEL") or _env("DEEPSEEK_MODEL")
                                  or _env("DEEPSEEK_CHAT_MODEL") or "deepseek-chat"),
                        "api_key": _env("DEEPSEEK_API_KEY"),
                        "deepseek_base_url": _env("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
                        "max_tokens": 2000,
                    },
                },
                "embedder": {
                    "provider": "openai",  # OpenAI 兼容协议，指向硅基流动
                    "config": {
                        "model": _env("MEM0_EMBED_MODEL", "BAAI/bge-m3"),
                        "api_key": _env("MEM0_EMBED_KEY"),
                        "openai_base_url": _env("MEM0_EMBED_BASE", "https://api.siliconflow.cn/v1"),
                    },
                },
                # 本地 qdrant（嵌入式，落盘，不用起服务）
                # embedding_model_dims 必须显式给：mem0 认不出我们用的是 OpenAI 兼容
                # 协议指向的第三方模型，默认按 ada-002 建 1536 维的库，而 bge-m3 是
                # 1024 维，写入时直接 shapes (0,1536) vs (1024,) 报错
                "vector_store": {
                    "provider": "qdrant",
                    "config": {
                        "path": os.path.join(data_dir, "mem0"),
                        "on_disk": True,
                        "embedding_model_dims": int(_env("MEM0_EMBED_DIMS", "1024")),
                    },
                },
            }
            _CLIENT = Memory.from_config(cfg)
            _LAST_ERR = ""
            return _CLIENT
        except Exception as exc:
            _LAST_ERR = f"{type(exc).__name__}: {exc}"[:300]
            _CLIENT = None
            return None


def recall(user_key: str, query: str, k: int = 4) -> list[str]:
    """按当前问题取相关记忆。任何异常都返回空——记忆挂了不能影响主流程。"""
    m = _client()
    if not m or not user_key or not query:
        return []
    try:
        # mem0 2.2 改了签名：user_id 不能再放顶层，必须走 filters。
        # 老版本反过来（不认 filters），两种都试一遍才不至于换版本就哑掉
        try:
            res = m.search(query=query[:300], filters={"user_id": user_key}, limit=k)
        except (TypeError, ValueError):
            res = m.search(query=query[:300], user_id=user_key, limit=k)
        items = res.get("results", res) if isinstance(res, dict) else res
        return [str(i.get("memory", "")).strip() for i in items if str(i.get("memory", "")).strip()][:k]
    except Exception:
        return []


def note_turn(user_key: str, user_text: str) -> None:
    """把用户这一轮说的话交给 mem0 抽事实。同步调用，调用方自己决定放后台还是等待。

    只喂用户的话，**不喂助手的答复**：实测把助手回答一起喂，mem0 会把助手自己
    编的建议抽成「用户的偏好」存下来（"用户的小红书文案风格规则（由用户此前定下、
    在对话中细化）…"），越存越歪。用户的偏好一定出现在用户的话里。
    """
    m = _client()
    if not m or not user_key or not (user_text or "").strip():
        return
    try:
        m.add([{"role": "user", "content": user_text[:800]}], user_id=user_key)
    except Exception:
        pass


def status() -> dict:
    """给 /api/health 或排查用：开没开、为什么没开。"""
    if enabled():
        return {"enabled": True, "ready": _client() is not None,
                "error": _LAST_ERR or None}
    return {
        "enabled": False,
        "reason": "未配置 MEM0_ENABLED=1 + MEM0_EMBED_KEY（mem0 需要 embedding 模型）",
    }
