---
name: health_check
description: "Check system health — hub status, agent statuses, budgets, patterns, and flag issues"
disable-model-invocation: true
---

Check the health of the multi-agent system and report issues.

## Instructions

### Step 1: Hub health

```bash
curl -s http://127.0.0.1:8040/health
```

If hub is unreachable, report and stop.

### Step 2: Dashboard snapshot

```bash
curl -s http://127.0.0.1:8040/dashboard
```

From the response, extract and report:

**Agents:**
- List each agent with status (idle/working/offline/unresponsive/rate_limited)
- Flag agents that are unresponsive (silent > 180s)
- Flag agents that are rate-limited
- Show cost per agent

**Tasks:**
- Total tasks, how many in_progress, how many pending
- Flag stuck tasks (in_progress for too long)

**Budget:**
- Total spent vs limit
- Warn if > 80% of budget used

### Step 3: Patterns

```bash
curl -s "http://127.0.0.1:8040/patterns?limit=5"
```

Report top patterns by score and total pattern count.

### Step 4: Recent activity

From dashboard snapshot, check last 10 activity entries for errors or blockers.

### Step 5: Report

Print a clean summary:
```
System Health
  Hub: online
  Agents: 4 total — 2 idle, 1 working, 1 offline
  Tasks: 12 total — 1 in_progress, 3 pending, 8 done
  Budget: $12.50 / $50.00 (25%)
  Patterns: 23 registered, top score: 8

Issues:
  - qa agent unresponsive (last seen 5m ago)
  - Task #15 stuck in_progress for 20m

All clear / N issues found
```
