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

## Routers

Each router is in `hub/routers/`. Register new routers in `hub/routers/__init__.py`.

- `agents.py` — Agent CRUD, poll, status, specialization, learnings
- `tasks.py` — Task CRUD, auto-assign, kanban
- `messages.py` — Message queue between agents
- `patterns.py` — Pattern registry (score-based, auto-prune)
- `analytics.py` — Dashboard data, export, autoscale
- `websocket.py` — WebSocket for dashboard + log streaming
- `git.py`, `logs.py`, `costs.py`, `credentials.py`, `health.py`

## Persistence

`_do_save()` writes to `hub_state.json`. Add new state dicts to both `_do_save()` snapshot and `load_state()` restore.

## Pattern Registry

Patterns live in `pattern_registry` dict. Score range: -5 to 10. Auto-prune at score <= -3. Categories are fixed in `PATTERN_CATEGORIES` list.
