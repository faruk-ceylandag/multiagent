# Multi-Agent System — Architecture Graph

This document maps the entire system. Claude MUST read and follow this graph when making changes.

## System Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                           start.py                                  │
│  Boot orchestrator: config → roles → ecosystem → hub → workers      │
└──────┬──────────┬──────────┬──────────┬──────────┬──────────────────┘
       │          │          │          │          │
       ▼          ▼          ▼          ▼          ▼
  lib/config  lib/roles  lib/memory  ecosystem/  hub/hub_server
  (load cfg)  (role.md)  (init mem)  (setup)     (FastAPI)
```

## Boot Sequence

```
start.py
  │
  ├─ 1. load_config(workspace)        ← multiagent.json or defaults
  ├─ 2. Inject 3 hidden reviewers     ← reviewer-logic/style/arch (haiku)
  ├─ 3. Port management               ← find free port if needed
  ├─ 4. scan_projects + detect_stack   ← populate stack.json
  ├─ 5. Init dirs                      ← MA_DIR/logs, sessions, hooks, memory
  ├─ 6. init_memory(MA_DIR, agents)
  ├─ 7. Save stack.json + config.json
  ├─ 8. generate_roles(...)            ← write {name}-role.md per agent
  ├─ 9. setup_shared_ecosystem(...)    ← shared subagents/commands/skills → MA_DIR/.claude/
  ├─ 10. setup_agent_ecosystem(...)    ← per-agent: symlinks + settings.json + .mcp.json
  ├─ 11. setup_workspace_claudemd(...) ← CLAUDE.md for projects without one
  ├─ 12. Copy hub/, agents/, dashboard/ to MA_DIR
  ├─ 13. Launch Hub                    ← uvicorn hub.hub_server:app
  ├─ 14. Launch Workers                ← agents.worker per agent (staggered)
  └─ 15. Watchdog loop (10s)
         ├─ check_new_agents()         ← config additions → spawn
         ├─ check_auto_scale()         ← scale_up recommendation → spawn worker-N
         └─ Process monitor            ← restart crashed workers (max 5)
```

## Hub Architecture

```
┌─────────────────────────── Hub (FastAPI) ───────────────────────────┐
│                                                                      │
│  hub/hub_server.py ── mounts all routers from hub/routers/           │
│                                                                      │
│  hub/state.py ── SINGLE SOURCE OF TRUTH                              │
│    ├─ ALL_AGENTS, HIDDEN_AGENTS, VISIBLE_AGENTS                      │
│    ├─ tasks: Dict[int, dict]          ← kanban board                 │
│    ├─ messages: Dict[str, List]       ← agent inboxes                │
│    ├─ agents: Dict[str, dict]         ← online presence              │
│    ├─ pipeline: Dict[str, dict]       ← working/idle/offline         │
│    ├─ sessions: Dict[str, dict]       ← claude session tracking      │
│    ├─ usage_log: Dict[str, dict]      ← token/cost per agent         │
│    ├─ task_reviews: Dict[str, dict]   ← reviewer verdicts            │
│    ├─ task_comments: Dict[str, list]  ← comment threads              │
│    ├─ pending_plans: Dict[int, dict]  ← architect plan proposals     │
│    ├─ pattern_registry: Dict          ← proven patterns (scored)     │
│    ├─ file_locks: Dict[str, dict]     ← concurrent edit protection   │
│    ├─ cache_registry: Dict            ← MCP content cache            │
│    ├─ _state_version                  ← monotonic counter            │
│    └─ get_dashboard_snapshot()        ← cached, lock-free            │
│                                                                      │
│  Background Threads:                                                 │
│    ├─ _save_timer          (10s)  ← hub_state.json persistence       │
│    ├─ _config_reload_timer (15s)  ← hot-reload multiagent.json       │
│    ├─ _lock_cleanup_timer  (120s) ← stale file lock removal          │
│    └─ _review_timeout_timer(60s)  ← 15min auto-approve reviews       │
│                                    ← 30min auto-dismiss plans        │
└──────────────────────────────────────────────────────────────────────┘
```

## Hub Routers

```
hub/routers/
  │
  ├─ agents.py       POST /poll/{name}, /agents/register, /agents/status
  │                   GET  /route, /classify-intent, /agents/profiles
  │                   POST /agents/{name}/stop, /agents/{name}/restart
  │
  ├─ tasks.py        POST /tasks, PUT /tasks/{tid}
  │                   GET  /tasks, /tasks/{tid}, /tasks/queue/{name}, /tasks/graph
  │                   POST /tasks/auto-assign/{name}
  │                   POST /tasks/{tid}/review     ← reviewer verdict
  │                   POST /tasks/{tid}/uat        ← user approve/reject
  │                   POST /tasks/{tid}/comments   ← add comment
  │                   GET  /tasks/{tid}/comments   ← list comments
  │                   POST /plan/approve, /plan/dismiss
  │                   Internal: _dispatch_code_review(), _dispatch_qa()
  │
  ├─ messages.py     GET  /messages/{name}
  │                   POST /messages, /broadcast
  │                   POST /sessions/update
  │
  ├─ websocket.py    WS   /ws                     ← dashboard real-time
  │                   GET  /ws/clients
  │
  ├─ git.py          GET  /git/branches, /git/log, /git/status
  │                   POST /git/commit, /git/push, /git/rollback
  │                   POST /files/lock, /files/unlock
  │                   POST /files/plan, /files/check-conflicts
  │                   POST /changes, GET /changes
  │
  ├─ logs.py         POST /logs/{name}/push
  │                   GET  /logs/{name}, /logs/{name}/stream (SSE)
  │
  ├─ costs.py        POST /costs/log
  │                   GET  /costs, /costs/estimate, /costs/budget
  │
  ├─ credentials.py  GET  /credentials, POST /credentials
  │                   GET  /services, POST /notifications/config
  │
  ├─ analytics.py    GET  /dashboard, /analytics, /export, /projects
  │                   GET  /autoscale/status, /metrics
  │
  ├─ health.py       GET  /health, /health/detailed, /health/diagnostics
  │                   POST /health/crash, GET /audit
  │
  ├─ patterns.py     POST /patterns, GET /patterns
  │                   POST /patterns/{pid}/vote
  │
  └─ cache.py        POST /cache, GET /cache/{key}, DELETE /cache/{key}
