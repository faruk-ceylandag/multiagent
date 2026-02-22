Migrate test files from one testing framework to another.

## Instructions

1. Parse arguments:
   - **File/directory path** — source tests to migrate
   - **Target framework** (optional) — default: Playwright

2. Auto-detect the source framework by scanning imports:
   - `from selenium` / `webdriver` → Selenium
   - `cy.` / `cypress` → Cypress
   - `jest.` → Jest
   - `describe(` + `mocha` → Mocha

3. Create a migration plan:
   - Count source files and test cases
   - Map source APIs to target equivalents
   - Identify patterns needing manual review

4. Migrate each file:
   - Convert framework APIs (find_element → locator, etc.)
   - Convert assertions (assertEqual → expect().toBe())
   - Handle sync→async conversion for Playwright
   - Create Page Objects where applicable
   - Add `// MIGRATED FROM: source:line` comments

5. Generate migration report:
```
Migration Complete:
  Source: N files, M tests (Framework)
  Target: N files, M tests (Playwright)
  Converted: X/M (Y%)
  Review needed: Z tests (marked TODO)
```

6. Suggest verification command: `npx playwright test`
