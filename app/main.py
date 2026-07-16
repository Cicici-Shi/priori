"""FastAPI 入口：/ingest、/ask，以及静态前端。"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import ingest, llm, prompts, store

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="Priori")


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
        "source": doc.get("source"),  # 原始链接/文件名，前端切文档时回填 URL 输入框
        "kind": doc.get("kind", "video"),  # video | web
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
    # 视频 → 抓字幕；其他链接 → 抓网页正文。按 key 去重复用缓存。
    vid = ingest.extract_video_id(body.url)
    key = vid or body.url.strip()
    existing_id = store.doc_id_for_key(key)
    if existing_id and (doc := store.load(existing_id)):
        return _doc_response(existing_id, doc)

    try:
        if vid:
            segments, title = ingest.from_youtube(body.url)
            chapters = None
        elif ingest.is_x_url(body.url):
            # X 长文正文只在登录态里，走浏览器抓（见 ingest.from_x_article）
            segments, title = ingest.from_x_article(body.url)
            chapters = ingest.web_chapters(segments)
        else:
            segments, title = ingest.from_web(body.url)
            chapters = ingest.web_chapters(segments)  # 网页：按标题预切章节（= 目录）
    except ingest.IngestError as e:
        raise HTTPException(status_code=400, detail=str(e))

    doc_id, _ = store.create_keyed(key, segments, source=body.url, title=title)
    store.update(doc_id, kind="video" if vid else "web", **({"chapters": chapters} if chapters else {}))
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


@app.get("/api/docs")
def docs_list():
    """历史记录：最近打开/活跃的文档元信息列表（不含正文）。"""
    return {"docs": store.list_docs()}


@app.get("/api/doc/{doc_id}")
def doc_get(doc_id: str):
    """按 id 直接加载一篇历史文档（供历史记录点击回看，无需重抓）。"""
    doc = store.load(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在或已删除。")
    store.touch(doc_id)  # 回看即置顶
    return _doc_response(doc_id, doc)


class TitleBody(BaseModel):
    title: str = ""


@app.post("/api/doc/{doc_id}/title")
def doc_set_title(doc_id: str, body: TitleBody):
    """写回文档标题。YouTube 真实片名要等播放器就绪才拿得到（入库时只有 id），
    前端拿到后调这里存一下，历史列表就显示真名而非 "YouTube <id>"。"""
    if store.load(doc_id) is None:
        raise HTTPException(status_code=404, detail="文档不存在。")
    t = body.title.strip()
    if t:
        store.update(doc_id, title=t)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# /ask — 选中片段 + 问题 → 答案（多轮靠 session 续接）
# --------------------------------------------------------------------------- #

class AskBody(BaseModel):
    doc_id: str
    question: str
    selected_text: str = ""
    seg_range: list[int] | None = None
    image: str | None = None  # 针对某张图提问时：图片 URL，后端按需拉字节当真图喂模型
    backend: str | None = None  # 可覆盖默认引擎
    model: str | None = None  # 可覆盖默认模型（仅 claude 引擎）


def _ask_images(image: str | None) -> list[tuple[bytes, str]] | None:
    """就某张图提问时，按需拉那张图的字节，作为真正的 image block 喂给模型（不落盘）。

    拉不到（坏链接 / 非 http，如旧文档存的本地文件名）就返回 None，退化成纯文本问答。
    """
    if not image:
        return None
    try:
        data, media_type = ingest.fetch_image_bytes(image)
    except ingest.IngestError:
        return None
    return [(data, media_type)]


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
    images = _ask_images(body.image)
    if images:
        prompt = prompts.with_image(prompt)

    try:
        backend = llm.get_backend(body.backend, body.model)
        answer, new_session = backend.ask(prompt, session_id, images=images)
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


def _batch_translate(backend, texts: list[str]) -> list[str]:
    """整批翻译，返回与 texts 严格 1:1 对齐的译文。

    `[[n]]` 编号回填法的致命点：模型把相邻短句合并成一条、再顺移后面的编号，
    解析器信了模型编号就会整体错位一格。故这里校验编号 0..n-1 是否齐全且非空，
    一旦不齐就判定对齐不可信 → 逐句单独重翻（一句一请求，无编号可错）。"""
    numbered = "\n\n".join(f"[[{k}]] {p}" for k, p in enumerate(texts))
    answer, _ = backend.ask(prompts.TRANSLATE_INSTRUCTION + "\n\n" + numbered, None)
    parsed = _parse_clean(answer)
    if all((parsed.get(k) or "").strip() for k in range(len(texts))):
        return [parsed[k].strip() for k in range(len(texts))]
    out = []
    for p in texts:
        ans, _ = backend.ask(prompts.TRANSLATE_INSTRUCTION + "\n\n[[0]] " + p, None)
        one = _parse_clean(ans)
        out.append((one.get(0) or ans or "").strip() or p)
    return out


@app.post("/api/translate")
def translate(body: CleanBody):
    """逐句中译：每句独立翻译，按句子文本缓存（前端并行分批请求）。"""
    doc = store.load(body.doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在，请重新导入。")

    cache = dict(doc.get("translated_map", {}))
    new: dict[str, str] = {}
    todo = [p for p in body.paragraphs if p not in cache]

    if todo:
        try:
            backend = llm.get_backend(body.backend, body.model)
            for p, zh in zip(todo, _batch_translate(backend, todo)):
                cache[p] = new[p] = zh
        except llm.LLMError as e:
            raise HTTPException(status_code=502, detail=str(e))
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
    images = _ask_images(body.image)
    if images:
        prompt = prompts.with_image(prompt)

    try:
        backend = llm.get_backend(body.backend, body.model)
    except llm.LLMError as e:
        raise HTTPException(status_code=502, detail=str(e))

    def gen():
        acc = []
        try:
            for kind, val in backend.ask_stream(prompt, session_id, images):
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


@app.get("/img")
def img_proxy(u: str):
    """按需图片代理：拿原始 URL 后端拉字节直出（不落盘）。前端所有 img_url 都走这里显示。"""
    try:
        data, media_type = ingest.fetch_image_bytes(u)
    except ingest.IngestError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return Response(content=data, media_type=media_type,
                    headers={"Cache-Control": "public, max-age=86400"})


# 兼容旧文档里已下到 data/img 的图（新文档不再落盘，一律走 /img 代理）。须在 "/" 之前挂。
_MEDIA_DIR = store.DATA_DIR / "img"
_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=_MEDIA_DIR), name="media")

app.mount("/", StaticFiles(directory=WEB_DIR), name="web")
