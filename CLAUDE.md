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

### Key API Endpoints

```
POST /tasks/{tid}/review    — {agent, verdict: "approve"|"request_changes", comments: [...]}
POST /tasks/{tid}/uat       — {action: "approve"|"reject", feedback: "..."}
POST /tasks/{tid}/comments  — {agent, text}
GET  /tasks/{tid}/comments  — list comments
POST /tasks/{tid}/comments/{cid}/resolve — mark resolved
```

### Hidden Agents

Agents with `"hidden": true` in config: invisible on dashboard, no team roster entry. Used for reviewer-logic, reviewer-style, reviewer-arch (haiku model).

## Quick Commands

```bash
ma                           # start system
ma send <agent> <message>    # send task to agent
ma status                    # show agent statuses
ma tasks                     # list tasks
ma tail <agent>              # follow logs
ma kill                      # stop everything
```

## Config

`multiagent.json` in project root or `config.json` in MA_DIR. Hot-reloaded every 15s — no restart needed.

## Known Limitations

- **No task-level timeout**: If Claude CLI hangs, task stays `in_progress` indefinitely
- **Save batching**: Hub saves every 5-10s — crash can lose recent tasks
- **Cooperative file locks**: Agents warned about conflicts but not prevented from editing
- **No state machine enforcement**: Invalid status transitions possible via API
- **MCP servers can die silently**: stdio MCP processes not monitored after start
- **Credentials in plaintext**: `credentials.env` stored with chmod 600 but no encryption
- **Config hot-reload partial**: Only `.mcp.json` watches for changes, not `multiagent.json`

See `GRAPH.md` "Known Gaps & Edge Cases" for the full audit (~80 gaps with line references).

## URL Routing — Use the Right MCP Tool

When a user provides a URL, use the matching MCP tool;
- `atlassian.net`, `jira`, `confluence` → Use **Atlassian MCP** tools
- `github.com` → Use **GitHub MCP** tools
- `figma.com` → Use **Figma MCP** tools
- `sentry.io` → Use **Sentry MCP** tools
- `docs.google.com`, `drive.google.com` → Use **Google MCP** tools