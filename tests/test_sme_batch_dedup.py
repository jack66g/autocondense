"""优化 3+4：SME 批量提交（/memories/batch）与段去重/强化测试。"""

from fastapi.testclient import TestClient

from autocondense.digest import Segment, split_digest
from autocondense.sme_bridge import SMEBridge

BASE_TAGS = ["project", "todo", "session_digest"]

MD = """# 会话快照 · proj · 2026-10-23T10:00:00 · 触发:plan_done

## 目标（用户最终想要什么）

- 把分段入库改成批量提交

## 关键决策与理由（含被否决的方案）

- 决策：≥2 段先走 /memories/batch，失败回落逐条

## 已完成（每项注明验证方式：测试通过/编译通过/用户确认）

- 批量路径：测试通过

## 未完成/进行中（精确到下一个动作；无则写「（无）」）

- 接入去重：下一步改 commit

## 文件变更清单（关键改动 file:line）

- sme_bridge.py:1

## 踩过的坑与已排除的方向

- 坑：batch 不返回各条 id

## 恢复指针（transcript 路径、更早快照路径、SME 检索关键词）

- transcript: C:/t.jsonl
"""


def _segs(md=MD):
    return split_digest(md, BASE_TAGS, "proj", "2026-10-23T10:00:00", "plan_done")


def _digest_ok(marker="proj"):
    from autocondense.digest import render_template
    md = render_template(marker, "2026-10-23T12:00:00", "plan_done")
    md += "\n<!-- 填充：这是一段用于测试的摘要正文，" + "内容补充。" * 30 + " -->"
    return md


# ---------------------------------------------------------------------------
# 优化 3：批量提交
# ---------------------------------------------------------------------------

def test_batch_success_one_round_trip(sme_stub):
    stub, base = sme_stub
    bridge = SMEBridge(base, timeout_s=2.0)
    segs = _segs()
    r = bridge.store_segments(segs, {"session_id": "s1", "project": "proj",
                                     "trigger": "plan_done"})
    assert stub.batch_calls == 1          # 一次往返
    assert r["stored"] == len(segs) and r["failed"] == 0
    assert r["memory_ids"] == []          # batch 不返回各条 id
    assert r["errors"] == []
    assert len(stub.memories) == len(segs)
    # 逐条 /memories 没被调用过（全部经 batch 入库，id 连续即可证明来源一致）
    for i, m in enumerate(stub.memories):
        assert m["text"] == segs[i].text
        assert m["metadata"]["segment_index"] == i


def test_batch_404_falls_back_per_item_with_ids(sme_stub):
    stub, base = sme_stub
    stub.reject_batch = True
    bridge = SMEBridge(base, timeout_s=2.0)
    segs = _segs()
    r = bridge.store_segments(segs, {"session_id": "s1", "project": "proj",
                                     "trigger": "plan_done"})
    assert stub.batch_calls == 1
    assert r["stored"] == len(segs) and r["failed"] == 0
    # 回落逐条才有 id
    assert r["memory_ids"] == [f"mem-{i}" for i in range(1, len(segs) + 1)]


def test_batch_dropped_not_treated_as_failure(sme_stub):
    stub, base = sme_stub
    stub.batch_dropped = True
    bridge = SMEBridge(base, timeout_s=2.0)
    segs = _segs()
    r = bridge.store_segments(segs, {"session_id": "s1", "project": "proj",
                                     "trigger": "plan_done"})
    assert r["stored"] == len(segs) - 1   # stored=added（SME 丢弃 1 条）
    assert r["failed"] == 0               # 不视为失败
    assert len(r["errors"]) == 1 and "丢弃 1 条" in r["errors"][0]


def test_single_segment_skips_batch(sme_stub):
    stub, base = sme_stub
    bridge = SMEBridge(base, timeout_s=2.0)
    one = [Segment(type="goal", text="[autocondense|p|t|tr] 单段", tags=BASE_TAGS,
                   importance=0.65)]
    r = bridge.store_segments(one, {"session_id": "s", "project": "p", "trigger": "t"})
    assert stub.batch_calls == 0          # 单段直接逐条
    assert r["stored"] == 1 and r["memory_ids"] == ["mem-1"]


def test_commit_batch_mode_memory_id_null(make_app, tmp_path, sme_stub):
    stub, base = sme_stub
    client = TestClient(make_app(tmp_path, sme_base_url=base))
    cwd = str(tmp_path / "proj")
    client.post("/session/start", json={"session_id": "s1", "cwd": cwd})
    c = client.post("/commit", json={"session_id": "s1", "cwd": cwd,
                                     "digest_md": _digest_ok()}).json()
    assert stub.batch_calls == 1
    assert c["sme"]["stored"] is True and c["sme"]["memory_id"] is None  # 批量契约
    assert client.get("/restore", params={"cwd": cwd}).json()["snapshots"][0][
        "sme_memory_id"] is None  # 无 id 不回填索引


# ---------------------------------------------------------------------------
# 优化 4：去重 / 强化
# ---------------------------------------------------------------------------

def test_dedupe_threshold_boundary(sme_stub):
    stub, base = sme_stub
    bridge = SMEBridge(base, timeout_s=2.0)
    segs = [Segment(type="decision", text=f"[autocondense|proj|t|tr] 决策内容{i}",
                    tags=BASE_TAGS, importance=0.8) for i in (1, 2, 3)]
    bridge.store_segments(segs, {"session_id": "s1", "project": "proj", "trigger": "t"})
    stub.search_score = 0.93
    # 阈值 = 分数（0.93 ≥ 0.93）→ 命中强化
    new, reinforced = bridge.dedupe_segments(segs, "proj", threshold=0.93)
    assert new == [] and len(reinforced) == 3
    # 阈值高于分数（0.94 > 0.93）→ 未命中，全部留作新段
    stub.hits.clear()
    new2, reinforced2 = bridge.dedupe_segments(segs, "proj", threshold=0.94)
    assert reinforced2 == [] and new2 == segs and stub.hits == []


