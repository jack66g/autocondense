"""/judge 判定流水线测试：判定矩阵全分支 / 延迟窗口 / enabled 开关 / commit 联动。"""

from pathlib import Path

from fastapi.testclient import TestClient

from autocondense.digest import render_template


def _digest_ok(project="proj") -> str:
    md = render_template(project, "2026-10-22T12:00:00", "plan_done")
    md += "\n<!-- 填充：这是一段用于测试的摘要正文，" + "内容补充。" * 30 + " -->"
    assert len(md) >= 200
    return md


def _verdict(**kw):
    base = dict(task_finished=True, impact="low", impact_reason="已收尾，无易丢失上下文",
                safe_to_compress=True, reason="本批任务完成，适合压缩")
    base.update(kw)
    return base


def _candidate_session(client, cwd):
    """建会话并让计划全部完成 → 命中候选。"""
    client.post("/session/start", json={"session_id": "j1", "cwd": cwd})
    r = client.post("/report", json={
        "session_id": "j1", "cwd": cwd,
        "plan_items": [{"title": "任务A", "status": "completed"}]})
    return r.json()


def _big_transcript(tmp_path) -> str:
    # 14 万 CJK 字符 ≈ 14 万 token ≈ 水位 0.875（≥0.85 → urgent）
    p = tmp_path / "big.jsonl"
    p.write_text("字" * 140000 + "\n", encoding="utf-8")
    return str(p)


# ---------------------------------------------------------------------------
# 判定矩阵全分支
# ---------------------------------------------------------------------------

def test_judge_normal_low_finished_safe_approved(make_app, tmp_path):
    client = TestClient(make_app(tmp_path))
    cwd = str(tmp_path / "proj")
    d = _candidate_session(client, cwd)
    assert d["should"] == "JUDGE" and d["trigger"] == "plan_done"
    assert "judgment_form" in d and "fields" in d["judgment_form"]
    assert "condense_judge" in d["action_hint"]

    r = client.post("/judge", json={"session_id": "j1", "verdict": _verdict()})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["decision"] == "COMPRESS_NOW" and b["urgency"] == "normal"
    assert "condense_begin" in b["action_hint"] and "无需询问用户" in b["action_hint"]
    # APPROVED 记入会话状态
    st = client.get("/status").json()
    s1 = next(s for s in st["sessions"] if s["session_id"] == "j1")
    assert s1["last_judge"]["result"] == "APPROVED"
    assert s1["last_judge"]["verdict"]["impact"] == "low"


def test_judge_normal_medium_deferred(make_app, tmp_path):
    client = TestClient(make_app(tmp_path))
    cwd = str(tmp_path / "proj")
    _candidate_session(client, cwd)
    r = client.post("/judge", json={"session_id": "j1",
                                    "verdict": _verdict(impact="medium")})
    b = r.json()
    assert b["decision"] == "DEFERRED"
    assert "medium" in b["reason"] and "low" in b["reason"]
    assert b["action_hint"] == "继续当前工作，不要压缩，稍后自动重新评估"
    # 延迟窗口 = 当前轮次(0，report 不 bump) + judge_defer_turns(默认5)
    assert b["defer_until_turn"] == 0 + 5


def test_judge_normal_unfinished_deferred(make_app, tmp_path):
    client = TestClient(make_app(tmp_path))
    cwd = str(tmp_path / "proj")
    _candidate_session(client, cwd)
    r = client.post("/judge", json={"session_id": "j1",
                                    "verdict": _verdict(task_finished=False)})
    b = r.json()
    assert b["decision"] == "DEFERRED" and "任务尚未收尾" in b["reason"]


def test_judge_urgent_medium_unfinished_safe_approved(make_app, tmp_path):
    client = TestClient(make_app(tmp_path))
    cwd = str(tmp_path / "proj")
    big = _big_transcript(tmp_path)
    client.post("/session/start", json={"session_id": "u1", "cwd": cwd,
                                        "transcript_path": big})
    d = client.get("/check", params={"session_id": "u1"}).json()
    assert d["should"] == "JUDGE" and d["trigger"] == "water_level"
    assert d["urgency"] == "urgent"
    r = client.post("/judge", json={"session_id": "u1",
                                    "verdict": _verdict(task_finished=False,
                                                        impact="medium")})
    b = r.json()
    assert b["decision"] == "COMPRESS_NOW" and b["urgency"] == "urgent"


