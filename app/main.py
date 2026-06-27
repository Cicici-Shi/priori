"""FastAPI 入口：/ingest、/ask，以及静态前端。"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import ingest, llm, prompts, store

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="Transcript Q&A")


@app.middleware("http")
async def no_cache(request, call_next):
    """本地调试工具：禁用浏览器缓存，刷新即拿最新前端资源。"""
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store"
    return resp


# --------------------------------------------------------------------------- #
# /ingest — 三种来源 → transcript
# --------------------------------------------------------------------------- #

class IngestUrl(BaseModel):
    url: str


def _doc_response(doc_id: str, doc: dict) -> dict:
    """统一的 doc 响应：带上已缓存的章节/说话人，前端可直接复用、无需重算。"""
    return {
        "doc_id": doc_id,
        "title": doc.get("title"),
        "video_id": ingest.extract_video_id(doc.get("source") or ""),  # YouTube 源才有，用于嵌入播放器
        "segments": doc["segments"],
        "chapters": doc.get("chapters", []),
        "turns": doc.get("turns", []),
        "speakers": doc.get("speakers", []),
        "cleaned_map": doc.get("cleaned_map", {}),
        "translated_map": doc.get("translated_map", {}),
        "chat": doc.get("chat", []),
        "reused": True,
    }


@app.post("/api/ingest/url")
def ingest_url(body: IngestUrl):
    # 先按视频 id / URL 去重：同一视频已导入过就直接复用（含章节/说话人/会话缓存）
    key = ingest.extract_video_id(body.url) or body.url.strip()
    existing_id = store.doc_id_for_key(key)
    if existing_id and (doc := store.load(existing_id)):
        return _doc_response(existing_id, doc)

    try:
        segments, title = ingest.from_youtube(body.url)
    except ingest.IngestError as e:
        raise HTTPException(status_code=400, detail=str(e))
    doc_id, reused = store.create_keyed(key, segments, source=body.url, title=title)
    doc = store.load(doc_id)
    return _doc_response(doc_id, doc)


@app.post("/api/ingest/file")
async def ingest_file(file: UploadFile = File(...)):
    raw = await file.read()
    # 按文件内容去重：同一文件再传直接复用缓存
    key = "file:" + hashlib.sha1(raw).hexdigest()
    existing_id = store.doc_id_for_key(key)
    if existing_id and (doc := store.load(existing_id)):
        return _doc_response(existing_id, doc)

    suffix = Path(file.filename or "upload").suffix
    # webvtt / whisper 需要真实文件路径，先落临时盘。
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(raw)
        tmp_path = Path(tmp.name)
    try:
        segments, title = ingest.from_file(file.filename or "upload", raw, tmp_path)
    except ingest.IngestError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        tmp_path.unlink(missing_ok=True)
    doc_id, _ = store.create_keyed(key, segments, source=file.filename or "upload", title=title)
    doc = store.load(doc_id)
    return _doc_response(doc_id, doc)


# --------------------------------------------------------------------------- #
# /ask — 选中片段 + 问题 → 答案（多轮靠 session 续接）
# --------------------------------------------------------------------------- #

class AskBody(BaseModel):
    doc_id: str
    question: str
    selected_text: str = ""
    seg_range: list[int] | None = None
    backend: str | None = None  # 可覆盖默认引擎
    model: str | None = None  # 可覆盖默认模型（仅 claude 引擎）


class SummaryBody(BaseModel):
    doc_id: str
    backend: str | None = None
    model: str | None = None
    force: bool = False  # 忽略缓存重新生成
    seg_range: list[int] | None = None  # 说话人识别：只识别该片段区间


def _fmt_ts(sec) -> str:
    if sec is None:
        return ""
    sec = int(float(sec))
    return f"{sec // 60:02d}:{sec % 60:02d}"


def _parse_chapters(text: str, n: int) -> list[dict]:
    """解析竖线分隔的章节行：`start | end | title | summary`，索引夹到 [0, n-1]。

    比 JSON 稳健——概括里的引号/标点不会破坏格式。
    """
    chapters = []
    for line in text.splitlines():
        line = line.strip().lstrip("-•").strip()
        if line.count("|") < 3:
            continue
        a, b, title, summ = (p.strip() for p in line.split("|", 3))
        # 起止字段必须是数字，否则视为非章节行（表头/说明等）跳过
        if not (a.lstrip("#").strip().isdigit() and b.strip().isdigit()):
            continue
        start = max(0, min(int(re.sub(r"\D", "", a)), n - 1))
        end = max(start, min(int(re.sub(r"\D", "", b)), n - 1))
        chapters.append({
            "title": title or "（未命名）",
            "start": start,
            "end": end,
            "summary": summ,
        })
    if not chapters:
        raise HTTPException(status_code=502, detail="未能从模型输出解析出章节。")

    # 归一化：保证按 start 升序、首章从 0、末章到 n-1、首尾相接不重叠不留空，
    # 这样前端按章节分组时不会漏掉任何一句字幕。
    chapters.sort(key=lambda c: c["start"])
    dedup = []
    for ch in chapters:
        if dedup and ch["start"] <= dedup[-1]["start"]:
            continue  # 起点不严格递增的丢掉，避免边界打架
        dedup.append(ch)
    dedup[0]["start"] = 0
    for i, ch in enumerate(dedup):
        ch["end"] = (dedup[i + 1]["start"] - 1) if i + 1 < len(dedup) else (n - 1)
        ch["end"] = max(ch["end"], ch["start"])
    return dedup


def _parse_turns(text: str, n: int) -> list[dict]:
    """解析说话人轮次行：`start | end | speaker`，归一化为连续覆盖全片段。"""
    turns = []
    for line in text.splitlines():
        line = line.strip().lstrip("-•").strip()
        if line.count("|") < 2:
            continue
        a, b, speaker = (p.strip() for p in line.split("|", 2))
        if not (a.lstrip("#").strip().isdigit() and b.strip().isdigit()):
            continue
        start = max(0, min(int(re.sub(r"\D", "", a)), n - 1))
        end = max(start, min(int(re.sub(r"\D", "", b)), n - 1))
        turns.append({"start": start, "end": end, "speaker": speaker or "说话人"})
    if not turns:
        raise HTTPException(status_code=502, detail="未能从模型输出解析出说话人轮次。")

    turns.sort(key=lambda t: t["start"])
    dedup = []
    for t in turns:
        if dedup and t["start"] <= dedup[-1]["start"]:
            continue
        dedup.append(t)
    dedup[0]["start"] = 0
    for i, t in enumerate(dedup):
        t["end"] = (dedup[i + 1]["start"] - 1) if i + 1 < len(dedup) else (n - 1)
        t["end"] = max(t["end"], t["start"])
    return dedup


def _speakers_list(turns: list[dict]) -> list[str]:
    seen, names = set(), []
    for t in turns:
        if t["speaker"] not in seen:
            seen.add(t["speaker"])
            names.append(t["speaker"])
    return names


@app.post("/api/speakers")
def speakers(body: SummaryBody):
    """推断说话人轮次。

    - 不传 seg_range：整篇识别（慢，结果缓存）。
    - 传 seg_range=[s,e]：只识别该区间（用于分章节渐进识别），结果**合并进**已缓存 turns。
    返回当前（合并后）的全部 turns + 说话人列表。
    """
    doc = store.load(body.doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在，请重新导入。")

    segments = doc["segments"]
    n = len(segments)
    rng = body.seg_range
    is_range = bool(rng and len(rng) == 2)

    # 整篇且已有缓存 → 直接返回
    if not is_range and not body.force and doc.get("turns"):
        return {"turns": doc["turns"], "speakers": doc.get("speakers", []),
                "cached": True, "model": None}

    session_id = doc.get("session_id")
    q = prompts.speakers_q(rng if is_range else None)
    if session_id:
        prompt = prompts.follow_up("", q, None, segments)
    else:
        prompt = prompts.first_turn(segments, "", q, None)

    try:
        backend = llm.get_backend(body.backend, body.model)
        answer, new_session = backend.ask(prompt, session_id)
    except llm.LLMError as e:
        raise HTTPException(status_code=502, detail=str(e))

    if new_session and new_session != session_id:
        store.set_session(body.doc_id, new_session)

    new_turns = _parse_turns(answer, n)

    if is_range:
        s, e = max(0, rng[0]), min(rng[1], n - 1)
        new_turns = [t for t in new_turns if t["start"] <= e and t["end"] >= s]
        for t in new_turns:  # 夹到区间内
            t["start"] = max(t["start"], s)
            t["end"] = min(t["end"], e)
        # 合并：去掉已缓存里与本区间重叠的轮次，再并入新轮次
        kept = [t for t in (doc.get("turns") or []) if t["end"] < s or t["start"] > e]
        merged = sorted(kept + new_turns, key=lambda t: t["start"])
        turns = merged
    else:
        turns = new_turns

    for t in turns:
        t["start_ts"] = _fmt_ts(segments[t["start"]].get("start"))
    names = _speakers_list(turns)
    store.update(body.doc_id, turns=turns, speakers=names)
    return {"turns": turns, "speakers": names, "session_id": new_session,
            "model": getattr(backend, "model", None)}


@app.post("/api/summary")
def summary(body: SummaryBody):
    """生成分章节摘要：每章带 transcript 片段区间 + 时间戳，供前端定位。"""
    doc = store.load(body.doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在，请重新导入。")

    if not body.force and doc.get("chapters"):
        return {"chapters": doc["chapters"], "cached": True, "model": None}

    segments = doc["segments"]
    session_id = doc.get("session_id")
    q = prompts.chapters_instruction(segments)
    if session_id:
        prompt = prompts.follow_up("", q, None, segments)
    else:
        prompt = prompts.first_turn(segments, "", q, None)

    try:
        backend = llm.get_backend(body.backend, body.model)
        answer, new_session = backend.ask(prompt, session_id)
    except llm.LLMError as e:
        raise HTTPException(status_code=502, detail=str(e))

    if new_session and new_session != session_id:
        store.set_session(body.doc_id, new_session)

    chapters = _parse_chapters(answer, len(segments))
    for ch in chapters:
        ch["start_ts"] = _fmt_ts(segments[ch["start"]].get("start"))
        ch["end_ts"] = _fmt_ts(segments[ch["end"]].get("end") or segments[ch["end"]].get("start"))

    store.update(body.doc_id, chapters=chapters)  # 缓存
    return {"chapters": chapters, "session_id": new_session, "model": getattr(backend, "model", None)}


class NotesBody(BaseModel):
    doc_id: str
    index: int
    backend: str | None = None
    model: str | None = None
    force: bool = False


def _parse_notes(text: str, n: int) -> tuple[str, list[dict]]:
    """解析逐章笔记：`> 序号 | 主旨`、`序号 | 要点`、缩进 `  序号 | 细节`（挂到上一条要点）。"""
    gist = ""
    points: list[dict] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        indented = raw[:1].isspace()  # 行首缩进 → 细节
        body = raw.strip().lstrip("-•").strip()
        is_gist = body.startswith(">")
        if is_gist:
            body = body.lstrip(">").strip()
        if "|" not in body:
            continue
        num, txt = (p.strip() for p in body.split("|", 1))
        num = num.lstrip("#").strip()
        if not num.isdigit() or not txt:
            continue
        seg = max(0, min(int(num), n - 1))
        if is_gist:
            if not gist:
                gist = txt
        elif indented and points:
            points[-1].setdefault("details", []).append({"seg": seg, "text": txt})
        else:
            points.append({"seg": seg, "text": txt})
    return gist, points


@app.post("/api/notes")
def notes(body: NotesBody):
    """逐章生成「重读即回忆」笔记：主旨 + 要点（带跳转锚点），按章缓存进 chapters。"""
    doc = store.load(body.doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在，请重新导入。")
    chapters = doc.get("chapters") or []
    if not (0 <= body.index < len(chapters)):
        raise HTTPException(status_code=400, detail="章节序号越界，请先生成章节。")

    ch = chapters[body.index]
    if not body.force and ch.get("points"):
        return {"index": body.index, "gist": ch.get("gist", ""),
                "points": ch["points"], "cached": True}

    segments = doc["segments"]
    n = len(segments)
    prompt = prompts.notes_prompt(segments, ch["start"], ch["end"])
    try:
        backend = llm.get_backend(body.backend, body.model)
        answer, _ = backend.ask(prompt, None)  # 无状态：单章聚焦，不污染问答会话
    except llm.LLMError as e:
        raise HTTPException(status_code=502, detail=str(e))

    gist, points = _parse_notes(answer, n)
    for p in points:
        p["ts"] = _fmt_ts(segments[p["seg"]].get("start"))
        for d in p.get("details", []):
            d["ts"] = _fmt_ts(segments[d["seg"]].get("start"))
    store.set_chapter_notes(body.doc_id, body.index, gist, points)  # 原子写：支持并发逐章
    return {"index": body.index, "gist": gist, "points": points,
            "model": getattr(backend, "model", None)}


class GlossBody(BaseModel):
    doc_id: str
    index: int
    backend: str | None = None
    model: str | None = None
    force: bool = False


def _parse_glossary(text: str, n: int) -> list[dict]:
    """解析生词行：`序号 | 英文原词 | 中文意思`。"""
    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip().lstrip("-•").strip()
        if line.count("|") < 2:
            continue
        a, term, zh = (p.strip() for p in line.split("|", 2))
        a = a.lstrip("#").strip()
        if not a.isdigit() or not term or not zh:
            continue
        seg = max(0, min(int(a), n - 1))
        out.append({"seg": seg, "term": term, "zh": zh})
    return out


@app.post("/api/glossary")
def glossary(body: GlossBody):
    """逐章挑出生词 + 中文意思（带片段锚点），按章缓存。"""
    doc = store.load(body.doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在，请重新导入。")
    chapters = doc.get("chapters") or []
    if not (0 <= body.index < len(chapters)):
        raise HTTPException(status_code=400, detail="章节序号越界，请先生成章节。")

    ch = chapters[body.index]
    if not body.force and ch.get("glossary") is not None:
        return {"index": body.index, "glossary": ch["glossary"], "cached": True}

    segments = doc["segments"]
    prompt = prompts.glossary_prompt(segments, ch["start"], ch["end"])
    try:
        backend = llm.get_backend(body.backend, body.model)
        answer, _ = backend.ask(prompt, None)  # 无状态，单章聚焦
    except llm.LLMError as e:
        raise HTTPException(status_code=502, detail=str(e))

    gloss = _parse_glossary(answer, len(segments))
    store.update_chapter(body.doc_id, body.index, glossary=gloss)
    return {"index": body.index, "glossary": gloss,
            "model": getattr(backend, "model", None)}


class DocIdBody(BaseModel):
    doc_id: str


@app.post("/api/session/new")
def session_new(body: DocIdBody):
    """开新对话：清空 session_id（下一问重新喂 transcript、开全新会话，不背旧缓存），
    并在对话流里留一条分隔标记。已显示的历史问答保留。"""
    doc = store.load(body.doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在，请重新导入。")
    store.update(body.doc_id, session_id=None)
    store.append_chat(body.doc_id, {"divider": True})
    return {"ok": True}


@app.post("/api/ask")
def ask(body: AskBody):
    doc = store.load(body.doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在，请重新导入。")

    segments = doc["segments"]
    session_id = doc.get("session_id")

    if session_id:
        prompt = prompts.follow_up(body.selected_text, body.question, body.seg_range, segments)
    else:
        prompt = prompts.first_turn(segments, body.selected_text, body.question, body.seg_range)

    try:
        backend = llm.get_backend(body.backend, body.model)
        answer, new_session = backend.ask(prompt, session_id)
    except llm.LLMError as e:
        raise HTTPException(status_code=502, detail=str(e))

    if new_session and new_session != session_id:
        store.set_session(body.doc_id, new_session)

    return {
        "answer": answer,
        "session_id": new_session,
        "backend": backend.name,
        "model": getattr(backend, "model", None),
    }


class CleanBody(BaseModel):
    doc_id: str
    paragraphs: list[str]
    backend: str | None = None
    model: str | None = None


def _parse_clean(text: str) -> dict[int, str]:
    """按 [[n]] 标记切分，支持清洗文本跨行。"""
    out: dict[int, str] = {}
    parts = re.split(r"\[\[(\d+)\]\]", text)
    it = iter(parts[1:])
    for num, body in zip(it, it):
        out[int(num)] = body.strip()
    return out


@app.post("/api/clean")
def clean(body: CleanBody):
    """AI 清洗口水词：按段落 1:1 清洗（保留原意与英文），按内容哈希缓存。"""
    doc = store.load(body.doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在，请重新导入。")

    cache = dict(doc.get("cleaned_map", {}))
    new: dict[str, str] = {}
    todo = [(i, p) for i, p in enumerate(body.paragraphs) if p not in cache]

    if todo:
        numbered = "\n\n".join(f"[[{k}]] {p}" for k, (_i, p) in enumerate(todo))
        prompt = prompts.CLEAN_INSTRUCTION + "\n\n" + numbered
        try:
            backend = llm.get_backend(body.backend, body.model)
            answer, _ = backend.ask(prompt, None)  # 无状态，不污染问答会话
        except llm.LLMError as e:
            raise HTTPException(status_code=502, detail=str(e))
        parsed = _parse_clean(answer)
        for k, (_i, p) in enumerate(todo):
            cache[p] = new[p] = (parsed.get(k) or "").strip() or p
    store.merge_map(body.doc_id, "cleaned_map", new)  # 原子合并，避免并发覆盖

    result = [cache.get(p, p) for p in body.paragraphs]
    return {"cleaned": result}


@app.post("/api/translate")
def translate(body: CleanBody):
    """段落级中译：每段独立翻译，按内容哈希缓存（前端可并行分批请求）。"""
    doc = store.load(body.doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在，请重新导入。")

    cache = dict(doc.get("translated_map", {}))
    new: dict[str, str] = {}
    todo = [(i, p) for i, p in enumerate(body.paragraphs) if p not in cache]

    if todo:
        numbered = "\n\n".join(f"[[{k}]] {p}" for k, (_i, p) in enumerate(todo))
        prompt = prompts.TRANSLATE_INSTRUCTION + "\n\n" + numbered
        try:
            backend = llm.get_backend(body.backend, body.model)
            answer, _ = backend.ask(prompt, None)  # 无状态，不污染问答会话
        except llm.LLMError as e:
            raise HTTPException(status_code=502, detail=str(e))
        parsed = _parse_clean(answer)  # 同样的 [[n]] 分段格式
        for k, (_i, p) in enumerate(todo):
            cache[p] = new[p] = (parsed.get(k) or "").strip() or p
    store.merge_map(body.doc_id, "translated_map", new)  # 原子合并，避免并发覆盖

    result = [cache.get(p, p) for p in body.paragraphs]
    return {"translated": result}


@app.post("/api/ask/stream")
def ask_stream(body: AskBody):
    """流式问答：NDJSON 逐行返回 {type:delta,text} / {type:done,session_id} / {type:error}。"""
    doc = store.load(body.doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在，请重新导入。")

    segments = doc["segments"]
    session_id = doc.get("session_id")
    if session_id:
        prompt = prompts.follow_up(body.selected_text, body.question, body.seg_range, segments)
    else:
        prompt = prompts.first_turn(segments, body.selected_text, body.question, body.seg_range)

    try:
        backend = llm.get_backend(body.backend, body.model)
    except llm.LLMError as e:
        raise HTTPException(status_code=502, detail=str(e))

    def gen():
        acc = []
        try:
            for kind, val in backend.ask_stream(prompt, session_id):
                if kind == "text":
                    acc.append(val)
                    yield json.dumps({"type": "delta", "text": val}, ensure_ascii=False) + "\n"
                elif kind == "session":
                    if val and val != session_id:
                        store.set_session(body.doc_id, val)
                    answer = "".join(acc).strip()
                    if answer:  # 落盘，刷新后可恢复
                        store.append_chat(body.doc_id, {
                            "selected": body.selected_text,
                            "question": body.question,
                            "answer": answer,
                        })
                    yield json.dumps({"type": "done", "session_id": val}) + "\n"
                elif kind == "error":
                    yield json.dumps({"type": "error", "error": val}, ensure_ascii=False) + "\n"
        except llm.LLMError as e:
            yield json.dumps({"type": "error", "error": str(e)}, ensure_ascii=False) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


# --------------------------------------------------------------------------- #
# 静态前端
# --------------------------------------------------------------------------- #

@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


app.mount("/", StaticFiles(directory=WEB_DIR), name="web")
