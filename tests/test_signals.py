"""signals 模块测试：七条判定顺序逐一覆盖 + 候选制/urgency + collect/估算。"""

from datetime import datetime, timedelta

from autocondense.config import Config
from autocondense.signals import (URGENT_WATER, collect, decide,
                                  estimate_transcript_tokens, urgency_of)
from autocondense.state import SessionState


def make_state(**kw) -> SessionState:
    base = dict(session_id="s", cwd="C:/p")
    base.update(kw)
    return SessionState(**base)


def sig(**kw):
    base = dict(plan_total=0, plan_completed=0, plan_in_progress=0,
                turns_since_condense=0, minutes_since_condense=None,
                est_tokens=None, water_level=None, cooldown_active=False)
    base.update(kw)
    return base


def test_rule1_in_progress_blocks_everything():
    st = make_state()
    s = sig(plan_in_progress=1, water_level=0.99, turns_since_condense=999,
            plan_total=2, plan_completed=1)
    d = decide(st, s, Config())
    assert d["should"] == "NO" and d["trigger"] is None
    assert "进行中" in d["reason"]


def test_rule2_cooldown_blocks_triggers():
    st = make_state(cooldown_until=(datetime.now() + timedelta(minutes=10)).isoformat())
    s = sig(cooldown_active=True, plan_total=2, plan_completed=2, water_level=0.9)
    d = decide(st, s, Config(), now=datetime.now())
    assert d["should"] == "NO"
    assert "冷却" in d["reason"] and "10" in d["reason"]


def test_rule3_all_plan_done():
    st = make_state()
    d = decide(st, sig(plan_total=3, plan_completed=3), Config())
    assert d["should"] == "CANDIDATE" and d["trigger"] == "plan_done"
    assert d["urgency"] == "normal"


def test_rule4_long_task_done_current():
    st = make_state(plan_items=[
        {"title": "T", "status": "completed", "first_seen_turn": 1, "completed_turn": 9},
        {"title": "P", "status": "pending", "first_seen_turn": 1, "completed_turn": None},
    ])
    d = decide(st, sig(plan_total=2, plan_completed=1, plan_in_progress=0), Config())
    assert d["should"] == "CANDIDATE" and d["trigger"] == "task_done"


def test_rule4_long_task_done_from_archive():
    # 条目已从计划列表消失（智能体清理），但归档保留跨度
    st = make_state(plan_items=[],
                    plan_archive=[{"title": "T", "first_seen_turn": 1, "completed_turn": 9}])
    d = decide(st, sig(plan_total=0), Config())
    assert d["should"] == "CANDIDATE" and d["trigger"] == "task_done"


def test_rule4_span_too_small_falls_through():
    # 跨度不足（2-1=1 < 5）且还有 pending 未完成 → 不触发，落到最后 NO
    st = make_state(plan_items=[
        {"title": "T", "status": "completed", "first_seen_turn": 1, "completed_turn": 2},
        {"title": "P", "status": "pending", "first_seen_turn": 1, "completed_turn": None},
    ])
    d = decide(st, sig(plan_total=2, plan_completed=1), Config())
    assert d["should"] == "NO" and d["trigger"] is None


def test_rule5_water_level():
    st = make_state()
    d = decide(st, sig(water_level=0.75), Config())
    assert d["should"] == "CANDIDATE" and d["trigger"] == "water_level"
    assert d["urgency"] == "normal"  # 0.75 < 0.85
    # 恰好等于阈值也触发
    d2 = decide(st, sig(water_level=0.7), Config())
    assert d2["should"] == "CANDIDATE"
    # 低于阈值不触发
    d3 = decide(st, sig(water_level=0.69), Config())
    assert d3["should"] == "NO"
    # 危急水位 → urgent
    d4 = decide(st, sig(water_level=URGENT_WATER), Config())
    assert d4["should"] == "CANDIDATE" and d4["urgency"] == "urgent"


