"""优化 5：增量 delta 摘要测试（begin 模式 / 计数 / 索引标记 / restore 合并链）。"""

import json

from fastapi.testclient import TestClient

from autocondense.config import Config
from autocondense.state import StateStore, project_hash


def _digest(todo="- TODO：下一步动作", done=("完成A：测试通过",), files=("a.py:1",),
            marker="base"):
    parts = [f"# 会话快照 · proj · {marker}"]
    parts.append("## 目标（用户最终想要什么）\n\n- 目标：完成 delta 增量摘要链路改造")
    parts.append("## 关键决策与理由（含被否决的方案）\n\n- 决策：增量只追加新条目")
    parts.append("## 已完成（每项注明验证方式：测试通过/编译通过/用户确认）\n\n"
                 + "\n".join(f"- {d}" for d in done))
    parts.append(f"## 未完成/进行中（精确到下一个动作）\n\n{todo}")
    parts.append("## 文件变更清单（关键改动 file:line）\n\n"
                 + "\n".join(f"- {f} —— 改动" for f in files))
    parts.append("## 踩过的坑与已排除的方向\n\n- 坑：无")
    parts.append("## 恢复指针（transcript 路径、更早快照路径、SME 检索关键词）\n\n"
                 "- transcript: C:/t.jsonl\n- SME 检索关键词: delta test")
    md = "\n\n".join(parts) + "\n"
    assert len(md) >= 200
    return md


def _commit(client, cwd, digest, session="s1"):
    r = client.post("/begin", json={"session_id": session, "cwd": cwd})
    assert r.status_code == 200
    mode = r.json()["mode"]
    r = client.post("/commit", json={"session_id": session, "cwd": cwd,
                                     "digest_md": digest})
    assert r.status_code == 200, r.text
    return mode, r.json()


# ---------------------------------------------------------------------------
# StateStore：delta-state.json 读写
# ---------------------------------------------------------------------------

def test_delta_state_store_roundtrip(tmp_path):
    store = StateStore(tmp_path, Config())
    h = "deadbeef00"
    assert store.read_delta_state(h) == 0  # 缺失 → 0
    store.write_delta_state(h, 3)
    assert store.read_delta_state(h) == 3
    path = tmp_path / "snapshots" / h / "delta-state.json"
    assert path.is_file() and not list(path.parent.glob("*.tmp-*"))
    # 损坏 / 值异常 → 容忍按 0
    path.write_text("{bad", encoding="utf-8")
    assert store.read_delta_state(h) == 0
    path.write_text(json.dumps({"delta_since_full": "many"}), encoding="utf-8")
    assert store.read_delta_state(h) == 0


def test_save_snapshot_delta_flag(tmp_path):
    store = StateStore(tmp_path, Config())
    store.upsert_session("s1", cwd=str(tmp_path))
    _p, full_entry = store.save_snapshot("# 一", "t", "s1", delta=False)
    _p2, delta_entry = store.save_snapshot("# 二", "t", "s1", delta=True)
    assert full_entry["delta"] is False and delta_entry["delta"] is True
    snaps = store.list_snapshots(project_hash(str(tmp_path)))
    assert [e["delta"] for e in snaps] == [True, False]  # 最新在前


# ---------------------------------------------------------------------------
# /begin 模式选择 + /commit 计数
# ---------------------------------------------------------------------------

def test_begin_full_first_then_delta_then_merge_point(make_app, tmp_path):
    client = TestClient(make_app(tmp_path))
    cwd = str(tmp_path / "proj")
    client.post("/session/start", json={"session_id": "s1", "cwd": cwd})

    # 项目无快照 → 全量；不带 previous_digest
    r = client.post("/begin", json={"session_id": "s1", "cwd": cwd})
    b = r.json()
    assert b["mode"] == "full" and "previous_digest" not in b
    r = client.post("/commit", json={"session_id": "s1", "cwd": cwd,
                                     "digest_md": _digest(marker="full-0",
                                                          done=("全量0：测试通过",))})
    assert r.json()["mode"] == "full"

    # 已有快照且增量未攒满 → delta，附上一份快照全文与增量指引
    r = client.post("/begin", json={"session_id": "s1", "cwd": cwd})
    b = r.json()
    assert b["mode"] == "delta"
    assert "全量0：测试通过" in b["previous_digest"]
    assert "增量" in b["guidance"] and "整节重写" in b["guidance"]
    r = client.post("/commit", json={"session_id": "s1", "cwd": cwd,
                                     "digest_md": _digest(marker="d1")})
    assert r.json()["mode"] == "delta"

    # 连续 5 次 delta 后到合并点 → 强制全量合并
    for i in range(2, 6):
        mode, _c = _commit(client, cwd, _digest(marker=f"d{i}"))
        assert mode == "delta", f"第 {i} 份应为增量"
    h = project_hash(cwd)
    assert StateStore(tmp_path / "data").read_delta_state(h) == 5
    r = client.post("/begin", json={"session_id": "s1", "cwd": cwd})
    b = r.json()
    assert b["mode"] == "full"
    assert "全量合并" in b["guidance"] and "增量" in b["guidance"]
    r = client.post("/commit", json={"session_id": "s1", "cwd": cwd,
                                     "digest_md": _digest(marker="full-merge")})
    assert r.json()["mode"] == "full"
    assert StateStore(tmp_path / "data").read_delta_state(h) == 0  # 归零

    # 索引：最新为全量，往回 5 份 delta
    snaps = client.get("/restore", params={"cwd": cwd, "top_n": 10}).json()["snapshots"]
    deltas = [e.get("delta") for e in snaps]
    assert deltas[0] is False
    assert deltas[1:6] == [True] * 5


