---
allowed-tools: Read, Write, Edit, Bash(*), Glob, Grep
description: Fix a specific issue or bug by description
---

Fix the following issue: $ARGUMENTS

## Steps
1. Understand the issue — read relevant code and reproduce if possible
2. Identify root cause
3. Implement the fix with minimal changes
4. Write/update tests for the fix
5. Run existing tests to ensure no regression
6. Commit with message: "fix: [brief description]"

## Rules
- Don't refactor unrelated code
- Keep the fix focused and minimal
- Always verify the fix works before committing
