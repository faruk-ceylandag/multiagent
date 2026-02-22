---
allowed-tools: Read, Glob, Grep, Bash(grep *, find *, git *)
description: Scan codebase for security vulnerabilities
---

Perform a security scan on the current project.

## Checks
1. **Hardcoded secrets**: grep for API keys, passwords, tokens in source
   ```bash
   grep -rn "password\|secret\|api_key\|token\|AUTH" --include="*.py" --include="*.js" --include="*.ts" --include="*.php" --include="*.go" --include="*.env" . | grep -v node_modules | grep -v vendor | grep -v ".git"
   ```
2. **SQL injection**: raw query construction with string interpolation
3. **XSS**: unescaped user input in templates/HTML
4. **CSRF**: missing CSRF protection on state-changing routes
5. **Dependencies**: check for known vulnerable packages
6. **Permissions**: overly permissive file/directory permissions
7. **Auth**: missing authentication on sensitive endpoints

## Output
```
SECURITY SCAN REPORT
====================
🔴 CRITICAL: [count]
🟡 WARNING: [count]
🔵 INFO: [count]

[detailed findings with file:line references]
```
