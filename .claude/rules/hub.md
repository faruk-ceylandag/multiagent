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