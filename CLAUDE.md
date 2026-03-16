# Multi-Agent System

AI agent team that collaborates to build, test, and ship code. Architect delegates, devs implement, reviewers check, QA tests, user approves.

## Structure

```
start.py          Entry point — copies packages to .multiagent/, starts hub + workers
hub/              FastAPI server (state.py + modular routers in hub/routers/)
hub/dashboard/    Web UI (vanilla JS, WebSocket)
agents/           Claude CLI agent workers (worker.py = main loop)
ecosystem/        MCP configs, subagents, commands, skills, hooks, templates
lib/              Config, roles, memory utilities
install.sh        System-wide install to ~/.local/share/multiagent
```

### Hub Server (`hub/`)

| File | Purpose |
|------|---------|
| `hub_server.py` | FastAPI app init, router registration, background threads, static dashboard |
| `state.py` | Global state dicts (tasks, agents, messages, patterns), RLock, persistence to `hub_state.json`, config hot-reload |
| `models.py` | Pydantic models for API request/response validation |
| `validation.py` | Input validation helpers |
| `responses.py` | Standardized JSON response wrappers |

### Hub Routers (`hub/routers/`)

| Router | Purpose |
|--------|---------|
| `tasks.py` | Task CRUD, auto-assign, dependency graphs, code review dispatch, UAT workflow, rework logic |
| `agents.py` | Agent registration, status, specialization, progress tracking |
| `analytics.py` | Dashboard snapshot, cost tracking, autoscale decisions |
| `messages.py` | Inter-agent messaging, task & chat queues, inbox |
| `git.py` | Branch management, diff viewing, commit tracking |
| `health.py` | System health checks, agent responsiveness |
| `costs.py` | Token usage logging, budget tracking |
| `credentials.py` | Credential CRUD with encryption/masking |
| `patterns.py` | Score-based pattern registry (-5 to 10, auto-prune at ≤-3) |
| `cache.py` | LRU cache for expensive queries |
| `logs.py` | Log streaming |
| `websocket.py` | Real-time dashboard updates via WebSocket |
| `workspaces.py` | Multi-workspace routing |

### Agent Workers (`agents/`)

| File | Purpose |
|------|---------|
| `worker.py` | Main loop: boot → register → poll tasks → run Claude CLI → verify → extract learnings |
| `context.py` | `AgentContext` dataclass — all per-agent shared state (avoids globals) |
| `hub_client.py` | Hub API communication: `hub_post()`, `hub_get()`, `hub_msg()` |
| `claude_runner.py` | Claude CLI execution with streaming, model complexity scoring, token tracking |
| `verify.py` | Lint/test/build verification loop (up to 8 retry cycles) |
| `git_ops.py` | Branch management, commit, rollback, PR creation, file locking |
| `learning.py` | Pattern extraction, ecosystem refresh, broadcast updates |
| `mcp_manager.py` | MCP server setup, file watching, credential loading, health checks |
| `chat_handler.py` | Background thread for chat messages during task execution |
| `credentials.py` | Load/save credentials, expiration checks, OAuth refresh |
| `log_utils.py` | Structured logging, streaming to hub, humanization |

### Ecosystem (`ecosystem/`)

Extensible tool stack for agents. Copied to `.multiagent/.claude/` at boot, refreshed at each task start via diff + symlink.

| Subdirectory | Content |
|---|---|
| `mcp/` | MCP server definitions (GitHub, Sentry, Figma, Context7, Sequential Thinking) |
| `subagents/` | Specialized tool agents (code-reviewer, explorer, test-writer, db-reader, playwright-*) |
| `commands/` | Slash commands (/review, /submit-review, /test, /fix-issue, /uat, etc.) |
| `skills/` | Directory-based skills (health_check, deploy, jira_dev_check, playwright_test, etc.) |
| `hooks/` | Hook config generation (auto-format, auto-lint, file-lock, notifications) |
| `templates/` | Dynamic CLAUDE.md generator per project (`generate_claude_md.py`) |

### Library (`lib/`)

