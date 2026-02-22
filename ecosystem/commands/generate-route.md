Generate a Vue Router route definition for a new page.

## Instructions

1. Parse arguments for:
   - **Path** — the URL path (e.g. `/users/:id/settings`)
   - **Component** — Vue component name or file
   - **Options** — auth required, permissions, layout, etc.

2. Read the existing router config to understand conventions:
   - Find the router file (typically `src/router/index.js` or `src/router/routes.js`)
   - Note the pattern: lazy loading, meta fields, guard usage, nesting

3. Generate the route definition matching project conventions:
   ```javascript
   {
     path: '/path',
     name: 'route-name',
     component: () => import('@/views/Component.vue'),
     meta: { title: 'Page Title', requiresAuth: true },
   }
   ```

4. Determine where to insert the route:
   - Under a parent route if it's a nested path
   - In the correct section (public vs authenticated routes)

5. Update the router file with the new route

6. If the component file doesn't exist yet, create a basic scaffold

7. Report what was added:
```
Route added:
  Path: /users/:id/settings
  Name: user-settings
  Component: @/views/UserSettings.vue
  Auth: required
```
