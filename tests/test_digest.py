"""digest 模块测试：模板 / 校验 / 恢复载荷截断。"""

from autocondense.digest import (MIN_CHARS, REQUIRED_SECTIONS, TEMPLATE,
                                 build_restore_payload, render_template, validate)


def test_template_render():
    md = render_template("myproj", "2026-10-04T12:00:00", "plan_done")
    assert "myproj" in md and "2026-10-04T12:00:00" in md and "plan_done" in md
    for sec in ["## 目标", "## 关键决策", "## 已完成", "## 未完成", "## 文件变更清单",
                "## 踩过的坑", "## 恢复指针"]:
        assert sec in md
    assert md.startswith("# 会话快照 · myproj ·")


def test_template_itself_is_valid():
    ok, errors = validate(render_template("p", "t", "r"))
    assert ok, errors


def test_validate_missing_section():
    md = render_template("p", "t", "r").replace("## 已完成（每项注明验证方式：测试通过/编译通过/用户确认）\n\n- （已完成事项：验证方式）\n\n", "")
    ok, errors = validate(md)
    assert not ok
    assert any("## 已完成" in e for e in errors)


def test_validate_too_short():
    short = "# 会话快照\n## 目标\n## 已完成\n## 未完成\n## 文件变更清单\n## 恢复指针\n"
    assert len(short) < MIN_CHARS
    ok, errors = validate(short)
    assert not ok
    assert any("200" in e for e in errors)


def test_validate_empty():
    ok, errors = validate("")
    assert not ok and errors


def test_build_restore_payload(tmp_path):
    new = tmp_path / "new.md"
    old = tmp_path / "old.md"
    new.write_text("# 新快照标题\n\n新快照正文" + "X" * 200, encoding="utf-8")
    old.write_text("# 旧快照标题\n\n旧快照正文" + "Y" * 200, encoding="utf-8")

    # 不截断：最新全文 + 旧快照只有标题行
    p = build_restore_payload([str(new), str(old)], 100000)
    assert p["truncated"] is False
    assert p["full_file"] == str(new.resolve())
    assert p["text"].startswith("# 新快照标题")
    assert "# 旧快照标题" in p["text"]
    assert "旧快照正文" not in p["text"]
    assert "Read 工具" in p["note"] and str(new.resolve()) in p["note"]
    assert p["reread_files"] == []  # 无文件变更清单章节 → 无建议重读文件

    # 截断：预算不够时丢内容 + 末尾保留完整快照指针行
    p2 = build_restore_payload([str(new), str(old)], 50)
    assert p2["truncated"] is True
    assert len(p2["text"]) <= 50
    assert "（完整快照：" in p2["text"]
    assert p2["full_file"] == str(new.resolve())

    # 空列表 / 文件不存在
    p3 = build_restore_payload([], 100)
    assert p3["text"] == "" and p3["full_file"] is None and p3["note"] is None
    assert p3["truncated"] is False and p3["reread_files"] == []
    p4 = build_restore_payload([str(tmp_path / "gone.md")], 100)
    assert p4["full_file"] is None