def test_dedupe_project_and_type_mismatch(sme_stub):
    stub, base = sme_stub
    bridge = SMEBridge(base, timeout_s=2.0)
    segs = _segs()
    bridge.store_segments(segs, {"session_id": "s1", "project": "proj",
                                 "trigger": "plan_done"})
    stub.search_score = 0.99
    # 项目不同 → 不算重复
    new, reinforced = bridge.dedupe_segments(_segs(), "other-proj", threshold=0.92)
    assert reinforced == [] and len(new) == len(segs)
    # 段类型不同（内容命中但 metadata.segment_type 不匹配）→ 不算重复
    retyped = [Segment(type="misc", text=s.text, tags=s.tags, importance=s.importance)
               for s in _segs()]
    new2, reinforced2 = bridge.dedupe_segments(retyped, "proj", threshold=0.92)
    assert reinforced2 == [] and len(new2) == len(retyped)


def test_dedupe_search_failure_tolerated(sme_stub):
    stub, base = sme_stub
    bridge = SMEBridge(base, timeout_s=2.0)
    stub.search_fail = True
    segs = _segs()
    new, reinforced = bridge.dedupe_segments(segs, "proj", threshold=0.92)
    assert reinforced == [] and new == segs  # 检索失败按无重复：全量入库，不中断
    assert stub.hits == []


def test_dedupe_hit_failure_treated_as_no_dup(sme_stub):
    stub, base = sme_stub
    bridge = SMEBridge(base, timeout_s=2.0)
    segs = _segs()
    bridge.store_segments(segs, {"session_id": "s1", "project": "proj",
                                 "trigger": "plan_done"})
    stub.search_score = 0.99
    stub.hit_fail = True   # 强化端点 500
    new, reinforced = bridge.dedupe_segments(_segs(), "proj", threshold=0.92)
    assert reinforced == []            # 强化失败不计入
    assert len(new) == len(segs)       # 按无重复处理：全部留作新段入库


def test_commit_dedup_reinforces_instead_of_storing(make_app, tmp_path, sme_stub):
    stub, base = sme_stub
    stub.search_score = 0.95  # 全部命中阈值
    client = TestClient(make_app(tmp_path, sme_base_url=base))
    cwd = str(tmp_path / "proj")
    client.post("/session/start", json={"session_id": "s1", "cwd": cwd})
    r1 = client.post("/commit", json={"session_id": "s1", "cwd": cwd,
                                      "digest_md": _digest_ok()}).json()
    n_first = r1["segments_written"]
    assert n_first > 0 and r1["segments_reinforced"] == 0
    mem_after_first = len(stub.memories)
    # 同一项目重复提交同一份摘要 → 全部命中旧段：强化而非重复入库
    client.post("/begin", json={"session_id": "s1", "cwd": cwd})
    r2 = client.post("/commit", json={"session_id": "s1", "cwd": cwd,
                                      "digest_md": _digest_ok()}).json()
    assert r2["segments_written"] == 0
    assert r2["segments_reinforced"] == n_first
    assert len(stub.memories) == mem_after_first  # 没有新增入库
    assert len(stub.hits) == n_first
    # 去重吃掉全部段时不做整篇兜底（避免同内容整篇重复入库）
    assert r2["sme"]["stored"] is False and r2["sme"]["error"] is None


def test_commit_dedup_disabled_and_config_validation(make_app, tmp_path, sme_stub):
    stub, base = sme_stub
    stub.search_score = 0.95
    client = TestClient(make_app(tmp_path, sme_base_url=base))
    r = client.put("/config", json={"sme": {"dedup_threshold": "high"}})
    assert r.status_code == 400 and "数字" in r.json()["errors"][0]
    r = client.put("/config", json={"sme": {"dedup": "nope"}})
    assert r.status_code == 400 and "布尔" in r.json()["errors"][0]
    r = client.put("/config", json={"sme": {"dedup": False}})
    assert r.status_code == 200 and r.json()["sme"]["dedup"] is False
    cwd = str(tmp_path / "proj")
    client.post("/session/start", json={"session_id": "s1", "cwd": cwd})
    client.post("/commit", json={"session_id": "s1", "cwd": cwd,
                                 "digest_md": _digest_ok()})
    client.post("/begin", json={"session_id": "s1", "cwd": cwd})
    r2 = client.post("/commit", json={"session_id": "s1", "cwd": cwd,
                                      "digest_md": _digest_ok()}).json()
    # 去重关闭：第二次照常入库（分数再高也不强化）
    assert r2["segments_written"] > 0 and r2["segments_reinforced"] == 0
    assert stub.hits == []


def test_dedupe_soft_budget_exceeded_skips_dedup(sme_stub):
    stub, base = sme_stub
    bridge = SMEBridge(base, timeout_s=2.0)
    segs = _segs()
    # 软预算给 0（立即超时）→ 跳过查重，全部按新段返回
    new, reinforced = bridge.dedupe_segments(segs, "proj", threshold=0.92,
                                             budget_s=0.0)
    assert reinforced == [] and new == segs
    assert stub.hits == []
