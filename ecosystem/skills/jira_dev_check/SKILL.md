---
name: jira_dev_check
description: "Fetch a Jira ticket and analyse codebase impact — affected files, APIs, schema changes, risks"
argument-hint: "[jira-url or issue-key]"
disable-model-invocation: true
---

Analyse a Jira ticket for codebase impact and generate a dev impact report.

**Argument:** `$ARGUMENTS`

## Instructions

### Step 1: Parse the Jira reference

Extract the issue key from `$ARGUMENTS`:
- If it's a URL like `https://xxx.atlassian.net/browse/PROJ-123`, extract `PROJ-123`
- If it's already an issue key like `PROJ-123`, use it directly
- If neither, report an error and stop

### Step 2: Fetch the Jira ticket

Use the Atlassian MCP tool to fetch the ticket:
```
mcp__atlassian__getJiraIssue with issueIdOrKey="<ISSUE_KEY>"
```

Extract:
- Title, description, acceptance criteria
- Subtasks and linked issues
- Priority, labels, components, fix version

### Step 3: Codebase impact analysis

Search the codebase for affected areas:

1. **Keyword search** — grep for feature names, component names, API endpoint paths mentioned in the ticket
2. **File structure** — check models, routes, controllers, services, middleware, configs
3. **Import chains** — trace which modules depend on affected files
4. **API surface** — identify endpoints that would need creation or modification
5. **Database/schema** — check models, migrations, seeders for schema changes
6. **Configuration** — check env files, config files, feature flags

### Step 4: Generate the report

Format:

```markdown
# Dev Impact Analysis — <ISSUE_KEY>
**Ticket:** <title>
**URL:** <jira_url>
**Priority:** <priority>

## Summary
<2-3 sentence overview>

## Affected Files
| File | Module | Expected Changes | Risk |
|------|--------|-----------------|------|
| path | module | description | Low/Med/High |

## API Changes
- <endpoints to modify or create>

## Database / Schema Changes
- <model or migration changes>

## Dependencies
- <new packages, affected internal modules>

## Architecture Impact
- <how this fits existing patterns, refactoring needed>

## Risk Assessment
- **Complexity:** Low/Medium/High
- **Regression risk:** <areas that could break>
- **Cross-cutting concerns:** <auth, caching, logging, etc.>

## Implementation Notes
- <suggested approach, gotchas, order of operations>
```

### Step 5: Save the report

```bash
mkdir -p "$WORKSPACE/.multiagent/reports"
```

Save to `$WORKSPACE/.multiagent/reports/dev-check_<ISSUE_KEY>_<timestamp>.md`

### Step 6: Send to user

```bash
curl -s -X POST $HUB/messages -H 'Content-Type: application/json' \
  -d '{"sender":"$AGENT_NAME","receiver":"user","content":"<REPORT>","msg_type":"check_report"}'
```
