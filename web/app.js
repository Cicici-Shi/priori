// 状态
const state = {
  docId: null,
  segments: [],
  chapters: [],
  turns: [],          // [{start,end,speaker}]
  segSpeaker: [],     // segIdx -> 原始说话人标签
  speakerOrder: [],   // 去重说话人，按出现顺序（决定配色）
  speakerNames: {},   // 原始标签 -> 用户改的显示名
  cleanMode: false,   // 阅读模式：AI 清洗口水词
  paragraphs: [],     // [{ci,a,b,startSec,raw}] 规则分好的段落
  cleanedMap: {},     // 原始段落文本 -> AI 清洗后文本
  selection: null,    // { text, segRange: [lo, hi] }
  collapsed: new Set(),    // 折叠的章节 index
  notesLoading: new Set(), // 正在生成笔记的章节 index
  videoId: null,           // 当前 YouTube 视频 id（用于按视频存播放位置）
  glossBySeg: {},          // segIdx -> [{term, zh}] 生词标注
  translateMode: false,    // 段落中文翻译开关
  subtitleMode: false,     // 逐句双语对照（更细的切分 + 每句下挂中文）
  translatedMap: {},       // 段落原文 -> 中文翻译
};

// 说话人识别暂时关闭（文本猜测不可靠，等接入声学分离再开）
const SPEAKERS_ENABLED = false;

// 规则分段参数（纯靠时间戳 + 标点，零 AI 猜测）
const PARA_OPTS = { maxSec: 30, maxChars: 300, gapSec: 1.4, hardSec: 55, hardGap: 2.5 }; // 正常段落
const SUB_OPTS = { maxSec: 6, maxChars: 70, gapSec: 0.7, hardSec: 12, hardGap: 1.6 };    // 逐句字幕：碎得多

const SPK_COLORS = ["#2563eb", "#d97757", "#16a34a", "#9333ea", "#ca8a04", "#0891b2"];
const colorForSpeaker = (label) => {
  const i = state.speakerOrder.indexOf(label);
  return SPK_COLORS[(i < 0 ? 0 : i) % SPK_COLORS.length];
};
const displayName = (label) => state.speakerNames[label] || label;

const $ = (id) => document.getElementById(id);

// 工具
function fmtTs(sec) {
  if (sec == null) return "";
  sec = Math.floor(sec);
  const m = String(Math.floor(sec / 60)).padStart(2, "0");
  const s = String(sec % 60).padStart(2, "0");
  return `${m}:${s}`;
}

function setStatus(msg, kind = "") {
  const el = $("ingest-status");
  el.textContent = msg || "";
  el.className = "status " + kind;
}

async function postJSON(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(err.detail || "请求失败");
  }
  return r.json();
}

// 导入
async function ingestUrl() {
  const url = $("url-input").value.trim();
  if (!url) return;
  setStatus("正在抓取字幕…", "loading");
  try {
    const data = await postJSON("/api/ingest/url", { url });
    onIngested(data);
  } catch (e) {
    setStatus("导入失败：" + e.message, "error");
  }
}

async function ingestFile(file) {
  setStatus(`正在处理 ${file.name}…（音视频转写可能较久）`, "loading");
  const fd = new FormData();
  fd.append("file", file);
  try {
    const r = await fetch("/api/ingest/file", { method: "POST", body: fd });
    if (!r.ok) {
      const err = await r.json().catch(() => ({ detail: r.statusText }));
      throw new Error(err.detail || "处理失败");
    }
    onIngested(await r.json());
  } catch (e) {
    setStatus("导入失败：" + e.message, "error");
  }
}

function onIngested(data) {
  state.docId = data.doc_id;
  state.segments = data.segments;
  state.chapters = [];
  state.turns = [];
  state.segSpeaker = [];
  state.speakerOrder = [];
  state.speakerNames = {};
  state.cleanMode = false;
  state.cleanedMap = data.cleaned_map || {};
  state.collapsed = new Set();
  state.notesLoading = new Set();
  state.glossBySeg = {};
  lastNoteCh = -1; lastNotePoint = null; // 复位笔记同步
  state.translatedMap = data.translated_map || {};
  state.translateMode = LS.get("tq.translate") === "1"; // 恢复上次的翻译开关
  $("translate-toggle").checked = state.translateMode;
  state.subtitleMode = LS.get("tq.subtitle") === "1";   // 恢复逐句双语开关
  $("subtitle-toggle").checked = state.subtitleMode;
  $("read-toolbar").hidden = false;
  $("mode-raw").classList.add("active");
  $("mode-clean").classList.remove("active");
  $("doc-title").textContent = data.video_id ? "" : (data.title || ""); // 视频名等播放器就绪后填
  renderChat(data.chat || []); // 刷新后恢复历史问答
  $("settings-pop").hidden = true; // 导入后收起设置浮层
  mountVideo(data.video_id); // YouTube 源 → 嵌入吸顶播放器并联动；其他源自动隐藏
  applyHideVideo(); // 恢复「隐藏视频」开关

  // 命中缓存（同一视频/文件之前导入过）→ 直接复用章节/说话人，无需重算
  const hasCachedChapters = data.chapters && data.chapters.length;
  const hasCachedTurns = SPEAKERS_ENABLED && data.turns && data.turns.length;
  if (hasCachedTurns) {
    state.turns = data.turns;
    state.speakerOrder = data.speakers || [];
    state.segSpeaker = new Array(state.segments.length).fill(null);
    state.turns.forEach((t) => {
      for (let i = t.start; i <= t.end && i < state.segSpeaker.length; i++) state.segSpeaker[i] = t.speaker;
    });
  }
  if (hasCachedChapters) {
    state.chapters = data.chapters;
    renderChapterCards(state.chapters);
    buildGlossIndex(); // 缓存的生词 → 索引，供 renderTranscript 即时标注
  }
  renderTranscript();
  renderSpeakerBar();
  if (state.translateMode || state.subtitleMode) ensureTranslated(); // 补齐未缓存的翻译

  if (hasCachedChapters) {
    setStatus("已导入 ✓（复用缓存）");
    fetchAllNotes().then(() => fetchAllGlossary()); // 补齐尚未生成的笔记 / 生词
  } else {
    setStatus("已导入 ✓");
    fetchChapters(); // 首次导入：切分章节 → 笔记 → 生词
  }
}

