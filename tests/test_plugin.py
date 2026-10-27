"""插件打包结构与运行位置无关性测试（plugin-creator 规范的本地校验）。

覆盖：manifest 合法且目录名一致；声明组件真实存在且不越出插件根；
.mcp.json / hooks.json 形状与 ${CLAUDE_PLUGIN_ROOT} 展开；技能与命令在位；
从陌生 cwd 导入（模拟宿主缓存安装场景）；spawn cwd 指向插件根。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "plugins" / "autocondense"


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_manifest_valid_and_matches_dir():
    manifest = _read_json(PLUGIN_ROOT / ".zcode-plugin" / "plugin.json")
    assert manifest["name"] == "autocondense" == PLUGIN_ROOT.name
    assert manifest["version"] == "0.3.0"
    for key in ("skills", "commands", "hooks", "mcpServers"):
        rel = manifest.get(key)
        if not rel:
            continue
        p = (PLUGIN_ROOT / rel).resolve()
        assert p.exists(), f"{key} -> {rel} 不存在"
        assert PLUGIN_ROOT.resolve() in p.parents, f"{key} 越出插件根：{p}"


def test_marketplace_entry_in_sync():
    entry = _read_json(REPO_ROOT / "plugins" / "marketplace.json")["plugins"][0]
    manifest = _read_json(PLUGIN_ROOT / ".zcode-plugin" / "plugin.json")
    assert entry["name"] == "autocondense" and entry["source"] == "./autocondense"
    assert entry["displayName"] == "autocondense 会话压缩"
    assert entry["version"] == manifest["version"] == "0.3.0"
    # 描述与 manifest 同为全自动表述
    assert "自动" in entry["description"] and "手动" not in entry["description"]


def test_mcp_json_shape():
    data = _read_json(PLUGIN_ROOT / ".mcp.json")
    server = data["mcpServers"]["autocondense"]
    assert server["type"] == "stdio"
    import shutil
    # 开源通用写法：依赖 PATH 上的 python，避免硬编码本机绝对路径（含用户名）
    assert server["command"] == "python" and shutil.which("python"), \
        "command 应为 PATH 可解析的 'python'"
    script = server["args"][0]
    assert script.startswith("${CLAUDE_PLUGIN_ROOT}/"), "MCP 参数必须用插件根令牌，不许猜缓存路径"
    real = PLUGIN_ROOT / script.replace("${CLAUDE_PLUGIN_ROOT}/", "")
    assert real.is_file(), f"{real} 不存在"


def test_hooks_json_events():
    data = _read_json(PLUGIN_ROOT / "hooks" / "hooks.json")
    events = data["hooks"]
    # ZCode 七个钩子事件中我们用四个；PreCompact 不在其列，绝不写进插件
    assert set(events) == {"PostToolUse", "PostToolUseFailure", "Stop", "SessionStart"}
    # matcher 是 regex：服务端按 tool_name 精确分类（TodoWrite 计划、Edit/Write 文件活动轮）
    assert events["PostToolUse"][0]["matcher"] == "TodoWrite|Edit|Write"
    assert "matcher" not in events["Stop"][0]
    assert "matcher" not in events["PostToolUseFailure"][0]
    assert events["SessionStart"][0]["matcher"] == "compact|clear|resume"
    hook_py = PLUGIN_ROOT / "autocondense" / "hooks" / "ac_hook.py"
    for groups in events.values():
        for g in groups:
            for h in g["hooks"]:
                assert "${CLAUDE_PLUGIN_ROOT}" in h["command"]
                assert h["timeout"] == 10
    assert hook_py.is_file()


def test_skill_and_command_present():
    assert (PLUGIN_ROOT / "skills" / "autocondense" / "SKILL.md").is_file()
    cmd = PLUGIN_ROOT / "commands" / "condense.md"
    assert cmd.is_file()
    assert "condense_begin" in cmd.read_text(encoding="utf-8")


def test_skill_and_command_content_semantics():
    """SKILL/命令措辞与新语义一致：7 个工具、判定表提交、无先问用户/manual 模式残留。"""
    skill = (PLUGIN_ROOT / "skills" / "autocondense" / "SKILL.md").read_text(encoding="utf-8")
    cmd = (PLUGIN_ROOT / "commands" / "condense.md").read_text(encoding="utf-8")
    # SKILL：七个工具全部点名 + frontmatter 描述
    assert "7 个 MCP 工具" in skill
    assert "七个工具" in skill
    for tool in ("condense_check", "condense_report", "condense_judge",
                 "condense_begin", "condense_commit", "condense_restore",
                 "condense_status"):
        assert tool in skill, f"SKILL.md 缺少工具 {tool}"
    # SKILL：JUDGE 判定流 + 开关语义 + 分段画像 + 判定矩阵
    assert "should=JUDGE" in skill and "模型自评" in skill
    assert "enabled" in skill and "分段" in skill
    assert "low" in skill and "high" in skill
    # SKILL：v0.3.0 语义——判定表可选 confidence、恢复包 next_actions、
    # idle 候选、增量摘要、SME 查重强化
    assert "confidence" in skill
    assert "next_actions" in skill and "reread_files" in skill
    assert "idle" in skill and "增量" in skill
    assert "查重" in skill and "强化" in skill
    # /condense：主动触发直接执行、不走判定、trigger=user
    assert "condense_judge" in cmd and "直接执行" in cmd
    assert 'trigger="user"' in cmd
    # 旧双模式/询问文案不得残留
    for text in (skill, cmd):
        assert "先问用户" not in text
        assert "询问用户是否" not in text
        assert "手动模式" not in text
        assert "manual" not in text
        assert "| auto |" not in text  # manual/auto 模式差异表格


def test_docs_consistency():
    """轻量 docs 一致性：契约含 /judge 与 v0.3.0 新语义，README 同步。"""
    contract = (REPO_ROOT / "docs" / "CONTRACT.md").read_text(encoding="utf-8")
    assert "POST /judge" in contract
    assert '"JUDGE | NO"' in contract
    assert "COMPRESS_NOW" in contract and "DEFERRED" in contract
    assert "segments_written" in contract and "judgment_form" in contract
    assert "judge_defer_turns" in contract and "split_segments" in contract
    # v0.3.0：新增事件/信号/端点语义/配置键/数据文件
    assert "PostToolUseFailure" in contract and "idle" in contract
    assert "rounds_to_full" in contract and "token_source" in contract
    assert "confidence" in contract and "judge_defer_max_turns" in contract
    assert "next_actions" in contract and "reread_files" in contract
    assert "segments_reinforced" in contract and "delta-state.json" in contract
    assert "delta_merge_every" in contract and "restore_reread_files" in contract
    assert "idle_signal_rounds" in contract and "dedup_threshold" in contract
    assert "TodoWrite|Edit|Write" in contract
    assert "先询问" not in contract and "请询问用户" not in contract
    # 英文主 README（开源门面）+ 中文版 README.zh-CN.md 双语并存
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "condense_judge" in readme and "enabled" in readme
    assert "mode=manual" not in readme
    assert "187" in readme and "idle" in readme and "incremental" in readme.lower()
    assert "README.zh-CN.md" in readme and "Apache-2.0" in readme
    zh = (REPO_ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
    assert "7 个 condense_*" in zh and "增量" in zh and "README.md" in zh


def test_importable_from_foreign_cwd(tmp_path):
    """模拟宿主缓存安装：从任意 cwd 导入 mcp_server（sys.path 自举生效）。"""
    code = (
        "import sys; sys.path.insert(0, r'%s'); "
        "import autocondense.mcp_server as m; "
        "assert m.condense_check and m.condense_commit and m.condense_restore "
        "and m.condense_judge; print('IMPORT_OK')"
        % PLUGIN_ROOT
    )
    env = {**os.environ,
           "AUTOCONDENSE_DATA_DIR": str(tmp_path / "dd"),
           "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          cwd=str(tmp_path), timeout=60, env=env)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")
    assert "IMPORT_OK" in proc.stdout.decode("utf-8", errors="replace")


def test_spawn_cwd_is_plugin_root():
    """mcp_server 以插件根为 cwd 拉起服务（-m autocondense.service 在该 cwd 可解析）。"""
    import autocondense.mcp_server as m
    assert m.REPO_ROOT == PLUGIN_ROOT.resolve()
