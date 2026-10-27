# autocondense REST 契约（v0.3.0）

> **接入层（MCP / hooks / installer）按本文档开发。** 本文档与 `autocondense/service.py` 的实现保持一字不差；任何行为变更必须先改这里。

- 基地址：`http://127.0.0.1:8761`（可用 `--port/--host` 或配置覆盖）
- 所有请求/响应均为 JSON（UTF-8）
- 启动：`python -m autocondense.service [--port --host --data-dir]` 或 `uvicorn autocondense.service:app`
- 服务无鉴权，仅监听 127.0.0.1，不对外
- **全自动语义**：`enabled=true`（默认）时全程零提示、零人工确认——规则命中「压缩候选」（should=JUDGE）后由调用方（智能体本身即大模型）立即 `POST /judge` 提交判定表，判定通过直接执行压缩；`enabled=false` 时一切判定 NO，完全不干预。不存在「询问用户」环节。
- **耗时提示**：`POST /commit` 在 SME 在线时含逐段去重检索 + 批量入库，实测可达 ~90 秒；MCP 代理的 `condense_commit` 已单独放宽到 300 秒（其余工具默认 60 秒）。自研接入方直接调 REST 时请按此设置客户端超时。

## 错误约定

| 场景 | 状态码 | 响应体 |
| --- | --- | --- |
| 业务校验失败（非法摘要、未知配置项、会话不存在、缺少 session_id、判定表字段非法） | 400 | `{"errors": ["中文错误1", ...]}` |
| 请求体不符合 pydantic 模型（缺必填字段/类型不符，FastAPI 默认行为） | 422 | `{"detail": [...]}` |
| 其余未捕获异常 | 500 | FastAPI 默认 |

SME 记忆服务不可达**不是错误**：所有 SME 失败只降级（`stored:false` / `sme_results:[]` / `sme_connected:false`），不影响本地快照与判定。

## 公共对象

### 判定结果对象（候选判定）

`POST /report`、`GET /check`、`POST /events/hook`(PostToolUse-TodoWrite / Stop) 的核心返回（trigger 取值含 idle）：

```json
{
  "should": "JUDGE | NO",
  "trigger": "plan_done | task_done | water_level | idle | turns | null",
  "urgency": "normal | urgent",
  "reason": "中文原因",
  "signals": { "...见下..." },
  "action_hint": "中文动作建议或 null",
  "judgment_form": { "...仅 should=JUDGE 时携带，见下..." }
}
```

- `should=NO` 时 `trigger=null`、`action_hint=null`、无 `judgment_form`
- `should=JUDGE`（规则命中压缩候选）时：
  - `action_hint` 恒为 `"立即调用 condense_judge 提交判定，不要询问用户"`
  - `rounds_to_full`：informational（无采样时 null），由真实 token 采样估算「还有多少轮水位到 95%」，帮判定模型感受上下文逼近速度
  - `judgment_form` 为大模型判定表说明，调用方据此立即填写并 `POST /judge`：

```json
{
  "description": "轻量判定表：请基于当前会话真实状态填写并提交，不要询问用户",
  "fields": {
    "task_finished": "bool，当前批次任务是否已收尾（计划清空/目标达成）",
    "impact": "low|medium|high 三选一，现在压缩对当前工作的影响",
    "impact_reason": "str，影响判定的简要理由",
    "safe_to_compress": "bool，是否存在未保存/易丢失的关键上下文",
    "reason": "str，整体判定理由",
    "confidence": "可选，high|medium|low，对本次判定的置信度（不传则忽略）"
  },
  "submit": "POST /judge，body：{\"session_id\": \"...\"（或 \"cwd\"），\"verdict\": {上述五字段}}",
  "matrix": {
    "urgent（水位≥0.85）": "safe_to_compress 且 impact != high 即通过（任务未完成也可压：水位危急时先存档胜过宿主有损强压）",
    "normal": "safe_to_compress 且 task_finished 且 impact == low 才通过"
  }
}
```

- `urgency`：水位 ≥ 0.85（URGENT_WATER）→ `"urgent"`（判定矩阵放宽、可无视延迟窗口）；其余（含无水位）→ `"normal"`

### signals 对象

```json
{
  "plan_total": 3,            // 当前计划条目数
  "plan_completed": 1,
  "plan_in_progress": 1,
  "turns_since_condense": 12, // 距上次压缩的轮次
  "minutes_since_condense": 25.5,  // null=从未压缩过
  "est_tokens": 84000,        // token：优先新鲜的智能体上报值（context_tokens 且最近 10 轮内更新），其次 transcript 估算，再次过期上报值；null=皆无
  "token_source": "reported", // est_tokens 来源："reported"=智能体上报 | "estimated"=transcript 启发式估算 | null
  "rounds_to_full": 8,        // 由 token 采样估算「还有多少轮水位到 95%」；采样不足两轮/增量为负时 null（informational，不参与判定）
  "idle_rounds": 2,           // 距最近一次「文件修改 / 工具失败」活动轮的轮数；无任何活动记录时 null（不据此误报 idle）
  "water_level": 0.525,       // est_tokens / usable_context_tokens；null=无 est
  "cooldown_active": false,
  "urgency": "normal"         // water_level ≥ 0.85 时为 "urgent"
}
```

