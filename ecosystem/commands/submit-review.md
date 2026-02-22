Submit a code review verdict for the current task.

## Instructions
1. Determine the task ID from your current assignment
2. Review the code changes (git diff on the task branch)
3. Decide: APPROVE or REQUEST_CHANGES
4. For each issue found, note: file path, line number, description, severity (critical/warning/info)
5. Submit via hub API:

```bash
curl -s -X POST "$HUB_URL/tasks/$TASK_ID/review" \
  -H "Content-Type: application/json" \
  -d "{\"agent\": \"$AGENT_NAME\", \"verdict\": \"approve|request_changes\", \"comments\": [{\"file\": \"path\", \"line\": 10, \"text\": \"issue\", \"severity\": \"warning\"}]}"
```

## Verdict Rules
- **approve**: Code meets your review standards, no blocking issues
- **request_changes**: Found issues that must be fixed before merge. Include specific comments.
- All 3 reviewers must approve for task to proceed to QA
