---
paths:
  - "hub/**"
---

# Hub Rules

Hub is a FastAPI server. All shared state lives in `hub/state.py`.

## Concurrency

- Global `threading.Lock` in state.py — **only for WRITE operations**
- Read-only endpoints have NO lock — Python GIL ensures atomic dict/list reads
- Dashboard snapshot is cached via `get_dashboard_snapshot()` — rebuilds only when `_state_version` changes
- Call `bump_version()` after any state mutation
- Call `save_state()` after persistent data changes

## Task Lifecycle

States: `to_do → in_progress → code_review → in_testing → uat → done / failed`

- `code_review`: Auto-dispatches 3 hidden reviewers (reviewer-logic, reviewer-style, reviewer-arch). 3/3 approve → `in_testing`.
- `in_testing`: QA agent runs tests. Pass → `uat`.
- `uat`: User approves/rejects from dashboard.
- Rework: Any reject → `in_progress` with feedback. Max 3 rework cycles then auto-approve.
- Review timeout: 15 min auto-approve in `_review_timeout_timer`.

State dicts: `task_comments` (comment threads), `task_reviews` (reviewer verdicts), `HIDDEN_AGENTS` (filtered from dashboard).

Legacy status migration: `created→to_do`, `assigned→in_progress`, `in_review→code_review` in `load_state()`.

## Routers

Each router is in `hub/routers/`. Register new routers in `hub/routers/__init__.py`.

- `agents.py` — Agent CRUD, poll, status, specialization, learnings
- `tasks.py` — Task CRUD, auto-assign, kanban, code review dispatch, UAT, comments
- `messages.py` — Message queue between agents
- `patterns.py` — Pattern registry (score-based, auto-prune)
- `analytics.py` — Dashboard data, export, autoscale
- `websocket.py` — WebSocket for dashboard + log streaming
- `git.py`, `logs.py`, `costs.py`, `credentials.py`, `health.py`

### Review & UAT Endpoints (in tasks.py)

- `POST /tasks/{tid}/review` — Reviewer verdict (approve/request_changes + comments)
- `POST /tasks/{tid}/uat` — User decision (approve/reject + feedback)
- `POST /tasks/{tid}/comments` — Add comment
- `GET /tasks/{tid}/comments` — List comments
- `POST /tasks/{tid}/comments/{cid}/resolve` — Resolve comment

## Persistence

`_do_save()` writes to `hub_state.json`. Add new state dicts to both `_do_save()` snapshot and `load_state()` restore. Includes: `task_comments`, `task_reviews`, `_comment_counter`.

## Pattern Registry

Patterns live in `pattern_registry` dict. Score range: -5 to 10. Auto-prune at score <= -3. Categories are fixed in `PATTERN_CATEGORIES` list.

## Known Gaps

When modifying hub code, be aware of these documented issues (see GRAPH.md for full details):

- **H1**: Task status transitions not atomic — dispatch failure leaves partial state (tasks.py:114-336)
- **H2**: Save batching (5s) — crash can lose recent tasks (state.py:429-436)
- **H3**: Reviewer timeout moves parent to in_testing but doesn't cancel subtasks (state.py:1077-1250)
- **H4**: State lock not held across network calls in message routing (messages.py:44-164)
- **H5**: Hub crash leaves orphaned workers — duplicate workers on restart (start.py:505-601)
- **H6**: Message queue unbounded growth, no max per agent (state.py:122-127)
- **H7**: QA dispatch without checking QA agent availability (tasks.py:549-645)
- **H8**: Rework cycle counter not properly enforced across code_review/QA/UAT (tasks.py:725-828)
- **H9**: UAT has no timeout — task stuck in UAT indefinitely
- **H10**: Plan steps with non-existent agents — tasks created but unassignable (messages.py:84-135)
- **H11**: No state machine validation — can transition directly code_review→done (tasks.py:114-336)
- **H12**: Port conflict causes silent startup failure (start.py:180-196)