def test_judge_urgent_high_deferred(make_app, tmp_path):
    client = TestClient(make_app(tmp_path))
    cwd = str(tmp_path / "proj")
    big = _big_transcript(tmp_path)
    client.post("/session/start", json={"session_id": "u1", "cwd": cwd,
                                        "transcript_path": big})
    client.get("/check", params={"session_id": "u1"})
    r = client.post("/judge", json={"session_id": "u1",
                                    "verdict": _verdict(impact="high")})
    b = r.json()
    assert b["decision"] == "DEFERRED" and "high" in b["reason"]


def test_judge_unsafe_to_compress_deferred_both_urgencies(make_app, tmp_path):
    client = TestClient(make_app(tmp_path))
    cwd = str(tmp_path / "proj")
    _candidate_session(client, cwd)
    r = client.post("/judge", json={"session_id": "j1",
                                    "verdict": _verdict(safe_to_compress=False)})
    assert r.json()["decision"] == "DEFERRED"
    assert "未保存" in r.json()["reason"] or "易丢失" in r.json()["reason"]


# ---------------------------------------------------------------------------
# 入参校验
# ---------------------------------------------------------------------------

def test_judge_validation_400(make_app, tmp_path):
    client = TestClient(make_app(tmp_path))
    cwd = str(tmp_path / "proj")
    _candidate_session(client, cwd)

    # 缺 impact
    bad = _verdict()
    bad.pop("impact")
    r = client.post("/judge", json={"session_id": "j1", "verdict": bad})
    assert r.status_code == 400
    assert any("缺少 impact" in e for e in r.json()["errors"])

    # 非法 impact 取值
    r = client.post("/judge", json={"session_id": "j1",
                                    "verdict": _verdict(impact="critical")})
    assert r.status_code == 400
    assert any("low、medium 或 high" in e for e in r.json()["errors"])

    # 布尔字段类型错
    r = client.post("/judge", json={"session_id": "j1",
                                    "verdict": _verdict(task_finished="yes")})
    assert r.status_code == 400
    assert any("task_finished 应为布尔值" in e for e in r.json()["errors"])
    r = client.post("/judge", json={"session_id": "j1",
                                    "verdict": _verdict(safe_to_compress=1)})
    assert r.status_code == 400

    # 字符串字段类型错 / 缺失
    r = client.post("/judge", json={"session_id": "j1",
                                    "verdict": _verdict(impact_reason=5)})
    assert r.status_code == 400
    bad2 = _verdict()
    bad2.pop("reason")
    r = client.post("/judge", json={"session_id": "j1", "verdict": bad2})
    assert r.status_code == 400

    # verdict 缺失 / 非对象
    r = client.post("/judge", json={"session_id": "j1"})
    assert r.status_code == 400 and any("verdict" in e for e in r.json()["errors"])
    r = client.post("/judge", json={"session_id": "j1", "verdict": "yes"})
    assert r.status_code == 400

    # 会话标识缺失 / 未知
    r = client.post("/judge", json={"verdict": _verdict()})
    assert r.status_code == 400
    r = client.post("/judge", json={"session_id": "ghost", "verdict": _verdict()})
    assert r.status_code == 400 and "会话不存在" in r.json()["errors"][0]
    r = client.post("/judge", json={"cwd": str(tmp_path / "nowhere"),
                                    "verdict": _verdict()})
    assert r.status_code == 400 and "cwd" in r.json()["errors"][0]


def test_judge_resolves_session_by_cwd(make_app, tmp_path):
    client = TestClient(make_app(tmp_path))
    cwd = str(tmp_path / "proj")
    _candidate_session(client, cwd)
    r = client.post("/judge", json={"cwd": cwd, "verdict": _verdict()})
    assert r.status_code == 200 and r.json()["decision"] == "COMPRESS_NOW"


# ---------------------------------------------------------------------------
# 延迟窗口
# ---------------------------------------------------------------------------