// 说话人识别
function renderSpeakerBar() {
  const bar = $("speaker-bar");
  if (!SPEAKERS_ENABLED) { bar.hidden = true; return; }
  bar.hidden = !state.docId;
  bar.innerHTML = "";
  if (!state.docId) return;

  if (!state.turns.length) {
    const btn = document.createElement("button");
    btn.className = "primary";
    btn.textContent = "🗣️ 识别说话人";
    btn.onclick = () => identifySpeakers();
    bar.appendChild(btn);
    const hint = document.createElement("span");
    hint.className = "hint";
    hint.textContent = "按内容推断谁在说（最佳推断，约 1–4 分钟，结果会缓存）";
    bar.appendChild(hint);
    return;
  }

  state.speakerOrder.forEach((label) => {
    const chip = document.createElement("span");
    chip.className = "spk-legend";
    chip.title = "点击重命名";
    chip.innerHTML =
      `<span class="spk-dot" style="background:${colorForSpeaker(label)}"></span>` +
      `<span>${escapeHtml(displayName(label))}</span>`;
    chip.onclick = () => renameSpeaker(label);
    bar.appendChild(chip);
  });
  const re = document.createElement("button");
  re.className = "ghost";
  re.textContent = "重新识别";
  re.onclick = () => identifySpeakers(true);
  bar.appendChild(re);
}

function renameSpeaker(label) {
  const cur = displayName(label);
  const name = window.prompt(`把「${cur}」改成：`, cur);
  if (name && name.trim()) {
    state.speakerNames[label] = name.trim();
    renderSpeakerBar();
    renderTranscript();
  }
}

async function identifySpeakers(force = false) {
  const backend = $("backend-select").value || undefined;
  const chapters = state.chapters;
  try {
    if (chapters && chapters.length) {
      // 分章节渐进识别：逐章调用，每次返回累积结果，边出边渲染
      if (force) { state.turns = []; state.segSpeaker = []; state.speakerOrder = []; renderTranscript(); }
      for (let i = 0; i < chapters.length; i++) {
        setStatus(`正在识别说话人… ${i + 1}/${chapters.length} 章`, "loading");
        const ch = chapters[i];
        const data = await postJSON("/api/speakers", {
          doc_id: state.docId, backend, seg_range: [ch.start, ch.end],
        });
        applySpeakers(data); // 后端返回合并后的全部 turns，渐进填充
      }
      setStatus("说话人已标注 ✓");
    } else {
      setStatus("正在识别说话人…（约 1–4 分钟）", "loading");
      const data = await postJSON("/api/speakers", { doc_id: state.docId, backend, force: !!force });
      applySpeakers(data);
      setStatus("说话人已标注 ✓");
    }
  } catch (e) {
    setStatus("识别失败：" + e.message, "error");
  }
}

function applySpeakers(data) {
  state.turns = data.turns || [];
  state.speakerOrder = data.speakers || [];
  state.segSpeaker = new Array(state.segments.length).fill(null);
  state.turns.forEach((t) => {
    for (let i = t.start; i <= t.end && i < state.segSpeaker.length; i++) {
      state.segSpeaker[i] = t.speaker;
    }
  });
  renderSpeakerBar();
  renderTranscript();
}

function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// Markdown → 安全 HTML
function mdRender(text) {
  try {
    return DOMPurify.sanitize(marked.parse(text || "", { breaks: true, gfm: true }));
  } catch (e) {
    return escapeHtml(text || "");
  }
}

// 行内 Markdown（只渲染 **加粗** 等，不包 <p>）—— 用于笔记要点
function mdInline(text) {
  try {
    return DOMPurify.sanitize(marked.parseInline(text || "", { gfm: true }));
  } catch (e) {
    return escapeHtml(text || "");
  }
}

async function fetchChapters(force = false) {
  const body = $("summary-body");
  body.className = "summary-body pending";
  body.textContent = "正在切分章节…";
  const backend = $("backend-select").value || undefined;
  try {
    const data = await postJSON("/api/summary", { doc_id: state.docId, backend, force });
    state.chapters = data.chapters || [];
    state.collapsed = new Set();
    renderChapterCards(state.chapters);
    renderTranscript(); // 重渲染：按章节分组（含说话人，如已标注）
    fetchAllNotes(force).then(() => fetchAllGlossary(force)); // 笔记 → 生词，边出边渲染
  } catch (e) {
    body.className = "summary-body";
    body.textContent = "章节生成失败：" + e.message;
  }
}

// 左栏：逐章笔记卡片
function renderChapterCards(chapters) {
  const body = $("summary-body");
  body.className = "summary-body";
  body.innerHTML = "";
  chapters.forEach((ch, i) => body.appendChild(buildChapterCard(ch, i)));
}

// 单张笔记卡片：章节头 + 主旨 + 要点（要点可点击跳到对应字幕）
function buildChapterCard(ch, i) {
  const card = document.createElement("div");
  card.className = "chap-card";
  if (state.collapsed.has(i)) card.classList.add("collapsed");
  if (i === lastNoteCh) card.classList.add("active"); // 正在播放的章节
  card.dataset.chapter = i;

  const head = document.createElement("div");
  head.className = "chap-card-head";
  head.innerHTML =
    `<span class="chap-caret">▾</span>` +
    `<span class="chap-ts">${ch.start_ts || ""}</span>` +
    `<span class="chap-title">${escapeHtml(ch.title)}</span>`;
  head.querySelector(".chap-caret").addEventListener("click", (e) => {
    e.stopPropagation();
    toggleCollapse(i);
  });
  head.addEventListener("click", () => locateChapter(i)); // 点章节头 → 跳到该章字幕
  card.appendChild(head);

  const noteBody = document.createElement("div");
  noteBody.className = "note-body";

  if (ch.gist) {
    const g = document.createElement("div");
    g.className = "note-gist";
    g.innerHTML = mdInline(ch.gist);
    noteBody.appendChild(g);
  }

  if (ch.points && ch.points.length) {
    const ul = document.createElement("ul");
    ul.className = "note-points";
    ch.points.forEach((p) => ul.appendChild(buildPoint(p)));
    noteBody.appendChild(ul);
  } else if (state.notesLoading.has(i)) {
    const ld = document.createElement("div");
    ld.className = "note-loading";
    ld.textContent = "正在整理要点…";
    noteBody.appendChild(ld);
  }

  card.appendChild(noteBody);
  return card;
}

function buildPoint(p) {
  const li = document.createElement("li");
  li.className = "note-point";
  li.dataset.seg = p.seg; // 供播放时定位当前要点
  li.innerHTML =
    `<span class="np-text">${mdInline(p.text)}</span>` +
    (p.ts ? `<span class="np-ts">⤷${p.ts}</span>` : "");
  li.addEventListener("click", () => locateSeg(p.seg)); // 点要点 → 跳到对应字幕句
  if (p.details && p.details.length) {
    const dul = document.createElement("ul");
    dul.className = "note-details";
    p.details.forEach((d) => {
      const dli = document.createElement("li");
      dli.innerHTML = mdInline(d.text);
      dli.addEventListener("click", (e) => { e.stopPropagation(); locateSeg(d.seg); });
      dul.appendChild(dli);
    });
    li.appendChild(dul);
  }
  return li;
}

