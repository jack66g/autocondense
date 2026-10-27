"""优化 1：恢复包优先级预算分配 + reread_files + /restore next_actions 测试。"""

from pathlib import Path

from fastapi.testclient import TestClient

from autocondense.digest import build_restore_payload, extract_reread_files

MD = """# 会话快照 · proj · 2026-10-23T10:00:00 · 触发:plan_done

## 目标（用户最终想要什么）

- 完成恢复包优先级改造，预算内先装最关键章节

## 关键决策与理由（含被否决的方案）

- 决策：恢复包按优先级分配预算（未完成 > 恢复指针 > 决策/坑 > 目标 > 已完成）
- 被否决：整篇截断（会先丢最关键的未完成事项）

## 已完成（每项注明验证方式：测试通过/编译通过/用户确认）

- 优先级打包函数：测试通过
- reread_files 解析：测试通过
- 服务接线：测试通过

## 未完成/进行中（精确到下一个动作；无则写「（无）」）

- 补充边界测试：预算用尽丢低优先级

## 文件变更清单（关键改动 file:line）

- digest.py:120 —— 打包器
- src/app.py:8 —— 接线
- tests/test_restore_pack.py:1 —— 测试
- （路径:行号 —— 关键改动说明）

## 踩过的坑与已排除的方向

- 坑：截断指针行自身也要计入预算

## 恢复指针（transcript 路径、更早快照路径、SME 检索关键词）

- transcript: C:/t.jsonl
- SME 检索关键词: autocondense restore pack
"""


def _write(tmp_path, name, md=MD):
    p = tmp_path / name
    p.write_text(md, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# 纯函数层：extract_reread_files / 优先级打包
# ---------------------------------------------------------------------------

def test_extract_reread_files_order_and_placeholder_skip():
    files = extract_reread_files(MD, 5)
    # 按原顺序取路径 token，模板占位（全角括号开头）跳过
    assert files == ["digest.py:120".split(":")[0], "src/app.py", "tests/test_restore_pack.py"]
    assert extract_reread_files(MD, 2) == ["digest.py", "src/app.py"]
    assert extract_reread_files(MD, 0) == []
    # 无文件变更清单章节 → 空
    assert extract_reread_files("## 目标\n\n- x\n", 5) == []


def test_pack_priority_order_and_budget(tmp_path):
    f = _write(tmp_path, "snap.md")
    p = build_restore_payload([str(f)], 100000)
    text = p["text"]
    # 预算充足：全部章节都在，且按优先级排序（未完成最先、标题打头）
    assert text.startswith("# 会话快照")
    assert "## 未完成" in text and "## 恢复指针" in text
    assert "## 关键决策与理由" in text and "## 踩过的坑" in text
    assert "## 目标" in text and "## 已完成" in text
    assert text.index("## 未完成") < text.index("## 恢复指针") < text.index("## 关键决策与理由")
    assert text.index("## 已完成") > text.index("## 目标")
    assert p["truncated"] is False
    assert "（完整快照：" not in text
    assert p["reread_files"] == ["digest.py", "src/app.py", "tests/test_restore_pack.py"]
    assert p["todo_open"] is True


def test_pack_budget_exhausted_drops_low_priority_first(tmp_path):
    f = _write(tmp_path, "snap.md")
    # 紧预算：高优先级（未完成）保留，低优先级（已完成/目标）先丢
    p = build_restore_payload([str(f)], 260)
    text = p["text"]
    assert p["truncated"] is True
    assert "（完整快照：" in text and str(f.resolve()) in text
    assert len(text) <= 260
    assert "## 未完成" in text           # 最高优先级必须在
    assert "补充边界测试" in text
    assert "## 已完成" not in text       # 低优先级被丢
    assert "## 目标（" not in text       # 目标也被丢（优先级低于决策/坑）


def test_pack_goal_char_cap_and_done_line_cap(tmp_path):
    done = "\n".join(f"- 已完成事项{i}：测试通过" for i in range(1, 13))
    md = MD.replace("- 优先级打包函数：测试通过\n- reread_files 解析：测试通过\n- 服务接线：测试通过",
                    done)
    md = md.replace("- 完成恢复包优先级改造，预算内先装最关键章节", "- " + "长" * 500)
    f = _write(tmp_path, "cap.md", md)
    p = build_restore_payload([str(f)], 100000)
    # 目标截 300（内容含 "- " 前缀 → 298 个「长」）；已完成最多 10 条
    goal = p["text"].split("## 目标")[1].split("## 已完成")[0]
    assert goal.count("长") == 298
    assert "已完成事项10" in p["text"] and "已完成事项11" not in p["text"]
    assert p["truncated"] is True
    done_line = next(l for l in p["text"].splitlines() if "已完成事项1：" in l)
    assert len(done_line) <= 80 + 2  # "- " 前缀
    # bullet 输出不得出现双重 "- - " 前缀
    assert "- - " not in p["text"]


def test_pack_todo_placeholder_not_open(tmp_path):
    md = MD.replace("- 补充边界测试：预算用尽丢低优先级", "（无）")
    f = _write(tmp_path, "none.md", md)
    p = build_restore_payload([str(f)], 100000)
    assert p["todo_open"] is False


# ---------------------------------------------------------------------------
# 服务层：/restore next_actions
# ---------------------------------------------------------------------------

def _commit_ok(client, cwd, digest):
    r = client.post("/commit", json={"session_id": "s1", "cwd": cwd, "digest_md": digest})
    assert r.status_code == 200, r.text
    return r.json()


def test_restore_next_actions_files_and_todo(make_app, tmp_path):
    client = TestClient(make_app(tmp_path))
    cwd = str(tmp_path / "proj")
    client.post("/session/start", json={"session_id": "s1", "cwd": cwd})
    _commit_ok(client, cwd, MD + "\n<!-- 填充 -->" * 10)
    res = client.get("/restore", params={"cwd": cwd}).json()
    actions = res["next_actions"]
    assert actions[0].startswith("重读关键文件：")
    assert "digest.py" in actions[0] and "src/app.py" in actions[0]
    assert actions[1] == "继续未完成事项"
    # reread 上限走配置：restore_reread_files=1 只取第一个
    client.put("/config", json={"restore_reread_files": 1})
    res = client.get("/restore", params={"cwd": cwd}).json()
    assert res["next_actions"][0] == "重读关键文件：digest.py"


def test_restore_next_actions_empty_when_no_files_no_todo(make_app, tmp_path):
    client = TestClient(make_app(tmp_path))
    cwd = str(tmp_path / "proj")
    client.post("/session/start", json={"session_id": "s1", "cwd": cwd})
    md = MD.replace("- digest.py:120 —— 打包器", "").replace(
        "- src/app.py:8 —— 接线", "").replace(
        "- tests/test_restore_pack.py:1 —— 测试", "").replace(
        "- （路径:行号 —— 关键改动说明）", "").replace(
        "- 补充边界测试：预算用尽丢低优先级", "（无）")
    _commit_ok(client, cwd, md)
    res = client.get("/restore", params={"cwd": cwd}).json()
    assert res["next_actions"] == []


def test_restore_reread_limit_config_validation(make_app, tmp_path):
    client = TestClient(make_app(tmp_path))
    r = client.put("/config", json={"restore_reread_files": "five"})
    assert r.status_code == 400 and "整数" in r.json()["errors"][0]
    r = client.put("/config", json={"restore_reread_files": 8})
    assert r.status_code == 200 and r.json()["restore_reread_files"] == 8
