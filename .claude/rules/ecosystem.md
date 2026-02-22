---
paths:
  - "ecosystem/**"
---

# Ecosystem Rules

Ecosystem provides the tool stack for agents: MCP servers, subagents, commands, skills, hooks, templates.

## Structure

```
ecosystem/
  setup_ecosystem.py    Boot-time setup + refresh_agent_tools() for runtime discovery
  mcp/                  MCP server definitions and .mcp.json generation
  subagents/            Agent .md definitions (copied to .claude/agents/)
  commands/             Slash command .md files (copied to .claude/commands/)
  skills/               Skill directories (copied to .claude/skills/)
  hooks/                Hook generation (settings.json)
  templates/            CLAUDE.md generation per project
```

## Boot-time Setup

`setup_agent_ecosystem()` runs once per agent at start:
1. Copy subagents, commands, skills to agent's `.claude/` dir
2. Generate hooks config + settings.json
3. Generate .mcp.json with credentials
4. Adopt project-level ecosystem (subagents, commands from workspace projects)

## Runtime Tool Discovery

`refresh_agent_tools(agent_cwd, ma_dir)` — called at each task start by worker:
- Diffs ecosystem/subagents vs .claude/agents — copies new .md files
- Diffs ecosystem/commands vs .claude/commands — copies new .md files
- Diffs ecosystem/skills vs .claude/skills — copies new skill dirs
- Returns list of newly discovered tool names

This means: drop a new .md in ecosystem/subagents/ and agents pick it up at their next task without restart.

## Adding New Tools

- **Subagent**: Add `my-agent.md` to `ecosystem/subagents/`
- **Command**: Add `my-command.md` to `ecosystem/commands/`
- **Skill**: Add `my-skill/` directory to `ecosystem/skills/`
- **MCP server**: Add to `ecosystem/mcp/setup_mcp.py` MCP_SERVERS dict
