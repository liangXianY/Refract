"""编排引擎：plan → execute → reflect 循环。

终止条件有四个，缺一个就可能死循环或者烧钱：
  1. 通过（action=pass）
  2. 轮数上限
  3. 连续两轮无改进
  4. 异常（直接降级到上一轮产物）
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from ..llm import LLMClient, estimate_tokens
from ..schemas import ContentBrief
from .brief import plan
from .compose import compose_stream
from .reflect import reflect

Emitter = Callable[[dict[str, Any]], Awaitable[None]]


async def run_pipeline(
    *,
    task_id: str,
    user_input: str,
    platform: str,
    llm: LLMClient,
    words: str = "medium",
    style: str = "auto",
    emit: Emitter,
    max_rounds: int = 2,
    persona: str = "",
    history_hint: str = "",
) -> dict[str, Any]:
    """编排循环。

    persona / history_hint：来自项目记忆与长期记忆。
    传进来就把「人设」约束进 Writer、「历史结构表现」约束进 Reflector——
    这样记忆才真的被用上，而不是只存在库里。
    """
    started = time.time()
    await emit({"type": "stage", "stage": "plan", "label": "拆解输入，生成内容方案"})

    brief: ContentBrief = await plan(llm, user_input, platform, words)
    await emit({"type": "brief", "brief": brief.model_dump()})

    body = ""
    rounds: list[dict[str, Any]] = []
    tokens = 0
    best: tuple[float, str, int] = (0.0, "", 0)
    no_gain = 0
    last_total = -1.0

    for rn in range(1, max_rounds + 1):
        hints = rounds[-1]["issues"] if rounds else None

        await emit({"type": "stage", "stage": "compose", "round": rn, "label": f"第 {rn} 轮写作"})
        await emit({"type": "round_start", "round": rn})

        parts: list[str] = []
        async for kind, text in compose_stream(llm, brief, hints, persona=persona):
            parts.append(text)
            await emit({"type": "delta", "kind": kind, "text": text})
        body = "".join(parts)
        tokens += estimate_tokens(body)

        await emit({"type": "stage", "stage": "reflect", "round": rn, "label": f"第 {rn} 轮反思校验"})
        refl = await reflect(
            llm, brief, body, round_no=rn, max_rounds=max_rounds, history_hint=history_hint
        )
        rounds.append(
            {
                "round": rn,
                "total": refl.total,
                "scores": refl.scores,
                "issues": refl.issues,
                "action": refl.repair_action,
                "chars": len(body),
            }
        )
        await emit({"type": "reflection", "reflection": refl.model_dump()})

        if refl.total > best[0]:
            best = (refl.total, body, rn)
        else:
            no_gain += 1

        # 终止判断
        if refl.repair_action == "pass":
            break
        if no_gain >= 2:
            await emit({"type": "note", "text": "连续两轮无改进，停止迭代"})
            break
        if refl.repair_action == "replan" and rn == 1:
            # 只在第一轮回退重规划：跑到最后一轮再 replan，等于多付一次规划成本却没有机会重写
            await emit({"type": "note", "text": "判定方案层面有问题，回退重规划"})
            brief = await plan(
                llm,
                user_input + "\n\n上一版方案的问题（必须避免）：\n" + "\n".join(refl.issues),
                platform,
                words,
            )
            await emit({"type": "brief", "brief": brief.model_dump()})
        last_total = refl.total
        await asyncio.sleep(0.05)

    # 取历史最高分的那一版，而不是最后一版。
    # 实测第 2 轮（修订轮）经常反而更差：字数膨胀、语气走样。
    # 所以"最后一版"不等于"最好的一版"，这个取舍必须显式做。
    final_body = body if not best[1] else best[1]
    kept_round = best[2] if best[2] else len(rounds)

    await emit({"type": "stage", "stage": "render", "label": "生成动画产物"})
    elapsed = int((time.time() - started) * 1000)
    result = {
        "task_id": task_id,
        "brief": brief.model_dump(),
        "body": final_body,
        "rounds": rounds,
        "kept_round": kept_round,
        "word_count": len(final_body),
        "tokens": tokens,
        "elapsed_ms": elapsed,
        "model": llm.cfg.model,
        "mocked": llm.cfg.mock,
        "style": style,
        # 结构标识 + 最终分：上层拿它写进长期记忆（数据回流）
        "structure": "-".join(s.get("heading", "")[:6] for s in brief.model_dump()["outline"])[:120],
        "final_score": best[0] if best[0] else (rounds[-1]["total"] if rounds else 0.0),
    }
    return result
