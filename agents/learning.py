"""agents/learning.py — Learning extraction, skills loading, hooks, templates."""
import os
import sys
import json
import subprocess
from .log_utils import log


# ── Ecosystem smart hints (loaded lazily) ──
_ecosystem_available = False
try:
    from ecosystem.setup_ecosystem import get_smart_hints
    _ecosystem_available = True
except ImportError:
    def get_smart_hints(task_content, project_name=None, ma_dir="", hub_url="", role=""):
        return ""


def read_file(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def truncate_context(ctx, text, limit=None):
    """Smart context truncation that preserves important sections."""
    limit = limit or ctx.MAX_CONTEXT
    if len(text) <= limit:
        return text

    # Identify protected sections that should never be truncated
    # Role/contract info at the start, and the latest instructions at the end

    # Find the last "=== MESSAGES ===" or task block — always keep it
    last_msg_idx = text.rfind("=== MESSAGES ===")
    last_task_idx = text.rfind("=== TASK ===")
    tail_start = max(last_msg_idx, last_task_idx)

    if tail_start > 0:
        head = text[:tail_start]
        tail = text[tail_start:]
    else:
        # No clear sections — keep first 30% and last 50%
        head_budget = int(limit * 0.3)
        tail_budget = int(limit * 0.5)
        return text[:head_budget] + f"\n\n... [{len(text) - limit} chars truncated] ...\n\n" + text[-tail_budget:]

    # If tail alone exceeds limit, compress the tail's diff blocks
    if len(tail) > limit * 0.7:
        tail = _compress_diffs(tail, int(limit * 0.6))

    # Budget for head: whatever's left after tail
    head_budget = limit - len(tail) - 100  # 100 chars for truncation message
    if head_budget < 500:
        head_budget = 500

    if len(head) > head_budget:
        # Keep the first part (role/contract) and trim middle
        first_section_end = min(int(head_budget * 0.6), len(head))
        trimmed = len(head) - head_budget
        head = head[:first_section_end] + f"\n\n... [{trimmed} chars of earlier context trimmed] ...\n\n" + head[-(head_budget - first_section_end):]

    result = head + tail
    # Final safety: hard truncate if still over limit
    if len(result) > limit:
        half = limit // 2 - 50
        result = result[:half] + f"\n\n... [{len(result) - limit} chars truncated] ...\n\n" + result[-half:]
    return result


def _compress_diffs(text, max_len):
    """Compress diff blocks by keeping only changed lines (+ and - lines)."""
    if len(text) <= max_len:
        return text

    lines = text.split('\n')
    result = []
    in_diff = False
    diff_context_count = 0

    for line in lines:
        if line.startswith('```diff') or line.startswith('diff --git'):
            in_diff = True
            diff_context_count = 0
            result.append(line)
        elif in_diff and line.startswith('```') and not line.startswith('```diff'):
            in_diff = False
            result.append(line)
        elif in_diff:
            # Keep +/- lines and @@ headers, skip context lines in diffs
            if line.startswith('+') or line.startswith('-') or line.startswith('@@'):
                result.append(line)
                diff_context_count = 0
            else:
                diff_context_count += 1
                if diff_context_count <= 1:  # keep max 1 context line
                    result.append(line)
        else:
            result.append(line)

    compressed = '\n'.join(result)
    if len(compressed) > max_len:
        # Still too long — hard truncate
        half = max_len // 2 - 50
        compressed = compressed[:half] + f"\n... [{len(compressed) - max_len} chars compressed] ...\n" + compressed[-half:]
    return compressed


def save_session(ctx):
    if ctx.SESSION_ID:
        try:
            tmp = ctx.SESSION_FILE + ".tmp"
            with open(tmp, "w") as f:
                f.write(ctx.SESSION_ID)
            os.replace(tmp, ctx.SESSION_FILE)
        except OSError:
            pass


def load_session(ctx):
    try:
        with open(ctx.SESSION_FILE) as f:
            return f.read().strip()
    except OSError:
        return None


def load_skills(ctx):
    skills_dir = os.path.join(ctx.MA_DIR, "skills")
    if not os.path.isdir(skills_dir):
        return ""
    parts = []
    for f in sorted(os.listdir(skills_dir)):
        if not f.endswith(".md"):
            continue
        if f.startswith(ctx.AGENT_NAME + "-") or f.startswith("all-") or "-" not in f:
            content = read_file(os.path.join(skills_dir, f))
            if content:
                parts.append(f"### Skill: {f}\n{content}")
    return "\n\n".join(parts)


def run_hook(ctx, hook_name, context=None):
    hooks_dir = os.path.join(ctx.MA_DIR, "hooks")
    if not os.path.isdir(hooks_dir):
        return
    for f in sorted(os.listdir(hooks_dir)):
        if not f.startswith(hook_name):
            continue
        path = os.path.join(hooks_dir, f)
        try:
            env = {**os.environ, "MA_AGENT": ctx.AGENT_NAME, "MA_WORKSPACE": ctx.WORKSPACE,
                   "MA_PROJECT": ctx.current_project or "", "MA_TASK_ID": str(ctx.current_task_id or "")}
            if context:
                env["MA_CONTEXT"] = json.dumps(context)
            if f.endswith(".sh"):
                subprocess.run(["bash", path], env=env, timeout=30, capture_output=True)
            elif f.endswith(".py"):
                subprocess.run([sys.executable, path], env=env, timeout=30, capture_output=True)
            log(ctx, f"🔌 hook: {f}")
        except Exception as e:
            log(ctx, f"hook err {f}: {e}")


def load_template(ctx, name, default=""):
    tpl_path = os.path.join(ctx.MA_DIR, "templates", f"{name}.md")
    return read_file(tpl_path) or default


def render_template(tpl, **kw):
    for k, v in kw.items():
        tpl = tpl.replace(f"{{{{{k}}}}}", str(v))
    return tpl


# ── Pattern Classification ──
_CATEGORY_KEYWORDS = {
    "playwright": ["playwright", "e2e", "browser", "selector", "locator", "getbytestid"],
    "figma": ["figma", "design token", "figma-to", "design system", "mcp__figma"],
    "vue": ["vue", "nuxt", "composable", "ref(", "computed", "v-model", "pinia"],
    "react": ["react", "nextjs", "next.js", "useState", "useEffect", "jsx", "tsx"],
    "testing": ["test", "jest", "vitest", "assert", "mock", "fixture", "coverage"],
    "i18n": ["i18n", "translation", "trans(", "locale", "gettext", "intl"],
    "routing": ["router", "route", "middleware", "endpoint", "url pattern"],
    "mcp": ["mcp", "mcp__", "mcp server", "tool_use"],
    "backend": ["api", "controller", "migration", "model", "database", "query", "sql"],
    "database": ["postgres", "mysql", "redis", "mongo", "prisma", "orm", "schema"],
    "devops": ["docker", "ci/cd", "pipeline", "deploy", "k8s", "kubernetes", "nginx"],
    "security": ["auth", "token", "csrf", "xss", "injection", "permission", "rbac"],
}


def classify_learning_category(text):
    """Classify text into a pattern category by keyword match count."""
    if not text:
        return "general"
    low = text.lower()
    scores = {}
    for cat, keywords in _CATEGORY_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in low)
        if score > 0:
            scores[cat] = score
    if not scores:
        return "general"
    return max(scores, key=scores.get)


