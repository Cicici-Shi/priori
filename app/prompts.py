"""提示构造。

设计要点：
- 首轮把整篇 transcript 喂进会话（连同系统指令），让模型有完整上下文。
- 追问只发"这次选中的片段 + 问题"，transcript 已在会话上下文里，不重发 → 省额度、更连贯。
- 始终要求用中文讲解；可以引用时间戳定位。
"""

from __future__ import annotations

from typing import Any

SYSTEM = """你是一个帮助用户看懂视频/音频字幕的助手。用户在读一篇 transcript，会选中其中某一段并就这段提问。

要求：
- 始终用**简体中文**回答。
- 当用户选中的是英文时，先解释这句的意思，再补充必要的背景知识（人名、产品、术语、梗、上下文）。
- 回答要紧扣用户选中的那一段，结合整篇 transcript 的上下文，但不要泛泛复述全文。
- 可以引用时间戳（如 [12:34]）帮助用户定位。
- 简洁清楚，必要时用要点列出。"""


_CHAPTERS_FORMAT = """**输出格式**：每个章节占一行，用竖线 `|` 分隔 4 个字段，顺序固定：

起始序号 | 结束序号 | 章节标题 | 一句话概括

- 起始/结束序号：用 transcript 每行最前面的那个数字序号。
- 章节标题：简短中文小标题（6–16 字）。
- 一句话概括：1–3 句中文，**详略适中**，能让人不看原文也大致明白，但不要逐句复述；**必须写在同一行内，不要换行**。

只输出这些行，**不要**表头、编号、JSON、代码块或任何额外说明。示例（仅示意格式）：

0 | 13 | 开场回顾 | 两位主持人回顾 Claude Code 刚上线时的样子，感慨一年来的巨变。
14 | 65 | Agent 树与验证 | 介绍现在的 Agent 树工作模式，并澄清 Agent 语境下"验证"的真正含义。"""


def chapters_instruction(segments: list[dict[str, Any]]) -> str:
    """按时长动态决定章节数：约每 4–5 分钟一章，避免长视频被压成一两个大章。"""
    n = len(segments)
    last = segments[-1] if segments else {}
    dur_s = last.get("end") or last.get("start") or 0
    # 无时间戳来源（.txt）兜底：粗估每 8 段约 1 分钟。
    dur_min = (dur_s / 60) if dur_s else (n / 8)
    target = max(4, min(round(dur_min / 4.5), 30))
    lo, hi = max(4, target - 2), target + 2
    return (
        "请把这篇 transcript 按主题切分成若干**章节**：把讲同一件事的连续片段归为一章。\n\n"
        f"分成 **{lo}–{hi} 个章节**（全片约 {round(dur_min)} 分钟，平均每章 4–5 分钟；"
        "**务必覆盖到最后一个片段，不要把大段后半程内容压进同一章**）。"
        "每章覆盖一段**连续的片段序号**；章节之间首尾相接、不重叠、不留空"
        "（第一章起始序号为 0，最后一章结束序号为最后一个片段的序号，序号连续衔接）。\n\n"
        + _CHAPTERS_FORMAT
    )


SPEAKERS_INSTRUCTION = """这是一段（可能多人的）对话/访谈 transcript。请根据内容**推断每一段是谁说的**，按"说话人轮次"切分：连续属于同一说话人的片段合为一轮。

- 如果能从对话里自信地认出说话人的**真实姓名或角色**（比如互相称呼、自我介绍、第三方提到名字），就用真实姓名；否则用"说话人A""说话人B"，并保持同一个人标签**始终一致**。
- 必须覆盖全部片段，轮次之间首尾相接、不重叠、不留空（第一轮从 0 开始，最后一轮到最后一个序号）。
- 每个轮次输出一行，用竖线 `|` 分隔 3 个字段，顺序固定：

起始序号 | 结束序号 | 说话人

只输出这些行，**不要**表头、编号、JSON、代码块或任何额外说明。示例（仅示意格式）：

0 | 9 | Cat Wu
10 | 14 | Boris"""


