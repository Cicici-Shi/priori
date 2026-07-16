"""三种来源 → 统一的 transcript 片段列表。

Segment = {"start": float秒, "end": float秒, "text": str}
（.txt 这类无时间戳的来源，start/end 用 None 占位。）
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

Segment = dict[str, Any]

# 图片一律不落盘：Segment 里存**原始 URL**（img_url），前端走 /img?u= 代理拉流显示，
# 提问看图时后端按需拉字节、当真正的 image block 喂模型（见 fetch_image_bytes）。


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
    import requests
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api._errors import TranscriptsDisabled

    video_id = extract_video_id(url)
    if not video_id:
        raise IngestError("无法从链接里解析出 YouTube 视频 id，请检查链接。")

    prefer = ["en", "en-US", "en-GB"]
    # trust_env=False：直连，不读环境变量/macOS 系统代理。否则本机开着抓包代理
    # （如 whistle 8899）时，requests 会走代理拿到被改写的 HTTPS 证书 → 证书验证失败。
    session = requests.Session()
    session.trust_env = False
    api = YouTubeTranscriptApi(http_client=session)
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
# X（Twitter）长文 → 走用户已登录的浏览器抓渲染后的正文
# --------------------------------------------------------------------------- #
#
# 为什么单独一条路：X 长文（Article）正文是登录后 JS 才渲染的，静态 HTML 只给一段
# 截断预览（`…it's what I give…`）。所有无登录抓法（syndication / fx·vxtwitter /
# guest-token GraphQL / 直接抓 /i/article 页）都只拿得到 preview_text，拿不到全文。
# 唯一稳定来源是用户自己已登录 X 的浏览器——用 Kimi WebBridge daemon 驱动它打开文章、
# 抽渲染后的 `twitterArticleRichTextView` DOM。标题/封面则用 syndication 的稳定 JSON
# （DOM 里这俩不好可靠定位）。

_WEBBRIDGE = "http://127.0.0.1:10086/command"
_X_HOST_RE = re.compile(r"^https?://(?:www\.|mobile\.)?(?:twitter|x)\.com/", re.I)
_X_STATUS_RE = re.compile(r"(?:twitter|x)\.com/[^/]+/status/(\d+)", re.I)

# 在页面里跑（**同步**——webbridge 的 evaluate 遇 async/await 会挂死，等待改由 Python 侧轮询）：
# querySelectorAll 按文档顺序回块，原样保留「文字块 ↔ 图片块」的先后——图天然就在正文正确位置，
# 不需要任何"让模型猜图插在哪"。X 把每张插图渲染成 <section data-block>，只有 tab 前台可见时才
# 懒加载出 <img src="pbs.twimg.com/...">；imgTotal/imgLoaded 供 Python 判断图是否都加载好了
# （隐藏 tab 下 X 一张都不加载，故 from_x_article 必须先用 CDP Page.bringToFront 把 tab 切前台）。
_X_EXTRACT_JS = r"""
(()=>{
  const root=document.querySelector('[data-testid="twitterArticleRichTextView"]');
  if(!root) return JSON.stringify({error:"no_article"});
  const _secs=root.querySelectorAll('section[data-block="true"]');
  let imgTotal=_secs.length, imgLoaded=0;
  _secs.forEach(s=>{ const i=s.querySelector('img'); if(i&&i.src) imgLoaded++; });
  let title=null, node=root;               // 标题在正文块的前置兄弟里（单行散文、非互动数字条）
  outer: for(let d=0; d<6 && node; d++){
    let sib=node.previousElementSibling;
    while(sib){
      const t=(sib.innerText||'').trim();
      if(t && t.indexOf('\n')===-1 && t.length>=5 && t.length<=200 &&
         !/^[\d,.\sKM万]+$/.test(t)){ title=t; break outer; }
      sib=sib.previousElementSibling;
    }
    node=node.parentElement;
  }
  const out=[];
  root.querySelectorAll('[data-block="true"]').forEach(el=>{
    const img=el.querySelector('img');
    if(img){ out.push({t:"img",src:img.getAttribute('src'),alt:img.getAttribute('alt')||""}); return; }
    const txt=(el.innerText||"").replace(/ /g,' ').trim();
    if(!txt) return;
    const m=/^H([1-6])$/.exec(el.tagName);
    if(m){ out.push({t:"h",lvl:+m[1],txt}); return; }
    if(el.tagName==="LI"){ out.push({t:"li",txt}); return; }
    out.push({t:"p",txt});
  });
  return JSON.stringify({title,blocks:out,imgTotal,imgLoaded});
})()
"""


def is_x_url(url: str) -> bool:
    return bool(_X_HOST_RE.match(url.strip()))


def _x_status_id(url: str) -> str | None:
    m = _X_STATUS_RE.search(url)
    return m.group(1) if m else None


def _x_article_meta(status_id: str) -> tuple[str | None, str | None]:
    """从 syndication 拿长文的标题 + 封面图 url（best-effort，拿不到就 (None, None)）。"""
    import json
    import urllib.request

    api = f"https://cdn.syndication.twimg.com/tweet-result?id={status_id}&token=a&lang=en"
    try:
        req = urllib.request.Request(api, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.load(r)
    except Exception:  # noqa: BLE001  syndication 挂了不致命，标题回退到 DOM
        return None, None
    art = d.get("article") or {}
    cover = ((art.get("cover_media") or {}).get("media_info") or {}).get("original_img_url")
    return art.get("title"), cover


def _webbridge(action: str, args: dict | None = None, timeout: int = 60) -> dict:
    """调本地 Kimi WebBridge daemon。连不上/报错 → 抛可读的 IngestError。"""
    import requests

    session = requests.Session()
    session.trust_env = False  # localhost 别走系统代理（whistle 等会劫持）
    try:
        r = session.post(_WEBBRIDGE, json={"action": action, "args": args or {}, "session": "priori"},
                         timeout=timeout)
    except requests.exceptions.ConnectionError as e:
        raise IngestError(
            "连不上 Kimi WebBridge（127.0.0.1:10086）。X 长文要通过你已登录的浏览器抓，"
            "请先启动：`~/.kimi-webbridge/bin/kimi-webbridge start`，并确认浏览器已登录 X。"
        ) from e
    except Exception as e:  # noqa: BLE001  超时等
        raise IngestError(f"调用 Kimi WebBridge 失败：{e}") from e
    try:
        data = r.json()
    except Exception as e:  # noqa: BLE001
        raise IngestError("Kimi WebBridge 返回了非预期内容。") from e
    if not data.get("ok"):
        raise IngestError(f"Kimi WebBridge 报错：{data.get('error') or data}")
    return data.get("data") or {}


def from_x_article(url: str) -> tuple[list[Segment], str]:
    """抓 X 长文：驱动浏览器把文章渲染出来（含正文插图）+ syndication 补标题/封面 → 分段。

    关键三步：navigate 打开文章 → **CDP Page.bringToFront 把 tab 切到前台可见**（否则 X 懒加载不触发、
    正文插图一张都不渲染）→ 轮询等所有 <section> 里的 <img> 加载出真实 pbs URL。图在 DOM 里天然
    按正文顺序排列，位置零猜测。产出的 Segment 形状与 from_web 一致（img_url 存原始 URL，不落盘）。
    """
    import json
    import time

    _webbridge("navigate", {"url": url})
    # 切前台：X 只在 tab 真·可见时才加载文章图（visibilityState=visible）。CDP Page.bringToFront
    # 由我们主动把这个后台 tab 激活到前台——不依赖用户此刻正在看它。这几秒会占用屏幕。
    _webbridge("cdp", {"method": "Page.bringToFront", "params": {}})

    payload: dict = {}
    for _ in range(24):  # 最多约 12s：等正文渲染 + 所有插图加载齐
        res = _webbridge("evaluate", {"code": _X_EXTRACT_JS})
        try:
            payload = json.loads(res.get("value") or "")
        except Exception:  # noqa: BLE001  页面还没就绪，稍后重试
            payload = {}
        if payload.get("blocks") and payload.get("imgLoaded", 0) >= payload.get("imgTotal", 0):
            break
        time.sleep(0.5)

    if not payload.get("blocks"):
        raise IngestError(
            "没能在这个 X 链接里找到长文正文。请确认：① 浏览器已登录 X；"
            "② 这是一篇 X 长文（Article），不是普通推文。"
        )

    title, cover = (None, None)
    if (sid := _x_status_id(url)):
        title, cover = _x_article_meta(sid)
    title = title or payload.get("title") or "X 文章"

    segments: list[Segment] = [{"start": None, "end": None, "text": title, "level": 1}]
    if cover:  # 封面来自 syndication 的公开 URL
        segments.append({"start": None, "end": None, "level": 0, "type": "image",
                         "img_url": cover, "alt": "", "text": "〔图片：封面〕"})

    li_buf: list[str] = []

    def flush_li() -> None:
        nonlocal li_buf
        if li_buf:  # 连续列表项攒成一个 markdown 列表块（和 from_web 的攒块一致）
            segments.append({"start": None, "end": None, "level": 0,
                             "text": "\n".join("- " + t for t in li_buf)})
            li_buf = []

    for b in payload["blocks"]:
        t = b.get("t")
        if t == "li":
            li_buf.append(b.get("txt", ""))
            continue
        flush_li()
        if t == "h":
            lvl = min(max(int(b.get("lvl", 2)), 1), 6)
            segments.append({"start": None, "end": None, "text": b.get("txt", ""), "level": lvl})
        elif t == "img":
            src = b.get("src")
            if src:  # 就是 pbs.twimg 的公开 URL，直接存；显示走 /img 代理、看图走 fetch_image_bytes
                alt = (b.get("alt") or "").strip()
                segments.append({"start": None, "end": None, "level": 0, "type": "image",
                                 "img_url": src, "alt": alt,
                                 "text": f"〔图片{('：' + alt) if alt else ''}〕"})
        else:  # p
            segments.append({"start": None, "end": None, "text": b.get("txt", ""), "level": 0})
    flush_li()

    return segments, title


# --------------------------------------------------------------------------- #
# 网页正文 → 带标题层级的块（trafilatura 抽正文）
# --------------------------------------------------------------------------- #


# markdown 图片：`![alt](url)`。单独成行的当图片块，行内的回写成 /img 代理引用。
_IMG_LINE = re.compile(r"^!\[(?P<alt>[^\]]*)\]\((?P<url>[^)]+)\)$")
_IMG_ANY = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<url>[^)]+)\)")
# 拿不到 content-type 时按扩展名兜底一个 media_type（喂模型的 image block 需要它）。
_EXT_CT = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
}
_IMG_MAX = 20 * 1024 * 1024  # 单图上限 20MB


@lru_cache(maxsize=128)
def fetch_image_bytes(url: str) -> tuple[bytes, str]:
    """按需拉一张图，返回 (字节, media_type)。**不落盘**，进程内 LRU 缓存避免显示+提问重复拉。

    显示（/img 代理）和看图提问（喂模型 image block）都走这里。失败抛 IngestError。
    """
    import urllib.request

    if not re.match(r"^https?://", url):
        raise IngestError("不支持的图片地址。")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Priori)"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            ct = (resp.headers.get_content_type() or "").lower()
            data = resp.read(_IMG_MAX)
    except Exception as e:  # noqa: BLE001  网络/超时/坏链接
        raise IngestError(f"拉取图片失败：{e}") from e
    if not data:
        raise IngestError("拉回来的图片为空。")
    if not ct.startswith("image/"):  # pbs 正常给 image/jpeg；异常时按扩展名兜底
        ct = _EXT_CT.get(Path(url.split("?", 1)[0]).suffix.lower(), "image/jpeg")
    return data, ct


def from_web(url: str) -> tuple[list[Segment], str]:
    """抓网页正文，按 markdown **块**切成 Segment（保留原排版）。

    每块 = 一个 Segment：
    - 标题：`level`=1-6，`text` 为标题文字。
    - 图片：`type`="image"、`img_url`=原始 URL、`alt`=替代文字；`text` 是给 LLM 的占位说明。
    - 其余（段落 / 列表 / 表格 / 代码块）：`level`=0，`text` 为该块的原始 markdown，前端整块渲染。
    无时间戳（start/end 为 None）。图片一律不落盘：行内图回写成 /img 代理引用、单行图存 img_url。
    """
    from urllib.parse import quote

    try:
        import trafilatura
    except ImportError as e:  # noqa: BLE001
        raise IngestError("网页抽取需要 trafilatura，请先 `uv sync`。") from e

    html = trafilatura.fetch_url(url)
    if not html:
        raise IngestError("抓不到该网页（可能需要登录、或被反爬）。请改贴别的链接。")
    md = trafilatura.extract(
        html, output_format="markdown",
        include_links=True, include_images=True, include_tables=True,
    )
    if not md or not md.strip():
        raise IngestError("没能从这个网页提取出正文。")

    def _inline_local(text: str) -> str:
        """段落里的行内图：URL 回写成 /img 代理引用（不落盘，显示时后端按需拉流）。"""
        def repl(m: re.Match) -> str:
            return f"![{m.group('alt')}](/img?u={quote(m.group('url'), safe='')})"
        return _IMG_ANY.sub(repl, text)

    segments: list[Segment] = []
    buf: list[str] = []           # 段落 / 列表 / 表格：连续非空行攒成一块
    fence: list[str] | None = None  # ``` 代码块：整块攒起来

    def flush() -> None:
        nonlocal buf
        if buf:
            text = _inline_local("\n".join(buf).strip())
            if text.strip():
                segments.append({"start": None, "end": None, "text": text, "level": 0})
        buf = []

    for raw in md.splitlines():
        s = raw.strip()
        if fence is not None:               # 代码块进行中
            fence.append(raw)
            if s.startswith("```"):
                segments.append({"start": None, "end": None,
                                 "text": "\n".join(fence), "level": 0})
                fence = None
            continue
        if s.startswith("```"):             # 代码块开始
            flush()
            fence = [raw]
            continue
        if not s:                           # 空行 = 块分隔
            flush()
            continue
        if s.startswith("#"):               # 标题各自成块
            flush()
            h = len(s) - len(s.lstrip("#"))
            title = s[h:].strip()
            if title:
                segments.append({"start": None, "end": None,
                                 "text": title, "level": min(h, 6)})
            continue
        mi = _IMG_LINE.match(s)             # 整行就是一张图 → 图片块
        if mi:
            flush()
            alt = mi.group("alt").strip()
            segments.append({
                "start": None, "end": None, "level": 0, "type": "image",
                "img_url": mi.group("url"), "alt": alt,
                "text": f"〔图片{('：' + alt) if alt else ''}〕",  # 喂 LLM 的文本占位
            })
            continue
        buf.append(raw)
    if fence:                                # 文末未闭合的代码块
        segments.append({"start": None, "end": None, "text": "\n".join(fence), "level": 0})
    flush()

    if not segments:
        raise IngestError("正文为空。")

    title = next((seg["text"] for seg in segments if seg.get("level") == 1), None) or "网页"
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
