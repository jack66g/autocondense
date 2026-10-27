#!/usr/bin/env python3
"""autocondense 接入安装器：把 MCP 服务器与 hooks 配置写入各 AI 编程宿主。

支持的宿主（--hosts 逗号分隔，或 all）：

    zcode   ~/.zcode/cli/config.json   【仅 ZCode CLI 场景】
            - MCP  : mcp.servers.autocondense（嵌套键，ZCode 专用格式）
            - hooks: hooks.events.<Event>（须 hooks.enabled=true）
            ⚠ ZCode 桌面端不消费该文件的 mcp/hooks（实测），桌面端请走插件市场：
              添加本地市场 plugins/ → 安装 autocondense 插件（MCP 自动连接、
              hooks 自动启用 runner、技能与命令一并分发）。
    claude  ~/.claude.json            mcpServers.autocondense
            ~/.claude/settings.json   hooks.<Event>（Claude Code 格式）
    codex   ~/.codex/config.toml      [mcp_servers.autocondense]（文本级插入）
    cursor  ~/.cursor/mcp.json        mcpServers.autocondense

hooks 仅写入 zcode / claude（codex / cursor 无 hooks 机制），事件按宿主区分：
    共有       PostToolUse matcher "TodoWrite|Edit|Write"（服务端再按 tool_name 精确分类：
               TodoWrite 记计划，Edit/Write 等记文件活动轮）；Stop（无 matcher）；
               SessionStart matcher "compact|clear|resume"
    仅 zcode   PostToolUseFailure（无 matcher；ZCode 独有事件，记失败活动轮供 idle 信号）
    仅 claude  PreCompact matcher "auto"（Claude Code 独有；ZCode 七事件里没有它）

安全设计：
- 默认 dry-run：只打印每个目标文件的「改前 → 改后」diff，不落盘；--yes 才写入。
- 写前备份 {file}.bak-autocondense-{时间戳}（文件已存在时）。
- 深合并：只增改 autocondense 自己的键，绝不删除/覆盖他人的条目；重跑幂等
  （先摘除旧的 autocondense 条目再写入新的）。
- --uninstall 只摘除我们写入的键名（JSON 深摘除；TOML 只处理带标记的块）。
- --list 列出宿主、目标路径与当前配置状态。
- JSON 文件损坏时跳过该文件并告警，绝不盲目覆盖。

已知局限（TOML 无标准库解析）：codex 的 config.toml 采用文本级插入/摘除，
只处理带 ``# >>> autocondense`` 标记的块；手工写的同名 [mcp_servers.autocondense]
表不会被识别/修改，此时请手工合并。

用法::

    python installer.py --list
    python installer.py                       # dry-run，四宿主
    python installer.py --hosts zcode,claude --yes
    python installer.py --uninstall --yes
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent
PLUGIN_ROOT = REPO_ROOT / "plugins" / "autocondense"
MCP_NAME = "autocondense"
# ZCode 支持七个钩子事件；我们写四个（PreCompact 不在 ZCode 事件之列）。
# PostToolUseFailure 是 ZCode 独有事件，Claude Code 没有——两侧事件清单必须分开维护。
ZCODE_HOOK_EVENTS = ["PostToolUse", "Stop", "SessionStart", "PostToolUseFailure"]
# Claude Code 侧：无 PostToolUseFailure，有 PreCompact
CLAUDE_HOOK_EVENTS = ["PostToolUse", "Stop", "SessionStart", "PreCompact"]
# 卸载超集清理：两宿主事件并集（早期版本曾给 zcode 写过 PreCompact，一并摘掉）
HOOK_EVENTS_UNINSTALL = sorted(set(ZCODE_HOOK_EVENTS) | set(CLAUDE_HOOK_EVENTS))
HOOK_MATCHERS = {
    # regex；服务端按 tool_name 精确分类：TodoWrite→计划，Edit/Write→文件活动轮
    "PostToolUse": "TodoWrite|Edit|Write",
    "SessionStart": "compact|clear|resume",
    "PreCompact": "auto",
}
HOOK_TIMEOUT_S = 10  # 钩子转发 3s 超时，留足余量

AC_HOOK_PY = PLUGIN_ROOT / "autocondense" / "hooks" / "ac_hook.py"
MCP_SERVER_PY = PLUGIN_ROOT / "autocondense" / "mcp_server.py"

TOML_BEGIN = "# >>> autocondense (mcp_servers) >>>"
TOML_END = "# <<< autocondense (mcp_servers) <<<"

ALL_HOSTS = ["zcode", "claude", "codex", "cursor"]


# ---------------------------------------------------------------------------
# 文件变更模型：before/after 为文本；None 表示「文件不存在」/「写后删除」
# ---------------------------------------------------------------------------

@dataclass
class FileChange:
    path: Path
    before: Optional[str] = None   # None = 文件当前不存在
    after: Optional[str] = None    # None = 卸载后删除该文件
    error: Optional[str] = None    # 非 None 表示该文件被跳过（解析失败等）

    @property
    def changed(self) -> bool:
        return self.error is None and self.before != self.after


def _read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as e:
        raise RuntimeError(f"读取失败：{e}") from e


def _backup(path: Path) -> Optional[Path]:
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = path.with_name(f"{path.name}.bak-autocondense-{stamp}")
    shutil.copy2(path, bak)
    return bak


def _apply(change: FileChange) -> Optional[Path]:
    """落盘一个变更（调用方已确认 --yes）；返回备份路径。"""
    if not change.changed:
        return None
    bak = _backup(change.path)
    if change.after is None:
        if change.path.exists():
            change.path.unlink()
    else:
        change.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = change.path.with_name(f"{change.path.name}.tmp-{os.getpid()}")
        tmp.write_text(change.after, encoding="utf-8")
        os.replace(tmp, change.path)
    return bak


# ---------------------------------------------------------------------------
# JSON 宿主（zcode / claude / cursor）
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    """读 JSON；缺失返回 {}；损坏/非对象返回 None（调用方跳过该文件）。"""
    text = _read_text(path)
    if text is None:
        return {}
    try:
        data = json.loads(text)
    except ValueError as e:
        raise RuntimeError(f"JSON 解析失败（{e}），为安全起见跳过该文件") from e
    if not isinstance(data, dict):
        raise RuntimeError("JSON 根不是对象，为安全起见跳过该文件")
    return data


def _dump_json(data: Dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def _json_change(path: Path, mutate, delete_if_empty: bool = False) -> FileChange:
    """通用 JSON 变更。before/after 都走规范化序列化，diff 只显示真实变化。

    mutate(data, python) 就地修改解析后的 dict。delete_if_empty=True（卸载）时，
    若摘除后对象为空字典且该文件本就只含我们的条目，则删除整个文件。
    """
    try:
        before_text = _read_text(path)
        data = _load_json(path)
        if data is None:  # pragma: no cover（_load_json 损坏时抛 RuntimeError）
            return FileChange(path, error="跳过")
        # 规范化 before：把原始 JSON 重新序列化，diff 不受格式差异干扰
        if before_text is not None:
            before_norm = _dump_json(json.loads(before_text))
        else:
            before_norm = None
        mutate(data)
        if delete_if_empty and not data:
            return FileChange(path, before=before_norm, after=None)
        after_text = _dump_json(data)
        return FileChange(path, before=before_norm, after=after_text)
    except (RuntimeError, ValueError) as e:
        return FileChange(path, error=str(e))


def mcp_entry(python: str) -> Dict[str, Any]:
    return {
        "type": "stdio",
        "command": python,
        "args": [str(MCP_SERVER_PY)],
    }


def _hook_cmd(python: str, event: str) -> str:
    return f'"{python}" "{AC_HOOK_PY}" {event}'


def _is_our_hook(h: Any) -> bool:
    return isinstance(h, dict) and "ac_hook.py" in str(h.get("command", ""))


def _is_our_group(g: Any) -> bool:
    return isinstance(g, dict) and any(_is_our_hook(h) for h in g.get("hooks", []) or [])


def _set_mcp_flat(data: Dict[str, Any], python: str) -> None:
    """claude/.claude.json、cursor：顶层 mcpServers 键。"""
    data.setdefault("mcpServers", {})[MCP_NAME] = mcp_entry(python)


def _unset_mcp_flat(data: Dict[str, Any], python: str = "") -> None:
    servers = data.get("mcpServers")
    if isinstance(servers, dict) and MCP_NAME in servers:
        del servers[MCP_NAME]
        if not servers:
            del data["mcpServers"]


def _set_mcp_zcode(data: Dict[str, Any], python: str) -> None:
    """zcode：嵌套 mcp.servers 键（~/.zcode/cli/config.json）。"""
    data.setdefault("mcp", {}).setdefault("servers", {})[MCP_NAME] = mcp_entry(python)


def _unset_mcp_zcode(data: Dict[str, Any], python: str = "") -> None:
    mcp = data.get("mcp")
    if not isinstance(mcp, dict):
        return
    servers = mcp.get("servers")
    if isinstance(servers, dict) and MCP_NAME in servers:
        del servers[MCP_NAME]
        if not servers:
            del mcp["servers"]
    if not mcp:
        del data["mcp"]


def _set_hooks_zcode(data: Dict[str, Any], python: str) -> None:
    """zcode hooks：hooks.events.<Event>，且必须 hooks.enabled=true 才会运行。"""
    hooks = data.setdefault("hooks", {})
    hooks["enabled"] = True
    events = hooks.setdefault("events", {})
    for ev in ZCODE_HOOK_EVENTS:
        groups = [g for g in (events.get(ev) or []) if not _is_our_group(g)]
        group: Dict[str, Any] = {"hooks": [{
            "type": "command", "command": _hook_cmd(python, ev), "timeout": HOOK_TIMEOUT_S,
        }]}
        if ev in HOOK_MATCHERS:
            group["matcher"] = HOOK_MATCHERS[ev]
        groups.append(group)
        events[ev] = groups


def _unset_hooks_zcode(data: Dict[str, Any], python: str = "") -> None:
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return
    events = hooks.get("events")
    if isinstance(events, dict):
        # 卸载用超集清理：ZCode/Claude 两宿主事件并集
        # （早期版本曾给 zcode 写过 PreCompact，一并摘掉）
        for ev in HOOK_EVENTS_UNINSTALL:
            if ev in events:
                rest = [g for g in (events[ev] or []) if not _is_our_group(g)]
                if rest:
                    events[ev] = rest
                else:
                    del events[ev]
        if not events:
            del hooks["events"]
    # 只剩我们加的 enabled 开关时一并摘除；用户自己的 hooks 键（timeoutMs 等）保留
    if hooks in ({}, {"enabled": True}):
        del data["hooks"]


def _set_hooks_claude(data: Dict[str, Any], python: str) -> None:
    """claude settings.json hooks：hooks.<Event>（无 events 包裹、无 enabled）。"""
    hooks = data.setdefault("hooks", {})
    for ev in CLAUDE_HOOK_EVENTS:
        groups = [g for g in (hooks.get(ev) or []) if not _is_our_group(g)]
        group: Dict[str, Any] = {"hooks": [{
            "type": "command", "command": _hook_cmd(python, ev),
        }]}
        if ev in HOOK_MATCHERS:
            group["matcher"] = HOOK_MATCHERS[ev]
        groups.append(group)
        hooks[ev] = groups


def _unset_hooks_claude(data: Dict[str, Any], python: str = "") -> None:
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return
    for ev in CLAUDE_HOOK_EVENTS:
        if ev in hooks:
            rest = [g for g in (hooks[ev] or []) if not _is_our_group(g)]
            if rest:
                hooks[ev] = rest
            else:
                del hooks[ev]
    if not hooks:
        del data["hooks"]


# ---------------------------------------------------------------------------
# TOML 宿主（codex）：文本级插入/摘除，只处理带标记的块
# ---------------------------------------------------------------------------

def _toml_str(value: str) -> str:
    """TOML 字符串：优先单引号字面量（Windows 路径反斜杠免转义）。"""
    if "'" not in value:
        return f"'{value}'"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_block(python: str) -> str:
    return (
        f"{TOML_BEGIN}\n"
        f"[mcp_servers.{MCP_NAME}]\n"
        f"command = {_toml_str(python)}\n"
        f"args = [{_toml_str(str(MCP_SERVER_PY))}]\n"
        f"{TOML_END}\n"
    )


def _toml_change(path: Path, python: Optional[str]) -> FileChange:
    """python=None 表示卸载。只在标记块外做纯文本级操作。"""
    try:
        before = _read_text(path) or ""
    except RuntimeError as e:
        return FileChange(path, error=str(e))
    pattern = re.compile(
        re.escape(TOML_BEGIN) + r".*?" + re.escape(TOML_END) + r"\n?",
        re.DOTALL,
    )
    body = pattern.sub("", before).rstrip("\n")
    if python is not None:
        after = (body + "\n\n" if body else "") + _toml_block(python)
    else:
        after = body + "\n" if body else ""  # 空内容 → after="" 视为删除
    if python is None and not body:
        return FileChange(path, before=before or None, after=None)
    return FileChange(path, before=before or None, after=after)


# ---------------------------------------------------------------------------
# 宿主定义与计划构建
# ---------------------------------------------------------------------------

@dataclass
class HostSpec:
    name: str
    targets: List[Tuple[str, Path]]  # (kind, path)，kind ∈ mcp|hooks|toml


def host_specs(home: Path) -> List[HostSpec]:
    return [
        HostSpec("zcode", [
            ("json", home / ".zcode" / "cli" / "config.json"),
        ]),
        HostSpec("claude", [
            ("json", home / ".claude.json"),
            ("json", home / ".claude" / "settings.json"),
        ]),
        HostSpec("codex", [
            ("toml", home / ".codex" / "config.toml"),
        ]),
        HostSpec("cursor", [
            ("json", home / ".cursor" / "mcp.json"),
        ]),
    ]


def _install_zcode(data: Dict[str, Any], python: str) -> None:
    _set_mcp_zcode(data, python)
    _set_hooks_zcode(data, python)


def _uninstall_zcode(data: Dict[str, Any], python: str = "") -> None:
    _unset_mcp_zcode(data)
    _unset_hooks_zcode(data)


def build_changes(hosts: List[str], home: Path, python: str,
                  uninstall: bool) -> List[Tuple[str, FileChange]]:
    """生成 (宿主名, 文件变更) 列表。所有 JSON mutator 统一为 (data, python) 签名。"""
    out: List[Tuple[str, FileChange]] = []
    # (宿主, 文件, 安装 mutator, 卸载 mutator)
    plan: List[Tuple[str, Path, Any, Any]] = [
        ("zcode", home / ".zcode" / "cli" / "config.json",
         _install_zcode, _uninstall_zcode),
        ("claude", home / ".claude.json", _set_mcp_flat, _unset_mcp_flat),
        ("claude", home / ".claude" / "settings.json",
         _set_hooks_claude, _unset_hooks_claude),
        ("codex", home / ".codex" / "config.toml", None, None),
        ("cursor", home / ".cursor" / "mcp.json", _set_mcp_flat, _unset_mcp_flat),
    ]
    for host, path, install_fn, uninstall_fn in plan:
        if host not in hosts:
            continue
        if host == "codex":
            out.append((host, _toml_change(path, None if uninstall else python)))
        else:
            fn = uninstall_fn if uninstall else install_fn
            out.append((host, _json_change(
                path, lambda d, f=fn, p=python: f(d, p),
                delete_if_empty=uninstall)))
    return out


def _mcp_configured(path: Path, kind: str) -> Optional[bool]:
    """该文件中是否已配置 autocondense；None = 无法判断（文件损坏等）。"""
    try:
        if kind == "toml":
            text = _read_text(path) or ""
            return "[mcp_servers.autocondense]" in text
        data = _load_json(path)
        if data is None:
            return None
        if path.name == "config.json" and ".zcode" in path.parts:
            mcp = data.get("mcp")
            return bool(isinstance(mcp, dict)
                        and MCP_NAME in (mcp.get("servers") or {}))
        if path.name == "settings.json":
            hooks = data.get("hooks")
            return any(_is_our_group(g) for ev in CLAUDE_HOOK_EVENTS
                       for g in ((hooks or {}).get(ev) or []))
        return MCP_NAME in (data.get("mcpServers") or {})
    except RuntimeError:
        return None


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

def _print_diff(change: FileChange) -> None:
    before_lines = (change.before or "").splitlines()
    after_lines = (change.after or "").splitlines()
    diff = difflib.unified_diff(
        before_lines, after_lines,
        fromfile=f"{change.path}（改前）", tofile=f"{change.path}（改后）",
        lineterm="")
    for line in diff:
        print(line)


def _describe(change: FileChange) -> str:
    if change.error:
        return f"跳过：{change.error}"
    if not change.changed:
        return "无变化（已是目标状态）"
    if change.before is None:
        return "新建文件"
    if change.after is None:
        return "删除文件（卸载后为空）"
    return "更新（写前自动备份）"


def cmd_list(home: Path) -> int:
    print(f"autocondense 安装器：--home={home}")
    for spec in host_specs(home):
        print(f"\n[{spec.name}]")
        for kind, path in spec.targets:
            flag = _mcp_configured(path, kind)
            state = {True: "已配置 autocondense", False: "未配置",
                     None: "无法解析（文件损坏？）"}[flag]
            what = "hooks" if path.name == "settings.json" else "MCP"
            if path.name == "config.json" and ".zcode" in path.parts:
                what = "MCP + hooks"
            print(f"  {what:<10} {path}   [{state}]")
    return 0


def run(hosts: List[str], home: Path, python: str,
        uninstall: bool, apply: bool) -> int:
    action = "卸载" if uninstall else "安装"
    mode = "写入" if apply else "dry-run（预览，不落盘；加 --yes 实际写入）"
    print(f"autocondense {action}：宿主 {','.join(hosts)}；Python={python}\n模式：{mode}\n")
    changes = build_changes(hosts, home, python, uninstall)
    exit_code = 0
    for host, change in changes:
        print(f"== [{host}] {change.path}")
        if change.error:
            print(f"   跳过：{change.error}")
            exit_code = 1
            continue
        print(f"   {_describe(change)}")
        if change.changed:
            _print_diff(change)
            if apply:
                bak = _apply(change)
                if bak:
                    print(f"   已备份：{bak}")
                print(f"   已{'卸载' if uninstall else '写入'}：{change.path}")
        print()
    if apply:
        print("完成。注意：宿主（ZCode/Claude Code/Codex/Cursor）需重启会话或应用后配置才生效。")
    else:
        print("以上为预览 diff，未写入任何文件。确认无误后加 --yes 执行。")
    return exit_code


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="installer.py",
        description="autocondense 接入安装器：写入/卸载各宿主的 MCP 与 hooks 配置")
    parser.add_argument("--hosts", default="all",
                        help=f"逗号分隔：{','.join(ALL_HOSTS)} 或 all（默认 all）")
    parser.add_argument("--home", default=str(Path.home()),
                        help="用户主目录（默认真实 home；测试时注入临时目录）")
    parser.add_argument("--python", default=sys.executable,
                        help="MCP/钩子使用的 Python 解释器（默认当前解释器）")
    parser.add_argument("--yes", action="store_true",
                        help="实际写入（默认 dry-run 只打印 diff）")
    parser.add_argument("--uninstall", action="store_true",
                        help="摘除 autocondense 写入的配置")
    parser.add_argument("--list", action="store_true", help="列出宿主与目标路径")
    args = parser.parse_args(argv)

    home = Path(args.home).expanduser()
    if args.list:
        return cmd_list(home)

    raw = [h.strip().lower() for h in args.hosts.split(",") if h.strip()]
    if "all" in raw:
        hosts = list(ALL_HOSTS)
    else:
        bad = [h for h in raw if h not in ALL_HOSTS]
        if bad:
            parser.error(f"未知宿主：{','.join(bad)}（可选：{','.join(ALL_HOSTS)},all）")
        hosts = raw
    if not hosts:
        parser.error("--hosts 为空")

    return run(hosts, home, args.python, args.uninstall, apply=args.yes)


if __name__ == "__main__":
    sys.exit(main())
