"""可插拔 LLM 后端。

核心思路：不调计费 API，而是 subprocess 调用一个**已用订阅登录的本地 CLI 的 headless 模式**。
- ClaudeCLIBackend：`claude -p ... --output-format json`，走 Claude Pro 订阅，零 API 花费。
  多轮靠 `--resume <session_id>` 续接同一会话（transcript 只在首轮喂一次）。
- CodexCLIBackend：预留（本机未装 codex），`codex exec` / `codex exec resume`。

选择由环境变量 LLM_BACKEND 决定，默认 claude。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Protocol

# 单次问答超时（秒）。说话人识别要对全篇逐段判断、输出很长，可能要几分钟，给足。
TIMEOUT_S = int(os.environ.get("LLM_TIMEOUT_S", "420"))

# 默认模型：Sonnet 4.6（质量够、比 Opus 快且省额度）。
# 可用 CLAUDE_MODEL 覆盖，接受别名（sonnet/opus/haiku）或完整 id（claude-sonnet-4-6 等）。
DEFAULT_CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")


class LLMError(Exception):
    """对用户可见的 LLM 调用失败。"""


class LLMBackend(Protocol):
    name: str

    def ask(self, prompt: str, session_id: str | None) -> tuple[str, str]:
        """返回 (answer, session_id)。session_id 为空表示开新会话。"""
        ...


class ClaudeCLIBackend:
    name = "claude"

    def __init__(self, model: str | None = None) -> None:
        self.bin = shutil.which("claude")
        if not self.bin:
            raise LLMError("找不到 `claude` 命令，请确认已安装并登录 Claude Code。")
        self.model = model or DEFAULT_CLAUDE_MODEL

    def ask(self, prompt: str, session_id: str | None) -> tuple[str, str]:
        cmd = [self.bin, "-p", prompt, "--output-format", "json"]
        if self.model:
            cmd += ["--model", self.model]
        if session_id:
            cmd += ["--resume", session_id]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as e:
            raise LLMError(f"claude 调用超时（>{TIMEOUT_S}s）。") from e

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

    def ask_stream(self, prompt: str, session_id: str | None):
        """流式生成。yield ("text", 增量) 若干次，最后 yield ("session", id) 或 ("error", msg)。"""
        cmd = [self.bin, "-p", prompt, "--output-format", "stream-json",
               "--include-partial-messages", "--verbose"]
        if self.model:
            cmd += ["--model", self.model]
        if session_id:
            cmd += ["--resume", session_id]

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
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

    def ask(self, prompt: str, session_id: str | None) -> tuple[str, str]:
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
