"""service 全链路测试（TestClient，SME 用线程内 stub）。"""

from pathlib import Path

from fastapi.testclient import TestClient

from autocondense.digest import REQUIRED_SECTIONS, render_template


def _digest_ok(project="proj") -> str:
    md = render_template(project, "2026-10-04T12:00:00", "test")
    md += "\n<!-- 填充：这是一段用于测试的摘要正文，" + "内容补充。" * 30 + " -->"
    assert len(md) >= 200
    return md


def _verdict(**kw):
    base = dict(task_finished=True, impact="low", impact_reason="已收尾",
                safe_to_compress=True, reason="适合压缩")
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# 主链路：start → report(pending)=NO → check=NO → report(全完成)=JUDGE
#         → judge=COMPRESS_NOW → begin → commit → 快照/索引/事件/冷却/分段写入
#         → restore → PUT /config → status
# ---------------------------------------------------------------------------

def test_full_chain(make_app, tmp_path, sme_stub):
    _stub, base = sme_stub
    app = make_app(tmp_path, sme_base_url=base)
    client = TestClient(app)
    cwd = str(tmp_path / "proj")

    # 1) 会话启动（多余 mode 键宽容忽略，不报错）
    r = client.post("/session/start", json={"session_id": "s1", "cwd": cwd,
                                            "mode": "auto"})
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == "s1" and "mode" not in body

    # 2) report 只有 pending → NO
    r = client.post("/report", json={
        "session_id": "s1", "cwd": cwd,
        "plan_items": [{"title": "任务A", "status": "pending"}]})
    d = r.json()
    assert d["should"] == "NO" and d["signals"]["plan_total"] == 1

    # 3) check → NO（轮次远未到阈值）
    r = client.get("/check", params={"session_id": "s1"})
    assert r.json()["should"] == "NO"

    # 4) 全部 completed → 候选 JUDGE / plan_done，要求提交判定（不问用户）
    r = client.post("/report", json={
        "session_id": "s1", "cwd": cwd,
        "plan_items": [{"title": "任务A", "status": "completed"}]})
    d = r.json()
    assert d["should"] == "JUDGE" and d["trigger"] == "plan_done"
    assert d["urgency"] == "normal"
    assert "condense_judge" in d["action_hint"]
    assert "fields" in d["judgment_form"]

    # 5) 判定通过 → COMPRESS_NOW
    r = client.post("/judge", json={"session_id": "s1", "verdict": _verdict()})
    assert r.status_code == 200
    j = r.json()
    assert j["decision"] == "COMPRESS_NOW"
    assert "condense_begin" in j["action_hint"]

    # 6) begin
    r = client.post("/begin", json={"session_id": "s1", "cwd": cwd})
    b = r.json()
    assert "会话快照" in b["template"]
    assert b["required_sections"] == REQUIRED_SECTIONS
    assert "指引" in b["guidance"] or "章节" in b["guidance"]
    assert "无需询问用户" in b["guidance"]

    # 7) commit（合法 digest，SME stub 在线 → 分段批量写入）
    digest = _digest_ok()
    r = client.post("/commit", json={"session_id": "s1", "cwd": cwd, "digest_md": digest})
    assert r.status_code == 200, r.text
    c = r.json()
    assert Path(c["snapshot_path"]).is_file()
    # 批量契约：≥2 段走 /memories/batch，不返回各条 id → sme.memory_id 为 null
    assert c["sme"]["stored"] is True and c["sme"]["memory_id"] is None
    assert c["segments_written"] >= 5
    assert c["sme_segments"]["stored"] == c["segments_written"]
    assert c["mode"] == "full"  # 项目首份快照必为全量
    assert "/compact" in c["next_instruction"]

    # 快照 index（批量无 id → 不回填 sme_memory_id）+ 事件
    r = client.get("/restore", params={"cwd": cwd})
    res = r.json()
    assert res["project"] == "proj"
    assert len(res["snapshots"]) == 1
    entry = res["snapshots"][0]
    assert entry["sme_memory_id"] is None
    assert entry["delta"] is False
    assert entry["title"].startswith("# 会话快照")
    # restore 拿回优先级恢复包（预算内不截断，含模板章节内容）
    assert res["restore"]["full_file"] == c["snapshot_path"]
    assert res["restore"]["truncated"] is False
    assert "## 目标" in res["restore"]["text"] and "填充" in res["restore"]["text"]
    assert "Read 工具" in res["restore"]["note"]
    # SME 检索结果含刚入库的分段
    assert res["sme_results"]

    # mark_condensed：冷却生效；延迟窗口清空
    r = client.get("/check", params={"session_id": "s1"})
    d = r.json()
    assert d["should"] == "NO" and "冷却" in d["reason"]
    # completed 计划条目被清理
    st = client.get("/status").json()
    s1 = next(s for s in st["sessions"] if s["session_id"] == "s1")
    assert s1["plan_total"] == 0 and s1["turns_since_condense"] <= 1
    assert s1["defer_until_turn"] is None
    assert s1["last_judge"]["result"] == "APPROVED"

    # 8) 收紧 restore_max_chars → 截断生效
    r = client.put("/config", json={"restore_max_chars": 100})
    assert r.status_code == 200 and r.json()["restore_max_chars"] == 100
    r = client.get("/restore", params={"cwd": cwd})
    res = r.json()
    assert res["restore"]["truncated"] is True
    assert len(res["restore"]["text"]) <= 100

    # 9) 收紧 turns_threshold → 新会话（避开冷却）按新阈值触发候选
    r = client.put("/config", json={"turns_threshold": 3})
    assert r.status_code == 200
    client.post("/session/start", json={"session_id": "s2", "cwd": cwd})
    last = None
    for _ in range(3):
        last = client.get("/check", params={"session_id": "s2"}).json()
    assert last["should"] == "JUDGE" and last["trigger"] == "turns"

    # 10) 未知配置项 / 类型错 → 400
    r = client.put("/config", json={"nope": 1})
    assert r.status_code == 400 and "未知配置项" in r.json()["errors"][0]
    r = client.put("/config", json={"cooldown_minutes": "fast"})
    assert r.status_code == 400
    r = client.put("/config", json={"enabled": "off"})
    assert r.status_code == 400

    # 11) status 可见历史与新字段
    r = client.get("/status")
    st = r.json()
    assert st["enabled"] is True and st["judge_defer_turns"] == 5
    assert st["config"]["enabled"] is True
    assert {s["session_id"] for s in st["sessions"]} >= {"s1", "s2"}
    assert st["config"]["turns_threshold"] == 3
    events = [e["event"] for e in st["recent_events"]]
    assert "condense" in events and "commit" in events and "session_start" in events
    assert "judge" in events


