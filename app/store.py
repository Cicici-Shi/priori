"""极简文档存储：每个 doc 一个 JSON 文件，存 transcript、来源信息和 LLM 会话 id。

不引数据库——这是个本地小工具，JSON 文件足够，且方便人工查看/调试。
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)

# 文件级写锁：FastAPI 默认多线程跑同步路由，避免并发写串行化掉。
_lock = threading.Lock()


def _path(doc_id: str) -> Path:
    return DATA_DIR / f"{doc_id}.json"


def new_doc_id() -> str:
    return uuid.uuid4().hex[:12]


def create(segments: list[dict[str, Any]], source: str, title: str) -> str:
    """落盘一篇新 transcript，返回 doc_id。session_id 初始为空，首轮问答后写入。"""
    doc_id, _ = create_keyed(new_doc_id(), segments, source, title)
    return doc_id


def create_keyed(key: str, segments: list[dict[str, Any]], source: str, title: str) -> tuple[str, bool]:
    """按内容键去重：doc_id = hash(key)。

    若同 key 的 doc 已存在 → 直接复用（保留其 chapters/turns/session 缓存），返回 (doc_id, True)。
    否则新建，返回 (doc_id, False)。
    """
    doc_id = key_to_id(key)
    if _path(doc_id).exists():
        return doc_id, True
    save(doc_id, {
        "doc_id": doc_id,
        "title": title,
        "source": source,
        "segments": segments,
        "session_id": None,
    })
    return doc_id, False


def key_to_id(key: str) -> str:
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def doc_id_for_key(key: str) -> str | None:
    """同 key 的 doc 若已存在返回其 id，否则 None（用于导入前去重、跳过重复抓取）。"""
    doc_id = key_to_id(key)
    return doc_id if _path(doc_id).exists() else None


def load(doc_id: str) -> dict[str, Any] | None:
    p = _path(doc_id)
    if not p.exists():
        return None
    with p.open(encoding="utf-8") as f:
        return json.load(f)


def _write(doc_id: str, doc: dict[str, Any]) -> None:
    """无锁写盘（原子 replace）。调用方需自行持有 _lock。"""
    tmp = _path(doc_id).with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    tmp.replace(_path(doc_id))


def save(doc_id: str, doc: dict[str, Any]) -> None:
    with _lock:
        _write(doc_id, doc)


def merge_map(doc_id: str, field: str, entries: dict[str, Any]) -> None:
    """原子地把 entries 并入 doc[field]（dict 型，如 translated_map / cleaned_map），
    只合并新增项、不整篇覆盖——避免并发请求互相覆盖丢更新。"""
    if not entries:
        return
    with _lock:
        doc = load(doc_id)
        if doc is None:
            return
        m = doc.get(field) or {}
        m.update(entries)
        doc[field] = m
        _write(doc_id, doc)


def append_chat(doc_id: str, entry: dict[str, Any]) -> None:
    """原子追加一条对话记录（问答 / 新对话分隔），供刷新后恢复显示。"""
    with _lock:
        doc = load(doc_id)
        if doc is None:
            return
        chat = doc.get("chat") or []
        chat.append(entry)
        doc["chat"] = chat
        _write(doc_id, doc)


def update_chapter(doc_id: str, index: int, **fields: Any) -> None:
    """原子更新某一章的任意字段（如 glossary）：锁内 load→改→写，支持并发逐章。"""
    with _lock:
        doc = load(doc_id)
        if doc is None:
            return
        chapters = doc.get("chapters") or []
        if 0 <= index < len(chapters):
            chapters[index].update(fields)
            _write(doc_id, doc)


def set_chapter_notes(doc_id: str, index: int, gist: str, points: list[Any]) -> None:
    """原子写入某一章的笔记：锁内 load→改→写，避免并发 /api/notes 互相覆盖整篇 chapters。"""
    with _lock:
        doc = load(doc_id)
        if doc is None:
            return
        chapters = doc.get("chapters") or []
        if 0 <= index < len(chapters):
            chapters[index]["gist"] = gist
            chapters[index]["points"] = points
            _write(doc_id, doc)


def set_session(doc_id: str, session_id: str) -> None:
    update(doc_id, session_id=session_id)


def update(doc_id: str, **fields) -> None:
    """读出 doc，合并字段后写回（用于缓存 chapters / turns / session 等）。"""
    doc = load(doc_id)
    if doc is None:
        return
    doc.update(fields)
    save(doc_id, doc)
