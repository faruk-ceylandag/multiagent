Review the current git diff for quality, security, and correctness issues.

## Instructions
1. Run `git diff --cached` (or `git diff HEAD` if nothing staged)
2. For each changed file, check:
   - Security vulnerabilities (injections, XSS, hardcoded secrets)
   - Logic errors and edge cases
   - Performance issues
   - Missing error handling
3. Output a summary with severity levels

## Format
```
[🔴|🟡|🔵] file:line — issue description
  → fix suggestion
```

If everything looks good: "LGTM ✓ — N files reviewed, no issues found"

## Pipeline Integration
This is a manual review command. For automated pipeline reviews, code review is triggered automatically when a task enters `code_review` status — 3 hidden reviewer agents (logic, style, architecture) review in parallel. Use `/submit-review` to submit a verdict programmatically.
