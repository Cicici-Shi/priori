# highlight-to-learn

> **读文章、看视频时，选中任意一句就问 AI —— 带着整篇上下文，就地讲清。**
> Reading or watching anything? **Select any line and ask AI** — context-aware, right where you are. Auto notes, optional bilingual subtitles, all on your Claude subscription (no API key).

把任何 YouTube 视频、播客、文章变成**能对话、能精读、能复习**的材料：看到不懂的（一个词、一句话、一个梗）**划一下直接问**，AI 带着整篇上下文讲给你听。内容是外语？**双语字幕逐句对照 + 生词标注**帮你读下去。全程走 **Claude 订阅 CLI**，**零 API 花费**。

> Turn any video, podcast, or article into something you can read, question, and review. Select any line for a context-aware explanation, auto per-chapter notes, and — for foreign-language content — line-by-line bilingual subtitles with vocabulary glossing. Works for native-language reading too. All through your existing Claude subscription, **no API key needed**.

---

## 它能帮你什么 · Why you'll want it

- ✋ **选中即问（核心）· Select-to-ask** — 字幕里划中任意一句，**回车**就让 AI 讲清这句什么意思、背景术语是什么，带着整篇上下文，不用切走打字。
- 🗂️ **结构化笔记 · Auto notes** — 自动按主题切章，每章生成"重读即回忆"的要点笔记，随播放高亮跟随，可一键**导出 Markdown**。
- 🀄 **双语字幕 · Bilingual subtitles** — 逐句"英文一行、中文一行"对照（字幕式），或整段中译；并行生成、永久缓存。
- ✏️ **生词标注 · Vocabulary glossing** — 不常见的词 / 短语自动画下划线 + 小字中文，扫一眼就懂。
- ▶️ **视频联动 · Synced player** — 吸顶播放器，字幕跟播放高亮滚动；点字幕 / 笔记跳转；倍速、进度、上次播放位置全部记忆。
- 💬 **持续对话 · Persistent chat** — 问答整条留存、刷新不丢；一键「新对话」开全新会话，省掉长上下文的开销。
- 💸 **零 API 花费 · Free** — 问答 / 翻译 / 笔记全部通过 `claude -p` 走你的 Claude 订阅，不需要 API key。
- 🖥️ **终端风界面（可隐身）· Terminal-style, low-key** — 整个界面长得像在敲命令行：等宽字体、深色、`$` 提示符、日志式排版。开「隐藏视频」后基本是纯文本，**不想让旁人看到你在看什么时很方便**——扫一眼就是个普通终端。

适合：**边看边学**的自学者、想**精读中英文内容**的人、把长视频/文章**快速变笔记**的学生与研究者。

> P.S. 我自己学东西时都用它，一直在 dogfooding。欢迎你也用。

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

1. 左上角 **`[cfg]`** 里粘贴 **YouTube / 视频链接**，或上传 `.srt / .vtt / .txt` 及音视频文件。
2. 自动生成**章节笔记**（左栏）。播放时字幕高亮跟随、笔记同步定位。
3. 在字幕里**选中一段 → 回车**：AI 直接讲解（含生词、术语、背景）。也可在右侧对话框继续追问。
4. **`[cfg]`** 里可开**隐藏视频 / 逐句双语对照 / 整段翻译**；正文里生词自带下划线 + 中文。
5. 笔记栏可一键**导出 Markdown**（`⤓`）。
6. 倍速、进度、播放位置、对话历史、面板尺寸都会**记在本地**，刷新即恢复。

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
