# Refract 视频管线 v2 交接文档（video-shotcraft Remotion 方案）

> 更新时间：2026-11-01 14:35 ｜ 作者：WorkBuddy Agent（Claw 通道）
> 供后续 Agent / 开发者接手阅读。读完这份即可了解全部改动、位置、验证方式与已知坑。

---

## 1. 项目位置与背景

- **项目根目录**：`D:\content-agent`（下称 `<root>`）
  - 2026-11-07 从 `D:\workbuddy\2026-10-31-16-04-03\content-agent` 迁出；服务器部署路径 `/home/ubuntu/app/content-agent` 不受影响
- **用户决策**：将 Refract 原视频方案**完全替换**为 video-shotcraft（15.5k star 的 Remotion 视频生成 Agent Skill）方案：
  - 本地测试用 **DeepSeek API**（已跑通）
  - 上线服务器换 **Opus**（配 `CCTQ_API_KEY` 即切，零代码改动）
  - 视频输出**带音效**、产物为 **Remotion 工程目录**，**不转 mp4**（用户明确要求）

## 2. 核心管线（替换后的主链路）

```
用户输入主题/链接
  → brief.plan()            整理成 ContentBrief（LLM）
  → recommend_plans()       出 3 套方案供选（LLM）
  → remotion_video.generate()  核心管线：
      ① 映射：LLM 选配方卡 + 填参数（结构化 JSON）
      ② 逐镜头：LLM 生成单镜头 TSX（小输出）→ 静态闸门校验
         → 不过则带错误信息回炉一次 → 仍不过用确定性模板兜底
      ③ 确定性拼装：Root.tsx / Sequence / Audio / SFX / lib/refract.tsx
      ④ 工程级闸门：全工程完整性检查
  → write_info_page()       生成信息页 HTML（/v/<task_id> 路由可看）
```

- 产物 = Remotion 工程，用户侧 `npm install && npm run studio` 预览（浏览器，无 mp4）。
- 音效由 `sfx.py` 用 stdlib wave 现场合成（pop/whoosh/hit/chord/sparkle/riser 六类），随机源 mulberry32 与画面同种子 → 完全确定性。

## 3. 新增文件（全部在 `<root>` 下）

| 文件 | 作用 |
|---|---|
| `app/pipeline/recipes.py` | 解析 `skills/video-shotcraft/` 124 张配方卡 frontmatter，建索引（`index()` / `stats()`） |
| `app/pipeline/sfx.py` | stdlib 合成 6 类 wav 音效，mulberry32 与画面同源随机 |
| `app/pipeline/remotion_video.py` | **核心主模块**：选卡映射 → 逐镜头 TSX 生成+闸门+回炉+兜底 → 工程拼装 → 工程级闸门 |
| `tests/test_remotion.py` | 单测（mock LLM，不花钱）：SFX 确定性、闸门、映射校验、兜底、全工程生成等 |
| `tests/remotion_probe.py` | 真实 DeepSeek 端到端探针（固定 brief）→ `data/videos/probe-rm-remotion/` |
| `tests/topic_probe.py` | **按主题全链路探针**：brief → 3 方案 → 生成，`python tests/topic_probe.py "任意主题"`（需 `LLM_MOCK=0` + `DEEPSEEK_API_KEY`） |
| `skills/video-shotcraft/` | clone 的配方卡库（ghfast.top 镜像 zip，124 卡 10 类，root 在 `references/shots/`） |

## 4. 修改的既有文件

| 文件 | 改动 |
|---|---|
| `app/main.py` | `_run_video` 切换到 `remotion_video.generate` + `write_info_page`；`opus_video.py` 保留为 legacy，不再走主链路 |

## 5. 闸门（check_shot_tsx）现有检查清单——都是真实炸点换来的

1. 长度 ≥400、括号配对、`export default` 存在
2. 必须用 `useCurrentFrame`（不是 Remotion 组件就打回）
3. 禁 `Math.random` / `Date.now` / 定时器 / fetch（确定性要求）
4. import 白名单：仅 `remotion` / `react` / `../lib/refract`
5. 必须有中文文案 / 渲染 `{title}`（含逐字渲染放行规则：`title.split` / `Array.from(title)` / `[...title]`）
6. props 未解构就使用（`title/accent/accent2/bg/lines`）→ 运行时 ReferenceError
7. **style 对象重复 key**（`_dup_style_keys`）→ tsc TS1117。实例：同一 style 里写两次 `opacity`
8. **lib 组件不存在 props**（`_bad_component_props`）→ tsc TS2322。`lib/refract.tsx` 的 `Caption` 只接受 `text/accent/color/size/y/weight`；实测模型传过 `opacity/fontSize/letterSpacing/texts(数组)`
9. **interpolate inputRange 单调性**（`_nonmonotonic_ranges`）→ 运行时黑屏。做法：正则抓 `const NAME = 数字` 常量表，代入 inputRange 各元素做白名单算术 eval，相邻对验严格递增；未知标识符（`durationInFrames`、循环变量 `ci` 等）求不出值就跳过该对，不误杀

