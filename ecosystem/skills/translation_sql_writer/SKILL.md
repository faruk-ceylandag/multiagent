---
name: translation_sql_writer
description: "Scan code for hardcoded strings and trans() keys, generate SQL INSERTs"
argument-hint: "[file-path or trans-key] [figma-url or English text mappings]"
---

Generate SQL INSERT statements for new translation keys and identify hardcoded strings that need i18n.

**Argument:** `$ARGUMENTS`

## Instructions

### Step 1: Parse arguments and resolve English text

`$ARGUMENTS` may contain a **file path or trans-key**, and optionally a **text source** (Figma URL or hardcoded English text).

#### Determine the text source

Parse `$ARGUMENTS` to identify which text source is provided:

1. **Figma URL** — argument contains `figma.com/design/` URL:
   - Extract `fileKey` and `nodeId` from the URL
   - Use `mcp__figma__get_design_context` (or `mcp__figma__get_screenshot` as fallback) to fetch the design
   - Collect all visible text strings from the Figma node — these are the **authoritative English texts**
   - Match `trans()` keys to Figma text by usage context (placeholder → input text, headerName → column header, etc.)

2. **Hardcoded English text in prompt** — argument contains `key = "text"` or `key: "text"` mappings, or plain English descriptions alongside keys/file paths. Examples:
   - `/translation_sql_writer statistics.my-tooltip = "Data is available from {date}"`
   - `/translation_sql_writer src/File.vue statistics.my-key: "Hello World", statistics.other-key: "Goodbye"`
   - `/translation_sql_writer statistics.ribbon-text "Engagement metrics start from {date}"`
   - Any free-text English provided alongside a trans key — use it as `en_text`

3. **No text source** — only a file path, trans-key, or empty:
   - For hardcoded strings found in code, the string itself is the English text
   - For `trans('key')` calls, flag with `⚠️` and ask user to confirm English text

#### Determine the scan target

- If `$ARGUMENTS` contains a **file path**, scan that file for `trans()` calls and hardcoded strings
- If `$ARGUMENTS` contains a **translation key** (with or without English text), use that directly
- If `$ARGUMENTS` is **empty**, scan `git diff` for newly added `trans()` calls AND hardcoded strings

**Priority:** User-provided text (prompt or Figma) > hardcoded string from code > guessed text. Never guess when a source is available.

### Step 2: Hardcoded string detection patterns

Scan for these patterns in `.vue` and `.js` files:

| Pattern | Regex hint | Example |
|---------|-----------|---------|
| `placeholder="..."` | `placeholder="[A-Z][a-zA-Z\s]+"` | `placeholder="Enter Duration"` |
| `placeholder-text="..."` | `placeholder-text="[A-Z][^"]*"` | `placeholder-text="Select Variants"` |
| `state-message="..."` | `state-message="[A-Z][^"]*"` | `state-message="Select one or more..."` |
| `label="..."` (non-bound) | `[^:]label="[A-Z][^"]*"` | `label="Campaign Name"` |
| `description="..."` | `description="[A-Z][^"]*"` | `description="Choose a strategy"` |
| `text="..."` (non-bound) | `[^:]text="[A-Z][^"]*"` | `text="No data available"` |
| `headerName: '...'` | `headerName:\s*['"][A-Z][^'"]+` | `headerName: 'Actions'` |
| `title: '...'` (in data/computed) | `title:\s*['"][A-Z][^'"]+['"]` | `title: 'Algorithms and Method'` |
| Hardcoded status strings | Object values like `'Active'`, `'Passive'` | `0: 'Passive'` |

**Exclude** from detection:
- Strings that are already using `trans()` or `:attribute="trans('...')"` binding
- CSS class names, HTML attributes (id, class, ref, data-testid, data-test-id)
- Component names, event names, prop names
- Technical strings (URLs, file paths, enum values, store names)
- Single-word strings that are clearly non-translatable (e.g. `type="primary"`)

### Step 3: Generate translation key names

Follow existing codebase conventions for key naming:
- **Prefix by domain**: `statistics.`, `products.`, `date-time.`, `discovery-strategy.`, `algorithm-panel.`
- **Format**: `domain.kebab-case-description` (e.g. `products.enter-duration`, `statistics.actions`)
- **Infer prefix** from the file location:
  - `organisms/smart-recommender/` → `products.`
  - `organisms/smart-recommender-analytics*/` → `statistics.`
  - `organisms/recommendation-strategies/` → `discovery-strategy.`
  - `organisms/recommendation-algorithms/` → `algorithm-panel.`

### Step 4: Generate SQL

The translation table is `translator_translations` with columns: `locale`, `namespace`, `group`, `item`, `text`, `unstable`, `locked`, `created_at`, `updated_at`.

