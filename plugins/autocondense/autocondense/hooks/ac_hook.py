"""autocondense 薄钩子脚本（ZCode / Claude Code hooks 协议）。

用法（宿主 hooks 配置，installer.py 可自动写入）::

    python /绝对路径/autocondense/hooks/ac_hook.py <EventName>

EventName ∈ PostToolUse | Stop | SessionStart | PreCompact | PostToolUseFailure。

行为：
- 读 stdin 原始字节并 UTF-8 容错解码（Windows 控制台编码坑），解析 JSON 提取
  session_id / cwd / transcript_path / source / tool_name / tool_input /
  context_tokens，转发 POST {base}/events/hook（3s 超时，trust_env=False）。
- SessionStart：响应含 additional_context 时输出 {"additionalContext": 文本}
  （截到 9000 字符；截断时末尾附「完整快照：{full_file}，需要时用 Read 读取」）。
- Stop：响应 should != "NO" 且带 instruction 时输出
  {"decision":"block","reason": instruction}，带 15 分钟闩锁防死循环
  （data/hook-latch/{session_id}.json 记 last_block_ts，窗口内已 block 过则不输出）。
- PostToolUse / PreCompact / PostToolUseFailure：无输出。
- 服务不在线/任何异常：静默 exit 0（绝不干扰宿主），stderr 日志一行到
  %TEMP%/autocondense_hook.log。

base_url 取 env AUTOCONDENSE_BASE_URL，否则 http://127.0.0.1:8761。
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

# 脚本方式运行时保证 autocondense 包可导入（-m 方式下该插入是无害的冗余）
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from autocondense.config import atomic_write_text, resolve_data_dir  # noqa: E402

HOOK_TIMEOUT_S = 3.0
LATCH_DIR_NAME = "hook-latch"
LATCH_WINDOW_S = 15 * 60  # 15 分钟内同一会话只 block 一次
MAX_ADDITIONAL_CONTEXT = 9000
_HOOK_FIELDS = ("session_id", "cwd", "transcript_path", "source",
                "tool_name", "tool_input", "context_tokens")


def _log(event: str, msg: str) -> None:
    """stderr 日志一行到 %TEMP%/autocondense_hook.log（尽力而为）。"""
    try:
        path = Path(tempfile.gettempdir()) / "autocondense_hook.log"
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {event} {msg}\n")
    except OSError:
        pass


def _emit(obj: Dict[str, Any]) -> None:
    """按宿主 hooks stdout 协议输出纯 JSON 一行（UTF-8 字节直写，绕开控制台编码）。"""
    data = json.dumps(obj, ensure_ascii=False)
    sys.stdout.buffer.write(data.encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def _call_service(base_url: str, event: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """转发事件到常驻服务；失败返回 None（调用方静默退出）。"""
    import httpx  # 延迟导入：钩子进程启动路径保持最薄

    payload: Dict[str, Any] = {"event": event}
    for key in _HOOK_FIELDS:
        value = data.get(key)
        if value is not None:
            payload[key] = value
    try:
        r = httpx.post(f"{base_url.rstrip('/')}/events/hook", json=payload,
                       timeout=HOOK_TIMEOUT_S, trust_env=False)
        r.raise_for_status()
        body = r.json()
        return body if isinstance(body, dict) else None
    except Exception as e:  # 铁律：绝不干扰宿主
        _log(event, f"调用服务失败：{type(e).__name__}: {e}")
        return None


# ---------------------------------------------------------------------------
# Stop 闩锁：15 分钟内同一会话最多 block 一次，防「block→重试→再 block」死循环
# ---------------------------------------------------------------------------

def _latch_path(session_id: str) -> Path:
    safe = re.sub(r"[^\w\-]+", "_", session_id)[:100] or "unknown"
    return resolve_data_dir() / LATCH_DIR_NAME / f"{safe}.json"


def _latch_allows_and_arm(session_id: str) -> bool:
    """窗口内已 block 过返回 False；否则记录本次时间并返回 True。闩锁自身失败不拦 block。"""
    try:
        path = _latch_path(session_id)
        now = time.time()
        if path.exists():
            try:
                last = float(json.loads(path.read_text(encoding="utf-8"))
                             .get("last_block_ts") or 0)
            except (OSError, ValueError, AttributeError):
                last = 0.0
            if now - last < LATCH_WINDOW_S:
                return False
        atomic_write_text(path, json.dumps({"last_block_ts": now}))
        return True
    except OSError as e:
        _log("Stop", f"闩锁读写失败（不拦截本次 block）：{e}")
        return True


# ---------------------------------------------------------------------------
# 事件差异化输出
# ---------------------------------------------------------------------------

def _on_session_start(body: Dict[str, Any]) -> None:
    ac = body.get("additional_context")
    if not isinstance(ac, dict) or not ac.get("text"):
        return
    text = str(ac["text"])
    if len(text) > MAX_ADDITIONAL_CONTEXT or ac.get("truncated"):
        hint = f"\n（完整快照：{ac.get('full_file')}，需要时用 Read 读取）"
        room = MAX_ADDITIONAL_CONTEXT - len(hint)
        if len(text) > room:
            text = text[:room]
        text += hint
    _emit({"additionalContext": text})


def _on_stop(body: Dict[str, Any], data: Dict[str, Any]) -> None:
    if body.get("should") == "NO" or not body.get("instruction"):
        return
    session_id = str(data.get("session_id") or "")
    if not session_id:
        return
    if not _latch_allows_and_arm(session_id):
        _log("Stop", f"会话 {session_id} 15 分钟内已 block 过，闩锁生效跳过")
        return
    _emit({"decision": "block", "reason": body["instruction"]})


def main(argv: Optional[list] = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    event = argv[0] if argv else ""
    if event not in ("PostToolUse", "Stop", "SessionStart", "PreCompact",
                     "PostToolUseFailure"):
        _log(event or "-", "未指定或未知事件名，忽略")
        return 0
    raw = sys.stdin.buffer.read()
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}

    base_url = os.environ.get("AUTOCONDENSE_BASE_URL", "http://127.0.0.1:8761")
    body = _call_service(base_url, event, data)
    if body is None:
        return 0
    try:
        if event == "SessionStart":
            _on_session_start(body)
        elif event == "Stop":
            _on_stop(body, data)
        # PostToolUse / PreCompact / PostToolUseFailure：无输出
    except Exception as e:  # 输出阶段也绝不带崩宿主
        _log(event, f"输出构造失败：{type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
