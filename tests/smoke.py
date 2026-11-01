"""端到端冒烟测试：起一个任务，读完 SSE，检查产物页。

用法（服务已启动）：python tests/smoke.py [base_url]
不依赖 pytest，因为这条链路的价值在于"真的跑一遍"，而不是断言几个字段。
"""

from __future__ import annotations

import json
import sys
import time

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def main() -> int:
    with httpx.Client(base_url=BASE, timeout=120.0) as c:
        h = c.get("/api/health").json()
        print(f"[health] mock={h['mock']} model={h['model']} ready={h['ready']}")

        t0 = time.time()
        r = c.post("/api/generate", json={"input": "给后端工程师讲清楚向量数据库到底解决了什么问题", "platform": "wechat"})
        r.raise_for_status()
        task_id = r.json()["task_id"]
        print(f"[task] {task_id}")

        counts: dict[str, int] = {}
        body_chars = 0
        done = None
        stages: list[str] = []
        reflections: list[dict] = []

        with c.stream("GET", f"/api/tasks/{task_id}/events") as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line.startswith("data:"):
                    continue
                ev = json.loads(line[5:])
                counts[ev["type"]] = counts.get(ev["type"], 0) + 1
                if ev["type"] == "stage":
                    stages.append(ev["stage"])
                    print(f"  → stage {ev['stage']}: {ev.get('label','')}")
                elif ev["type"] == "delta" and ev["kind"] == "content":
                    body_chars += len(ev["text"])
                elif ev["type"] == "reflection":
                    reflections.append(ev["reflection"])
                    print(f"  ✦ 反思 round={ev['reflection']['round']} total={ev['reflection']['total']} action={ev['reflection']['repair_action']}")
                elif ev["type"] == "done":
                    done = ev
                    break
                elif ev["type"] == "error":
                    print("[ERROR]", ev["message"])
                    return 1

        elapsed = time.time() - t0
        print(f"\n[events] {counts}")
        print(f"[stream] 正文 {body_chars} 字，耗时 {elapsed:.1f}s")
        print(f"[stages] {' → '.join(stages)}")

        if not done:
            print("!! 没收到 done 事件")
            return 1

        url = done["url"]
        page = c.get(url)
        page.raise_for_status()
        html = page.text
        # /api/generate 是文章接口，产物页是文章渲染页（视频页才有 scene/const DATA）
        checks = {
            "长度 > 3000": len(html) > 3000,
            "含正文结构": "<h2>" in html or "<h3>" in html or "<p>" in html,
            "无未替换占位符": "__DATA__" not in html and "__TITLE__" not in html,
            "无占位图残留": "](prompt:" not in html,
        }
        print("\n[artifact] " + url)
        for k, v in checks.items():
            print(f"  {'OK ' if v else 'FAIL'} {k}")

        result = done["result"]
        print(f"\n[result] {result['word_count']} 字 · {len(result['rounds'])} 轮 · {result['elapsed_ms']} ms")
        for rd in result["rounds"]:
            print(f"  round{rd['round']}: {rd['total']} 分 · {rd['action']} · {rd['chars']} 字")
        print(f"[reflect] 共 {len(reflections)} 次反思")

        ok = all(checks.values()) and bool(done)
        print("\n==> " + ("PASS" if ok else "FAIL"))
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
