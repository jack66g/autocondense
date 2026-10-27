"""会话摘要（digest）模板、校验、分段画像与恢复载荷。

恢复载荷按「优先级预算分配」打包（对齐 Claude Code「摘要 + 重读最近文件」思路）：
高优先级章节先装入 max_chars 预算，超预算时低优先级先丢，被截断时保留
结尾指针行「（完整快照：{full_file}）」；同时返回建议重读的文件清单
（reread_files，来自文件变更清单章节）。

增量（delta）摘要：/begin 可要求只写自上一份快照以来的变化；/restore 沿
delta 链把多份增量合并成完整状态（merge_delta_chain）后再打包。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("autocondense")

# 完整 markdown 模板，占位符仅 {project} {ts} {reason}
TEMPLATE = """# 会话快照 · {project} · {ts} · 触发:{reason}

## 目标（用户最终想要什么）

- （用 1-3 句写清用户最终想要什么，写结论不写过程）

## 关键决策与理由（含被否决的方案）

- 决策：（做了什么决定、为什么）
- 被否决：（考虑过但放弃的方案及原因）

## 已完成（每项注明验证方式：测试通过/编译通过/用户确认）

- （已完成事项：验证方式）

## 未完成/进行中（精确到下一个动作；无则写「（无）」）

（无）

## 文件变更清单（关键改动 file:line）

- （路径:行号 —— 关键改动说明）

## 踩过的坑与已排除的方向

- （坑与已排除方向，避免恢复后重蹈覆辙）

## 恢复指针（transcript 路径、更早快照路径、SME 检索关键词）

- transcript:（当前会话 transcript 路径）
- 更早快照:（最近一次快照文件路径，如无写「无」）
- SME 检索关键词:（3-5 个用于检索历史记忆的关键词）
"""

# 必填章节（validate 逐个检查是否出现）
REQUIRED_SECTIONS = ["## 目标", "## 已完成", "## 未完成", "## 文件变更清单", "## 恢复指针"]

# 摘要最小总长度（字符）
MIN_CHARS = 200

# 章节标题前缀（canonical）：split_digest 分段与恢复包打包共用同一套前缀，
# 避免「两套解析」漂移。模板章节标题都是这些前缀的完整形态。
SEC_GOAL = "## 目标"
SEC_DECISIONS = "## 关键决策与理由"
SEC_DONE = "## 已完成"
SEC_TODO = "## 未完成"
SEC_FILES = "## 文件变更清单"
SEC_PITFALLS = "## 踩过的坑与已排除的方向"
SEC_RESTORE_PTR = "## 恢复指针"


def render_template(project: str, ts: str, reason: str) -> str:
    return TEMPLATE.format(project=project, ts=ts, reason=reason)


# ---------------------------------------------------------------------------
# 章节解析（split_digest 与恢复包共用）
# ---------------------------------------------------------------------------

def _is_section_heading(line: str) -> bool:
    """`## ` 开头的二级标题（### 及更深不算章节边界）。"""
    return line.startswith("## ") or line.strip() == "##"


def split_sections(md: str) -> Tuple[List[str], List[Tuple[str, List[str]]]]:
    """按二级标题切分 markdown，返回 (preamble 行, [(标题行, 正文行列表)])。

    共享解析助手：分段画像（split_digest）、恢复包打包与 delta 合并链都
    走这一份实现，任何格式演进只需改这里。
    """
    preamble: List[str] = []
    sections: List[Tuple[str, List[str]]] = []
    cur_heading: Optional[str] = None
    cur_lines: List[str] = []
    for line in str(md or "").splitlines():
        if _is_section_heading(line):
            if cur_heading is not None:
                sections.append((cur_heading, cur_lines))
            cur_heading = line.strip()
            cur_lines = []
        elif cur_heading is None:
            preamble.append(line)
        else:
            cur_lines.append(line)
    if cur_heading is not None:
        sections.append((cur_heading, cur_lines))
    return preamble, sections


def _find_section(sections: List[Tuple[str, List[str]]],
                  prefix: str) -> Optional[Tuple[str, List[str]]]:
    """按 canonical 前缀找第一个命中章节，找不到返回 None。"""
    for heading, lines in sections:
        if heading.startswith(prefix):
            return heading, lines
    return None