transcript 估算口径：逐行累计 `CJK 字符数 ×1 + 其他字符数 ×0.25`（不同模型 tokenizer 不同，水位判定只需量级，故不做精确 tokenize）。

`rounds_to_full` 估算口径：每轮平均增量 = (最新采样 - 最早采样) / 轮差（采样来自 hooks/接口上报的 context_tokens，最多保留 10 条）；avg>0 时 `rounds = ceil(usable*0.95 - 当前 token) / avg`，否则 null。

### 会话摘要对象（session summary）

`GET /status` 的 sessions 元素、`POST /session/start` 的返回：

```json
{
  "session_id": "...", "cwd": "...",
  "transcript_path": null, "context_tokens": null,
  "context_tokens_turn": null,   // context_tokens 最近一次上报时的轮次（判断真实值是否新鲜，10 轮保鲜）
  "token_samples": [],           // 最近 {turn, tokens} 采样（最多 10 条，rounds_to_full 估算依据）
  "est_tokens": null,
  "plan_total": 0, "plan_completed": 0, "plan_in_progress": 0,
  "turns": 0, "turns_since_condense": 0,
  "last_condense_at": null, "cooldown_until": null, "last_trigger": null,
  "condensing": false,
  "last_judge": null,         // 最近一次大模型判定 {turn, ts, verdict(原样字段，可含 confidence), result:"APPROVED"|"DEFERRED"}；从未判定为 null
  "defer_until_turn": null,   // 判定 DEFERRED 后的延迟窗口：当前轮次 < 该值时不重新判定（urgent 除外）；commit 后清空
  "defer_streak": 0,          // 连续 DEFERRED 次数（指数退避指数）；commit 后归零
  "begin_mode": null,         // 本次 /begin 决定的摘要模式 "delta"|"full"；/commit 消费后清空
  "last_file_turn": null,     // 最近一次文件修改类工具（Edit/Write/MultiEdit/NotebookEdit）所在轮；idle 信号用
  "last_fail_turn": null,     // 最近一次工具失败（PostToolUseFailure）所在轮；idle 信号用
  "created_at": "...", "last_seen": "..."
}
```

### 快照索引条目（index entry）

```json
{
  "file": "C:\\...\\snapshots\\{hash}\\20261004-223056-plan_done.md",
  "ts": "2026-10-04T22:30:56.123",   // ISO 毫秒精度：同一秒内连续提交（delta 链场景）也能按 ts 稳定排序
  "trigger": "plan_done",
  "session_id": "...",
  "title": "# 会话快照 · ...（摘要第一个非空行）",
  "char_count": 735,
  "sme_memory_id": "mem-1",           // SME 入库成功且拿到具体 id 时回填（分段写入时为首个成功段 id）；批量入库不返回各条 id，此时为 null
  "delta": false                       // true=增量快照（恢复时沿 delta 链向前合并到全量锚点）；老索引无该字段按全量（false）处理
}
```

## 判定流水线（/check、/report、Stop hook 共用，顺序固定）

| 序 | 条件 | 结果 |
| --- | --- | --- |
| 0a | `enabled=false`（插件关闭） | NO（reason=插件已关闭） |
| 0b | `turns < defer_until_turn` 且 urgency != urgent（判定延迟窗口内） | NO（reason=延迟中，第 N 轮后重新评估） |
| 1 | `plan_in_progress > 0`（有进行中任务，绝不压） | NO |
| 2 | `cooldown_active`（冷却未过） | NO |
| 3 | `plan_total > 0` 且 `plan_completed == plan_total` | JUDGE / `plan_done` |
| 4 | 存在 completed 条目（当前计划或归档）满足 `completed_turn - first_seen_turn >= task_min_rounds`，且无 in_progress | JUDGE / `task_done` |
| 5 | `water_level >= water_threshold` | JUDGE / `water_level` |
| 6 | `idle_rounds >= idle_signal_rounds`（活动记录存在时；连续 N 轮无文件修改与工具失败，疑似任务停歇） | JUDGE / `idle` |
| 7 | `turns_since_condense >= turns_threshold` | JUDGE / `turns` |
| 8 | 否则 | NO |

规则只产生**压缩候选**（JUDGE）；是否真正压缩由 `POST /judge` 的判定矩阵决定（见下）。会话不存在 → 400。

