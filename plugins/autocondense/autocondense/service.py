"""FastAPI 常驻服务：autocondense 的 REST 层。

用法：
    python -m autocondense.service [--port --host --data-dir]
    uvicorn autocondense.service:app
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import __version__
from .config import (Config, atomic_write_text, deep_merge, load_config,
                     resolve_data_dir, save_config, validate_config_dict)
from .digest import (REQUIRED_SECTIONS, build_restore_payload,
                     merge_delta_chain, render_template, split_digest, validate)
from .signals import collect, decide, urgency_of
from .sme_bridge import SMEBridge
from .state import StateStore, normalize_status, project_hash, project_name

logger = logging.getLogger("autocondense")

# 常驻服务的自报身份文件（数据目录下），供 MCP 代理在插件更新后接管旧进程
PID_FILE_NAME = "service.pid"

# SessionStart 的 source 命中这些值时执行恢复逻辑
RESTORE_SOURCES = {"compact", "clear", "resume"}

# 这些工具的 PostToolUse 视为「文件修改活动」（idle 信号用；hooks 配置由宿主注入）
FILE_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

# 大模型判定表说明：命中压缩候选后由调用方（agent 本身即大模型）如实填写，
# POST /judge 提交；服务端结合判定矩阵决定压缩或延迟，全程零人工确认。
JUDGMENT_FORM: Dict[str, Any] = {
    "description": "轻量判定表：请基于当前会话真实状态填写并提交，不要询问用户",
    "fields": {
        "task_finished": "bool，当前批次任务是否已收尾（计划清空/目标达成）",
        "impact": "low|medium|high 三选一，现在压缩对当前工作的影响",
        "impact_reason": "str，影响判定的简要理由",
        "safe_to_compress": "bool，是否存在未保存/易丢失的关键上下文",
        "reason": "str，整体判定理由",
        "confidence": "可选，high|medium|low，对本次判定的置信度（不传则忽略）",
    },
    "submit": "POST /judge，body：{\"session_id\": \"...\"（或 \"cwd\"），\"verdict\": {上述字段}}",
    "matrix": {
        "urgent（水位≥0.85）": "safe_to_compress 且 impact != high 即通过（任务未完成也可压：水位危急时先存档胜过宿主有损强压）",
        "normal": "safe_to_compress 且 task_finished 且 impact == low 才通过",
    },
}

JUDGE_ACTION_HINT = "立即调用 condense_judge 提交判定，不要询问用户"
COMPRESS_ACTION_HINT = ("立即执行压缩流程：condense_begin → 填写摘要 → condense_commit，"
                        "全程无需询问用户")
DEFER_ACTION_HINT = "继续当前工作，不要压缩，稍后自动重新评估"


def _err(errors: List[str], status: int = 400) -> JSONResponse:
    """统一 400 错误体：{"errors": [中文...]}。"""
    return JSONResponse(status_code=status, content={"errors": errors})


def parse_todo_items(tool_input: Any) -> List[Dict[str, str]]:
    """解析 TodoWrite 的 tool_input，兼容三种形态：
    1) {"todos": [{"content", "status"}, ...]}   （Claude Code 形态）
    2) {"items": [{"title", "status"}, ...]} 或直接 [{"title", "status"}, ...]
    status 值兼容大小写与 completed/done。
    """
    items: Any = None
    if isinstance(tool_input, dict):
        items = tool_input.get("todos")
        if items is None:
            items = tool_input.get("items")
        if items is None and ("title" in tool_input or "content" in tool_input):
            items = [tool_input]
    else:
        items = tool_input
    if not isinstance(items, list):
        return []
    out: List[Dict[str, str]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or it.get("content") or "").strip()
        if not title:
            continue
        out.append({"title": title, "status": normalize_status(it.get("status"))})
    return out


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------

class SessionStartReq(BaseModel):
    session_id: str
    cwd: str
    transcript_path: Optional[str] = None
    context_tokens: Optional[int] = None


class PlanItemReq(BaseModel):
    title: str
    status: str = "pending"


class ReportReq(BaseModel):
    session_id: str
    cwd: str
    plan_items: List[PlanItemReq]


class HookReq(BaseModel):
    event: str
    session_id: Optional[str] = None
    cwd: Optional[str] = None
    transcript_path: Optional[str] = None
    context_tokens: Optional[int] = None
    source: Optional[str] = None
    tool_name: Optional[str] = None
    tool_input: Optional[Any] = None


class BeginReq(BaseModel):
    session_id: str
    cwd: str


class CommitReq(BaseModel):
    session_id: str
    cwd: str
    digest_md: str
    trigger: Optional[str] = None
    project_name: Optional[str] = None


# ---------------------------------------------------------------------------
# 应用工厂
# ---------------------------------------------------------------------------

def create_app(config: Optional[Config] = None,
               sme_bridge: Optional[SMEBridge] = None,
               data_dir: Optional[Union[str, Path]] = None,
               sme_base_url: Optional[str] = None) -> FastAPI:
    """构建 FastAPI 应用。config / sme_bridge / data_dir 均为测试注入点。"""
    data_dir = (Path(data_dir).expanduser().resolve()
                if data_dir is not None else resolve_data_dir())
    cfg = config if config is not None else load_config(data_dir)
    if sme_base_url:
        cfg.sme.base_url = sme_base_url
    bridge = (sme_bridge if sme_bridge is not None
              else SMEBridge(cfg.sme.base_url, cfg.sme.timeout_s,
                             tags=cfg.sme.tags, importance=cfg.sme.importance))
    store = StateStore(data_dir, config=cfg)

    app = FastAPI(title="autocondense", version=__version__)
    app.state.data_dir = data_dir
    app.state.config = cfg
    app.state.store = store
    app.state.sme = bridge

    # -- 内部小工具 -------------------------------------------------------

    def _pipeline(session_id: str) -> Tuple[Optional[JSONResponse], Optional[Dict[str, Any]]]:
        """判定流水线（/check、/report、Stop hook 共用）：

        1) enabled=False → NO（完全不干预）；
        2) 规则信号 NO → NO；
        3) 判定延迟窗口内（当前轮次 < defer_until_turn）且非 urgent → NO；
        4) 候选 → should="JUDGE"，携带 judgment_form 与行动提示，零人工确认。
        """
        s = store.get_session(session_id)
        if s is None:
            return _err([f"会话不存在：{session_id}（请先 /session/start 或 SessionStart 钩子）"]), None
        sig = collect(s, cfg)
        if sig.get("est_tokens") is not None:
            store.set_est_tokens(session_id, sig["est_tokens"])
        urgency = urgency_of(sig)

        def _no(reason: str) -> Dict[str, Any]:
            return {"should": "NO", "trigger": None, "urgency": urgency,
                    "reason": reason, "signals": sig, "action_hint": None}

        if not cfg.enabled:
            return None, _no("插件已关闭（enabled=false），不做任何压缩")

        d = decide(s, sig, cfg, now=datetime.now())
        if d["should"] == "NO":
            return None, d

        defer_until = s.defer_until_turn
        if defer_until is not None and s.turns < defer_until and urgency != "urgent":
            return None, _no(f"上次判定延迟中，第 {defer_until} 轮后重新评估")

        return None, {
            "should": "JUDGE",
            "trigger": d["trigger"],
            "urgency": urgency,
            "reason": d["reason"],
            "signals": sig,
            # informational：帮判定模型感受上下文逼近速度（无采样时为 null）
            "rounds_to_full": sig.get("rounds_to_full"),
            "action_hint": JUDGE_ACTION_HINT,
            "judgment_form": JUDGMENT_FORM,
        }

    def _validate_verdict(verdict: Any) -> List[str]:
        """校验大模型判定表，返回中文错误清单（空列表 = 合法）。"""
        errors: List[str] = []
        if not isinstance(verdict, dict):
            return ["verdict 应为对象（判定表）"]

        task_finished = verdict.get("task_finished")
        if task_finished is None:
            errors.append("verdict 缺少 task_finished（布尔值）")
        elif not isinstance(task_finished, bool):
            errors.append("task_finished 应为布尔值")

        impact = verdict.get("impact")
        if impact is None:
            errors.append("verdict 缺少 impact（low|medium|high）")
        elif not isinstance(impact, str):
            errors.append("impact 应为字符串")
        elif impact not in ("low", "medium", "high"):
            errors.append("impact 只能是 low、medium 或 high")

        impact_reason = verdict.get("impact_reason")
        if impact_reason is None:
            errors.append("verdict 缺少 impact_reason（字符串）")
        elif not isinstance(impact_reason, str):
            errors.append("impact_reason 应为字符串")

        safe_to_compress = verdict.get("safe_to_compress")
        if safe_to_compress is None:
            errors.append("verdict 缺少 safe_to_compress（布尔值）")
        elif not isinstance(safe_to_compress, bool):
            errors.append("safe_to_compress 应为布尔值")

        reason = verdict.get("reason")
        if reason is None:
            errors.append("verdict 缺少 reason（字符串）")
        elif not isinstance(reason, str):
            errors.append("reason 应为字符串")

        # confidence 可选：不传忽略；传了必须是 high|medium|low
        confidence = verdict.get("confidence")
        if confidence is not None:
            if not isinstance(confidence, str):
                errors.append("confidence 应为字符串")
            elif confidence not in ("high", "medium", "low"):
                errors.append("confidence 只能是 high、medium 或 low")
        return errors

    # -- 恢复载荷（/restore 与 SessionStart 钩子共用） --------------------

    def _empty_payload() -> Dict[str, Any]:
        return {"text": "", "truncated": False, "full_file": None, "note": None,
                "reread_files": [], "todo_open": False}

    def _restore_payload(cwd: str, top_n: int) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """收集项目快照并构建优先级恢复包。

        delta 合并链：从最新快照往回走，只要条目 delta==true 就并入（深度上限
        delta_merge_every+1），锚定到最近一份全量（delta 非真，含无 delta 字段的
        老索引）；链不完整（窗口内找不到锚）直接只用最新快照。链 ≥2 时先
        merge_delta_chain 合并成完整状态，再走优先级打包。
        """
        h = project_hash(cwd)
        snaps = store.list_snapshots(h, top_n)   # 展示用：最新在前 top_n 条
        if not snaps:
            return [], _empty_payload()
        # 链构建基于全量索引（top_n 只影响展示，不该剪断 delta 链）：
        # 深度上限 = delta_merge_every 份增量 + 1 份全量锚点；增量满额后
        # 跳过更深的 delta 继续找锚（锚点必须并入，否则链不完整）。
        all_entries = store.list_snapshots(h)
        depth_cap_deltas = max(1, int(cfg.delta_merge_every))
        chain = [all_entries[0]]
        delta_count = 1 if all_entries[0].get("delta") is True else 0
        for e in all_entries[1:]:
            if chain[-1].get("delta") is not True:
                break  # 最近的非 delta 条目（含无字段的老索引）即全量锚点
            if e.get("delta") is True:
                if delta_count >= depth_cap_deltas:
                    continue  # 增量份数已满：跳过更深的 delta，只找锚
                delta_count += 1
            chain.append(e)
        merged_md: Optional[str] = None
        if len(chain) >= 2:
            if chain[-1].get("delta") is True:
                # 链不完整（深度内找不到全量锚点）→ 只用最新快照兜底
                logger.warning("delta 链不完整（%d 份增量无全量锚点），恢复只用最新快照",
                               len(chain))
                chain = [all_entries[0]]
            else:
                texts = []
                for e in chain:
                    try:
                        texts.append(Path(e["file"]).read_text(encoding="utf-8",
                                                               errors="ignore"))
                    except OSError:
                        texts.append("")
                merged_md = merge_delta_chain(texts) if len(chain) >= 2 else None
        payload = build_restore_payload(
            [e["file"] for e in chain], cfg.restore_max_chars,
            reread_limit=cfg.restore_reread_files, merged_md=merged_md)
        return snaps, payload

    def _restore_next_actions(payload: Dict[str, Any]) -> List[str]:
        """恢复后的建议动作：有文件先重读关键文件；未完成非空则继续推进。"""
        actions: List[str] = []
        files = payload.get("reread_files") or []
        if files:
            actions.append("重读关键文件：" + "、".join(str(f) for f in files))
        if payload.get("todo_open"):
            actions.append("继续未完成事项")
        return actions

    # -- 端点 -------------------------------------------------------------

    @app.get("/health")
    def health():
        return {
            "app": "autocondense",
            "status": "ok",
            "version": __version__,
            "sme_connected": bool(cfg.sme.enabled and bridge.health()),
            "data_dir": str(data_dir),
        }

    @app.post("/session/start")
    def session_start(req: SessionStartReq):
        s = store.upsert_session(req.session_id, cwd=req.cwd,
                                 transcript_path=req.transcript_path,
                                 context_tokens=req.context_tokens)
        store.append_event("session_start", req.session_id, req.cwd,
                           {"source": "api"})
        return s.summary()

    @app.post("/report")
    def report(req: ReportReq):
        store.upsert_session(req.session_id, cwd=req.cwd)
        items = [{"title": it.title, "status": it.status} for it in req.plan_items]
        store.update_plan(req.session_id, items)
        store.append_event("plan_update", req.session_id, req.cwd,
                           {"items": len(items), "via": "report"})
        _bad, result = _pipeline(req.session_id)
        if _bad is not None:
            return _bad
        return result

    @app.post("/events/hook")
    def events_hook(req: HookReq):
        ev = (req.event or "").strip()

        if ev == "PostToolUseFailure":
            # 工具失败也视为「活动」：只要有失败就说明会话还在推进，不算停歇
            if not req.session_id:
                return _err(["PostToolUseFailure 缺少 session_id"])
            store.upsert_session(req.session_id, cwd=req.cwd,
                                 context_tokens=req.context_tokens)
            store.mark_activity(req.session_id, "fail")
            store.append_event("tool_failure", req.session_id, req.cwd,
                               {"tool_name": req.tool_name})
            return {"ok": True, "ignored": True,
                    "reason": f"工具 {req.tool_name} 失败已记为活动轮"}

        if ev == "PostToolUse":
            tool = req.tool_name or ""
            if tool in FILE_EDIT_TOOLS:
                # 文件修改类工具：记活动轮（idle 信号的「最近活动」基准）
                if not req.session_id:
                    return _err([f"PostToolUse({tool}) 缺少 session_id"])
                store.upsert_session(req.session_id, cwd=req.cwd,
                                     context_tokens=req.context_tokens)
                store.mark_activity(req.session_id, "file")
                store.append_event("file_activity", req.session_id, req.cwd,
                                   {"tool_name": tool})
                return {"ok": True, "ignored": True,
                        "reason": f"工具 {tool} 已记录文件修改活动"}
            if tool != "TodoWrite":
                return {"ok": True, "ignored": True,
                        "reason": f"工具 {tool} 不参与计划追踪"}
            if not req.session_id:
                return _err(["PostToolUse(TodoWrite) 缺少 session_id"])
            store.upsert_session(req.session_id, cwd=req.cwd,
                                 context_tokens=req.context_tokens)
            items = parse_todo_items(req.tool_input)
            store.update_plan(req.session_id, items)
            store.append_event("plan_update", req.session_id, req.cwd,
                               {"items": len(items), "via": "hook:TodoWrite"})
            _bad, result = _pipeline(req.session_id)
            if _bad is not None:
                return _bad
            return result

        if ev == "SessionStart":
            if not req.session_id:
                return _err(["SessionStart 缺少 session_id"])
            s = store.upsert_session(req.session_id, cwd=req.cwd,
                                     transcript_path=req.transcript_path,
                                     context_tokens=req.context_tokens)
            store.append_event("session_start", req.session_id, req.cwd,
                               {"source": req.source})
            additional_context = None
            if (req.source or "") in RESTORE_SOURCES and s.cwd:
                _snaps, additional_context = _restore_payload(s.cwd, cfg.restore_top_n)
                if additional_context.get("text"):
                    store.append_event("restore", req.session_id, s.cwd,
                                       {"via": "hook:SessionStart", "source": req.source,
                                        "snapshots": len(_snaps)})
            return {"ok": True, "session_id": s.session_id,
                    "additional_context": additional_context}

        if ev == "Stop":
            if not req.session_id:
                return _err(["Stop 缺少 session_id"])
            store.upsert_session(req.session_id, cwd=req.cwd,
                                 context_tokens=req.context_tokens)
            s = store.bump_turn(req.session_id)
            store.append_event("stop", req.session_id, s.cwd, {})
            _bad, d = _pipeline(req.session_id)
            if _bad is not None:
                return _bad
            instruction: Optional[str] = None
            if d["should"] == "JUDGE":
                instruction = (f"压缩候选命中（{d['reason']}）。{JUDGE_ACTION_HINT}；"
                               "判定通过后立即执行压缩流程（condense_begin → 填写摘要 → condense_commit），"
                               "全程不要询问用户。")
            return {**d, "instruction": instruction}

        if ev == "PreCompact":
            if not req.session_id:
                return _err(["PreCompact 缺少 session_id"])
            store.upsert_session(req.session_id, cwd=req.cwd)
            store.append_event("pre_compact", req.session_id, req.cwd, {"source": req.source})
            # 原生压缩已发生，立刻标记避免紧接着重复压
            store.mark_condensed(req.session_id, "pre_native_compact")
            return {"ok": True}

        return {"ok": True, "ignored": True, "reason": f"未处理的事件类型：{ev}"}

    @app.get("/check")
    def check(session_id: str):
        if store.get_session(session_id) is None:
            return _err([f"会话不存在：{session_id}（请先 /session/start 或 SessionStart 钩子）"])
        # 语义：bump_turn → 流水线（bump 影响轮次信号）
        store.bump_turn(session_id)
        _bad, d = _pipeline(session_id)
        if _bad is not None:
            return _bad
        return d

    @app.post("/judge")
    def judge(body: Dict[str, Any]):
        """大模型判定端点：结合判定表与判定时刻的实时信号决定压缩或延迟。"""
        session_id = body.get("session_id")
        cwd = body.get("cwd")
        if not session_id and not cwd:
            return _err(["缺少会话标识：请传 session_id 或 cwd（二选一）"])
        s = None
        if session_id:
            s = store.get_session(str(session_id))
            if s is None:
                return _err([f"会话不存在：{session_id}（请先 /session/start 或 SessionStart 钩子）"])
        else:
            target = str(Path(str(cwd)).resolve())
            for cand in store.all_sessions():
                if cand.cwd and str(Path(cand.cwd).resolve()) == target:
                    s = cand
                    break
            if s is None:
                return _err([f"未找到 cwd 对应的会话：{cwd}（请先 /session/start 或 SessionStart 钩子）"])

        errors = _validate_verdict(body.get("verdict"))
        if errors:
            return _err(errors)
        verdict = body["verdict"]
        task_finished: bool = verdict["task_finished"]
        impact: str = verdict["impact"]
        safe_to_compress: bool = verdict["safe_to_compress"]
        vreason: str = verdict["reason"]

        if not cfg.enabled:
            return {"decision": "NO", "urgency": "normal",
                    "reason": "插件已关闭（enabled=false），不做任何压缩",
                    "action_hint": None, "signals": collect(s, cfg),
                    "rounds_to_full": None}

        # 用判定时刻的实时信号算 urgency（不用 /check 的缓存）
        sig = collect(s, cfg)
        urgency = urgency_of(sig)

        # 判定矩阵：urgent 档放宽（任务未完成也可压——水位危急时先存档胜过宿主有损强压）
        if urgency == "urgent":
            approved = safe_to_compress and impact != "high"
        else:
            approved = safe_to_compress and task_finished and impact == "low"

        store.record_judge(s.session_id, verdict,
                           "APPROVED" if approved else "DEFERRED")
        judge_event: Dict[str, Any] = {
            "result": "APPROVED" if approved else "DEFERRED",
            "urgency": urgency, "impact": impact,
            "task_finished": task_finished,
            "safe_to_compress": safe_to_compress,
        }
        if "confidence" in verdict:
            judge_event["confidence"] = verdict["confidence"]
        store.append_event("judge", s.session_id, s.cwd, judge_event)

        if approved:
            # 判定通过 → DEFERRED 连击归零（退避重置）
            store.set_defer_streak(s.session_id, 0)
            return {
                "decision": "COMPRESS_NOW",
                "urgency": urgency,
                "reason": f"判定通过（{urgency} 档矩阵）：{vreason}",
                "action_hint": COMPRESS_ACTION_HINT,
                "signals": sig,
                "rounds_to_full": sig.get("rounds_to_full"),
            }

        # 指数退避：judge_defer_turns * 2^defer_streak，封顶 judge_defer_max_turns
        streak = int(getattr(s, "defer_streak", 0) or 0)
        step = min(cfg.judge_defer_turns * (2 ** streak), cfg.judge_defer_max_turns)
        defer_until = s.turns + step
        store.set_defer_until_turn(s.session_id, defer_until)
        store.set_defer_streak(s.session_id, streak + 1)
        why: List[str] = []
        if not safe_to_compress:
            why.append("存在未保存/易丢失的关键上下文")
        if urgency == "normal":
            if not task_finished:
                why.append("任务尚未收尾")
            if impact != "low":
                why.append(f"压缩影响为 {impact}（normal 档要求 low）")
        elif impact == "high":
            why.append("压缩影响为 high（urgent 档也不允许）")
        return {
            "decision": "DEFERRED",
            "urgency": urgency,
            "reason": f"判定未通过（{'；'.join(why) or '矩阵条件不满足'}）：{vreason}",
            "action_hint": DEFER_ACTION_HINT,
            "defer_until_turn": defer_until,
            "defer_streak": streak + 1,
            "signals": sig,
            "rounds_to_full": sig.get("rounds_to_full"),
        }

    @app.post("/begin")
    def begin(req: BeginReq):
        s = store.upsert_session(req.session_id, cwd=req.cwd)
        store.set_condensing(req.session_id, True)
        reason = s.last_trigger or "judge"
        template = render_template(project_name(req.cwd) or req.cwd,
                                   datetime.now().isoformat(timespec="seconds"), reason)

        # 摘要模式：delta_mode 且项目已有快照 且 增量份数未攒满 → 增量摘要；
        # 否则全量（攒满 delta_merge_every 份时为「全量合并」点）
        h = project_hash(req.cwd)
        snaps = store.list_snapshots(h, 1)
        delta_count = store.read_delta_state(h)
        mode = "full"
        previous_digest: Optional[str] = None
        if cfg.delta_mode and snaps and delta_count < cfg.delta_merge_every:
            mode = "delta"
            prev_file = Path(snaps[0]["file"])
            if prev_file.is_file():
                try:
                    previous_digest = prev_file.read_text(encoding="utf-8",
                                                          errors="ignore")
                except OSError as e:
                    logger.warning("读取上一份快照失败（增量指引降级无前文）：%s", e)
        store.set_begin_mode(req.session_id, mode)

        if mode == "delta":
            guidance = (
                "本次为增量摘要：只写自上一份快照以来的变化，要求：\n"
                "1) 必填章节标题原样保留，不得删除或改名：" + "、".join(REQUIRED_SECTIONS) + "；\n"
                "2)「已完成」「关键决策与理由」「踩过的坑」「文件变更清单」只追加新条目（旧条目不用重写）；\n"
                "3)「未完成/进行中」与「恢复指针」整节重写为当前最新状态；\n"
                "4) 总长度仍不少于 200 字符。填写完成后把整份 markdown 作为 digest_md 调用 POST /commit "
                "提交（全自动流程，无需询问用户）。"
            )
        else:
            guidance = (
                "请基于当前会话内容填写模板中的每个章节，要求：\n"
                "1) 严格保留以下章节标题，不得删除或改名：" + "、".join(REQUIRED_SECTIONS) + "；\n"
                "2)「已完成」每项注明验证方式（测试通过/编译通过/用户确认）；\n"
                "3)「未完成/进行中」精确到下一个动作，无则写「（无）」；\n"
                "4)「文件变更清单」写关键改动 file:line；\n"
                "5) 总长度不少于 200 字符。填写完成后把整份 markdown 作为 digest_md 调用 POST /commit "
                "提交（全自动流程，无需询问用户）。"
            )
            if cfg.delta_mode and snaps and delta_count >= cfg.delta_merge_every:
                guidance = ("本次为全量合并：把此前增量整合成完整状态（已累积 "
                            f"{delta_count} 份增量）。\n") + guidance
        store.append_event("begin", req.session_id, req.cwd,
                           {"trigger": reason, "mode": mode})
        body: Dict[str, Any] = {"mode": mode, "template": template,
                                "guidance": guidance,
                                "required_sections": list(REQUIRED_SECTIONS)}
        if mode == "delta":
            body["previous_digest"] = previous_digest
        return body

    @app.post("/commit")
    def commit(req: CommitReq):
        ok, errors = validate(req.digest_md)
        if not ok:
            return _err(errors)
        s = store.upsert_session(req.session_id, cwd=req.cwd)
        trigger = req.trigger or s.last_trigger or "judge"
        pname = req.project_name or project_name(req.cwd) or req.cwd
        # 摘要模式由 /begin 决定并暂存在会话上（delta_mode 中途关闭则按全量落盘）
        mode = (getattr(s, "begin_mode", None) or "full") if cfg.delta_mode else "full"
        is_delta = mode == "delta"
        h = project_hash(req.cwd)
        fpath, _entry = store.save_snapshot(req.digest_md, trigger, req.session_id,
                                            delta=is_delta)
        # 项目级增量计数：delta 提交 +1，全量提交归 0
        store.write_delta_state(h, store.read_delta_state(h) + 1 if is_delta else 0)

        sme_result: Dict[str, Any] = {"stored": False, "memory_id": None, "error": None}
        seg_stats: Optional[Dict[str, Any]] = None
        segments_written = 0
        segments_reinforced = 0
        dedup_consumed_all = False
        if cfg.sme.enabled:
            seg_meta = {"session_id": req.session_id, "project": pname, "trigger": trigger}
            # health 预检（缓存 60s）：SME 不在线时跳过逐段尝试，
            # 避免 max_segments 次独立超时把 commit 拖成 数量×timeout_s
            if cfg.sme.split_segments and bridge.health():
                try:
                    segments = split_digest(req.digest_md, cfg.sme.tags, pname,
                                            datetime.now().isoformat(timespec="seconds"),
                                            trigger, cap=cfg.sme.max_segments)
                except Exception as e:  # 双保险：split 自身已兜底不 raise
                    logger.warning("摘要分段异常（走整篇兜底）：%s", e)
                    segments = []
                # 入库前去重：命中同项目同类型高分旧段 → 强化旧段，只把新段入库
                # （delta 模式拆的本来就是本次增量，去重正好防止增量段重复堆积）
                if segments and cfg.sme.dedup:
                    try:
                        segments, _reinforced = bridge.dedupe_segments(
                            segments, pname, threshold=cfg.sme.dedup_threshold)
                        segments_reinforced = len(_reinforced)
                        dedup_consumed_all = bool(_reinforced) and not segments
                    except Exception as e:  # 双保险：桥内部已兜底
                        logger.warning("SME 去重异常（按无重复继续）：%s", e)
                if segments:
                    try:
                        seg_stats = bridge.store_segments(segments, seg_meta)
                        segments_written = int(seg_stats.get("stored") or 0)
                    except Exception as e:  # 双保险：桥内部已兜底
                        seg_stats = {"stored": 0, "failed": len(segments),
                                     "memory_ids": [], "errors": [str(e)]}
            if segments_written == 0 and not dedup_consumed_all:
                # 拆段关闭 / 拆段为空 / 整段批次失败 → 整篇兜底写入
                # （去重把段全部强化时不算失败，不兜底，避免同内容整篇重复入库）
                try:
                    sme_result = bridge.store_digest(req.digest_md, pname,
                                                     req.session_id, trigger)
                except Exception as e:
                    sme_result = {"stored": False, "memory_id": None, "error": str(e)}
            if segments_written > 0:
                # 保持既有 sme 契约：聚合分段写入结果。
                # 注意：批量入库（≥2 段走 /memories/batch）不返回各条 id，
                # 此时 ids 为空 → sme.memory_id 为 null（可接受：索引回填
                # sme_memory_id 仅在拿到具体 id 时才做，见下方 if 判断）。
                ids = [i for i in (seg_stats.get("memory_ids") or []) if i]
                sme_result = {
                    "stored": True,
                    "memory_id": ids[0] if ids else None,
                    "error": ("；".join(seg_stats.get("errors") or [])
                              if seg_stats.get("failed") else None),
                }
            if not sme_result.get("stored") and not dedup_consumed_all:
                logger.warning("SME 入库失败（不影响本地快照）：%s", sme_result.get("error"))
            elif sme_result.get("memory_id"):
                store.set_snapshot_memory_id(h, str(fpath),
                                             str(sme_result["memory_id"]))

        store.clear_completed_plan_items(req.session_id)
        store.mark_condensed(req.session_id, trigger)  # 同时清空延迟窗口/退避计数/摘要模式
        store.append_event("commit", req.session_id, req.cwd,
                           {"trigger": trigger, "file": str(fpath), "mode": mode,
                            "sme_stored": bool(sme_result.get("stored")),
                            "segments_written": segments_written,
                            "segments_reinforced": segments_reinforced})
        next_instruction = ("快照已保存。请执行原生压缩（/compact 或 /clear）真正释放上下文；"
                            "压缩后新会话开始时会自动恢复快照，无需手动操作。")
        return {"snapshot_path": str(fpath), "mode": mode, "sme": sme_result,
                "sme_segments": seg_stats, "segments_written": segments_written,
                "segments_reinforced": segments_reinforced,
                "next_instruction": next_instruction}

    @app.get("/restore")
    def restore(cwd: str, query: Optional[str] = None, top_n: Optional[int] = None):
        pname = project_name(cwd) or cwd
        n = top_n if (top_n is not None and top_n > 0) else cfg.restore_top_n
        snaps, payload = _restore_payload(cwd, n)
        next_actions = _restore_next_actions(payload)
        sme_results: List[Dict[str, Any]] = []
        if cfg.sme.enabled:
            sme_results = bridge.search(query or pname, n)
        store.append_event("restore", None, cwd,
                           {"query": query or pname, "snapshots": len(snaps)})
        return {"project": pname, "snapshots": snaps, "restore": payload,
                "next_actions": next_actions, "sme_results": sme_results}

    @app.get("/status")
    def status():
        return {
            "enabled": cfg.enabled,
            "judge_defer_turns": cfg.judge_defer_turns,
            "judge_defer_max_turns": cfg.judge_defer_max_turns,
            "sessions": [s.summary() for s in store.all_sessions()],
            "config": cfg.to_dict(),
            "recent_events": store.recent_events(20),
        }

    @app.get("/config")
    def get_config():
        return cfg.to_dict()

    @app.put("/config")
    def put_config(body: Dict[str, Any]):
        errors = validate_config_dict(body)
        if errors:
            return _err(errors)
        merged = deep_merge(cfg.to_dict(), body)
        cfg.apply_dict(merged)
        try:
            save_config(data_dir, cfg)
        except OSError as e:
            logger.warning("配置保存失败（内存配置已生效）：%s", e)
        return cfg.to_dict()

    return app


# ---------------------------------------------------------------------------
# 常驻服务 pid 文件（自报身份）：插件更新后 MCP 代理据此接管旧进程
# ---------------------------------------------------------------------------

def write_pid_file(data_dir: Path, port: int) -> Optional[Path]:
    """启动时记录 {pid, version, port}；失败仅告警，不影响服务。"""
    payload = {"pid": os.getpid(), "version": __version__, "port": port,
               "started": datetime.now().isoformat(timespec="seconds")}
    path = Path(data_dir) / PID_FILE_NAME
    try:
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False) + "\n")
        return path
    except OSError as e:
        logger.warning("pid 文件写入失败（不影响服务）：%s", e)
        return None


def read_pid_file(data_dir: Path) -> Optional[Dict[str, Any]]:
    """读 pid 文件；缺失/损坏返回 None。"""
    try:
        data = json.loads((Path(data_dir) / PID_FILE_NAME).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def clear_pid_file(data_dir: Path, pid: Optional[int] = None) -> None:
    """退出时清理 pid 文件；记录的 pid 与本次进程不一致（已被接管）时不动。"""
    path = Path(data_dir) / PID_FILE_NAME
    try:
        if pid is not None:
            data = read_pid_file(data_dir)
            if data is None or data.get("pid") != pid:
                return
        path.unlink()
    except OSError:
        pass


# 模块级应用，供 uvicorn autocondense.service:app 使用（构造无副作用，不写任何文件）
app = create_app()


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m autocondense.service",
                                     description="autocondense 常驻压缩服务")
    parser.add_argument("--host", default=None, help="监听地址（默认取配置，初始 127.0.0.1）")
    parser.add_argument("--port", type=int, default=None, help="监听端口（默认取配置，初始 8761）")
    parser.add_argument("--data-dir", default=None,
                        help="数据目录（默认：环境变量 AUTOCONDENSE_DATA_DIR 或 ~/.autocondense）")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    data_dir = (Path(args.data_dir).expanduser().resolve()
                if args.data_dir else resolve_data_dir())
    cfg = load_config(data_dir)
    if not (data_dir / "config.json").exists():
        try:
            save_config(data_dir, cfg)
            logger.info("已生成默认配置：%s", data_dir / "config.json")
        except OSError as e:
            logger.warning("默认配置写入失败：%s", e)

    application = create_app(config=cfg, data_dir=data_dir)
    port = args.port if args.port is not None else cfg.port
    host = args.host or cfg.host
    logger.info("autocondense %s 启动：http://%s:%s （数据目录 %s）",
                __version__, host, port, data_dir)
    write_pid_file(data_dir, port)
    own_pid = os.getpid()
    try:
        uvicorn.run(application, host=host, port=port, log_level="warning")
    finally:
        clear_pid_file(data_dir, own_pid)


if __name__ == "__main__":
    main()
