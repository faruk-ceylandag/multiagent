Convert a Figma design to a Vue component.

## Instructions

1. Extract the Figma URL from the arguments
2. Parse `fileKey` and `nodeId` from the URL:
   - `figma.com/design/:fileKey/:fileName?node-id=:nodeId`
   - Convert node-id from `1-2` to `1:2` format
3. Use `mcp__figma__get_design_context` to fetch the design
4. Analyze the project for:
   - Vue version (2 vs 3) — check for `createApp` or `new Vue`
   - Existing design tokens / CSS variables
   - Component naming conventions
   - CSS approach (scoped, modules, Tailwind, etc.)
5. Generate a Vue SFC that:
   - Maps Auto Layout to flexbox
   - Uses existing design tokens where available
   - Follows project naming conventions (BEM, etc.)
   - Uses `$t()` or `trans()` for user-visible text
   - Includes proper props for dynamic content
6. Write the component file and report:
   - File path
   - Props interface
   - Any design tokens that need to be added

If no Figma URL is provided, ask the user for one.
