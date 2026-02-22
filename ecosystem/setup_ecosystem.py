#!/usr/bin/env python3
"""
Multi-Agent Ecosystem Setup
============================
Sets up Claude Code's full feature stack for each agent:
  1. MCP servers (GitHub, Context7, Sentry, Sequential Thinking)
  2. Subagents (code-reviewer, explorer, test-writer, db-reader)
  3. Hooks (auto-format, auto-lint, file-lock, notification)
  4. Slash commands (/review, /fix-issue, /test, /security-scan)
  5. CLAUDE.md per project (stack-aware context)

Called from start.py during boot.
"""
import json
import os
import shutil
import sys

ECOSYSTEM_DIR = os.path.dirname(os.path.abspath(__file__))

def setup_shared_ecosystem(ma_dir, workspace):
    """Set up shared ecosystem content (agents, commands, skills) once for all agents.

    Content is stored in MA_DIR/.claude/ and symlinked from each agent's session dir.
    """
    shared_claude = os.path.join(ma_dir, ".claude")
    results = []

    # 1. Copy subagents to shared location
    subagents_src = os.path.join(ECOSYSTEM_DIR, "subagents")
    agents_dst = os.path.join(shared_claude, "agents")
    os.makedirs(agents_dst, exist_ok=True)
    if os.path.isdir(subagents_src):
        for f in os.listdir(subagents_src):
            if f.endswith(".md"):
                shutil.copy2(os.path.join(subagents_src, f), os.path.join(agents_dst, f))

    # 2. Copy slash commands to shared location
    commands_src = os.path.join(ECOSYSTEM_DIR, "commands")
    commands_dst = os.path.join(shared_claude, "commands")
    os.makedirs(commands_dst, exist_ok=True)
    if os.path.isdir(commands_src):
        for f in os.listdir(commands_src):
            if f.endswith(".md"):
                shutil.copy2(os.path.join(commands_src, f), os.path.join(commands_dst, f))

    # 3. Copy skills to shared location
    skills_src = os.path.join(ECOSYSTEM_DIR, "skills")
    skills_dst = os.path.join(shared_claude, "skills")
    os.makedirs(skills_dst, exist_ok=True)
    skill_count = 0
    if os.path.isdir(skills_src):
        for skill_name in os.listdir(skills_src):
            skill_dir = os.path.join(skills_src, skill_name)
            if not os.path.isdir(skill_dir):
                continue
            dst_dir = os.path.join(skills_dst, skill_name)
            os.makedirs(dst_dir, exist_ok=True)
            for f in os.listdir(skill_dir):
                shutil.copy2(os.path.join(skill_dir, f), os.path.join(dst_dir, f))
            skill_count += 1

    # 4. Adopt project-level ecosystem (subagents, commands from workspace projects)
    try:
        for proj in os.listdir(workspace):
            proj_dir = os.path.join(workspace, proj)
            if not os.path.isdir(proj_dir) or proj.startswith("."):
                continue
            proj_agents_dir = os.path.join(proj_dir, ".claude", "agents")
            if os.path.isdir(proj_agents_dir):
                for f in os.listdir(proj_agents_dir):
                    if f.endswith(".md") and not os.path.exists(os.path.join(agents_dst, f)):
                        shutil.copy2(os.path.join(proj_agents_dir, f), os.path.join(agents_dst, f))
            proj_cmds_dir = os.path.join(proj_dir, ".claude", "commands")
            if os.path.isdir(proj_cmds_dir):
                for f in os.listdir(proj_cmds_dir):
                    if f.endswith(".md") and not os.path.exists(os.path.join(commands_dst, f)):
                        shutil.copy2(os.path.join(proj_cmds_dir, f), os.path.join(commands_dst, f))
    except Exception:
        pass  # Discovery is best-effort, don't break boot

    agent_count = len([f for f in os.listdir(agents_dst) if f.endswith(".md")])
    cmd_count = len([f for f in os.listdir(commands_dst) if f.endswith(".md")])
    results.append(f"  subagents: {agent_count} installed")
    results.append(f"  commands: {cmd_count} installed")
    if skill_count:
        results.append(f"  skills: {skill_count} installed")

    return results


