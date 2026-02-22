Generate or fix Playwright E2E tests based on the provided arguments.

## Instructions

1. Parse the argument to determine the action:
   - **URL or page path** → Generate new tests using @playwright-generator
   - **"fix" or "heal" + test file** → Fix broken tests using @playwright-healer
   - **"plan" + description** → Create a test plan using @playwright-planner
   - **No argument** → Analyze `git diff` for changed pages and suggest tests

2. For **test generation** (default):
   - Identify the target page/flow
   - Create Page Object(s) in `tests/pages/`
   - Generate test spec in `tests/`
   - Use locator priority: data-testid > role > text > CSS
   - Include happy path + error cases

3. For **test fixing**:
   - Run the failing test to see the error
   - Diagnose: selector change, timing issue, or page structure change
   - Apply fix and re-run to verify

4. For **test planning**:
   - Create structured test plan with scenarios, priority, and page objects
   - Output plan as markdown (don't write tests yet)

## Output

Summarize what was generated/fixed:
```
[Generated|Fixed|Planned]:
  - file1.spec.ts (N tests)
  - pages/PageObject.ts
Run: npx playwright test <file> --headed
```
