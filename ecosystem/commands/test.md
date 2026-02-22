---
allowed-tools: Read, Write, Edit, Bash(*), Glob, Grep
description: Generate tests for recently changed files
---

Write tests for the recently changed files.

## Steps
1. Run `git diff --name-only HEAD~1` to find changed files
2. For each changed file, identify the testing pattern used in this project
3. Write comprehensive tests covering:
   - Happy path (normal usage)
   - Edge cases (empty input, null, boundaries)
   - Error handling (invalid input, failures)
4. Place tests following project conventions
5. Run the tests and fix any failures

## Rules
- Match existing test style and framework
- Don't duplicate existing test coverage
- Use descriptive test names
- Mock external dependencies
