---
name: figma_to_vue
description: "Convert a Figma design to a Vue component"
argument-hint: "[figma-url] [component-name]"
---

Convert a Figma design into a Vue Single File Component.

**Argument:** `$ARGUMENTS`

## Instructions

### Step 1: Parse arguments

Extract from `$ARGUMENTS`:
- **Figma URL** — contains `figma.com/design/`
- **Component name** (optional) — name for the generated component

If a Figma URL is provided, extract `fileKey` and `nodeId`:
- URL format: `figma.com/design/:fileKey/:fileName?node-id=:nodeId`
- Convert `node-id` format from `1-2` to `1:2`

### Step 2: Fetch Figma design

Use `mcp__figma__get_design_context` with the extracted fileKey and nodeId to get:
- Component structure and layout
- Design tokens (colors, spacing, typography)
- Screenshot for visual reference

If MCP is not available, ask the user to describe the component or provide a screenshot.

### Step 3: Analyze existing project

1. Check for existing design tokens / CSS variables in the project
2. Look at existing components for naming and style conventions
3. Identify if Vue 2 (Options API) or Vue 3 (Composition API) is used
4. Check for utility CSS frameworks (Tailwind, etc.)

### Step 4: Generate Vue component

Create a Vue SFC matching the design:

```vue
<template>
  <div class="component-name">
    <!-- Map Figma layers to HTML elements -->
  </div>
</template>

<script>
export default {
  name: 'ComponentName',
  props: {
    // Props derived from Figma component properties
  },
};
</script>

<style scoped>
/* Map Figma design tokens to CSS */
</style>
```

**Mapping rules:**
- Auto Layout → flexbox (direction, gap, padding)
- Fill Container → flex: 1 or width: 100%
- Hug Contents → natural sizing
- Design tokens → CSS variables where available
- Text content → use `$t()` / `trans()` for i18n

### Step 5: Output

1. Write the component file
2. Print a summary with file path and component props
3. Note any design tokens that need to be added to the project
