# highlight-to-learn

> **看懂任何视频 / 文章 —— 划中任意一句，AI 立刻带上下文给你讲。**
> Understand any video or article with AI — bilingual subtitles, auto notes, and **select any line to ask**. Zero API cost; runs on your Claude subscription.

把一段 YouTube 讲座、播客或英文文章，变成**能对话、能精读、能复习**的学习材料。英语没那么好也不怕：双语字幕逐句对照、生词自动标注、看不懂的地方**划一下就问 AI**。全程走你已登录的 **Claude 订阅 CLI**，**不花一分 API 钱**。

> Turn any English video/article into study material you can read, question, and review. Bilingual line-by-line subtitles, automatic vocabulary glossing, per-chapter notes, and one-keystroke "explain this" on anything you select — all through your existing Claude subscription, **no API key needed**.

---

## 它能帮你什么 · Why you'll want it

- ✋ **选中即问（核心）· Select-to-ask** — 字幕里划中任意一句，**回车**就让 AI 讲清这句什么意思、背景术语是什么，带着整篇上下文，不用切走打字。
- 🀄 **双语字幕 · Bilingual subtitles** — 逐句"英文一行、中文一行"对照（字幕式），或整段中译；并行生成、永久缓存。
- ✏️ **生词标注 · Vocabulary glossing** — 不常见的词 / 短语自动画下划线 + 小字中文，扫一眼就懂。
- 🗂️ **结构化笔记 · Auto notes** — 自动按主题切章，每章生成"重读即回忆"的要点笔记，随播放高亮跟随。
- ▶️ **视频联动 · Synced player** — 吸顶播放器，字幕跟播放高亮滚动；点字幕 / 笔记跳转；倍速、进度、上次播放位置全部记忆。
- 💬 **持续对话 · Persistent chat** — 问答整条留存、刷新不丢；一键「新对话」开全新会话，省掉长上下文的开销。
- 💸 **零 API 花费 · Free** — 问答 / 翻译 / 笔记全部通过 `claude -p` 走你的 Claude 订阅，不需要 API key。

适合：**用英文视频自学**的人、想**精读外语内容**的语言学习者、需要把长讲座**快速变笔记**的学生与研究者。

---

## 快速开始 · Quick start

前置：[`uv`](https://docs.astral.sh/uv/)（Python 3.12）+ 已登录的 [`claude` CLI](https://docs.claude.com/claude-code)（Claude 订阅）。

```bash
git clone https://github.com/Cicici-Shi/highlight-to-learn.git
cd highlight-to-learn
uv sync
uv run uvicorn app.main:app --port 8000 --reload
# 浏览器打开 http://localhost:8000
```

转写本地音视频（可选，首次会下 Whisper 模型）：

```bash
uv sync --extra whisper
```

## 用法 · Usage

1. 左上角 ⚙ 里粘贴 **YouTube / 视频链接**，或上传 `.srt / .vtt / .txt` 及音视频文件。
2. 自动生成**章节笔记**（左栏）。播放时字幕高亮跟随、笔记同步定位。
3. 在字幕里**选中一段 → 回车**：AI 直接讲解（含生词、术语、背景）。也可在右侧对话框继续追问。
4. ⚙ 里可开**逐句双语对照 / 整段翻译**；正文里生词自带下划线 + 中文。
5. 倍速、进度、播放位置、对话历史、面板尺寸都会**记在本地**，刷新即恢复。

## 配置 · Config

| 环境变量 | 说明 | 默认 |
|---|---|---|
| `LLM_BACKEND` | 问答引擎：`claude` / `codex` | `claude` |
| `CLAUDE_MODEL` | claude 引擎用的模型（接受 `sonnet`/`opus`/`haiku` 别名或完整 id） | `claude-sonnet-4-6` |
| `LLM_TIMEOUT_S` | 单次调用超时（秒） | `420` |

- **claude**：`claude -p ... --output-format json`，走 Claude 订阅；多轮用 `--resume` 续接，整篇 transcript 只在首轮喂一次。
- **codex**：预留实现，装好 Codex CLI 并登录后 `LLM_BACKEND=codex` 即可。

## 结构 · Layout

```
app/        FastAPI 后端
  main.py     /api/ingest、/summary(章节)、/notes(笔记)、/glossary(生词)、
              /translate、/ask·/ask/stream(问答)、/session/new
  ingest.py   YouTube 字幕 / 音视频(faster-whisper) / srt·vtt·txt → 统一 segments
  llm.py      可插拔 LLM 后端（subprocess 调订阅 CLI）
  prompts.py  中文讲解 / 笔记 / 生词 / 翻译 提示
  store.py    每个 doc 一个 JSON（transcript + 章节 + 缓存 + 会话）
web/        原生单页（终端风 UI）：渲染、选区提问、视频联动、双语 / 生词 / 笔记
  vendor/     marked + DOMPurify（本地 Markdown 渲染，无 CDN 依赖）
data/       运行期生成（已 gitignore）
```

> 字幕数据、翻译、笔记都缓存在本地 `data/`，**不会上传**；问答全程在本机通过订阅 CLI 完成。
