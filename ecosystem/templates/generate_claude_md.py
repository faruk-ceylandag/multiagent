#!/usr/bin/env python3
"""Generate CLAUDE.md files for each project based on detected stack info.

CLAUDE.md is the most important file for Claude Code — it's read at the start
of every session and provides project-specific context, conventions, and rules.
"""
import json, os, sys

TEMPLATES = {
    "php": {
        "laravel": """## Coding Standards
- PSR-12 coding style
- Use Eloquent ORM, avoid raw queries
- Form requests for validation
- Resource controllers for REST
- Policies for authorization

## Key Commands
```bash
php artisan serve          # Dev server
php artisan test           # Run tests
php artisan migrate        # Run migrations
./vendor/bin/phpstan analyse  # Static analysis
./vendor/bin/pint          # Code formatting
```

## Architecture
- Controllers: `app/Http/Controllers/`
- Models: `app/Models/`
- Routes: `routes/api.php`, `routes/web.php`
- Migrations: `database/migrations/`
- Tests: `tests/Feature/`, `tests/Unit/`
""",
    },
    "javascript": {
        "vue": """## Coding Standards
- Vue 3 Composition API with `<script setup>`
- TypeScript for all new components
- Pinia for state management
- Tailwind CSS for styling

## Key Commands
```bash
npm run dev        # Dev server
npm run build      # Production build
npm run test       # Run tests
npm run lint       # ESLint
```

## Architecture
- Components: `src/components/`
- Views/Pages: `src/views/` or `src/pages/`
- Stores: `src/stores/`
- Composables: `src/composables/`
- Types: `src/types/`
""",
        "react": """## Coding Standards
- Functional components with hooks
- TypeScript for all new code
- CSS Modules or Tailwind CSS
- React Query for server state

## Key Commands
```bash
npm run dev        # Dev server
npm run build      # Production build
npm run test       # Jest tests
npm run lint       # ESLint
```

## Architecture
- Components: `src/components/`
- Pages: `src/pages/`
- Hooks: `src/hooks/`
- Utils: `src/utils/`
- Types: `src/types/`
""",
    },
    "go": {
        "_default": """## Coding Standards
- Standard Go formatting (gofmt)
- Error wrapping with `fmt.Errorf("context: %w", err)`
- Table-driven tests
- Context propagation for cancellation
- Interface-driven design

## Key Commands
```bash
go run .           # Run
go test ./...      # All tests
go vet ./...       # Static analysis
golangci-lint run  # Comprehensive lint
```

## Architecture
- `cmd/` — Entry points
- `internal/` — Private packages
- `pkg/` — Public packages
- `*_test.go` — Tests alongside source
""",
    },
    "python": {
        "_default": """## Coding Standards
- PEP 8 style (enforced by black)
- Type hints for all public functions
- Docstrings for modules and classes
- pytest for testing

## Key Commands
```bash
python -m pytest       # Run tests
python -m black .      # Format
python -m ruff check . # Lint
python -m mypy .       # Type check
```

## Architecture
- `src/` or package root — Source code
- `tests/` — Test files
- `requirements.txt` or `pyproject.toml` — Dependencies
""",
    },
}

def generate_claude_md(project_name, stack_info):
    """Generate CLAUDE.md content for a project."""
    langs = stack_info.get("lang", [])
    frameworks = stack_info.get("fw", [])

    lines = [f"# {project_name}\n"]

    # Stack summary
    lang_str = ", ".join(langs) if langs else "unknown"
    fw_str = ", ".join(frameworks) if frameworks else "none"
    lines.append(f"**Stack:** {lang_str} | **Frameworks:** {fw_str}\n")

    # Add language + framework specific content
    added = False
    for lang in langs:
        lang_templates = TEMPLATES.get(lang, {})
        for fw in frameworks:
            if fw in lang_templates:
                lines.append(lang_templates[fw])
                added = True
                break
        if not added and "_default" in lang_templates:
            lines.append(lang_templates["_default"])
            added = True

    # Test/lint/build commands from stack.json
    test_cmds = stack_info.get("test", [])
    lint_cmds = stack_info.get("lint", [])
    build_cmds = stack_info.get("build", [])

    if test_cmds or lint_cmds or build_cmds:
        lines.append("\n## Project Commands\n```bash")
        for c in test_cmds:
            lines.append(f"{c}  # Test")
        for c in lint_cmds:
            lines.append(f"{c}  # Lint")
        for c in build_cmds:
            lines.append(f"{c}  # Build")
        lines.append("```\n")

    # Common rules
    lines.append("""## Rules for AI Agents
- ALWAYS `cd` into this project directory before any work
- Run tests after every change
- Don't modify files outside this project
- Commit with conventional commits: `feat:`, `fix:`, `refactor:`, `test:`, `docs:`
- Ask for clarification if requirements are ambiguous

## URL Routing — Use the Right MCP Tool
When a user provides a URL, use the matching MCP tool — NEVER try to browse, fetch, or search the codebase for it:
- `atlassian.net`, `jira`, `confluence` → Use **Atlassian MCP** tools
- `github.com` → Use **GitHub MCP** tools
- `figma.com` → Use **Figma MCP** tools
- `sentry.io` → Use **Sentry MCP** tools
- `docs.google.com`, `drive.google.com` → Use **Google MCP** tools
Do NOT open these URLs with WebFetch, curl, or search for them in the repo.
""")

    return "\n".join(lines)


def write_claude_md_files(workspace, stack_json_path):
    """Write CLAUDE.md to each project that has stack info."""
    try:
        with open(stack_json_path) as f:
            stacks = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []

    written = []
    for project, info in stacks.items():
        project_dir = os.path.join(workspace, project)
        if not os.path.isdir(project_dir):
            continue

        claude_md_path = os.path.join(project_dir, "CLAUDE.md")

        # Don't overwrite existing CLAUDE.md
        if os.path.exists(claude_md_path):
            continue

        content = generate_claude_md(project, info)
        with open(claude_md_path, "w") as f:
            f.write(content)
        written.append(project)

    return written


if __name__ == "__main__":
    workspace = sys.argv[1] if len(sys.argv) > 1 else "."
    stack_path = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(workspace), "stack.json")
    written = write_claude_md_files(workspace, stack_path)
    for p in written:
        print(f"  ✓ {p}/CLAUDE.md")
    if not written:
        print("  No new CLAUDE.md files needed")