## 端点明细

### GET /health

```json
{"app": "autocondense", "status": "ok", "version": "<autocondense.__version__>", "sme_connected": false, "data_dir": "C:\\..."}
```

`sme_connected`：SME 启用且 `GET {sme.base_url}/health` 返回 200（结果缓存 60 秒）。
`app`/`version`：MCP 代理据此识别「本插件族」并做**版本接管**（见下）。

### 版本接管（插件更新后的残留服务，全自动恢复）

常驻服务由 MCP 代理惰性拉起，进程独立于宿主存活——插件更新后端口上可能
残留旧版本服务。MCP 代理的 `_ensure_service` 约定：

1. `/health` 只认 `status=ok` 且带 `version` 的应答体（不明应用不算就绪，绝不误杀）；
2. 端口上的服务 `version` 与本代理不一致 → 接管：读 `data/service.pid` 定点结束
   （无 pid 文件的旧版经 netstat/lsof 按端口反查），等端口空出后重拉当前版本；
3. 识别不到进程、或旧进程杀不掉 → 抛 RuntimeError 提示手动处理，绝不盲杀。

`service.pid` 由 `python -m autocondense.service` 启动时写入（`{pid, version, port,
started}`，原子写），正常退出时清理；Windows 强杀残留的陈旧记录由读取方容忍。

### POST /session/start

请求：`{"session_id": 必须, "cwd": 必须, "transcript_path": 可选, "context_tokens": 可选int}`
响应 200：会话摘要对象。会话已存在则更新（未给出的字段保留原值）。

### POST /report

请求：`{"session_id"!, "cwd"!, "plan_items"!: [{"title"!, "status": 默认"pending"}]}`

- upsert 会话 → 用 plan_items 整体合并计划 → 返回判定结果对象
- `status` 归一化：大小写无关；`completed/done → completed`，`in_progress/in-progress/inprogress → in_progress`，其余（含 pending）→ `pending`

**计划合并语义**（对接入层透明，但影响判定）：
- 新条目记 `first_seen_turn = 当前 turns`；初见即 completed 的同时记 `completed_turn = 当前 turns`
- 状态变为 completed 记 `completed_turn = 当前 turns`（first_seen 不覆盖）
- 从列表消失的 completed 条目移入会话内归档 `plan_archive`（保留跨度供规则 4 评分）；消失的 pending/in_progress 条目直接丢弃
- `POST /commit` 成功后：当前 completed 条目被清除（**不进归档**——已被本次压缩消费），pending/in_progress 保留
- `mark_condensed`（commit 或 PreCompact）清空 `plan_archive`，避免冷却结束后旧条目重复触发 task_done

### POST /judge（大模型判定）

请求：

```json
{"session_id": "..." , "cwd": "...", "verdict": {
   "task_finished": true,          // bool，必填
   "impact": "low",                // "low"|"medium"|"high"，必填
   "impact_reason": "已收尾",       // str，必填（可为空串）
   "safe_to_compress": true,       // bool，必填
   "reason": "适合压缩",            // str，必填（可为空串）
   "confidence": "high"             // 可选，"high"|"medium"|"low"，对本次判定的置信度（不传则忽略；不参与矩阵）
 }}
```

- 会话标识 `session_id` / `cwd` 二选一（都缺 → 400；`cwd` 按绝对路径匹配现有会话）
- `verdict` 五必填字段缺一/类型错/`impact` 越界 → 400 `{"errors": [...]}`（中文逐条）；`confidence` 传了但非 high|medium|low 同样 400
- `enabled=false` → 直接返回 `{"decision":"NO", ...}`
- urgency 用**判定时刻**的实时信号计算（不用 /check 的缓存）

**判定矩阵**：

| urgency | 通过条件 | 未通过时的延迟 |
| --- | --- | --- |
| `normal` | `safe_to_compress` 且 `task_finished` 且 `impact == low` | 指数退避（见下） |
| `urgent`（水位 ≥0.85） | `safe_to_compress` 且 `impact != high`（任务未完成也可压：水位危急时先存档胜过宿主有损强压） | 同上（但 urgent 候选可无视窗口立即重新判定） |

**DEFERRED 指数退避**：连续 DEFERRED 会拉长延迟窗口，
`step = min(judge_defer_turns × 2^defer_streak, judge_defer_max_turns)`（默认 5 → 10 → 20 封顶），
`defer_until_turn = 当前轮次 + step`，`defer_streak` +1；判定通过（APPROVED）或 commit 成功后 `defer_streak` 归零、窗口清空。

响应 200（通过）：

