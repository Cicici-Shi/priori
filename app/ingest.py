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


# 句末标点 + 可选引号/括号 + 必须跟空白：要求空白能避开 "3.5" / "U.S.A" / 网址里的点被误切。
_SENT_BOUNDARY = re.compile(r"[.!?。！？…]+[\"'”’)\]]*\s+")


def resegment_by_sentence(segments: list[Segment]) -> list[Segment]:
    """把按时间切的字幕块重组成按句子切的块。

    句号常落在 YouTube 时间块中间（"make. They were..."），而一句话又常跨多个块，
    所以先把全文拼成连续流、给每个字符按块内位置线性插值一个时间，再在句末标点处切，
    每句的 start/end 取首尾字符的插值时间。无标点的自动字幕（切不出句子）原样返回。
    """
    chars: list[str] = []
    times: list[float] = []  # 与 chars 等长：第 k 个字符的时间
    for s in segments:
        text = s.get("text") or ""
        st, en = s.get("start"), s.get("end")
        if st is None or en is None:
            return segments  # 无时间戳来源，不动
        n = len(text)
        for k, ch in enumerate(text):
            chars.append(ch)
            times.append(st + (k / n if n else 0.0) * (en - st))
        chars.append(" ")  # 块间补空格，时间记块末
        times.append(en)
    full = "".join(chars)

    out: list[Segment] = []
    pos = 0
    for m in _SENT_BOUNDARY.finditer(full):
        end = m.end()
        txt = full[pos:end].strip()
        if txt:
            out.append({"start": times[pos], "end": times[end - 1], "text": txt})
        pos = end
    tail = full[pos:].strip()
    if tail and pos < len(times):
        out.append({"start": times[pos], "end": times[-1], "text": tail})

    return out if len(out) >= 2 else segments  # 切不出句子 → 保留原始块，别揉成一个巨块


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
    segments = resegment_by_sentence(segments)  # 时间块 → 句子块，句末对齐
    return segments, f"YouTube {video_id}"


# --------------------------------------------------------------------------- #
# 网页正文 → 带标题层级的块（trafilatura 抽正文）
# --------------------------------------------------------------------------- #


def from_web(url: str) -> tuple[list[Segment], str]:
    """抓网页正文，按 markdown 标题/段落切成块。

    每个块 = 一个 Segment，额外带 `level`：0=正文段落，1-6=标题层级。
    无时间戳（start/end 为 None）。
    """
    try:
        import trafilatura
    except ImportError as e:  # noqa: BLE001
        raise IngestError("网页抽取需要 trafilatura，请先 `uv sync`。") from e

    html = trafilatura.fetch_url(url)
    if not html:
        raise IngestError("抓不到该网页（可能需要登录、或被反爬）。请改贴别的链接。")
    md = trafilatura.extract(
        html, output_format="markdown",
        include_links=False, include_images=False, include_tables=True,
    )
    if not md or not md.strip():
        raise IngestError("没能从这个网页提取出正文。")

    segments: list[Segment] = []
    for line in md.splitlines():
        s = line.strip()
        if not s:
            continue
        level = 0
        if s.startswith("#"):
            h = len(s) - len(s.lstrip("#"))
            level, s = min(h, 6), s[h:].strip()
        if s:
            segments.append({"start": None, "end": None, "text": s, "level": level})
    if not segments:
        raise IngestError("正文为空。")

    title = next((seg["text"] for seg in segments if seg["level"] == 1), None) or "网页"
    return segments, title


def web_chapters(segments: list[Segment]) -> list[dict]:
    """按标题把网页正文切成"章节"（= 目录项）：每个标题起一节，首章并入开头内容。"""
    n = len(segments)
    heads = [i for i, s in enumerate(segments) if s.get("level", 0) > 0]
    if not heads:
        return [{"title": "正文", "start": 0, "end": n - 1}]
    chs = []
    for k, i in enumerate(heads):
        end = (heads[k + 1] - 1) if k + 1 < len(heads) else (n - 1)
        chs.append({"title": segments[i]["text"], "start": i, "end": end, "level": segments[i].get("level", 1)})
    chs[0]["start"] = 0  # 开头（首标题之前）的内容并入首章
    return chs


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
