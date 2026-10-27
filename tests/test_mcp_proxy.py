"""mcp_server 薄代理测试：线程内真起服务，直接调用工具函数走全链路。

不启动 stdio 传输（stdio 链路由真实冒烟验证），聚焦：
- cwd → session_id 映射（建立/复用/原子写/损坏容忍）
- 七个工具的输出契约（判定/判定表提交/模板/提交/恢复/状态）
- _ensure_service 拉起分支：单飞锁 + 双检 + 轮询 + 超时
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest
import uvicorn

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def service_env(tmp_path_factory):
    """起真服务（随机端口）+ 注入环境变量 + 导入 mcp_server 模块。"""
    from autocondense.service import create_app

    data_dir = tmp_path_factory.mktemp("ac_mcp_data")
    # SME 指向必然不在线的端口：验证降级路径（不依赖 stub）
    app = create_app(data_dir=data_dir, sme_base_url="http://127.0.0.1:9")
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

    saved = {k: os.environ.get(k) for k in ("AUTOCONDENSE_PORT", "AUTOCONDENSE_DATA_DIR")}
    os.environ["AUTOCONDENSE_PORT"] = str(port)
    os.environ["AUTOCONDENSE_DATA_DIR"] = str(data_dir)

    import autocondense.mcp_server as m

    yield m, data_dir

    server.should_exit = True
    thread.join(timeout=5)
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _digest_ok(project="proj") -> str:
    from autocondense.digest import render_template
    md = render_template(project, "2026-10-04T12:00:00", "plan_done")
    md += "\n<!-- 填充：这是一段用于测试的摘要正文，" + "内容补充。" * 30 + " -->"
    assert len(md) >= 200
    return md


# ---------------------------------------------------------------------------
# 全链路：check 建会话 → report → 候选 JUDGE → begin → commit → restore → status
# ---------------------------------------------------------------------------

def test_full_chain_via_mcp_tools(service_env):
    m, data_dir = service_env
    cwd = str(data_dir / "proj")
    map_file = data_dir / "mcp-session-map.json"

    # 1) 首次 check：按 cwd 自动建会话，判定 NO
    out = m.condense_check(cwd=cwd)
    assert "判定：NO" in out
    assert map_file.is_file()
    mapping = json.loads(map_file.read_text(encoding="utf-8"))
    assert len(mapping) == 1
    sid = next(iter(mapping.values()))
    assert sid.startswith("mcp-") and len(sid.split("-")) == 3  # mcp-xxxxxxxx-xxxx

    # 2) report 进行中 → NO（进行中阻断）
    out = m.condense_report('[{"title":"任务A","status":"in_progress"}]', cwd=cwd)
    assert "判定：NO" in out and "进行中" in out

    # 3) report 全部完成 → 候选 JUDGE / plan_done（动作建议：提交大模型判定）
    out = m.condense_report('[{"title":"任务A","status":"completed"}]', cwd=cwd)
    assert "JUDGE" in out and "plan_done" in out
    assert "condense_judge" in out
    assert "不要询问用户" in out

    # 4) 再次 check 复用同一会话（映射不增长）
    out = m.condense_check(cwd=cwd)
    assert "JUDGE" in out
    assert json.loads(map_file.read_text(encoding="utf-8")) == mapping

    # 5) begin：模板 + 指引 + 必填章节
    out = m.condense_begin(cwd=cwd)
    assert "已进入压缩流程" in out and "会话快照" in out
    assert "填写指引" in out and "必填章节" in out
    assert "## 恢复指针" in out and "condense_commit" in out

    # 6) commit 校验失败 → 中文 errors 透传，指引修正
    out = m.condense_commit("太短且缺章节", cwd=cwd)
    assert "校验未通过" in out and "章节" in out and "200" in out

    # 7) commit 成功 → 快照路径 + SME 降级说明 + next_instruction
    out = m.condense_commit(_digest_ok(), cwd=cwd)
    assert "快照已保存" in out and "未入库" in out and "下一步" in out
    snap_path = Path(out.splitlines()[0].split("：", 1)[1])
    assert snap_path.is_file()

    # 8) restore：恢复文本 + 截断指引占位（此处未截断）
    out = m.condense_restore(cwd=cwd)
    assert "恢复内容" in out and "填充" in out
    assert "历史快照" in out and "SME" in out

    # 9) status 摘要
    out = m.condense_status()
    assert "autocondense 服务" in out and "配置：" in out

    # 10) 原子写：无临时文件残留
    assert not list(data_dir.glob("*.tmp-*"))


# ---------------------------------------------------------------------------
# condense_judge 代理：JUDGE 候选 → 判定表提交 → COMPRESS_NOW / DEFERRED / 拒答降级
# ---------------------------------------------------------------------------

def test_judge_tool_roundtrip(service_env):
    m, data_dir = service_env
    cwd = str(data_dir / "judgeproj")
    # 建候选：计划全部完成 → JUDGE（含判定表字段提示）
    out = m.condense_report('[{"title":"任务A","status":"completed"}]', cwd=cwd)
    assert "JUDGE" in out and "plan_done" in out
    assert "判定表字段" in out and "task_finished" in out

    # 判定通过（normal 档矩阵）→ COMPRESS_NOW + action_hint 原样带回（直接压缩、无需确认）
    out = m.condense_judge(cwd=cwd, task_finished=True, impact="low",
                           safe_to_compress=True, impact_reason="已收尾",
                           reason="本批任务完成")
    assert "已提交判定" in out and "COMPRESS_NOW" in out and "normal" in out
    assert "condense_begin" in out and "无需询问用户" in out

    # status 可见最近判定 APPROVED
    assert "APPROVED" in m.condense_status()

    # 判定不通过 → DEFERRED + 延迟窗口（默认 5 轮）+ 继续干活的动作建议
    out = m.condense_judge(cwd=cwd, task_finished=False, impact="medium",
                           safe_to_compress=True)
    assert "DEFERRED" in out and "延迟窗口" in out
    assert "继续当前工作" in out
    st = m.condense_status()
    assert "DEFERRED" in st and "延迟至第" in st  # 会话状态可见判定与窗口

    # 非法 impact → 服务 400 中文错误透传为降级文案
    out = m.condense_judge(cwd=cwd, task_finished=True, impact="critical",
                           safe_to_compress=True)
    assert "判定提交失败" in out and "low、medium 或 high" in out

    # 显式 session_id 但会话不存在 → 中文失败信息
    out = m.condense_judge(cwd="", task_finished=True, impact="low",
                           safe_to_compress=True, session_id="ghost-sid")
    assert "判定提交失败" in out and "会话不存在" in out


def test_judge_payload_passthrough(service_env, monkeypatch):
    m, _ = service_env
    captured = []

    def fake_api(method, path, payload=None, params=None, timeout=60.0):
        captured.append((method, path, payload))
        return {"decision": "COMPRESS_NOW", "urgency": "normal", "reason": "x",
                "action_hint": "h", "signals": {}}

    monkeypatch.setattr(m, "_api", fake_api)
    out = m.condense_judge(cwd="C:/tmp/x", task_finished=True, impact="low",
                           safe_to_compress=False, impact_reason="r1",
                           reason="r2", session_id="sid-x")
    assert "COMPRESS_NOW" in out
    # 参数透传：POST /judge，五字段 verdict 原样带回（可选参数默认空串补齐）
    assert len(captured) == 1  # 显式 session_id 不触发 /session/start
    method, path, payload = captured[0]
    assert method == "POST" and path == "/judge"
    assert payload["session_id"] == "sid-x" and payload["cwd"] == "C:/tmp/x"
    assert payload["verdict"] == {"task_finished": True, "impact": "low",
                                  "impact_reason": "r1",
                                  "safe_to_compress": False, "reason": "r2"}


def test_explicit_session_id_wins(service_env):
    m, data_dir = service_env
    cwd = str(data_dir / "proj2")
    # 显式 id 但会话不存在 → 400 透传为中文失败信息
    out = m.condense_check(session_id="explicit-s1", cwd=cwd)
    assert "检查失败" in out and "会话不存在" in out
    # report 会用显式 id 建会话
    out = m.condense_report('[{"title":"X","status":"pending"}]',
                            session_id="explicit-s1", cwd=cwd)
    assert "已上报" in out
    out = m.condense_check(session_id="explicit-s1")
    assert "判定：NO" in out


def test_report_bad_json_guidance(service_env):
    m, data_dir = service_env
    cwd = str(data_dir / "proj3")
    out = m.condense_report("not-json", cwd=cwd)
    assert "不是合法 JSON" in out
    out = m.condense_report('{"title":"X"}', cwd=cwd)
    assert "必须是 JSON 数组" in out


def test_restore_requires_cwd(service_env):
    m, _ = service_env
    out = m.condense_restore()
    assert "cwd" in out


def test_check_without_anything_guidance(service_env):
    m, _ = service_env
    out = m.condense_check()
    assert "检查失败" in out and "cwd" in out


# ---------------------------------------------------------------------------
# 会话映射：损坏容忍 + 原子写
# ---------------------------------------------------------------------------

def test_session_map_corrupt_tolerated(service_env):
    m, data_dir = service_env
    map_file = data_dir / "mcp-session-map.json"
    original = map_file.read_text(encoding="utf-8") if map_file.exists() else None
    try:
        map_file.write_text("{ 坏 JSON", encoding="utf-8")
        assert m._load_session_map() == {}
    finally:
        if original is not None:
            map_file.write_text(original, encoding="utf-8")
        else:
            map_file.unlink(missing_ok=True)
    # 原子写往返
    m._save_session_map({"deadbeef": "mcp-deadbeef-ab12"})
    assert m._load_session_map() == {"deadbeef": "mcp-deadbeef-ab12"}


# ---------------------------------------------------------------------------
# _ensure_service 拉起分支（monkeypatch 假 Popen + 假 health）
# ---------------------------------------------------------------------------

def test_ensure_service_single_flight_and_poll(service_env, monkeypatch):
    m, _ = service_env
    calls = {"health": 0, "popen": 0}
    ready = threading.Event()

    def fake_health_info(timeout: float = 3.0):
        calls["health"] += 1
        if calls["popen"] >= 1 and ready.is_set():
            return {"status": "ok", "version": m.PKG_VERSION}
        return None

    class FakeProc:
        pass

    def fake_popen(*args, **kwargs):
        calls["popen"] += 1
        ready.set()  # 「拉起后立刻就绪」
        return FakeProc()

    monkeypatch.setattr(m, "_health_info", fake_health_info)
    monkeypatch.setattr(m.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)

    results: list = []
    errors: list = []

    def worker():
        try:
            m._ensure_service(wait_seconds=5, poll_interval=0.01)
            results.append("ok")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert not errors and len(results) == 4
    assert calls["popen"] == 1  # 单飞：并发只拉起一次


def test_ensure_service_timeout_raises_with_log_path(service_env, monkeypatch):
    m, _ = service_env
    monkeypatch.setattr(m, "_health_info", lambda timeout=3.0: None)
    monkeypatch.setattr(m, "_spawn_service", lambda: None)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="autocondense_service"):
        m._ensure_service(wait_seconds=0.3, poll_interval=0.01)