def test_commit_invalid_digest_400(make_app, tmp_path):
    app = make_app(tmp_path)
    client = TestClient(app)
    client.post("/session/start", json={"session_id": "s1", "cwd": "C:/x"})
    r = client.post("/commit", json={"session_id": "s1", "cwd": "C:/x",
                                     "digest_md": "太短且缺章节"})
    assert r.status_code == 400
    errors = r.json()["errors"]
    assert any("章节" in e for e in errors) and any("200" in e for e in errors)


def test_check_unknown_session_400(make_app, tmp_path):
    client = TestClient(make_app(tmp_path))
    r = client.get("/check", params={"session_id": "ghost"})
    assert r.status_code == 400 and r.json()["errors"]


# ---------------------------------------------------------------------------
# 钩子入口
# ---------------------------------------------------------------------------

def test_hook_todowrite_shapes(make_app, tmp_path):
    client = TestClient(make_app(tmp_path))
    cwd = str(tmp_path / "proj")
    client.post("/session/start", json={"session_id": "h1", "cwd": cwd})

    # {"todos":[{"content","status"}]} 形态，status 大小写兼容
    r = client.post("/events/hook", json={
        "event": "PostToolUse", "tool_name": "TodoWrite",
        "session_id": "h1", "cwd": cwd,
        "tool_input": {"todos": [{"content": "T1", "status": "IN_PROGRESS"}]}})
    d = r.json()
    assert d["should"] == "NO" and "进行中" in d["reason"]
    assert d["signals"]["plan_in_progress"] == 1

    # {"items":[...]} 形态 + done 兼容
    r = client.post("/events/hook", json={
        "event": "PostToolUse", "tool_name": "TodoWrite",
        "session_id": "h1", "cwd": cwd,
        "tool_input": {"items": [{"title": "T1", "status": "Done"}]}})
    assert r.json()["trigger"] == "plan_done"

    # 直接数组形态
    r = client.post("/events/hook", json={
        "event": "PostToolUse", "tool_name": "TodoWrite",
        "session_id": "h1", "cwd": cwd,
        "tool_input": [{"title": "T1", "status": "completed"}]})
    assert r.json()["should"] == "JUDGE"

    # 非 TodoWrite 工具 → 忽略
    r = client.post("/events/hook", json={
        "event": "PostToolUse", "tool_name": "Bash",
        "session_id": "h1", "cwd": cwd, "tool_input": {}})
    assert r.json()["ignored"] is True

    # 缺 session_id → 400
    r = client.post("/events/hook", json={
        "event": "PostToolUse", "tool_name": "TodoWrite",
        "cwd": cwd, "tool_input": {"todos": []}})
    assert r.status_code == 400