function toggleCollapse(i) {
  if (state.collapsed.has(i)) state.collapsed.delete(i);
  else state.collapsed.add(i);
  document.querySelector(`.chap-card[data-chapter="${i}"]`)?.classList.toggle("collapsed");
}

// 替换某一章的卡片（笔记到达 / 加载态变化时局部刷新，不动其他卡片的折叠状态）
function refreshCard(i) {
  const old = document.querySelector(`.chap-card[data-chapter="${i}"]`);
  if (old) old.replaceWith(buildChapterCard(state.chapters[i], i));
}

// 逐章生成笔记：并发池（前面的章节先处理），每章到达即刷新该卡片（聚焦、稳定、可缓存）
const NOTES_CONCURRENCY = 4;
async function fetchAllNotes(force = false) {
  const docId = state.docId;
  const backend = $("backend-select").value || undefined;
  // 待办章节，升序入队 → worker 按序取，靠前的章节先开工
  const queue = [];
  state.chapters.forEach((ch, i) => {
    if (force || !(ch.points && ch.points.length)) queue.push(i);
  });
  let cursor = 0;
  async function worker() {
    while (cursor < queue.length) {
      const i = queue[cursor++];
      if (state.docId !== docId) return; // 期间切换了视频 → 停手
      const ch = state.chapters[i];
      state.notesLoading.add(i);
      refreshCard(i);
      try {
        const data = await postJSON("/api/notes", { doc_id: docId, index: i, backend, force });
        ch.gist = data.gist || "";
        ch.points = data.points || [];
      } catch (e) {
        ch.points = ch.points || []; // 失败留空，可点 ↻ 重试
      } finally {
        state.notesLoading.delete(i);
        if (state.docId === docId) refreshCard(i);
      }
    }
  }
  const n = Math.min(NOTES_CONCURRENCY, queue.length);
  await Promise.all(Array.from({ length: n }, () => worker()));
}

// 生词标注：逐章挑生词 → 字幕里下划线 + 小字中文
const GLOSS_CONCURRENCY = 3;
async function fetchAllGlossary(force = false) {
  const docId = state.docId;
  const backend = $("backend-select").value || undefined;
  const queue = [];
  state.chapters.forEach((ch, i) => { if (force || ch.glossary === undefined) queue.push(i); });
  let cursor = 0;
  async function worker() {
    while (cursor < queue.length) {
      const i = queue[cursor++];
      if (state.docId !== docId) return;
      try {
        const data = await postJSON("/api/glossary", { doc_id: docId, index: i, backend, force });
        state.chapters[i].glossary = data.glossary || [];
        applyChapterGloss(i);
      } catch (e) {
        state.chapters[i].glossary = state.chapters[i].glossary || [];
      }
    }
  }
  await Promise.all(Array.from({ length: Math.min(GLOSS_CONCURRENCY, queue.length) }, () => worker()));
}

// 已有章节的生词汇入 segIdx 索引（渲染时即时标注）
function buildGlossIndex() {
  state.glossBySeg = {};
  state.chapters.forEach((ch) => (ch.glossary || []).forEach((g) => {
    (state.glossBySeg[g.seg] = state.glossBySeg[g.seg] || []).push({ term: g.term, zh: g.zh });
  }));
}

// 某章生词到达 → 汇入索引并就地更新受影响的字幕句（不整篇重渲染）
function applyChapterGloss(i) {
  const touched = new Set();
  (state.chapters[i].glossary || []).forEach((item) => {
    (state.glossBySeg[item.seg] = state.glossBySeg[item.seg] || []).push({ term: item.term, zh: item.zh });
    touched.add(item.seg);
  });
  const box = $("transcript");
  touched.forEach((idx) => {
    const span = box.querySelector(`.seg[data-idx="${idx}"]`);
    if (span && state.segments[idx]) span.innerHTML = segHTML(state.segments[idx].text, state.glossBySeg[idx]);
  });
}

// 字幕文本 → HTML：命中的生词包 <ruby>（下划线 + 小字中文）
function segHTML(text, glosses) {
  if (!glosses || !glosses.length) return escapeHtml(text) + " ";
  const low = text.toLowerCase();
  const ranges = [];
  glosses.forEach((g) => {
    const idx = low.indexOf(String(g.term).toLowerCase());
    if (idx >= 0) ranges.push({ s: idx, e: idx + g.term.length, zh: g.zh });
  });
  ranges.sort((a, b) => a.s - b.s);
  const keep = [];
  let lastEnd = -1;
  for (const r of ranges) { if (r.s >= lastEnd) { keep.push(r); lastEnd = r.e; } }
  if (!keep.length) return escapeHtml(text) + " ";
  let out = "", pos = 0;
  for (const r of keep) {
    out += escapeHtml(text.slice(pos, r.s));
    out += `<ruby class="gloss">${escapeHtml(text.slice(r.s, r.e))}<rt>${escapeHtml(r.zh)}</rt></ruby>`;
    pos = r.e;
  }
  out += escapeHtml(text.slice(pos)) + " ";
  return out;
}

// 段落中文翻译（设置里开关，译文放在每段下面）
const TRANSLATE_CONCURRENCY = 3;
async function ensureTranslated() {
  const docId = state.docId;
  const backend = $("backend-select").value || undefined;
  const byChapter = {};
  state.paragraphs.forEach((p) => { (byChapter[p.ci] = byChapter[p.ci] || []).push(p); });
  const cis = Object.keys(byChapter);
  let cursor = 0, done = 0;
  async function worker() {
    while (cursor < cis.length) {
      const ci = cis[cursor++];
      if (state.docId !== docId) return;
      const need = byChapter[ci].filter((p) => !state.translatedMap[p.raw]);
      done++;
      if (!need.length) continue;
      setStatus(`翻译中… ${done}/${cis.length} 章`, "loading");
      try {
        const data = await postJSON("/api/translate", { doc_id: docId, backend, paragraphs: need.map((p) => p.raw) });
        (data.translated || []).forEach((t, k) => { state.translatedMap[need[k].raw] = t; });
        if (state.translateMode) updateTranslations();
      } catch (e) { /* 失败留空，可重开开关重试 */ }
    }
  }
  await Promise.all(Array.from({ length: TRANSLATE_CONCURRENCY }, () => worker()));
  if (state.docId === docId) setStatus("翻译完成 ✓");
}