def setup_agent_ecosystem(agent_name, agent_cwd, ma_dir, workspace, hub_url, stack_info=None):
    """Per-agent setup: symlink shared content + generate agent-specific configs."""
    results = []

    claude_dir = os.path.join(agent_cwd, ".claude")
    os.makedirs(claude_dir, exist_ok=True)

    # Symlink shared directories (agents, commands, skills)
    shared_claude = os.path.join(ma_dir, ".claude")
    for subdir in ("agents", "commands", "skills"):
        shared_path = os.path.join(shared_claude, subdir)
        agent_path = os.path.join(claude_dir, subdir)
        if not os.path.isdir(shared_path):
            continue
        # Remove old non-symlink directory
        if not os.path.islink(agent_path) and os.path.isdir(agent_path):
            shutil.rmtree(agent_path)
        # Atomic symlink swap: create tmp symlink, then os.replace()
        rel_target = os.path.relpath(shared_path, claude_dir)
        tmp_link = agent_path + ".tmp"
        try:
            os.symlink(rel_target, tmp_link)
            os.replace(tmp_link, agent_path)
        except OSError:
            # Fallback: remove + create
            try: os.unlink(tmp_link)
            except OSError: pass
            if os.path.islink(agent_path):
                os.unlink(agent_path)
            os.symlink(rel_target, agent_path)

    # Generate hooks + settings.json (per-agent — permissions differ by role)
    if stack_info is None:
        stack_info = _load_stack(ma_dir)

    from ecosystem.hooks.setup_hooks import generate_hooks_config, generate_settings_json
    hooks = generate_hooks_config(stack_info, hub_url, agent_name)
    settings = generate_settings_json(hooks, agent_name=agent_name)
    settings_path = os.path.join(claude_dir, "settings.json")
    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)
    hook_count = len(hooks.get("PreToolUse", [])) + len(hooks.get("PostToolUse", []))
    results.append(f"  hooks: {hook_count} configured")

    # MCP config (per-agent — includes credentials)
    from ecosystem.mcp.setup_mcp import generate_mcp_json
    cfg = _load_config(ma_dir)
    _creds = {}
    _creds_file = os.path.join(ma_dir, "credentials.env")
    if os.path.exists(_creds_file):
        try:
            with open(_creds_file) as _cf:
                for _line in _cf:
                    _line = _line.strip()
                    if _line and "=" in _line and not _line.startswith("#"):
                        _k, _v = _line.split("=", 1)
                        _creds[_k.strip()] = _v.strip()
        except Exception:
            pass
    mcp_data = generate_mcp_json(cfg, _creds)
    mcp_path = os.path.join(agent_cwd, ".mcp.json")
    with open(mcp_path, "w") as f:
        json.dump(mcp_data, f, indent=2)
    os.chmod(mcp_path, 0o600)
    mcp_count = len(mcp_data.get("mcpServers", {}))
    results.append(f"  mcp: {mcp_count} servers")

    return results


def setup_workspace_claudemd(workspace, ma_dir):
    """Generate CLAUDE.md for projects that don't have one."""
    from ecosystem.templates.generate_claude_md import write_claude_md_files
    stack_path = os.path.join(ma_dir, "stack.json")
    return write_claude_md_files(workspace, stack_path)


def _load_stack(ma_dir):
    try:
        with open(os.path.join(ma_dir, "stack.json")) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _load_config(ma_dir):
    for name in ["multiagent.json", "config.json"]:
        try:
            with open(os.path.join(ma_dir, name)) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def refresh_agent_tools(agent_cwd, ma_dir):
    """Runtime tool discovery: sync new subagents/commands/skills from ecosystem dir.
    Returns list of newly discovered tool names."""
    claude_dir = os.path.join(agent_cwd, ".claude")
    new_tools = []

    def _needs_update(src_file, dst_file):
        """Check if source is newer than destination."""
        if not os.path.exists(dst_file):
            return True
        try:
            return os.path.getmtime(src_file) > os.path.getmtime(dst_file)
        except OSError:
            return False

    # Subagents
    src = os.path.join(ECOSYSTEM_DIR, "subagents")
    dst = os.path.join(claude_dir, "agents")
    if os.path.isdir(src) and os.path.isdir(dst):
        for f in os.listdir(src):
            if f.endswith(".md") and _needs_update(os.path.join(src, f), os.path.join(dst, f)):
                shutil.copy2(os.path.join(src, f), os.path.join(dst, f))
                new_tools.append(f"subagent:{f[:-3]}")

    # Commands
    src = os.path.join(ECOSYSTEM_DIR, "commands")
    dst = os.path.join(claude_dir, "commands")
    if os.path.isdir(src) and os.path.isdir(dst):
        for f in os.listdir(src):
            if f.endswith(".md") and _needs_update(os.path.join(src, f), os.path.join(dst, f)):
                shutil.copy2(os.path.join(src, f), os.path.join(dst, f))
                new_tools.append(f"command:{f[:-3]}")

    # Skills
    src = os.path.join(ECOSYSTEM_DIR, "skills")
    dst = os.path.join(claude_dir, "skills")
    if os.path.isdir(src):
        os.makedirs(dst, exist_ok=True)
        for skill_name in os.listdir(src):
            skill_src = os.path.join(src, skill_name)
            skill_dst = os.path.join(dst, skill_name)
            if os.path.isdir(skill_src) and not os.path.isdir(skill_dst):
                os.makedirs(skill_dst, exist_ok=True)
                for f in os.listdir(skill_src):
                    shutil.copy2(os.path.join(skill_src, f), os.path.join(skill_dst, f))
                new_tools.append(f"skill:{skill_name}")

    return new_tools