def test_hook_session_start_restore(make_app, tmp_path, sme_stub):
    _stub, base = sme_stub
    client = TestClient(make_app(tmp_path, sme_base_url=base))
    cwd = str(tmp_path / "proj")
    client.post("/session/start", json={"session_id": "s1", "cwd": cwd})
    client.post("/commit", json={"session_id": "s1", "cwd": cwd, "digest_md": _digest_ok()})

    # source=resume → additional_context 携带快照恢复载荷
    r = client.post("/events/hook", json={
        "event": "SessionStart", "session_id": "s2", "cwd": cwd,
        "source": "resume", "transcript_path": None, "context_tokens": 1000})
    b = r.json()
    assert b["ok"] is True and b["session_id"] == "s2"
    ac = b["additional_context"]
    assert ac and ac["full_file"] and "会话快照" in ac["text"]

    # 普通启动（source 不在 compact/clear/resume）→ 无恢复
    r = client.post("/events/hook", json={
        "event": "SessionStart", "session_id": "s3", "cwd": cwd, "source": "startup"})
    assert r.json()["additional_context"] is None


def test_hook_stop_instruction_auto_flow(make_app, tmp_path):
    client = TestClient(make_app(tmp_path))
    cwd = str(tmp_path / "proj")
    client.put("/config", json={"turns_threshold": 1})

    # 候选命中 → Stop 指令要求立即提交判定（全自动，不问用户）
    client.post("/session/start", json={"session_id": "m1", "cwd": cwd})
    r = client.post("/events/hook", json={"event": "Stop", "session_id": "m1", "cwd": cwd})
    d = r.json()
    assert d["should"] == "JUDGE" and d["trigger"] == "turns"
    assert "condense_judge" in d["instruction"]
    assert "不要询问用户" in d["instruction"]

    # 老 hook 载荷带 mode 键 → 宽容忽略不报错
    client.post("/session/start", json={"session_id": "m2", "cwd": cwd})
    r = client.post("/events/hook", json={"event": "Stop", "session_id": "m2",
                                          "cwd": cwd, "mode": "auto"})
    assert r.status_code == 200 and r.json()["should"] == "JUDGE"

    # 未触发时 instruction 为 null
    client.post("/session/start", json={"session_id": "m3", "cwd": cwd})
    client.put("/config", json={"turns_threshold": 100})
    r = client.post("/events/hook", json={"event": "Stop", "session_id": "m3", "cwd": cwd})
    assert r.json()["instruction"] is None


