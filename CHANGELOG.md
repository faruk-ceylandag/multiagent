# Changelog

## 2026-03-03

### Features
- **Jira integration** — Check Jira ticket status from tasks, harden SSE stream parsing (`9bb218a`)

### Improvements
- **Reviewer isolation** — Lock dev file changes instead of staging; reviewers get read-only access (`2410749`)
- **State persistence** — Preserve logs, task assignments, and review verdicts across hub restarts (`2e45749`)
- **Architect upgrade** — Switch architect model from haiku to sonnet for better task descriptions (`8a4600e`)

### Fixes
- Fix CLI exit=1 crash from stale session resume + model fallback for haiku (`6ac9adf`)

---

## 2026-02-28

### Features
- **Autonomous task flow** — Auto-approve plans, architect tracking, QA enforcement (`b26a165`)
- **SSE MCP servers** — Enable Atlassian, Sentry, Figma MCP servers for agents (`6960415`)

### Improvements
- **Production optimization** — Fix 35 issues across security, reliability, performance, cleanup (`59eca31`)
- **Architect automation** — Pre-fetch URLs, use haiku, block exploration tools (`78687b5`)
- **Architect permissions** — Restrict to prevent codebase exploration, disallow interactive tools (`9aa969d`, `be67ba4`)
- **Live stats** — Show per-task cost, fix retry token double-count (`26b1fc4`)
- **Architecture docs** — Document ~80 known system gaps with line references (`86c2f29`)

### Fixes
- Fix Jira MCP: skip subprocess pre-fetch for SSE servers, fix health check (`e8f24fb`)
- Fix intent classification: URLs and ticket IDs always classified as task (`8906ccf`)
- Fix architect plan proposal: remove plan mode, execute curl via Bash (`dcadad8`)
- Fix haiku model: wrong default ID and missing env var in start.py (`08a7e87`)
- Fix agent context loss after credential refresh (`49c0e3e`)

---

## 2026-02-27

### Features
- **QA subtasks** — QA subtask creation, task detail API, auto-assign exclusion, QA failure handling (`22051c0`)
- **Kanban indicators** — QA subtask hierarchy with kanban progress indicators (`2c4242d`)
- **Project config** — Add `multiagent.json` project configuration (`41e814e`)
- **4-phase upgrade** — 15 features: UI/UX, agent intelligence, production hardening, advanced analytics (`30cd0ab`)

### Fixes
- Fix pipeline gaps, `--session-id` CLI error, and MCP visibility (`e1958f9`)
- Fix agent stop: actually pause agent until resume signal (`a1cd36e`)
- Fix shutdown to kill all child processes (claude CLIs, MCP servers) (`545b053`)
- Fix `--session-id` without `--continue/--resume` causing CLI errors (`8e9d005`, `6c88234`)
- Fix health endpoint: read `_state_initialized` dynamically from module (`4df4d19`)
- Fix deadlock: use RLock instead of Lock in hub state (`7da29ce`)
- Fix hub startup: set `_state_initialized` on first run (`fa43140`)
- Fix Python 3.14 compatibility: `asyncio.get_event_loop()` no longer creates implicit loop (`8c8c102`)
- Speed up architect delegation and fix duplicate log lines (`b8e89fe`)

---

## 2026-02-26

### Improvements
- Dashboard polish + system fixes (12 improvements) (`71eb501`)

---

## 2026-02-25

### Features
- **Multi-workspace support** + enhanced autonomy (6 phases) (`5481efc`)

### Improvements
- **Production hardening** — 13 fixes: WS auth, bounded data, crash-safe state, process cleanup (`318d8d5`)
- Dashboard UX + pipeline optimization (7 fixes) (`2b74712`)

---

## 2026-02-23

### Features
- **Collapsible log blocks**, task-scoped file locks, QA task lifecycle (`dd9aeb1`)
- **Hidden agents** — Show reviewers in sidebar as dimmed cards (`d29dccc`)
- **Shared ecosystem** — Symlink subagents/commands/skills instead of per-agent copies (`546f4e2`)

### Improvements
- **Lifecycle fix** — Reviewer auto-injection + rework handling + system graph (`bb87816`)
- QA failure same-task rework cycle + subtask-based code review (`be0be98`)

### Fixes
- Fix commit message role leak + plan approval button persistence (`da861ed`)
- Fix 11 system issues from GRAPH analysis (`4742b98`)

---

## 2026-02-22 — Initial Release

### Features
- **Multi-Agent Collaborate System** — Initial architecture: hub (FastAPI), worker agents (Claude CLI), web dashboard (`823f3d6`)
- **Extended task lifecycle** — `to_do → in_progress → code_review → in_testing → uat → done` (`5bc3cd9`)
- **Plan proposal UI** + auto-inject hidden reviewer agents on boot (`3d310b3`)
- **Sidebar agent targeting** + AI intent classification (`64b8f91`)
- **Branch naming** — Extract Jira/ticket IDs from URLs (`6fbae10`)
- **Session management** — Hub startup reset, help modal, Mac shortcuts, Cmd+C stop (`13f1599`)
- **Plan approval flow** — UI polish, auto-start after approve (`b381de9`)

### Fixes
- Fix critical: reviewer/QA agents no longer hijack task status (`89a9105`)
- Fix plan approval: set tasks to `in_progress` and send as user (`4398978`)
- Fix architect premature task done: keep `in_progress` until plan approved (`26e0dca`)
- Fix review tab branch filter: `escAttr` was stripping slashes (`25f0603`)
- Redesign plan proposal UI + fix buttons staying after approve/dismiss (`d190905`)
- Fix commit msg priority, plan parent lifecycle & plan timeout (`223a315`)
- Fix help modal, Mac shortcuts, Cmd+C stop & agent hub-down exit (`5fcb488`)
- Reset Claude CLI session for architect/QA/reviewers between tasks (`837624d`)
- Skip branch creation when no real ticket ID is provided (`26027e6`)