```json
{"decision": "COMPRESS_NOW", "urgency": "normal",
 "reason": "判定通过（normal 档矩阵）：{判定 reason}",
 "action_hint": "立即执行压缩流程：condense_begin → 填写摘要 → condense_commit，全程无需询问用户",
 "signals": {...},
 "rounds_to_full": 8}
```

响应 200（未通过）：

```json
{"decision": "DEFERRED", "urgency": "normal",
 "reason": "判定未通过（任务尚未收尾；压缩影响为 medium（normal 档要求 low））：{判定 reason}",
 "action_hint": "继续当前工作，不要压缩，稍后自动重新评估",
 "defer_until_turn": 5, "defer_streak": 1,
 "signals": {...},
 "rounds_to_full": 8}
```

`rounds_to_full`：informational（无采样时 null），帮判定模型感受上下文逼近速度。

副作用：写入会话 `last_judge`（{turn, ts, verdict, result:"APPROVED"|"DEFERRED"}，verdict 原样含可选 confidence）与 `judge` 事件；DEFERRED 时设置 `defer_until_turn` 并累加 `defer_streak`。延迟窗口内（且非 urgent）候选判定一律 NO；commit 成功后窗口清空、退避计数归零。

### POST /events/hook（钩子统一入口）

请求（全部字段可选除 event）：

```json
{"event"!, "session_id"?, "cwd"?, "transcript_path"?, "context_tokens"?,
 "source"?, "tool_name"?, "tool_input"?}
```

按 event 分支：

| event | 行为 | 响应 200 |
| --- | --- | --- |
| `PostToolUse` 且 `tool_name == "TodoWrite"` | 解析 tool_input 更新计划 → 判定流水线 | 判定结果对象 |
| `PostToolUse` 且 `tool_name ∈ {Edit, Write, MultiEdit, NotebookEdit}` | 记文件活动轮（`last_file_turn`，idle 信号基准）；顺带更新 context_tokens 采样 | `{"ok": true, "ignored": true, "reason": "工具 X 已记录文件修改活动"}` |
| `PostToolUse` 其他工具 | 忽略 | `{"ok": true, "ignored": true, "reason": "工具 X 不参与计划追踪"}` |
| `PostToolUseFailure` | 记失败活动轮（`last_fail_turn`——有失败说明会话仍在推进，不算 idle）；顺带更新 context_tokens 采样 | `{"ok": true, "ignored": true, "reason": "工具 X 失败已记为活动轮"}` |
| `SessionStart` | upsert 会话、记 transcript_path/context_tokens；`source ∈ {compact, clear, resume}` 且存在快照时构建恢复载荷 | `{"ok": true, "session_id": "...", "additional_context": {恢复载荷} 或 null}` |
| `Stop` | bump_turn → 判定流水线 | 判定结果对象 + `"instruction": 中文指令 或 null` |
| `PreCompact` | 记事件并 `mark_condensed("pre_native_compact")`（原生压缩已发生，防止紧接着重复压） | `{"ok": true}` |
| 其他 | 忽略 | `{"ok": true, "ignored": true, "reason": "未处理的事件类型：X"}` |

需要 session_id 的事件缺失时 → 400 `{"errors": [...]}`。

**hooks 事件配置（接入层应注册哪些事件）**：

| 宿主 | 事件 | 说明 |
| --- | --- | --- |
| ZCode（插件 hooks.json 或 `hooks.events`，4 个） | PostToolUse（matcher `"TodoWrite|Edit|Write"`，regex；服务端再按 tool_name 精确分类）、Stop（无 matcher）、SessionStart（matcher `"compact|clear|resume"`）、PostToolUseFailure（无 matcher） | PostToolUseFailure 为 ZCode 独有事件 |
| Claude Code（settings.json `hooks`，4 个） | PostToolUse（matcher 同上）、Stop、SessionStart，PreCompact（matcher `"auto"`） | Claude Code 无 PostToolUseFailure；PreCompact 仅 Claude Code |

**TodoWrite 的 tool_input 兼容三种形态**（title 取 `title` 或 `content`，status 归一化同上）：

```json
{"todos": [{"content": "...", "status": "in_progress"}]}     // Claude Code 形态
{"items": [{"title": "...", "status": "pending"}]}           // items 包裹
[{"title": "...", "status": "DONE"}]                          // 直接数组
```

**additional_context**（SessionStart 恢复载荷，= build_restore_payload 优先级恢复包，钩子脚本直接包装进 additionalContext）：

```json
{"text": "h1 标题\n## 未完成/进行中 整节\n## 恢复指针 整节\n## 关键决策与理由（逐条）\n...\n旧快照标题行...",
 "truncated": false,
 "full_file": "C:\\...\\xxx.md",
 "note": "完整内容在 C:\\...\\xxx.md，需要时用 Read 读取",
 "reread_files": ["src/a.py", "tests/test_a.py"],
 "todo_open": true}
```