def test_defer_window_blocks_then_recovers(make_app, tmp_path):
    client = TestClient(make_app(tmp_path))
    cwd = str(tmp_path / "proj")
    _candidate_session(client, cwd)  # turns=0（report 不 bump）
    client.post("/judge", json={"session_id": "j1",
                                "verdict": _verdict(impact="medium")})  # defer=5

    # 窗口内（turns 1..4 < 5）：/check 恒 NO 且说明延迟
    for _ in range(4):
        d = client.get("/check", params={"session_id": "j1"}).json()
        assert d["should"] == "NO" and "延迟" in d["reason"]
        assert "第 5 轮" in d["reason"]
    # 轮次推进到 5 → 恢复候选
    d = client.get("/check", params={"session_id": "j1"}).json()
    assert d["should"] == "JUDGE" and d["trigger"] == "plan_done"

    # status 可见延迟窗口
    st = client.get("/status").json()
    s1 = next(s for s in st["sessions"] if s["session_id"] == "j1")
    assert s1["defer_until_turn"] == 5
    assert s1["last_judge"]["result"] == "DEFERRED"


def test_urgent_bypasses_defer_window(make_app, tmp_path):
    client = TestClient(make_app(tmp_path))
    cwd = str(tmp_path / "proj")
    big = _big_transcript(tmp_path)
    client.put("/config", json={"turns_threshold": 1})
    client.post("/session/start", json={"session_id": "j1", "cwd": cwd})
    d = client.get("/check", params={"session_id": "j1"}).json()  # turns=1 → 候选(normal)
    assert d["should"] == "JUDGE" and d["urgency"] == "normal" and d["trigger"] == "turns"
    client.post("/judge", json={"session_id": "j1",
                                "verdict": _verdict(impact="medium")})  # defer=1+5=6
    # 窗口内且 normal → NO
    d = client.get("/check", params={"session_id": "j1"}).json()  # turns=2 < 6
    assert d["should"] == "NO" and "延迟" in d["reason"]
    # 水位冲高 → urgency=urgent 无视窗口立即重新判定
    client.post("/session/start", json={"session_id": "j1", "cwd": cwd,
                                        "transcript_path": big})
    d = client.get("/check", params={"session_id": "j1"}).json()  # turns=3 < 6 但 urgent
    assert d["should"] == "JUDGE" and d["urgency"] == "urgent"


def test_commit_clears_defer_window(make_app, tmp_path):
    client = TestClient(make_app(tmp_path))
    cwd = str(tmp_path / "proj")
    _candidate_session(client, cwd)
    client.post("/judge", json={"session_id": "j1",
                                "verdict": _verdict(impact="medium")})
    # 窗口内换个判定通过 → 压缩 → 窗口清空
    r = client.post("/judge", json={"session_id": "j1", "verdict": _verdict()})
    assert r.json()["decision"] == "COMPRESS_NOW"
    client.post("/begin", json={"session_id": "j1", "cwd": cwd})
    r = client.post("/commit", json={"session_id": "j1", "cwd": cwd,
                                     "digest_md": _digest_ok()})
    assert r.status_code == 200
    st = client.get("/status").json()
    s1 = next(s for s in st["sessions"] if s["session_id"] == "j1")
    assert s1["defer_until_turn"] is None
    assert s1["last_judge"]["result"] == "APPROVED"


# ---------------------------------------------------------------------------
# enabled 总开关
# ---------------------------------------------------------------------------

