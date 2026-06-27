# Transcript Q&A — 选中即问的字幕理解工具

抓取/转写视频字幕 → 前端渲染 → **选中任意一段文字就能针对那段提问**，AI 用中文讲解，支持多轮追问。
后端调用 LLM 走**已登录订阅的本地 CLI（headless）**，不消耗计费 API key。

## 运行

```bash
cd transcript-qa
uv sync                      # 核心依赖
uv run uvicorn app.main:app --port 8000 --reload
# 浏览器打开 http://localhost:8000
```

转写本地音视频（可选，体积较大，首次会下 Whisper 模型）：

```bash
uv sync --extra whisper
```

## 用法

1. 粘贴 YouTube/视频链接 → **导入链接**；或上传 `.srt/.vtt/.txt` / 音视频文件。
2. 导入后自动生成**章节速览**（按主题分段，每章带时间戳、标题、概括）。点任意章节 → 左侧字幕滚动定位并高亮该章；字幕也按章节分组、带章节标题。
3. 点 **🗣️ 识别说话人**（可选）：让 Claude 按内容推断谁在说，字幕按说话人分色分轮。
   - 是**最佳推断**（无音频佐证），首次约 1–4 分钟，结果会缓存。
   - 默认标 `说话人A/B`；点图例彩色标签可**重命名**为真实姓名（如 Boris / Cat Wu），全局生效。
4. 在字幕里用鼠标**选中一段文字** → 点浮出的 **💬 问这段** → 输入问题。
5. 也可在右侧对话面板**底部输入框直接追问**（无需选中，如“上一个回答没听懂”）。
6. 回答**流式输出**并按 **Markdown** 渲染（标题/列表/代码/引用）。
7. 所有问答 / 章节 / 说话人共用同一会话，上下文自动延续。
8. **按内容去重**：同一视频 URL / 同一文件再次导入会复用同一 doc，连带章节、说话人、会话缓存，秒开。

## 配置

| 环境变量 | 说明 | 默认 |
|---|---|---|
| `LLM_BACKEND` | 问答引擎：`claude` / `codex` | `claude` |
| `CLAUDE_MODEL` | claude 引擎用的模型，接受别名（`sonnet`/`opus`/`haiku`）或完整 id | `claude-sonnet-4-6` |
| `LLM_TIMEOUT_S` | 单次问答超时（秒）；说话人识别较慢，故默认偏大 | `420` |

> 也可在 `/api/ask` 请求里传 `model` 字段按单次覆盖。

- **claude**：`claude -p ... --output-format json`，走 Claude Pro 订阅。多轮用 `--resume <session_id>` 续接，整篇 transcript 只在首轮喂一次。
- **codex**：预留实现，装好 Codex CLI 并用 ChatGPT 账号登录后 `LLM_BACKEND=codex` 即可（字段可能需按实际版本微调）。

## 结构

```
app/
  main.py      FastAPI：/api/ingest/*、/api/summary(分章节)、/api/speakers(说话人,支持分区间)、/api/ask、/api/ask/stream(流式)
web/vendor/    marked + DOMPurify（本地 Markdown 渲染，无 CDN 依赖）
  ingest.py    YouTube字幕 / 音视频(faster-whisper) / srt·vtt·txt → 统一 segments
  llm.py       可插拔 LLM 后端（subprocess 调订阅 CLI）
  prompts.py   中文讲解 system 提示 + 首轮(含transcript)/追问 模板
  store.py     每个 doc 一个 JSON（transcript + session_id）
web/           原生单页：渲染、选区捕获、提问、对话面板
data/          运行期生成（已 gitignore）
```
