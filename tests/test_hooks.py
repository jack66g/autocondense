"""ac_hook.py 薄钩子测试：subprocess 真跑脚本喂 stdin JSON。

覆盖：
① PostToolUse(TodoWrite) → 服务端计划更新（用服务 API 断言）
② 计划全完成后 Stop → stdout 是合法 JSON 且 decision=block
③ 闩锁：15 分钟内第二次 Stop → 无输出
④ SessionStart source=compact → additionalContext 含快照内容、超长截断含 full_file 提示
⑤ 服务不在线 → exit 0 且无 stdout
⑥ PostToolUse(Edit) → 服务端记文件活动轮；PostToolUseFailure → 钩子静默不崩（ac_hook
   allowlist 未含该事件名，服务端消费用直连断言），服务不在线同样静默 exit 0
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

REPO_ROOT = Path(__file__).resolve().parent.parent
AC_HOOK = REPO_ROOT / "plugins" / "autocondense" / "autocondense" / "hooks" / "ac_hook.py"


@pytest.fixture
def svc(tmp_path):
    """起真服务（随机端口，SME 指向必然秒拒的空端口做离线降级），返回 (base_url, data_dir, tmp)。"""
    from autocondense.service import create_app

    data_dir = tmp_path / "data"
    app = create_app(data_dir=data_dir,
                     sme_base_url=f"http://127.0.0.1:{_unused_port()}")
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "测试服务启动失败"
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", data_dir, tmp_path
    server.should_exit = True
    thread.join(timeout=5)


def run_hook(event: str, payload: dict, base_url: str, data_dir: Path) -> subprocess.CompletedProcess:
    """子进程跑 ac_hook.py（UTF-8 字节喂 stdin，Windows 编码坑隔离）。"""
    env = {**os.environ,
           "AUTOCONDENSE_BASE_URL": base_url,
           "AUTOCONDENSE_DATA_DIR": str(data_dir),
           "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(AC_HOOK), event],
        input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        capture_output=True, timeout=60, env=env, cwd=str(REPO_ROOT))


def api(method: str, base_url: str, path: str, **kwargs):
    import httpx
    kwargs.setdefault("timeout", 5.0)
    kwargs.setdefault("trust_env", False)
    r = httpx.request(method, f"{base_url}{path}", **kwargs)
    assert r.status_code < 400, r.text
    return r.json()


def _digest_big(padding_chars: int = 12000) -> str:
    from autocondense.digest import render_template
    md = render_template("proj", "2026-10-04T12:00:00", "plan_done")
    md += "\n<!-- 填充：" + "内" * padding_chars + " -->"
    return md


def _unused_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ① PostToolUse(TodoWrite) → 服务端计划更新
def test_posttooluse_todowrite_updates_plan(svc):
    base, _data_dir, tmp = svc
    cwd = str(tmp / "proj")
    proc = run_hook("PostToolUse", {
        "session_id": "h1", "cwd": cwd, "tool_name": "TodoWrite",
        "tool_input": {"todos": [{"content": "T1", "status": "pending"},
                                 {"content": "T2", "status": "completed"}]},
    }, base, _data_dir)
    assert proc.returncode == 0 and proc.stdout == b""
    st = api("GET", base, "/status")
    s1 = next(s for s in st["sessions"] if s["session_id"] == "h1")
    assert s1["plan_total"] == 2 and s1["plan_completed"] == 1


# ② 计划全完成 + Stop → decision=block（全自动：要求立即提交判定，透传服务端指令）
def test_stop_blocks_when_plan_done(svc):
    base, data_dir, tmp = svc
    cwd = str(tmp / "proj")
    run_hook("PostToolUse", {
        "session_id": "h2", "cwd": cwd, "tool_name": "TodoWrite",
        "tool_input": {"todos": [{"content": "T1", "status": "completed"}]},
    }, base, data_dir)
    proc = run_hook("Stop", {"session_id": "h2", "cwd": cwd}, base, data_dir)
    assert proc.returncode == 0
    out = json.loads(proc.stdout.decode("utf-8"))
    assert out["decision"] == "block"
    assert "condense_judge" in out["reason"]  # 全自动判定指令
    assert "不要询问用户" in out["reason"]
    assert "请询问用户" not in out["reason"]  # 旧「询问用户」文案不得残留


# ③ 闩锁：15 分钟内第二次 Stop → 无输出
def test_stop_latched_second_time(svc):
    base, data_dir, tmp = svc
    cwd = str(tmp / "proj")
    run_hook("PostToolUse", {
        "session_id": "h3", "cwd": cwd, "tool_name": "TodoWrite",
        "tool_input": {"todos": [{"content": "T1", "status": "completed"}]},
    }, base, data_dir)
    first = run_hook("Stop", {"session_id": "h3", "cwd": cwd}, base, data_dir)
    assert json.loads(first.stdout.decode("utf-8"))["decision"] == "block"
    assert (data_dir / "hook-latch" / "h3.json").is_file()

    second = run_hook("Stop", {"session_id": "h3", "cwd": cwd}, base, data_dir)
    assert second.returncode == 0 and second.stdout == b""  # 闩锁：不再 block


# ④ SessionStart source=compact → additionalContext（含截断 full_file 提示）
def test_session_start_compact_restore(svc):
    base, data_dir, tmp = svc
    cwd = str(tmp / "proj")
    # 放开服务端截断 → 超长由钩子 9000 上限截断
    api("PUT", base, "/config", json={"restore_max_chars": 200000})
    api("POST", base, "/session/start",
        json={"session_id": "s1", "cwd": cwd})
    api("POST", base, "/commit",
        json={"session_id": "s1", "cwd": cwd, "digest_md": _digest_big()})

    proc = run_hook("SessionStart",
                    {"session_id": "s9", "cwd": cwd, "source": "compact"}, base, data_dir)
    assert proc.returncode == 0
    out = json.loads(proc.stdout.decode("utf-8"))
    text = out["additionalContext"]
    assert "会话快照" in text
    assert len(text) <= 9000  # 钩子上限
    assert "完整快照" in text and text.rstrip().endswith("，需要时用 Read 读取）")
    assert ".md" in text  # full_file 快照路径在提示里

    # 服务端截断（默认 8000）同样追加 full_file 提示
    api("PUT", base, "/config", json={"restore_max_chars": 8000})
    proc = run_hook("SessionStart",
                    {"session_id": "s10", "cwd": cwd, "source": "clear"}, base, data_dir)
    out = json.loads(proc.stdout.decode("utf-8"))
    assert "完整快照" in out["additionalContext"]

    # source=startup 不恢复 → 无输出
    proc = run_hook("SessionStart",
                    {"session_id": "s11", "cwd": cwd, "source": "startup"}, base, data_dir)
    assert proc.returncode == 0 and proc.stdout == b""


# ⑤ 服务不在线 → exit 0 且无 stdout（绝不干扰宿主）
def test_service_offline_silent():
    proc = run_hook("Stop", {"session_id": "x", "cwd": "C:/x"},
                    f"http://127.0.0.1:{_unused_port()}", Path(os.environ.get("TEMP", ".")))
    assert proc.returncode == 0 and proc.stdout == b""


# ⑥ PostToolUseFailure / PostToolUse(Edit) → 钩子静默无输出；服务端记活动轮
def test_posttooluse_failure_and_edit_record_activity(svc):
    base, data_dir, tmp = svc
    cwd = str(tmp / "proj")
    # 文件修改（PostToolUse Edit，matcher 放行的新工具）：钩子静默，服务端记文件活动轮
    proc = run_hook("PostToolUse", {
        "session_id": "h5", "cwd": cwd, "tool_name": "Edit", "tool_input": {},
    }, base, data_dir)
    assert proc.returncode == 0 and proc.stdout == b""
    # 工具失败（PostToolUseFailure，ZCode 独有事件）：钩子静默退出不崩
    # （ac_hook 现版本 allowlist 未含该事件名，未知事件静默忽略；转发端到端
    #   打通只差 ac_hook 白名单，服务端消费已就绪——下方直连断言）
    proc = run_hook("PostToolUseFailure", {
        "session_id": "h5", "cwd": cwd, "tool_name": "Bash",
    }, base, data_dir)
    assert proc.returncode == 0 and proc.stdout == b""
    # 服务端消费：直连 /events/hook 断言失败轮已记账（/status 可见，idle 信号用）
    api("POST", base, "/events/hook", json={
        "event": "PostToolUseFailure", "session_id": "h5", "cwd": cwd,
        "tool_name": "Bash"})
    st = api("GET", base, "/status")
    s5 = next(s for s in st["sessions"] if s["session_id"] == "h5")
    assert s5["last_file_turn"] is not None
    assert s5["last_fail_turn"] is not None


# ⑥' 服务不在线时 PostToolUseFailure 也静默 exit 0（秒拒端口）
def test_posttooluse_failure_offline_silent():
    proc = run_hook("PostToolUseFailure", {"session_id": "x", "cwd": "C:/x",
                                           "tool_name": "Bash"},
                    f"http://127.0.0.1:{_unused_port()}",
                    Path(os.environ.get("TEMP", ".")))
    assert proc.returncode == 0 and proc.stdout == b""


def test_non_todowrite_and_precompact_no_output(svc):
    base, data_dir, tmp = svc
    cwd = str(tmp / "proj")
    proc = run_hook("PostToolUse", {
        "session_id": "h4", "cwd": cwd, "tool_name": "Bash", "tool_input": {},
    }, base, data_dir)
    assert proc.returncode == 0 and proc.stdout == b""
    proc = run_hook("PreCompact", {"session_id": "h4", "cwd": cwd, "source": "auto"},
                    base, data_dir)
    assert proc.returncode == 0 and proc.stdout == b""
    # 原生压缩已记入冷却
    d = api("GET", base, "/check", params={"session_id": "h4"})
    assert d["should"] == "NO" and "冷却" in d["reason"]


def test_stdin_garbage_and_unknown_event_silent(svc):
    base, data_dir, _ = svc
    env = {**os.environ, "AUTOCONDENSE_BASE_URL": base,
           "AUTOCONDENSE_DATA_DIR": str(data_dir), "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(AC_HOOK), "Stop"],
        input=b"\xff\xfe{not json", capture_output=True, timeout=60, env=env,
        cwd=str(REPO_ROOT))
    assert proc.returncode == 0 and proc.stdout == b""
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(AC_HOOK), "Whatever"],
        input=b"{}", capture_output=True, timeout=60, env=env, cwd=str(REPO_ROOT))
    assert proc.returncode == 0 and proc.stdout == b""
