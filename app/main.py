"""FastAPI 入口。

任务模型刻意做得很轻：进程内 dict + asyncio.Queue。
理由：单机、单用户演示、每天几十次请求。上 Celery/Redis 只会多一层排查成本，
而服务器只有 1.9G 内存。真要多实例时，把 TaskState 换成 Redis Stream 即可，
接口不用动。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import settings
from .llm import LLMClient
from .pipeline.engine import run_pipeline
from .pipeline.renderer import save_artifact
from .pipeline.video import save_video, styles_meta

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="内容生产 Agent", version="0.1.0")
llm = LLMClient(settings)


class GenerateIn(BaseModel):
    input: str = Field(min_length=2, max_length=4000)
    platform: str = Field(default="wechat", pattern="^(wechat|xiaohongshu|video)$")
    # 视频风格：auto 表示让模型按内容自己挑
    style: str = Field(default="auto", pattern="^(auto|minimal|apple|editorial|tech|sketch|hud|line|pixel|geo)$")
    # 篇幅档位
    words: str = Field(default="medium", pattern="^(short|medium|long)$")


class VideoIn(BaseModel):
    input: str = Field(min_length=2, max_length=4000)
    platform: str = Field(default="video", pattern="^(wechat|xiaohongshu|video)$")
    # 视频风格：直接从风格库选，空则用默认
    style: str = Field(default="hud")


class TaskState:
    def __init__(self, task_id: str) -> None:
        self.id = task_id
        self.status = "queued"
        self.events: list[dict[str, Any]] = []
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.result: dict[str, Any] | None = None

    async def push(self, ev: dict[str, Any]) -> None:
        ev["seq"] = len(self.events)
        self.events.append(ev)
        await self.queue.put(ev)


TASKS: dict[str, TaskState] = {}


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "mock": settings.mock,
        "model": settings.model,
        "ready": settings.is_ready,
        "tasks": len(TASKS),
    }


@app.post("/api/generate")
async def generate(payload: GenerateIn) -> dict[str, str]:
    if not settings.is_ready:
        raise HTTPException(400, "未配置 DEEPSEEK_API_KEY，且未开启 LLM_MOCK")

    task_id = uuid.uuid4().hex[:12]
    task = TaskState(task_id)
    TASKS[task_id] = task
    asyncio.create_task(_run(task, payload.input, payload.platform, payload.words, payload.style))
    return {"task_id": task_id}


async def _run(task: TaskState, user_input: str, platform: str, words: str, style: str) -> None:
    task.status = "running"
    try:
        result = await run_pipeline(
            task_id=task.id,
            user_input=user_input,
            platform=platform,
            llm=llm,
            words=words,
            style=style,
            emit=task.push,
            max_rounds=settings.max_rounds,
        )
        from .schemas import ContentBrief

        brief = ContentBrief.model_validate(result["brief"])
        secs = result["elapsed_ms"] / 1000
        dur = f"{secs:.1f}s" if secs < 60 else f"{secs / 60:.1f}min"
        meta = (
            f"{result['model']} · 迭代 {len(result['rounds'])} 轮 · "
            f"{result['word_count']} 字 · {dur} · "
            f"{'mock 数据' if result['mocked'] else '真实生成'}"
        )
        save_artifact(brief, result["body"], task.id, settings.data_dir, meta)
        task.result = result
        await task.push({"type": "done", "url": f"/r/{task.id}", "result": result})
        task.status = "done"
    except Exception as exc:  # 任何异常都要让前端知道，不能静默失败
        await task.push({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        task.status = "failed"


def _sse(ev: dict[str, Any]) -> str:
    return f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"


@app.get("/api/tasks/{task_id}/events")
async def events(task_id: str, request: Request) -> StreamingResponse:
    task = TASKS.get(task_id)
    if task is None:
        raise HTTPException(404, "task not found")

    async def gen():
        seen = 0
        # 先补发已产生的事件（支持中途打开/刷新页面）
        for ev in list(task.events):
            yield _sse(ev)
            seen = ev["seq"] + 1
        if task.status in ("done", "failed"):
            return
        while True:
            try:
                ev = await asyncio.wait_for(task.queue.get(), timeout=20)
            except asyncio.TimeoutError:
                yield ": ping\n\n"  # 心跳，防中间层掐断
                if await request.is_disconnected():
                    return
                continue
            if ev["seq"] < seen:
                continue
            seen = ev["seq"] + 1
            yield _sse(ev)
            if ev["type"] in ("done", "error"):
                return

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/r/{artifact_id}")
async def artifact(artifact_id: str) -> FileResponse:
    if not artifact_id.isalnum():
        raise HTTPException(400, "bad id")
    path = settings.data_dir / "artifacts" / f"{artifact_id}.html"
    if not path.exists():
        raise HTTPException(404, "artifact not found")
    return FileResponse(path, media_type="text/html; charset=utf-8")


@app.get("/api/tasks/{task_id}")
async def task_detail(task_id: str) -> dict[str, Any]:
    task = TASKS.get(task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    return {"id": task.id, "status": task.status, "result": task.result}


@app.get("/api/styles")
async def styles() -> dict[str, Any]:
    """风格清单，供前端「视频风格选择器」使用。"""
    return {"styles": styles_meta()}


@app.post("/api/video")
async def video(payload: VideoIn) -> dict[str, str]:
    """视频生成：输入 → 分镜 → 指定风格的动画视频页。

    独立于 /api/generate（那条是文案主链），这是视频主模块的入口。
    暂不走 DeepSeek，纯规则分镜 + 本地 canvas 渲染，不产生 API 费用。
    """
    if not settings.is_ready:
        raise HTTPException(400, "未配置 DEEPSEEK_API_KEY，且未开启 LLM_MOCK")

    task_id = uuid.uuid4().hex[:12]
    # 用 mock brief 生成（不调 API）：从输入文本提取标题/大纲
    from .pipeline.brief import plan
    from .schemas import ContentBrief

    async def _video_run() -> None:
        try:
            brief = await plan(llm, payload.input, payload.platform, "medium")
            meta = f"{brief.platform} · {payload.style} 风格 · mock"
            path = save_video(brief, "", task_id, settings.data_dir, payload.style, meta)
            TASKS[task_id] = TaskState(task_id)
            t = TASKS[task_id]
            t.status = "done"
            t.result = {"brief": brief.model_dump(), "video_url": f"/v/{task_id}"}
            await t.push({"type": "done", "url": f"/v/{task_id}"})
        except Exception as exc:
            t = TASKS.setdefault(task_id, TaskState(task_id))
            t.status = "failed"
            await t.push({"type": "error", "message": f"{type(exc).__name__}: {exc}"})

    TASKS[task_id] = TaskState(task_id)
    asyncio.create_task(_video_run())
    return {"task_id": task_id}


@app.get("/v/{artifact_id}")
async def video_artifact(artifact_id: str) -> FileResponse:
    """视频产物。"""
    if not artifact_id.isalnum():
        raise HTTPException(400, "bad id")
    path = settings.data_dir / "videos" / f"{artifact_id}.html"
    if not path.exists():
        raise HTTPException(404, "video not found")
    return FileResponse(path, media_type="text/html; charset=utf-8")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html", media_type="text/html; charset=utf-8")


app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")