def get_ecosystem_prompt_additions(agent_name, ma_dir, hub_url):
    """DEPRECATED: Use get_smart_hints() instead for token-efficient prompts."""
    return ""  # No longer dump everything


def get_smart_hints(task_content, project_name=None, ma_dir="", hub_url="", role=""):
    """Analyze task content and return relevant ecosystem hints.

    Role-aware: QA gets mandatory tool instructions, devs get contextual hints.
    Token budget: aim for <300 tokens.
    """
    if not task_content:
        return ""

    low = task_content.lower()
    rules = []   # Mandatory REQUIRED: instructions
    hints = []   # Optional TIP: suggestions

    # ── Role-specific mandatory rules ──
    if role == "qa":
        rules.append("REQUIRED: Run ALL test suites and linters BEFORE marking task as done.")
        rules.append("REQUIRED: Use @code-reviewer subagent for security + quality audit on code changes.")
        if any(k in low for k in ["verify", "test", "check", "review", "qa"]):
            rules.append("REQUIRED: Execute project test commands (npm test, pytest, etc.) and report results.")
    elif role in ("frontend", "backend"):
        rules.append("REQUIRED: Run tests after making changes. Lock files before editing.")
        rules.append("REQUIRED: After work is done, set task to code_review (NOT done). Reviewers auto-dispatch.")
        if any(k in low for k in ["write test", "add test", "test coverage"]):
            rules.append("REQUIRED: Use @test-writer subagent for comprehensive test generation.")
    elif role.startswith("reviewer-"):
        rules.append("REQUIRED: Review diff, then POST /tasks/{tid}/review with verdict and comments. Use /submit-review command.")
        if "logic" in role:
            rules.append("FOCUS: Logic correctness, bugs, edge cases, null checks, race conditions.")
        elif "style" in role:
            rules.append("FOCUS: Naming, formatting, readability, DRY, code duplication.")
        elif "arch" in role:
            rules.append("FOCUS: Design patterns, separation of concerns, scalability, SOLID principles.")

    # ── MCP: only if task keywords match ──
    if any(k in low for k in ["documentation", "docs", "api reference", "how to use",
                               "library", "framework", "package", "module", "import",
                               "deprecated", "latest version", "migration guide"]):
        hints.append('TIP: Say "use context7" for up-to-date library docs.')

    if any(k in low for k in ["pull request", "pr ", "merge", "issue #", "github",
                               "branch", "review pr", "create pr"]):
        hints.append("TIP: GitHub MCP available — can create PRs, manage issues directly.")

    if any(k in low for k in ["sentry", "production error", "prod bug", "production bug",
                               "stack trace", "500 error", "crash in prod", "debug prod",
                               "error tracking", "production crash"]):
        hints.append("TIP: Sentry MCP available — check production errors with it.")

    if any(k in low for k in ["figma", "design", "mockup", "ui design", "pixel",
                               "component design", "style guide", "design system"]):
        hints.append("TIP: Figma MCP available — inspect design files and extract styles.")

    word_count = len(task_content.split())
    if word_count > 100 or any(k in low for k in ["refactor", "rewrite entire",
                                                    "architecture", "redesign", "overhaul",
                                                    "migrate system", "migrate to"]):
        hints.append("TIP: Use Sequential Thinking for complex multi-step planning.")

    # ── Subagents ──
    if any(k in low for k in ["investigate", "find where", "understand", "how does",
                               "locate", "search for", "trace", "where is"]):
        hints.append("TIP: Use @explorer subagent for read-only codebase discovery.")

    if any(k in low for k in ["write test", "add test", "test coverage", "unit test",
                               "integration test", "missing test"]):
        hints.append("TIP: Use @test-writer subagent for comprehensive test generation.")

    if any(k in low for k in ["code review", "security audit", "check quality", "audit code",
                               "review for security", "quality check", "vulnerability scan",
                               "peer review"]):
        hints.append("REQUIRED: Use @code-reviewer subagent for quality + security review.")

    # ── Code Review Pipeline (reviewer agents) ──
    if any(k in low for k in ["review_request", "review request", "code_review"]):
        rules.append("REQUIRED: Review the diff, then POST /tasks/{tid}/review with verdict='approve' or 'request_changes' and comments list.")
        rules.append("REQUIRED: Each comment must have: file, line (optional), description, severity (critical/warning/info).")

    # ── Rework from review feedback ──
    if any(k in low for k in ["review_feedback", "request_changes", "reviewer comment",
                               "fix review", "address review", "rework"]):
        hints.append("TIP: Read review comments from task, fix each issue, then set status to code_review to trigger re-review.")

    # ── QA after review approval ──
    if any(k in low for k in ["qa_request", "qa request", "in_testing"]):
        rules.append("REQUIRED: Run ALL test suites. If tests pass, set status to 'uat'. If tests fail, set status to 'in_progress' with failure details.")

    # ── UAT ──
    if any(k in low for k in ["uat", "user acceptance", "user_approval"]):
        hints.append("TIP: UAT decisions are made by the user from the dashboard. Use POST /tasks/{tid}/uat with action='approve' or 'reject'.")

    if any(k in low for k in ["query", "database", "sql ", "select ", "table ",
                               "db migration", "schema change", "add column"]):
        hints.append("TIP: Use @db-reader subagent for safe read-only DB queries.")

    if any(k in low for k in ["translation", "trans(", "i18n", "hardcoded string",
                               "çeviri", "translate", "locale", "translator_translations"]):
        hints.append("TIP: Use /translation_sql_writer skill to scan for hardcoded strings and generate translation SQL.")

    if any(k in low for k in ["playwright", "e2e test", "e2e ", "browser test",
                               "end-to-end", "end to end"]):
        hints.append("TIP: Use @playwright-generator subagent or /playwright_test skill for E2E tests.")

    if any(k in low for k in ["test fail", "flaky test", "broken test", "test broke",
                               "fix test", "heal test", "test timeout"]):
        hints.append("TIP: Use @playwright-healer subagent to diagnose and fix broken tests.")

    if any(k in low for k in ["figma to vue", "convert figma", "figma component",
                               "figma vue", "design to code", "design to vue"]):
        hints.append("TIP: Use @figma-to-vue subagent or /figma_to_vue skill for Figma→Vue conversion.")

    if any(k in low for k in ["migrate test", "selenium to playwright", "convert test",
                               "test migration", "cypress to playwright"]):
        hints.append("TIP: Use @test-migrator subagent or /test_migration skill for test framework migration.")

    if any(k in low for k in ["new route", "vue router", "add route", "generate route",
                               "route definition"]):
        hints.append("TIP: Use @route-generator subagent or /generate-route command for Vue Router routes.")

    if any(k in low for k in ["deploy", "deployment", "release", "staging", "production",
                               "ship", "go live"]):
        hints.append("TIP: Use /deploy skill to build, verify, and deploy the current project.")

    if any(k in low for k in ["health", "system status", "agent status", "budget",
                               "check system", "diagnostics"]):
        hints.append("TIP: Use /health_check skill to check system health, agent statuses, and budgets.")

    if any(k in low for k in ["jira", "ticket", "issue key", "atlassian",
                               "impact analysis", "dev check"]):
        hints.append("TIP: Use /jira_dev_check skill for dev impact analysis or /jira_qa_check skill for QA analysis on Jira tickets.")

    if any(k in low for k in ["pvd", "tech design", "tech-design", "product vision",
                               "breakdown", "decompose", "break down", "work items"]):
        hints.append("TIP: Use /breakdown_pvd skill to break down a PVD/tech-design into Jira tasks.")

    # ── Build output: rules first, then hints ──
    parts = []
    if rules:
        parts.append("TOOL USAGE RULES:\n" + "\n".join(f"  {r}" for r in rules))
    if hints:
        parts.extend(hints[:3])

    if not parts:
        return ""

    return "\n" + "\n".join(parts)