```

## Agent Worker Architecture

```
┌──────────────────── agents/worker.py ────────────────────────┐
│                                                               │
│  BOOT:                                                        │
│    hub health check → register → MCP setup → CLI check        │
│    → team roster → clear stale session → ONLINE                │
│                                                               │
│  MAIN LOOP (every 2-15s adaptive):                            │
│    POST /poll/{name}                                          │
│    ├─ stop signal? → terminate, rollback, idle                │
│    ├─ count=0? → auto-assign check (every 3rd idle)           │
│    └─ messages found:                                         │
│         ├─ filter: ack, heartbeat, session_reset              │
│         ├─ process ecosystem_updates                          │
│         ├─ deduplicate by content                             │
│         ├─ handle credentials                                 │
│         ├─ classify: is_task / is_chat / is_rework            │
│         ├─ detect task_id, project, branch                    │
│         ├─ fresh session if reviewer/qa/architect OR rework   │
│         ├─ MCP pre-flight (detect needed, check creds)        │
│         ├─ build prompt (role + context + patterns + MCP)     │
│         ├─ call_claude(prompt)                                │
│         ├─ post-task: verify_loop, git stage                  │
│         ├─ status transition:                                 │
│         │    dev → code_review                                │
│         │    architect → done (or in_progress if plan pending)│
│         │    reviewer → submit verdict to parent task         │
│         │    qa → uat (pass) or failed (fail)                 │
│         ├─ extract_learning, vote patterns                    │
│         └─ auto-assign next task                              │
│                                                               │
│  MODULES:                                                     │
│    context.py       AgentContext dataclass                     │
│    hub_client.py    HTTP to hub (post, get, msg, status)      │
│    claude_runner.py Claude CLI with streaming                  │
│    verify.py        Lint/test/build loop (up to 3 cycles)     │
│    git_ops.py       Branch, commit, rollback, PR              │
│    mcp_manager.py   .mcp.json generation, reload, ensure      │
│    learning.py      Learning, hooks, templates, hints          │
│    chat_handler.py  Background chat while working              │
│    credentials.py   Load/save credentials.env                  │
│    log_utils.py     Async log buffer → hub push                │
└───────────────────────────────────────────────────────────────┘
```

## Task Lifecycle (State Machine)

```
                    ┌──────────────────────────────────────────┐
                    │              TASK LIFECYCLE               │
                    └──────────────────────────────────────────┘

  ┌────────┐    auto-assign     ┌─────────────┐    verify ok    ┌─────────────┐
  │ to_do  │ ─────────────────► │ in_progress  │ ─────────────► │ code_review │
  └────────┘    or message      └─────────────┘                 └──────┬──────┘
       ▲                              ▲   ▲                            │
       │                              │   │                    ┌───────┴───────┐
       │                   rework     │   │                    │ 3 reviewers   │
       │              (review_feedback│   │                    │ dispatched    │
       │               or qa_feedback)│   │                    │ in parallel   │
       │                              │   │                    └───────┬───────┘
       │                              │   │                            │
       │                              │   │         ┌──────────────────┼──────────────┐
       │                              │   │         │                  │              │
       │                              │   │    ┌────┴────┐    ┌───────┴───┐   ┌──────┴──────┐
       │                              │   │    │reviewer- │    │reviewer-  │   │reviewer-    │
       │                              │   │    │logic     │    │style      │   │arch         │
       │                              │   │    └────┬────-┘    └───────┬───┘   └──────┬──────┘
       │                              │   │         │                  │              │
       │                              │   │         └──────────────────┼──────────────┘
       │                              │   │                            │
       │                              │   │              ┌─────────────┴────────────┐
       │                              │   │              │                          │
       │                              │   │         ALL approve              ANY request_changes
       │                              │   │              │                          │
       │                              │   │              ▼                          │
       │                              │   │       ┌─────────────┐                  │
       │                              │   └───────│ in_testing  │    rework ◄──────┘
       │                              │           └──────┬──────┘    (max 3 cycles
       │                              │                  │            then auto-approve)
       │                              │           ┌──────┴──────┐
       │                              │           │             │
       │                              │       QA pass       QA fail
       │                              │           │             │
       │                              │           ▼             │
       │                              │      ┌─────────┐       │
       │                              └──────│   uat   │       │
       │                             reject  └────┬────┘       │
       │                                          │            │
       │                                    user approve       │
       │                                          │            │
       │                                          ▼            │
       │                                     ┌────────┐        │
       │                                     │  done  │        │
       │                                     └────────┘        │
       │                                                       │
       │              ┌────────┐                               │
       └──────────────│ failed │ ◄─────────────────────────────┘
          retry       └────────┘    (or timeout, crash, agent give-up)
