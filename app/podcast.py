"""播客：链接 → 音频 → 千问「音视频速读」转写 → 带说话人的 segments。

为什么走千问而不是本地 Whisper：中文播客上千问有**声学说话人分离**、标点自然、不会复读
（Whisper 在静音/口头禅处会陷入 "啊,可以去。" 循环），且在云端跑、本机不发烫。代价是它
没有公开 API，只能驱动用户**已登录千问**的浏览器（Kimi WebBridge，同 X 长文那条路）：

  打开 qianwen.com/discover/audioread → base64 分块把音频塞进 <input type=file>
  → 选 语言 / 区分发言人 → 确认 → 轮询「最近记录」直到完成 → 进结果页导出原文 .md
  → 从 ~/Downloads 读回、解析成 segments。

几个踩过的坑（与 ~/.claude/skills/qianwen-audioread 一致）：
- `DOM.setFileInputFiles` 在 chrome.debugger 下是 "Not allowed"；页面 fetch 本地 HTTP 会被
  阿里风控 SDK 包过的 window.fetch 挂死。只能把字节经 evaluate 分块推进页面。
- antd 下拉对合成 click 无效，必须 CDP Input.dispatchMouseEvent 发真实鼠标事件。
- 完成判断别只看状态词（上传中 / 解析中 …不止一个中间态），以「时长不再是 00:00」为准。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .ingest import IngestError, Segment, _webbridge

QIANWEN_URL = "https://www.qianwen.com/discover/audioread"
_SESSION = "priori-qianwen"
# 语言：中文 / 英语 / 日语 / 粤语 / 中英文自由说；发言人：暂不体验 / 单人演讲 / 2人对话 / 多人讨论
QW_LANG = os.environ.get("QIANWEN_LANG", "中英文自由说")
QW_SPEAKERS = os.environ.get("QIANWEN_SPEAKERS", "多人讨论")
DOWNLOADS = Path(os.environ.get("PRIORI_DOWNLOADS", str(Path.home() / "Downloads")))

_AUDIO_EXTS = (".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac")
_XYZ_RE = re.compile(r"xiaoyuzhoufm\.com/episode/", re.I)
_APPLE_RE = re.compile(r"podcasts\.apple\.com/.*/id(\d+).*[?&]i=(\d+)", re.I)

Progress = Callable[[str], None]


# --------------------------------------------------------------------------- #
# 链接 → 音频地址
# --------------------------------------------------------------------------- #

def _is_audio_url(url: str) -> bool:
    return url.split("?", 1)[0].lower().endswith(_AUDIO_EXTS)


def is_podcast_url(url: str) -> bool:
    url = url.strip()
    return bool(_XYZ_RE.search(url) or _APPLE_RE.search(url) or _is_audio_url(url))


def _http() -> Any:
    import requests

    s = requests.Session()
    s.trust_env = False  # 同 ingest：不走系统代理（抓包代理会改写证书）
    s.headers["User-Agent"] = "Mozilla/5.0 (Priori)"
    return s


def _meta(html: str, prop: str) -> str | None:
    m = re.search(rf'<meta[^>]+property="{re.escape(prop)}"[^>]+content="([^"]+)"', html)
    return m.group(1) if m else None


def _xyz_shownotes(page: str) -> str:
    """小宇宙把单集 shownotes（HTML）放在 __NEXT_DATA__ 里。取出来转纯文本，给纠错当术语表。"""
    import html as htmllib

    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    if not m:
        return ""
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return ""
    stack = [data]
    while stack:  # 结构随版本变，别写死路径：深搜第一个 shownotes 字段
        o = stack.pop()
        if isinstance(o, dict):
            if isinstance(o.get("shownotes"), str):
                return _html_to_text(htmllib.unescape(o["shownotes"]))
            stack.extend(o.values())
        elif isinstance(o, list):
            stack.extend(o)
    return ""


def _html_to_text(h: str) -> str:
    h = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", h, flags=re.I)
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"<[^>]+>", "", h)).strip()


def resolve(url: str) -> tuple[str, str, str]:
    """播客链接 → (音频直链, 标题, shownotes 纯文本)。支持小宇宙单集、Apple Podcasts 单集、音频直链。

    shownotes 里的专有名词写法是对的（Opus、eSIM、主播名…），纠错时当术语表用。"""
    import html as htmllib
    from urllib.parse import unquote, urlparse

    url = url.strip()
    if _is_audio_url(url):
        name = unquote(Path(urlparse(url).path).stem) or "播客"
        return url, name, ""

    s = _http()
    if _XYZ_RE.search(url):
        try:
            page = s.get(url, timeout=20).text
        except Exception as e:  # noqa: BLE001
            raise IngestError(f"打不开小宇宙页面：{e}") from e
        audio = _meta(page, "og:audio")
        if not audio:
            raise IngestError("没在小宇宙页面里找到音频地址（可能是付费单集）。")
        return audio, htmllib.unescape(_meta(page, "og:title") or "小宇宙播客"), _xyz_shownotes(page)

    if (m := _APPLE_RE.search(url)):
        show_id, ep_id = m.group(1), int(m.group(2))
        try:  # 单集 id 不能直接 lookup，只能列出节目的单集再按 trackId 匹配
            d = s.get("https://itunes.apple.com/lookup",
                      params={"id": show_id, "entity": "podcastEpisode", "limit": 200},
                      timeout=20).json()
        except Exception as e:  # noqa: BLE001
            raise IngestError(f"查询 Apple Podcasts 失败：{e}") from e
        for r in d.get("results", []):
            if r.get("trackId") == ep_id and r.get("episodeUrl"):
                return r["episodeUrl"], r.get("trackName") or "播客", _html_to_text(r.get("description") or "")
        raise IngestError("在 Apple Podcasts 里没找到这一集（只查得到最近 200 集）。")

    raise IngestError("不认识的播客链接。支持小宇宙单集、Apple Podcasts 单集或音频直链。")


def _download(audio_url: str, dest: Path) -> None:
    try:
        with _http().get(audio_url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with dest.open("wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
    except Exception as e:  # noqa: BLE001
        raise IngestError(f"下载音频失败：{e}") from e


def _compress(src: Path) -> Path:
    """人声 48kbps 单声道足够转写，体积降到约 1/3：分块注入少、浏览器负担小。没有 ffmpeg 就原样上传。"""
    if not shutil.which("ffmpeg"):
        return src
    out = src.with_name(src.stem + "-48k.mp3")
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
                        "-ac", "1", "-ar", "16000", "-b:a", "48k", str(out)],
                       capture_output=True)
    return out if r.returncode == 0 and out.exists() else src


def _duration(path: Path) -> float | None:
    if not shutil.which("ffprobe"):
        return None
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# 驱动千问页面
# --------------------------------------------------------------------------- #

def _wb(action: str, args: dict | None = None, timeout: int = 60) -> dict:
    return _webbridge(action, args, timeout=timeout, session=_SESSION)


def _eval(code: str, timeout: int = 60) -> Any:
    return _wb("evaluate", {"code": code}, timeout=timeout).get("value")


def _click(find_js: str) -> None:
    """find_js：返回目标元素的 JS 表达式。用 CDP 真实鼠标事件点它（antd 不认合成 click）。

    点之前先把 tab 切到前台：后台 tab（visibilityState=hidden）收不到 CDP 鼠标事件，点了等于没点。
    转写要等十几分钟，期间用户多半切走了，所以每次点击都切一次，而不是只在开头切。
    """
    _wb("cdp", {"method": "Page.bringToFront", "params": {}})
    pt = _eval(
        "(()=>{const el=(%s);if(!el)return null;el.scrollIntoView({block:'center'});"
        "const r=el.getBoundingClientRect();return JSON.stringify({x:r.x+r.width/2,y:r.y+r.height/2})})()"
        % find_js
    )
    if not pt:
        raise IngestError("千问页面结构变了，找不到要点的按钮。")
    p = json.loads(pt)
    for t in ("mouseMoved", "mousePressed", "mouseReleased"):
        _wb("cdp", {"method": "Input.dispatchMouseEvent",
                    "params": {"type": t, "x": p["x"], "y": p["y"], "button": "left",
                               "buttons": 1, "clickCount": 1}})
        time.sleep(0.05)


def _wait(cond_js: str, timeout: float, every: float = 1.0) -> Any:
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = _eval(cond_js)
        except IngestError:
            v = None  # 导航中页面短暂不可用
        if v:
            return v
        time.sleep(every)
    return None


def _child_by_text(test_id: str, text: str) -> str:
    return ("[...document.querySelector('[data-e2e-test-id=%s]').children]"
            ".find(c=>c.textContent.trim()===%s)" % (test_id, json.dumps(text)))


_SELECTED_JS = """(()=>{
  const sel=id=>[...(document.querySelector(`[data-e2e-test-id=${id}]`)||{children:[]}).children]
    .filter(c=>/Selected|btnActive/i.test(c.className)).map(c=>c.textContent.trim());
  const t=document.querySelector('[data-e2e-test-id=homeTransFile_targetLan_select]');
  return JSON.stringify({lang:sel('homeTransFile_chooseLan_select'),
    speaker:sel('homeTransFile_speaker_div'), trans:t?t.textContent.trim():''});
})()"""


def _row_js(stem: str) -> str:
    return ("(()=>{const r=[...document.querySelectorAll('[data-e2e-test-id=folders_item_div]')]"
            ".find(x=>x.innerText.includes(%s));return r?r.innerText.replace(/\\n+/g,' | '):''})()"
            % json.dumps(stem))


def _visible_button(text: str, last: bool = False) -> str:
    pick = "b[b.length-1]" if last else "b[0]"
    return ("(()=>{const b=[...document.querySelectorAll('button')].filter(e=>e.offsetParent&&"
            "e.innerText.replace(/\\s/g,'')===%s);return %s})()" % (json.dumps(text), pick))


def _inject(path: Path, name: str) -> None:
    data = path.read_bytes()
    chunk = 1_500_000  # 单块 base64 过 ~2MB 有截断风险
    mime = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".wav": "audio/wav"}.get(path.suffix.lower(), "audio/mpeg")
    _eval("window.__up=[];'ok'")
    for i in range(0, len(data), chunk):
        _eval("window.__up.push(%s);window.__up.length" % json.dumps(base64.b64encode(data[i:i + chunk]).decode()),
              timeout=180)
    res = _eval("""(()=>{
      const parts=window.__up.map(b=>{const s=atob(b),a=new Uint8Array(s.length);
        for(let i=0;i<s.length;i++)a[i]=s.charCodeAt(i);return a;});
      const f=new File([new Blob(parts,{type:%s})],%s,{type:%s});
      const dt=new DataTransfer();dt.items.add(f);
      const inp=document.querySelector('input[type=file]');if(!inp)return 'no-input';
      inp.files=dt.files;inp.dispatchEvent(new Event('change',{bubbles:true}));
      delete window.__up;return 'ok';})()""" % (json.dumps(mime), json.dumps(name), json.dumps(mime)))
    if res != "ok":
        raise IngestError("千问页面上找不到上传框，页面结构可能变了。")


def qianwen_transcribe(path: Path, stem: str, progress: Progress) -> tuple[str, str]:
    """把音频交给千问转写，返回 (导出的原文 markdown, 结果页 URL)。

    stem 用作千问里的记录名（不含空格），也是在「最近记录」里认出这条任务的依据。
    """
    progress("打开千问音视频速读…")
    _wb("navigate", {"url": QIANWEN_URL, "newTab": True, "group_title": "Priori 播客转写"})
    ready = "(()=>document.body&&document.body.innerText.includes('最近记录'))()"
    if not _wait(ready, 15):
        progress("请在弹出的浏览器里登录千问（qianwen.com），登录后会自动继续…")
        if not _wait(ready, 300, every=3):
            raise IngestError("等了 5 分钟还没登录千问，已放弃。登录后重新导入即可。")

    size_mb = path.stat().st_size / 1e6
    progress(f"上传音频到千问（{size_mb:.0f} MB）…")
    _inject(path, stem + path.suffix)
    if not _wait("document.body.innerText.includes(%s)" % json.dumps(stem), 30):
        raise IngestError("音频塞进千问后没有出现文件卡片，上传失败。")

    progress("设置转写选项…")
    _click(_child_by_text("homeTransFile_chooseLan_select", QW_LANG))
    _click(_child_by_text("homeTransFile_speaker_div", QW_SPEAKERS))
    time.sleep(0.5)
    sel = json.loads(_eval(_SELECTED_JS) or "{}")
    if sel.get("lang") != [QW_LANG] or sel.get("speaker") != [QW_SPEAKERS] or sel.get("trans") != "不翻译":
        raise IngestError(f"千问转写选项没设上：{sel}")

    _click(_visible_button("确认"))
    if not _wait("document.body.innerText.includes('任务添加成功')", 15, every=0.5):
        raise IngestError("点了确认，但千问没有提示「任务添加成功」。")

    # 轮询直到完成：时长不再是 00:00 即完成（中间态有 上传中 x% / 解析中 …）
    start, row = time.time(), ""
    while time.time() - start < 90 * 60:
        row = _eval(_row_js(stem)) or ""
        if re.search(r"(上传|解析|转写|识别)失败", row):  # 别匹配裸「失败」：完成后的关键词标签里也可能有
            raise IngestError(f"千问转写失败：{row}")
        if row and not re.search(r"上传中|解析中|排队", row) and not re.search(r"\b00:00\b", row) \
                and re.search(r"\d{2}:\d{2}", row):
            break
        state = re.search(r"上传中\s*\d*%?|解析中|排队中?", row)
        progress(f"千问转写中（{state.group(0) if state else '等待列表刷新'}，已等 {int(time.time() - start) // 60} 分钟）…")
        time.sleep(20)
    else:
        raise IngestError("千问 90 分钟都没转完，已放弃。")

    progress("转写完成，导出原文…")
    _click("[...document.querySelectorAll('[data-e2e-test-id=folders_item_div]')]"
           ".find(x=>x.innerText.includes(%s))" % json.dumps(stem))
    result_url = _wait("(()=>/transcripts\\//.test(location.href)&&location.href)()", 30) or ""
    if not _wait("(()=>[...document.querySelectorAll('button')].some(e=>e.offsetParent&&e.innerText.trim()==='导出'))()", 30):
        raise IngestError("结果页没有加载出「导出」按钮。")

    t0 = time.time()
    _click(_visible_button("导出"))
    _wait("(()=>document.querySelectorAll('button[role=checkbox]').length>0)()", 10, every=0.5)
    # 只导原文：原文默认勾上，其他块默认不勾——按 data-state 校正，别闭眼点
    _eval("""(()=>{[...document.querySelectorAll('button[role=checkbox]')].filter(e=>e.offsetParent)
      .forEach((c,i)=>c.setAttribute('data-priori-cb',i));return 1})()""")
    boxes = json.loads(_eval("""JSON.stringify([...document.querySelectorAll('[data-priori-cb]')]
      .map(c=>({i:c.dataset.prioriCb,label:c.parentElement.innerText.trim(),on:c.dataset.state==='checked'})))""") or "[]")
    for b in boxes:
        if b["on"] != b["label"].startswith("原文"):
            _click("document.querySelector('[data-priori-cb=\"%s\"]')" % b["i"])
    # 原文格式（第一个可见下拉）改成 .md
    _click("[...document.querySelectorAll('.ant-select')].filter(e=>e.offsetParent)[0].querySelector('.ant-select-selector')")
    time.sleep(0.6)
    _click("[...document.querySelectorAll('.ant-select-item-option')].find(e=>e.offsetParent&&e.innerText.trim()==='.md')")
    time.sleep(0.4)
    _click(_visible_button("导出", last=True))  # 面板底部的确认「导出」= 最后一个可见的

    deadline = time.time() + 60
    while time.time() < deadline:
        hits = [p for p in DOWNLOADS.glob(f"{stem}_原文*.md") if p.stat().st_mtime >= t0 - 1]
        if hits:
            f = max(hits, key=lambda p: p.stat().st_mtime)
            time.sleep(0.5)  # 等 Chrome 写完
            text = f.read_text(encoding="utf-8")
            f.unlink(missing_ok=True)  # 不在 Downloads 留副本
            return text, result_url
        time.sleep(1)
    raise IngestError(f"千问导出后没在 {DOWNLOADS} 里等到 {stem}_原文.md。")


# --------------------------------------------------------------------------- #
# 千问原文 markdown → segments + 说话人轮次
# --------------------------------------------------------------------------- #

_HEAD_RE = re.compile(r"^(发言人\s*\d+|[^\s].{0,20}?)\s{2,}((?:\d{1,2}:)?\d{1,2}:\d{2})\s*$")
_SENT_RE = re.compile(r"[^。！？!?…]+[。！？!?…]*[”」』）)]*")


# 盘古之白：中文与英文 / 数字之间加一个空格（中文排版惯例）。只认汉字和假名，不含中文标点，
# 「GPT，」「，GPT」这类挨着标点的不加。
_CJK = r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]"
_CJK_THEN_LATIN = re.compile(rf"({_CJK})([A-Za-z0-9@#$&(\[])")
_LATIN_THEN_CJK = re.compile(rf"([A-Za-z0-9%)\]])({_CJK})")


def pangu(text: str) -> str:
    return _LATIN_THEN_CJK.sub(r"\1 \2", _CJK_THEN_LATIN.sub(r"\1 \2", text))


def _ts(s: str) -> float:
    return float(sum(int(x) * 60 ** i for i, x in enumerate(reversed(s.split(":")))))


def _fmt(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 60:02d}:{sec % 60:02d}"


def parse_qianwen_md(md: str, duration: float | None = None) -> tuple[list[Segment], list[dict]]:
    """千问导出格式：

        <记录名>
        <导出时间>
        发言人1   00:00
        正文……

    每段（一个人的一次发言）按句末标点再切成句子，句子时间在本段起点到下段起点之间按字数插值
    ——和 ingest.resegment_by_sentence 同一思路。返回 (segments, turns)。
    """
    paras: list[list] = []  # [speaker, start, text]
    for line in md.splitlines()[2:]:
        s = line.strip()
        m = _HEAD_RE.match(s)
        if m:
            paras.append([re.sub(r"\s+", "", m.group(1)), _ts(m.group(2)), ""])
        elif s and paras:
            paras[-1][2] += s
    paras = [p for p in paras if p[2]]
    if not paras:
        raise IngestError("千问导出的原文是空的。")

    segments: list[Segment] = []
    turns: list[dict] = []
    for k, (spk, st, text) in enumerate(paras):
        nxt = paras[k + 1][1] if k + 1 < len(paras) else (duration or st + len(text) * 0.22)
        nxt = max(nxt, st + 0.5)
        sents = [pangu(x.strip()) for x in _SENT_RE.findall(text) if x.strip()] or [pangu(text)]
        total, pos = sum(len(x) for x in sents), 0
        first = len(segments)
        for x in sents:
            a = st + (nxt - st) * pos / total
            pos += len(x)
            segments.append({"start": round(a, 2), "end": round(st + (nxt - st) * pos / total, 2),
                             "text": x, "speaker": spk})
        if turns and turns[-1]["speaker"] == spk:
            turns[-1]["end"] = len(segments) - 1
        else:
            turns.append({"start": first, "end": len(segments) - 1, "speaker": spk, "start_ts": _fmt(st)})
    return segments, turns


# --------------------------------------------------------------------------- #
# 后台任务：整条链路动辄十几分钟，不能卡住 HTTP 请求
# --------------------------------------------------------------------------- #

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_browser_lock = threading.Lock()  # 同一时间只驱动一个千问任务（共用一个浏览器 session）


def job_for_key(key: str) -> dict | None:
    with _jobs_lock:
        return next((j for j in _jobs.values() if j["key"] == key and j["status"] == "running"), None)


def get_job(job_id: str) -> dict | None:
    return _jobs.get(job_id)


def start_task(key: str, fn: Callable[[Progress], str], what: str = "任务") -> dict:
    """通用后台任务：fn(progress) 返回 doc_id。前端统一轮询 /api/ingest/job/{id} 看进度。"""
    with _jobs_lock:
        job = {"id": uuid.uuid4().hex[:12], "key": key, "status": "running",
               "message": "排队中…", "doc_id": None, "error": None}
        _jobs[job["id"]] = job

    def progress(msg: str) -> None:
        job["message"] = msg

    def run() -> None:
        try:
            job["doc_id"] = fn(progress)
            job["status"] = "done"
        except IngestError as e:
            job["status"], job["error"] = "error", str(e)
        except Exception as e:  # noqa: BLE001  兜底：线程里的异常不能悄悄吞掉
            job["status"], job["error"] = "error", f"{what}出错：{e}"

    threading.Thread(target=run, daemon=True).start()
    return job


def start_job(url: str, key: str, on_done: Callable[[list[Segment], list[dict], dict, Progress], str]) -> dict:
    """播客导入全链路。on_done(segments, turns, meta, progress) 负责入库（含纠错）并返回 doc_id。"""

    def work(progress: Progress) -> str:
        tmp = Path(tempfile.mkdtemp(prefix="priori-pod-"))
        try:
            progress("解析播客链接…")
            audio_url, title, notes = resolve(url)
            progress("下载音频…")
            raw = tmp / ("audio" + (Path(audio_url.split("?", 1)[0]).suffix or ".mp3"))
            _download(audio_url, raw)
            duration = _duration(raw)
            progress("压缩音频…")
            up = _compress(raw)
            stem = "priori-" + hashlib.sha1(key.encode()).hexdigest()[:10]
            if _browser_lock.locked():
                progress("前面还有一个千问任务在跑，排队等待…")
            with _browser_lock:
                md, result_url = qianwen_transcribe(up, stem, progress)
            segments, turns = parse_qianwen_md(md, duration)
            return on_done(segments, turns, {"audio_url": audio_url, "title": title,
                                             "shownotes": notes, "qianwen_url": result_url}, progress)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    return start_task(key, work, "播客导入")