// 就地填入已到达的译文（不整篇重渲染，避免滚动跳动）
function updateTranslations() {
  const box = $("transcript");
  state.paragraphs.forEach((p, pi) => {
    const t = state.translatedMap[p.raw];
    if (!t) return;
    const el = box.querySelector(`.para-zh[data-pi="${pi}"]`);
    if (el) { el.textContent = t; el.classList.remove("pending"); }
  });
}

// 找到片段 i 对应的 DOM（清洗模式只渲染段落起始 seg，取 <= i 的最近一个）
function segEleFor(i) {
  const box = $("transcript");
  let el = box.querySelector(`.seg[data-idx="${i}"]`);
  if (el) return el;
  let best = null, bestIdx = -1;
  box.querySelectorAll(".seg[data-idx]").forEach((s) => {
    const idx = parseInt(s.dataset.idx, 10);
    if (idx <= i && idx > bestIdx) { bestIdx = idx; best = s; }
  });
  return best;
}

// 点要点/字幕 → 滚动到对应句并高亮闪烁，同时把视频跳到该时间
function locateSeg(seg) {
  const el = segEleFor(seg);
  if (el) {
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    el.classList.remove("seg-flash");
    void el.offsetWidth; // 重置动画
    el.classList.add("seg-flash");
  }
  seekVideo(state.segments[seg] && state.segments[seg].start);
}

// YouTube 播放器 ←→ 字幕联动
let player = null, playerReady = false, followTimer = null;
let activeEl = null, userScrollUntil = 0, notesScrollUntil = 0, _ytReady = null;

function ensureYT() {
  if (window.YT && window.YT.Player) return Promise.resolve();
  if (_ytReady) return _ytReady;
  _ytReady = new Promise((resolve) => {
    window.onYouTubeIframeAPIReady = resolve;
    const tag = document.createElement("script");
    tag.src = "https://www.youtube.com/iframe_api";
    document.head.appendChild(tag);
  });
  return _ytReady;
}

// 挂载/切换视频；非 YouTube 源传 null → 隐藏播放器
async function mountVideo(videoId) {
  const pane = $("video-pane");
  stopFollow();
  if (player && player.destroy) { try { player.destroy(); } catch (e) {} }
  player = null; playerReady = false; activeEl = null;
  state.videoId = videoId || null;
  if (!videoId) { pane.hidden = true; return; }
  pane.hidden = false;
  document.querySelector(".video-frame").innerHTML = '<div id="yt-player"></div>';
  await ensureYT();
  player = new YT.Player("yt-player", {
    // 普通 youtube.com 域复用已登录会话，避开机器人验证墙
    videoId,
    // controls:0 去掉自带控件，改用自有控制条
    playerVars: {
      rel: 0, modestbranding: 1, playsinline: 1, origin: location.origin,
      controls: 0, iv_load_policy: 3, disablekb: 0, // 开 YT 原生键盘：焦点在视频上时左右键也能 ±5s
    },
    events: {
      onReady: () => { playerReady = true; setTitleFromVideo(); initVidCtrl(); restorePos(); applyRate(parseFloat(LS.get("tq.rate") || "1")); },
      onStateChange: (e) => {
        setTitleFromVideo(); // 元数据有时晚于 onReady 才就绪，这里补一次
        $("vid-play").innerHTML = (e.data === YT.PlayerState.PLAYING) ? PAUSE_SVG : PLAY_SVG;
        if (e.data === YT.PlayerState.PLAYING) startFollow();
        else { stopFollow(); savePos(); }
      },
    },
  });
}

// 用真实视频名作标签页标题
function setTitleFromVideo() {
  const d = player && player.getVideoData && player.getVideoData();
  if (d && d.title) {
    document.title = d.title;
    const el = $("doc-title"); el.textContent = d.title; el.title = d.title;
  }
}

// 自有控制条
let vidSeeking = false, lastPosSave = 0;
function initVidCtrl() {
  const dur = player && player.getDuration ? player.getDuration() : 0;
  if (dur) { $("vid-seek").max = dur; $("vid-dur").textContent = fmtTs(dur); }
  $("vid-cur").textContent = fmtTs(player.getCurrentTime ? player.getCurrentTime() : 0);
}
function updateVidCtrl(t) {
  const seek = $("vid-seek");
  const dur = player && player.getDuration ? player.getDuration() : 0;
  if (dur && !vidSeeking) { if (+seek.max !== dur) seek.max = dur; seek.value = t; }
  $("vid-cur").textContent = fmtTs(t);
  if (dur) $("vid-dur").textContent = fmtTs(dur);
  const now = Date.now(); // 播放中每 2s 存一次进度
  if (state.videoId && !vidSeeking && now - lastPosSave > 2000) { LS.set("tq.pos." + state.videoId, Math.floor(t)); lastPosSave = now; }
}
// 播放/暂停图标（SVG，像素级居中）
const PLAY_SVG = '<svg viewBox="0 0 24 24" width="11" height="11"><path fill="currentColor" d="M8 5v14l11-7z"/></svg>';
const PAUSE_SVG = '<svg viewBox="0 0 24 24" width="11" height="11"><path fill="currentColor" d="M6 5h4v14H6zM14 5h4v14h-4z"/></svg>';

// 倍速：选项与 YouTube 原生一致
const RATES = [0.25, 0.5, 0.75, 1, 1.25, 1.5, 1.75, 2];
function applyRate(r) {
  if (player && player.setPlaybackRate) player.setPlaybackRate(r);
  $("vid-rate").value = String(r);
  LS.set("tq.rate", String(r));
}

function applyHideVideo() {
  const hv = LS.get("tq.hidevideo") === "1";
  $("hide-video-toggle").checked = hv;
  $("video-pane").classList.toggle("user-hidden", hv);
}

// 按视频 id 记/取播放位置
function savePos() {
  if (state.videoId && player && player.getCurrentTime) LS.set("tq.pos." + state.videoId, Math.floor(player.getCurrentTime()));
}
function restorePos() {
  if (!state.videoId) return;
  const p = LS.get("tq.pos." + state.videoId);
  if (p && player.seekTo) player.seekTo(parseFloat(p), true); // 跳到上次位置（不自动播放）
}

// 字幕/要点 → 视频：跳到指定秒并播放
function seekVideo(sec) {
  if (sec == null || !player || !playerReady || !player.seekTo) return;
  player.seekTo(sec, true);
  player.playVideo();
}

function startFollow() { stopFollow(); followTimer = setInterval(syncActiveSeg, 250); }
function stopFollow() { if (followTimer) { clearInterval(followTimer); followTimer = null; } }