- **优先级预算打包**（从高到低装入 `restore_max_chars` 预算，超预算低优先级先丢）：
  0) h1 标题行；1) 「未完成/进行中」整节；2) 「恢复指针」整节；3) 决策/坑逐条（每条 ≤200 字符、每节 ≤10 条）；
  4) 目标（≤300 字符）；5) 已完成每条压一行（≤80 字符、≤10 条）；6) preamble 散行；7) 更旧快照标题行。
  触发任一上限截断或预算外丢弃 → `truncated: true`，文末追加指针行「（完整快照：{full_file}）」。
- `reread_files`：从「文件变更清单」解析的建议重读文件（模板占位跳过，取前 `restore_reread_files` 条）；
  `todo_open`：未完成章节是否有实际内容（非「（无）」占位）——两者供 `/restore` 的 `next_actions` 使用
- delta 链：项目存在连续增量快照时，恢复前先沿 delta 链（深度上限 `delta_merge_every`+1）合并成完整状态再打包；
  链不完整（窗口内找不到全量锚点）则只用最新快照兜底
- 无快照时 `additional_context` 为 `null`（payload 的空形态：`{"text":"", "truncated":false, "full_file":null, "note":null, "reread_files":[], "todo_open":false}`，仅在直接调用时出现）

**Stop 的 instruction**（仅 should=JUDGE 时非 null；NO 时为 null）：

```
压缩候选命中（{reason}）。立即调用 condense_judge 提交判定，不要询问用户；判定通过后立即执行压缩流程（condense_begin → 填写摘要 → condense_commit），全程不要询问用户。
```

### GET /check?session_id=xxx

bump_turn（turns 与 turns_since_condense +1）→ 返回判定结果对象。会话不存在 → 400。

### POST /begin

请求：`{"session_id"!, "cwd"!}`（会话不存在则创建；幂等）
行为：置 `condensing=true`，并决定本次**摘要模式**（记入会话 `begin_mode`，`/commit` 据此打 delta 标记）
响应 200：

```json
{"mode": "delta",
 "template": "# 会话快照 · {project} · {ts} · 触发:{reason}\n\n## 目标（...）...",
 "guidance": "本次为增量摘要：只写自上一份快照以来的变化，要求：...",
 "required_sections": ["## 目标", "## 已完成", "## 未完成", "## 文件变更清单", "## 恢复指针"],
 "previous_digest": "（上一份快照全文，仅 mode=delta 时携带；读取失败为 null）"}
```

- `mode` 取值：`delta_mode=true`（默认）且项目已有快照且增量份数未攒满 `delta_merge_every` → `"delta"`；
  否则 `"full"`。攒满 `delta_merge_every`（默认 5）份增量后的 begin 为「全量合并」点，guidance 会
  额外说明「把此前增量整合成完整状态」
- `delta` 指引语义：必填章节标题原样保留；「已完成」「关键决策与理由」「踩过的坑」「文件变更清单」
  只追加新条目（旧条目不用重写）；「未完成/进行中」与「恢复指针」整节重写为当前最新状态；总长仍 ≥200 字符
- 模板占位符仅 `{project} {ts} {reason}`；reason 取会话 `last_trigger`（无则 `judge`）。模板固定章节：目标 / 关键决策与理由 / 已完成 / 未完成进行中 / 文件变更清单 / 踩过的坑与已排除的方向 / 恢复指针。

### POST /commit

请求：`{"session_id"!, "cwd"!, "digest_md"!, "trigger"?, "project_name"?}`

- 摘要校验失败 → 400 `{"errors": ["缺少必填章节：## 已完成", "总长度不足 200 字符（当前 87）"]}`
  - 校验规则：五个必填章节必须出现 + `len(digest_md) >= 200`
- 成功流程：save_snapshot（写快照 + 更新 index，delta 模式条目打 `delta:true`）→ SME 去重 + 分段画像写入（见下；SME 不在线兜底整篇，均不阻塞）→ 清除计划中 completed → mark_condensed（进冷却、turns_since_condense=0、清空延迟窗口与退避计数、消费 begin_mode）→ 追加 commit 事件
- `trigger` 缺省取会话 `last_trigger`，再缺省 `judge`；`project_name` 缺省取 `Path(cwd).name`
- 摘要模式由 `/begin` 决定并暂存在会话上（`delta_mode` 中途关闭则按全量落盘）；项目级增量计数
  （`snapshots/{hash}/delta-state.json`）：delta 提交 +1，全量提交归 0

响应 200：

```json
{"snapshot_path": "C:\\...\\20261004-223056-plan_done.md",
 "mode": "delta",
 "sme": {"stored": true, "memory_id": null, "error": null},
 "sme_segments": {"stored": 9, "failed": 0, "memory_ids": [], "errors": []},
 "segments_written": 9,
 "segments_reinforced": 2,
 "next_instruction": "快照已保存。请执行原生压缩（/compact 或 /clear）真正释放上下文；压缩后新会话开始时会自动恢复快照，无需手动操作。"}
```