def _section_content(md: str, prefix: str) -> str:
    """取 md 中指定章节的正文（strip 后）；章节缺席返回空串。"""
    _preamble, sections = split_sections(md)
    sec = _find_section(sections, prefix)
    if sec is None:
        return ""
    return "\n".join(sec[1]).strip()


# ---------------------------------------------------------------------------
# 分段画像（split_digest）
# ---------------------------------------------------------------------------

@dataclass
class Segment:
    """单条语义段画像：独立 tags / importance，逐条写入 SME 记忆库。"""

    type: str      # goal/accomplishment/todo/decision/pitfall/file_change/restore_pointer/misc/digest
    text: str
    tags: list
    importance: float


# 章节标题前缀 → (type, 追加 tag, importance, 拆分方式)
# 拆分方式：whole=整节一段；bullets=每个顶层 bullet 一段；files=bullet≤8 逐条、>8 合并
_SEGMENT_SPECS: List[Tuple[str, str, str, float, str]] = [
    (SEC_GOAL, "goal", "session_goal", 0.65, "whole"),
    (SEC_DECISIONS, "decision", "decision", 0.8, "bullets"),
    (SEC_DONE, "accomplishment", "accomplishment", 0.7, "bullets"),
    (SEC_TODO, "todo", "todo", 0.75, "bullets"),
    (SEC_FILES, "file_change", "file_change", 0.6, "files"),
    (SEC_PITFALLS, "pitfall", "pitfall", 0.8, "bullets"),
    (SEC_RESTORE_PTR, "restore_pointer", "restore_pointer", 0.5, "whole"),
]

_MISC_TYPE = "misc"
_MISC_TAG = "misc"
_MISC_IMPORTANCE = 0.55
_DIGEST_TYPE = "digest"
_DIGEST_TAG = "session_digest"
_DIGEST_IMPORTANCE = 0.7
# 文件变更清单 bullet 条数上限：≤ 上限逐条一段，超过则整节合并为一段
_FILE_LIST_MAX_BULLETS = 8


def _spec_for_heading(heading: str) -> Optional[Tuple[str, str, float, str]]:
    """按标题前缀匹配章节规格（模板章节标题原文的前缀匹配）。"""
    for prefix, seg_type, tag, importance, mode in _SEGMENT_SPECS:
        if heading.startswith(prefix):
            return seg_type, tag, importance, mode
    return None


def _split_top_bullets(lines: List[str]) -> List[List[str]]:
    """按顶层 bullet（行首 `- `/`* `）分组；缩进续行与非 bullet 散行并入所属 bullet。"""
    groups: List[List[str]] = []
    cur: Optional[List[str]] = None
    for line in lines:
        if line.startswith("- ") or line.startswith("* ") or line.strip() in ("-", "*"):
            cur = [line]
            groups.append(cur)
        elif cur is not None:
            # 缩进续行或非 bullet 散行 → 并入上一段
            if line.strip():
                cur.append(line)
        # bullet 之前的散行：无所属段落，忽略
    return groups


def _dedupe_tags(base_tags: List[str], extra: str) -> List[str]:
    out = list(base_tags or [])
    if extra not in out:
        out.append(extra)
    return out


def split_digest(digest_md: str, base_tags: List[str], project: str, ts: str,
                 trigger: str, cap: Optional[int] = None) -> List[Segment]:
    """把整篇摘要拆成多条语义段画像（纯函数，不碰磁盘/网络，任何输入不 raise）。

    - 每段 text 带 `[autocondense|{project}|{ts}|{trigger}]` 前缀，自包含可检索；
    - 段数超过 cap 时，按 importance 从低到高把溢出段合并成一个 misc 段（0.55）；
    - 空/没章节头/烂格式：兜底返回单个 type="digest" 整篇段（0.7）；
      有章节头但全部内容为空时返回 []（由调用方兜底整篇写入）。
    """
    try:
        return _split_digest_impl(digest_md, base_tags, project, ts, trigger, cap)
    except Exception as e:  # 铁律：分段绝不因烂输入 raise
        logger.warning("摘要分段失败，兜底整篇段：%s", e)
        prefix = f"[autocondense|{project}|{ts}|{trigger}] "
        return [Segment(type=_DIGEST_TYPE, text=prefix + str(digest_md or "").strip(),
                        tags=_dedupe_tags(base_tags, _DIGEST_TAG),
                        importance=_DIGEST_IMPORTANCE)]


