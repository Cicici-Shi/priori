"""可插拔 LLM 后端。

核心思路：不调计费 API，而是 subprocess 调用一个**已用订阅登录的本地 CLI 的 headless 模式**。
- ClaudeCLIBackend：`claude -p ... --output-format json`，走 Claude Pro 订阅，零 API 花费。
  多轮靠 `--resume <session_id>` 续接同一会话（transcript 只在首轮喂一次）。
- CodexCLIBackend：预留（本机未装 codex），`codex exec` / `codex exec resume`。

选择由环境变量 LLM_BACKEND 决定，默认 claude。
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import threading
from typing import Protocol

# 单次问答超时（秒）。说话人识别要对全篇逐段判断、输出很长，可能要几分钟，给足。
TIMEOUT_S = int(os.environ.get("LLM_TIMEOUT_S", "420"))

# claude 订阅版 headless 基本是单车道：并发 subprocess 会互相拖死（各自重载 ~25k token 上下文、
# 抢订阅额度），实测 3~4 个并发就全部超时/502。故全局闸门限制同时在跑的 `claude -p`——
# 无论前端开几个 worker、几个 tab，都在这里排队。默认 1 车道，可用 LLM_MAX_CONCURRENCY 调。
class _PriorityGate:
    """单车道闸门 + 优先级。空闲即放行；被占用时，释放瞬间**优先唤醒高优先级等待者**，
    让翻译（尤其用户正在看的那页字幕）插到 summary/笔记前面。绝不打断在跑的调用——
    只在下一次放行时插队。high 的放行条件只看有没有空位；low 还得额外等到没有 high 在排队。"""

    def __init__(self, slots: int = 1) -> None:
        self._cond = threading.Condition()
        self._free = slots
        self._hi_waiting = 0  # 正在排队的高优先级数量

    def acquire(self, high: bool = False) -> None:
        with self._cond:
            if high:
                self._hi_waiting += 1
            try:
                while not (self._free > 0 and (high or self._hi_waiting == 0)):
                    self._cond.wait()
                self._free -= 1
            finally:
                if high:
                    self._hi_waiting -= 1

    def release(self) -> None:
        with self._cond:
            self._free += 1
            self._cond.notify_all()


_CLAUDE_GATE = _PriorityGate(int(os.environ.get("LLM_MAX_CONCURRENCY", "1")))

# 默认模型：Sonnet 4.6（质量够、比 Opus 快且省额度）。
# 可用 CLAUDE_MODEL 覆盖，接受别名（sonnet/opus/haiku）或完整 id（claude-sonnet-4-6 等）。
DEFAULT_CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")


class LLMError(Exception):
    """对用户可见的 LLM 调用失败。"""


# 一张待喂给模型的图：(原始字节, media_type)。带图时走 stream-json 输入，图作为真正的
# image content block 直接进模型视野——不落盘、不靠"让模型自己去 Read 本地文件"。
Image = tuple[bytes, str]


def _stream_input_message(prompt: str, images: list[Image]) -> str:
    """构造 `--input-format stream-json` 要吃的一条 user message（text + image blocks）。"""
    content: list[dict] = [{"type": "text", "text": prompt}]
    for data, media_type in images:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type,
                       "data": base64.b64encode(data).decode()},
        })
    return json.dumps({"type": "user", "message": {"role": "user", "content": content}})


class LLMBackend(Protocol):
    name: str

    def ask(self, prompt: str, session_id: str | None,
            images: list[Image] | None = None,
            high_priority: bool = False) -> tuple[str, str]:
        """返回 (answer, session_id)。session_id 为空表示开新会话。images 非空时随图提问。
        high_priority=True 时在单车道闸门里插队（翻译用，优先于 summary/笔记）。"""
        ...


class ClaudeCLIBackend:
    name = "claude"

    def __init__(self, model: str | None = None) -> None:
        self.bin = shutil.which("claude")
        if not self.bin:
            raise LLMError("找不到 `claude` 命令，请确认已安装并登录 Claude Code。")
        self.model = model or DEFAULT_CLAUDE_MODEL

    def ask(self, prompt: str, session_id: str | None,
            images: list[Image] | None = None,
            high_priority: bool = False) -> tuple[str, str]:
        # 带图：走 stream-json 输入把图当真图喂进去，复用流式路径累积成整段答案。
        if images:
            acc, new_session = [], session_id or ""
            for kind, val in self.ask_stream(prompt, session_id, images):
                if kind == "text":
                    acc.append(val)
                elif kind == "session":
                    new_session = val or new_session
                elif kind == "error":
                    raise LLMError(val)
            answer = "".join(acc).strip()
            if not answer:
                raise LLMError("claude 返回了空答案。")
            return answer, new_session

        cmd = [self.bin, "-p", prompt, "--output-format", "json"]
        if self.model:
            cmd += ["--model", self.model]
        if session_id:
            cmd += ["--resume", session_id]
        # 全局排队：同一时刻只放行 LLM_MAX_CONCURRENCY 个 claude 进程；high 的能插队到 low 前面
        _CLAUDE_GATE.acquire(high=high_priority)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as e:
            raise LLMError(f"claude 调用超时（>{TIMEOUT_S}s）。") from e
        finally:
            _CLAUDE_GATE.release()

        if proc.returncode != 0:
            raise LLMError(f"claude 调用失败（exit {proc.returncode}）：{proc.stderr.strip()[:500]}")

        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise LLMError(f"无法解析 claude 输出：{proc.stdout[:300]}") from e

        if data.get("is_error"):
            raise LLMError(f"claude 返回错误：{data.get('result', '')[:500]}")

        answer = (data.get("result") or "").strip()
        new_session = data.get("session_id") or session_id or ""
        if not answer:
            raise LLMError("claude 返回了空答案。")
        return answer, new_session

    def ask_stream(self, prompt: str, session_id: str | None,
                   images: list[Image] | None = None):
        """流式生成。yield ("text", 增量) 若干次，最后 yield ("session", id) 或 ("error", msg)。

        images 非空时，prompt+图作为一条 user message 从 stdin 走 stream-json 输入
        （`--input-format stream-json` 必须搭 `--output-format stream-json`）。
        """
        if images:
            cmd = [self.bin, "-p", "--input-format", "stream-json",
                   "--output-format", "stream-json", "--include-partial-messages", "--verbose"]
        else:
            cmd = [self.bin, "-p", prompt, "--output-format", "stream-json",
                   "--include-partial-messages", "--verbose"]
        if self.model:
            cmd += ["--model", self.model]
        if session_id:
            cmd += ["--resume", session_id]

        stdin = subprocess.PIPE if images else None
        proc = subprocess.Popen(cmd, stdin=stdin, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        if images:
            proc.stdin.write(_stream_input_message(prompt, images))
            proc.stdin.close()  # EOF → CLI 处理这条消息
        new_session = session_id or ""
        got_text = False
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("session_id"):
                    new_session = ev["session_id"]
                etype = ev.get("type")
                if etype == "stream_event":
                    e = ev.get("event", {})
                    if e.get("type") == "content_block_delta":
                        d = e.get("delta", {})
                        if d.get("type") == "text_delta" and d.get("text"):
                            got_text = True
                            yield ("text", d["text"])
                elif etype == "result" and ev.get("is_error"):
                    yield ("error", (ev.get("result") or "claude 返回错误")[:500])
                    return
            proc.wait(timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()
        if proc.returncode not in (0, None) and not got_text:
            err = (proc.stderr.read() or "").strip()[:500] if proc.stderr else ""
            yield ("error", f"claude 调用失败（exit {proc.returncode}）：{err}")
            return
        yield ("session", new_session)


class CodexCLIBackend:
    name = "codex"

    def __init__(self) -> None:
        self.bin = shutil.which("codex")
        if not self.bin:
            raise LLMError("找不到 `codex` 命令。安装 Codex CLI 并用 ChatGPT 账号登录后可用。")

    def ask(self, prompt: str, session_id: str | None,
            images: list[Image] | None = None,
            high_priority: bool = False) -> tuple[str, str]:
        if images:
            raise LLMError("codex 引擎暂不支持就图片提问，请切到 claude 引擎。")
        # 预留实现：codex exec [resume <id>] --json。具体字段以装上后的版本为准。
        if session_id:
            cmd = [self.bin, "exec", "resume", session_id, prompt, "--json"]
        else:
            cmd = [self.bin, "exec", prompt, "--json"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired as e:
            raise LLMError(f"codex 调用超时（>{TIMEOUT_S}s）。") from e
        if proc.returncode != 0:
            raise LLMError(f"codex 调用失败：{proc.stderr.strip()[:500]}")
        # codex 的 JSON 是逐行事件流；取最后一条带文本/会话的事件。
        answer, sess = "", session_id or ""
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            sess = ev.get("session_id") or ev.get("thread_id") or sess
            for key in ("text", "message", "result", "content"):
                if isinstance(ev.get(key), str) and ev[key].strip():
                    answer = ev[key].strip()
        if not answer:
            raise LLMError("codex 未返回可解析的答案（预留实现，可能需按实际版本调整）。")
        return answer, sess


_BACKENDS = {"claude": ClaudeCLIBackend, "codex": CodexCLIBackend}


def get_backend(name: str | None = None, model: str | None = None) -> LLMBackend:
    name = (name or os.environ.get("LLM_BACKEND") or "claude").lower()
    cls = _BACKENDS.get(name)
    if cls is None:
        raise LLMError(f"未知 LLM_BACKEND: {name}（可选 claude / codex）。")
    if name == "claude":
        return cls(model=model)
    return cls()