CLEAN_INSTRUCTION = """下面是英文口语转写的若干段落，每段以 [[编号]] 开头。请逐段做"清洗"，只为提升可读性：

- 去掉口水词和语气填充：um / uh / like（作语气词时）/ you know / I mean / sort of / kind of / 重复起头 / 明显口误与重述。
- 修正断句、大小写、标点，让句子完整通顺。
- **必须保持英文原文、保持原意与说话风格**：不要翻译、不要改写措辞、不要增删信息、不要合并或拆分段落、不要加任何解释。
- 当某段作语气词的 like 也是有意义的动词/比较（I like it / looks like / just like 等）时，保留。

输出：对每一段，另起一段、以 `[[原编号]]` 开头，后跟清洗后的英文文本。只输出这些段落。"""


TRANSLATE_INSTRUCTION = """下面是英文转写的若干段落，每段以 [[编号]] 开头。请把每一段**通顺地翻译成简体中文**：

- 忠实原意、口语自然，别逐字硬翻。
- 人名 / 产品名 / 技术术语可保留英文（如 GPT-4 / Transformer / Stanford）。
- 不要解释、不要加注。

**严格 1:1，不许合并**：输入有几段就输出几段，**每个 `[[编号]]` 必须原样出现且各自单独成段**，编号一一对应、不许跳号或改号。即使相邻两段是同一句话被切开的（如引号里的连续问句），也**分别翻译、分别用各自编号输出**，绝不并到一段里。短到只有一两个词也照样单独输出。

输出：对每一段，另起一段、以 `[[原编号]]` 开头，后跟该段简体中文翻译。只输出这些段落。"""


GLOSSARY_INSTRUCTION = """下面是某一章英文 transcript 片段（每行：序号 [时间戳] 文本）。请挑出其中**中等英语水平的中国学习者可能不认识**的词或短语：习语、俚语、不常见词、动词短语（phrasal verb）、专业术语。

- 跳过常见词，跳过纯人名 / 地名 / 机构名。
- 每个给出在**本句语境下**的简短中文意思（尽量短，几个字）。
- 本章最多挑 8 个，挑最值得标的。
- 英文原词要和原文**大小写一致**、能在该句里原样找到。

只输出这些行，每行一个，竖线分隔 3 个字段，不要表头 / 编号 / 代码块 / 解释：

序号 | 英文原词 | 中文意思

示例：
44 | tears up | （激动得）热泪盈眶
52 | proprietary | 专有的、私有的"""


def glossary_prompt(segments: list[dict[str, Any]], start: int, end: int) -> str:
    """单章生词 prompt：只喂这一章的片段。"""
    end = min(end, len(segments) - 1)
    lines = []
    for i in range(start, end + 1):
        s = segments[i]
        ts = _fmt_ts(s.get("start"))
        prefix = f"[{ts}] " if ts else ""
        lines.append(f"{i} {prefix}{s['text']}")
    return f"{GLOSSARY_INSTRUCTION}\n\ntranscript：\n" + "\n".join(lines)


NOTES_INSTRUCTION = """你在为一段课程做「重读即可回忆」的笔记。下面是某一章的 transcript 片段（每行：序号 [时间戳] 文本）。

产出（只输出下面这些行，不要任何解释 / 表头 / 编号 / 代码块）：
- 第一行写主旨，格式： > 序号 | 一句话说清这章在讲什么（不超过 30 字）
- 然后 3~5 条要点，每条一行，格式： 序号 | 要点
- 若某条要点有关键的例子 / 数字 / 反例，可另起一行挂在它下方，**行首加两个空格**： 序号 | 细节

每条要点的硬性要求：
- 写成**完整断言句**，让人不看原文也能想起这件事；【禁止】只写关键词短语。
- 用 **…** 标出这条里最关键的一个术语 / 数字 / 结论，一条只标一处。
- 有因果 / 对比 / 动机就写出来（…所以… / …而不是…）。
- 保留具体数字、人名、例子；删掉寒暄、口水、过渡。
- 行首序号 = 这条要点最贴合的那个片段序号，用于点击跳转。
- 全部用简体中文。"""


