"""installer.py 测试：--home 注入临时目录，四宿主全覆盖，绝不碰真实用户配置。

覆盖：dry-run 只打印不落盘；--yes 写入且深合并保留已有条目（预置假 sme-memory）；
备份存在；幂等重跑；--uninstall 摘除后与原文件等价；--list / 未知宿主。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALLER = REPO_ROOT / "installer.py"

HOSTS = ("zcode", "claude", "codex", "cursor")

ZCODE_CFG = "{home}/.zcode/cli/config.json"
CLAUDE_MCP = "{home}/.claude.json"
CLAUDE_SETTINGS = "{home}/.claude/settings.json"
CODEX_TOML = "{home}/.codex/config.toml"
CURSOR_MCP = "{home}/.cursor/mcp.json"


def run_installer(home: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(INSTALLER), "--home", str(home), *args],
        capture_output=True, timeout=120, env=env, cwd=str(REPO_ROOT))


def preset_existing_entries(home: Path) -> None:
    """预置各宿主已有配置（假 sme-memory 等），验证深合并不删别人的条目。"""
    zc = home / ".zcode" / "cli" / "config.json"
    zc.parent.mkdir(parents=True, exist_ok=True)
    zc.write_text(json.dumps({
        "mcp": {"servers": {"sme-memory": {"type": "stdio", "command": "python",
                                            "args": ["C:/sme/mcp_server.py"]}}},
        "plugins": {"disabled": ["foo"]},
    }, ensure_ascii=False), encoding="utf-8")

    cj = home / ".claude.json"
    cj.write_text(json.dumps({
        "mcpServers": {"sme-memory": {"command": "python",
                                       "args": ["C:/sme/mcp_server.py"]}},
        "numStartups": 42,
    }, ensure_ascii=False), encoding="utf-8")

    cs = home / ".claude" / "settings.json"
    cs.parent.mkdir(parents=True, exist_ok=True)
    cs.write_text(json.dumps({
        "model": "glm-5.3",
        "hooks": {"PostToolUse": [{"matcher": "Bash",
                                   "hooks": [{"type": "command", "command": "echo hi"}]}]},
    }, ensure_ascii=False), encoding="utf-8")

    ct = home / ".codex" / "config.toml"
    ct.parent.mkdir(parents=True, exist_ok=True)
    ct.write_text('model = "gpt-5"\n\n[mcp_servers.sme-memory]\ncommand = "python"\nargs = ["C:/sme/mcp_server.py"]\n', encoding="utf-8")

    cm = home / ".cursor" / "mcp.json"
    cm.parent.mkdir(parents=True, exist_ok=True)
    cm.write_text(json.dumps({
        "mcpServers": {"sme-memory": {"command": "python",
                                       "args": ["C:/sme/mcp_server.py"]}},
    }, ensure_ascii=False), encoding="utf-8")


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# dry-run：只打印 diff，不落盘
# ---------------------------------------------------------------------------

def test_dry_run_prints_diff_without_writing(tmp_path):
    home = tmp_path / "home"
    proc = run_installer(home)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")
    out = proc.stdout.decode("utf-8", errors="replace")
    assert "dry-run" in out and "改前" in out and "改后" in out
    assert "@@" in out  # 有 diff 内容
    assert not home.exists()  # 一个文件都没写


# ---------------------------------------------------------------------------
# --yes：四宿主写入 + 深合并保留已有条目 + 备份
# ---------------------------------------------------------------------------

def test_install_yes_preserves_and_merges(tmp_path):
    home = tmp_path / "home"
    preset_existing_entries(home)

    proc = run_installer(home, "--yes")
    assert proc.returncode == 0, proc.stdout.decode("utf-8", errors="replace")

    # zcode：mcp.servers 深合并保留 sme-memory；plugins 不动；hooks 结构正确
    zc = load(home / ".zcode" / "cli" / "config.json")
    servers = zc["mcp"]["servers"]
    assert "autocondense" in servers and "sme-memory" in servers
    assert servers["autocondense"]["type"] == "stdio"
    assert servers["autocondense"]["command"].endswith("python.exe") or "python" in servers["autocondense"]["command"]
    assert servers["autocondense"]["args"][0].endswith("mcp_server.py")
    assert zc["plugins"] == {"disabled": ["foo"]}
    assert zc["hooks"]["enabled"] is True
    events = zc["hooks"]["events"]
    # ZCode 七事件中写四个：PostToolUseFailure 为 ZCode 独有；zcode 宿主不写 PreCompact
    assert set(events) == {"PostToolUse", "PostToolUseFailure", "Stop", "SessionStart"}
    assert events["PostToolUse"][0]["matcher"] == "TodoWrite|Edit|Write"
    assert "ac_hook.py" in events["PostToolUse"][0]["hooks"][0]["command"]
    assert "matcher" not in events["Stop"][0]  # Stop 无 matcher
    assert "matcher" not in events["PostToolUseFailure"][0]  # 失败事件无 matcher
    assert "PostToolUseFailure" in events["PostToolUseFailure"][0]["hooks"][0]["command"]
    assert events["SessionStart"][0]["matcher"] == "compact|clear|resume"

    # claude：PreCompact 只写给 Claude Code（它支持该事件）；
    # Claude Code 没有 PostToolUseFailure——绝不写入
    cs_hooks = load(home / ".claude" / "settings.json")["hooks"]
    cs_precompact = cs_hooks.get("PreCompact")
    assert cs_precompact and cs_precompact[0]["matcher"] == "auto"
    assert "PostToolUseFailure" not in cs_hooks
    assert any(g.get("matcher") == "TodoWrite|Edit|Write" for g in cs_hooks["PostToolUse"]
               if any("ac_hook.py" in h.get("command", "") for h in g.get("hooks", [])))

    # claude：.claude.json 保留 sme-memory 与 numStartups；settings.json 保留 model 与别人的 hooks
    cj = load(home / ".claude.json")
    assert "autocondense" in cj["mcpServers"] and "sme-memory" in cj["mcpServers"]
    assert cj["numStartups"] == 42
    cs = load(home / ".claude" / "settings.json")
    assert cs["model"] == "glm-5.3"
    assert "autocondense" not in cs  # hooks 在 settings.json 的 hooks 键里
    chooks = cs["hooks"]
    assert any(g.get("matcher") == "Bash" for g in chooks["PostToolUse"])  # 别人的保留
    assert any("ac_hook.py" in h["command"]
               for g in chooks["PostToolUse"] for h in g.get("hooks", []))

    # codex：TOML 文本级追加，原内容保留
    ct_text = (home / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert '[mcp_servers.sme-memory]' in ct_text
    assert "[mcp_servers.autocondense]" in ct_text
    assert "mcp_server.py" in ct_text and "command = " in ct_text
    assert ct_text.index("[mcp_servers.sme-memory]") < ct_text.index("[mcp_servers.autocondense]")

    # cursor：保留 sme-memory
    cm = load(home / ".cursor" / "mcp.json")
    assert "autocondense" in cm["mcpServers"] and "sme-memory" in cm["mcpServers"]

    # 备份：5 个已存在文件都有备份
    baks = list(home.rglob("*.bak-autocondense-*"))
    assert len(baks) == 5, [str(b) for b in baks]

    # --list 显示已配置
    proc = run_installer(home, "--list")
    out = proc.stdout.decode("utf-8", errors="replace")
    assert out.count("已配置 autocondense") >= 4


def test_install_idempotent_rerun(tmp_path):
    home = tmp_path / "home"
    preset_existing_entries(home)
    assert run_installer(home, "--yes").returncode == 0
    zc_before = (home / ".zcode" / "cli" / "config.json").read_text(encoding="utf-8")
    ct_before = (home / ".codex" / "config.toml").read_text(encoding="utf-8")
    n_baks_before = len(list(home.rglob("*.bak-autocondense-*")))

    proc = run_installer(home, "--yes")
    out = proc.stdout.decode("utf-8", errors="replace")
    assert proc.returncode == 0
    assert "无变化" in out  # 幂等：重复条目先摘再写，结果一致
    assert (home / ".zcode" / "cli" / "config.json").read_text(encoding="utf-8") == zc_before
    assert (home / ".codex" / "config.toml").read_text(encoding="utf-8") == ct_before
    assert len(list(home.rglob("*.bak-autocondense-*"))) == n_baks_before  # 无变化不备份


# ---------------------------------------------------------------------------
# --uninstall：摘除后与原文件等价
# ---------------------------------------------------------------------------

def test_uninstall_restores_original(tmp_path):
    home = tmp_path / "home"
    preset_existing_entries(home)
    originals = {
        p: p.read_text(encoding="utf-8")
        for p in [
            home / ".zcode" / "cli" / "config.json",
            home / ".claude.json",
            home / ".claude" / "settings.json",
            home / ".codex" / "config.toml",
            home / ".cursor" / "mcp.json",
        ]
    }
    assert run_installer(home, "--yes").returncode == 0
    proc = run_installer(home, "--uninstall", "--yes")
    assert proc.returncode == 0, proc.stdout.decode("utf-8", errors="replace")

    # JSON：解析后与原对象等价（写回会规范化缩进，故比较解析结果）
    for p in (home / ".zcode" / "cli" / "config.json", home / ".claude.json",
              home / ".claude" / "settings.json", home / ".cursor" / "mcp.json"):
        assert load(p) == json.loads(originals[p]), f"{p} 摘除后与原文件不等价"

    # TOML：文本级摘除，逐字等价
    assert (home / ".codex" / "config.toml").read_text(encoding="utf-8") == \
        originals[home / ".codex" / "config.toml"]

    # --list 全部回到未配置
    out = run_installer(home, "--list").stdout.decode("utf-8", errors="replace")
    assert "未配置" in out and "已配置 autocondense" not in out


def test_uninstall_on_fresh_home_noop(tmp_path):
    home = tmp_path / "home"
    proc = run_installer(home, "--uninstall", "--yes")
    assert proc.returncode == 0
    assert not home.exists()  # 什么都不创建


def test_uninstall_removes_files_we_created(tmp_path):
    # 空宿主安装→卸载：我们新建的文件（只含 autocondense 条目）应被清掉
    home = tmp_path / "home"
    assert run_installer(home, "--yes").returncode == 0
    assert (home / ".cursor" / "mcp.json").exists()
    assert run_installer(home, "--uninstall", "--yes").returncode == 0
    assert not (home / ".cursor" / "mcp.json").exists()
    assert not (home / ".codex" / "config.toml").exists()


def test_hook_event_lists_per_host():
    """事件清单按宿主区分：PostToolUseFailure 仅 ZCode；PreCompact 仅 Claude Code。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("installer_under_test", INSTALLER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclass 解析需要模块已注册
    spec.loader.exec_module(mod)
    assert "PostToolUseFailure" in mod.ZCODE_HOOK_EVENTS
    assert "PreCompact" not in mod.ZCODE_HOOK_EVENTS
    assert "PostToolUseFailure" not in mod.CLAUDE_HOOK_EVENTS
    assert "PreCompact" in mod.CLAUDE_HOOK_EVENTS
    assert mod.HOOK_MATCHERS["PostToolUse"] == "TodoWrite|Edit|Write"
    assert "PostToolUseFailure" not in mod.HOOK_MATCHERS  # 失败事件无 matcher


# ---------------------------------------------------------------------------
# CLI 参数
# ---------------------------------------------------------------------------

def test_unknown_host_rejected(tmp_path):
    proc = run_installer(tmp_path / "h", "--hosts", "vscode")
    assert proc.returncode != 0
    assert "未知宿主" in proc.stderr.decode("utf-8", errors="replace")


def test_single_host_scope(tmp_path):
    home = tmp_path / "home"
    proc = run_installer(home, "--hosts", "cursor", "--yes")
    assert proc.returncode == 0
    assert (home / ".cursor" / "mcp.json").exists()
    assert not (home / ".zcode").exists()
    assert not (home / ".codex").exists()
