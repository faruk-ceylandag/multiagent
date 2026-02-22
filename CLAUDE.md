# Multi-Agent System

AI agent team that collaborates to build, test, and ship code. Architect delegates, devs implement, QA verifies, reviewer checks.

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

## URL Routing — Use the Right MCP Tool

When a user provides a URL, use the matching MCP tool;
- `atlassian.net`, `jira`, `confluence` → Use **Atlassian MCP** tools
- `github.com` → Use **GitHub MCP** tools
- `figma.com` → Use **Figma MCP** tools
- `sentry.io` → Use **Sentry MCP** tools
- `docs.google.com`, `drive.google.com` → Use **Google MCP** tools