def get_project_index(ctx):
    """Build a quick index of projects in the workspace."""
    workspace = ctx.WORKSPACE
    if not os.path.isdir(workspace):
        return ""
    projects = []
    for name in sorted(os.listdir(workspace)):
        d = os.path.join(workspace, name)
        if not os.path.isdir(d) or name.startswith("."):
            continue
        # Check for common project markers
        markers = ["package.json", "pyproject.toml", "go.mod", "Cargo.toml",
                    "composer.json", "requirements.txt", "Makefile", "CLAUDE.md"]
        found = [m for m in markers if os.path.exists(os.path.join(d, m))]
        if found:
            projects.append(f"  {name}/ ({', '.join(found[:3])})")
        elif any(os.path.exists(os.path.join(d, f)) for f in ["src", "lib", "app"]):
            projects.append(f"  {name}/")
    return "\n".join(projects) if projects else ""


def get_project_stack(ctx, project_name):
    """Get stack info for a specific project."""
    if not project_name:
        return ""
    from lib.config import detect_stack
    project_dir = os.path.join(ctx.WORKSPACE, project_name)
    if not os.path.isdir(project_dir):
        return ""
    stack = detect_stack(project_dir)
    parts = []
    if stack.get("lang"):
        parts.append(f"Languages: {', '.join(stack['lang'])}")
    if stack.get("fw"):
        parts.append(f"Frameworks: {', '.join(stack['fw'])}")
    if stack.get("tools"):
        parts.append(f"Tools: {', '.join(stack['tools'])}")
    if stack.get("test"):
        parts.append(f"Test: {', '.join(stack['test'])}")
    if stack.get("lint"):
        parts.append(f"Lint: {', '.join(stack['lint'])}")
    if stack.get("build"):
        parts.append(f"Build: {', '.join(stack['build'])}")
    return "\n".join(parts)


def get_verify_cmds(ctx, project_name):
    """Get verification commands (test, lint, build) for a project."""
    if not project_name:
        return []
    from lib.config import detect_stack
    project_dir = os.path.join(ctx.WORKSPACE, project_name)
    if not os.path.isdir(project_dir):
        return []
    stack = detect_stack(project_dir)
    cmds = []
    cmds.extend(stack.get("lint", []))
    cmds.extend(stack.get("test", []))
    cmds.extend(stack.get("build", []))
    return cmds


def track_ecosystem_use(ctx, tool_name, tool_input):
    """Track which ecosystem tools are being used by agents."""
    if not tool_name:
        return
    # Track MCP tool usage for ecosystem learning (deduplicate per task)
    if tool_name.startswith("mcp__"):
        if tool_name in ctx._eco_reported:
            return
        ctx._eco_reported.add(tool_name)
        try:
            from .hub_client import hub_post
            hub_post(ctx, "/agents/learning", {
                "agent_name": ctx.AGENT_NAME,
                "category": "mcp",
                "learning": f"Used {tool_name}",
                "task": str(ctx.current_task_id or ""),
            })
        except Exception:
            pass


def extract_learning(ctx, task_summary):
    """Extract learnings from completed task and submit to hub."""
    if not task_summary:
        return
    category = classify_learning_category(task_summary)
    try:
        from .hub_client import hub_post
        hub_post(ctx, "/agents/learning", {
            "agent_name": ctx.AGENT_NAME,
            "category": category,
            "learning": task_summary[:500],
            "task": str(ctx.current_task_id or ""),
        })
    except Exception:
        pass


def _broadcast_ecosystem_update(ctx, subtype, data):
    """Broadcast ecosystem updates (new patterns, tools, MCPs) to all agents."""
    try:
        from .hub_client import hub_msg
        msg_content = json.dumps({"type": "ecosystem_update", "subtype": subtype, **data})
        hub_msg(ctx, "all", msg_content, msg_type="ecosystem_update")
    except Exception:
        pass