def discover_project_ecosystem(project_dir):
    """Scan a project directory for ecosystem configs that the agent should adopt."""
    findings = {}

    # 1. .mcp.json — project-level MCP servers
    for pattern in [".mcp.json", ".mcp/config.json"]:
        mcp_path = os.path.join(project_dir, pattern)
        if os.path.exists(mcp_path):
            try:
                with open(mcp_path) as f:
                    findings["mcp"] = json.load(f)
            except Exception:
                pass
            break

    # 2. .claude/ directory contents
    claude_dir = os.path.join(project_dir, ".claude")
    if os.path.isdir(claude_dir):
        cmd_dir = os.path.join(claude_dir, "commands")
        if os.path.isdir(cmd_dir):
            findings["commands"] = [f for f in os.listdir(cmd_dir) if f.endswith(".md")]
        agents_dir = os.path.join(claude_dir, "agents")
        if os.path.isdir(agents_dir):
            findings["subagents"] = [f for f in os.listdir(agents_dir) if f.endswith(".md")]
        skills_dir = os.path.join(claude_dir, "skills")
        if os.path.isdir(skills_dir):
            findings["skills"] = [d for d in os.listdir(skills_dir)
                                  if os.path.isdir(os.path.join(skills_dir, d))]
        settings_path = os.path.join(claude_dir, "settings.json")
        if os.path.exists(settings_path):
            try:
                with open(settings_path) as f:
                    settings = json.load(f)
                if "hooks" in settings:
                    findings["hooks"] = list(settings["hooks"].keys())
            except Exception:
                pass

    # 3. CLAUDE.md
    claude_md = os.path.join(project_dir, "CLAUDE.md")
    if os.path.exists(claude_md):
        try:
            with open(claude_md) as f:
                content = f.read()
            findings["claude_md"] = content[:2000]
        except Exception:
            pass

    # 4. Project-specific config files
    for cfg in [".eslintrc.json", "tsconfig.json", "pyproject.toml", "Makefile",
                ".prettierrc", "biome.json", "vitest.config.ts", "jest.config.js"]:
        if os.path.exists(os.path.join(project_dir, cfg)):
            findings.setdefault("config_files", []).append(cfg)

    # 5. AI Migrater detection — check for playwright/figma/translation agents
    ai_migrater_caps = _classify_ai_migrater_capabilities(
        findings.get("subagents", []),
        findings.get("commands", []),
        findings.get("skills", []),
    )
    if ai_migrater_caps:
        findings["ai_migrater"] = ai_migrater_caps

    return findings