def test_rule6_turns_candidate():
    st = make_state()
    d = decide(st, sig(turns_since_condense=25), Config())
    assert d["should"] == "CANDIDATE" and d["trigger"] == "turns"
    d2 = decide(st, sig(turns_since_condense=24), Config())
    assert d2["should"] == "NO"


def test_rule7_no_signal():
    d = decide(make_state(), sig(), Config())
    assert d["should"] == "NO" and d["trigger"] is None and d["action_hint"] is None
    assert d["urgency"] == "normal"


# ---------------------------------------------------------------------------
# 优化 2：token 口径真实化（reported/estimated）与 rounds_to_full
# ---------------------------------------------------------------------------

def test_collect_reported_fresh_wins_over_estimate(tmp_path):
    f = tmp_path / "t.jsonl"
    f.write_text("你好世界\nabcdef\n", encoding="utf-8")  # 估算 6 token
    st = make_state(transcript_path=str(f), context_tokens=80000,
                    context_tokens_turn=5, turns=8,
                    token_samples=[{"turn": 5, "tokens": 80000}])
    s = collect(st, Config())
    assert s["est_tokens"] == 80000 and s["token_source"] == "reported"
    assert s["water_level"] == round(80000 / 160000, 4)


def test_collect_reported_stale_falls_back_to_estimate(tmp_path):
    f = tmp_path / "t.jsonl"
    f.write_text("你好世界\nabcdef\n", encoding="utf-8")
    # 上报已 11 轮未更新（> 10 轮保鲜期）→ 回落 transcript 估算
    st = make_state(transcript_path=str(f), context_tokens=80000,
                    context_tokens_turn=0, turns=11,
                    token_samples=[{"turn": 0, "tokens": 80000}])
    s = collect(st, Config())
    assert s["est_tokens"] == 6 and s["token_source"] == "estimated"
    assert s["water_level"] == round(6 / 160000, 4)


def test_collect_no_transcript_stale_reported_still_used():
    # 无 transcript 可估：过期的上报值聊胜于无，仍按 reported 口径
    st = make_state(context_tokens=80000, context_tokens_turn=0, turns=50)
    s = collect(st, Config())
    assert s["est_tokens"] == 80000 and s["token_source"] == "reported"


def test_rounds_to_full_from_samples():
    st = make_state(context_tokens=110000, context_tokens_turn=10, turns=10,
                    token_samples=[{"turn": 0, "tokens": 10000},
                                   {"turn": 10, "tokens": 110000}])
    s = collect(st, Config())
    # avg=10000/轮，剩余 160000*0.95-110000=42000 → ceil(4.2)=5
    assert s["rounds_to_full"] == 5
    # 已越线（> 95%）→ 0
    st2 = make_state(context_tokens=200000, context_tokens_turn=10, turns=10,
                     token_samples=[{"turn": 0, "tokens": 10000},
                                    {"turn": 10, "tokens": 200000}])
    assert collect(st2, Config())["rounds_to_full"] == 0


def test_rounds_to_full_none_cases():
    # 无采样 → None
    assert collect(make_state(context_tokens=1000), Config())["rounds_to_full"] is None
    # 采样不足（轮差 0）→ None
    st = make_state(context_tokens=1000, context_tokens_turn=0, turns=0,
                    token_samples=[{"turn": 0, "tokens": 1000},
                                   {"turn": 0, "tokens": 1200}])
    assert collect(st, Config())["rounds_to_full"] is None
    # 每轮增量为负（上下文反而变小）→ None，绝不瞎猜
    st2 = make_state(context_tokens=1000, context_tokens_turn=5, turns=5,
                     token_samples=[{"turn": 0, "tokens": 5000},
                                    {"turn": 5, "tokens": 1000}])
    assert collect(st2, Config())["rounds_to_full"] is None


# ---------------------------------------------------------------------------
# 优化 6：idle 候选
# ---------------------------------------------------------------------------

