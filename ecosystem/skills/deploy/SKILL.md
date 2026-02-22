---
name: deploy
description: "Build, verify, and deploy the current project using detected stack commands"
argument-hint: "[project name or environment: staging|production]"
disable-model-invocation: true
---

Build, verify, and deploy a project.

**Argument:** `$ARGUMENTS`

## Instructions

### Step 1: Identify the project

Parse `$ARGUMENTS` for a project name or environment target (staging/production).
If no project name, use the current working directory.

Read `stack.json` from the MA_DIR to get the project's stack info:
```bash
cat "$MA_DIR/stack.json" 2>/dev/null
```

### Step 2: Pre-flight checks

1. **Check git status** — ensure working tree is clean (no uncommitted changes)
2. **Check branch** — confirm you're on the expected branch (main/develop/feature)
3. **Run lint** — execute the project's lint commands from stack.json
4. **Run tests** — execute the project's test commands from stack.json
5. If any check fails, stop and report what failed

### Step 3: Build

Run the project's build commands from stack.json:
```bash
# Typical patterns (auto-detected):
npm run build        # JS/TS
python -m build      # Python
go build ./...       # Go
php artisan optimize # Laravel
```

If build fails, report the error and stop.

### Step 4: Deploy

Based on `$ARGUMENTS` environment target:

- **staging** — deploy to staging environment
- **production** — deploy to production (require explicit confirmation)
- **no target** — just build and verify, don't deploy

Look for deploy commands in:
1. `stack.json` deploy field
2. `package.json` scripts (deploy, deploy:staging, deploy:prod)
3. `Makefile` targets (deploy, release)
4. `.github/workflows/` for CI/CD reference

### Step 5: Report

Print summary:
```
Deploy complete:
  Project: project-name
  Branch: main
  Lint: passed
  Tests: 42 passed, 0 failed
  Build: success
  Deploy: staging (or: skipped — dry run)
```