def test_hook_precompact(make_app, tmp_path):
    client = TestClient(make_app(tmp_path))
    cwd = str(tmp_path / "proj")
    client.post("/session/start", json={"session_id": "p1", "cwd": cwd})
    r = client.post("/events/hook", json={"event": "PreCompact", "session_id": "p1", "cwd": cwd})
    assert r.json() == {"ok": True}
    # 原生压缩后进入冷却
    d = client.get("/check", params={"session_id": "p1"}).json()
    assert d["should"] == "NO" and "冷却" in d["reason"]
    st = client.get("/status").json()
    s = next(s for s in st["sessions"] if s["session_id"] == "p1")
    assert s["last_trigger"] == "pre_native_compact"
    assert any(e["event"] == "pre_compact" for e in st["recent_events"])


def test_health_and_unknown_event(make_app, tmp_path, sme_stub):
    _stub, base = sme_stub
    client = TestClient(make_app(tmp_path, sme_base_url=base))
    h = client.get("/health").json()
    assert h["status"] == "ok" and h["version"] and h["sme_connected"] is True
    assert "data" in h["data_dir"]
    r = client.post("/events/hook", json={"event": "Whatever"})
    assert r.status_code == 200 and r.json()["ignored"] is True


def test_sme_offline_still_commits(make_app, tmp_path):
    # SME 不在线：commit 依旧成功，仅降级（分段整批失败 → 无整篇兜底可写）
    client = TestClient(make_app(tmp_path, sme_base_url="http://127.0.0.1:9"))
    cwd = str(tmp_path / "proj")
    client.post("/session/start", json={"session_id": "s1", "cwd": cwd})
    r = client.post("/commit", json={"session_id": "s1", "cwd": cwd, "digest_md": _digest_ok()})
    assert r.status_code == 200
    c = r.json()
    assert Path(c["snapshot_path"]).is_file()
    assert c["sme"]["stored"] is False and c["sme"]["error"]
    assert c["segments_written"] == 0
    res = client.get("/restore", params={"cwd": cwd}).json()
    assert res["sme_results"] == []
    assert res["snapshots"][0]["sme_memory_id"] is None


def test_water_level_via_transcript(make_app, tmp_path):
    # transcript 估算水位触发 water_level 候选，且 urgency=urgent
    client = TestClient(make_app(tmp_path))
    cwd = str(tmp_path / "proj")
    big = tmp_path / "transcript.jsonl"
    big.write_text("字" * 200000 + "\n", encoding="utf-8")  # 约 20 万 token
    client.post("/session/start", json={"session_id": "w1", "cwd": cwd,
                                        "transcript_path": str(big)})
    d = client.get("/check", params={"session_id": "w1"}).json()
    assert d["trigger"] == "water_level" and d["should"] == "JUDGE"
    assert d["urgency"] == "urgent"
    assert d["signals"]["water_level"] >= 1.0
    assert d["signals"]["token_source"] == "estimated"  # 无上报 → 估算口径


# ---------------------------------------------------------------------------
# 优化 6：完成信号扩展（文件修改/工具失败活动轮）+ idle 候选
# ---------------------------------------------------------------------------

def _status_session(client, sid):
    st = client.get("/status").json()
    return next(s for s in st["sessions"] if s["session_id"] == sid)