// 视频 → 字幕：高亮当前播放所在的句子，必要时滚动跟随（用户手动滚动后暂停跟随 4s）
function syncActiveSeg() {
  if (!playerReady || !player.getCurrentTime) return;
  const t = player.getCurrentTime();
  updateVidCtrl(t); // 同步自有控制条（进度 + 时间）
  const segs = state.segments;
  if (!segs.length) return;
  let lo = 0, hi = segs.length - 1, idx = -1; // 二分：start <= t 的最后一个片段
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if ((segs[mid].start ?? 0) <= t) { idx = mid; lo = mid + 1; } else hi = mid - 1;
  }
  if (idx < 0) return;
  syncNotes(idx); // 左栏笔记同步高亮
  const el = segEleFor(idx);
  if (!el || el === activeEl) return;
  if (activeEl) activeEl.classList.remove("seg-active");
  el.classList.add("seg-active");
  activeEl = el;
  if (Date.now() > userScrollUntil) scrollSegToFraction(el);
}

// 把当前字幕句滚到紧贴视频下方，像字幕一样始终可见
function scrollSegToFraction(el) {
  const pane = $("content-pane");
  const paneRect = pane.getBoundingClientRect();
  const vp = $("video-pane");
  const topRef = (vp && !vp.hidden) ? vp.getBoundingClientRect().bottom : paneRect.top;
  const target = topRef + 8; // 紧贴视频下方，像字幕一样直接看到当前句
  const delta = el.getBoundingClientRect().top - target;
  if (Math.abs(delta) > 24) pane.scrollTo({ top: pane.scrollTop + delta, behavior: "smooth" });
}

// 笔记跟随播放：高亮当前章节（并滚入视野）+ 当前所在要点
let lastNoteCh = -1, lastNotePoint = null;
function syncNotes(idx) {
  if (!state.chapters.length) return;
  const ci = state.chapters.findIndex((c) => idx >= c.start && idx <= c.end);
  if (ci < 0) return;
  if (ci !== lastNoteCh) {
    document.querySelectorAll(".chap-card.active").forEach((e) => e.classList.remove("active"));
    const c = document.querySelector(`.chap-card[data-chapter="${ci}"]`);
    if (c) { c.classList.add("active"); if (Date.now() > notesScrollUntil) scrollNotesCenter(c); }
    lastNoteCh = ci;
  }
  const card = document.querySelector(`.chap-card[data-chapter="${ci}"]`);
  if (!card) return;
  let best = null, bestSeg = -1; // 该章里 seg <= idx 的最后一个要点
  card.querySelectorAll(".note-point[data-seg]").forEach((li) => {
    const s = +li.dataset.seg;
    if (s <= idx && s > bestSeg) { bestSeg = s; best = li; }
  });
  if (best !== lastNotePoint) {
    if (lastNotePoint) lastNotePoint.classList.remove("np-current");
    if (best) best.classList.add("np-current");
    lastNotePoint = best;
    if (Date.now() > notesScrollUntil) scrollNotesCenter(best || card); // 滚到笔记栏中部
  }
}

// 把当前要点/章节滚到笔记栏竖直中部
function scrollNotesCenter(el) {
  el.scrollIntoView({ behavior: "smooth", block: "center" });
}

// 点章节卡片 → 滚动到对应字幕并高亮
function locateChapter(i) {
  document.querySelectorAll(".chap-card.active").forEach((e) => e.classList.remove("active"));
  document.querySelector(`.chap-card[data-chapter="${i}"]`)?.classList.add("active");

  const block = document.querySelector(`.chap-block[data-chapter="${i}"]`);
  if (block) {
    block.scrollIntoView({ behavior: "smooth", block: "start" });
    block.classList.remove("flash");
    void block.offsetWidth; // 重置动画
    block.classList.add("flash");
  }
}

// 渲染 transcript：每段一个可定位的 span
function makeSeg(seg, i) {
  const span = document.createElement("span");
  span.className = "seg";
  span.dataset.idx = i;
  span.innerHTML = segHTML(seg.text, state.glossBySeg[i]); // 生词下划线 + 小字中文
  return span;
}

function rangeText(segs, a, b) {
  let t = "";
  for (let i = a; i <= b && i < segs.length; i++) t += (segs[i].text || "") + " ";
  return t.trim();
}

// 按章节 + 规则分段，得到段落列表（带起始时间戳和原始文本）
function computeParagraphs() {
  const segs = state.segments;
  const paras = [];
  const opts = state.subtitleMode ? SUB_OPTS : PARA_OPTS; // 字幕模式切得更碎
  const chs = state.chapters.length ? state.chapters : [{ start: 0, end: segs.length - 1 }];
  chs.forEach((ch, ci) => {
    const end = Math.min(ch.end, segs.length - 1);
    for (const [a, b] of paragraphBreaks(segs, ch.start, end, opts)) {
      paras.push({ ci, a, b, startSec: segs[a].start, raw: rangeText(segs, a, b) });
    }
  });
  return paras;
}

