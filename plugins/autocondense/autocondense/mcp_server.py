"""autocondense 的 MCP 服务器（stdio 薄代理）。

把常驻 REST 服务（``python -m autocondense.service``，默认 127.0.0.1:8761）的
压缩判定/摘要提交流程暴露为 7 个 MCP 工具，供任意 MCP 宿主（ZCode / Claude
Code / Codex / Cursor）在会话中调用：

    condense_check(cwd, session_id)                检查是否需要压缩（命中候选返回判定表）
    condense_report(plan_items, cwd, session_id)   上报计划清单 → 返回判定
    condense_judge(cwd, task_finished, impact, ...)  提交大模型判定表 → 压缩或延迟
    condense_begin(cwd, session_id)                开始压缩：取模板与指引
    condense_commit(digest_md, trigger, cwd, ...)  提交摘要快照（分段写入 SME）
    condense_restore(query, cwd, top_n)            恢复最近快照 + SME 检索
    condense_status()                              服务/会话/配置摘要

架构（复刻记忆插件 sme/mcp_server.py 的成熟模式）：本进程是薄代理，唯一
数据落盘方是常驻 REST 服务。服务未运行时自动拉起（Windows 分离进程，POSIX
setsid），日志追加到系统临时目录 autocondense_service.log。无模型加载，秒级起。

会话解析：宿主 agent 通常不知道自己的 session_id，因此工具都带可选参数
``cwd``（当前工作目录）。本模块维护 ``data/mcp-session-map.json``
{project_hash → session_id}：首次对某 cwd 调用时自动创建会话并记入映射，
后续复用；显式传 session_id 则优先。

用法（宿主配置示例，installer.py 可自动写入）::

    {"mcp": {"servers": {"autocondense": {
        "type": "stdio",
        "command": "python",
        "args": ["C:/path/to/autocondense/autocondense/mcp_server.py"]
    }}}}

支持「脚本路径」与 ``python -m autocondense.mcp_server`` 两种启动方式。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from mcp.server.mcpserver import MCPServer

# 脚本路径方式运行时（python .../autocondense/mcp_server.py），仓库根不在
# sys.path，补上以保证 autocondense 包可导入；-m 方式下该插入是无害的冗余。
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from autocondense import __version__ as PKG_VERSION  # noqa: E402
from autocondense.config import atomic_write_text, resolve_data_dir  # noqa: E402
from autocondense.state import project_hash  # noqa: E402

DEFAULT_PORT = 8761
SERVICE_LOG_NAME = "autocondense_service.log"
SESSION_MAP_FILE = "mcp-session-map.json"
SERVICE_PID_FILE = "service.pid"

# 回环直连：httpx 不读环境代理（trust_env=False），系统代理会劫持 localhost
_NO_ENV_PROXY = {"trust_env": False}

mcp = MCPServer(
    name="autocondense",
    title="autocondense 会话压缩",
    description="AI 编程智能体会话上下文自动压缩：压缩候选判定、大模型判定表、生成快照、恢复上下文",
    instructions=(
        "使用纪律："
        "1) 会话开始或上下文被压缩/清空后，先调用 condense_restore 恢复上次快照；"
        "2) 每完成一批任务或每轮对话结束时调用 condense_check 检查是否需要压缩"
        "（用了计划/Todo 工具时改用 condense_report 上报计划清单）；"
        "3) 返回 should=JUDGE 时——立即基于会话真实状态填写判定表并调用 condense_judge 提交"
        "（这是模型自评，不要询问用户）：task_finished 任务是否收尾、impact 压缩影响 low/medium/high、"
        "safe_to_compress 有无未落盘的关键信息；"
        "4) condense_judge 返回 COMPRESS_NOW 时直接 condense_begin 获取模板 → 按模板写摘要 → "
        "condense_commit 提交，全程无需任何人确认；返回 DEFERRED 时什么都不做继续干活"
        "（服务端自动延迟若干轮后重新评估）；"
        "5) 带可选参数 cwd 的工具永远传你当前的工作目录。"
    ),
)


# ---------------------------------------------------------------------------
# 服务地址与拉起（每次调用动态读环境变量，便于测试注入端口）
# ---------------------------------------------------------------------------

def _port() -> int:
    try:
        return int(os.environ.get("AUTOCONDENSE_PORT", str(DEFAULT_PORT)))
    except ValueError:
        return DEFAULT_PORT


def _base_url() -> str:
    return f"http://127.0.0.1:{_port()}"


def _service_log_path() -> Path:
    return Path(tempfile.gettempdir()) / SERVICE_LOG_NAME


def _health_info(timeout: float = 3.0) -> Optional[Dict[str, Any]]:
    """GET /health：只认「本插件族」的应答体（status=ok 且带版本号）。

    端口上若是不明应用（非 JSON / 无版本字段），视为未就绪——绝不误用、绝不误杀。
    """
    try:
        r = httpx.get(f"{_base_url()}/health", timeout=timeout, **_NO_ENV_PROXY)
        if r.status_code != 200:
            return None
        data = r.json()
        if (isinstance(data, dict) and data.get("status") == "ok"
                and isinstance(data.get("version"), str)):
            return data
    except Exception:  # noqa: BLE001  网络层任何异常都视为未就绪
        return None
    return None


def _service_up(timeout: float = 3.0) -> bool:
    """GET /health 探活（3s 超时）。"""
    return _health_info(timeout) is not None


def _version_matches(info: Optional[Dict[str, Any]]) -> bool:
    """端口上的服务版本必须与本代理一致（插件更新后的残留旧进程不算就绪）。"""
    return info is not None and info.get("version") == PKG_VERSION


# 单飞锁：同一 MCP 进程内并发的工具调用只允许一个线程进入 spawn 窗口；
# 等待就绪的轮询在锁外进行，不互相阻塞。
_SPAWN_LOCK = threading.Lock()


def _spawn_service() -> None:
    """后台分离进程拉起常驻服务，stdout/stderr 追加到临时目录日志。"""
    flags = 0
    popen_kwargs: Dict[str, Any] = {}
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        # POSIX 侧脱离会话（setsid）：MCP 进程退出/终端关闭不带走服务
        popen_kwargs["start_new_session"] = True
    log_fh = open(_service_log_path(), "ab")
    try:
        subprocess.Popen(
            [sys.executable, "-m", "autocondense.service", "--port", str(_port())],
            cwd=str(REPO_ROOT),
            creationflags=flags,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            close_fds=True,
            **popen_kwargs,
        )
    finally:
        # 句柄已被子进程继承，父端可安全关闭
        log_fh.close()


def _read_service_pid(data_dir: Optional[Path] = None) -> Optional[int]:
    """读 data/service.pid（服务启动时自报身份）。缺失/损坏返回 None。"""
    base = Path(data_dir) if data_dir is not None else resolve_data_dir()
    try:
        data = json.loads((base / SERVICE_PID_FILE).read_text(encoding="utf-8"))
        pid = int(data.get("pid") or 0)
        return pid if pid > 0 else None
    except Exception:  # noqa: BLE001
        return None


def _find_port_pid(port: int) -> Optional[int]:
    """端口 → 监听进程 pid（netstat/lsof）。兜底：兼容无 pid 文件的旧版服务。"""
    try:
        if os.name == "nt":
            out = subprocess.run(["netstat", "-ano", "-p", "tcp"],
                                 capture_output=True, timeout=10, text=True).stdout or ""
            for line in out.splitlines():
                parts = line.split()
                # 形如：TCP  127.0.0.1:8761  0.0.0.0:0  LISTENING  12345
                if (len(parts) >= 5 and parts[0].upper() == "TCP"
                        and parts[3].upper() == "LISTENING"
                        and parts[1].rsplit(":", 1)[-1] == str(port)):
                    return int(parts[4])
        else:
            out = subprocess.run(["lsof", "-t", "-i", f"tcp:{port}", "-sTCP:LISTEN"],
                                 capture_output=True, timeout=10, text=True).stdout or ""
            for tok in out.split():
                return int(tok)
    except Exception:  # noqa: BLE001
        return None
    return None


def _terminate_pid(pid: int) -> bool:
    """结束指定进程（本插件自己拉起的常驻服务）。失败返回 False，不抛。"""
    try:
        if pid <= 4 or pid == os.getpid():  # 系统进程/自身绝不杀
            return False
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                           capture_output=True, timeout=10, check=False)
        else:
            import signal
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                os.kill(pid, signal.SIGKILL)
        return True
    except Exception:  # noqa: BLE001
        return False


def _takeover_stale_service(stale_info: Dict[str, Any]) -> None:
    """端口被旧版本常驻服务占用（插件更新后的残留进程）：定点结束后等端口空出。

    只处理 /health 已自证是 autocondense 的旧版本进程；识别不到 pid 则报错，
    绝不盲杀。
    """
    stale_ver = stale_info.get("version")
    pid = _read_service_pid()
    if pid is None:
        pid = _find_port_pid(_port())
    if pid is None or not _terminate_pid(pid):
        raise RuntimeError(
            f"端口 {_port()} 上是旧版 {stale_ver} 服务且无法安全接管"
            f"（pid={pid}），请手动结束该进程后重试")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _health_info() is None:
            return
        time.sleep(0.5)
    raise RuntimeError(
        f"旧版 {stale_ver} 服务未能退出（端口 {_port()} 仍被占用），请手动处理")


def _ensure_service(wait_seconds: float = 30.0, poll_interval: float = 0.5) -> None:
    """服务不在（或是旧版本）就接管/拉起并等就绪；超时抛 RuntimeError 指向日志路径。

    端口上若是旧版本服务，按 pid 文件/端口反查进程定点接管，保证宿主更新插件后
    依然全自动恢复，无需人工干预。服务无模型加载，秒级起，默认最多等 30 秒。
    """
    if _version_matches(_health_info()):
        return
    with _SPAWN_LOCK:
        info = _health_info()
        if _version_matches(info):
            # 双检：排队等锁期间，前面的线程已经把服务拉起来了
            return
        if info is not None:
            _takeover_stale_service(info)
        _spawn_service()
    # 等待段在锁外：并发调用各自轮询就绪，不占着 spawn 锁
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if _version_matches(_health_info()):
            return
        time.sleep(poll_interval)
    raise RuntimeError(
        f"autocondense 服务 {_wait_s(wait_seconds)} 内未就绪"
        f"（{_base_url()}），请查看日志 {_service_log_path()}"
    )


def _wait_s(seconds: float) -> str:
    return f"{seconds:.0f} 秒"


class ApiError(RuntimeError):
    """服务端 400 业务错误（errors 中文列表透传给调用方）。"""

    def __init__(self, errors: List[Any]):
        self.errors = [str(e) for e in errors]
        super().__init__("；".join(self.errors))


def _api(method: str, path: str, payload: Optional[Dict[str, Any]] = None,
         params: Optional[Dict[str, Any]] = None,
         timeout: float = 60.0) -> Dict[str, Any]:
    """调用常驻服务；4xx 业务错误转 ApiError（中文 errors 透传）。"""
    _ensure_service()
    try:
        r = httpx.request(method, f"{_base_url()}{path}", json=payload,
                          params=params, timeout=timeout, **_NO_ENV_PROXY)
    except httpx.HTTPError as e:
        raise RuntimeError(f"autocondense 服务请求失败：{e}") from e
    if r.status_code >= 400:
        try:
            body = r.json()
        except ValueError:
            body = {}
        errors = body.get("errors") if isinstance(body, dict) else None
        if errors:
            raise ApiError(errors)
        r.raise_for_status()
    try:
        return r.json()
    except ValueError as e:
        raise RuntimeError(f"服务响应不是 JSON：{e}") from e


# ---------------------------------------------------------------------------
# 会话解析：cwd → session_id 映射（data/mcp-session-map.json，原子写）
# ---------------------------------------------------------------------------

def _session_map_path() -> Path:
    return resolve_data_dir() / SESSION_MAP_FILE


def _load_session_map() -> Dict[str, str]:
    try:
        data = json.loads(_session_map_path().read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_session_map(mapping: Dict[str, str]) -> None:
    atomic_write_text(_session_map_path(),
                      json.dumps(mapping, ensure_ascii=False, indent=2) + "\n")


def _new_session_id(cwd: str) -> str:
    return f"mcp-{project_hash(cwd)[:8]}-{uuid.uuid4().hex[:4]}"


def _resolve_session(cwd: str, session_id: str = "") -> str:
    """显式 session_id 优先；否则按 cwd 的 project_hash 复用/创建会话。"""
    if session_id:
        return session_id
    if not cwd:
        raise ValueError(
            "无法确定会话：请传 cwd（你当前的工作目录），或显式传 session_id"
        )
    h = project_hash(cwd)
    mapping = _load_session_map()
    sid = mapping.get(h)
    if sid:
        return sid
    sid = _new_session_id(cwd)
    _api("POST", "/session/start", {"session_id": sid, "cwd": cwd})
    mapping[h] = sid
    _save_session_map(mapping)
    return sid


# ---------------------------------------------------------------------------
# 结果格式化（中文可读，把 should/reason/关键信号、next_instruction 原文带回）
# ---------------------------------------------------------------------------

def _fmt_decision(d: Dict[str, Any]) -> str:
    sig = d.get("signals") or {}
    lines: List[str] = []
    should = d.get("should", "?")
    trigger = d.get("trigger")
    head = f"判定：{should}" + (f"（触发：{trigger}）" if trigger else "")
    if should == "NO":
        head += "，暂不压缩"
    elif d.get("urgency") == "urgent":
        head += "，紧急（水位危急）"
    lines.append(head)
    if d.get("reason"):
        lines.append(f"原因：{d['reason']}")
    bits = []
    if "plan_total" in sig:
        bits.append(f"计划 {sig.get('plan_completed', 0)}/{sig.get('plan_total', 0)} 完成、"
                    f"进行中 {sig.get('plan_in_progress', 0)}")
    if sig.get("turns_since_condense") is not None:
        bits.append(f"距上次压缩 {sig.get('turns_since_condense')} 轮")
    if sig.get("est_tokens") is not None:
        bits.append(f"估算 {sig.get('est_tokens')} token")
    if sig.get("water_level") is not None:
        bits.append(f"水位 {sig.get('water_level'):.0%}")
    if sig.get("cooldown_active"):
        bits.append("冷却中")
    if bits:
        lines.append("信号：" + "；".join(str(b) for b in bits))
    jf_fields = (d.get("judgment_form") or {}).get("fields") or {}
    if jf_fields:
        lines.append("判定表字段：" + "、".join(str(k) for k in jf_fields))
    if d.get("action_hint"):
        lines.append(f"动作建议：{d['action_hint']}")
    return "\n".join(lines)


def _fmt_snapshots(snaps: List[Dict[str, Any]]) -> List[str]:
    out = []
    for i, s in enumerate(snaps or [], 1):
        out.append(f"{i}. {s.get('ts')} [{s.get('trigger')}] {s.get('title')}（{s.get('char_count')} 字）")
    return out


# ---------------------------------------------------------------------------
# 七个 MCP 工具
# ---------------------------------------------------------------------------

@mcp.tool()
def condense_check(cwd: str = "", session_id: str = "") -> str:
    """检查当前会话是否需要压缩上下文。每完成一批任务或每轮对话结束时调用一次。

    Args:
        cwd: 当前工作目录（必传之一：cwd 或 session_id；首次传 cwd 会自动建立会话）
        session_id: 会话 ID（可选，显式传入则优先于 cwd）
    """
    try:
        sid = _resolve_session(cwd, session_id)
        d = _api("GET", "/check", params={"session_id": sid})
        return _fmt_decision(d)
    except (RuntimeError, ValueError) as e:
        return f"检查失败：{e}"


@mcp.tool()
def condense_report(plan_items: str, cwd: str = "", session_id: str = "") -> str:
    """上报当前任务计划清单，返回是否需要压缩的判定。用 TodoWrite/计划工具后或阶段性汇报时调用。

    Args:
        plan_items: 计划条目的紧凑 JSON 数组字符串，如
            [{"title":"实现A","status":"in_progress"},{"title":"实现B","status":"completed"}]，
            status 取 pending | in_progress | completed（大小写与 done 兼容）
        cwd: 当前工作目录（首次调用会用它建立会话）
        session_id: 会话 ID（可选，显式传入则优先于 cwd）
    """
    try:
        items = json.loads(plan_items)
    except ValueError:
        return ("plan_items 不是合法 JSON。请传紧凑 JSON 数组字符串，例如："
                '[{"title":"任务A","status":"completed"},{"title":"任务B","status":"pending"}]')
    if not isinstance(items, list):
        return "plan_items 必须是 JSON 数组（每项 {\"title\":..., \"status\":...}）。"
    norm = []
    for it in items:
        if not isinstance(it, dict):
            continue
        norm.append({"title": str(it.get("title", "")), "status": str(it.get("status", "pending"))})
    try:
        sid = _resolve_session(cwd, session_id)
        d = _api("POST", "/report", {"session_id": sid, "cwd": cwd, "plan_items": norm})
        return f"已上报 {len(norm)} 项计划（会话 {sid}）。\n" + _fmt_decision(d)
    except (RuntimeError, ValueError) as e:
        return f"上报失败：{e}"


@mcp.tool()
def condense_judge(cwd: str, task_finished: bool, impact: str,
                   safe_to_compress: bool, impact_reason: str = "",
                   reason: str = "", session_id: str = "") -> str:
    """提交大模型压缩判定表。condense_check / condense_report / Stop 钩子返回 should=JUDGE 时
    立即调用（模型自评，不询问用户）：按当前会话真实状态如实填写，服务端结合判定矩阵决定立即压缩或延迟。

    Args:
        cwd: 当前工作目录（必填；首次传 cwd 会自动建立/复用会话映射）
        task_finished: 当前批次任务是否已收尾（计划清空/目标达成）
        impact: 现在压缩对当前工作的影响，三选一："low" | "medium" | "high"
        safe_to_compress: 是否不存在未保存/易丢失的关键上下文
        impact_reason: 影响判定的简要理由（可选，默认空）
        reason: 整体判定理由（可选，默认空）
        session_id: 会话 ID（可选，显式传入则优先于 cwd）
    """
    try:
        sid = _resolve_session(cwd, session_id)
        d = _api("POST", "/judge", {
            "session_id": sid,
            "cwd": cwd,
            "verdict": {
                "task_finished": task_finished,
                "impact": impact,
                "impact_reason": impact_reason,
                "safe_to_compress": safe_to_compress,
                "reason": reason,
            },
        })
    except ApiError as e:
        return ("判定提交失败：\n"
                + "\n".join(f"- {line}" for line in e.errors)
                + "\n请修正后重新调用 condense_judge。")
    except (RuntimeError, ValueError) as e:
        return f"判定提交失败：{e}"
    lines = [f"已提交判定（会话 {sid}）。"]
    decision = d.get("decision", "?")
    head = f"判定结果：{decision}"
    if d.get("urgency"):
        head += f"（{d['urgency']} 档）"
    lines.append(head)
    if d.get("reason"):
        lines.append(f"原因：{d['reason']}")
    if d.get("defer_until_turn") is not None:
        lines.append(f"延迟窗口：第 {d['defer_until_turn']} 轮后自动重新评估")
    if d.get("action_hint"):
        lines.append(f"动作建议：{d['action_hint']}")
    return "\n".join(lines)


@mcp.tool()
def condense_begin(cwd: str = "", session_id: str = "") -> str:
    """开始压缩流程：获取摘要模板与填写指引。condense_judge 返回 COMPRESS_NOW 后立即调用；
    用户主动 /condense 命令也直接调用（意图明确，无需再走判定）。

    Args:
        cwd: 当前工作目录
        session_id: 会话 ID（可选，显式传入则优先于 cwd）
    """
    try:
        sid = _resolve_session(cwd, session_id)
        d = _api("POST", "/begin", {"session_id": sid, "cwd": cwd})
    except (RuntimeError, ValueError) as e:
        return f"进入压缩流程失败：{e}"
    return (
        f"已进入压缩流程（会话 {sid}，condensing=true）。"
        "请把下面的模板复制出来，基于当前会话内容逐节填写：\n"
        f"\n【填写指引】\n{d.get('guidance', '')}\n"
        f"\n【必填章节】\n{'、'.join(d.get('required_sections') or [])}\n"
        f"\n【模板】\n{d.get('template', '')}\n"
        "\n填完后把整份 markdown 作为 digest_md 调用 condense_commit 提交。"
    )


@mcp.tool()
def condense_commit(digest_md: str, trigger: str = "", cwd: str = "",
                    session_id: str = "") -> str:
    """提交会话摘要快照。condense_begin 拿到模板并填写完成后调用；校验失败会返回错误清单，按提示修正后重提。

    Args:
        digest_md: 填写好的整份摘要 markdown（含全部必填章节，总长 ≥200 字符）
        trigger: 触发原因（可选：plan_done | task_done | water_level | turns | user，缺省取会话最近触发或 judge）
        cwd: 当前工作目录
        session_id: 会话 ID（可选，显式传入则优先于 cwd）
    """
    try:
        sid = _resolve_session(cwd, session_id)
        payload: Dict[str, Any] = {"session_id": sid, "cwd": cwd, "digest_md": digest_md}
        if trigger:
            payload["trigger"] = trigger
        # commit 含去重检索（逐段语义查询）+ 批量入库，SME 在线时实测可达 ~90 秒，
        # 必须比 _api 默认 60 秒宽，否则真实代理路径会先超时（服务端其实能完成）
        d = _api("POST", "/commit", payload, timeout=300.0)
    except ApiError as e:
        return (
            "提交失败，摘要校验未通过：\n"
            + "\n".join(f"- {line}" for line in e.errors)
            + "\n请修正 digest_md 后重新调用 condense_commit（必填章节标题必须原样保留）。"
        )
    except (RuntimeError, ValueError) as e:
        return f"提交失败：{e}"
    sme = d.get("sme") or {}
    segments_written = d.get("segments_written") or 0
    if sme.get("stored"):
        if segments_written:
            sme_line = (f"SME 记忆入库：成功（分段画像 {segments_written} 条，"
                        f"首条 memory_id={sme.get('memory_id')}）")
        else:
            sme_line = f"SME 记忆入库：成功（整篇，memory_id={sme.get('memory_id')}）"
    else:
        sme_line = f"SME 记忆入库：未入库（{sme.get('error') or 'SME 服务不在线'}）——不影响本地快照"
    return (
        f"快照已保存：{d.get('snapshot_path')}\n"
        f"{sme_line}\n"
        f"下一步：{d.get('next_instruction')}"
    )


@mcp.tool()
def condense_restore(query: str = "", cwd: str = "", top_n: int = 3) -> str:
    """恢复最近的历史快照（含 SME 记忆检索）。会话开始或上下文被压缩/清空后首先调用。

    Args:
        query: SME 记忆检索关键词（可选，缺省用项目名）
        cwd: 当前工作目录（必填，用于定位该项目的快照）
        top_n: 取最近 N 份快照（默认 3）
    """
    if not cwd:
        return "恢复失败：请传 cwd（你当前的工作目录），用于定位该项目的快照。"
    try:
        params: Dict[str, Any] = {"cwd": cwd, "top_n": top_n}
        if query:
            params["query"] = query
        d = _api("GET", "/restore", params=params)
    except RuntimeError as e:
        return f"恢复失败：{e}"
    snaps = d.get("snapshots") or []
    if not snaps:
        return f"项目 {d.get('project')} 还没有历史快照，无需恢复。"
    restore = d.get("restore") or {}
    lines = [f"项目 {d.get('project')} 的历史快照（最新在前）："]
    lines.extend(_fmt_snapshots(snaps))
    lines.append("\n【恢复内容（按优先级装配：未完成/恢复指针优先）】")
    lines.append(restore.get("text") or "")
    if restore.get("truncated"):
        lines.append(f"（以上内容已截断，完整快照：{restore.get('full_file')}，需要时用 Read 工具读取）")
    actions = d.get("next_actions") or []
    if actions:
        lines.append("恢复后动作：" + "；".join(str(a) for a in actions))
    sme_hits = d.get("sme_results") or []
    if sme_hits:
        lines.append("\n【SME 记忆检索】")
        for m in sme_hits:
            txt = str(m.get("text") or "")
            lines.append(f"[{m.get('id')}] score={m.get('score', 0):.2f} {txt[:80]}")
    return "\n".join(lines)


@mcp.tool()
def condense_status() -> str:
    """查看 autocondense 服务状态：会话列表（含最近判定与延迟窗口）、关键配置、最近事件。排障或了解当前压缩配置时调用。"""
    try:
        d = _api("GET", "/status")
    except RuntimeError as e:
        return f"状态查询失败：{e}"
    cfg = d.get("config") or {}
    sessions = d.get("sessions") or []
    events = d.get("recent_events") or []
    enabled = d.get("enabled", cfg.get("enabled"))
    lines = [f"autocondense 服务：{'启用（全自动）' if enabled else '已关闭（enabled=false，不干预）'}，"
             f"会话 {len(sessions)} 个，最近事件 {len(events)} 条。"]
    for s in sessions[:5]:
        line = (f"- {s.get('session_id')} 项目 {s.get('cwd')} "
                f"计划 {s.get('plan_completed')}/{s.get('plan_total')} 完成 "
                f"轮次 {s.get('turns_since_condense')}")
        last_judge = s.get("last_judge") or {}
        if last_judge.get("result"):
            line += f" 上次判定 {last_judge.get('result')}"
        if s.get("defer_until_turn") is not None:
            line += f"（延迟至第 {s.get('defer_until_turn')} 轮）"
        lines.append(line)
    sme = cfg.get("sme") or {}
    lines.append(
        f"配置：enabled={'开' if enabled else '关'} "
        f"cooldown={cfg.get('cooldown_minutes')} 分钟 "
        f"turns_threshold={cfg.get('turns_threshold')} "
        f"water_threshold={cfg.get('water_threshold')} "
        f"judge_defer_turns={d.get('judge_defer_turns', cfg.get('judge_defer_turns'))} "
        f"restore_top_n={cfg.get('restore_top_n')} "
        f"SME={'启用（' + str(sme.get('base_url')) + '，分段=' + str(sme.get('split_segments')) + '）' if sme.get('enabled') else '停用'}"
    )
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run(transport="stdio")
