# autocondense

**Zero-prompt context compression for AI coding agents — LLM-judged timing, incremental session snapshots, and per-fact long-term memory. MCP-native, works with Claude Code, Codex, Cursor & ZCode.**

English | [中文](README.zh-CN.md)

## Why

Every agent ships auto-compact: it fires at a hard token threshold and hopes the summary lands well — usually mid-task. autocondense takes a different route:

- **The agent decides when to compress.** Rule signals (plan closed, long task finished, water level, idle streak) only *propose* a candidate. The agent itself answers a 5-field judgment form — *is the task finished? what's the impact of compressing right now? is anything unsaved?* — and only then is a snapshot written. The best moment to compress is a task boundary; no threshold knows where it is, the agent does.
- **Snapshots are structured and incremental.** A fixed template (goals / done / pending / decisions / pitfalls / file changes / restore pointers). Delta-only by default with a full merge every 5 snapshots — countering the well-known quality decay of repeated compaction.
- **Memory, not a blob.** Each snapshot is split into per-fact segments — every decision, pitfall, todo and accomplishment stored separately with its own tags and importance — so retrieval and reinforcement (Ebbinghaus-style) hit individual facts, and everything survives across sessions via an optional local memory backend.
- **Restoration is priority-packed.** After `/compact` the agent gets pending items and restore pointers first, plus a short list of key files to re-read — not a wall of truncated text.
- **Fully automatic, or fully off.** `enabled=true` means zero prompts and zero confirmations; `enabled=false` means the plugin is completely inert. There is no "should I compress?" chatter, ever.
- **Local & private.** A resident service bound to `127.0.0.1` only. No cloud, no API keys, no telemetry.

## How it works

```
rules propose a candidate ──▶ agent self-judges (5-field form) ──▶ verdict
                                     │                                │
                              DEFERRED (backoff                COMPRESS_NOW
                              5→10→20 turns)                          │
                                     │                          write snapshot
                                  keep working                    (delta mode)
                                                                    │
                                              after /compact ◀───────┤
                                                     │                └▶ per-fact
                                            priority restore pack  ──▶ memory segments
                                            + re-read file hints
```

Judgment matrix: **normal** requires *task finished AND low impact AND safe*; **urgent** (water level ≥85%) only requires *safe AND impact ≠ high* — when the host is about to lossily compact anyway, archiving first is a net win even mid-task.

## Architecture

```
┌────────────────────────────── host agent ───────────────────────────────┐
│  ZCode / Claude Code / Codex / Cursor                                    │
│    │ MCP (stdio)                              │ hooks (stdin→stdout)    │
│    ▼                                          ▼                         │
│  mcp_server.py (thin proxy, 7 tools,     ac_hook.py (event forwarder,   │
│  lazy service spawn, cwd→session map)     Stop can block, latch-guarded,│
│                                            SessionStart injects restore)│
└──────────┬─────────────────────────────────────┬───────────────────────┘
           │ HTTP 127.0.0.1:8761  (trust_env=False)
           ▼                                     │
┌───────────────────────────────────────────────▼─────────────────────────┐
│  autocondense.service  (FastAPI, the single writer)                      │
│    /session/start /report /check /judge /begin /commit /restore          │
│    /status /config /events/hook                                           │
│    signals · state · digest · sme_bridge · config                        │
└──────────────┬─────────────────────────────────────────┬────────────────┘
               ▼                                         ▼
   ~/.autocondense/ (atomic writes:         optional memory backend
   config, snapshots + index, sessions,     (local HTTP API, degrades
   delta-state, service.pid)                gracefully when offline)
```

Design notes: [docs/设计.md](docs/设计.md) · Full REST contract: [docs/CONTRACT.md](docs/CONTRACT.md) (Chinese).

## Install

Requirements: Python 3.10+, `pip install -r requirements.txt`.

**ZCode (desktop, recommended)** — via the plugin marketplace (the desktop app does not read `~/.zcode/cli/config.json`):

1. Marketplace → Add → Add Plugin Marketplace → paste the `plugins/` directory of this repo
2. Personal → **autocondense** → Install
3. New conversation: MCP auto-connects, all four hooks register, skill + `/condense` command ship with the plugin

**Other hosts (Claude Code / Codex / Cursor, and ZCode CLI)** — installer, dry-run by default:

```bash
python installer.py                # preview diffs per host
python installer.py --hosts claude,codex,cursor --yes
python installer.py --uninstall --yes   # removes only our entries
```

Manual configs for every host: [docs/接入.md](docs/接入.md) (Chinese).

**Verify:**

```bash
python -m pytest -q                          # 187 tests
python -m autocondense.service --port 8761 & curl http://127.0.0.1:8761/health
```

## The seven MCP tools

| Tool | When | What |
| --- | --- | --- |
| `condense_restore` | session start / after compaction | priority restore pack + memory hits + next actions |
| `condense_report` / `condense_check` | after each task batch / turn | report plan items, get candidate verdict |
| `condense_judge` | when `should=JUDGE` | submit the 5-field judgment form (agent self-assessment) |
| `condense_begin` / `condense_commit` | after COMPRESS_NOW | get template (delta by default) → submit snapshot |
| `condense_status` | debugging | service / sessions / config overview |

## Judgment pipeline (rules, in order)

1. in-progress tasks > 0 → **NO** (task continuity first)
2. cooldown → **NO**
3. all plan items completed → candidate / `plan_done`
4. task spanning ≥5 turns completed → candidate / `task_done`
5. water level ≥70% (host-reported tokens preferred) → candidate / `water_level`
6. ≥3 turns without file edits or failures → candidate / `idle`
7. ≥25 turns since last compress → candidate / `turns`

## Configuration

`~/.autocondense/config.json` (hot-reload via `PUT /config`); key items:

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | fully automatic vs completely inert |
| `cooldown_minutes` / `turns_threshold` / `water_threshold` | 30 / 25 / 0.7 | candidate thresholds |
| `usable_context_tokens` | 160000 | context window for water level |
| `judge_defer_turns` / `judge_defer_max_turns` | 5 / 20 | deferred-judgment backoff cap |
| `restore_max_chars` / `restore_reread_files` | 8000 / 5 | restore pack budget & re-read list |
| `delta_mode` / `delta_merge_every` | true / 5 | incremental snapshots & full-merge cycle |
| `idle_signal_rounds` | 3 | idle candidate threshold |
| `sme.*` | — | memory backend: url, segmentation, dedup threshold, batch size |

## FAQ

- **SME/memory backend offline?** Everything degrades: snapshots still land on local disk, restore skips memory hits, judgment is unaffected. Never blocks.
- **Port 8761 occupied?** Start with `--port 8762`, set `AUTOCONDENSE_PORT` (MCP) and `AUTOCONDENSE_BASE_URL` (hooks).
- **Disable temporarily?** `PUT /config {"enabled": false}` — instantly inert, or just disable the plugin in the host.
- **Plugin upgrade leaves a stale service?** The MCP proxy health-checks the version and takes over the old process automatically via `service.pid` — never kills blindly.

## License

Apache-2.0
