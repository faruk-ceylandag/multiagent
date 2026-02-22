Submit a UAT (User Acceptance Testing) decision for a task.

## Instructions
1. Get the task ID
2. Review the implemented changes
3. Decide: approve or reject

```bash
# Approve
curl -s -X POST "$HUB_URL/tasks/$TASK_ID/uat" \
  -H "Content-Type: application/json" \
  -d '{"action": "approve"}'

# Reject with feedback
curl -s -X POST "$HUB_URL/tasks/$TASK_ID/uat" \
  -H "Content-Type: application/json" \
  -d '{"action": "reject", "feedback": "What needs to change..."}'
```

## Notes
- Approve → task moves to `done`
- Reject → task returns to `in_progress`, dev gets feedback message
- UAT is typically done by the user from the dashboard UI