const _num = (v) => (typeof v === "number" ? v : null);
const _endsSentence = (t) => /[.!?。！？…”"]\s*$/.test((t || "").trim());

// 规则分段：把片段区间 [s,e] 切成多个"好读的段落"。
// 只用时间戳 + 标点这两个确定性信号，不做任何 AI 猜测。返回 [[a,b],...]。
function paragraphBreaks(segs, s, e, o = PARA_OPTS) {
  const out = [];
  let i = s;
  while (i <= e) {
    const startT = _num(segs[i].start);
    let j = i, chars = (segs[i].text || "").length;
    while (j < e) {
      const cur = segs[j], nxt = segs[j + 1];
      const curEnd = _num(cur.end) ?? _num(cur.start);
      const dur = startT == null || curEnd == null ? 0 : curEnd - startT;
      const gap = startT == null ? 0 : (_num(nxt.start) ?? curEnd ?? 0) - (curEnd ?? 0);
      const ends = _endsSentence(cur.text);
      const breakHere =
        (ends && (dur >= o.maxSec || chars >= o.maxChars || gap >= o.gapSec)) || // 句末 + 够长/有停顿
        (ends && dur >= o.hardSec) ||      // 句末 + 硬上限
        gap >= o.hardGap ||                // 明显长停顿，任何位置都断
        chars >= o.maxChars * 2;           // 无标点兜底，防止超长段
      if (breakHere) break;
      j++;
      chars += (segs[j].text || "").length;
    }
    out.push([i, j]);
    i = j + 1;
  }
  return out;
}

// 渲染：按段落输出；清洗模式显示 AI 清洗文本，原文模式显示原始片段
function renderTranscript() {
  const box = $("transcript");
  box.className = "transcript" + (state.subtitleMode ? " subtitle" : "");
  box.innerHTML = "";
  activeEl = null; // DOM 重建，旧高亮引用失效；跟随循环会重新打点
  const segs = state.segments;
  if (!segs.length) return;
  state.paragraphs = computeParagraphs();
  const hasChapters = state.chapters.length > 0;

  let curBlock = null, curCi = -1;
  state.paragraphs.forEach((p, pi) => {
    if (hasChapters && p.ci !== curCi) {
      curCi = p.ci;
      const ch = state.chapters[p.ci];
      curBlock = document.createElement("div");
      curBlock.className = "chap-block";
      curBlock.dataset.chapter = p.ci;
      const head = document.createElement("div");
      head.className = "chap-block-head";
      head.innerHTML =
        `<span class="chap-ts">${ch.start_ts}</span>` +
        `<span class="chap-title">${escapeHtml(ch.title)}</span>`;
      head.addEventListener("click", () => locateChapter(p.ci));
      curBlock.appendChild(head);
      box.appendChild(curBlock);
    }
    const container = curBlock || box;

    const para = document.createElement("div");
    para.className = "para";
    const gutter = document.createElement("div");
    gutter.className = "ts-gutter";
    gutter.textContent = fmtTs(p.startSec);
    para.appendChild(gutter);

    const text = document.createElement("div");
    text.className = "ptext";
    if (state.cleanMode && !state.subtitleMode) {
      const span = document.createElement("span");
      span.className = "seg";
      span.dataset.idx = p.a; // 选区仍可映射到该段起始片段
      const cleaned = state.cleanedMap[p.raw];
      span.textContent = cleaned || p.raw;
      if (!cleaned) span.classList.add("seg-pending");
      text.appendChild(span);
    } else {
      for (let i = p.a; i <= p.b && i < segs.length; i++) text.appendChild(makeSeg(segs[i], i));
    }
    para.appendChild(text);
    container.appendChild(para);

    if (state.translateMode || state.subtitleMode) { // 译文行，挂在该段/句下面
      const zh = document.createElement("div");
      zh.className = "para-zh";
      zh.dataset.pi = pi;
      const t = state.translatedMap[p.raw];
      zh.textContent = t || "翻译中…";
      if (!t) zh.classList.add("pending");
      container.appendChild(zh);
    }
  });
}

// 进度式 AI 清洗：逐章请求未清洗的段落，回填并重渲染
async function ensureCleaned() {
  const backend = $("backend-select").value || undefined;
  const byChapter = {};
  state.paragraphs.forEach((p) => { (byChapter[p.ci] = byChapter[p.ci] || []).push(p); });
  const cis = Object.keys(byChapter);
  let done = 0;
  for (const ci of cis) {
    const paras = byChapter[ci];
    const need = paras.filter((p) => !state.cleanedMap[p.raw]);
    done++;
    if (!need.length) continue;
    setStatus(`AI 清洗中… ${done}/${cis.length} 章`, "loading");
    try {
      const data = await postJSON("/api/clean", { doc_id: state.docId, backend, paragraphs: need.map((p) => p.raw) });
      (data.cleaned || []).forEach((c, i) => { state.cleanedMap[need[i].raw] = c; });
      if (state.cleanMode) renderTranscript();
    } catch (e) {
      setStatus("清洗失败：" + e.message, "error");
      return;
    }
  }
  setStatus("已清洗 ✓");
}

function setReadMode(clean) {
  state.cleanMode = clean;
  $("mode-raw").classList.toggle("active", !clean);
  $("mode-clean").classList.toggle("active", clean);
  renderTranscript();
  if (clean) ensureCleaned();
}

// 找到节点所属的 .seg 索引
function segIdxOf(node) {
  let el = node.nodeType === Node.TEXT_NODE ? node.parentElement : node;
  while (el && el !== document.body) {
    if (el.classList && el.classList.contains("seg")) {
      return parseInt(el.dataset.idx, 10);
    }
    el = el.parentElement;
  }
  return null;
}

// 选区 → 浮动按钮
document.addEventListener("mouseup", (e) => {
  // 点在提问框/按钮上时不处理
  if (e.target.closest("#ask-box") || e.target.closest("#ask-float")) return;

  const sel = window.getSelection();
  const text = sel.toString().trim();
  if (!text || !state.docId) {
    hideFloat();
    // 纯点击（无选区）落在某句字幕上 → 视频跳到该句
    if (!text && state.docId && $("transcript").contains(e.target)) {
      const i = segIdxOf(e.target);
      if (i != null) seekVideo(state.segments[i] && state.segments[i].start);
    }
    return;
  }
  // 选区必须落在 transcript 内
  if (!$("transcript").contains(sel.anchorNode)) {
    hideFloat();
    return;
  }
  let lo = segIdxOf(sel.anchorNode);
  let hi = segIdxOf(sel.focusNode);
  if (lo == null) lo = 0;
  if (hi == null) hi = lo;
  if (lo > hi) [lo, hi] = [hi, lo];

  state.selection = { text, segRange: [lo, hi] };

  const rect = sel.getRangeAt(0).getBoundingClientRect();
  const btn = $("ask-float");
  btn.hidden = false;
  btn.style.left = window.scrollX + rect.left + "px";
  btn.style.top = window.scrollY + rect.bottom + 8 + "px";
});

function hideFloat() {
  $("ask-float").hidden = true;
}

// 提问框
function openAskBox() {
  if (!state.selection) return;
  const box = $("ask-box");
  const btn = $("ask-float");
  $("ask-selected").textContent = state.selection.text;
  box.hidden = false;
  box.style.left = btn.style.left;
  box.style.top = btn.style.top;
  hideFloat();
  $("ask-input").value = "";
  $("ask-input").focus();
}

function closeAskBox() {
  $("ask-box").hidden = true;
}

// 公共问答核心：流式 + Markdown 渲染。选中提问和常驻追问都走这里
async function runAsk({ selectedText = "", segRange = null, question }) {
  if (!question) return;
  if (!state.docId) {
    setStatus("请先导入一个链接或文件。", "error");
    return;
  }
  const turn = addTurn(selectedText, question);
  const chat = $("chat");
  const backend = $("backend-select").value || undefined;
  let acc = "";
  try {
    const r = await fetch("/api/ask/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        doc_id: state.docId, question,
        selected_text: selectedText, seg_range: segRange, backend,
      }),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({ detail: r.statusText }));
      throw new Error(err.detail || "请求失败");
    }
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    const atBottom = () => chat.scrollHeight - chat.scrollTop - chat.clientHeight < 60;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!line) continue;
        let ev;
        try { ev = JSON.parse(line); } catch { continue; }
        if (ev.type === "delta") {
          acc += ev.text;
          const stick = atBottom();
          turn.answerEl.classList.remove("pending");
          turn.answerEl.innerHTML = mdRender(acc);
          if (stick) chat.scrollTop = chat.scrollHeight;
        } else if (ev.type === "error") {
          throw new Error(ev.error);
        }
      }
    }
    if (!acc) turn.answerEl.textContent = "(空回答)";
  } catch (e) {
    turn.answerEl.classList.remove("pending");
    turn.answerEl.textContent = "出错：" + e.message;
  }
}

