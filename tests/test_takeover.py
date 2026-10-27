"""插件更新后的旧服务接管测试（pid 文件 + 版本握手 + 定点结束）。

不杀真实进程：_terminate_pid / _spawn_service / _health_info 全部 monkeypatch，
验证决策逻辑（何时杀、杀谁、何时报错、绝不盲杀）。真实接管由冒烟脚本验证。
"""

from __future__ import annotations

import json

import pytest

from autocondense import service as svc
import autocondense.mcp_server as m


# ---------------------------------------------------------------------------
# pid 文件读写（service 层）
# ---------------------------------------------------------------------------

def test_pid_file_roundtrip(tmp_path):
    path = svc.write_pid_file(tmp_path, port=8761)
    assert path is not None and path.exists()
    data = svc.read_pid_file(tmp_path)
    assert data is not None
    assert data["pid"] > 0 and data["port"] == 8761
    assert isinstance(data["version"], str) and data["version"]


def test_pid_file_clear_own_and_keep_foreign(tmp_path):
    svc.write_pid_file(tmp_path, port=1)
    own = svc.read_pid_file(tmp_path)["pid"]
    svc.clear_pid_file(tmp_path, own)
    assert svc.read_pid_file(tmp_path) is None

    svc.write_pid_file(tmp_path, port=1)
    svc.clear_pid_file(tmp_path, pid=own + 1)  # 已被接管：不动别人的记录
    assert svc.read_pid_file(tmp_path) is not None


def test_pid_file_corrupt_tolerated(tmp_path):
    (tmp_path / svc.PID_FILE_NAME).write_text("{oops", encoding="utf-8")
    assert svc.read_pid_file(tmp_path) is None
    assert m._read_service_pid(tmp_path) is None  # 代理侧同样容忍


def test_read_service_pid_bad_payload(tmp_path):
    (tmp_path / m.SERVICE_PID_FILE).write_text(
        json.dumps({"pid": "abc"}), encoding="utf-8")
    assert m._read_service_pid(tmp_path) is None
    assert m._read_service_pid(tmp_path / "不存在") is None


# ---------------------------------------------------------------------------
# _health_info 识别纪律：非本插件族应答不算就绪
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, code, body):
        self.status_code = code
        self._body = body

    def json(self):
        return self._body


def test_health_info_requires_status_and_version(monkeypatch):
    queue = []

    def fake_get(url, timeout=3.0, **kw):
        return _Resp(200, queue.pop(0))

    monkeypatch.setattr(m.httpx, "get", fake_get)
    queue.append({"status": "ok"})
    assert m._health_info() is None  # 缺 version
    queue.append({"error": "x"})
    assert m._health_info() is None  # 缺 status
    queue.append({"status": "ok", "version": "0.1.0"})
    assert m._health_info() == {"status": "ok", "version": "0.1.0"}
    queue.append({"hello": "world"})  # 端口上是不明应用
    assert m._health_info() is None


def test_version_matches():
    assert m._version_matches({"status": "ok", "version": m.PKG_VERSION})
    assert not m._version_matches({"status": "ok", "version": "0.1.0"})
    assert not m._version_matches(None)


# ---------------------------------------------------------------------------
# 接管决策（全 mock）
# ---------------------------------------------------------------------------

@pytest.fixture
def takeover_env(monkeypatch, tmp_path):
    """准备：端口上是旧版服务；pid 文件指向 pid=4321；杀掉后端口空出可重拉。"""
    state = {"terminated": [], "spawned": 0, "port_free_after_kill": True}
    monkeypatch.setenv("AUTOCONDENSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AUTOCONDENSE_PORT", "18761")
    (tmp_path / m.SERVICE_PID_FILE).write_text(
        json.dumps({"pid": 4321, "version": "0.1.0", "port": 18761}),
        encoding="utf-8")

    def fake_health(timeout: float = 3.0):
        if state["terminated"]:
            return None  # 杀掉后端口空出
        return {"status": "ok", "version": "0.1.0"}

    def fake_terminate(pid: int) -> bool:
        state["terminated"].append(pid)
        return pid > 4

    def fake_spawn() -> None:
        state["spawned"] += 1

    monkeypatch.setattr(m, "_health_info", fake_health)
    monkeypatch.setattr(m, "_terminate_pid", fake_terminate)
    monkeypatch.setattr(m, "_spawn_service", fake_spawn)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    return state