- **`locale`**: always `'en'`
- **`namespace`**: always `'*'`
- **`group`**: the prefix before the first `.` in the trans key (e.g. `products`, `statistics`, `date-time`)
- **`item`**: everything after the first `.` in the trans key (e.g. `enter-duration`, `ribbon-text`)
- **`text`**: the English text
- **`unstable`**: always `'0'`
- **`locked`**: always `'1'`

Combine all keys into a **single INSERT** statement with multiple VALUE rows:
```sql
INSERT INTO `translator_translations` (`locale`, `namespace`, `group`, `item`, `text`, `unstable`, `locked`, `created_at`, `updated_at`)
VALUES
    ('en', '*', 'products', 'enter-duration', 'Enter Duration', '0', '1', NOW(), NOW()),
    ('en', '*', 'statistics', 'engagement-tooltip', 'Engagement data from {date}', '0', '1', NOW(), NOW()),
    ('en', '*', 'statistics', 'ribbon-text', 'Metrics available from {date}', '0', '1', NOW(), NOW());
```

- For keys with interpolation (`{date}`, `{count}`), preserve placeholders in `text`
- If English text was provided (via prompt or Figma), use it as `text`
- If no text source is available for a `trans()` key, flag with `⚠️` and ask the user to confirm

### Step 5: Generate revert SQL

Generate a single `DELETE` statement that removes all inserted keys, so the migration can be rolled back:

```sql
DELETE FROM `translator_translations`
WHERE `locale` = 'en'
  AND `namespace` = '*'
  AND (`group`, `item`) IN (
      ('products', 'enter-duration'),
      ('statistics', 'engagement-tooltip'),
      ('statistics', 'ribbon-text')
  );
```

- Include every `(group, item)` pair from Step 4
- Use a single `DELETE` with `IN (...)` — not one statement per key

### Step 6: Generate replacement code

For each hardcoded string found, also output the **replacement code**:

```
File: src/components/organisms/.../GoalDetails.vue
Line: 85
- Before: placeholder="Enter Duration"
- After:  :placeholder="trans('products.enter-duration')"
- SQL:    INSERT INTO `translator_translations` (`locale`, `namespace`, `group`, `item`, `text`, `unstable`, `locked`, `created_at`, `updated_at`)
          VALUES ('en', '*', 'products', 'enter-duration', 'Enter Duration', '0', '1', NOW(), NOW());
```

**Replacement rules:**
- Template attributes: `attr="Text"` → `:attr="trans('key')"`
- Options API data/computed: `'Text'` → `this.trans('key')`
- Composition API: `'Text'` → `trans('key')` (ensure trans is imported)

### Step 7: Output

Write the results to a markdown file at `translations-output.md` in the project root. Use the following structure:

```markdown
# Translation SQL Output

> Generated from: `<file-path or git diff>`
> Date: YYYY-MM-DD

## Summary

| # | File | Hardcoded String / trans() Key | English Text | Source | Type |
|---|------|-------------------------------|-------------|--------|------|
| 1 | GoalDetails.vue:85 | Enter Duration | Enter Duration | code | placeholder |
| 2 | Filters.vue:12 | trans('statistics.date-range') | Date Range | figma | label |
| 3 | — | trans('statistics.my-tooltip') | Data is available from {date} | prompt | tooltip |
| 4 | ... | ... | ... | ... | ... |

## SQL Statements

\```sql
INSERT INTO `translator_translations` (`locale`, `namespace`, `group`, `item`, `text`, `unstable`, `locked`, `created_at`, `updated_at`)
VALUES
    ('en', '*', 'products', 'enter-duration', 'Enter Duration', '0', '1', NOW(), NOW()),
    ('en', '*', 'statistics', 'engagement-tooltip', 'Engagement data from {date}', '0', '1', NOW(), NOW()),
    ('en', '*', 'statistics', 'ribbon-text', 'Metrics available from {date}', '0', '1', NOW(), NOW());
\```

## Revert SQL

\```sql
DELETE FROM `translator_translations`
WHERE `locale` = 'en'
  AND `namespace` = '*'
  AND (`group`, `item`) IN (
      ('products', 'enter-duration'),
      ('statistics', 'engagement-tooltip'),
      ('statistics', 'ribbon-text')
  );
\```

## Replacements

### `<file-path>`

| Line | Before | After |
|------|--------|-------|
| 85 | `placeholder="Enter Duration"` | `:placeholder="trans('products.enter-duration')"` |
| ... | ... | ... |
```

After writing the file, inform the user of the file path and print a brief count (e.g. "Found 5 hardcoded strings, wrote translations-output.md").
