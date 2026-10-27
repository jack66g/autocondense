---
name: autocondense
description: 会话上下文自动压缩。当一批任务全部完成、上下文明显变大、距上次压缩轮次很多，或用户要求压缩/归档/恢复会话上下文时使用。通过 7 个 MCP 工具检查压缩时机、提交大模型判定、生成会话快照并恢复；开=全自动（零提示零确认），关=完全不干预；摘要按分段画像写入 SME 记忆长期沉淀。
---

# autocondense：会话上下文自动压缩

把冗长的会话历史在恰当时机压缩成一份结构化「会话快照」，原生压缩（/compact、/clear）后自动恢复，
过程细节归档、结论保留。常驻服务（默认 `http://127.0.0.1:8761`）负责判定与落盘，本技能的工具都是薄代理。
判定链路全自动：规则命中「压缩候选」→ 模型自评一张轻量判定表 → 通过则直接压缩、不通过则自动延迟。

## 七个工具与使用纪律

按会话生命周期排列：

1. **会话开始 / 上下文被压缩或清空后** → 立即 `condense_restore(query?, cwd, top_n=3)`
   恢复该项目的最近快照（含 SME 记忆检索）。恢复内容按优先级打包成「恢复包」：
   未完成、恢复指针最优先，其次决策/坑/目标/已完成；同时带 `next_actions` 建议动作
   （先重读 `reread_files` 建议清单里的关键文件，再继续未完成事项）。返回内容直接作为
   上下文继续工作；若提示「已截断」，用 Read 工具读取给出的完整快照文件。
2. **日常工作中**（每完成一批任务 / 更新了计划 / 每轮对话结束）→
   - 用了计划/Todo 工具或有明确任务清单时：`condense_report(plan_items, cwd)` 上报
     `[{"title":"...","status":"pending|in_progress|completed"}]`（紧凑 JSON 字符串）；
   - 否则 `condense_check(cwd)` 检查一次。
3. **返回 should=JUDGE 时**（工具会带回 reason/信号、判定表字段与 action_hint）→
   **立即自己填写判定表并调用 `condense_judge(cwd, task_finished, impact, safe_to_compress,
   impact_reason?, reason?)`**。这是模型自评，不要询问用户，如实回答三个问题：
   - `task_finished`：当前批次任务是否真的收尾（计划清空/目标达成）？
   - `impact`：现在压缩对当前工作的影响，low / medium / high 三选一；
   - `safe_to_compress`：有没有未落盘、压缩后容易丢失的关键信息？
   - 可选 `confidence`（high/medium/low）标注本次判定的置信度；服务端响应会带回
     `rounds_to_full`（估算还有多少轮水位到顶），帮你判断紧迫度。
4. **根据 condense_judge 的返回行动**：
   - `COMPRESS_NOW` → **直接执行压缩流程，全程无需任何人确认**：
     `condense_begin(cwd)` 取回模板与填写指引 → 按模板逐节填写摘要（见下），整份 markdown
     作为 `digest_md` → `condense_commit(digest_md, trigger?, cwd)` 提交。校验失败会返回中文
     错误清单，按清单修正后重新提交（必填章节标题必须原样保留）；成功后按返回的
     `next_instruction` 行动——通常是**提示用户执行 /compact 或 /clear** 真正释放上下文
     （原生压缩后新会话会自动恢复快照，无需手动搬运）。
   - `DEFERRED` → 什么都不做，继续当前工作。服务端已自动设置延迟窗口（指数退避：
     5 → 10 → 20 轮封顶，`judge_defer_turns`/`judge_defer_max_turns` 可配），到期会
     重新评估，无需自己记时间。
5. **排障/查看配置** → `condense_status()`（含 enabled 开关、延迟窗口与各会话最近判定）。

**cwd 永远传你当前的工作目录**（首次调用会用它自动建立会话映射；显式 session_id 可选，一般不用传）。

## 摘要（digest）模板要点

模板由 `condense_begin` 返回，必填章节：`## 目标`、`## 已完成`、`## 未完成`、`## 文件变更清单`、`## 恢复指针`
（另有「关键决策与理由」「踩过的坑与已排除的方向」强烈建议填写）。总长度 ≥200 字符。

- 增量模式：项目已有快照时 begin 默认返回 `mode:"delta"` 并附 `previous_digest`（上一份快照全文），
  此时只写自上一份快照以来的**变化**（已完成/决策/坑/文件清单只追加新条目，未完成与恢复指针整节重写）；
  每攒满 `delta_merge_every`（默认 5）次增量，begin 会自动改为全量合并，无需自己记次数。
- 「已完成」每项注明验证方式（测试通过/编译通过/用户确认）；
- 「未完成/进行中」精确到下一个动作，无则写「（无）」；
- 「文件变更清单」写关键改动 file:line（恢复包会从中挑建议重读的文件）；
- 写结论不写过程——恢复后靠它直接续命，不靠翻 transcript。

## 开关语义（enabled）

| enabled | 行为 |
| --- | --- |
| `true`（默认） | 全自动：候选 → 大模型判定 → 直接压缩，零提示、零确认 |
| `false` | 完全不干预：一切判定返回 NO，不产生任何压缩动作 |

- 对应宿主里插件的**启用/禁用按钮**：禁用插件即关闭自动压缩。
- 运行时可通过 `PUT /config` 修改（如 `{"enabled": false}`），立即生效，`condense_status()` 可核对。
- 用户主动执行 `/condense` 命令不受此开关影响判定流程（用户意图明确，直接走压缩）。

## 与 SME 记忆插件的联动：分段画像

`condense_commit` 不再把快照整篇入库，而是自动按章节拆成**多条语义段**逐条写入 SME：
每条已完成事项、待办、决策、踩坑、文件变更都独立成段，各带独立 tags
（goal/accomplishment/todo/decision/pitfall/file_change 等）与 importance（0.5-0.8），
因此可被 Ebbinghaus 强化机制**逐条**命中与强化，而不是只有一整篇快照可检索。
`condense_restore` 会附带 SME 检索结果。入库前自动查重：相似段（同项目同类型且相似度达阈值）
会强化已有记忆而非重复新增。SME 不在线时兜底整篇写入或跳过，绝不阻塞——
快照仍落本地磁盘，判定不受任何影响。

## 判定信号速查（候选制）

规则只产生「压缩候选」（should=JUDGE），是否真压由 `condense_judge` 的判定矩阵决定：
**normal 档需 任务完成 且 影响 low 且 安全；urgent 档（水位 ≥85%）只需 安全 且 影响≠high**（水位危急时先存档，任务未收尾也可压）。

候选信号：进行中任务 >0 绝不进候选；冷却期（默认 30 分钟）不进；计划全部完成 → plan_done；
跨度 ≥5 轮的任务完成 → task_done；水位 ≥70% → water_level；连续 idle_signal_rounds（默认 3）轮
无文件修改与失败 → idle（疑似任务停歇）；距上次压缩 ≥25 轮 → turns。
水位用智能体上报的真实 context_tokens 优先（其次 transcript 估算）。判定不通过自动延迟
（指数退避：5 → 10 → 20 轮封顶）后重新评估；urgent 候选可无视延迟窗口。
