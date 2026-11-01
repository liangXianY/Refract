# Refract MCP 服务接入说明

把 Refract 的能力（抓网页 / 热榜 / 联网搜索 / 写稿 / 出视频 / 记忆）封装成 MCP 工具，
供外部 AI 客户端（Claude Desktop、Cursor、WorkBuddy 等）直接调用。

## 工具清单（10 个）

| 工具 | 能力 | 说明 |
|---|---|---|
| `fetch_url_tool` | 网页读取 | 抓 URL 抽正文（标题/字数/正文前 1500 字） |
| `hot_topics` | 热点读取 | 拉平台热榜（小红书/微博/知乎/抖音/B站/百度） |
| `web_search` | 联网搜索 | Tavily 搜索，带来源链接 |
| `lookup_hot` | 热点详情 | 针对热点标题搜正文，理解热点讲什么 |
| `compose_article` | 写稿 | 公众号/小红书/口播稿 |
| `plan_video` | 分镜 | 需求 → 分镜表 |
| `render_video_page` | 出片 | 生成可播放动画 HTML |
| `memory_write` | 记忆写 | 存人设/禁用词/语气 |
| `memory_recall` | 记忆读 | 读项目记忆 |
| `memory_structures` | 结构表现 | 历史内容结构数据回流 |

## 接入方式

### 1. Claude Desktop

编辑 `claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "refract": {
      "command": "D:/workbuddy/2026-10-31-16-04-03/content-agent/.venv/Scripts/python.exe",
      "args": ["D:/workbuddy/2026-10-31-16-04-03/content-agent/mcp_server.py"],
      "env": {
        "DEEPSEEK_API_KEY": "你的 key",
        "TAVILY_API_KEY": "你的 key（web_search 用）",
        "HOT_API_BASE": "http://127.0.0.1:4399"
      }
    }
  }
}
```

### 2. WorkBuddy

在 WorkBuddy 的自定义连接器里，选「MCP」，command/args 同上。

## 依赖的 key（按需配）

- `DEEPSEEK_API_KEY`：写稿、分镜、出片用（必需，否则这些工具报错）
- `TAVILY_API_KEY`：`web_search` / `lookup_hot` 用（可选，缺了这两个工具降级报错）
- `HOT_API_BASE`：热榜数据源，默认公共实例 `60s.viki.moe`；自部署填 `http://127.0.0.1:4399`

## 自检

```bash
cd content-agent
.venv/Scripts/python.exe tests/mcp_e2e.py
```

输出「连接成功，共 10 个工具」即链路通。
