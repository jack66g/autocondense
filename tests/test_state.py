"""state 模块测试：hash 稳定 / 快照与索引 / 损坏容忍 / 事件 / 计划合并 / 判定状态。"""

import json
from datetime import datetime
from pathlib import Path

from autocondense.config import Config
from autocondense.state import StateStore, project_hash, project_name


def test_project_hash_stable(tmp_path):
    cwd = str(tmp_path)
    h1 = project_hash(cwd)
    assert len(h1) == 10
    # 不同书写形式同一项目 → 同一 hash
    assert project_hash(cwd + "\\") == h1
    assert project_hash(str(Path(cwd) / ".")) == h1
    assert project_name(cwd) == tmp_path.name
    # 不同项目 → 不同 hash
    assert project_hash(str(tmp_path / "other")) != h1


def test_project_hash_case_insensitive_on_windows(tmp_path):
    cwd = str(tmp_path)
    if any(c.isupper() for c in cwd):
        assert project_hash(cwd.lower()) == project_hash(cwd)


def test_snapshot_write_and_index(tmp_path):
    store = StateStore(tmp_path, Config())
    store.upsert_session("s1", cwd=str(tmp_path))
    digest = "# 快照标题\n\n正文内容"
    path, entry = store.save_snapshot(digest, "plan_done", "s1")
    h = project_hash(str(tmp_path))
    assert path.parent == tmp_path / "snapshots" / h
    assert path.is_file()
    assert path.name.endswith("-plan_done.md")
    assert entry["title"] == "# 快照标题"
    assert entry["char_count"] == len(digest)
    assert entry["trigger"] == "plan_done"
    assert entry["session_id"] == "s1"
    assert entry["sme_memory_id"] is None
    snaps = store.list_snapshots(h, 10)
    assert len(snaps) == 1 and snaps[0]["file"] == str(path)
    # save_snapshot 追加 condense 事件
    assert any(e["event"] == "condense" for e in store.recent_events(10))
    # 索引可更新 sme_memory_id
    assert store.set_snapshot_memory_id(h, str(path), "mem-9") is True
    assert store.list_snapshots(h)[0]["sme_memory_id"] == "mem-9"


def test_list_snapshots_order_and_top_n(tmp_path):
    store = StateStore(tmp_path, Config())
    store.upsert_session("s1", cwd=str(tmp_path))
    store.save_snapshot("# 一", "t1", "s1")
    store.save_snapshot("# 二", "t2", "s1")
    store.save_snapshot("# 三", "t3", "s1")
    snaps = store.list_snapshots(project_hash(str(tmp_path)))
    assert len(snaps) == 3
    assert snaps[0]["trigger"] == "t3"  # 最新在前
    assert len(store.list_snapshots(project_hash(str(tmp_path)), 2)) == 2


def test_sessions_state_corrupt_tolerant(tmp_path):
    (tmp_path / "sessions-state.json").write_text("{bad", encoding="utf-8")
    store = StateStore(tmp_path, Config())
    assert store.get_session("s1") is None
    assert list(tmp_path.glob("sessions-state.json.corrupt-*"))


def test_persistence_roundtrip(tmp_path):
    store = StateStore(tmp_path, Config())
    store.upsert_session("s1", cwd=str(tmp_path))
    store.bump_turn("s1")
    store.update_plan("s1", [{"title": "A", "status": "completed"}])
    store2 = StateStore(tmp_path, Config())
    s = store2.get_session("s1")
    assert s is not None
    assert s.turns == 1
    assert s.plan_items[0]["status"] == "completed"


def test_events_append_and_recent(tmp_path):
    store = StateStore(tmp_path, Config())
    store.append_event("session_start", "s1", "C:/p", {"a": 1})
    store.append_event("stop", "s1", "C:/p", {})
    evs = store.recent_events(1)
    assert len(evs) == 1 and evs[0]["event"] == "stop"
    assert evs[0]["session_id"] == "s1" and evs[0]["cwd"] == "C:/p"
    assert [e["event"] for e in store.recent_events(10)] == ["session_start", "stop"]
    # 坏行跳过
    with open(tmp_path / "sessions.jsonl", "a", encoding="utf-8") as f:
        f.write("not-json\n")
    assert len(store.recent_events(10)) == 2