| File | Purpose |
|------|---------|
| `config.py` | Config loading, project scanning, stack detection, task routing map, model aliases |
| `roles.py` | Dynamic role generation (architect, frontend, backend, qa, devops, security, reviewer-*) |
| `memory.py` | Shared memory init: project-context.md, task-board.md, contracts.md, architecture.md, agents.md, learnings/ |

### Dashboard (`hub/dashboard/`)

Single-page vanilla JS app (no build step). Dark theme, responsive grid.

| File | Purpose |
|------|---------|
| `app.js` | All frontend logic: WebSocket, kanban board, code review UI, analytics, git, logs |
| `index.html` | DOM structure |
| `style.css` | Styling |

**Dashboard sections**: Logs, Tasks (7-column kanban), Inbox, Review, Git, Tests, Activity, Locks, Analytics, Timeline, Alerts.

## Task Lifecycle

```
to_do → in_progress → code_review → in_testing → uat → done
```

| Status | What Happens |
|--------|-------------|
| `to_do` | Task created, waiting for assignment |
| `in_progress` | Dev agent working on it |
| `code_review` | 3 hidden reviewers (logic, style, arch) auto-dispatched in parallel |
| `in_testing` | QA agent runs tests (after 3/3 reviewer approval) |
| `uat` | User approves/rejects from dashboard |
| `done` | Complete |
| `failed` | Can retry → `to_do` |

**Rework**: Any reviewer `request_changes` → back to `in_progress` with comments. Max 3 cycles then auto-approve.
**Timeout**: Reviewers auto-approve after 15 min.

## Key API Endpoints

```
POST /tasks/{tid}/review           — {agent, verdict: "approve"|"request_changes", comments: [...]}
POST /tasks/{tid}/uat              — {action: "approve"|"reject", feedback: "..."}
POST /tasks/{tid}/comments         — {agent, text}
GET  /tasks/{tid}/comments         — list comments
POST /tasks/{tid}/comments/{cid}/resolve — mark resolved
POST /agents/{name}/register       — register agent with hub
GET  /agents/{name}/status         — agent status
POST /agents/{name}/progress       — progress update
POST /messages                     — inter-agent messaging
GET  /messages/{agent}/inbox       — agent inbox
GET  /health                       — system health check
GET  /analytics/dashboard          — dashboard snapshot (cached)
POST /costs/usage                  — log token usage
GET  /git/{project}/branches       — branch list
POST /credentials                  — store credential
GET  /patterns                     — pattern registry
```

## Architecture

**Design**: Multi-process, hub-and-spoke. Hub is a FastAPI server; agents are long-running Claude CLI processes.

**Concurrency model**:
- `threading.RLock()` for write operations only in hub state
- Read-only endpoints are lock-free (GIL ensures atomic dict reads)
- Dashboard snapshot cached, rebuilt only on state change
- Circuit breaker for rate-limited agents
- Cooperative file locking via hub API

**Boot sequence** (`start.py`):
1. Parse args → load config → auto-inject reviewer agents
2. Port management (find free port or reuse existing hub)
3. Project detection (`scan_projects()` + `detect_stack()`)
4. Init directories, shared memory, config, stack.json
5. Generate roles per agent, setup ecosystem (copy to `.multiagent/.claude/`)
6. Start FastAPI hub server (uvicorn, background)
7. Spawn agent worker processes (staggered by `boot_stagger` config)
8. Monitor loop: keep agents alive, restart on crash, handle SIGTERM/SIGINT

**Agent loop** (`agents/worker.py`):
1. Register with hub → poll every 2s for tasks
2. On task: refresh ecosystem tools → run Claude CLI → verify (lint/test/build)
3. Extract learnings → broadcast to peers → update hub state
4. On crash: auto-restart by start.py monitor

**Learning & patterns**:
- Score-based pattern registry (-5 to 10), auto-prune at ≤ -3
- Extracted after each task success/failure
- Broadcast to all agents as peer learnings
- Smart hint injection (role-aware, keyword-based, token-efficient)

## Hidden Agents

