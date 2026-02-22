---
paths:
  - "hub/dashboard/**"
---

# Dashboard Rules

Single-page web UI in vanilla JS. No build step, no framework.

## Key File

`hub/dashboard/app.js` — All frontend logic. Uses WebSocket for real-time updates.

## Connection

- WebSocket at `ws://localhost:{port}/ws` for live dashboard data + log streaming
- HTTP endpoints for actions (commit, task create, message send, etc.)

## Data Flow

Dashboard calls `get_dashboard_snapshot()` on hub side — cached, rebuilds only on state change. Includes: agents, tasks, usage, locks, activity, patterns, inbox.