- `mode`：本次摘要模式 `"delta"|"full"`；`segments_written`：新入库段数；`segments_reinforced`：查重命中并被强化的旧段条数
- `sme` 是聚合契约（分段成功时 `stored=true`、`memory_id`=首个成功段 id）；分段明细在 `sme_segments`，条数统计在 `segments_written`
- **批量入库时 `sme.memory_id=null`**：≥2 段走 `POST /memories/batch` 一次往返，该端点不返回各条 id
  （`memory_ids=[]`）；索引 `sme_memory_id` 仅在拿到具体 id 时才回填，null 不代表失败
- SME 停用 / 不在线 / 分段全失败 → 兜底整篇写入（`segments_written=0`、`sme_segments=null` 或全失败统计）；
  查重把段全部强化时不兜底（避免同内容整篇重复入库）；整篇也失败 → `sme.stored=false`，本地快照不受影响

**分段画像（split_digest）**：`sme.split_segments=true`（默认）时，commit 把摘要按章节拆成语义段，
先逐段查重（`sme.dedup=true` 时，见「SME 桥接」），再把剩余新段批量入库（≥2 段走 `POST /memories/batch`，
失败回落逐条 `POST /memories`），每段独立 tags 与 importance，可被 Ebbinghaus 强化逐条命中：

| 章节（标题前缀匹配） | type | 追加 tag | importance | 拆分方式 |
| --- | --- | --- | --- | --- |
| `## 目标` | goal | session_goal | 0.65 | 整节一段 |
| `## 关键决策与理由` | decision | decision | 0.8 | 每个顶层 bullet 一段 |
| `## 已完成` | accomplishment | accomplishment | 0.7 | 每个顶层 bullet 一段 |
| `## 未完成` | todo | todo | 0.75 | 每个顶层 bullet 一段 |
| `## 文件变更清单` | file_change | file_change | 0.6 | ≤8 条逐条一段，>8 合并一段 |
| `## 踩过的坑与已排除的方向` | pitfall | pitfall | 0.8 | 每个顶层 bullet 一段 |
| `## 恢复指针` | restore_pointer | restore_pointer | 0.5 | 整节一段 |
| 未识别章节 / 散行 | misc | misc | 0.55 | 合并为一段 |
| 无任何章节头（烂格式兜底） | digest | session_digest | 0.7 | 整篇一段 |

- 每段 text 带 `[autocondense|{project}|{ts}|{trigger}]` 前缀（自包含可检索）；tags = `sme.tags` + 段 tag
- 段数超过 `sme.max_segments`（默认 15）时，按 importance 从低到高把溢出段合并成一个 misc 段
- 有章节头但全部内容为空 → 返回空列表，由服务整篇兜底

### GET /restore?cwd=xxx&query=yyy&top_n=3

响应 200：

```json
{"project": "myproj",
 "snapshots": [索引条目，最新在前，取 top_n 条（默认 restore_top_n=3）],
 "restore": {优先级恢复包，结构同 additional_context（含 reread_files / todo_open）},
 "next_actions": ["重读关键文件：src/a.py、tests/test_a.py", "继续未完成事项"],
 "sme_results": [{"id": "mem-1", "text": "...", "score": 0.9}]}
```

- `restore.text` 为优先级预算打包的恢复包（未完成/恢复指针最优先；有增量快照时先沿 delta 链合并再打包，见上文）
- `next_actions`：恢复后的建议动作，按序——`reread_files` 非空先「重读关键文件」（对齐 Claude Code
  「摘要 + 重读最近文件」思路），`todo_open=true` 再「继续未完成事项」；均为建议，调用方自行执行
- SME 检索词 = `query` 或（缺省时）`project`；只保留 tags 含 `session_digest` 的结果
- `cwd` 为必填 query 参数

### GET /status

```json
{"enabled": true,
 "judge_defer_turns": 5,
 "judge_defer_max_turns": 20,
 "sessions": [会话摘要对象（含 last_judge / defer_until_turn / defer_streak / begin_mode），按 last_seen 倒序],
 "config": {完整配置对象},
 "recent_events": [最近 20 条事件，时间正序]}
```

### GET /config · PUT /config

GET 返回完整配置对象（结构见下表）。

PUT：请求体为**部分配置对象**，只更新给出的键（与现配置深合并），成功后原子保存到 `{数据目录}/config.json` 并就地生效（含冷却时长、enabled 开关等运行中行为）。

- 未知键 / 类型不符 → 400 `{"errors": ["未知配置项：xxx", ...]}`
- 响应 200：更新后的完整配置对象