def test_idle_candidate_when_no_activity():
    st = make_state(turns=6, last_file_turn=3)
    d = decide(st, sig(idle_rounds=3), Config())
    assert d["should"] == "CANDIDATE" and d["trigger"] == "idle"
    assert "3 轮无文件修改与失败" in d["reason"]


def test_idle_below_threshold_or_missing_records_no_trigger():
    st = make_state(turns=6, last_file_turn=4)
    assert decide(st, sig(idle_rounds=2), Config())["should"] == "NO"
    # 活动记录缺失（老会话/hook 未生效）→ idle_rounds=None，绝不误报
    st2 = make_state(turns=100)
    assert decide(st2, sig(idle_rounds=None, turns_since_condense=10),
                  Config())["should"] == "NO"


def test_idle_order_between_water_and_turns():
    # 水位优先于 idle
    st = make_state(turns=6, last_file_turn=0)
    d = decide(st, sig(idle_rounds=6, water_level=0.8), Config())
    assert d["trigger"] == "water_level"
    # idle 优先于轮次
    cfg = Config(turns_threshold=3)
    d2 = decide(st, sig(idle_rounds=6, turns_since_condense=9), cfg)
    assert d2["trigger"] == "idle"


def test_collect_idle_rounds_from_state():
    st = make_state(turns=10, last_file_turn=2, last_fail_turn=7)
    s = collect(st, Config())
    assert s["idle_rounds"] == 3  # turns - max(2, 7)
    assert collect(make_state(turns=10), Config())["idle_rounds"] is None


def test_candidate_action_hint_no_user_asking():
    st = make_state()
    d = decide(st, sig(plan_total=1, plan_completed=1), Config())
    assert d["action_hint"]
    assert "condense_judge" in d["action_hint"]
    assert "不要询问用户" in d["action_hint"]


def test_urgency_of():
    assert urgency_of({"water_level": 0.85}) == "urgent"
    assert urgency_of({"water_level": 0.99}) == "urgent"
    assert urgency_of({"water_level": 0.84}) == "normal"
    assert urgency_of({"water_level": None}) == "normal"
    assert urgency_of({}) == "normal"


def test_estimate_transcript_tokens(tmp_path):
    assert estimate_transcript_tokens(None) is None
    assert estimate_transcript_tokens(str(tmp_path / "nope.jsonl")) is None
    f = tmp_path / "t.jsonl"
    # 4 个 CJK + 1 换行 → 4 + 0.25；6 个字母 + 1 换行 → 1.75；合计 6.0
    f.write_text("你好世界\nabcdef\n", encoding="utf-8")
    assert estimate_transcript_tokens(str(f)) == 6


def test_collect(tmp_path):
    f = tmp_path / "t.jsonl"
    f.write_text("你好世界\nabcdef\n", encoding="utf-8")
    st = make_state(transcript_path=str(f))
    s = collect(st, Config())
    assert s["est_tokens"] == 6
    assert s["water_level"] == round(6 / 160000, 4)
    assert s["minutes_since_condense"] is None
    assert s["cooldown_active"] is False
    assert s["urgency"] == "normal"
    assert "mode" not in s  # 双模式已删除
    assert s["plan_total"] == 0 and s["plan_completed"] == 0 and s["plan_in_progress"] == 0

    # 无 transcript → 回退 context_tokens
    st2 = make_state(context_tokens=8000)
    s2 = collect(st2, Config())
    assert s2["est_tokens"] == 8000
    assert s2["water_level"] == round(8000 / 160000, 4)

    # 冷却与时间
    now = datetime.now()
    st3 = make_state(last_condense_at=(now - timedelta(minutes=5)).isoformat(timespec="seconds"),
                     cooldown_until=(now + timedelta(minutes=25)).isoformat(timespec="seconds"))
    s3 = collect(st3, Config())
    assert s3["cooldown_active"] is True
    assert 4 <= s3["minutes_since_condense"] <= 6

    # 危急水位 → urgency=urgent
    st4 = make_state(context_tokens=160000)
    assert collect(st4, Config())["urgency"] == "urgent"