def test_hook_file_edit_records_activity(make_app, tmp_path):
    client = TestClient(make_app(tmp_path))
    cwd = str(tmp_path / "proj")
    client.post("/session/start", json={"session_id": "a1", "cwd": cwd})
    for tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        r = client.post("/events/hook", json={
            "event": "PostToolUse", "tool_name": tool,
            "session_id": "a1", "cwd": cwd, "tool_input": {}})
        assert r.status_code == 200 and r.json()["ignored"] is True
        assert "文件修改活动" in r.json()["reason"]
    s = _status_session(client, "a1")
    assert s["last_file_turn"] == 0 and s["last_fail_turn"] is None
    # 非 TodoWrite 非文件工具（Bash）不记活动
    client.post("/events/hook", json={
        "event": "PostToolUse", "tool_name": "Bash",
        "session_id": "a1", "cwd": cwd, "tool_input": {}})
    assert _status_session(client, "a1")["last_file_turn"] == 0
    # 缺 session_id → 400
    r = client.post("/events/hook", json={
        "event": "PostToolUse", "tool_name": "Edit", "cwd": cwd, "tool_input": {}})
    assert r.status_code == 400


def test_hook_posttoolusefailure_records_activity(make_app, tmp_path):
    client = TestClient(make_app(tmp_path))
    cwd = str(tmp_path / "proj")
    client.post("/session/start", json={"session_id": "f1", "cwd": cwd})
    r = client.post("/events/hook", json={
        "event": "PostToolUseFailure", "session_id": "f1", "cwd": cwd,
        "tool_name": "Bash"})
    assert r.status_code == 200 and r.json()["ok"] is True
    client.post("/events/hook", json={"event": "Stop", "session_id": "f1", "cwd": cwd})
    client.post("/events/hook", json={"event": "Stop", "session_id": "f1", "cwd": cwd})
    s = _status_session(client, "f1")
    assert s["turns"] == 2 and s["last_fail_turn"] == 0 and s["last_file_turn"] is None
    # 缺 session_id → 400
    assert client.post("/events/hook", json={
        "event": "PostToolUseFailure", "cwd": cwd}).status_code == 400


def test_idle_candidate_via_hooks(make_app, tmp_path):
    client = TestClient(make_app(tmp_path))
    cwd = str(tmp_path / "proj")
    client.post("/session/start", json={"session_id": "i1", "cwd": cwd})
    # 文件修改（turn 0）后连续 3 个 Stop → idle_rounds=3 → idle 候选
    client.post("/events/hook", json={
        "event": "PostToolUse", "tool_name": "Edit",
        "session_id": "i1", "cwd": cwd, "tool_input": {}})
    for _ in range(2):
        last = client.post("/events/hook", json={
            "event": "Stop", "session_id": "i1", "cwd": cwd}).json()
        assert last["should"] == "NO"  # idle 1/2 轮未达阈值
    last = client.post("/events/hook", json={
        "event": "Stop", "session_id": "i1", "cwd": cwd}).json()
    assert last["should"] == "JUDGE" and last["trigger"] == "idle"
    assert "无文件修改与失败" in last["reason"]
    # 工具失败会刷新活动轮：idle 重新计数（turns=4，last_fail=3 → idle=1）
    client.post("/events/hook", json={
        "event": "PostToolUseFailure", "session_id": "i1", "cwd": cwd,
        "tool_name": "Bash"})
    d = client.post("/events/hook", json={
        "event": "Stop", "session_id": "i1", "cwd": cwd}).json()
    assert d["should"] == "NO" and d["signals"]["idle_rounds"] == 1


def test_idle_never_fires_without_activity_records(make_app, tmp_path):
    # 老会话/hook 未生效：无活动记录 → 连续多轮 Stop 也不误报 idle
    client = TestClient(make_app(tmp_path))
    cwd = str(tmp_path / "proj")
    client.post("/session/start", json={"session_id": "old1", "cwd": cwd})
    for _ in range(5):
        last = client.post("/events/hook", json={
            "event": "Stop", "session_id": "old1", "cwd": cwd}).json()
    assert last["should"] == "NO" and last["signals"]["idle_rounds"] is None
    s = _status_session(client, "old1")
    assert s["last_file_turn"] is None and s["last_fail_turn"] is None
