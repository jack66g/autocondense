"""digest 分段画像（split_digest）与 SME 逐段写入（store_segments）测试。"""

from autocondense.digest import Segment, split_digest
from autocondense.sme_bridge import SMEBridge

BASE_TAGS = ["project", "todo", "session_digest"]

MD = """# 会话快照 · proj · 2026-10-22T10:00:00 · 触发:plan_done

## 目标（用户最终想要什么）

- 把插件重构为全自动压缩

## 关键决策与理由（含被否决的方案）

- 决策：删掉 manual/auto 双模式
  被否决：保留 manual（与零提示目标冲突）
- 决策：新增 /judge 判定端点

## 已完成（每项注明验证方式：测试通过/编译通过/用户确认）

- 重构 config：测试通过
- 重构 signals：测试通过

## 未完成/进行中（精确到下一个动作；无则写「（无）」）

- 接入层改造：下一步更新 MCP 工具

## 文件变更清单（关键改动 file:line）

- config.py:10
- signals.py:20

## 踩过的坑与已排除的方向

- 坑：Windows 控制台编码

## 恢复指针（transcript 路径、更早快照路径、SME 检索关键词）

- transcript: C:/t.jsonl
- SME 检索关键词: autocondense judge segments
"""


def _split(md=MD, cap=None):
    return split_digest(md, BASE_TAGS, "proj", "2026-10-22T10:00:00",
                        "plan_done", cap=cap)


def test_standard_sections_types_tags_importance():
    segs = _split()
    by_type = {}
    for s in segs:
        by_type.setdefault(s.type, []).append(s)
    assert [s.type for s in segs] == [
        "goal", "decision", "decision", "accomplishment", "accomplishment",
        "todo", "file_change", "file_change", "pitfall", "restore_pointer"]
    # 每类 tags / importance 符合规格
    assert by_type["goal"][0].importance == 0.65
    assert "session_goal" in by_type["goal"][0].tags
    assert BASE_TAGS == by_type["goal"][0].tags[:3]  # base_tags 在前
    assert all(s.importance == 0.8 and "decision" in s.tags for s in by_type["decision"])
    assert all(s.importance == 0.7 and "accomplishment" in s.tags for s in by_type["accomplishment"])
    assert by_type["todo"][0].importance == 0.75 and "todo" in by_type["todo"][0].tags
    assert all(s.importance == 0.6 and "file_change" in s.tags for s in by_type["file_change"])
    assert by_type["pitfall"][0].importance == 0.8 and "pitfall" in by_type["pitfall"][0].tags
    assert by_type["restore_pointer"][0].importance == 0.5
    assert "restore_pointer" in by_type["restore_pointer"][0].tags
    # 标题行（h1）不产生 misc 段
    assert "misc" not in by_type


def test_text_prefix_and_content():
    segs = _split()
    prefix = "[autocondense|proj|2026-10-22T10:00:00|plan_done] "
    for s in segs:
        assert s.text.startswith(prefix)
    goal = segs[0]
    assert "把插件重构为全自动压缩" in goal.text
    # 恢复指针整节一段：两个 bullet 在同一段
    restore = segs[-1]
    assert "transcript: C:/t.jsonl" in restore.text
    assert "SME 检索关键词" in restore.text


def test_indented_continuation_merged_into_bullet():
    segs = _split()
    decisions = [s for s in segs if s.type == "decision"]
    assert len(decisions) == 2
    # 缩进续行「被否决：…」并入所属 bullet
    assert "被否决" in decisions[0].text
    assert "被否决" not in decisions[1].text


def test_file_list_over_8_bullets_merged():
    lines = ["## 文件变更清单（关键改动 file:line）", ""]
    lines += [f"- dir/file{i}.py:{i * 10}" for i in range(1, 10)]  # 9 条
    segs = split_digest("\n".join(lines), BASE_TAGS, "p", "t", "tr", cap=None)
    fc = [s for s in segs if s.type == "file_change"]
    assert len(fc) == 1  # >8 合并一段
    assert "file1.py" in fc[0].text and "file9.py" in fc[0].text


def test_file_list_8_or_fewer_per_bullet():
    lines = ["## 文件变更清单（关键改动 file:line）", ""]
    lines += [f"- file{i}.py" for i in range(1, 9)]  # 8 条
    segs = split_digest("\n".join(lines), BASE_TAGS, "p", "t", "tr", cap=None)
    fc = [s for s in segs if s.type == "file_change"]
    assert len(fc) == 8


def test_missing_sections_tolerated():
    # 只有目标 + 已完成：其余章节缺席不报错
    md = "## 目标（用户最终想要什么）\n\n- 目标X\n\n## 已完成\n\n- 完成Y\n"
    segs = split_digest(md, BASE_TAGS, "p", "t", "tr")
    assert [s.type for s in segs] == ["goal", "accomplishment"]


def test_unrecognized_section_goes_misc():
    md = "## 目标（用户最终想要什么）\n\n- 目标X\n\n## 随便写写的章节\n\n- 散内容A\n"
    segs = split_digest(md, BASE_TAGS, "p", "t", "tr")
    misc = [s for s in segs if s.type == "misc"]
    assert len(misc) == 1
    assert misc[0].importance == 0.55 and "misc" in misc[0].tags
    assert "随便写写的章节" in misc[0].text and "散内容A" in misc[0].text


