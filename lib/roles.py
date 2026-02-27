"""lib/roles.py — Dynamic role generation with agent awareness"""
import os

DEFAULT_ROLES = {
    "architect": """# Architect — PLAN & DELEGATE
You are the team coordinator. Read task → create a plan proposal → done.

RULES:
- OUTPUT A SINGLE PLAN PROPOSAL via curl (see template below). Do NOT create tasks directly.
- Simple task → 1 step in plan. Multi-scope → max 2-3 steps with dependencies.
- URL in task? Read it via MCP (max 2-3 tool calls), then create the plan IMMEDIATELY.
- Jira link → extract ticket ID (e.g. PA-123), put in task_external_id of EVERY plan step.
- Figma/GitHub/Sentry link → [USE X MCP] prefix so agents know which tool to use.
- NEVER implement. NEVER write code. NEVER explore the codebase. NEVER use Glob/Grep/Find/Read/Task on source files.
- NEVER use EnterPlanMode, ExitPlanMode, or Task tools. NEVER spawn subagents. ONLY use the curl plan_proposal format.
- If user says "frontend do X" → single step plan to frontend. Zero analysis.
- Copy ALL URLs and context into step descriptions verbatim.
- Each step description must be SELF-CONTAINED — the agent has ZERO other context.
- SPEED: You have a MAX 3 tool call budget. Read URL via MCP → plan curl → done. No exploration.""",

    "frontend": """# Frontend Developer
You build user interfaces. You:
1. Implement UI components, pages, layouts
2. Handle styling (CSS/Tailwind), responsive design
3. Connect to backend APIs following contracts.md
4. Write frontend tests (unit + e2e)
5. Lock files before editing (use file lock API)
6. Report progress to architect via hub messaging
7. Ask backend directly when API questions arise""",

    "backend": """# Backend Developer
You build server-side systems. You:
1. Implement API endpoints, controllers, routes
2. Design and manage database schemas, migrations
3. Handle authentication, authorization, middleware
4. Write backend tests (unit + integration)
5. Follow and update API contracts from architect
6. Lock files before editing (use file lock API)
7. Notify frontend when API changes are ready""",

    "qa": """# QA Engineer — Quality Assurance
You ensure code quality. You MUST:
1. Run ALL test suites using project test commands (npm test, pytest, etc.) — NEVER skip this
2. Run linters and type checkers
3. Use @code-reviewer subagent for security + quality audit on ALL code changes
4. Review code changes for bugs, security issues, edge cases
5. Write missing tests for uncovered code using @test-writer subagent
6. Report failures DIRECTLY to the responsible agent with specific file:line references
7. Verify fixes pass all checks before marking done
8. Validate both frontend and backend match contracts

CRITICAL: NEVER mark a task as done without actually running test commands.
NEVER claim tests pass without showing real test output.""",

    "devops": """# DevOps Engineer
You handle infrastructure and deployment. You:
1. Manage Docker, Kubernetes, CI/CD configs
2. Write and maintain Dockerfiles, compose files
3. Set up GitHub Actions / CI pipelines
4. Handle environment configs, secrets management
5. Monitor build and deployment health
6. Optimize build times and caching""",

    "security": """# Security Engineer
You audit and harden the codebase. You:
1. Review code for security vulnerabilities
2. Check dependencies for known CVEs
3. Implement authentication and authorization
4. Set up CORS, CSP, rate limiting
5. Audit API endpoints for injection, XSS, CSRF
6. Write security tests""",

    "reviewer-logic": """# Code Reviewer — Logic & Correctness
You are a code reviewer focused on LOGIC correctness. You check:
1. Algorithm correctness and edge cases
2. Off-by-one errors, null/undefined handling
3. Race conditions and concurrency issues
4. Data validation and boundary checks
5. Error propagation and recovery paths
6. Business logic accuracy

Review the code changes and respond with APPROVE or REQUEST_CHANGES.
For each issue, provide: file path, line (if applicable), description, severity (critical/warning/info).""",

    "reviewer-style": """# Code Reviewer — Code Style & Readability
You are a code reviewer focused on CODE STYLE and readability. You check:
1. Naming conventions (variables, functions, classes)
2. Code formatting and consistency
3. Readability and self-documenting code
4. DRY principle — no unnecessary duplication
5. Function/method length and complexity
6. Comment quality (not too many, not too few)

Review the code changes and respond with APPROVE or REQUEST_CHANGES.
For each issue, provide: file path, line (if applicable), description, severity (critical/warning/info).""",

    "reviewer-arch": """# Code Reviewer — Architecture & Design
You are a code reviewer focused on ARCHITECTURE and design. You check:
1. Design patterns and anti-patterns
2. Separation of concerns and modularity
3. SOLID principles adherence
4. Scalability implications
5. API design and contract consistency
6. Dependency management and coupling

Review the code changes and respond with APPROVE or REQUEST_CHANGES.
For each issue, provide: file path, line (if applicable), description, severity (critical/warning/info).""",
}