def test_ensure_takes_over_stale_by_pid_file(takeover_env, monkeypatch):
    # 重拉后新服务报告匹配版本 → 就绪
    takeover_env["terminated"] = []
    monkeypatch.setattr(
        m, "_health_info",
        lambda timeout=3.0: ({"status": "ok", "version": m.PKG_VERSION}
                             if takeover_env["spawned"] else
                             (None if takeover_env["terminated"]
                              else {"status": "ok", "version": "0.1.0"})))
    m._ensure_service(wait_seconds=5, poll_interval=0.01)
    assert takeover_env["terminated"] == [4321]  # 按 pid 文件定点
    assert takeover_env["spawned"] == 1


def test_ensure_takes_over_via_port_when_no_pid_file(takeover_env, monkeypatch, tmp_path):
    (tmp_path / m.SERVICE_PID_FILE).unlink()
    monkeypatch.setattr(m, "_find_port_pid", lambda port: 9999)
    monkeypatch.setattr(
        m, "_health_info",
        lambda timeout=3.0: ({"status": "ok", "version": m.PKG_VERSION}
                             if takeover_env["spawned"] else
                             (None if takeover_env["terminated"]
                              else {"status": "ok", "version": "0.1.0"})))
    m._ensure_service(wait_seconds=5, poll_interval=0.01)
    assert takeover_env["terminated"] == [9999]  # 兜底：端口反查


def test_ensure_errors_when_stale_unidentifiable(takeover_env, monkeypatch, tmp_path):
    (tmp_path / m.SERVICE_PID_FILE).unlink()
    monkeypatch.setattr(m, "_find_port_pid", lambda port: None)
    with pytest.raises(RuntimeError, match="无法安全接管"):
        m._ensure_service(wait_seconds=2, poll_interval=0.01)
    assert takeover_env["spawned"] == 0  # 绝不盲拉（端口被占，拉了也起不来）


def test_ensure_errors_when_terminate_fails(takeover_env, monkeypatch):
    monkeypatch.setattr(m, "_terminate_pid", lambda pid: False)
    with pytest.raises(RuntimeError, match="无法安全接管"):
        m._ensure_service(wait_seconds=2, poll_interval=0.01)


def test_ensure_errors_when_stale_refuses_to_die(takeover_env, monkeypatch):
    # 杀了但 health 一直应答（僵尸/权限不足）→ 报错而不是死循环
    class FakeTime:  # 避免真实等满 10 秒
        now = 0.0

        def monotonic(self):
            FakeTime.now += 1.0
            return FakeTime.now

        def sleep(self, s):
            return None

    monkeypatch.setattr(m, "time", FakeTime())

    def health_forever(timeout: float = 3.0):
        return {"status": "ok", "version": "0.1.0"}

    monkeypatch.setattr(m, "_health_info", health_forever)
    with pytest.raises(RuntimeError, match="未能退出"):
        m._ensure_service(wait_seconds=2, poll_interval=0.01)


def test_ensure_no_takeover_when_version_matches(takeover_env, monkeypatch):
    monkeypatch.setattr(
        m, "_health_info",
        lambda timeout=3.0: {"status": "ok", "version": m.PKG_VERSION})
    m._ensure_service(wait_seconds=2, poll_interval=0.01)
    assert takeover_env["terminated"] == [] and takeover_env["spawned"] == 0


def test_terminate_pid_guards(monkeypatch):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args[0])
        return None

    monkeypatch.setattr(m.subprocess, "run", fake_run)
    monkeypatch.setattr(m.os, "kill", lambda *a, **kw: None)  # POSIX 分支安全兜底
    assert m._terminate_pid(0) is False            # 系统保留
    assert m._terminate_pid(4) is False
    assert m._terminate_pid(m.os.getpid()) is False  # 绝不自杀
    assert calls == []
    assert m._terminate_pid(4321) is True          # 正常路径交给 taskkill
    assert calls and "4321" in str(calls[0])


def test_takeover_reports_stale_version(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOCONDENSE_DATA_DIR", str(tmp_path))
    (tmp_path / m.SERVICE_PID_FILE).write_text(
        json.dumps({"pid": 4321}), encoding="utf-8")
    killed = []

    def fake_terminate(pid):
        killed.append(pid)
        return True

    def health_never(timeout=3.0):
        return None  # 杀完立刻消失

    monkeypatch.setattr(m, "_terminate_pid", fake_terminate)
    monkeypatch.setattr(m, "_health_info", health_never)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    m._takeover_stale_service({"status": "ok", "version": "0.1.0"})
    assert killed == [4321]