// 选中提问
function send() {
  const question = $("ask-input").value.trim();
  if (!question || !state.selection) return;
  const sel = state.selection;
  closeAskBox();
  runAsk({ selectedText: sel.text, segRange: sel.segRange, question });
}

// 常驻追问，无需选中
function sendChat() {
  const ta = $("chat-input");
  const question = ta.value.trim();
  if (!question) return;
  ta.value = "";
  ta.style.height = "";  // 复位高度
  runAsk({ question });
}

// 对话渲染
function addTurn(selectedText, question) {
  const chat = $("chat");
  const turn = document.createElement("div");
  turn.className = "turn";

  if (selectedText) {
    const quote = document.createElement("div");
    quote.className = "quote";
    quote.textContent = selectedText;
    turn.appendChild(quote);
  }
  const q = document.createElement("div");
  q.className = "q";
  q.textContent = question;
  turn.appendChild(q);

  const a = document.createElement("div");
  a.className = "a md pending";
  a.textContent = "思考中…";
  turn.appendChild(a);

  chat.appendChild(turn);
  chat.scrollTop = chat.scrollHeight;
  return { answerEl: a };
}

function addChatDivider() {
  const d = document.createElement("div");
  d.className = "chat-divider";
  d.innerHTML = "<span>新对话</span>";
  $("chat").appendChild(d);
}

// 从落盘记录恢复整条对话流（含「新对话」分隔）
function renderChat(entries) {
  const chat = $("chat");
  chat.innerHTML = "";
  (entries || []).forEach((en) => {
    if (en.divider) { addChatDivider(); return; }
    const turn = addTurn(en.selected || "", en.question || "");
    turn.answerEl.classList.remove("pending");
    turn.answerEl.innerHTML = mdRender(en.answer || "");
  });
  chat.scrollTop = chat.scrollHeight;
}

// 事件绑定
$("url-btn").addEventListener("click", ingestUrl);
$("url-input").addEventListener("keydown", (e) => { if (e.key === "Enter") ingestUrl(); });
$("file-input").addEventListener("change", (e) => {
  if (e.target.files[0]) ingestFile(e.target.files[0]);
});
$("summary-refresh").addEventListener("click", () => { if (state.docId) fetchChapters(true); });
// 用户主动滚动字幕区 → 暂停视频跟随滚动 4s，避免被来回拽
$("content-pane").addEventListener("wheel", () => { userScrollUntil = Date.now() + 4000; }, { passive: true });
$("summary-body").addEventListener("wheel", () => { notesScrollUntil = Date.now() + 4000; }, { passive: true });

// 设置浮层（藏视频链接 + 引擎）
function toggleSettings(show) {
  const pop = $("settings-pop");
  pop.hidden = show === undefined ? !pop.hidden : !show;
  if (!pop.hidden) $("url-input").focus();
}
$("settings-btn").addEventListener("click", (e) => { e.stopPropagation(); toggleSettings(); });
document.addEventListener("mousedown", (e) => {
  if (!$("settings-pop").hidden && !e.target.closest("#settings-pop") && !e.target.closest("#settings-btn")) {
    toggleSettings(false);
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("settings-pop").hidden) toggleSettings(false);
});

// 自有视频控制条：播放/暂停 + 拖动进度
function togglePlay() {
  if (!player || !playerReady) return;
  if (player.getPlayerState() === YT.PlayerState.PLAYING) player.pauseVideo();
  else player.playVideo();
}
$("vid-play").addEventListener("click", togglePlay);
(() => {
  const seek = $("vid-seek");
  const begin = () => { vidSeeking = true; };
  const end = () => { vidSeeking = false; savePos(); };
  seek.addEventListener("mousedown", begin);
  seek.addEventListener("touchstart", begin, { passive: true });
  seek.addEventListener("input", () => {
    const v = parseFloat(seek.value);
    $("vid-cur").textContent = fmtTs(v);
    if (player && playerReady) player.seekTo(v, true); // 拖动时只定位，不强制播放
  });
  seek.addEventListener("change", end);
  seek.addEventListener("mouseup", end);
  seek.addEventListener("touchend", end);
})();

// 倍速：下拉选择 + Shift+> / Shift+< 步进（与 YouTube 一致）
$("vid-rate").addEventListener("change", (e) => applyRate(parseFloat(e.target.value)));
document.addEventListener("keydown", (e) => {
  if (!e.shiftKey || !player || !playerReady) return;
  const t = e.target;
  if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
  const up = e.key === ">" || e.key === ".";
  const down = e.key === "<" || e.key === ",";
  if (!up && !down) return;
  e.preventDefault();
  const cur = player.getPlaybackRate ? player.getPlaybackRate() : 1;
  let idx = 0, bestd = Infinity;
  RATES.forEach((r, k) => { const d = Math.abs(r - cur); if (d < bestd) { bestd = d; idx = k; } });
  applyRate(RATES[Math.max(0, Math.min(RATES.length - 1, idx + (up ? 1 : -1)))]);
});

// 点视频后焦点会进 YouTube iframe、吃掉键盘事件 → 立刻收回页面，保证快捷键始终有效
window.addEventListener("blur", () => {
  setTimeout(() => {
    const a = document.activeElement;
    if (a && a.tagName === "IFRAME" && a.closest("#video-pane")) a.blur();
  }, 0);
});

// 整段中文翻译开关
$("translate-toggle").addEventListener("change", (e) => {
  state.translateMode = e.target.checked;
  LS.set("tq.translate", e.target.checked ? "1" : "0"); // 开关状态存本地
  renderTranscript();
  if (state.translateMode || state.subtitleMode) ensureTranslated();
});
$("subtitle-toggle").addEventListener("change", (e) => {
  state.subtitleMode = e.target.checked;
  LS.set("tq.subtitle", e.target.checked ? "1" : "0");
  renderTranscript(); // 切换粒度（逐句 ↔ 段落）
  if (state.subtitleMode || state.translateMode) ensureTranslated();
});
$("hide-video-toggle").addEventListener("change", (e) => {
  LS.set("tq.hidevideo", e.target.checked ? "1" : "0");
  $("video-pane").classList.toggle("user-hidden", e.target.checked);
});
$("notes-export").addEventListener("click", exportNotesMd);