def test_plan_merge_semantics(tmp_path):
    store = StateStore(tmp_path, Config())
    store.upsert_session("s1", cwd=str(tmp_path))
    for _ in range(3):
        store.bump_turn("s1")
    # 新条目记 first_seen_turn=当前 turns
    store.update_plan("s1", [{"title": "A", "status": "pending"}])
    s = store.get_session("s1")
    assert s.plan_items[0]["first_seen_turn"] == 3
    assert s.plan_items[0]["completed_turn"] is None
    # 状态变 completed 记 completed_turn
    store.update_plan("s1", [{"title": "A", "status": "completed"},
                             {"title": "B", "status": "pending"}])
    s = store.get_session("s1")
    a = next(i for i in s.plan_items if i["title"] == "A")
    assert a["status"] == "completed"
    assert a["completed_turn"] == 3
    assert a["first_seen_turn"] == 3  # first_seen 不被覆盖
    # 消失的 completed 条目 → 归档；pending 消失 → 直接丢弃
    store.update_plan("s1", [{"title": "B", "status": "in_progress"}])
    s = store.get_session("s1")
    assert [i["title"] for i in s.plan_items] == ["B"]
    arch = [i for i in s.plan_archive if i["title"] == "A"]
    assert len(arch) == 1 and arch[0]["completed_turn"] == 3
    # 初见即完成（done 大小写兼容在 normalize 层，这里直接给 completed）
    store.update_plan("s1", [{"title": "B", "status": "in_progress"},
                             {"title": "C", "status": "completed"}])
    s = store.get_session("s1")
    c = next(i for i in s.plan_items if i["title"] == "C")
    assert c["first_seen_turn"] == 3 and c["completed_turn"] == 3


def test_clear_completed_keeps_pending(tmp_path):
    store = StateStore(tmp_path, Config())
    store.upsert_session("s1", cwd=str(tmp_path))
    store.update_plan("s1", [{"title": "A", "status": "completed"},
                             {"title": "B", "status": "pending"},
                             {"title": "C", "status": "in_progress"}])
    store.clear_completed_plan_items("s1")
    s = store.get_session("s1")
    assert [i["title"] for i in s.plan_items] == ["B", "C"]
    # 压缩消费掉的完成项不进归档（避免冷却后重复触发 task_done）
    assert s.plan_archive == []


def test_mark_condensed(tmp_path):
    store = StateStore(tmp_path, Config())
    store.upsert_session("s1", cwd=str(tmp_path))
    store.update_plan("s1", [{"title": "A", "status": "completed"}])
    store.bump_turn("s1")
    store.bump_turn("s1")
    store.set_condensing("s1", True)
    store.mark_condensed("s1", "plan_done")
    s = store.get_session("s1")
    assert s.turns_since_condense == 0
    assert s.last_trigger == "plan_done"
    assert s.condensing is False
    assert s.last_condense_at is not None
    assert datetime.fromisoformat(s.cooldown_until) > datetime.now()
    # 归档被清空
    assert s.plan_archive == []


def test_judge_record_and_defer_window(tmp_path):
    store = StateStore(tmp_path, Config())
    store.upsert_session("s1", cwd=str(tmp_path))
    store.bump_turn("s1")  # turns=1
    store.bump_turn("s1")  # turns=2
    verdict = {"task_finished": False, "impact": "medium",
               "impact_reason": "还有收尾", "safe_to_compress": True, "reason": "测试"}
    s = store.record_judge("s1", verdict, "DEFERRED")
    assert s.last_judge["turn"] == 2
    assert s.last_judge["result"] == "DEFERRED"
    assert s.last_judge["verdict"] == verdict
    assert s.last_judge["ts"]

    store.set_defer_until_turn("s1", 2 + 5)
    s = store.get_session("s1")
    assert s.defer_until_turn == 7
    # summary 暴露判定摘要与延迟窗口
    assert s.summary()["defer_until_turn"] == 7
    assert s.summary()["last_judge"]["result"] == "DEFERRED"

    # 压缩 commit 成功后延迟窗口清空（last_judge 保留作历史）
    store.mark_condensed("s1", "plan_done")
    s = store.get_session("s1")
    assert s.defer_until_turn is None
    assert s.last_judge is not None

    # APPROVED 记录覆盖
    store.record_judge("s1", dict(verdict, task_finished=True), "APPROVED")
    assert store.get_session("s1").last_judge["result"] == "APPROVED"