def _split_digest_impl(digest_md: str, base_tags: List[str], project: str, ts: str,
                       trigger: str, cap: Optional[int]) -> List[Segment]:
    text = str(digest_md or "")
    preamble, sections = split_sections(text)

    # 没有任何章节头 → 兜底整篇一段
    if not sections:
        prefix = f"[autocondense|{project}|{ts}|{trigger}] "
        return [Segment(type=_DIGEST_TYPE, text=prefix + text.strip(),
                        tags=_dedupe_tags(base_tags, _DIGEST_TAG),
                        importance=_DIGEST_IMPORTANCE)]

    # (type, tag, importance, content)：先攒内容，最后统一加前缀（便于溢出合并）
    raw: List[Tuple[str, str, float, str]] = []
    misc_chunks: List[str] = []

    # preamble 里的 h1 标题行信息已由前缀携带，忽略；其余散行归 misc
    for line in preamble:
        if line.strip() and not line.startswith("# "):
            misc_chunks.append(line.strip())

    for heading, sec_lines in sections:
        spec = _spec_for_heading(heading)
        content = "\n".join(sec_lines).strip()
        if spec is None:
            # 未识别章节 → 合并为一个 misc 段
            if content:
                misc_chunks.append(f"{heading}\n{content}")
            continue
        seg_type, tag, importance, mode = spec
        if not content:
            continue
        if mode == "whole":
            raw.append((seg_type, tag, importance, content))
            continue
        groups = _split_top_bullets(sec_lines)
        if not groups:
            # 无顶层 bullet（如「（无）」散行）：整节一段，避免信息丢失
            raw.append((seg_type, tag, importance, content))
        elif mode == "files" and len(groups) > _FILE_LIST_MAX_BULLETS:
            # 文件清单超长：合并为一段
            raw.append((seg_type, tag, importance, content))
        else:
            for g in groups:
                gtext = "\n".join(g).strip()
                if gtext:
                    raw.append((seg_type, tag, importance, gtext))

    if misc_chunks:
        raw.append((_MISC_TYPE, _MISC_TAG, _MISC_IMPORTANCE, "\n\n".join(misc_chunks)))

    # 溢出合并：保留 importance 最高的 cap-1 段，其余合成一个 misc 段
    if cap is not None and cap >= 1 and len(raw) > cap:
        keep = cap - 1
        order = sorted(range(len(raw)), key=lambda i: raw[i][2])
        overflow = set(order[: len(raw) - keep])
        kept = [r for i, r in enumerate(raw) if i not in overflow]
        merged = "\n\n".join(raw[i][3] for i in sorted(overflow))
        kept.append((_MISC_TYPE, _MISC_TAG, _MISC_IMPORTANCE, merged))
        raw = kept

    if not raw:
        return []

    prefix = f"[autocondense|{project}|{ts}|{trigger}] "
    return [Segment(type=t, text=prefix + c, tags=_dedupe_tags(base_tags, tag),
                    importance=imp)
            for t, tag, imp, c in raw]


def validate(digest_md: str) -> Tuple[bool, List[str]]:
    """校验摘要：必填章节齐全 + 总长 ≥ 200 字符。返回 (ok, 中文错误列表)。"""
    errors: List[str] = []
    if not digest_md or not digest_md.strip():
        return False, ["摘要为空"]
    for sec in REQUIRED_SECTIONS:
        if sec not in digest_md:
            errors.append(f"缺少必填章节：{sec}")
    if len(digest_md) < MIN_CHARS:
        errors.append(f"总长度不足 {MIN_CHARS} 字符（当前 {len(digest_md)}）")
    return (not errors), errors