def test_no_bullets_section_whole_content_segment():
    # 模板未完成章节常见「（无）」散行：整节一段不丢信息
    md = "## 未完成/进行中（精确到下一个动作）\n\n（无）\n"
    segs = split_digest(md, BASE_TAGS, "p", "t", "tr")
    assert len(segs) == 1
    assert segs[0].type == "todo" and "（无）" in segs[0].text


def test_garbage_inputs_fallback_single_digest_segment():
    for bad in ("", "   \n\n  ", "just plain text without any heading",
                "随机乱码 ??! \ufffd"):
        segs = split_digest(bad, BASE_TAGS, "p", "t", "tr")
        assert len(segs) == 1, bad
        s = segs[0]
        assert s.type == "digest" and s.importance == 0.7
        assert "session_digest" in s.tags
        assert s.text.startswith("[autocondense|p|t|tr] ")
    # 非 str 输入也不 raise
    segs = split_digest(None, BASE_TAGS, "p", "t", "tr")  # type: ignore[arg-type]
    assert len(segs) == 1 and segs[0].type == "digest"


def test_headers_but_all_empty_returns_empty():
    md = "## 目标（用户最终想要什么）\n\n## 已完成\n\n"
    segs = split_digest(md, BASE_TAGS, "p", "t", "tr")
    assert segs == []  # 拆段结果为空 → 调用方兜底整篇写入


def test_cap_overflow_merges_lowest_importance_into_misc():
    segs = _split(cap=5)
    assert len(segs) == 5
    # 保留 importance 最高的 4 段 + 1 个合并 misc 段（0.55）
    misc = [s for s in segs if s.type == "misc"]
    assert len(misc) == 1 and misc[0].importance == 0.55
    others = [s for s in segs if s.type != "misc"]
    # 最高优先级段保留：todo(0.75)/decision(0.8)/pitfall(0.8)
    types = {s.type for s in others}
    assert {"decision", "pitfall", "todo"} <= types
    # 溢出段（restore/file_change/goal/accomplishment）内容合入 misc
    assert "transcript: C:/t.jsonl" in misc[0].text
    assert "把插件重构为全自动压缩" in misc[0].text


def test_cap_not_exceeded_and_no_cap():
    assert len(_split(cap=15)) == 10      # 未超上限不合并
    assert len(_split(cap=10)) == 10      # 恰好等于不合并
    assert len(_split()) == 10            # cap=None 不设限
    assert len(_split(cap=1)) == 1        # 极小 cap 也不低于 1 段


def test_tags_dedupe():
    # base_tags 已含 "todo"（sme 默认配置）：todo 段不重复
    segs = split_digest("## 未完成/进行中\n\n- 事项A\n", ["todo"], "p", "t", "tr")
    assert segs[0].tags == ["todo"]


# ---------------------------------------------------------------------------
# sme_bridge.store_segments
# ---------------------------------------------------------------------------

def _segs():
    return split_digest(MD, BASE_TAGS, "proj", "ts", "plan_done")


def test_store_segments_posts_each(sme_stub):
    stub, base = sme_stub
    stub.reject_batch = True  # 强制逐条路径（批量契约另测）
    bridge = SMEBridge(base, timeout_s=2.0)
    segs = _segs()
    r = bridge.store_segments(segs, {"session_id": "s1", "project": "proj",
                                     "trigger": "plan_done"})
    assert r["stored"] == len(segs) and r["failed"] == 0
    assert r["memory_ids"] == [f"mem-{i}" for i in range(1, len(segs) + 1)]
    assert r["errors"] == []
    assert len(stub.memories) == len(segs)  # 逐段 POST
    assert stub.batch_calls == 1  # 先试过 batch（404 回落）
    # 请求体逐项校验
    for i, (m, seg) in enumerate(zip(stub.memories, segs)):
        assert m["text"] == seg.text
        assert m["tags"] == seg.tags
        assert m["importance"] == seg.importance
        assert m["source"] == "session"
        assert m["metadata"]["tool"] == "autocondense"
        assert m["metadata"]["segment_type"] == seg.type
        assert m["metadata"]["segment_index"] == i
        assert m["metadata"]["session_id"] == "s1"
        assert m["metadata"]["project"] == "proj"
        assert m["metadata"]["trigger"] == "plan_done"


def test_store_segments_single_failure_continues(sme_stub):
    stub, base = sme_stub
    stub.fail_markers = ["/judge 判定端点"]  # 命中第二个 decision 段（整体第 3 段）→ 500
    bridge = SMEBridge(base, timeout_s=2.0)
    segs = _segs()
    r = bridge.store_segments(segs, {"session_id": "s1", "project": "proj",
                                     "trigger": "plan_done"})
    assert r["stored"] == len(segs) - 1 and r["failed"] == 1
    assert len(r["errors"]) == 1 and "500" in r["errors"][0]
    assert r["memory_ids"][2] is None  # 失败位无 id，其余照常
    assert r["memory_ids"][0] == "mem-1" and r["memory_ids"][1] == "mem-2"
    assert len(stub.memories) == len(segs) - 1  # 失败段未入库，其余继续


def test_store_segments_offline_never_raises():
    bridge = SMEBridge("http://127.0.0.1:9", timeout_s=0.3)
    r = bridge.store_segments(_segs(), {"session_id": "s", "project": "p",
                                        "trigger": "t"})
    assert r["stored"] == 0 and r["failed"] == len(_segs())
    assert all(e for e in r["errors"])
    assert bridge.store_segments([], {}) == {"stored": 0, "failed": 0,
                                             "memory_ids": [], "errors": []}
