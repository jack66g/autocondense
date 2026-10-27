"""会话状态、快照与事件流水的线程安全存储（RLock）。

持久化布局（全部在 data/ 下）：
- sessions-state.json   会话内存态快照（原子写、损坏容忍）
- sessions.jsonl        事件流水（追加）
- snapshots/{hash}/index.json                每项目快照索引（原子写）
- snapshots/{hash}/{YYYYmmdd-HHMMSS}-{trigger}.md   快照正文
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import Config, atomic_write_text, quarantine_corrupt

logger = logging.getLogger("autocondense")

SNAPSHOTS_DIR = "snapshots"
SESSIONS_STATE_FILE = "sessions-state.json"
EVENTS_FILE = "sessions.jsonl"
INDEX_FILE = "index.json"
DELTA_STATE_FILE = "delta-state.json"

def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def project_hash(cwd: str) -> str:
    """sha1(规范化绝对路径)[:10]；normcase 保证 Windows 大小写不敏感。"""
    resolved = str(Path(cwd or ".").resolve())
    norm = os.path.normcase(os.path.normpath(resolved))
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:10]


def project_name(cwd: str) -> str:
    return Path(cwd).name if cwd else ""


def normalize_status(raw: Any) -> str:
    """归一化状态值：大小写无关，done→completed，未知值回落 pending。"""
    s = str(raw or "").strip().lower()
    if s in ("completed", "done"):
        return "completed"
    if s in ("in_progress", "in-progress", "inprogress"):
        return "in_progress"
    return "pending"


@dataclass
class SessionState:
    """单个会话的全部状态。"""

    session_id: str
    cwd: str = ""
    transcript_path: Optional[str] = None
    context_tokens: Optional[int] = None
    est_tokens: Optional[int] = None
    # 最近 {turn, tokens} 采样（最多 10 条，来自 hook/接口上报的真实值，
    # 供 rounds_to_full 估算每轮平均 token 增量）
    token_samples: List[Dict[str, Any]] = field(default_factory=list)
    # context_tokens 最近一次上报时的轮次（判断真实值是否仍然新鲜）
    context_tokens_turn: Optional[int] = None
    # 活动轮记录（idle 信号用；None = 无记录，绝不据此误报 idle）
    last_file_turn: Optional[int] = None   # 最近一次文件修改类工具（Edit/Write/...）
    last_fail_turn: Optional[int] = None   # 最近一次工具失败（PostToolUseFailure）
    # [{title, status(pending|in_progress|completed), first_seen_turn, completed_turn}]
    plan_items: List[Dict[str, Any]] = field(default_factory=list)
    # 从计划里消失的 completed 条目归档（供 task_done 评分）；mark_condensed 时清空
    plan_archive: List[Dict[str, Any]] = field(default_factory=list)
    turns: int = 0
    turns_since_condense: int = 0
    last_condense_at: Optional[str] = None
    cooldown_until: Optional[str] = None
    last_trigger: Optional[str] = None
    condensing: bool = False
    # 大模型判定记录：{turn, ts, verdict(原样 dict), result("APPROVED"|"DEFERRED")}
    last_judge: Optional[Dict[str, Any]] = None
    # 判定 DEFERRED 后的延迟窗口：当前轮次 < defer_until_turn 时不允许重新判定
    # （urgent 候选可无视窗口）；压缩 commit 成功后清空
    defer_until_turn: Optional[int] = None
    # 连续 DEFERRED 次数（指数退避：judge_defer_turns * 2^streak，封顶 judge_defer_max_turns）
    defer_streak: int = 0
    # 本次 /begin 决定的摘要模式（"delta"|"full"），/commit 据此打 delta 标记，压缩后清空
    begin_mode: Optional[str] = None
    created_at: str = field(default_factory=_now_iso)
    last_seen: str = field(default_factory=_now_iso)

    def summary(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "cwd": self.cwd,
            "transcript_path": self.transcript_path,
            "context_tokens": self.context_tokens,
            "context_tokens_turn": self.context_tokens_turn,
            "token_samples": list(self.token_samples),
            "est_tokens": self.est_tokens,
            "plan_total": len(self.plan_items),
            "plan_completed": sum(1 for it in self.plan_items if it.get("status") == "completed"),
            "plan_in_progress": sum(1 for it in self.plan_items if it.get("status") == "in_progress"),
            "turns": self.turns,
            "turns_since_condense": self.turns_since_condense,
            "last_condense_at": self.last_condense_at,
            "cooldown_until": self.cooldown_until,
            "last_trigger": self.last_trigger,
            "condensing": self.condensing,
            "last_judge": self.last_judge,
            "defer_until_turn": self.defer_until_turn,
            "defer_streak": self.defer_streak,
            "begin_mode": self.begin_mode,
            "last_file_turn": self.last_file_turn,
            "last_fail_turn": self.last_fail_turn,
            "created_at": self.created_at,
            "last_seen": self.last_seen,
        }


_FIELDS = {f.name for f in dataclasses.fields(SessionState)}


def _session_from_dict(d: Dict[str, Any]) -> Optional[SessionState]:
    if not isinstance(d, dict):
        return None
    known = {k: v for k, v in d.items() if k in _FIELDS}
    try:
        return SessionState(**known)
    except TypeError:
        return None


class StateStore:
    """线程安全的会话/快照/事件存储。"""

    def __init__(self, data_dir: Path, config: Optional[Config] = None):
        self.data_dir = Path(data_dir)
        self.config = config if config is not None else Config()
        self._lock = threading.RLock()
        self._sessions: Dict[str, SessionState] = {}
        self._load_sessions()

    # ------------------------------------------------------------------
    # 会话
    # ------------------------------------------------------------------

    def _load_sessions(self) -> None:
        path = self.data_dir / SESSIONS_STATE_FILE
        if not path.exists():
            return
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            backup = quarantine_corrupt(path)
            logger.warning("会话状态文件损坏（%s），已清空重来%s", e,
                           f"，现场保留为 {backup.name}" if backup else "")
            return
        raw = None
        if isinstance(loaded, dict):
            raw = loaded.get("sessions")
        elif isinstance(loaded, list):
            raw = loaded
        if isinstance(raw, dict):
            raw = list(raw.values())
        if not isinstance(raw, list):
            logger.warning("会话状态文件结构异常（sessions 字段非列表/字典），已忽略")
            return
        for item in raw:
            s = _session_from_dict(item)
            if s is not None and s.session_id:
                self._sessions[s.session_id] = s

    def _save_sessions(self) -> None:
        payload = {
            "version": 1,
            "saved_at": _now_iso(),
            "sessions": {sid: asdict(s) for sid, s in self._sessions.items()},
        }
        atomic_write_text(self.data_dir / SESSIONS_STATE_FILE,
                          json.dumps(payload, ensure_ascii=False, indent=2))

    def upsert_session(self, session_id: str, cwd: Optional[str] = None,
                       transcript_path: Optional[str] = None,
                       context_tokens: Optional[int] = None,
                       est_tokens: Optional[int] = None) -> SessionState:
        with self._lock:
            s = self._sessions.get(session_id)
            if s is None:
                s = SessionState(session_id=session_id, cwd=cwd or "")
                self._sessions[session_id] = s
            else:
                if cwd:
                    s.cwd = cwd
            if transcript_path:
                s.transcript_path = transcript_path
            if context_tokens is not None:
                # 真实 token 上报：记当前轮次 + 追加采样（水位真实化/轮增估算用）
                s.context_tokens = context_tokens
                s.context_tokens_turn = s.turns
                s.token_samples.append({"turn": s.turns, "tokens": context_tokens})
                if len(s.token_samples) > 10:
                    s.token_samples = s.token_samples[-10:]
            if est_tokens is not None:
                s.est_tokens = est_tokens
            s.last_seen = _now_iso()
            self._save_sessions()
            return s

    def mark_activity(self, session_id: str, kind: str) -> Optional[SessionState]:
        """记录活动轮（kind: "file"=文件修改类工具 / "fail"=工具失败）。

        idle 信号语义：有活动记录时，turns - 最近活动轮 才是「停歇轮数」；
        记录缺失（老会话 / hook 未生效）时保持 None，绝不误报 idle。
        """
        with self._lock:
            s = self._sessions.get(session_id)
            if s is None:
                return None
            if kind == "file":
                s.last_file_turn = s.turns
            elif kind == "fail":
                s.last_fail_turn = s.turns
            s.last_seen = _now_iso()
            self._save_sessions()
            return s

    def get_session(self, session_id: str) -> Optional[SessionState]:
        with self._lock:
            return self._sessions.get(session_id)

    def all_sessions(self) -> List[SessionState]:
        with self._lock:
            return sorted(self._sessions.values(), key=lambda s: s.last_seen, reverse=True)

    def set_condensing(self, session_id: str, flag: bool) -> Optional[SessionState]:
        with self._lock:
            s = self._sessions.get(session_id)
            if s is None:
                return None
            s.condensing = flag
            s.last_seen = _now_iso()
            self._save_sessions()
            return s

    def set_est_tokens(self, session_id: str, est: Optional[int]) -> Optional[SessionState]:
        with self._lock:
            s = self._sessions.get(session_id)
            if s is None or s.est_tokens == est:
                return s
            s.est_tokens = est
            self._save_sessions()
            return s

    def bump_turn(self, session_id: str) -> Optional[SessionState]:
        with self._lock:
            s = self._sessions.get(session_id)
            if s is None:
                return None
            s.turns += 1
            s.turns_since_condense += 1
            s.last_seen = _now_iso()
            self._save_sessions()
            return s

    def mark_condensed(self, session_id: str, trigger: str) -> Optional[SessionState]:
        """记录一次压缩：进冷却、清零计数、清空归档、判定延迟窗口与退避计数。"""
        with self._lock:
            s = self._sessions.get(session_id)
            if s is None:
                return None
            now = datetime.now()
            s.last_condense_at = now.isoformat(timespec="seconds")
            s.cooldown_until = (now + timedelta(minutes=self.config.cooldown_minutes)).isoformat(timespec="seconds")
            s.last_trigger = trigger
            s.turns_since_condense = 0
            s.condensing = False
            s.plan_archive = []
            s.defer_until_turn = None
            s.defer_streak = 0       # 压缩成功 → DEFERRED 连击归零（退避重置）
            s.begin_mode = None      # 本次摘要模式已消费
            s.last_seen = _now_iso()
            self._save_sessions()
            return s

    # ------------------------------------------------------------------
    # 大模型判定（judge）
    # ------------------------------------------------------------------

    def record_judge(self, session_id: str, verdict: Dict[str, Any],
                     result: str) -> Optional[SessionState]:
        """记录一次大模型判定：{turn, ts, verdict(原样), result(APPROVED|DEFERRED)}。"""
        with self._lock:
            s = self._sessions.get(session_id)
            if s is None:
                return None
            s.last_judge = {
                "turn": s.turns,
                "ts": _now_iso(),
                "verdict": verdict,
                "result": result,
            }
            s.last_seen = _now_iso()
            self._save_sessions()
            return s

    def set_defer_until_turn(self, session_id: str,
                             turn: Optional[int]) -> Optional[SessionState]:
        """设置/清空判定延迟窗口（DEFERRED 后设为当时轮次 + 退避步长）。"""
        with self._lock:
            s = self._sessions.get(session_id)
            if s is None:
                return None
            s.defer_until_turn = turn
            s.last_seen = _now_iso()
            self._save_sessions()
            return s

    def set_defer_streak(self, session_id: str, streak: int) -> Optional[SessionState]:
        """设置连续 DEFERRED 次数（指数退避因子；通过判定/压缩成功时归零）。"""
        with self._lock:
            s = self._sessions.get(session_id)
            if s is None:
                return None
            s.defer_streak = int(streak)
            s.last_seen = _now_iso()
            self._save_sessions()
            return s

    def set_begin_mode(self, session_id: str, mode: Optional[str]) -> Optional[SessionState]:
        """记录 /begin 决定的摘要模式（"delta"|"full"），/commit 消费后清空。"""
        with self._lock:
            s = self._sessions.get(session_id)
            if s is None:
                return None
            s.begin_mode = mode
            s.last_seen = _now_iso()
            self._save_sessions()
            return s

    # ------------------------------------------------------------------
    # 计划合并
    # ------------------------------------------------------------------

    def update_plan(self, session_id: str, items: List[Dict[str, Any]]) -> Optional[SessionState]:
        """合并计划快照（以传入列表为准）：
        - 新条目记 first_seen_turn=当前 turns；初见即 completed 的同时记 completed_turn；
        - 状态变为 completed 记 completed_turn=当前 turns；
        - 消失的 completed 条目移入 plan_archive 保留跨度供 task_done 评分；
          消失的 pending/in_progress 条目直接丢弃。
        """
        with self._lock:
            s = self._sessions.get(session_id)
            if s is None:
                return None
            current = {it.get("title"): it for it in s.plan_items}
            turns = s.turns
            merged: Dict[str, Dict[str, Any]] = {}
            for raw in items or []:
                if not isinstance(raw, dict):
                    continue
                title = str(raw.get("title") or raw.get("content") or "").strip()
                if not title:
                    continue
                status = normalize_status(raw.get("status"))
                prev = current.get(title)
                if prev is not None:
                    entry = dict(prev)
                    entry["status"] = status
                    if status == "completed" and prev.get("status") != "completed":
                        entry["completed_turn"] = turns
                else:
                    entry = {
                        "title": title,
                        "status": status,
                        "first_seen_turn": turns,
                        "completed_turn": turns if status == "completed" else None,
                    }
                merged[title] = entry
            for title, prev in current.items():
                if title not in merged and prev.get("status") == "completed":
                    s.plan_archive.append({
                        "title": title,
                        "first_seen_turn": prev.get("first_seen_turn"),
                        "completed_turn": prev.get("completed_turn"),
                    })
            s.plan_items = list(merged.values())
            s.last_seen = _now_iso()
            self._save_sessions()
            return s

    def clear_completed_plan_items(self, session_id: str) -> Optional[SessionState]:
        """压缩提交后清理已完成条目（pending/in_progress 保留）。

        注意：这里被消费的 completed 条目不进 plan_archive——它已被本次压缩覆盖，
        再归档会在冷却结束后重复触发 task_done。
        """
        with self._lock:
            s = self._sessions.get(session_id)
            if s is None:
                return None
            s.plan_items = [it for it in s.plan_items if it.get("status") != "completed"]
            self._save_sessions()
            return s

    # ------------------------------------------------------------------
    # 事件流水
    # ------------------------------------------------------------------

    def append_event(self, event: str, session_id: Optional[str] = None,
                     cwd: Optional[str] = None, data: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            try:
                self.data_dir.mkdir(parents=True, exist_ok=True)
                rec = {
                    "ts": _now_iso(),
                    "event": event,
                    "session_id": session_id,
                    "cwd": cwd,
                    "data": data or {},
                }
                with open(self.data_dir / EVENTS_FILE, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except OSError as e:
                logger.warning("事件流水写入失败（不阻断主流程）：%s", e)

    def recent_events(self, n: int) -> List[Dict[str, Any]]:
        with self._lock:
            path = self.data_dir / EVENTS_FILE
            if not path.exists():
                return []
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError as e:
                logger.warning("事件流水读取失败：%s", e)
                return []
            out: List[Dict[str, Any]] = []
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning("事件流水存在坏行，已跳过")
            return out[-n:] if n and n > 0 else out

    # ------------------------------------------------------------------
    # 快照
    # ------------------------------------------------------------------

    def _snap_dir(self, project_h: str) -> Path:
        return self.data_dir / SNAPSHOTS_DIR / project_h

    def _read_index(self, project_h: str) -> List[Dict[str, Any]]:
        path = self._snap_dir(project_h) / INDEX_FILE
        if not path.exists():
            return []
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                return loaded
            logger.warning("快照索引结构异常（%s），按空处理", path)
            return []
        except Exception as e:
            backup = quarantine_corrupt(path)
            logger.warning("快照索引损坏（%s），已重建%s", e,
                           f"，现场保留为 {backup.name}" if backup else "")
            return []

    def _write_index(self, project_h: str, entries: List[Dict[str, Any]]) -> None:
        entries.sort(key=lambda e: (str(e.get("ts") or ""), str(e.get("file") or "")), reverse=True)
        atomic_write_text(self._snap_dir(project_h) / INDEX_FILE,
                          json.dumps(entries, ensure_ascii=False, indent=2))

    def save_snapshot(self, digest_md: str, trigger: str, session_id: str,
                      delta: bool = False) -> Tuple[Path, Dict[str, Any]]:
        """写快照正文 + 更新 index.json + 追加 condense 事件，返回 (路径, 索引条目)。

        delta=True 表示这是一份增量快照（恢复时沿 delta 链向前合并直到全量锚点）；
        老索引条目无 delta 字段，读取方按全量（False）处理。
        """
        with self._lock:
            s = self._sessions.get(session_id)
            if s is None:
                raise KeyError(f"会话不存在：{session_id}")
            h = project_hash(s.cwd)
            pdir = self._snap_dir(h)
            pdir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            safe = re.sub(r"[^\w\-]+", "_", trigger or "manual").strip("_") or "manual"
            fname = f"{ts}-{safe}.md"
            i = 1
            while (pdir / fname).exists():
                fname = f"{ts}-{safe}-{i}.md"
                i += 1
            fpath = pdir / fname
            atomic_write_text(fpath, digest_md)
            title = ""
            for line in digest_md.splitlines():
                if line.strip():
                    title = line.strip()
                    break
            entry = {
                "file": str(fpath),
                # 毫秒精度：同一秒内连续提交（delta 链场景）也能按 ts 稳定排序
                "ts": datetime.now().isoformat(timespec="milliseconds"),
                "trigger": trigger,
                "session_id": session_id,
                "title": title,
                "char_count": len(digest_md),
                "sme_memory_id": None,
                "delta": bool(delta),
            }
            index = self._read_index(h)
            index.append(entry)
            self._write_index(h, index)
            self.append_event("condense", session_id, s.cwd,
                              {"trigger": trigger, "file": str(fpath),
                               "char_count": len(digest_md), "delta": bool(delta)})
            return fpath, entry

    # ------------------------------------------------------------------
    # 项目级 delta 状态（snapshots/{hash}/delta-state.json）
    # ------------------------------------------------------------------

    def read_delta_state(self, project_h: str) -> int:
        """读「距上一份全量快照已累积的增量份数」；缺失/损坏返回 0。"""
        path = self._snap_dir(project_h) / DELTA_STATE_FILE
        if not path.exists():
            return 0
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            value = data.get("delta_since_full") if isinstance(data, dict) else None
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
            logger.warning("delta 状态值异常（%r），按 0 处理", value)
            return 0
        except Exception as e:
            logger.warning("delta 状态文件损坏（%s），按 0 处理", e)
            return 0

    def write_delta_state(self, project_h: str, value: int) -> None:
        """原子写 delta_since_full（delta 提交 +1，全量提交归 0）。"""
        atomic_write_text(self._snap_dir(project_h) / DELTA_STATE_FILE,
                          json.dumps({"delta_since_full": int(value)},
                                     ensure_ascii=False) + "\n")

    def list_snapshots(self, project_h: str, top_n: Optional[int] = None) -> List[Dict[str, Any]]:
        """按时间倒序返回索引条目（最新在前），可截取前 top_n 条。"""
        with self._lock:
            entries = self._read_index(project_h)
            entries = sorted(entries,
                             key=lambda e: (str(e.get("ts") or ""), str(e.get("file") or "")),
                             reverse=True)
            if top_n is not None and top_n > 0:
                entries = entries[:top_n]
            return entries

    def set_snapshot_memory_id(self, project_h: str, file_path: str, memory_id: str) -> bool:
        with self._lock:
            entries = self._read_index(project_h)
            for e in entries:
                if e.get("file") == str(file_path):
                    e["sme_memory_id"] = memory_id
                    self._write_index(project_h, entries)
                    return True
            return False