def test_enabled_false_disables_everything(make_app, tmp_path):
    client = TestClient(make_app(tmp_path))
    cwd = str(tmp_path / "proj")
    r = client.put("/config", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False

    # /check 恒 NO
    d = _candidate_session(client, cwd)  # report 走同一流水线
    assert d["should"] == "NO"
    assert d["reason"] == "插件已关闭（enabled=false），不做任何压缩"
    d = client.get("/check", params={"session_id": "j1"}).json()
    assert d["should"] == "NO"

    # Stop hook 不产生压缩指令
    r = client.post("/events/hook", json={"event": "Stop", "session_id": "j1",
                                          "cwd": cwd})
    b = r.json()
    assert b["should"] == "NO" and b["instruction"] is None

    # /judge 也被关闭挡住
    r = client.post("/judge", json={"session_id": "j1", "verdict": _verdict()})
    assert r.status_code == 200
    assert r.json()["decision"] == "NO" and "已关闭" in r.json()["reason"]

    # 重新打开 → 恢复候选
    client.put("/config", json={"enabled": True})
    d = client.get("/check", params={"session_id": "j1"}).json()
    assert d["should"] == "JUDGE"


# ---------------------------------------------------------------------------
# /commit 分段写入端到端（SME stub）
# ---------------------------------------------------------------------------

def test_commit_segments_e2e(make_app, tmp_path, sme_stub):
    stub, base = sme_stub
    client = TestClient(make_app(tmp_path, sme_base_url=base))
    cwd = str(tmp_path / "proj")
    _candidate_session(client, cwd)
    r = client.post("/judge", json={"session_id": "j1", "verdict": _verdict()})
    assert r.json()["decision"] == "COMPRESS_NOW"
    client.post("/begin", json={"session_id": "j1", "cwd": cwd})
    r = client.post("/commit", json={"session_id": "j1", "cwd": cwd,
                                     "digest_md": _digest_ok()})
    assert r.status_code == 200, r.text
    c = r.json()
    # 分段写入统计（≥2 段走 batch：stored=added，memory_ids 空）
    assert c["segments_written"] >= 5
    assert c["sme_segments"]["stored"] == c["segments_written"]
    assert c["sme_segments"]["failed"] == 0
    assert c["sme_segments"]["memory_ids"] == []
    # 聚合契约：批量模式 sme.memory_id 为 null（batch 不返回各条 id，索引不回填）
    assert c["sme"]["stored"] is True and c["sme"]["memory_id"] is None
    assert Path(c["snapshot_path"]).is_file()
    # stub 收到的是分段而非整篇
    assert len(stub.memories) == c["segments_written"]
    assert all(m["text"].startswith("[autocondense|proj|") for m in stub.memories)
    types = {m["metadata"]["segment_type"] for m in stub.memories}
    assert {"goal", "accomplishment", "todo", "restore_pointer"} <= types
    # 批量无 id → 索引不回填
    res = client.get("/restore", params={"cwd": cwd}).json()
    assert res["snapshots"][0]["sme_memory_id"] is None
    # 事件含分段统计
    st = client.get("/status").json()
    ev = next(e for e in st["recent_events"] if e["event"] == "commit")
    assert ev["data"]["segments_written"] == c["segments_written"]
    assert ev["data"]["sme_stored"] is True


def test_commit_split_disabled_falls_back_whole(make_app, tmp_path, sme_stub):
    stub, base = sme_stub
    client = TestClient(make_app(tmp_path, sme_base_url=base))
    cwd = str(tmp_path / "proj")
    client.put("/config", json={"sme": {"split_segments": False}})
    client.post("/session/start", json={"session_id": "s1", "cwd": cwd})
    r = client.post("/commit", json={"session_id": "s1", "cwd": cwd,
                                     "digest_md": _digest_ok()})
    c = r.json()
    assert c["segments_written"] == 0 and c["sme_segments"] is None
    assert c["sme"]["stored"] is True  # 整篇兜底
    assert len(stub.memories) == 1
    assert stub.memories[0]["text"].startswith("[会话快照|proj|")


def test_commit_segments_all_fail_falls_back_whole(make_app, tmp_path, sme_stub):
    stub, base = sme_stub
    stub.fail_markers = ["[autocondense|"]  # 所有分段都 500 → 兜底整篇
    client = TestClient(make_app(tmp_path, sme_base_url=base))
    cwd = str(tmp_path / "proj")
    client.post("/session/start", json={"session_id": "s1", "cwd": cwd})
    r = client.post("/commit", json={"session_id": "s1", "cwd": cwd,
                                     "digest_md": _digest_ok()})
    assert r.status_code == 200
    c = r.json()
    assert c["segments_written"] == 0
    assert c["sme"]["stored"] is True and len(stub.memories) == 1
    assert stub.memories[0]["text"].startswith("[会话快照|proj|")


# ---------------------------------------------------------------------------
# 优化 7：DEFERRED 指数退避 + confidence + rounds_to_full
# ---------------------------------------------------------------------------

def test_defer_backoff_progression_5_10_20_capped(make_app, tmp_path):
    client = TestClient(make_app(tmp_path))
    cwd = str(tmp_path / "proj")
    _candidate_session(client, cwd)  # turns=0
    # 连续 4 次 DEFERRED：5 → 10 → 20 → 20（封顶 judge_defer_max_turns=20）
    expect = [5, 10, 20, 20]
    for i, step in enumerate(expect):
        r = client.post("/judge", json={"session_id": "j1",
                                        "verdict": _verdict(impact="medium")})
        b = r.json()
        assert b["decision"] == "DEFERRED"
        assert b["defer_until_turn"] == 0 + step, f"第 {i + 1} 次退避应为 {step}"
        assert b["defer_streak"] == i + 1
    st = client.get("/status").json()
    s1 = next(s for s in st["sessions"] if s["session_id"] == "j1")
    assert s1["defer_streak"] == 4 and s1["defer_until_turn"] == 20
    assert st["judge_defer_max_turns"] == 20


def test_defer_backoff_reset_on_approve(make_app, tmp_path):
    client = TestClient(make_app(tmp_path))
    cwd = str(tmp_path / "proj")
    _candidate_session(client, cwd)
    # 两次 DEFERRED（streak=2）→ 判定通过归零 → 再次 DEFERRED 回到基础步长 5
    client.post("/judge", json={"session_id": "j1", "verdict": _verdict(impact="medium")})
    client.post("/judge", json={"session_id": "j1", "verdict": _verdict(impact="medium")})
    r = client.post("/judge", json={"session_id": "j1", "verdict": _verdict()})
    assert r.json()["decision"] == "COMPRESS_NOW"
    st = client.get("/status").json()
    s1 = next(s for s in st["sessions"] if s["session_id"] == "j1")
    assert s1["defer_streak"] == 0
    r = client.post("/judge", json={"session_id": "j1", "verdict": _verdict(impact="medium")})
    assert r.json()["defer_until_turn"] == 0 + 5  # 重新从头退避


def test_judge_confidence_optional_field(make_app, tmp_path):
    client = TestClient(make_app(tmp_path))
    cwd = str(tmp_path / "proj")
    _candidate_session(client, cwd)
    # 合法值 → 记入 last_judge 与 judge 事件
    r = client.post("/judge", json={"session_id": "j1",
                                    "verdict": _verdict(confidence="high")})
    assert r.status_code == 200 and r.json()["decision"] == "COMPRESS_NOW"
    st = client.get("/status").json()
    s1 = next(s for s in st["sessions"] if s["session_id"] == "j1")
    assert s1["last_judge"]["verdict"]["confidence"] == "high"
    ev = next(e for e in st["recent_events"] if e["event"] == "judge")
    assert ev["data"]["confidence"] == "high"
    # 非法取值 / 类型错 → 400 中文报错；不传 → 照常
    r = client.post("/judge", json={"session_id": "j1",
                                    "verdict": _verdict(confidence="certain")})
    assert r.status_code == 400
    assert any("confidence 只能是 high、medium 或 low" in e for e in r.json()["errors"])
    r = client.post("/judge", json={"session_id": "j1",
                                    "verdict": _verdict(confidence=0.9)})
    assert r.status_code == 400
    assert any("confidence 应为字符串" in e for e in r.json()["errors"])
    r = client.post("/judge", json={"session_id": "j1", "verdict": _verdict()})
    assert r.status_code == 200


def test_rounds_to_full_in_candidate_and_judge(make_app, tmp_path):
    client = TestClient(make_app(tmp_path))
    cwd = str(tmp_path / "proj")
    # 单次上报：采样不足 → 候选响应 rounds_to_full 为 null
    client.post("/session/start", json={"session_id": "r1", "cwd": cwd,
                                        "context_tokens": 10000})
    d = client.post("/report", json={
        "session_id": "r1", "cwd": cwd,
        "plan_items": [{"title": "任务A", "status": "completed"}]}).json()
    assert d["should"] == "JUDGE" and d["rounds_to_full"] is None
    assert d["signals"]["token_source"] == "reported"
    # 两次 Stop 上报（turn0 10k→60k、turn1 110k，avg=100000/轮）→ 距 95% 水位 1 轮
    client.post("/events/hook", json={"event": "Stop", "session_id": "r1",
                                      "cwd": cwd, "context_tokens": 60000})
    client.post("/events/hook", json={"event": "Stop", "session_id": "r1",
                                      "cwd": cwd, "context_tokens": 110000})
    r = client.post("/judge", json={"session_id": "r1", "verdict": _verdict()})
    b = r.json()
    assert b["decision"] == "COMPRESS_NOW"
    assert b["rounds_to_full"] == 1
    assert b["signals"]["token_source"] == "reported"