// 把章节笔记拼成 Markdown 下载
function exportNotesMd() {
  if (!state.chapters.length) { setStatus("还没有笔记可导出", "error"); return; }
  let md = `# ${document.title || "notes"}\n\n`;
  state.chapters.forEach((ch) => {
    md += `## ${ch.start_ts ? `[${ch.start_ts}] ` : ""}${ch.title}\n\n`;
    if (ch.gist) md += `> ${ch.gist}\n\n`;
    (ch.points || []).forEach((p) => {
      md += `- ${p.text}${p.ts ? ` _(${p.ts})_` : ""}\n`;
      (p.details || []).forEach((d) => { md += `  - ${d.text}\n`; });
    });
    md += "\n";
  });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([md], { type: "text/markdown" }));
  a.download = `notes-${state.videoId || "doc"}.md`;
  a.click();
  URL.revokeObjectURL(a.href);
}

// 空格键控制视频播放/暂停（在输入框里打字时不拦截）
document.addEventListener("keydown", (e) => {
  if (e.code !== "Space") return;
  const t = e.target;
  if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
  if (!player || !playerReady) return;
  e.preventDefault(); // 阻止默认的翻页滚动
  if (player.getPlayerState() === YT.PlayerState.PLAYING) player.pauseVideo();
  else player.playVideo();
});

// 左右方向键 → 快退 / 快进 5 秒
document.addEventListener("keydown", (e) => {
  if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
  const t = e.target;
  if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
  if (!player || !playerReady || !player.getCurrentTime) return;
  e.preventDefault();
  const dur = player.getDuration ? player.getDuration() : 0;
  let to = player.getCurrentTime() + (e.key === "ArrowLeft" ? -5 : 5);
  to = Math.max(0, dur ? Math.min(to, dur) : to);
  player.seekTo(to, true);
  updateVidCtrl(to); // 暂停时也立刻更新控制条
  savePos();
});

// 选中字幕后按 Enter → 直接讲解（无需输入问题）
const EXPLAIN_Q = "解释一下我选中的这段：它在讲什么？如果有生词、术语、人名或文化背景，也一并讲清楚。";
document.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || e.shiftKey || e.isComposing) return;
  const t = e.target;
  if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
  if (!state.selection || !$("ask-box").hidden) return;
  e.preventDefault();
  const sel = state.selection;
  hideFloat();
  runAsk({ selectedText: sel.text, segRange: sel.segRange, question: EXPLAIN_Q });
  state.selection = null;
  window.getSelection().removeAllRanges();
});
$("mode-raw").addEventListener("click", () => setReadMode(false));
$("mode-clean").addEventListener("click", () => setReadMode(true));
$("ask-float").addEventListener("mousedown", (e) => { e.preventDefault(); openAskBox(); });
$("ask-send").addEventListener("click", send);
$("ask-cancel").addEventListener("click", closeAskBox);
$("ask-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
    e.preventDefault();
    send();
  }
  if (e.key === "Escape") closeAskBox();
});
// 新对话：清空后端会话（下一问重开全新 session，不背旧缓存），界面保留历史 + 分隔线
$("new-session").addEventListener("click", async () => {
  if (!state.docId) return;
  try {
    await postJSON("/api/session/new", { doc_id: state.docId });
    addChatDivider();
    setStatus("已开新对话 ✓");
  } catch (e) {
    setStatus("新对话失败：" + e.message, "error");
  }
});

// 常驻追问框
$("chat-send").addEventListener("click", sendChat);
$("chat-input").addEventListener("keydown", (e) => {
  // 回车发送；Shift+Enter 换行；输入法组合中(isComposing/229)不触发
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
    e.preventDefault();
    sendChat();
  }
});
$("chat-input").addEventListener("input", (e) => {
  e.target.style.height = "auto";
  e.target.style.height = Math.min(e.target.scrollHeight, 200) + "px";
});
// 点空白处收起提问框
document.addEventListener("mousedown", (e) => {
  if (!e.target.closest("#ask-box") && !e.target.closest("#ask-float")) {
    if (!$("ask-box").hidden) closeAskBox();
  }
});

// ── 可拖拽边距：列宽 + 视频高度（尺寸存 localStorage，刷新保持）──
const LS = {
  get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} },
};

function setColWidth(target, w) {
  target.style.flex = "0 0 auto";
  target.style.width = w + "px";
  if (target.id === "chat-pane") target.style.maxWidth = "none";
}

function initColResize(handleId, target, side, min, max, lsKey) {
  $(handleId).addEventListener("mousedown", (e) => {
    e.preventDefault();
    const startX = e.clientX, startW = target.getBoundingClientRect().width;
    $(handleId).classList.add("dragging");
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    let last = startW;
    const move = (ev) => {
      const dx = ev.clientX - startX;
      last = Math.max(min, Math.min(max, side === "left" ? startW + dx : startW - dx));
      setColWidth(target, last);
    };
    const up = () => {
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
      $(handleId).classList.remove("dragging");
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      LS.set(lsKey, Math.round(last)); // 拖完即存
    };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  });
}
initColResize("rz-left", $("sidebar"), "left", 220, 560, "tq.sidebarW");
initColResize("rz-right", $("chat-pane"), "right", 280, 680, "tq.chatW");

// 视频高度：往下拖 → 变高（CSS 用 --video-h，宽度按 16:9 跟随，封顶后变信箱式）
$("video-resize").addEventListener("mousedown", (e) => {
  e.preventDefault();
  const startY = e.clientY;
  const startH = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--video-h")) || 240;
  document.body.style.cursor = "row-resize";
  document.body.style.userSelect = "none";
  let last = startH;
  const move = (ev) => {
    last = Math.max(140, Math.min(560, startH + (ev.clientY - startY)));
    document.documentElement.style.setProperty("--video-h", last + "px");
  };
  const up = () => {
    document.removeEventListener("mousemove", move);
    document.removeEventListener("mouseup", up);
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
    LS.set("tq.videoH", Math.round(last)); // 拖完即存
  };
  document.addEventListener("mousemove", move);
  document.addEventListener("mouseup", up);
});

// 启动恢复上次拖动的尺寸
(function applySavedLayout() {
  const sw = LS.get("tq.sidebarW"); if (sw) setColWidth($("sidebar"), parseFloat(sw));
  const cw = LS.get("tq.chatW"); if (cw) setColWidth($("chat-pane"), parseFloat(cw));
  const vh = LS.get("tq.videoH"); if (vh) document.documentElement.style.setProperty("--video-h", parseFloat(vh) + "px");
})();

// 调试期：写死常用视频，刷新即自动载入（走缓存秒开）。调完删掉这一段。
const DEBUG_URL = "https://www.youtube.com/watch?v=SQ3fZ1sAqXI";
if (DEBUG_URL) {
  $("url-input").value = DEBUG_URL;
  ingestUrl();
}
