# autocondense

AI 编程智能体（ZCode / Claude Code / Codex / Cursor）的**会话上下文自动压缩**系统：
在恰当时机（计划闭环 / 长任务完成 / 上下文水位高 / 连续 idle / 轮次多）把冗长会话压缩成一份结构化
「会话快照」（支持**增量 delta 摘要**，攒满自动全量合并），原生压缩（/compact、/clear）后按优先级
恢复包自动恢复（未完成/恢复指针优先，附建议重读文件清单）——过程细节归档，结论与恢复指针保留。
开箱全自动（enabled=true 零提示零确认，enabled=false 完全不干预）；候选命中先过一张大模型
轻量判定表再压缩。可选联动 SME 记忆插件，把快照按**分段画像**沉淀为可逐条强化的长期记忆
（批量入库、入库前查重强化相似段）。

- 插件源目录：`plugins/autocondense/`（`.zcode-plugin/plugin.json` + Python 包 + hooks + skills + 命令）
- 常驻服务：`python -m autocondense.service`（默认 `http://127.0.0.1:8761`，仅监听本机回环）
- MCP 服务器：`python plugins/autocondense/autocondense/mcp_server.py`（stdio，自动拉起常驻服务）
- 钩子：`python plugins/autocondense/autocondense/hooks/ac_hook.py <Event>`（PostToolUse / Stop / SessionStart；PostToolUseFailure 仅 ZCode；PreCompact 仅 Claude Code）
- 数据目录：`~/.autocondense/`（与代码位置解耦，插件更新不丢数据）
- REST 契约：[docs/CONTRACT.md](docs/CONTRACT.md)（接入层开发以此为准）

## 架构

```
┌──────────────────────────────── 宿主智能体 ────────────────────────────────┐
│  ZCode / Claude Code / Codex / Cursor                                      │
│    │ MCP (stdio)                          │ hooks (stdin JSON → stdout)   │
│    ▼                                      ▼                               │
│  autocondense/mcp_server.py      autocondense/hooks/ac_hook.py            │
│  （薄代理：7 个 condense_* 工具，  （薄代理：转发事件，Stop 可 block，        │
│    惰性拉起服务，cwd→会话映射）      SessionStart 注入恢复上下文，闩锁防死循环）│
└──────────┬───────────────────────────────────┬──────────────────────────────┘
           │ HTTP 127.0.0.1:8761（trust_env=False）
           ▼                                   │
┌──────────────────────────────────────────────▼──────────────────────────────┐
│  autocondense.service（FastAPI 常驻，唯一数据落盘方）                          │
│    /session/start /report /check /judge /begin /commit /restore /status /config │
│    /events/hook（PostToolUse-TodoWrite/Edit/Write / Stop / SessionStart / PostToolUseFailure / PreCompact）│
│  ┌────────────┐ ┌────────────┐ ┌──────────┐ ┌───────────┐ ┌──────────────┐  │
│  │ signals    │ │ state      │ │ digest   │ │ sme_bridge│ │ config       │  │
│  │ 候选判定    │ │ 会话/快照/  │ │ 模板/校验 │ │ 分段画像   │ │ env>json 深合 │  │
│  │ 大模型判定表 │ │ 事件流水    │ │ 恢复载荷  │ │ 失败降级   │ │ 并默认原子写  │  │
│  └────────────┘ └────────────┘ └──────────┘ └───────────┘ └──────────────┘  │
└──────────────┬──────────────────────────────────────────┬───────────────────┘
               ▼                                          ▼
        ~/.autocondense/（原子写：config.json、      SME 记忆插件（127.0.0.1:8000，
        sessions-state.json、sessions.jsonl、       记忆入库/检索，不在线只降级）
        snapshots/{项目hash}/*.md + index.json）
```

设计细节见 [docs/设计.md](docs/设计.md)。

## 快速开始

```bash
pip install -r requirements.txt   # 或 pip install -e .
pytest -q                          # 187 个测试

# ZCode 桌面端（推荐）：走插件市场——桌面端不读 ~/.zcode/cli/config.json 的 mcp/hooks
#   插件市场 → 添加 → 添加插件市场 → 粘贴：plugins/ 目录 → 个人 → autocondense 会话压缩 → 安装
#   （插件自带 MCP + hooks + 技能 + /condense 命令，MCP 自动连接、hook runner 自动启用）
#   已装过要升级：市场页齿轮 → 刷新该市场 → 个人 → autocondense → 更新；
#   更新后旧版常驻服务会被 MCP 代理按版本握手自动接管替换（service.pid），无需手动处理

# 其他宿主（Claude Code / Codex / Cursor，及 ZCode CLI）：运行安装器（默认 dry-run，--yes 落盘）
python installer.py                # 预览各宿主的改前→改后 diff
python installer.py --hosts claude,codex,cursor --yes
python installer.py --list         # 查看各宿主配置状态
python installer.py --uninstall --yes   # 摘除我们写入的条目

# 验证：起服务 / 钩子手动喂事件
python -m autocondense.service --port 8761 &
curl http://127.0.0.1:8761/health
echo '{"session_id":"t1","cwd":"C:/tmp","source":"compact"}' | \
  python plugins/autocondense/autocondense/hooks/ac_hook.py SessionStart
```