def notes_prompt(segments: list[dict[str, Any]], start: int, end: int) -> str:
    """单章笔记 prompt：只喂这一章的片段（聚焦、稳定，且总量约等于喂一遍全文）。"""
    end = min(end, len(segments) - 1)
    lines = []
    for i in range(start, end + 1):
        s = segments[i]
        ts = _fmt_ts(s.get("start"))
        prefix = f"[{ts}] " if ts else ""
        lines.append(f"{i} {prefix}{s['text']}")
    return f"{NOTES_INSTRUCTION}\n\ntranscript：\n" + "\n".join(lines)


def speakers_q(seg_range: list[int] | None = None) -> str:
    """说话人识别的问题文本。给定区间则只标该区间（用于分章节渐进识别）。"""
    if not seg_range:
        return SPEAKERS_INSTRUCTION
    s, e = seg_range
    return (
        f"现在只针对**序号 {s} 到 {e}** 之间的片段，推断每段是谁说的，按说话人轮次切分。\n"
        f"- 必须覆盖 {s}–{e} 全部片段，轮次之间首尾相接、不重叠不留空（第一轮从 {s} 开始，最后一轮到 {e}）。\n"
        f"- **同一个说话人请始终用同一个标签**；如果你在本对话前面已经给某人起过名字/编号，请沿用，保持全片一致。\n"
        f"- 每个轮次一行，竖线分隔：起始序号 | 结束序号 | 说话人。\n"
        f"只输出这些行，不要表头/编号/JSON/代码块/解释。"
    )


def _fmt_ts(sec: Any) -> str:
    if sec is None:
        return ""
    sec = int(float(sec))
    return f"{sec // 60:02d}:{sec % 60:02d}"


def render_transcript(segments: list[dict[str, Any]]) -> str:
    lines = []
    for i, s in enumerate(segments):
        ts = _fmt_ts(s.get("start"))
        prefix = f"[{ts}] " if ts else ""
        lines.append(f"{i}\t{prefix}{s['text']}")
    return "\n".join(lines)


def _selection_block(selected: str, seg_range: list[int] | None, segments: list[dict]) -> str:
    loc = ""
    if seg_range and len(seg_range) == 2:
        lo, hi = seg_range
        start_ts = _fmt_ts(segments[lo]["start"]) if 0 <= lo < len(segments) else ""
        loc = f"（位于片段 {lo}–{hi}" + (f"，约 [{start_ts}]" if start_ts else "") + "）"
    return f'用户选中的片段{loc}：\n"""\n{selected}\n"""'


def first_turn(segments: list[dict], selected: str, question: str,
               seg_range: list[int] | None) -> str:
    """首轮：系统指令 + 整篇 transcript +（可选）选中片段 + 问题。"""
    sel_part = (
        f"{_selection_block(selected, seg_range, segments)}\n\n"
        if selected else ""
    )
    return (
        f"{SYSTEM}\n\n"
        f"以下是完整的 transcript（每行：序号<TAB>[时间戳] 文本）：\n"
        f"<transcript>\n{render_transcript(segments)}\n</transcript>\n\n"
        f"{sel_part}"
        f"用户的问题：{question}"
    )


def follow_up(selected: str, question: str, seg_range: list[int] | None,
              segments: list[dict]) -> str:
    """追问：transcript 已在会话里，只发新选中片段 + 问题。"""
    if selected:
        return f"{_selection_block(selected, seg_range, segments)}\n\n用户的问题：{question}"
    return f"用户的问题（针对整篇 transcript）：{question}"
