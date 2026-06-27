"""三种来源 → 统一的 transcript 片段列表。

Segment = {"start": float秒, "end": float秒, "text": str}
（.txt 这类无时间戳的来源，start/end 用 None 占位。）
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

Segment = dict[str, Any]


class IngestError(Exception):
    """对用户可见的、可预期的 ingest 失败（坏链接、无字幕、不支持的格式等）。"""


# --------------------------------------------------------------------------- #
# YouTube / 视频链接
# --------------------------------------------------------------------------- #

_YT_ID_PATTERNS = [
    r"(?:v=|/v/|youtu\.be/|/embed/|/shorts/)([0-9A-Za-z_-]{11})",
    r"^([0-9A-Za-z_-]{11})$",  # 直接传 11 位 id
]


def extract_video_id(url: str) -> str | None:
    url = url.strip()
    for pat in _YT_ID_PATTERNS:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def from_youtube(url: str) -> tuple[list[Segment], str]:
    """抓 YouTube 字幕。优先英文，没有就退而取任意一种可用语言。"""
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api._errors import TranscriptsDisabled

    video_id = extract_video_id(url)
    if not video_id:
        raise IngestError("无法从链接里解析出 YouTube 视频 id，请检查链接。")

    prefer = ["en", "en-US", "en-GB"]
    api = YouTubeTranscriptApi()
    try:
        tlist = api.list(video_id)
    except TranscriptsDisabled as e:
        raise IngestError("该视频关闭了字幕，无法抓取。请改为上传媒体或字幕文件。") from e
    except Exception as e:  # noqa: BLE001
        raise IngestError(f"抓取字幕失败：{e}") from e

    # 优先人工字幕（更准）；没有再退自动生成（ASR 会听错词，如 fun→up on）；
    # 最后退任意可用语言。
    transcript = None
    for finder in (tlist.find_manually_created_transcript, tlist.find_generated_transcript):
        try:
            transcript = finder(prefer)
            break
        except Exception:  # noqa: BLE001  # NoTranscriptFound 等 → 试下一个来源
            continue
    if transcript is None:
        transcript = next(iter(tlist), None)
    if transcript is None:
        raise IngestError("该视频没有可用字幕。请改为上传媒体或字幕文件。")

    try:
        fetched = transcript.fetch()
    except Exception as e:  # noqa: BLE001
        raise IngestError(f"抓取字幕失败：{e}") from e

    segments: list[Segment] = []
    for snip in fetched:
        start = float(getattr(snip, "start", 0.0))
        dur = float(getattr(snip, "duration", 0.0))
        text = str(getattr(snip, "text", "")).replace("\n", " ").strip()
        if text:
            segments.append({"start": start, "end": start + dur, "text": text})

    if not segments:
        raise IngestError("抓到的字幕是空的。")
    return segments, f"YouTube {video_id}"


# --------------------------------------------------------------------------- #
# 字幕文件 .srt / .vtt / .txt
# --------------------------------------------------------------------------- #


def from_srt(content: str) -> list[Segment]:
    import srt

    return [
        {"start": s.start.total_seconds(), "end": s.end.total_seconds(),
         "text": s.content.replace("\n", " ").strip()}
        for s in srt.parse(content)
        if s.content.strip()
    ]


def from_vtt(path: Path) -> list[Segment]:
    import webvtt

    segs: list[Segment] = []
    for cap in webvtt.read(str(path)):
        text = cap.text.replace("\n", " ").strip()
        if text:
            segs.append({"start": cap.start_in_seconds, "end": cap.end_in_seconds, "text": text})
    return segs


def from_plaintext(content: str) -> list[Segment]:
    """无时间戳：按空行/换行切成段落。"""
    chunks = re.split(r"\n\s*\n", content) if "\n\n" in content else content.splitlines()
    return [{"start": None, "end": None, "text": c.strip()} for c in chunks if c.strip()]


# --------------------------------------------------------------------------- #
# 本地音视频 → faster-whisper（懒加载，仅此路径需要）
# --------------------------------------------------------------------------- #

_MEDIA_EXTS = {".mp3", ".mp4", ".m4a", ".wav", ".aac", ".flac", ".ogg", ".webm", ".mov", ".mkv"}


def from_media(path: Path, model_size: str = "base") -> list[Segment]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise IngestError(
            "转写音视频需要 faster-whisper，请先安装：`uv sync --extra whisper`"
        ) from e

    # int8 在 CPU/Apple Silicon 上够快且省内存；首次会自动下载模型。
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    whisper_segs, _info = model.transcribe(str(path), vad_filter=True)
    segments = [
        {"start": float(s.start), "end": float(s.end), "text": s.text.strip()}
        for s in whisper_segs
        if s.text.strip()
    ]
    if not segments:
        raise IngestError("转写结果为空（可能是无语音内容）。")
    return segments


# --------------------------------------------------------------------------- #
# 文件入口：按扩展名分发
# --------------------------------------------------------------------------- #


def from_file(filename: str, raw: bytes, tmp_path: Path) -> tuple[list[Segment], str]:
    """tmp_path 是已落盘的临时文件路径（webvtt / whisper 需要真实路径）。"""
    ext = Path(filename).suffix.lower()
    if ext == ".srt":
        return from_srt(raw.decode("utf-8", errors="replace")), filename
    if ext == ".vtt":
        return from_vtt(tmp_path), filename
    if ext in {".txt", ".md"}:
        return from_plaintext(raw.decode("utf-8", errors="replace")), filename
    if ext in _MEDIA_EXTS:
        return from_media(tmp_path), filename
    raise IngestError(f"不支持的文件类型：{ext}。支持 .srt/.vtt/.txt 及常见音视频格式。")