def test_judge_fields_persisted(tmp_path):
    store = StateStore(tmp_path, Config())
    store.upsert_session("s1", cwd=str(tmp_path))
    store.bump_turn("s1")
    store.record_judge("s1", {"impact": "low"}, "DEFERRED")
    store.set_defer_until_turn("s1", 6)
    store2 = StateStore(tmp_path, Config())
    s = store2.get_session("s1")
    assert s.defer_until_turn == 6
    assert s.last_judge["result"] == "DEFERRED"
    assert s.last_judge["verdict"] == {"impact": "low"}


def test_judge_unknown_session_noop(tmp_path):
    store = StateStore(tmp_path, Config())
    assert store.record_judge("ghost", {}, "APPROVED") is None
    assert store.set_defer_until_turn("ghost", 3) is None


def test_legacy_session_state_with_mode_tolerated(tmp_path):
    """旧 sessions-state.json 含 mode 键：加载时被丢弃，不崩。"""
    (tmp_path / "sessions-state.json").write_text(
        json.dumps({"version": 1, "sessions": {
            "s1": {"session_id": "s1", "cwd": "C:/p", "mode": "auto", "turns": 3}}}),
        encoding="utf-8")
    store = StateStore(tmp_path, Config())
    s = store.get_session("s1")
    assert s is not None and s.turns == 3
    assert not hasattr(s, "mode")


def test_token_samples_recorded_and_capped(tmp_path):
    """带 context_tokens 的 upsert 记录采样；只保留最近 10 条并持久化。"""
    store = StateStore(tmp_path, Config())
    store.upsert_session("s1", cwd=str(tmp_path))
    for i in range(12):
        store.upsert_session("s1", context_tokens=1000 + i)
        store.bump_turn("s1")
    s = store.get_session("s1")
    assert len(s.token_samples) == 10          # 上限 10
    assert s.token_samples[-1]["tokens"] == 1011
    assert s.context_tokens == 1011 and s.context_tokens_turn == 11
    # 持久化往返
    store2 = StateStore(tmp_path, Config())
    s2 = store2.get_session("s1")
    assert s2.token_samples == s.token_samples and s2.context_tokens_turn == 11


def test_mark_activity_and_defer_streak_and_begin_mode(tmp_path):
    store = StateStore(tmp_path, Config())
    store.upsert_session("s1", cwd=str(tmp_path))
    store.bump_turn("s1")  # turns=1
    store.mark_activity("s1", "file")
    store.mark_activity("s1", "fail")
    s = store.get_session("s1")
    assert s.last_file_turn == 1 and s.last_fail_turn == 1
    assert s.summary()["last_file_turn"] == 1 and s.summary()["defer_streak"] == 0
    # 未知会话 no-op
    assert store.mark_activity("ghost", "file") is None
    store.set_defer_streak("s1", 3)
    store.set_begin_mode("s1", "delta")
    assert store.get_session("s1").defer_streak == 3
    assert store.get_session("s1").begin_mode == "delta"
    # mark_condensed 一并清空退避计数与摘要模式
    store.mark_condensed("s1", "plan_done")
    s = store.get_session("s1")
    assert s.defer_streak == 0 and s.begin_mode is None
    assert store.set_defer_streak("ghost", 1) is None
    assert store.set_begin_mode("ghost", "full") is None
