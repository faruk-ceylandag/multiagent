---
name: playwright_test
description: "Generate Playwright E2E tests from a URL, test plan, or user flow description"
argument-hint: "[URL or test plan description]"
---

Generate Playwright E2E tests with Page Object Model pattern.

**Argument:** `$ARGUMENTS`

## Instructions

### Step 1: Parse input

Determine what was provided in `$ARGUMENTS`:
- **URL** — navigate and analyze the page to create tests
- **Test plan** — structured description of scenarios to test
- **User flow** — natural language description of what to test
- **File path** — existing page to write tests for

### Step 2: Analyze the target

1. If a URL is provided, use Playwright MCP to navigate and inspect the page
2. Identify key interactive elements (forms, buttons, navigation)
3. Map the user flows that should be tested
4. Check for existing Page Objects in the project

### Step 3: Create Page Objects

For each page/component involved:
1. Create a Page Object class in `tests/pages/` (or project convention)
2. Define locators using priority: data-testid > role > text > CSS
3. Add action methods for common interactions

```typescript
// tests/pages/ExamplePage.ts
import { Page, Locator } from '@playwright/test';

export class ExamplePage {
  readonly page: Page;
  readonly heading: Locator;
  readonly submitBtn: Locator;

  constructor(page: Page) {
    this.page = page;
    this.heading = page.getByRole('heading', { name: 'Example' });
    this.submitBtn = page.getByTestId('submit-btn');
  }

  async navigate() {
    await this.page.goto('/example');
  }

  async submit() {
    await this.submitBtn.click();
  }
}
```

### Step 4: Generate test specs

Create test files in `tests/` following this structure:

```typescript
import { test, expect } from '@playwright/test';
import { ExamplePage } from './pages/ExamplePage';

test.describe('Example Feature', () => {
  let examplePage: ExamplePage;

  test.beforeEach(async ({ page }) => {
    examplePage = new ExamplePage(page);
    await examplePage.navigate();
  });

  test('should display heading', async () => {
    await expect(examplePage.heading).toBeVisible();
  });

  test('should submit form successfully', async ({ page }) => {
    await examplePage.submit();
    await expect(page).toHaveURL(/\/success/);
  });
});
```

### Step 5: Verify

1. Check that test files are syntactically valid
2. List all generated files and test count
3. Suggest running: `npx playwright test <spec-file> --headed` for visual verification

### Step 6: Output

Print a summary:
```
Generated:
  - tests/pages/ExamplePage.ts (Page Object)
  - tests/example.spec.ts (4 tests)

Run: npx playwright test tests/example.spec.ts --headed
```