## 配置项

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| port | int | 8761 | 监听端口；env `AUTOCONDENSE_PORT` 覆盖默认值（优先级：默认 < env < config.json） |
| host | str | "127.0.0.1" | 监听地址 |
| enabled | bool | true | 总开关：true=全自动压缩（零提示零确认）；false=完全不干预 |
| cooldown_minutes | int | 30 | 压缩冷却分钟数 |
| turns_threshold | int | 25 | 轮次候选阈值 |
| water_threshold | float | 0.7 | 水位候选阈值 |
| usable_context_tokens | int | 160000 | 可用上下文窗口 |
| task_min_rounds | int | 5 | 长任务完成的跨度阈值（轮） |
| judge_defer_turns | int | 5 | 判定 DEFERRED 后的基础延迟轮数（指数退避基数） |
| judge_defer_max_turns | int | 20 | 指数退避上限：`min(judge_defer_turns × 2^defer_streak, judge_defer_max_turns)`（5→10→20 封顶） |
| restore_max_chars | int | 8000 | 恢复包优先级打包的字符预算 |
| restore_top_n | int | 3 | 恢复时取最近 N 份快照 |
| restore_reread_files | int | 5 | 恢复包建议重读文件清单条数（从文件变更清单解析） |
| delta_mode | bool | true | 增量摘要：项目已有快照时 /begin 默认要增量，攒满 delta_merge_every 份强制全量合并 |
| delta_merge_every | int | 5 | 每累积 N 份增量后强制一次全量（恢复时 delta 链合并的深度上限同为该值） |
| idle_signal_rounds | int | 3 | idle 候选阈值：连续 N 轮无文件修改与工具失败 → 疑似任务停歇（活动记录缺失不触发） |
| sme.enabled | bool | true | 是否启用 SME 桥接 |
| sme.base_url | str | "http://127.0.0.1:8000" | SME 地址（默认对齐 SME 用户级配置 8000） |
| sme.timeout_s | float | 2.0 | SME 请求超时（秒） |
| sme.tags | list | ["project","todo","session_digest"] | 入库基础 tags（分段再加段 tag） |
| sme.importance | float | 0.7 | 整篇兜底入库重要度（分段用各段独立值） |
| sme.split_segments | bool | true | 摘要是否分段画像写入；false=整篇写入 |
| sme.max_segments | int | 15 | 分段条数上限（超出按重要度合并溢出段） |
| sme.dedup | bool | true | 入库前去重：命中同项目同类型高分旧段时强化旧段（POST /memories/{id}/hit）而非重复入库 |
| sme.dedup_threshold | float | 0.92 | 去重命中阈值：检索 score ≥ 该值且 project/segment_type 匹配才算重复 |

配置文件损坏：改名 `config.json.corrupt-{YYYYmmdd-HHMMSS}` 保留现场，回落默认值，服务照常启动。老配置遗留的 `mode` 键按未知键忽略（加载告警，不崩溃）。

## 数据目录布局

解析优先级：env `AUTOCONDENSE_DATA_DIR` > 用户级 `~/.autocondense/`（与代码位置解耦，插件缓存目录变化不影响数据）。

```
~/.autocondense/   （或 AUTOCONDENSE_DATA_DIR 指定目录）
├─ config.json                                  # 配置（首次 main() 启动时生成默认）
├─ sessions.jsonl                               # 事件流水，每行 {ts, event, session_id, cwd, data}
├─ sessions-state.json                          # 全部会话状态 {version, saved_at, sessions: {sid: {...}}}
├─ snapshots/
│  └─ {project_hash}/                           # project_hash = sha1(normcase(resolve(cwd)))[:10]
│     ├─ index.json                             # [索引条目]，按 ts 倒序（ISO 毫秒精度）
│     ├─ delta-state.json                       # {"delta_since_full": N}：距上一份全量快照已累积的增量份数（delta 提交 +1，全量提交归 0；缺失/损坏按 0）
│     └─ {YYYYmmdd-HHMMSS}-{trigger}.md         # 快照正文（同秒同名追加 -1/-2 后缀）
├─ mcp-session-map.json                         # 接入层运行时产物：{project_hash → session_id}（mcp_server 维护，原子写）
├─ service.pid                                  # 常驻服务自报身份 {pid, version, port, started}（main() 启动写入，退出清理；版本接管依据）
├─ hook-latch/{session_id}.json                 # 接入层运行时产物：Stop block 闩锁 {last_block_ts}（ac_hook 维护）
└─ *.corrupt-{YYYYmmdd-HHMMSS}                  # 坏文件现场备份（出现即代表发生过损坏容忍）
```

