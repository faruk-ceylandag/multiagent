---
paths:
  - "agents/**"
---

# Agent Worker Rules

Agents are Claude CLI workers. `worker.py` is the main loop — boot, poll hub, process messages, run tasks.

## Key Files

- `worker.py` — Boot + main loop. ~1000 lines, read carefully before changing
- `context.py` — AgentContext dataclass (all per-agent state)
- `hub_client.py` — Hub API calls: hub_post, hub_get, hub_msg, set_status, pattern/learning helpers
- `claude_runner.py` — Claude CLI execution with streaming output
- `learning.py` — Learning extraction, pattern classification (`classify_learning_category`), ecosystem broadcasts
- `mcp_manager.py` — MCP setup, reload, ensure_mcp, file watching
- `verify.py` — Post-task verification (lint, test, build — up to 8 cycles)
- `git_ops.py` — Branch, commit, rollback, PR operations

## Worker Flow

1. Boot: register with hub, setup MCP, restore session
2. Poll loop: `POST /poll/{name}` every 2s
3. Got messages → filter ecosystem updates → detect task vs chat
4. Task: detect project, branch, inject context (patterns, MCP, ecosystem), call Claude
5. Post-task: verify, vote on patterns, extract learning, stage changes for review

## Patterns & Learning

- `classify_learning_category(text)` maps text to category by keyword matching
- `_build_patterns_block()` creates concise context from proven patterns + peer learnings
- `_refresh_ecosystem()` syncs new tools from ecosystem/ dir at each task start
- `_process_ecosystem_updates()` handles peer broadcasts (pattern_discovered, tool_effective, new_mcp_found)
- Post-task voting: success → +1, failure → -1 on patterns used during the task

## Broadcasting

Use `_broadcast_ecosystem_update(ctx, subtype, data)` from learning.py. Subtypes: `pattern_discovered`, `tool_effective`, `new_mcp_found`.