```

### Review Dispatch Flow (tasks.py)

```
Task status → code_review
  │
  _dispatch_code_review(tid):
  │
  ├─ Find reviewer agents from ALL_AGENTS (reviewer-logic, reviewer-style, reviewer-arch)
  ├─ Create 3 SUBTASK entries (hidden, _is_review_subtask=True)
  ├─ Send message to each reviewer with parent task diff/context
  ├─ Set task.review_dispatched_at = now
  └─ Set task._review_subtask_ids = [sub1, sub2, sub3]

  Reviewer completes:
  ├─ worker.py _parse_review_verdict() → extracts VERDICT + COMMENTS
  ├─ POST /tasks/{parent_tid}/review → {agent, verdict, comments}
  └─ tasks.py handles:
       ├─ All 3 approve → status = in_testing, _dispatch_qa(tid)
       └─ Any request_changes → status = in_progress + review_feedback msg to dev
           (_review_cycle++, max MAX_REWORK_LOOPS=3 then auto-approve)

  Timeout (15 min):
  └─ _review_timeout_timer auto-approves missing reviews → in_testing
```

### QA Dispatch Flow

```
Task status → in_testing
  │
  _dispatch_qa(tid):
  ├─ Send message to QA agent with task context
  └─ QA agent runs tests, reports result

  QA result:
  ├─ Pass → status = uat (user approval)
  └─ Fail → status = in_progress + qa_feedback msg to dev
       (_qa_cycle++, max MAX_REWORK_ITERATIONS=5 then failed)