安装器写入位置（**zcode 条目仅对 ZCode CLI 生效，桌面端请走插件市场**）：
claude `~/.claude.json`（mcpServers）+ `~/.claude/settings.json`（hooks）、
codex `~/.codex/config.toml`（[mcp_servers.autocondense]）、cursor `~/.cursor/mcp.json`、
zcode-cli `~/.zcode/cli/config.json`。
MCP 工具的完整使用纪律见 [plugins/autocondense/skills/autocondense/SKILL.md](plugins/autocondense/skills/autocondense/SKILL.md)。

## 配置项

`~/.autocondense/config.json`（PUT /config 深合并热更新；env `AUTOCONDENSE_DATA_DIR` 换数据目录，
`AUTOCONDENSE_PORT` 换默认端口，`AUTOCONDENSE_BASE_URL` 覆盖钩子的服务地址）：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| port / host | 8761 / 127.0.0.1 | 常驻服务监听 |
| enabled | true | true=全自动压缩（零提示零确认）；false=完全不干预（等同禁用插件） |
| cooldown_minutes | 30 | 压缩后冷却（防重复压） |
| turns_threshold | 25 | 轮次候选阈值 |
| water_threshold | 0.7 | 上下文水位候选阈值（est/可用窗口；≥0.85 进入 urgent 档） |
| usable_context_tokens | 160000 | 可用上下文窗口 |
| task_min_rounds | 5 | 长任务完成的跨度阈值（轮） |
| judge_defer_turns / judge_defer_max_turns | 5 / 20 | 大模型判定不通过后的延迟：指数退避 5→10→20 封顶 |
| restore_max_chars | 8000 | 恢复包优先级打包的字符预算（钩子侧再截 9000） |
| restore_top_n | 3 | 恢复取最近 N 份快照 |
| restore_reread_files | 5 | 恢复包建议重读文件清单条数 |
| delta_mode / delta_merge_every | true / 5 | 增量摘要：已有快照时只写增量，每 5 份强制全量合并 |
| idle_signal_rounds | 3 | idle 候选阈值：连续 N 轮无文件修改与失败 → 疑似任务停歇 |
| sme.enabled / base_url / timeout_s / tags / importance / split_segments / max_segments | true / 127.0.0.1:8000 / 2.0 / … / true / 15 | SME 桥接与分段画像（split_segments=false 整篇入库） |
| sme.dedup / sme.dedup_threshold | true / 0.92 | 入库前查重：命中同项目同类型高分旧段时强化旧段而非重复新增 |

## 判定链路（候选制 + 大模型判定表）

1. 有进行中任务 → **NO**（任务连续性优先）
2. 冷却期内 → **NO**
3. 计划全部完成 → **候选** / plan_done
4. 跨度 ≥task_min_rounds 的任务已完成 → **候选** / task_done
5. 水位 ≥water_threshold → **候选** / water_level（水位优先用 hook 上报的真实 context_tokens）
6. 连续 idle_signal_rounds 轮无文件修改与失败 → **候选** / idle（疑似任务停歇）
7. 距上次压缩 ≥turns_threshold 轮 → **候选** / turns
8. 否则 → **NO**

命中候选（should=JUDGE）→ 智能体立即自评判定表提交 `condense_judge`（不询问用户；可选附
confidence 置信度，响应带回 rounds_to_full 紧迫度估算）。
判定矩阵：**normal 档需 任务完成 且 影响 low 且 安全；urgent 档（水位 ≥85%）只需 安全 且
影响≠high**（任务未收尾也可压）。通过 → 直接 begin→commit 全自动压缩（begin 默认增量模式，
只写自上一份快照以来的变化）；不通过 → 指数退避（5→10→20 轮封顶）后重新评估。

## 常见问题

- **8761 被占用？** 换端口三处一致即可：`python -m autocondense.service --port 8762`，
  MCP 侧给 mcp_server 传 env `AUTOCONDENSE_PORT=8762`，钩子侧传 env `AUTOCONDENSE_BASE_URL=http://127.0.0.1:8762`。
- **SME 不在线会怎样？** 只降级：快照照常落本地磁盘，restore 不带 SME 结果，判定不受任何影响。
- **如何停用自动压缩？** `PUT /config` 设 `enabled=false`（立即生效，等同在宿主里禁用插件）；重新设 true 恢复。用户主动 `/condense` 不受影响。
- **MCP 宿主杀进程树会带走常驻服务吗？** Windows 下 mcp 客户端退出时可能连分离进程一起杀；
  无妨——下次任一工具调用会通过 `_ensure_service` 秒级重新拉起。
- **钩子会不会拖垮宿主？** 不会：3s 超时、任何异常静默 exit 0；Stop 的 block 有 15 分钟闩锁防死循环。
