---
name: test_migration
description: "Migrate test files between testing frameworks (Selenium to Playwright, Jest to Vitest, etc.)"
argument-hint: "[source-file or directory] [target-framework]"
---

Migrate test files from one testing framework to another.

**Argument:** `$ARGUMENTS`

## Instructions

### Step 1: Parse arguments

Extract from `$ARGUMENTS`:
- **Source**: file path or directory containing tests to migrate
- **Target framework** (optional): playwright, vitest, jest, etc. Default: Playwright

If only a file/directory is provided, auto-detect the source framework.

### Step 2: Detect source framework

Scan source files for framework indicators:

| Framework | Indicators |
|-----------|-----------|
| Selenium (Python) | `from selenium`, `webdriver`, `By.`, `find_element` |
| Selenium (Java) | `import org.openqa.selenium`, `WebDriver` |
| Cypress | `cy.`, `describe(`, `cypress/` |
| Protractor | `browser.`, `element(by.`, `protractor` |
| Jest | `jest.`, `describe(`, `it(`, `@jest` |
| Mocha | `mocha`, `describe(`, `it(`, `chai` |

### Step 3: Create migration plan

1. **Inventory** source tests: count files, test cases, helpers
2. **Map APIs**: source framework API → target framework API
3. **Identify challenges**: patterns without direct equivalents
4. **Plan file structure**: output directory and naming

### Step 4: Migrate each file

For each source test file:

1. Read and parse the test structure (describes, tests, setup/teardown)
2. Convert framework-specific APIs to target equivalents
3. Convert assertion syntax
4. Add proper imports for target framework
5. Add `// MIGRATED FROM: source_file:line` comments
6. Handle async patterns (Selenium sync → Playwright async)

### Step 5: Generate shared utilities

If source tests use shared helpers/fixtures:
1. Create equivalent fixtures in the target framework
2. Create shared Page Objects if applicable
3. Generate a config file for the target framework

### Step 6: Verify and report

1. Run syntax check on generated files
2. Create a migration report:

```markdown
# Migration Report

## Summary
- Source: 15 files, 89 tests (Selenium/Python)
- Target: 15 files, 89 tests (Playwright/TypeScript)
- Converted: 85/89 (95.5%)
- Needs review: 4 tests (marked with TODO)

## Files
| Source | Target | Tests | Status |
|--------|--------|-------|--------|
| test_login.py | login.spec.ts | 5/5 | ✓ |
| test_dashboard.py | dashboard.spec.ts | 8/8 | ✓ |

## Manual Review Needed
- test_dashboard.spec.ts:45 — Custom wait pattern needs verification
```

3. Suggest running: `npx playwright test` to verify migrations
