"""压缩信号采集（collect）与候选判定（decide）。

候选制语义：规则只负责给出「压缩候选」（CANDIDATE，附 trigger 与 urgency），
是否真正压缩由 POST /judge 的大模型判定矩阵决定；本模块不再区分
manual/auto，也不产生任何「询问用户」类建议。

判定顺序（务必保持，勿调整）：
  1) plan_in_progress > 0        → NO            有进行中任务，绝不压
  2) cooldown 未过               → NO            冷却期
  3) 计划全部完成                → CANDIDATE     trigger=plan_done
  4) 某完成条目跨度 >= task_min_rounds 且无 in_progress
                                  → CANDIDATE     trigger=task_done
  5) water_level >= water_threshold
                                  → CANDIDATE     trigger=water_level
  6) idle_rounds >= idle_signal_rounds（且活动记录存在）
                                  → CANDIDATE     trigger=idle
  7) turns_since_condense >= turns_threshold
                                  → CANDIDATE     trigger=turns
  8) 否则                        → NO

token 口径（水位真实化）：智能体上报的 context_tokens（最近 10 轮内更新过）
优先；否则用 transcript 启发式估算。signals.token_source 记录来源，
rounds_to_full 由上报采样估算每轮平均增量得出（informational，帮判定模型
感受紧迫度）。

urgency：水位 ≥ URGENT_WATER(0.85) → "urgent"（危急，判定矩阵放宽、
可无视延迟窗口）；其余（含候选与非候选）→ "normal"。
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import Config
from .state import SessionState, _parse_iso

logger = logging.getLogger("autocondense")

# 水位达到该值即视为危急（urgent），判定矩阵放宽且可无视延迟窗口
URGENT_WATER = 0.85

# 上报 token 的保鲜期：context_tokens 超过这么多轮未更新就视为过期，回落估算
REPORTED_TOKEN_FRESH_TURNS = 10

# CJK 字符区间（估算 token 用，不必求全，量级对即可）
_CJK_RANGES = (
    (0x3040, 0x30FF),    # 平假名/片假名
    (0x3400, 0x4DBF),    # CJK 扩展 A
    (0x4E00, 0x9FFF),    # CJK 基本区
    (0xAC00, 0xD7A3),    # 韩文音节
    (0xF900, 0xFAFF),    # CJK 兼容表意
    (0xFF00, 0xFFEF),    # 全角符号
    (0x20000, 0x2A6DF),  # CJK 扩展 B
)


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def estimate_transcript_tokens(path: Optional[str]) -> Optional[int]:
    """按行估算 transcript（JSONL）的 token 数：CJK 字符 ×1 + 其他字符 ×0.25。

    为什么不做精确 tokenize：
    1) 不同模型 tokenizer 不同，transcript 里也拿不到权威的 token 计数，
       水位判定只需要量级（0.7 这种粗阈值），精确值没有意义；
    2) 引入 tokenizer 依赖（tiktoken 等）体积大、离线环境不可靠；
    3) 字符加权只需一次顺序读，O(行数)，成本可忽略。
    """
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    total = 0.0
    try:
        with p.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                cjk = 0
                for ch in line:
                    if _is_cjk(ch):
                        cjk += 1
                total += cjk + (len(line) - cjk) * 0.25
    except OSError as e:
        logger.warning("读取 transcript 失败，跳过 token 估算：%s", e)
        return None
    return int(total)


def urgency_of(signals: Dict[str, Any]) -> str:
    """按水位计算紧急度：≥ URGENT_WATER → "urgent"，其余（含无水位）→ "normal"。"""
    water = (signals or {}).get("water_level")
    if isinstance(water, (int, float)) and not isinstance(water, bool) and water >= URGENT_WATER:
        return "urgent"
    return "normal"


def _int_or_none(v: Any) -> Optional[int]:
    if isinstance(v, int) and not isinstance(v, bool):
        return v
    return None


def _rounds_to_full(samples: List[Dict[str, Any]], current: Optional[int],
                    usable: int) -> Optional[int]:
    """由 token 采样估算「还有多少轮到 95% 水位」。

    每轮平均增量 = (最新采样 - 最早采样) / 轮差；avg>0 时
    rounds = max(0, ceil(usable*0.95 - 当前 token) / avg)，否则 None。
    采样不足两轮（或轮差为 0/增量为负）时返回 None，绝不瞎猜。
    """
    if current is None or len(samples) < 2:
        return None
    first, last = samples[0], samples[-1]
    t0, t1 = _int_or_none(first.get("turn")), _int_or_none(last.get("turn"))
    v0, v1 = _int_or_none(first.get("tokens")), _int_or_none(last.get("tokens"))
    if t0 is None or t1 is None or v0 is None or v1 is None or t1 <= t0:
        return None
    avg = (v1 - v0) / (t1 - t0)
    if avg <= 0:
        return None
    remaining = usable * 0.95 - current
    if remaining <= 0:
        return 0
    return max(0, math.ceil(remaining / avg))


def collect(state: SessionState, config: Config) -> Dict[str, Any]:
    """从会话状态采集判定所需信号。

    token 来源优先级：新鲜的智能体上报值（context_tokens 且最近 10 轮内更新）
    > transcript 启发式估算 > 过期/无新鲜度记录的上报值（聊胜于无）。
    """
    est_transcript = estimate_transcript_tokens(state.transcript_path)
    reported = state.context_tokens
    reported_fresh = (
        reported is not None
        and _int_or_none(getattr(state, "context_tokens_turn", None)) is not None
        and (state.turns - state.context_tokens_turn) <= REPORTED_TOKEN_FRESH_TURNS
    )
    if reported_fresh:
        tokens, token_source = reported, "reported"
    elif est_transcript is not None:
        tokens, token_source = est_transcript, "estimated"
    elif reported is not None:
        # 老会话只有上报值、没有新鲜度记录：仍以上报值为准（无 transcript 可估时）
        tokens, token_source = reported, "reported"
    else:
        tokens, token_source = None, None

    minutes_since_condense: Optional[float] = None
    last = _parse_iso(state.last_condense_at)
    if last is not None:
        minutes_since_condense = round((datetime.now() - last).total_seconds() / 60, 1)

    cooldown_until = _parse_iso(state.cooldown_until)
    now = datetime.now()

    water_level: Optional[float] = None
    if tokens:
        water_level = round(tokens / config.usable_context_tokens, 4)

    rounds_to_full = _rounds_to_full(list(getattr(state, "token_samples", None) or []),
                                     tokens, config.usable_context_tokens)

    # idle：距最近一次「文件修改 / 工具失败」活动轮的轮数；无活动记录则 None（不误报）
    activity_turns = [t for t in (getattr(state, "last_file_turn", None),
                                  getattr(state, "last_fail_turn", None))
                      if _int_or_none(t) is not None]
    idle_rounds = (state.turns - max(activity_turns)) if activity_turns else None

    return {
        "plan_total": len(state.plan_items),
        "plan_completed": sum(1 for it in state.plan_items if it.get("status") == "completed"),
        "plan_in_progress": sum(1 for it in state.plan_items if it.get("status") == "in_progress"),
        "turns_since_condense": state.turns_since_condense,
        "minutes_since_condense": minutes_since_condense,
        "est_tokens": tokens,
        "token_source": token_source,
        "rounds_to_full": rounds_to_full,
        "idle_rounds": idle_rounds,
        "water_level": water_level,
        "cooldown_active": bool(cooldown_until is not None and cooldown_until > now),
        "urgency": urgency_of({"water_level": water_level}),
    }


def _completed_spans(state: SessionState) -> List[int]:
    """当前计划 + 归档中所有 completed 条目的 (completed_turn - first_seen_turn)。"""
    spans: List[int] = []
    sources = list(getattr(state, "plan_items", None) or []) + list(getattr(state, "plan_archive", None) or [])
    for it in sources:
        if not isinstance(it, dict) or it.get("status") not in (None, "completed"):
            continue
        ct, ft = it.get("completed_turn"), it.get("first_seen_turn")
        if isinstance(ct, int) and isinstance(ft, int):
            spans.append(max(0, ct - ft))
    return spans


def decide(state: SessionState, signals: Dict[str, Any], config: Config,
           now: Optional[datetime] = None) -> Dict[str, Any]:
    """按固定顺序判定是否产生压缩候选。

    返回 {should, trigger, urgency, reason, signals, action_hint}：
    - should ∈ {"CANDIDATE", "NO"}；CANDIDATE 附 trigger（plan_done/task_done/
      water_level/idle/turns）与 urgency（urgent/normal）。
    - 是否真正压缩由 service 层的 /judge 大模型判定矩阵决定。
    """
    now = now or datetime.now()
    sig = dict(signals or {})
    in_prog = int(sig.get("plan_in_progress") or 0)
    total = int(sig.get("plan_total") or 0)
    done = int(sig.get("plan_completed") or 0)
    urgency = urgency_of(sig)

    def _result(should: str, trigger: Optional[str], reason: str) -> Dict[str, Any]:
        hint: Optional[str] = None
        if should == "CANDIDATE":
            hint = "命中压缩候选，立即调用 condense_judge 提交大模型判定，不要询问用户"
        return {"should": should, "trigger": trigger, "urgency": urgency,
                "reason": reason, "signals": sig, "action_hint": hint}

    # 1) 有进行中任务：绝不压，保证任务连续性
    if in_prog > 0:
        return _result("NO", None, f"有 {in_prog} 项进行中的计划任务，为保证任务连续性暂不压缩")

    # 2) 冷却期未过：刚压过，别再压
    if sig.get("cooldown_active"):
        cu = _parse_iso(getattr(state, "cooldown_until", None))
        if cu is not None:
            remain = max(0.0, (cu - now).total_seconds() / 60)
            return _result("NO", None, f"压缩冷却期内（剩余约 {remain:.0f} 分钟），暂不压缩")
        return _result("NO", None, "压缩冷却期内，暂不压缩")

    # 3) 计划全部完成：任务脉络闭环，进入候选
    if total > 0 and done == total:
        return _result("CANDIDATE", "plan_done",
                       f"全部 {total} 项计划任务已完成，任务脉络已闭环，适合压缩")

    # 4) 长任务完成：跨度达标的 completed 条目（含归档），过程细节可安全归档
    spans = _completed_spans(state)
    if any(sp >= config.task_min_rounds for sp in spans):
        return _result("CANDIDATE", "task_done",
                       f"存在跨度 ≥{config.task_min_rounds} 轮的已完成任务，过程细节可安全归档")

    # 5) 水位：接近可用上下文上限
    water = sig.get("water_level")
    if water is not None and water >= config.water_threshold:
        return _result("CANDIDATE", "water_level",
                       f"上下文水位 {water:.0%} 已达阈值 {config.water_threshold:.0%}，接近可用上下文上限")

    # 6) idle：连续数轮无文件修改与失败（活动记录缺失时 idle_rounds=None，不触发）
    idle = sig.get("idle_rounds")
    if idle is not None and idle >= config.idle_signal_rounds:
        return _result("CANDIDATE", "idle",
                       f"连续 {idle} 轮无文件修改与失败，疑似任务停歇，适合压缩")

    # 7) 轮次：候选之一
    tsc = int(sig.get("turns_since_condense") or 0)
    if tsc >= config.turns_threshold:
        return _result("CANDIDATE", "turns",
                       f"距上次压缩已累计 {tsc} 轮对话（阈值 {config.turns_threshold}），建议压缩")

    # 8) 无信号
    return _result("NO", None, "暂未触发任何压缩信号")