def test_delta_mode_disabled_always_full(make_app, tmp_path):
    client = TestClient(make_app(tmp_path))
    cwd = str(tmp_path / "proj")
    client.put("/config", json={"delta_mode": False})
    client.post("/session/start", json={"session_id": "s1", "cwd": cwd})
    for i in range(3):
        mode, c = _commit(client, cwd, _digest(marker=f"f{i}"))
        assert mode == "full" and c["mode"] == "full"
    snaps = client.get("/restore", params={"cwd": cwd}).json()["snapshots"]
    assert all(e["delta"] is False for e in snaps)


# ---------------------------------------------------------------------------
# /restore 合并链
# ---------------------------------------------------------------------------

def _make_chain(make_app, tmp_path):
    client = TestClient(make_app(tmp_path))
    cwd = str(tmp_path / "proj")
    client.post("/session/start", json={"session_id": "s1", "cwd": cwd})
    _commit(client, cwd, _digest(marker="full", done=("完成A：测试通过", "完成B：编译通过"),
                                 files=("a.py:1",), todo="- TODO-FULL：全量遗留"))
    _commit(client, cwd, _digest(marker="d1", done=("完成C：用户确认",),
                                 files=("b.py:2",), todo="- TODO-D1：增量1待办"))
    _commit(client, cwd, _digest(marker="d2", done=("完成D：测试通过", "完成C：用户确认"),
                                 files=("c.py:3", "b.py:2"), todo="- TODO-D2：增量2待办"))
    return client, cwd, tmp_path / "data"


def test_restore_merges_delta_chain(make_app, tmp_path):
    client, cwd, data_dir = _make_chain(make_app, tmp_path)
    res = client.get("/restore", params={"cwd": cwd}).json()
    text = res["restore"]["text"]
    # bullet 并集：最新条目在前，相同文本去重（完成C 只出现一次）
    assert "完成D：测试通过" in text and "完成C：用户确认" in text
    assert "完成A：测试通过" in text and "完成B：编译通过" in text
    assert text.index("完成D：测试通过") < text.index("完成A：测试通过")
    assert text.count("完成C：用户确认") == 1
    # 未完成取最新版本：只有 D2 的待办，旧待办消失
    assert "TODO-D2" in text and "TODO-D1" not in text and "TODO-FULL" not in text
    # reread_files 来自合并后的文件清单（新条目在前）
    assert res["restore"]["reread_files"] == ["c.py", "b.py", "a.py"]
    assert res["next_actions"][0] == "重读关键文件：c.py、b.py、a.py"
    assert res["next_actions"][1] == "继续未完成事项"
    # 恢复锚定最新快照文件
    newest = res["snapshots"][0]
    assert res["restore"]["full_file"] == newest["file"]


def test_restore_broken_chain_falls_back_to_newest(make_app, tmp_path):
    client, cwd, data_dir = _make_chain(make_app, tmp_path)
    h = project_hash(cwd)
    index_path = data_dir / "snapshots" / h / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    # 把最老的全量锚点也改成 delta → 链内找不到锚 → 只用最新快照
    for e in index:
        e["delta"] = True
    index_path.write_text(json.dumps(index), encoding="utf-8")
    res = client.get("/restore", params={"cwd": cwd}).json()
    text = res["restore"]["text"]
    assert "TODO-D2" in text          # 最新快照内容在
    assert "完成A：测试通过" not in text  # 旧全量内容不并入（链断兜底）
    assert "TODO-D1" not in text


def test_restore_legacy_entries_without_delta_treated_as_full(make_app, tmp_path):
    client = TestClient(make_app(tmp_path))
    cwd = str(tmp_path / "proj")
    client.post("/session/start", json={"session_id": "s1", "cwd": cwd})
    # 老索引：StateStore 直接写（不带 delta 字段）
    store = StateStore(tmp_path / "data")
    store.upsert_session("legacy", cwd=cwd)
    store.save_snapshot(_digest(marker="legacy-full", done=("老条目：用户确认",)),
                        "manual", "legacy")
    # 新增量（走 API，begin→delta→commit）
    r = client.post("/begin", json={"session_id": "s1", "cwd": cwd})
    assert r.json()["mode"] == "delta"  # 老快照存在即算「已有快照」
    client.post("/commit", json={"session_id": "s1", "cwd": cwd,
                                 "digest_md": _digest(marker="new-delta",
                                                      done=("新条目：测试通过",))})
    res = client.get("/restore", params={"cwd": cwd}).json()
    text = res["restore"]["text"]
    assert "新条目：测试通过" in text and "老条目：用户确认" in text  # 老条目当全量锚并入


def test_delta_depth_capped_at_merge_every_plus_one(make_app, tmp_path):
    """链深度上限 delta_merge_every+1：更早的增量不并入。

    直接用 StateStore 构造全 delta 链（绕过 begin 的自动全量合并点），
    单测 restore 链深度的边界。
    """
    data_dir = tmp_path / "data"
    cwd = str(tmp_path / "proj")
    store = StateStore(data_dir)
    store.upsert_session("s1", cwd=cwd)
    store.save_snapshot(_digest(marker="full", done=("锚点条目：测试通过",)),
                        "manual", "s1", delta=False)
    for i in range(1, 8):  # 7 份增量 > delta_merge_every(5)
        store.save_snapshot(_digest(marker=f"d{i}", done=(f"增量条目{i}：测试通过",)),
                            "manual", "s1", delta=True)
    client = TestClient(make_app(tmp_path))
    res = client.get("/restore", params={"cwd": cwd}).json()
    text = res["restore"]["text"]
    # 深度上限 6：最新 5 份增量 + 全量锚点；更早的 d1/d2 超出窗口不并入
    assert "增量条目7" in text and "增量条目3" in text
    assert "增量条目2" not in text and "增量条目1" not in text
    assert "锚点条目" in text
