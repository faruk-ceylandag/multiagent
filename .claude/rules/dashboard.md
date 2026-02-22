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

Dashboard calls `get_dashboard_snapshot()` on hub side — cached, rebuilds only on state change. Includes: agents, tasks, usage, locks, activity, patterns, inbox, task_reviews, task_comments.

## Kanban Board

7 columns: To Do → In Progress → Code Review → Testing → UAT → Done → Failed.
CSS: `grid-template-columns:repeat(7,1fr)`, responsive breakpoints collapse to fewer columns.

## Task Detail Modal

- **Review badges**: Shows each reviewer's verdict (approve ✅ / changes requested 🔄)
- **Comment thread**: File-linked comments from reviewers with resolve/unresolve toggle
- **UAT controls**: Approve (green) / Reject (red + feedback textarea) buttons when task is in `uat` status

## Hidden Agents

Agents in `HIDDEN_AGENTS` are filtered from the dashboard agent list but their activity appears in the activity feed and review comment sections.