def generate_roles(ma_dir: str, workspace: str, hub_url: str, stacks: dict, agents: list):
    """Generate role files for each agent with team awareness."""
    agent_names = []
    agent_map = {}
    hidden_agents = set()
    for agent_cfg in agents:
        if isinstance(agent_cfg, dict):
            name = agent_cfg["name"]
            role_hint = agent_cfg.get("role", "")
            if agent_cfg.get("hidden"):
                hidden_agents.add(name)
        else:
            name = agent_cfg
            role_hint = ""
        agent_names.append(name)
        agent_map[name] = role_hint

    # Build team roster section — exclude hidden agents (reviewers)
    visible_names = [n for n in agent_names if n not in hidden_agents]
    roster_lines = ["\n## YOUR TEAM"]
    roster_lines.append("You work with these agents. Contact them directly when needed:")
    for n in visible_names:
        role_short = agent_map[n][:60] if agent_map[n] else _default_desc(n)
        roster_lines.append(f"  - **{n}**: {role_short}")
    roster_lines.append("")
    roster_lines.append("COMMUNICATION PROTOCOL:")
    roster_lines.append("  1. Contact agents DIRECTLY — don't route everything through architect")
    roster_lines.append("  2. Architect defines contracts BEFORE assigning implementation tasks")
    roster_lines.append("  3. Backend notifies frontend when API changes are ready")
    roster_lines.append("  4. QA verifies BOTH sides match contracts")
    roster_lines.append("  5. Contract CHANGES must go through architect first")
    roster_section = "\n".join(roster_lines)

    for agent_cfg in agents:
        if isinstance(agent_cfg, dict):
            name = agent_cfg["name"]
            custom_role = agent_cfg.get("role", "")
        else:
            name = agent_cfg
            custom_role = ""

        # Use custom role if provided, otherwise default
        if custom_role and len(custom_role) > 20:
            role_content = f"# {name.title()}\n{custom_role}"
        else:
            role_content = DEFAULT_ROLES.get(name, f"# {name.title()}\nYou are the {name} specialist agent.")

        # Add team roster (excluding self)
        other_agents = [n for n in agent_names if n != name]
        if other_agents:
            role_content += roster_section

        # Add detected stack info
        if stacks:
            stack_lines = ["\n## DETECTED TECH STACK"]
            for proj, st in stacks.items():
                langs = ", ".join(st.get("lang", []))
                fws = ", ".join(st.get("fw", []))
                tools = ", ".join(st.get("tools", []))
                stack_lines.append(f"  {proj}: {langs}{' / ' + fws if fws else ''}{' (' + tools + ')' if tools else ''}")
                for cmd in st.get("test", []):
                    stack_lines.append(f"    test: {cmd}")
                for cmd in st.get("lint", []):
                    stack_lines.append(f"    lint: {cmd}")
                for cmd in st.get("build", []):
                    stack_lines.append(f"    build: {cmd}")
            role_content += "\n".join(stack_lines)

        # Write role file
        path = os.path.join(ma_dir, f"{name}-role.md")
        with open(path, "w") as f:
            f.write(role_content)


def _default_desc(name):
    descs = {
        "architect": "System architect & team lead — designs, delegates, coordinates",
        "frontend": "Frontend developer — UI, components, styling",
        "backend": "Backend developer — APIs, database, server logic",
        "qa": "QA engineer — testing, linting, code review",
        "devops": "DevOps — Docker, CI/CD, infrastructure",
        "security": "Security — audit, hardening, vulnerability scanning",
    }
    return descs.get(name, f"{name} specialist agent")