所有写入均为原子写（tmp + os.replace）。`mark_condensed` 的 `cooldown_until = 压缩时刻 + 当前 cooldown_minutes`（改配置不回溯已存在的冷却）。

## 事件类型表（sessions.jsonl 的 event 值）

| event | 触发点 | data 内容 |
| --- | --- | --- |
| session_start | POST /session/start | `{"source": "api"}` |
| session_start | hook SessionStart | `{"source": <hook 的 source>}` |
| plan_update | POST /report 或 hook TodoWrite | `{"items": 条目数, "via": "report" \| "hook:TodoWrite"}` |
| file_activity | hook PostToolUse（Edit/Write/MultiEdit/NotebookEdit） | `{"tool_name": ...}` |
| tool_failure | hook PostToolUseFailure | `{"tool_name": ...}` |
| stop | hook Stop | `{}` |
| judge | POST /judge | `{"result": "APPROVED"\|"DEFERRED", "urgency", "impact", "task_finished", "safe_to_compress", "confidence"?}` |
| pre_compact | hook PreCompact | `{"source": ...}` |
| condense | save_snapshot（commit 内） | `{"trigger", "file", "char_count", "delta"}` |
| begin | POST /begin | `{"trigger": 模板使用的 reason, "mode": "delta"\|"full"}` |
| commit | POST /commit 成功 | `{"trigger", "file", "mode", "sme_stored", "segments_written", "segments_reinforced"}` |
| restore | GET /restore 或 hook SessionStart(compact/clear/resume) | `{"query", "snapshots"}` 或 `{"via", "source", "snapshots"}` |

## SME 桥接（服务内部行为，供排障参考）

- 所有 HTTP 走 `httpx` 且 `trust_env=False`（系统代理会劫持 localhost）
- `health()`：GET /health，200 即在线；结果缓存 60 秒（commit 分段前预检，SME 不在线时跳过逐段尝试，避免 max_segments 次独立超时拖慢 commit）
- `dedupe_segments`（入库前去重，`sme.dedup=true` 时）：逐段 POST /memories/search（取段文本前 160 字，
  top_k=3），命中条件 = score ≥ `sme.dedup_threshold` 且 `metadata.project == project` 且
  `metadata.segment_type == 段类型`；命中 → `POST /memories/{id}/hit` 强化旧段（Ebbinghaus）并跳过入库，
  强化失败按「无重复」处理（宁重不漏）。整个去重阶段有 ~10s 软预算，超时后剩余段跳过查重直接入库，绝不阻塞提交
- `store_segments`：≥2 段先试 `POST /memories/batch`（body `{"memories": [...]}`，一次往返；
  响应 `{"added", "dropped", "total"}` **不返回各条 id**，故 memory_ids 恒为空、commit 的
  `sme.memory_id` 为 null；dropped>0 只在 errors 注明不算失败）；batch 任何异常/非 2xx → 回落
  逐条 POST /memories（逐条才有 id）；单段失败记录并继续，返回 `{stored, failed, memory_ids, errors}`
- 单段 body：`{"text": "[autocondense|{project}|{ts}|{trigger}] "+段文本, "tags": sme.tags+段tag, "importance": 段重要度, "source": "session", "metadata": {"tool":"autocondense", "segment_type", "segment_index", "session_id", "project", "trigger"}}`
- `store_digest`（整篇兜底）：POST /memories，body `{"text": "[会话快照|{project}|{trigger}]\n"+digest_md, "tags": sme.tags, "importance": sme.importance, ...}`；取响应 `id` 作为 memory_id
- `search`：POST /memories/search `{"text": query, "top_k": n}`；客户端只保留结果项 tags 含 `session_digest` 的，映射为 `{id, text, score}`
- 任何异常都不抛给调用方，只降级 + 日志 warning（logger：`autocondense`）

## 典型接入流程（hooks 视角）

1. SessionStart（source=compact/clear/resume）→ 把响应 `additional_context.text`（或按 note 提示 Read `full_file`）注入 additionalContext；`reread_files` 非空时建议宿主/智能体重读这些关键文件
2. PostToolUse(TodoWrite|Edit|Write) / PostToolUseFailure / Stop → 读判定结果；`should=JUDGE` 时按 `action_hint`/`instruction` 立即提交大模型判定（POST /judge），不询问用户（服务端按 tool_name 精确分类：TodoWrite 记计划，文件工具记活动轮，失败记活动轮）
3. `POST /judge` 返回 COMPRESS_NOW → `POST /begin` 取模板（注意响应 `mode`：delta 只写增量，full 全量/全量合并）→ 生成摘要 → `POST /commit` 提交 → 按 `next_instruction` 执行/提示 `/compact` 或 `/clear`；返回 DEFERRED → 无动作（指数退避后自动重新评估）
4. PreCompact → 服务自动进冷却，无需额外处理