def _classify_ai_migrater_capabilities(subagents, commands, skills):
    """Classify AI Migrater capabilities from discovered agent/command/skill files."""
    caps = {}
    all_names = [f.lower() for f in (subagents or [])]
    all_cmds = [f.lower() for f in (commands or [])]
    all_skills = [s.lower() for s in (skills or [])]
    combined = " ".join(all_names + all_cmds + all_skills)

    if any(k in combined for k in ["playwright", "e2e", "browser-test"]):
        caps["playwright"] = True
    if any(k in combined for k in ["figma", "design-to", "vue-convert"]):
        caps["figma"] = True
    if any(k in combined for k in ["translation", "i18n", "locale"]):
        caps["translation"] = True
    if any(k in combined for k in ["migrat", "selenium", "cypress"]):
        caps["test_migration"] = True
    if any(k in combined for k in ["route", "router"]):
        caps["routing"] = True

    return caps


if __name__ == "__main__":
    if len(sys.argv) < 5:
        print("Usage: setup_ecosystem.py <agent_name> <agent_cwd> <ma_dir> <workspace> [hub_url]")
        sys.exit(1)
    agent_name = sys.argv[1]
    agent_cwd = sys.argv[2]
    ma_dir = sys.argv[3]
    workspace = sys.argv[4]
    hub_url = sys.argv[5] if len(sys.argv) > 5 else "http://127.0.0.1:8040"
    shared = setup_shared_ecosystem(ma_dir, workspace)
    for r in shared:
        print(r)
    results = setup_agent_ecosystem(agent_name, agent_cwd, ma_dir, workspace, hub_url)
    for r in results:
        print(r)
