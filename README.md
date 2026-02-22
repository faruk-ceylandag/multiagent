# Claude Multi-Agent Collaborate System

A team of AI agents that collaborate to build, test, and ship your code. You give tasks, they handle the rest — architect plans, devs implement, QA tests, reviewer checks.

## Quick Start

```bash
# Install (one-time)
bash install.sh

# Launch in your project
cd your-project
ma
```

Dashboard opens at `http://localhost:{HUB_PORT}`. Send tasks from the chat bar, watch agents work in real-time.

## How It Works

1. **You type a task** — plain language, a Jira link, a Figma URL, whatever you want done
2. **Architect** reads and breaks it into subtasks, delegates to the right agents
3. **Frontend & Backend** implement the code in parallel, each on their own branch
4. **Code Review** — 3 hidden reviewer agents (Logic, Style, Architecture) auto-review in parallel. All 3 must approve.
5. **QA** runs tests and linters automatically — agents iterate until everything passes
6. **UAT** — you approve or reject from the dashboard with feedback
7. **Done** — task is complete

### Task Pipeline

```
to_do → in_progress → code_review → in_testing → uat → done
                ↑          |              |         |
                └──────────┘──────────────┘─────────┘  (rework on reject)
```

- **Code Review**: 3 haiku-model reviewers run in parallel (cheap + fast). If any requests changes, task goes back to dev with specific comments. Max 3 rework cycles before auto-approve.
- **QA**: Automated testing after all reviewers approve.
- **UAT**: You get approve/reject buttons in the dashboard. Reject sends feedback to the dev.
- **Review timeout**: If a reviewer doesn't respond in 15 minutes, auto-approve.

Each agent runs Claude Code CLI under the hood — Sonnet for thinking, Opus for coding.

## Dashboard

Real-time web UI with everything in one place:

- **Logs** — live stream of what each agent is doing
- **Tasks** — 7-column kanban board (To Do → In Progress → Code Review → Testing → UAT → Done / Failed)
- **Inbox** — agent messages, review requests, chat with individual agents
- **Code Review** — review verdicts, comment threads, resolve/unresolve per issue
- **UAT** — approve/reject with feedback directly from task detail modal
- **Git** — branch status, changed files per project
- **Analytics** — cost tracking, token usage, budget limits per agent

## Integrations

Agents connect to your existing tools automatically:

| Service | What agents can do |
|---------|-------------------|
| **GitHub** | Create PRs, manage issues, read repos |
| **Jira / Confluence** | Read tickets, update status, search issues |
| **Sentry** | Debug production errors, read stack traces |
| **Figma** | Inspect designs, extract styles, convert to code |
| **Google Workspace** | Read/write Docs, Sheets, Slides |
| **Context7** | Look up any library's latest documentation |

Connect services from the dashboard with `/connect` — agents auto-detect which tools they need per task.

## Agents Learn and Evolve

The system gets smarter over time — agents don't just run tasks, they learn from them.

**They remember what works.** After each task, agents extract what they learned (patterns, gotchas, tricks). These get stored in a shared registry with quality scores. Good patterns rise, bad ones get pruned automatically.

**They teach each other.** When an agent discovers something useful, all other agents get notified. Before starting a new task, each agent gets briefed with relevant proven patterns and recent learnings from peers.

**They discover new tools on their own.** Drop a new subagent, command, or skill into the `ecosystem/` folder — agents pick it up at the next task, no restart needed. When one agent installs an MCP server (like Sentry or Figma), it broadcasts to the team so everyone gets it.

**Quality is score-based.** Patterns start at score 1. When agents succeed using a pattern, it gets upvoted. When they fail, it gets downvoted. Low-quality patterns are auto-removed. Only battle-tested knowledge survives.

## Configuration

Create `multiagent.json` in your project root:

```json
{
  "agents": [
    {"name": "architect"},
    {"name": "frontend"},
    {"name": "backend"},
    {"name": "qa"},
    {"name": "reviewer-logic", "model": "haiku", "hidden": true},
    {"name": "reviewer-style", "model": "haiku", "hidden": true},
    {"name": "reviewer-arch", "model": "haiku", "hidden": true}
  ],
  "coding_model": "claude-opus-4-6",
  "thinking_model": "claude-sonnet-4-5-20250929",
  "auto_verify": true,
  "budget_limit": 50
}
```

- **Hidden agents** (`"hidden": true`): Run in the background, invisible on dashboard. Used for automated code review.
- **Model shorthand**: `"haiku"`, `"sonnet"`, `"opus"` resolve to full model IDs automatically.
- Config hot-reloads every 15s — no restart needed.

## CLI

```bash
ma                           # start in current dir
ma send backend 'add API'    # send task to specific agent
ma send all 'refactor auth'  # broadcast to all
ma status                    # show agent statuses
ma tasks                     # list tasks
ma tail qa                   # follow agent logs
ma kill                      # stop everything
```

## What Makes It Different

- **Multi-project** — run in multiple projects at once, auto port allocation
- **Auto-verify** — lint + test after every task, agents iterate until tests pass (up to 8 cycles)
- **User stays in control** — changes are staged for your review, nothing gets committed without you
- **Never gives up** — agents try MCP, then curl, then WebFetch, then ask you. Every fallback chain is exhausted before reporting failure
- **Self-healing** — crashed agents restart automatically, sessions recover, stale locks get cleaned up
- **Live ecosystem** — agents discover tools at runtime, learn from each other, share knowledge across the team
- **Budget-safe** — per-agent cost limits, real-time cost tracking on dashboard

## Requirements

- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (`npm i -g @anthropic-ai/claude-code`)
- Python 3.10+
- Node.js 18+ (for MCP servers)

## License

MIT
