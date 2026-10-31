"""DeepSeek 客户端。

两个入口：
  structured() —— 要求返回 JSON，带校验与重试，用于 Planner / Reflector
  stream()     —— 流式文本，用于 Executor

关于模型的一个事实：deepseek-flash 是推理模型，响应里带 reasoning_content。
流式时必须把它和正文分开处理——前端拿它显示"思考中"，
否则用户会觉得卡住了。它按 token 计费，所以 max_tokens 要给推理留余量。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import AsyncIterator

import httpx

from . import mock
from .config import Settings


class LLMError(RuntimeError):
    pass


def loads_loose(text: str) -> dict:
    """宽松 JSON 解析。

    实测必要：模型偶尔会在 JSON 外面包 ```json 围栏、或留一个尾逗号，
    response_format=json_object 也拦不住。这种情况直接报错放弃太浪费——
    重试一次要花 20 秒，而修复一个尾逗号是 0 成本。
    """
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            body = text[start : end + 1]
            body = re.sub(r",(\s*[}\]])", r"\1", body)  # 去尾逗号
            return json.loads(body)
        raise


class LLMClient:
    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self._client = httpx.AsyncClient(
            base_url=cfg.base_url,
            timeout=httpx.Timeout(180.0, connect=15.0),
            headers={"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ 工具

    async def _post(self, payload: dict) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(2):
            try:
                resp = await self._client.post("/chat/completions", json=payload)
                if resp.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                    await asyncio.sleep(1.5)
                    continue
                if resp.status_code >= 400:
                    raise LLMError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                return resp
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = exc
                if attempt == 0:
                    await asyncio.sleep(1.0)
                    continue
                raise LLMError(f"网络错误：{exc}") from exc
        raise LLMError(str(last) if last else "unknown")

    # -------------------------------------------------------------- 结构化

    async def structured(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 2000,
        temperature: float = 0.3,
        validate=None,
    ) -> dict:
        """返回解析后的 JSON。解析或校验失败时，把错误回灌给模型重试一次。"""
        payload = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        last_error = ""
        for attempt in range(2):
            resp = await self._post(payload)
            data = resp.json()
            choice = (data.get("choices") or [{}])[0]
            content = (choice.get("message") or {}).get("content") or ""
            finish = choice.get("finish_reason")

            # 推理模型的 reasoning token 和正文共用 max_tokens 额度，
            # 额度给少了会把 JSON 拦腰截断。这种情况必须放大额度重试，
            # 而不是把半截 JSON 塞回去让模型"修格式"——它修不了。
            if finish == "length":
                last_error = "输出被截断（finish_reason=length）"
                if attempt == 0:
                    payload["max_tokens"] = min(8000, int(payload["max_tokens"] * 1.7))
                    continue
                raise LLMError(last_error)

            try:
                parsed = loads_loose(content)
                if validate is not None:
                    validate(parsed)
                return parsed
            except Exception as exc:  # JSON 错 / schema 错
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt == 0:
                    payload["messages"] = [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                        {
                            "role": "user",
                            "content": f"你上一次的输出不合法（{last_error}）。只输出合法 JSON，不要解释。",
                        },
                    ]
                    continue
        raise LLMError(f"结构化输出连续失败：{last_error}")

    # ---------------------------------------------------------------- 流式

    async def stream(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 2400,
        temperature: float = 0.6,
    ) -> AsyncIterator[tuple[str, str]]:
        """产出 (kind, text)，kind ∈ {reasoning, content}。"""
        payload = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": True,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        async with self._client.stream("POST", "/chat/completions", json=payload) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode()[:300]
                raise LLMError(f"HTTP {resp.status_code}: {body}")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    obj = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                delta = (obj.get("choices") or [{}])[0].get("delta") or {}
                if reasoning := delta.get("reasoning_content"):
                    yield "reasoning", reasoning
                if text := delta.get("content"):
                    yield "content", text


def estimate_tokens(text: str) -> int:
    """粗估：中文约 1 字 1 token，英文约 4 字符 1 token。用于展示，不用于计费。"""
    han = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return han + max(0, (len(text) - han) // 4)