Agents with `"hidden": true` in config: invisible on dashboard, no team roster entry. Used for reviewer-logic, reviewer-style, reviewer-arch (haiku model). Auto-injected by start.py even if omitted from config.

## Configuration

### `multiagent.json` (project root)

```json
{
  "port": 8040,
  "thinking_model": "claude-sonnet-4-5-20250929",
  "coding_model": "claude-opus-4-6",
  "agents": [
    {"name": "architect", "model": ""},
    {"name": "frontend", "model": ""},
    {"name": "backend", "model": ""},
    {"name": "qa", "model": ""}
  ],
  "focus_project": "",
  "auto_verify": true,
  "notifications": true,
  "boot_stagger": 2,
  "max_context": 12000,
  "mcp_servers": {},
  "budget_limit": 0
}
```

**Key config fields**:
- `agents`: List of `{name, model?, hidden?, role?}`. Reviewer agents auto-injected.
- `model` aliases: `"haiku"` → `claude-haiku-4-5-20251001`, `"sonnet"` → `claude-sonnet-4-5-20250929`, `"opus"` → `claude-opus-4-6`
- `budget_limit`: Global cost cap (0 = unlimited). Also supports `budget_per_agent`.
- `auto_uat`: Skip UAT, go straight to done.
- `auto_plan_approval`: Auto-approve architect plans.
- `auto_verify`: Run lint/test/build after each task.

Hot-reloaded every 15s — no restart needed.

## Quick Commands

```bash
ma                           # start system
ma send <agent> <message>    # send task to agent
ma status                    # show agent statuses
ma tasks                     # list tasks
ma tail <agent>              # follow logs
ma kill                      # stop everything
```

## Coding Conventions

**Python style**:
- Minimal type hints; concise function names (`hub_get()`, `hub_post()`, `hub_msg()`)
- Constants in UPPERCASE: `MAX_TASKS`, `MAX_CONTEXT`, `VALID_TRANSITIONS`
- Private functions prefixed with `_`
- Error handling: try/except with logging, fallback chains

**Naming**:
- Agent names: lowercase with hyphens (`architect`, `frontend`, `reviewer-logic`)
- Task IDs: auto-incrementing integers
- Status values: lowercase with underscores (`to_do`, `in_progress`, `code_review`, `in_testing`, `uat`, `done`, `failed`)
- Message types: lowercase (`message`, `chat`, `task`, `review_request`, `review_feedback`)

**Import order**:
```python
import os, json, time, threading          # stdlib
from fastapi import FastAPI, APIRouter    # third-party
from hub.state import lock, tasks         # local
```

**Module design**:
- One responsibility per file
- Shared state via `AgentContext` dataclass in agents (not global variables)
- Hub state via global dicts + `threading.RLock()` in `state.py`
- Routers in separate files, registered in `routers/__init__.py`

## URL Routing — Use the Right MCP Tool

When a user provides a URL, use the matching MCP tool:
- `atlassian.net`, `jira`, `confluence` → Use **Atlassian MCP** tools
- `github.com` → Use **GitHub MCP** tools
- `figma.com` → Use **Figma MCP** tools
- `sentry.io` → Use **Sentry MCP** tools
- `docs.google.com`, `drive.google.com` → Use **Google MCP** tools

## Key Files by Size

| File | Size | Purpose |
|------|------|---------|
| `hub/routers/tasks.py` | 81KB | Task CRUD, review dispatch, UAT |
| `start.py` | 38KB | Entry point, boot, agent spawning |
| `hub/dashboard/app.js` | 3333 lines | Dashboard frontend |
| `agents/worker.py` | 2338 lines | Agent main loop |
| `hub/state.py` | 1368 lines | Shared state, persistence |
| `hub/routers/agents.py` | 27KB | Agent management |
| `hub/routers/analytics.py` | 17KB | Dashboard & metrics |
| `hub/routers/git.py` | 17KB | Git operations |
| `install.sh` | 16KB | System installation |
| `hub/routers/health.py` | 15KB | Health checks |
