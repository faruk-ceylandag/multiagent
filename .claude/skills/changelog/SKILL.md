---
name: changelog
description: "Generate a changelog from recent git commits grouped by category"
argument-hint: "[since-tag-or-date]"
disable-model-invocation: true
---

Generate a changelog from recent git history.

**Argument:** `$ARGUMENTS`

## Instructions

### Step 1: Determine range

- If `$ARGUMENTS` is a git tag (e.g. `v1.0`), use `git log <tag>..HEAD`
- If `$ARGUMENTS` is a date (e.g. `2025-01-01`), use `git log --since="<date>"`
- If `$ARGUMENTS` is a number (e.g. `20`), use `git log -<number>`
- If empty, default to `git log -20`

### Step 2: Collect commits

Run:
```bash
git log <range> --pretty=format:"%h|%s|%an|%ai" --no-merges
```

### Step 3: Categorize

Group each commit by its prefix/intent:

| Category | Indicators |
|----------|-----------|
| Features | `add`, `new`, `implement`, `create` |
| Improvements | `update`, `upgrade`, `improve`, `enhance` |
| Bug Fixes | `fix`, `patch`, `resolve`, `hotfix` |
| Refactoring | `refactor`, `restructure`, `simplify`, `clean` |
| Infrastructure | `ci`, `docker`, `deploy`, `build`, `install` |
| Documentation | `doc`, `readme`, `comment` |

### Step 4: Format output

```markdown
# Changelog

**Range:** <description of range>
**Generated:** <current date>

## Features
- `<hash>` <description> — *<author>*

## Improvements
- `<hash>` <description> — *<author>*

## Bug Fixes
- `<hash>` <description> — *<author>*

## Refactoring
- `<hash>` <description> — *<author>*

## Infrastructure
- `<hash>` <description> — *<author>*
```

Only include categories that have commits. Omit empty sections.