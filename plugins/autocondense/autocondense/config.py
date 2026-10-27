"""配置模块：数据目录解析、默认配置、加载/保存与校验。

优先级：内置默认值 < 环境变量（仅数据目录与端口） < data/config.json。
坏文件改名 .corrupt-时间戳 保留现场后回落默认值，绝不阻断主流程。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("autocondense")

DATA_DIR_ENV = "AUTOCONDENSE_DATA_DIR"
PORT_ENV = "AUTOCONDENSE_PORT"


# ---------------------------------------------------------------------------
# 数据目录解析
# ---------------------------------------------------------------------------

def repo_root() -> Path:
    """代码根（插件源目录）= autocondense 包目录的父目录。

    注意：作为 ZCode 插件运行时代码会被复制进宿主缓存目录，此路径只用于
    sys.path 自举与服务 spawn 的 cwd，数据绝不放这里。
    """
    return Path(__file__).resolve().parent.parent


def resolve_data_dir() -> Path:
    """环境变量 AUTOCONDENSE_DATA_DIR 优先，否则用用户级 ~/.autocondense/。

    数据目录必须与代码位置解耦：插件更新会更换宿主缓存目录，仓库副本也可能
    移动，用户级固定目录保证快照/配置在任何运行位置都稳定。
    """
    env = os.environ.get(DATA_DIR_ENV)
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / ".autocondense"


# ---------------------------------------------------------------------------
# 通用文件工具（借鉴 SME 记忆插件已验证的工程模式）
# ---------------------------------------------------------------------------

def atomic_write_text(path: Path, text: str) -> None:
    """原子写：先写同目录临时文件，再 os.replace 覆盖，避免半截文件。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def quarantine_corrupt(path: Path) -> Optional[Path]:
    """坏文件改名 .corrupt-时间戳 保留现场；返回改名后的路径，失败返回 None。"""
    path = Path(path)
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.corrupt-{stamp}")
    try:
        os.replace(path, target)
        return target
    except OSError as e:
        logger.warning("坏文件改名失败 %s: %s", path, e)
        return None


# ---------------------------------------------------------------------------
# 配置模型
# ---------------------------------------------------------------------------

@dataclass
class SMEConfig:
    """SME 记忆服务桥接配置。"""

    enabled: bool = True
    base_url: str = "http://127.0.0.1:8000"
    timeout_s: float = 2.0
    tags: List[str] = field(default_factory=lambda: ["project", "todo", "session_digest"])
    importance: float = 0.7
    # digest 分段画像：True=逐段写入 SME，False=整篇兜底
    split_segments: bool = True
    max_segments: int = 15
    # 入库前去重：命中同项目同类型高分旧段时强化旧段（/memories/{id}/hit）而非重复入库
    dedup: bool = True
    # 去重命中阈值：检索 score ≥ 该值且 project/segment_type 匹配才算重复
    dedup_threshold: float = 0.92

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def apply_dict(self, d: Dict[str, Any]) -> None:
        for k, v in d.items():
            setattr(self, k, v)


@dataclass
class Config:
    """全局配置（PUT /config 深合并更新，原对象就地生效）。

    enabled：总开关。True=全自动压缩（零提示）；False=完全不干预。
    judge_defer_turns：大模型判定不通过后，延迟多少轮才允许重新判定。
    """

    port: int = 8761
    host: str = "127.0.0.1"
    enabled: bool = True
    cooldown_minutes: int = 30
    turns_threshold: int = 25
    water_threshold: float = 0.7
    usable_context_tokens: int = 160000
    task_min_rounds: int = 5
    judge_defer_turns: int = 5
    # DEFERRED 指数退避上限：judge_defer_turns * 2^defer_streak 封顶到该值
    judge_defer_max_turns: int = 20
    restore_max_chars: int = 8000
    restore_top_n: int = 3
    # 恢复包「重读关键文件」清单条数（从文件变更清单解析）
    restore_reread_files: int = 5
    # 增量摘要：True=已项目有快照时 /begin 默认要增量，攒满 delta_merge_every 份增量后强制全量合并
    delta_mode: bool = True
    delta_merge_every: int = 5
    # idle 候选：连续 N 轮无文件修改与工具失败 → 疑似任务停歇（活动记录缺失不触发）
    idle_signal_rounds: int = 3
    sme: SMEConfig = field(default_factory=SMEConfig)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def apply_dict(self, d: Dict[str, Any]) -> None:
        """就地深应用（嵌套 sme 对象合并而非替换，保证共享引用生效）。"""
        for k, v in d.items():
            if k == "sme" and isinstance(v, dict) and isinstance(self.sme, SMEConfig):
                self.sme.apply_dict(v)
            elif k != "sme":
                setattr(self, k, v)


# ---------------------------------------------------------------------------
# 校验规则（未知键拒绝 / 类型检查）
# ---------------------------------------------------------------------------

