---
name: jira_qa_check
description: "Fetch a Jira ticket and generate QA analysis — test scenarios, coverage gaps, regression risks"
argument-hint: "[jira-url or issue-key]"
disable-model-invocation: true
---

Analyse a Jira ticket for QA impact and generate a test plan.

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

### Step 3: QA analysis

1. **Map acceptance criteria to test scenarios** — each AC becomes one or more concrete test cases with expected results
2. **Search existing tests** — grep for related test files, check coverage for affected modules
3. **Identify coverage gaps** — what's tested vs what the ticket requires
4. **Regression risk** — which existing features could break, integration points to verify
5. **Edge cases** — boundary conditions, empty states, concurrent access, error paths
6. **Security review** — authentication, authorization, input validation, injection vectors
7. **Performance review** — N+1 queries, large payloads, missing pagination, caching

### Step 4: Generate the report

Format:

```markdown
# QA Analysis — <ISSUE_KEY>
**Ticket:** <title>
**URL:** <jira_url>
**Priority:** <priority>

## Summary
<2-3 sentence overview of testing scope>

## Acceptance Criteria → Test Scenarios
| AC | Test Scenario | Type | Priority |
|----|--------------|------|----------|
| criterion | test description | Unit/Integration/E2E | P0/P1/P2 |

## Existing Test Coverage
- <relevant test files found>
- <coverage gaps identified>

## Regression Risk Areas
- <modules/features that could break>
- <integration points to verify>

## Edge Cases & Boundary Conditions
- <edge cases to test>

## Security Considerations
- <auth, validation, injection risks>

## Performance Considerations
- <queries, payload sizes, caching concerns>

## Manual Test Plan
1. <step-by-step manual test scenarios>

## Recommended Test Strategy
- **Must test:** <critical paths>
- **Should test:** <important but lower risk>
- **Nice to test:** <edge cases if time permits>
```

### Step 5: Save the report

```bash
mkdir -p "$WORKSPACE/.multiagent/reports"
```

Save to `$WORKSPACE/.multiagent/reports/qa-check_<ISSUE_KEY>_<timestamp>.md`

### Step 6: Send to user

```bash
curl -s -X POST $HUB/messages -H 'Content-Type: application/json' \
  -d '{"sender":"$AGENT_NAME","receiver":"user","content":"<REPORT>","msg_type":"check_report"}'
```