```

## Message Flow

```
┌──────────┐         ┌──────────┐         ┌──────────┐
│   User   │         │   Hub    │         │  Agents  │
│(Dashboard│         │ (FastAPI)│         │ (Workers)│
└────┬─────┘         └────┬─────┘         └────┬─────┘
     │                     │                    │
     │  WebSocket /ws      │                    │
     │◄═══════════════════►│                    │
     │  (dashboard data)   │                    │
     │                     │                    │
     │  POST /messages     │                    │
     │────────────────────►│                    │
     │  (send task to      │  POST /poll/{name} │
     │   agent)            │◄───────────────────│  (every 2-15s)
     │                     │  {count, stop}     │
     │                     │───────────────────►│
     │                     │                    │
     │                     │  GET /messages/{n} │
     │                     │◄───────────────────│  (if count > 0)
     │                     │  [msg1, msg2, ...] │
     │                     │───────────────────►│
     │                     │                    │
     │                     │  POST /messages    │
     │  (agent→user msg)   │◄───────────────────│  (progress, info)
     │◄════════════════════│                    │
     │  (via WS push)      │                    │
     │                     │                    │
     │  POST /tasks/{t}/uat│                    │
     │────────────────────►│                    │
     │  (approve/reject)   │  POST /messages    │
     │                     │───────────────────►│  (feedback to dev)
     │                     │                    │
```

### Message Types

| msg_type | Direction | Purpose |
|---|---|---|
| `task` | user/system → agent | Work assignment |
| `message` | agent ↔ agent | Inter-agent communication |
| `chat` | user → agent | Quick question |
| `info` | system → agent/user | Status notification |
| `blocker` | agent → user | Blocking issue (credentials, access) |
| `review_feedback` | system → dev | Code review change requests (triggers rework) |
| `qa_feedback` | system → dev | QA failure details (triggers rework) |
| `uat_feedback` | system → dev | UAT rejection feedback (triggers rework) |
| `credential` | system → agents | Broadcast new credentials |
| `plan_proposal` | architect → user | Plan for approval |
| `ecosystem_update` | system → agents | Peer broadcasts |
| `review_request` | dev → user | Changes ready for commit |

## Ecosystem

```
ecosystem/
  │
  ├─ setup_ecosystem.py
  │    ├─ setup_shared_ecosystem()    ← ONCE: copy to MA_DIR/.claude/
  │    ├─ setup_agent_ecosystem()     ← PER AGENT: symlinks + config
  │    ├─ refresh_agent_tools()       ← RUNTIME: detect new tools
  │    ├─ discover_project_ecosystem()← per-project tools
  │    └─ get_smart_hints()           ← context-aware hints for prompts
  │
  ├─ mcp/
  │    └─ setup_mcp.py
  │         ├─ MCP_SERVERS registry   ← all known MCP server configs
  │         └─ generate_mcp_json()    ← writes .mcp.json per agent
  │
  ├─ hooks/
  │    └─ setup_hooks.py
  │         ├─ generate_hooks_config()   ← pre/post task hooks
  │         └─ generate_settings_json()  ← claude settings.json
  │
  ├─ subagents/                ← .md files → .claude/agents/
  │    ├─ code-reviewer.md
  │    ├─ explorer.md
  │    ├─ test-writer.md
  │    ├─ db-reader.md
  │    ├─ figma-to-vue.md
  │    ├─ playwright-*.md
  │    ├─ route-generator.md
  │    ├─ test-migrator.md
  │    ├─ performance-analyzer.md
  │    └─ translation-automator.md
  │
  ├─ commands/                 ← .md files → .claude/commands/
  │    ├─ review.md, fix-issue.md, test.md, uat.md
  │    ├─ generate-route.md, migrate-test.md
  │    ├─ playwright-test.md, security-scan.md
  │    ├─ submit-review.md, figma-to-vue.md
  │    └─ ...
  │
  ├─ skills/                   ← dirs → .claude/skills/
  │    ├─ deploy/
  │    ├─ figma_to_vue/
  │    ├─ health_check/
  │    ├─ playwright_test/
  │    ├─ test_migration/
  │    └─ translation_sql_writer/
  │
  └─ templates/
       └─ generate_claude_md.py  ← auto-generate CLAUDE.md for projects
```

## Lib Modules

```
lib/
  ├─ config.py    load_config(), save_default_config(), scan_projects(),
  │               detect_stack(), detect_target()
  │               _PROJECT_MARKERS: package.json, composer.json, go.mod, etc.
  │
  ├─ roles.py     generate_roles() → writes {name}-role.md per agent
  │               DEFAULT_ROLES: architect, frontend, backend, qa, devops, etc.
  │
  └─ memory.py    init_memory() → MA_DIR/memory/ directory structure
```

## Config Loading

```
Priority (first found wins):
  1. WORKSPACE/multiagent.json    ← user project config
  2. MA_DIR/config.json           ← generated by start.py

Hot-reload (every 15s by _config_reload_timer):
  ├─ Budget: BUDGET_LIMIT, BUDGET_PER_AGENT
  ├─ Agents: ALL_AGENTS, HIDDEN_AGENTS, VISIBLE_AGENTS
  │          + always injects reviewer-logic/style/arch
  ├─ Notifications: webhook config
  └─ Auto-scale: min/max agents, queue threshold

Default agents: [architect, frontend, backend, qa]
  + auto-injected: [reviewer-logic, reviewer-style, reviewer-arch] (hidden, haiku)
```

## Concurrency Model

```
Hub (FastAPI + ThreadPoolExecutor):
  ├─ Global threading.Lock → WRITE operations only
  ├─ Read-only endpoints → NO lock (GIL atomic reads)
  ├─ Dashboard snapshot → cached via _state_version (lock-free)
  ├─ WebSocket → calls get_dashboard_snapshot() directly
  ├─ Log push → lock-free (deque.append is thread-safe)
  └─ _do_save() → lock only for rare task cleanup (>500)

Workers (separate processes):
  ├─ Each agent = separate Python process
  ├─ Communicate via HTTP to hub (no shared memory)
  ├─ Claude CLI = subprocess with streaming stdout
  └─ File locks via hub API (cooperative locking)
```

## Dashboard (Web UI)

```
hub/dashboard/
  ├─ index.html     Single-page app
  ├─ style.css      Styling
  └─ app.js         Vanilla JS + WebSocket

WebSocket /ws:
  ├─ Receives: {"type": "dashboard", "data": snapshot}    (1s interval)
  ├─ Receives: {"type": "logs", "lines": [...]}           (when following agent)
  ├─ Sends:    {"type": "follow", "agent": "name"}        (subscribe to logs)
  └─ Sends:    "ping"                                     (keepalive)

User actions (REST calls from dashboard):
  ├─ Send task to agent         POST /messages
  ├─ Approve/reject UAT         POST /tasks/{tid}/uat
  ├─ Approve/dismiss plan       POST /plan/approve or /plan/dismiss
  ├─ Commit changes             POST /git/commit
  ├─ Push to remote             POST /git/push
  ├─ Rollback                   POST /git/rollback
  ├─ Stop agent                 POST /agents/{name}/stop
  ├─ Save credentials           POST /credentials
  └─ Configure notifications    POST /notifications/config
```

## Key Constants

| Constant | Value | Location | Purpose |
|---|---|---|---|
| `MAX_REWORK_LOOPS` | 3 | state.py | Max code review rework cycles |
| `MAX_TASKS` | 500 | state.py | Task cleanup threshold |
| `MSG_RATE_LIMIT` | 60 | state.py | Messages/min/sender |
| `PATTERN_SCORE_CAP` | (10, -5) | state.py | Pattern score bounds |
| `PATTERN_PRUNE_AT` | -3 | state.py | Auto-delete bad patterns |
| Review timeout | 15 min | state.py | Auto-approve pending reviews |
| Plan timeout | 30 min | state.py | Auto-dismiss pending plans |
| Lock cleanup | 300s | state.py | Stale lock removal threshold |
| Config reload | 15s | state.py | Hot-reload interval |
| Save interval | 10s | state.py | State persistence check |
| Backup rotation | 5 min | state.py | .bak.1/.bak.2/.bak.3 cycle |

## Safety Guards

These guards prevent subtle lifecycle bugs. Each was added to fix a real failure mode.

| Guard | Location | What it prevents |
|---|---|---|
| Reviewer auto-injection | `state.py:41-47` + hot-reload | Reviewers missing from ALL_AGENTS → silent auto-approve |
| submit_review status check | `tasks.py:835` | Stale verdicts from old review cycles causing duplicate QA dispatch |
| Auto-assign hidden exclusion | `tasks.py:706` | Reviewers grabbing regular dev tasks during idle |
| UAT rejection → uat_feedback | `tasks.py:973` | UAT rework not resetting dev session (stale context) |
| safe_project_dir(".") | `state.py:764` | Code review diff collection failing for single-project workspaces |
| Rework session reset | `worker.py:619` | Dev using stale Claude session on qa/review/uat feedback |
| Feedback msg_type recognition | `worker.py:559` | qa_feedback/review_feedback/uat_feedback not treated as tasks |

## File Dependencies

```
start.py
  ├── lib/config.py
  ├── lib/roles.py
  ├── lib/memory.py
  ├── ecosystem/setup_ecosystem.py
  │     ├── ecosystem/mcp/setup_mcp.py
  │     ├── ecosystem/hooks/setup_hooks.py
  │     └── ecosystem/templates/generate_claude_md.py
  ├── hub/hub_server.py
  │     ├── hub/state.py              ← imported by ALL routers
  │     └── hub/routers/__init__.py
  │           ├── agents.py
  │           ├── tasks.py            ← imports _dispatch_code_review, _dispatch_qa
  │           ├── messages.py
  │           ├── websocket.py
  │           ├── git.py
  │           ├── logs.py
  │           ├── costs.py
  │           ├── credentials.py
  │           ├── analytics.py
  │           ├── health.py
  │           ├── patterns.py
  │           └── cache.py
  └── agents/worker.py
        ├── agents/context.py
        ├── agents/hub_client.py      ← HTTP calls to hub
        ├── agents/claude_runner.py   ← claude CLI subprocess
        ├── agents/verify.py
        ├── agents/git_ops.py
        ├── agents/mcp_manager.py
        ├── agents/learning.py
        ├── agents/chat_handler.py
        ├── agents/credentials.py
        └── agents/log_utils.py
```

## Known Gaps & Edge Cases

Comprehensive audit of the system (Feb 2026). These are documented to guide future development — not all require immediate fixes.

### Worker Loop Gaps (agents/worker.py)

| # | Gap | Risk | Lines |
|---|-----|------|-------|
| W1 | Malformed hub response (non-list from poll) crashes loop | HIGH | 670-673 |
| W2 | Poll loop has no exponential backoff on hub failure | HIGH | 570-576 |
| W3 | Task stuck in `in_progress` if Claude hangs (no task-level timeout independent of CLI) | HIGH | 909, 1489 |
| W4 | hub_post null returns not validated before `.get()` in many places | HIGH | 642, 654, 776, 917 |
| W5 | Session map (`_session_map`) grows unbounded — never evicts old entries | LOW | context.py:109-114 |
| W6 | Credential dedup can drop retry sends of same token | MED | 710-719 |
| W7 | Credential wait `consume=false` peek then consume — not atomic | MED | 1069-1074 |
| W8 | File lock is cooperative only — no enforcement, just warning | MED | 1451-1457 |
| W9 | Auto-assign can race with manual assignment — no atomic claim | MED | 641-643 |
| W10 | Chat handler thread not killed on task timeout | MED | 1487, 1489 |
| W11 | Pattern voting penalizes patterns on unrelated failures | LOW | 1690-1698 |

### Claude Runner Gaps (agents/claude_runner.py)

| # | Gap | Risk | Lines |
|---|-----|------|-------|
| C1 | Process timeout only on `proc.wait()`, not actual execution time | HIGH | 505-516 |
| C2 | OOM kill (SIGKILL/-9) treated as intentional cancel, not retried | HIGH | 527-529 |
| C3 | Stderr thread never properly joined (5s timeout) — FD leak | MED | 516 |
| C4 | No limit on stderr_lines size — OOM on huge error output | MED | 382-383 |
| C5 | JSON line parsing silently drops incomplete lines | LOW | 407, 494 |
| C6 | Rate limit detection uses substring match — false positives | LOW | 169, 552 |
| C7 | No global retry budget per task (5 retries × N calls = unbounded) | MED | 216 |

### Hub Server Gaps (hub/)

| # | Gap | Risk | Lines |
|---|-----|------|-------|
| H1 | Task status transitions not atomic — dispatch failure leaves partial state | HIGH | tasks.py:114-336 |
| H2 | Save batching (5s) — tasks created within 5s of last save lost on crash | HIGH | state.py:429-436 |
| H3 | Reviewer timeout moves parent to in_testing but doesn't cancel subtasks | HIGH | state.py:1077-1250 |
| H4 | State lock not held across network calls in message routing | HIGH | messages.py:44-164 |
| H5 | Hub crash leaves orphaned workers — duplicate workers on restart | HIGH | start.py:505-601 |
| H6 | Message queue unbounded growth (no max per agent) | MED | state.py:122-127 |
| H7 | QA dispatch without checking QA agent availability | MED | tasks.py:549-645 |
| H8 | Rework cycle counter not properly enforced across code_review/QA/UAT | MED | tasks.py:725-828 |
| H9 | UAT has no timeout — task stuck in UAT indefinitely | MED | tasks.py |
| H10 | Plan steps with non-existent agents — tasks created but unassignable | MED | messages.py:84-135 |
| H11 | No state machine validation — can transition directly code_review→done | MED | tasks.py:114-336 |
| H12 | Port conflict causes silent startup failure | MED | start.py:180-196 |

### Dashboard Gaps (hub/dashboard/)

| # | Gap | Risk | Lines |
|---|-----|------|-------|
| D1 | No WebSocket heartbeat/ping-pong — silent disconnects after firewall timeout | MED | app.js:160, websocket.py:96 |
| D2 | Reconnection backoff too aggressive — 57s max delay | MED | app.js:154 |
| D3 | Dashboard cache not invalidated on hub restart — shows stale data | MED | app.js:232-240 |
| D4 | No loading spinners during long operations (git push, etc.) | LOW | app.js |
| D5 | HTTP fetch calls missing `.catch()` — unhandled promise rejections | LOW | app.js:1194 |
| D6 | No XSS protection on task descriptions in some contexts | MED | app.js |

### Ecosystem & MCP Gaps

| # | Gap | Risk | Lines |
|---|-----|------|-------|
| E1 | No MCP server crash detection — stdio servers die silently | HIGH | mcp_manager.py:131-212 |
| E2 | Concurrent writes to ~/.claude.json unprotected (no file lock) | HIGH | mcp_manager.py:35-91 |
| E3 | File watcher race condition + broken debounce (local var reset) | HIGH | mcp_manager.py:404-461 |
| E4 | Health check only verifies binary exists, not that server responds | MED | mcp_manager.py:514-541 |
| E5 | Credential reload doesn't trigger MCP re-initialization in running CLI | MED | mcp_manager.py:307-356 |
| E6 | Credentials stored in plaintext (no encryption at rest) | HIGH | credentials.py:24-44 |
| E7 | No token expiration tracking (OAuth tokens expire silently) | MED | credentials.py |
| E8 | Pattern classification keyword-only — no semantic understanding | LOW | learning.py:178-207 |
| E9 | No JSON schema validation for multiagent.json | MED | config.py:35-105 |
| E10 | Config hot-reload not actually implemented for multiagent.json (only .mcp.json) | MED | config.py |

### Git Operations Gaps (agents/git_ops.py)

| # | Gap | Risk | Lines |
|---|-----|------|-------|
| G1 | Stash pop conflict uses `checkout --theirs` — doesn't resolve all conflicts | HIGH | 205-216 |
| G2 | Git command 15s timeout not caught as TimeoutExpired | MED | 51-57 |
| G3 | Rollback returns True even if stash pop fails | MED | 136-141 |
| G4 | Branch name sanitization can create invalid names on truncation | LOW | 163-172 |

### Verification Gaps (agents/verify.py)

| # | Gap | Risk | Lines |
|---|-----|------|-------|
| V1 | Verify stuck if Claude doesn't write result file — loops until max_cycles | MED | 104-108 |
| V2 | No check if project actually has test/lint commands | MED | 41-84 |
| V3 | Max cycles can be 1 for single-file changes — no retry | LOW | 51-56 |

### Missing Safety Guards

Based on the gaps above, these guards should be added:

| Guard Needed | Addresses | What it would prevent |
|---|---|---|
| Hub response validation in poll loop | W1, W4 | Crash on malformed hub response |
| Exponential backoff on hub failure | W2 | Tight retry loops hammering dead hub |
| Task-level timeout (independent of CLI) | W3 | Tasks stuck forever when Claude hangs |
| Atomic task status transitions | H1, H11 | Partial state on dispatch failure, invalid transitions |
| MCP server liveness monitoring | E1 | Silent MCP death leaving agents without tools |
| File lock on ~/.claude.json writes | E2 | Corrupted config from concurrent agent writes |
| Credential encryption at rest | E6 | Plaintext secrets on disk |
| WebSocket ping-pong heartbeat | D1 | Silent dashboard disconnects |