_TOP_RULES: Dict[str, type] = {
    "port": int,
    "host": str,
    "enabled": bool,
    "cooldown_minutes": int,
    "turns_threshold": int,
    "water_threshold": float,
    "usable_context_tokens": int,
    "task_min_rounds": int,
    "judge_defer_turns": int,
    "judge_defer_max_turns": int,
    "restore_max_chars": int,
    "restore_top_n": int,
    "restore_reread_files": int,
    "delta_mode": bool,
    "delta_merge_every": int,
    "idle_signal_rounds": int,
}

_SME_RULES: Dict[str, type] = {
    "enabled": bool,
    "base_url": str,
    "timeout_s": float,
    "tags": list,
    "importance": float,
    "split_segments": bool,
    "max_segments": int,
    "dedup": bool,
    "dedup_threshold": float,
}


def _type_error(key: str, value: Any, expected: type) -> List[str]:
    if expected is bool:
        if not isinstance(value, bool):
            return [f"{key} 应为布尔值"]
    elif expected is int:
        if isinstance(value, bool) or not isinstance(value, int):
            return [f"{key} 应为整数"]
    elif expected is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return [f"{key} 应为数字"]
    elif expected is str:
        if not isinstance(value, str):
            return [f"{key} 应为字符串"]
    elif expected is list:
        if not isinstance(value, list):
            return [f"{key} 应为数组"]
    return []


def validate_config_dict(data: Any) -> List[str]:
    """校验一份待合并的配置片段，返回中文错误列表（空列表 = 合法）。"""
    if not isinstance(data, dict):
        return ["配置必须是 JSON 对象"]
    errors: List[str] = []
    for k, v in data.items():
        if k == "sme":
            if not isinstance(v, dict):
                errors.append("sme 应为对象")
                continue
            for sk, sv in v.items():
                if sk not in _SME_RULES:
                    errors.append(f"未知配置项：sme.{sk}")
                else:
                    errors.extend(_type_error(f"sme.{sk}", sv, _SME_RULES[sk]))
        elif k not in _TOP_RULES:
            errors.append(f"未知配置项：{k}")
        else:
            errors.extend(_type_error(k, v, _TOP_RULES[k]))
    return errors


def _sanitize_config_dict(d: Any) -> Dict[str, Any]:
    """加载期容忍：丢掉未知/类型不符的键并告警，而不是启动失败。

    老配置文件里遗留的 "mode" 键会在这里被当未知键丢弃（告警不崩溃）。
    """
    if not isinstance(d, dict):
        return {}
    out: Dict[str, Any] = {}
    for k, v in d.items():
        if k == "sme" and isinstance(v, dict):
            out["sme"] = {
                sk: sv for sk, sv in v.items()
                if sk in _SME_RULES and not _type_error(f"sme.{sk}", sv, _SME_RULES[sk])
            }
        elif k in _TOP_RULES and not _type_error(k, v, _TOP_RULES[k]):
            out[k] = v
        else:
            logger.warning("配置项 %r 未知或类型不符，已忽略", k)
    return out


# ---------------------------------------------------------------------------
# 加载 / 保存
# ---------------------------------------------------------------------------

def default_config_dict(use_env: bool = True) -> Dict[str, Any]:
    """默认配置字典；端口可被环境变量 AUTOCONDENSE_PORT 覆盖。"""
    d = Config().to_dict()
    if use_env:
        raw = os.environ.get(PORT_ENV)
        if raw:
            try:
                d["port"] = int(raw)
            except ValueError:
                logger.warning("环境变量 %s=%r 不是整数，忽略", PORT_ENV, raw)
    return d


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """深合并：override 优先，嵌套 dict 递归合并。"""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(data_dir: Path) -> Config:
    """读 data/config.json：缺失给默认；损坏改名保留现场后用默认；与默认深合并。"""
    path = Path(data_dir) / "config.json"
    stored: Dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("配置根必须是 JSON 对象")
            stored = loaded
        except Exception as e:  # 窄场景：坏配置不阻断启动
            backup = quarantine_corrupt(path)
            logger.warning("配置文件损坏（%s），已回落默认值%s", e,
                           f"，现场保留为 {backup.name}" if backup else "")
    merged = deep_merge(default_config_dict(use_env=True), stored)
    sanitized = _sanitize_config_dict(merged)
    cfg = Config()
    cfg.apply_dict({k: v for k, v in sanitized.items() if k != "sme"})
    if isinstance(sanitized.get("sme"), dict):
        cfg.sme.apply_dict(sanitized["sme"])
    return cfg


def save_config(data_dir: Path, config: Config) -> Path:
    """原子保存配置到 data/config.json，返回路径。"""
    data_dir = Path(data_dir)
    path = data_dir / "config.json"
    atomic_write_text(path, json.dumps(config.to_dict(), ensure_ascii=False, indent=2) + "\n")
    return path
