# 内容生产 Agent

输入一句话 / 一篇稿 / 一个链接，输出可发布的公众号文案 + 可播放的动画成品。
核心是一套 **Plan → Execute → Reflect** 的编排循环：先拆成结构化方案，再按方案写，
写完全量校验，不合格时按问题严重程度决定"原样重试 / 改参数重跑 / 回退重规划"。

线上演示：`http://<服务器地址>:8080`（demo 限流，仅展示链路）

---

## 为什么值得做

同类项目大多停在「调一次模型出文案」。真正难的是三件事，这个项目就是围绕它们做的：

1. **输入和产物都是多对多的** —— 3 种输入 × 3 种产物 = 9 条链路。
   所以第一件事不是画流程图，是定义中间表征 `ContentBrief`，下游只认它。
2. **生成结果不可信** —— 所以校验必须分层，且大部分检查不该花 token。
3. **重试会烧钱** —— 所以"怎么修"是有档位的，不能让模型一直说"再来一次"。

---

## 架构

```
输入（需求 / 文案 / 链接）
        │
        ▼
   ┌─────────────┐
   │  Planner    │  归一化成 ContentBrief（主题/角度/读者/语气/大纲/事实点/CTA）
   └─────────────┘
        │
        ▼
   ┌─────────────┐        ┌──────────────────────────────┐
   │  Executor   │───────▶│  Reflector                   │
   │  按大纲写作  │        │  L1 契约校验（不花 token）    │
   │  流式输出    │        │  L2 事实溯源（不花 token）    │
   └─────────────┘        │  L3 模型评分（唯一花钱的一层） │
        ▲                 └──────────────────────────────┘
        │                          │
        │      ┌───────────────────┼───────────────────┐
        └──────┤ retry  同参数重跑  │ refine 改参数重跑  │ replan 回退 Planner
               └───────────────────┴───────────────────┘
                                   │
                                   ▼
                          动画产物页（自包含 HTML）
```

### 四个终止条件（少一个就会死循环或烧钱）

1. 通过：综合分 ≥ 78 且无阻断问题
2. 轮数上限（默认 2 轮）
3. 连续两轮无改进
4. 异常：降级到已产出的最高分版本

---

## 关键设计决策

### 1. ContentBrief 是契约，不是数据结构

三种输入先归一化成同一个 schema，下游 `compose / reflect / render` 都只读它。
不这么做的话，每加一种输入就要复制一遍下游逻辑。

契约不稳定，后面全是返工。所以 `app/schemas.py` 里的字段刻意保持精简（10 个）。

### 2. 校验分三层，前两层不花钱

| 层 | 检查内容 | 成本 | 能挡掉 |
|---|---|---|---|
| L1 契约 | 字数偏离、漏节、缺 CTA | 0 | 大部分低级错误 |
| L2 事实 | 正文里的数字能否在事实清单找到出处 | 0 | 编造数据 |
| L3 质量 | 模型按 4 个维度打分 + 指出具体问题 | 一次调用 | 结构松散、语气跑偏 |

很多项目一上来就全交给模型打分，问题和钱都花在第三层。

### 3. `repair_action` 由代码判定，不由模型给

模型只负责"指出问题"，"该重试还是该重新规划"是成本决策。
交给模型会得到一个永远说 `refine` 的复读机。

```python
if total < 55 or blocking >= 2:  return "replan"
if blocking >= 1 or total < 78:  return "refine"
return "pass"
```

**阈值是实测校准出来的，不是拍的。** 第一版按 62/82 设，
真实模型把正常稿子打成 `replan`，每篇多付一次规划成本，耗时从 30 秒涨到 115 秒。

### 4. 阻断 vs 提示：误报率高的规则不能做阻断

第一版把"数字找不到出处"当阻断条件，结果正文里"三步""5 分钟"这类常规表述全被命中，
一次报 6 条，把 75 分的稿子打成 `replan`。

现在 `lint()` 返回 `(问题, 是否阻断)`，规则按误报率分级：
字数离谱、大面积漏节算阻断；缺 CTA、数字无出处只做提示。