工程级闸门另有：shot 文件齐全、lib/refract.tsx 存在、index.ts 注册 Root、Root.tsx 挂载全部镜头、有音轨、Sequence `from` 必须花括号（`from={6}` 不是 `from=6`）。

## 6. 版本与依赖坑（重要）

- `package.json` 全家桶**钉精确版本 remotion 4.0.539**。4.0.0（2022 初版）有 Audio 类型 bug（报 placeholder/onPointerEnterCapture 缺失）。
- ⚠️ `^4.0.0` 会被已装的 4.0.0 满足导致 npm 不升级，**必须钉精确版本**。
- lib 路径是 `src/lib/refract.tsx`（含 JSX 必须 .tsx，.ts 会被 tsc 打回）。

## 7. 模型路由（上线换 Opus 的方式）

- `app/models.py` 路由表：video 类 = `cctq(Opus)` → fallback `deepseek-chat`。
- 本地无 CCTQ 凭据自动落 DeepSeek；**上线只需配 `CCTQ_API_KEY`，零代码改动**。
- DeepSeek 走 OpenAI 兼容 `/chat/completions`，凭据读 `DEEPSEEK_API_KEY`（本机已配在环境变量）。

## 8. 验证状态（全部通过）

- 单测：`LLM_MOCK=1 python tests/test_remotion.py` → ALL PASSED（闸门 9 条含新增 3 类全覆盖）；`test_video.py` / `test_mcp.py` RC=0。
- 真实端到端：`tests/topic_probe.py "大模型的发展史"` → DeepSeek-chat，**122s**，方案A「剖面盖楼记」37s / 8 镜头全选真配方卡 / 0 模板兜底 / 22 段音效。
- 产物工程：`<root>/data/videos/topic-probe-remotion/`（`npx tsc --noEmit` RC=0，已手工修复 3 类闸门漏网问题）。
- 信息页：`<root>/data/videos/topic-probe.html`。
- Studio 预览曾起在 localhost:3000（本机，会话结束即停）。

## 9. 手工修复记录（生成产物内，历史参考）

首跑 LLM 输出被闸门漏过的三类问题已在产物工程手工修复（同时全部转化为闸门规则）：
- `Shot02.tsx`：Caption 传了不存在的 props → 改为合法 props
- `Shot03.tsx` / `Shot04.tsx`：style 重复 `opacity` → 删重复项
- `Shot07.tsx`：Caption 被传 `texts` 数组 → 改为内联 div 渲染末行字幕
- `Shot03.tsx`：inputRange `[0, 0.15*135, 0.88*135, BRAKE]`（118.8 > 100 不单调）→ `0.88` 改 `0.7`

## 10. 运行方式速查

```bash
# 单测（不花钱）
cd <root> && LLM_MOCK=1 .venv/Scripts/python.exe tests/test_remotion.py

# 按主题真实出片（需要 DEEPSEEK_API_KEY）
cd <root> && LLM_MOCK=0 .venv/Scripts/python.exe tests/topic_probe.py "主题"

# 预览某条片子
cd <root>/data/videos/topic-probe-remotion && npm run studio   # 依赖已装好

# Web 服务（完整产品形态）
cd <root> && LLM_MOCK=0 .venv/Scripts/python.exe -m uvicorn app.main:app
```

## 11. 部署备忘（用户服务器 1.9G 内存）

- **跑得通**：生成链路（LLM 全是远程 API，Python ~200MB）
- **勉强**：服务器上开 npm studio（~500-800MB，建议加 2G swap + `NODE_OPTIONS=--max-old-space-size=1024`）
- **跑不通**：服务器上 remotion render 出 mp4（headless Chrome 1.5G+）——已按用户要求规避（不出 mp4）
- 推荐形态：服务器只跑 FastAPI 生成 + 信息页 `/v/` 路由；预览由用户本地 studio 打开
- **尚未部署**：目前一切仅在本机，无公网访问

## 12. 工作日志（按时间）

1. clone video-shotcraft 到 `skills/`（github 直连超时，ghfast.top 镜像 zip 48MB 成功）
2. 写 `recipes.py`（卡索引）、`sfx.py`（音效合成）、`remotion_video.py`（主管线）
3. `main.py` 主链路切换，`opus_video.py` 转 legacy
4. 单测 8 项 + 真实探针跑通（~55-74s，6 镜头 0 兜底）
5. 收尾补验修坑：remotion 钉 4.0.539、兜底模板 `}` 多余、闸门放行逐字渲染 + 新增未解构 lines 检测 → tsc RC=0
6. 今日主题探针（大模型发展史）：新增 `topic_probe.py`，122s 出片成功
7. 真实 LLM 首跑暴露 3 类闸门漏网（重复 key / 不存在 props / inputRange 不单调）→ 全部转为闸门规则 + 单测，全套 RC=0
8. Studio 起在 localhost:3000 供用户预览，报错后定位修复 Shot03 单调性问题