def _read_text_safe(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError as e:
        logger.warning("读取快照失败 %s: %s", path, e)
        return ""


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


# ---------------------------------------------------------------------------
# 恢复载荷：优先级预算分配（优化 1）
# ---------------------------------------------------------------------------

# 恢复包各章节的设计上限（与预算无关的固定压缩规则）
_BULLET_CHAR_CAP = 200     # 决策/坑：每条截 ≤200 字符
_BULLET_PER_SECTION_CAP = 10   # 决策/坑/已完成：每节最多条数
_DONE_LINE_CAP = 80        # 已完成：每条压成一行 ≤80 字符
_GOAL_CHAR_CAP = 300       # 目标：≤300 字符

_RESTORE_POINTER_FMT = "（完整快照：{full_file}）"


def _strip_bullet_marker(block: str) -> str:
    """去掉块首行的顶层 bullet 标记（- / * ），保留续行原样。"""
    if not block:
        return block
    lines = block.splitlines()
    stripped = lines[0].lstrip()
    for marker in ("- ", "* "):
        if stripped.startswith(marker):
            lines = [stripped[len(marker):]] + lines[1:]
            break
    return "\n".join(lines)


def extract_reread_files(md: str, limit: int) -> List[str]:
    """从「## 文件变更清单」bullet 解析建议重读的文件路径。

    路径 token = bullet 行首（去掉 `- `/`* `）到首个冒号/空格前的内容；
    模板占位（以全角括号开头）与空 token 跳过，按原文顺序取前 limit 条。
    """
    if not md or not limit or limit <= 0:
        return []
    _preamble, sections = split_sections(md)
    sec = _find_section(sections, SEC_FILES)
    if sec is None:
        return []
    out: List[str] = []
    for group in _split_top_bullets(sec[1]):
        content = _strip_bullet_marker(group[0] if group else "").strip()
        token = re.split(r"[:\s]", content, 1)[0].strip() if content else ""
        if not token or token.startswith("（"):
            continue
        out.append(token)
        if len(out) >= limit:
            break
    return out


def _todo_open(md: str) -> bool:
    """未完成章节是否有实际内容（非空且不是「（无）」占位）。"""
    content = _section_content(md, SEC_TODO)
    if not content:
        return False
    return content.replace(" ", "") not in ("（无）", "(无)", "无")


def _pack_restore(md: str, max_chars: int, old_titles: List[str]) -> Tuple[str, bool]:
    """按优先级把 md 打包进 max_chars 预算，返回 (文本, 是否丢了内容)。

    优先级（从高到低，逐项装入直到预算用尽，超预算低优先级先丢）：
      0) h1 标题行（恢复包自身的文档头）
      1) 未完成 整节
      2) 恢复指针 整节
      3) 关键决策与理由 / 踩过的坑：逐 bullet（每条 ≤200 字符、每节 ≤10 条）
      4) 目标（≤300 字符）
      5) 已完成：每 bullet 压成一行（≤80 字符、≤10 条）
      6) preamble 里除 h1 外的散行（兜底不丢自定义内容）
      7) 更旧快照的标题行

    丢了内容 = 预算外丢弃 或 触发任一设计上限截断（此时都值得指路完整快照）。
    """
    if max_chars is None or max_chars <= 0:
        return "", bool(md and md.strip())

    preamble, sections = split_sections(md)
    parts: List[str] = []
    used = 0  # 每块按 len(block)+1（换行）累计，保证 join 后总长 ≤ max_chars
    lost = False

    def _room() -> int:
        return max_chars - used

    def _emit(block: str) -> None:
        nonlocal used
        parts.append(block)
        used += len(block) + 1

    def _whole_section(prefix: str) -> None:
        nonlocal lost
        sec = _find_section(sections, prefix)
        if sec is None:
            return
        heading, body = sec
        content = "\n".join(body).strip()
        if not content:
            return
        block = f"{heading}\n\n{content}"
        if len(block) + 1 <= _room():
            _emit(block)
        elif _room() > len(heading) + 3:
            # 整节放不下：保标题截正文（最高两档不整段丢弃）
            _emit(f"{heading}\n\n{content[:_room() - len(heading) - 3]}")
            lost = True
        else:
            lost = True

    def _bullet_section(prefix: str, one_line: bool) -> None:
        nonlocal lost
        sec = _find_section(sections, prefix)
        if sec is None:
            return
        heading, body = sec
        groups = _split_top_bullets(body)
        if not groups:
            # 无 bullet（如「（无）」散行）：整节一块
            content = "\n".join(body).strip()
            if not content:
                return
            if len(heading) + len(content) + 3 <= _room():
                _emit(f"{heading}\n\n{content}")
            else:
                lost = True
            return
        bullets: List[str] = []
        for g in groups:
            text = _strip_bullet_marker("\n".join(g)).strip()
            if one_line:
                text = " ".join(text.split())
            if not text:
                continue
            if len(text) > (_DONE_LINE_CAP if one_line else _BULLET_CHAR_CAP):
                lost = True  # 字符上限截断
            cap = _DONE_LINE_CAP if one_line else _BULLET_CHAR_CAP
            bullets.append(text[:cap])
        if len(bullets) > _BULLET_PER_SECTION_CAP:
            lost = True  # 条数上限丢弃
            bullets = bullets[:_BULLET_PER_SECTION_CAP]
        # 预算内尽量多装：从尾部丢 bullet 直到放得下（整节都放不下则整节丢弃）
        while bullets:
            block = f"{heading}\n" + "\n".join(f"- {b}" for b in bullets)
            if len(block) + 1 <= _room():
                _emit(block)
                return
            bullets.pop()
            lost = True
        lost = True  # 连一条 bullet 都放不下

    title = next((line.strip() for line in str(md or "").splitlines()
                  if line.strip()), "")
    if title:
        if len(title) + 1 <= _room():
            _emit(title)
        else:
            _emit(title[:max(0, _room() - 1)])
            lost = True

    if sections:
        _whole_section(SEC_TODO)          # 1) 未完成
        _whole_section(SEC_RESTORE_PTR)   # 2) 恢复指针
        _bullet_section(SEC_DECISIONS, one_line=False)  # 3) 决策
        _bullet_section(SEC_PITFALLS, one_line=False)   # 3) 坑
        # 4) 目标：≤300 字符
        sec = _find_section(sections, SEC_GOAL)
        if sec is not None:
            heading, body = sec
            content = "\n".join(body).strip()
            if content:
                if len(content) > _GOAL_CHAR_CAP:
                    lost = True
                    content = content[:_GOAL_CHAR_CAP]
                block = f"{heading}\n\n{content}"
                if len(block) + 1 <= _room():
                    _emit(block)
                elif _room() > len(heading) + 3:
                    _emit(f"{heading}\n\n{content[:_room() - len(heading) - 3]}")
                    lost = True
                else:
                    lost = True
        _bullet_section(SEC_DONE, one_line=True)        # 5) 已完成
    else:
        # 没有任何章节头（非模板格式的快照）：整篇视作单块，避免空手而归
        content = str(md or "").strip()
        if content:
            if len(content) + 1 <= _room():
                _emit(content)
            else:
                _emit(content[:max(0, _room() - 1)])
                lost = True

    # 6) preamble 散行（h1 除外）
    extra = "\n".join(l for l in preamble if l.strip() and not l.startswith("# "))
    if extra:
        if len(extra) + 1 <= _room():
            _emit(extra)
        else:
            lost = True

    # 7) 更旧快照标题行
    for t in old_titles:
        if len(t) + 1 <= _room():
            _emit(t)
        else:
            lost = True

    return "\n".join(parts), lost


def build_restore_payload(snapshot_files: List[Any], max_chars: int,
                          reread_limit: int = 5,
                          merged_md: Optional[str] = None) -> Dict[str, Any]:
    """合并快照生成优先级恢复载荷。

    约定 snapshot_files 最新在前；merged_md 非空时（delta 链已在调用方合并）
    以它为打包底稿，否则用最新快照全文。旧快照只贡献标题行（延续既有行为）。
    超预算被截断时文末保留「（完整快照：{full_file}）」指针行。
    """
    files = [Path(f) for f in (snapshot_files or []) if Path(f).is_file()]
    if not files:
        return {"text": "", "truncated": False, "full_file": None, "note": None,
                "reread_files": [], "todo_open": False}
    newest = files[0]
    full_file = str(newest.resolve())
    base_md = merged_md if merged_md is not None else _read_text_safe(newest)
    old_titles: List[str] = []
    for old in files[1:]:
        t = _first_line(_read_text_safe(old))
        if t:
            old_titles.append(t)

    text, lost = _pack_restore(base_md, max_chars, old_titles)
    if lost:
        pointer = _RESTORE_POINTER_FMT.format(full_file=full_file)
        reserve = len(pointer) + 1
        if len(text) + reserve > max_chars:
            text, _lost2 = _pack_restore(base_md, max(0, max_chars - reserve), old_titles)
        if text and len(text) + reserve <= max_chars:
            text = text.rstrip() + "\n" + pointer
        else:
            # 预算连指针行都放不下：只保留（截断的）指针行
            text = ("\n" + pointer)[:max_chars] if max_chars > len(pointer) else pointer[:max_chars]
    return {
        "text": text,
        "truncated": lost,
        "full_file": full_file,
        "note": f"完整内容在 {full_file}，需要时用 Read 工具读取",
        "reread_files": extract_reread_files(base_md, reread_limit),
        "todo_open": _todo_open(base_md),
    }


# ---------------------------------------------------------------------------
# delta 合并链（优化 5）：多份增量快照 → 一份完整状态
# ---------------------------------------------------------------------------

# 合并规则：whole=取最新非空版本；union=bullet 并集（相同文本去重，新条目在前）
_MERGE_SPECS: List[Tuple[str, str]] = [
    (SEC_GOAL, "whole"),
    (SEC_DECISIONS, "union"),
    (SEC_DONE, "union"),
    (SEC_TODO, "whole"),
    (SEC_FILES, "union"),
    (SEC_PITFALLS, "union"),
    (SEC_RESTORE_PTR, "whole"),
]


def _bullet_groups(lines: List[str]) -> List[str]:
    """顶层 bullet → 文本块列表（去掉 bullet 标记）；无 bullet 时把非空正文视作单块。"""
    groups = [g for g in _split_top_bullets(lines) if "\n".join(g).strip()]
    if groups:
        return [_strip_bullet_marker("\n".join(g)).strip() for g in groups]
    content = "\n".join(lines).strip()
    return [content] if content else []


def merge_delta_chain(texts: List[str]) -> str:
    """把 delta 链的多份摘要（最新在前）合并成一份完整 markdown。

    - 未完成/恢复指针/目标：取最新非空版本（整节代表当前状态）；
    - 已完成/决策/坑/文件变更：bullet 并集，相同文本去重，新条目在前；
    - 标题取自提供该章节内容的最新快照（缺失时用 canonical 前缀兜底）。
    """
    texts = [t for t in (texts or []) if t and t.strip()]
    if not texts:
        return ""
    if len(texts) == 1:
        return texts[0]
    parsed = [split_sections(t) for t in texts]

    out_blocks: List[str] = []
    title = next((_first_line(t) for t in texts if _first_line(t)), "")
    if title:
        out_blocks.append(title)

    for prefix, mode in _MERGE_SPECS:
        if mode == "whole":
            for _preamble, sections in parsed:
                sec = _find_section(sections, prefix)
                if sec is None:
                    continue
                content = "\n".join(sec[1]).strip()
                if content:
                    out_blocks.append(f"{sec[0]}\n\n{content}")
                    break
        else:
            seen: set = set()
            bullets: List[str] = []
            heading: Optional[str] = None
            for _preamble, sections in parsed:
                sec = _find_section(sections, prefix)
                if sec is None:
                    continue
                content = "\n".join(sec[1]).strip()
                if content and heading is None:
                    heading = sec[0]
                for b in _bullet_groups(sec[1]):
                    if b not in seen:
                        seen.add(b)
                        bullets.append(b)
            if bullets:
                out_blocks.append(f"{heading or prefix}\n" + "\n".join(f"- {b}" for b in bullets))

    return "\n\n".join(out_blocks).strip() + "\n"