### 5. 反思是非关键路径，失败不能拖死主流程

第 2 轮反思时模型偶尔输出非法 JSON（尾逗号、包了代码围栏），
第一版直接抛异常，整篇稿子作废。现在：

- `loads_loose()` 先做宽松解析（去围栏、修尾逗号），0 成本
- 仍失败则降级为纯规则校验，把"评分模型不可用"写进 issues，继续往下走

关键路径和非关键路径必须区别对待，这是可用性问题，不是容错技巧。

### 6. 推理模型的 `max_tokens` 要单独算

`deepseek-flash` 是推理模型，响应里带 `reasoning_content`，**推理 token 和正文共用额度**。
额度给少了会把 JSON 拦腰截断（实测 `finish_reason=length`）。

处理：检测 `finish_reason=length` 时放大 1.7 倍重试，而不是把半截 JSON 塞回去让模型"修格式"。

### 7. 最后一版 ≠ 最好的一版

实测第 2 轮（修订轮）经常反而更差：字数膨胀到 3 倍、语气走样。
所以最终产物取**历史最高分**那一轮，并在结果里记录 `kept_round`。

### 8. 产物是网页，不是 mp4

服务器只有 1.9G 内存，Remotion 要拉 Chromium，峰值 2–4G，一跑就 OOM。

而这类 motion graphics 本质是前端动画，输出网页反而是更好的体验：
秒开、可暂停、可拖进度、清晰度无损、体积几十 KB。要发平台时再离线导出 mp4。

---

## 快速开始

```bash
python -m venv .venv
.venv\Scripts\activate          # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt

# Mock 模式（默认，不消耗 token）
python -m uvicorn app.main:app --reload --port 8000

# 真实模型
set DEEPSEEK_API_KEY=sk-xxx     # Linux/macOS: export DEEPSEEK_API_KEY=sk-xxx
set LLM_MOCK=0
python -m uvicorn app.main:app --reload --port 8000
```

Windows 可以用 `.\run.ps1` / `.\run.ps1 -Real`（自动从系统环境变量读 key）。

打开 http://127.0.0.1:8000

### 自检

```bash
python tests/smoke.py                          # 端到端：起任务 → 读 SSE → 查产物
python tests/preview.py                        # 离线生成 demo 产物，看视觉
# 产物页加 ?static=1 关掉动画看布局，?scene=N 直接看第 N 个场景
```

---

## 目录

```
app/
  config.py          配置，全部走环境变量
  schemas.py         ContentBrief 等契约
  llm.py             DeepSeek 客户端：结构化 + 流式 + 宽松 JSON 解析
  mock.py            Mock 数据源（结构与真实调用完全一致）
  pipeline/
    brief.py         Planner
    compose.py       Executor
    reflect.py       Reflector（三层校验 + 三档决策）
    engine.py        plan-execute-reflect 循环
    renderer.py      产物渲染（自包含动画 HTML）
  main.py            FastAPI：SSE、任务、产物路由
web/                 前端（原生 JS，无构建步骤）
tests/
  smoke.py           端到端冒烟
  preview.py         离线产物预览
```

---

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/generate` | `{input, platform}` → `{task_id}` |
| GET | `/api/tasks/{id}/events` | SSE 事件流 |
| GET | `/r/{id}` | 产物页 |
| GET | `/api/health` | 健康检查 + 当前模式 |

SSE 事件类型：`stage / brief / round_start / delta / reflection / note / done / error`

`delta` 分 `kind=reasoning`（推理过程，前端显示"思考中"）和 `kind=content`（正文，流式渲染）。

---

## 已知限制

- 任务状态在进程内存里。单机演示够用，多实例要换 Redis Stream（接口不用动）
- 链接解析还没做（`P1` 剩余部分），当前只接受文本输入
- 反思的评分器没有用标注样本校准，L3 分数只能做相对比较
- 产物页是 16:9 横屏，竖屏版需要重排布局

---

## License

仅用于个人项目演示。
