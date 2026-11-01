"""模型客户端：一套接口，两套协议。

三个入口：
  structured() —— 要求返回 JSON，带校验与重试，用于 Planner / Reflector / 方案推荐
  stream()     —— 流式文本，用于 Executor
  complete()   —— 一次性返回纯文本，用于「生成一个完整 HTML 动画文件」这类长输出

按 route 路由到不同厂商（见 models.py）：
  route="text"  → DeepSeek（快、便宜）
  route="video" → Opus（代码与视觉理解强）

关于 DeepSeek 的一个事实：deepseek-flash 是推理模型，响应里带 reasoning_content。
流式时必须把它和正文分开处理——前端拿它显示"思考中"，
否则用户会觉得卡住了。它按 token 计费，所以 max_tokens 要给推理留余量。

关于 Anthropic 协议的三处差异（踩过才知道）：
  1. system 是顶层字段，不是 messages 里的一个 role
  2. 没有 response_format，JSON 只能靠提示词约束
  3. 截断是 stop_reason == "max_tokens"，不是 finish_reason == "length"
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import AsyncIterator

import httpx

from .config import Settings
from .models import ModelRegistry, Provider

ANTHROPIC_VERSION = "2023-06-01"


class LLMError(RuntimeError):
    pass


def _clean_error(body: str) -> str:
    """错误页可能是 HTML（网关 524/502 的页面）。抓 <title> 或第一段文字，别把源码甩给用户。"""
    m = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()[:120]
    text = re.sub(r"<[^>]+>", " ", body)
    return re.sub(r"\s+", " ", text).strip()[:120] or "（网关返回了空错误页）"


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


def strip_fence(text: str) -> str:
    """去掉 ```html 围栏。模型生成整份文件时几乎必加，直接存会渲染成纯文本。"""
    t = (text or "").strip()
    m = re.match(r"^```[a-zA-Z]*\s*\n(.*)\n```\s*$", t, re.S)
    if m:
        return m.group(1).strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


class LLMClient:
    def __init__(self, cfg: Settings, registry: ModelRegistry) -> None:
        self.cfg = cfg
        self.registry = registry
        self._clients: dict[str, httpx.AsyncClient] = {}

    async def aclose(self) -> None:
        for c in self._clients.values():
            await c.aclose()
        self._clients.clear()

    # ---------------------------------------------------------------- 路由

    def provider(self, route: str) -> Provider:
        p = self.registry.resolve(route)
        if p is None:
            cand = self.registry.candidate(route)
            hint = f"（首选 {cand.label} 未配置凭据）" if cand else ""
            raise LLMError(f"用途 {route} 没有可用模型{hint}，请检查环境变量或路由配置")
        return p

    def _client(self, p: Provider) -> httpx.AsyncClient:
        key = f"{p.key}|{p.base_url}|{p.api_key[-6:]}"
        c = self._clients.get(key)
        if c is None:
            if p.protocol == "anthropic":
                headers = {
                    "content-type": "application/json",
                    "anthropic-version": ANTHROPIC_VERSION,
                    # 中转站认 Bearer，官方认 x-api-key；两个都发，谁都拦不住
                    "authorization": f"Bearer {p.api_key}",
                    "x-api-key": p.api_key,
                }
            else:
                headers = {"Authorization": f"Bearer {p.api_key}"}
            c = httpx.AsyncClient(
                timeout=httpx.Timeout(300.0, connect=15.0),
                headers=headers,
                follow_redirects=True,
            )
            self._clients[key] = c
        return c

    @staticmethod
    def _v1(p: Provider, path: str) -> str:
        """base 可能已经带 /v1，避免拼成 /v1/v1。"""
        base = p.base_url.rstrip("/")
        return base + path if base.endswith("/v1") else base + "/v1" + path

    async def _post(self, p: Provider, url: str, payload: dict) -> httpx.Response:
        client = self._client(p)
        last: Exception | None = None
        for attempt in range(2):
            try:
                resp = await client.post(url, json=payload)
                if resp.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                    await asyncio.sleep(1.5)
                    continue
                if resp.status_code >= 400:
                    raise LLMError(f"[{p.label}] HTTP {resp.status_code}: {_clean_error(resp.text)}")
                return resp
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = exc
                if attempt == 0:
                    await asyncio.sleep(1.0)
                    continue
                raise LLMError(f"[{p.label}] 网络错误：{exc}") from exc
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
        route: str = "text",
    ) -> dict:
        """返回解析后的 JSON。解析或校验失败时，把错误回灌给模型重试一次。

        支持运行时降级：首选 provider 抛错（403/超时/挂了）时，顺着 route 的
        可用链往下试——resolve() 只看凭据配没配，不知道对面通不通，真容错在这里。
        """
        chain = self._chain(route)
        last_exc: Exception | None = None
        for i, p in enumerate(chain):
            try:
                return await self._structured_with(
                    p, system=system, user=user, max_tokens=max_tokens,
                    temperature=temperature, validate=validate,
                )
            except (LLMError, httpx.HTTPError, asyncio.TimeoutError) as exc:
                last_exc = self._as_llm_error(p, exc)
                if i < len(chain) - 1:
                    # 还有后备，换下一个试（403/超时都算）
                    continue
                raise last_exc
        raise last_exc if last_exc else LLMError(f"用途 {route} 没有可用模型")

    async def _structured_with(
        self, p: Provider, *, system: str, user: str, max_tokens: int,
        temperature: float, validate=None,
    ) -> dict:
        """用指定 provider 跑结构化输出（含截断放大 + 格式回灌重试）。"""
        last_error = ""
        budget = max_tokens

        for attempt in range(2):
            if p.protocol == "anthropic":
                content, stop = await self._anthropic_once(p, system, user, budget, temperature)
            else:
                content, stop = await self._openai_once(p, system, user, budget, temperature)

            # 推理模型的 reasoning token 和正文共用 max_tokens 额度，
            # 额度给少了会把 JSON 拦腰截断。这种情况必须放大额度重试，
            # 而不是把半截 JSON 塞回去让模型"修格式"——它修不了。
            if stop in ("length", "max_tokens"):
                last_error = f"输出被截断（{stop}）"
                if attempt == 0:
                    budget = min(16000, int(budget * 1.7))
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
                    user = (
                        user
                        + f"\n\n你上一次的输出不合法（{last_error}）。只输出合法 JSON，不要解释。"
                    )
                    continue
        raise LLMError(f"结构化输出连续失败：{last_error}")

    async def _openai_once(
        self, p: Provider, system: str, user: str, max_tokens: int, temperature: float
    ) -> tuple[str, str]:
        payload = {
            "model": p.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        resp = await self._post(p, self._v1(p, "/chat/completions"), payload)
        choice = (resp.json().get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content") or ""
        return content, choice.get("finish_reason") or ""

    async def _anthropic_once(
        self, p: Provider, system: str, user: str, max_tokens: int, temperature: float
    ) -> tuple[str, str]:
        payload = {
            "model": p.model,
            "max_tokens": min(max_tokens, p.max_tokens),
            "system": system + "\n\n只输出合法 JSON，不要任何额外文字或 Markdown 围栏。",
            "messages": [{"role": "user", "content": user}],
            "temperature": temperature,
        }
        resp = await self._post(p, self._v1(p, "/messages"), payload)
        data = resp.json()
        text = "".join(
            b.get("text", "") for b in data.get("content") or [] if b.get("type") == "text"
        )
        return text, data.get("stop_reason") or ""

    # ---------------------------------------------------------------- 纯文本

    async def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 8000,
        temperature: float = 0.7,
        route: str = "video",
    ) -> tuple[str, Provider]:
        """一次性长输出，返回 (文本, 实际使用的 provider)。

        视频成片代码就靠这个：几千 token 的 HTML 没法流式拼装校验，
        必须整份拿到再跑闸门。

        支持运行时降级：首选 403/超时/挂了，顺着链换下一个 provider 重来。
        """
        chain = self._chain(route)
        last_exc: Exception | None = None
        for i, p in enumerate(chain):
            try:
                return await self._complete_with(p, system=system, user=user,
                                                 max_tokens=max_tokens, temperature=temperature)
            except (LLMError, httpx.HTTPError, asyncio.TimeoutError) as exc:
                last_exc = self._as_llm_error(p, exc)
                if i < len(chain) - 1:
                    continue  # 还有后备，换下一个
                raise last_exc
        raise last_exc if last_exc else LLMError(f"用途 {route} 没有可用模型")

    async def _complete_with(
        self, p: Provider, *, system: str, user: str, max_tokens: int, temperature: float
    ) -> tuple[str, Provider]:
        """用指定 provider 跑一次完整长输出（流式收集）。"""
        if p.protocol == "anthropic":
            # 走流式收集而不是一次性请求：
            # 中转站前面是 Cloudflare，非流式请求等 Opus 生成 3-5 分钟必吃 524；
            # 流式时连接上一直有数据，网关就不会掐。
            text, stop = await self._anthropic_collect(p, system, user, max_tokens, temperature)
            return strip_fence(text), p

        # OpenAI 协议（原生 DeepSeek）同样走流式收集：
        # deepseek-flash 是推理模型，非流式长请求（几千行 HTML）同样会被网关掐，
        # 且 reasoning token 和正文共用额度，必须在流式里把正文攒完整。
        text, stop = await self._openai_collect(p, system, user, max_tokens, temperature)
        return strip_fence(text), p

    def _chain(self, route: str) -> list[Provider]:
        """取该用途的可用 provider 链（凭据齐全的，按配置顺序）。

        空链时复用 provider() 的友好报错。
        """
        chain = self.registry.resolve_chain(route)
        if not chain:
            self.provider(route)  # 抛出「没有可用模型」的原始提示
        return chain

    @staticmethod
    def _as_llm_error(p: Provider, exc: Exception) -> LLMError:
        """把各种底层异常统一成 LLMError，让降级逻辑只认一种。

        HTTP 错误在 _anthropic_* / _openai_* 里已经转成 LLMError 了；
        这里主要兜网络层（超时/连不上）——那种情况同样该换下一个 provider 试。
        """
        if isinstance(exc, LLMError):
            return exc
        if isinstance(exc, (asyncio.TimeoutError, httpx.TimeoutException)):
            return LLMError(f"[{p.label}] 请求超时：{type(exc).__name__}")
        return LLMError(f"[{p.label}] 网络错误：{type(exc).__name__}: {exc}")

    # ---------------------------------------------------------------- 流式

    async def stream(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 2400,
        temperature: float = 0.6,
        route: str = "text",
    ) -> AsyncIterator[tuple[str, str]]:
        """产出 (kind, text)，kind ∈ {reasoning, content}。

        降级只在「还没吐出任何内容」时生效——已经流给前端的东西收不回来，
        中途换 provider 会造成文本拼接错乱，所以首包一到就算建立成功。
        """
        chain = self._chain(route)
        last_exc: Exception | None = None
        for i, p in enumerate(chain):
            if p.protocol == "anthropic":
                gen: AsyncIterator[tuple[str, str]] = self._stream_anthropic(
                    p, system, user, max_tokens, temperature
                )
            else:
                gen = self._stream_openai(p, system, user, max_tokens, temperature)
            produced = False
            try:
                async for item in gen:
                    produced = True
                    yield item
                return
            except (LLMError, httpx.HTTPError, asyncio.TimeoutError) as exc:
                # 网络异常（超时/连不上）没被包装成 LLMError，也要能触发降级；
                # 上游 403/超时都算「这个 provider 现在不能用」。
                last_exc = self._as_llm_error(p, exc)
                if produced or i >= len(chain) - 1:
                    raise last_exc  # 已经吐过内容，或没后备了，就这么报
                continue
        raise last_exc if last_exc else LLMError(f"用途 {route} 没有可用模型")

    async def _anthropic_collect(
        self, p: Provider, system: str, user: str, max_tokens: int, temperature: float
    ) -> tuple[str, str]:
        """流式发起、服务端收集成完整文本。

        和 _stream_anthropic 的区别：那个把增量吐给调用方（写作流式），
        这个攒成整份返回（成片代码需要完整文件再跑闸门）。
        两者都必须走流式——对网关来说，流式 = 一直有数据 = 不掐连接。
        """
        payload = {
            "model": p.model,
            "max_tokens": min(max_tokens, p.max_tokens),
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "stream": True,
            "temperature": temperature,
        }
        client = self._client(p)
        parts: list[str] = []
        stop = ""
        async with client.stream("POST", self._v1(p, "/messages"), json=payload) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode()[:300]
                raise LLMError(f"[{p.label}] HTTP {resp.status_code}: {_clean_error(body)}")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if not chunk or chunk == "[DONE]":
                    continue
                try:
                    obj = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                kind = obj.get("type")
                if kind == "content_block_delta":
                    d = obj.get("delta") or {}
                    if d.get("type") == "text_delta" and d.get("text"):
                        parts.append(d["text"])
                elif kind == "message_delta":
                    d = obj.get("delta") or {}
                    stop = d.get("stop_reason") or stop
        return "".join(parts), stop

    async def _openai_collect(
        self, p: Provider, system: str, user: str, max_tokens: int, temperature: float
    ) -> tuple[str, str]:
        """流式发起、收集成完整文本（OpenAI 协议，原生 DeepSeek 用）。

        和 _anthropic_collect 同款哲学：流式 = 一直有数据 = 网关不掐长请求。
        deepseek-flash 是推理模型，reasoning 单独走 reasoning_content 字段，
        正文走 content；这里只攒正文，reasoning 忽略（成片代码只要结果）。
        """
        payload = {
            "model": p.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": True,
            "temperature": temperature,
            "max_tokens": min(max_tokens, p.max_tokens),
        }
        client = self._client(p)
        parts: list[str] = []
        stop = ""
        async with client.stream("POST", self._v1(p, "/chat/completions"), json=payload) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode()[:300]
                raise LLMError(f"[{p.label}] HTTP {resp.status_code}: {_clean_error(body)}")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if not chunk or chunk == "[DONE]":
                    continue
                try:
                    obj = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                choice = (obj.get("choices") or [{}])[0]
                delta = choice.get("delta") or {}
                if text := delta.get("content"):
                    parts.append(text)
                if fr := choice.get("finish_reason"):
                    stop = fr
        return "".join(parts), stop

    async def _stream_openai(
        self, p: Provider, system: str, user: str, max_tokens: int, temperature: float
    ) -> AsyncIterator[tuple[str, str]]:
        payload = {
            "model": p.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": True,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        client = self._client(p)
        async with client.stream("POST", self._v1(p, "/chat/completions"), json=payload) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode()[:300]
                raise LLMError(f"[{p.label}] HTTP {resp.status_code}: {_clean_error(body)}")
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

    async def _stream_anthropic(
        self, p: Provider, system: str, user: str, max_tokens: int, temperature: float
    ) -> AsyncIterator[tuple[str, str]]:
        payload = {
            "model": p.model,
            "max_tokens": min(max_tokens, p.max_tokens),
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "stream": True,
            "temperature": temperature,
        }
        client = self._client(p)
        async with client.stream("POST", self._v1(p, "/messages"), json=payload) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode()[:300]
                raise LLMError(f"[{p.label}] HTTP {resp.status_code}: {_clean_error(body)}")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if not chunk or chunk == "[DONE]":
                    continue
                try:
                    obj = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                kind = obj.get("type")
                if kind == "content_block_delta":
                    d = obj.get("delta") or {}
                    if d.get("type") == "text_delta" and d.get("text"):
                        yield "content", d["text"]
                    elif d.get("type") == "thinking_delta" and d.get("thinking"):
                        yield "reasoning", d["thinking"]


def estimate_tokens(text: str) -> int:
    """粗估：中文约 1 字 1 token，英文约 4 字符 1 token。用于展示，不用于计费。"""
    han = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return han + max(0, (len(text) - han) // 4)
