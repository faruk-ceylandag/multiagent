"""agents/worker.py — Agent boot sequence + main loop.

All logic is split into focused modules:
  context.py      — AgentContext dataclass (shared state)
  hub_client.py   — Hub API communication
  log_utils.py    — Logging, streaming, humanization
  credentials.py  — Credential management
  git_ops.py      — Git operations (branch, commit, rollback, PR)
  claude_runner.py— Claude CLI execution with streaming
  verify.py       — Verification loop (lint, test, build)
  chat_handler.py — Background chat thread
  mcp_manager.py  — MCP server setup & reload
  learning.py     — Learning extraction, skills, hooks, templates
"""
import sys
import os
import time
import json
import subprocess
import signal
import re
import random

from .context import AgentContext
from .log_utils import log, start_log_thread, flush_logs
from .hub_client import (hub_post, hub_get, hub_msg, set_status,
                         update_session, update_task_status, report_progress,
                         get_agent_roster, is_degraded)
from .credentials import load_credentials, save_credential, check_missing_credentials
from .git_ops import (git_rollback, git_branch, git_changed_files, collect_changes, git, unlock_all)
from .claude_runner import call_claude
from .verify import verify_loop
from .chat_handler import start_chat_handler, stop_chat_handler
from .mcp_manager import setup_mcp, reload_mcp, get_available_mcp, start_mcp_watcher, check_mcp_health
from .learning import (read_file, run_hook, load_template,
                       render_template, extract_learning, get_project_index,
                       get_project_stack, get_smart_hints, load_session,
                       classify_learning_category, _broadcast_ecosystem_update)
from .hub_client import get_relevant_patterns, get_peer_learnings

# ── JSON Schema for structured review verdicts ──
_REVIEW_SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "request_changes"]},
        "summary": {"type": "string"},
        "comments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "severity": {"type": "string", "enum": ["critical", "warning", "info"]},
                    "text": {"type": "string"}
                },
                "required": ["file", "severity", "text"]
            }
        }
    },
    "required": ["verdict", "comments"]
})


# ── Auto-retry helpers ──

def _classify_error(error_info):
    """Classify error for retry decisions."""
    msg = str(error_info).lower()
    if "rate limit" in msg or "429" in msg:
        return "rate_limit"
    if "timeout" in msg or "timed out" in msg:
        return "timeout"
    if "test" in msg and ("fail" in msg or "error" in msg):
        return "test_failure"
    if "lint" in msg:
        return "lint_failure"
    if "connection" in msg or "network" in msg:
        return "transient"
    return "unknown"


def _should_auto_retry(ctx, task_id, error_info):
    """Decide if task should be auto-retried."""
    error_type = _classify_error(error_info)
    retryable = {"rate_limit", "timeout", "transient", "test_failure"}
    if error_type not in retryable:
        return False, ""
    # Check retry count
    try:
        task = hub_get(ctx, f"/tasks/{task_id}") or {}
        retry_count = task.get("_retry_count", 0)
        if retry_count >= 3:
            return False, "max retries reached"
    except Exception:
        return False, "could not check retry count"

    hints = {
        "rate_limit": "Wait briefly then retry with reduced complexity",
        "timeout": "Break the task into smaller steps",
        "transient": "Simple retry should work",
        "test_failure": "Fix the failing tests first, then verify",
    }
    return True, hints.get(error_type, "Retry the task")


def _fetch_workspaces(ctx):
    """Fetch registered workspaces and add_dirs from hub."""
    try:
        resp = hub_get(ctx, "/workspaces")
        if isinstance(resp, list):
            ctx._workspaces = {}
            for ws in resp:
                ws_id = ws.get("ws_id", "")
                if ws_id and not ws.get("is_primary"):
                    ctx._workspaces[ws_id] = {
                        "path": ws.get("path", ""),
                        "name": ws.get("name", ""),
                        "projects": ws.get("projects", []),
                        "stacks": ws.get("stacks", {}),
                    }
            ctx._workspace_refresh_at = time.time()
    except Exception:
        pass
    # Sync add_dirs from config + workspace paths → ctx._extra_dirs
    dirs = []
    # Workspace paths
    for ws in getattr(ctx, '_workspaces', {}).values():
        p = ws.get("path", "")
        if p and os.path.isdir(p):
            dirs.append(p)
    # Explicit add_dirs from config
    try:
        cfg = hub_get(ctx, "/config")
        if isinstance(cfg, dict):
            for d in cfg.get("add_dirs", []):
                if d and os.path.isdir(d) and d not in dirs:
                    dirs.append(d)
    except Exception:
        pass
    ctx._extra_dirs = dirs


def _setup_stop_signal(ctx):
    """Setup SIGUSR1 handler for instant stop."""
    def _handle_sigusr1(signum, frame):
        ctx._should_stop = True
        if ctx.current_proc:
            try:
                ctx.current_proc.terminate()
            except OSError:
                pass
    signal.signal(signal.SIGUSR1, _handle_sigusr1)


def _parse_review_verdict(ctx):
    """Parse review verdict from reviewer's output.
    Tries JSON schema output first, then falls back to text parsing."""
    lines = ctx._last_output_lines[-30:]  # Check last 30 lines
    verdict = None
    comments = []

    # ── Try JSON schema output first (from --json-schema flag) ──
    full_output = "\n".join(lines)
    try:
        # Look for JSON object in output
        for line in reversed(lines):
            line = line.strip()
            if line.startswith("{") and "verdict" in line:
                parsed = json.loads(line)
                v = parsed.get("verdict", "")
                if v in ("approve", "request_changes"):
                    verdict = v
                    comments = parsed.get("comments", [])
                    # Normalize comment format
                    comments = [
                        {"file": c.get("file", ""), "line": c.get("line", 0),
                         "severity": c.get("severity", "warning"), "text": c.get("text", "")}
                        for c in comments if isinstance(c, dict)
                    ]
                    return verdict, comments
    except (json.JSONDecodeError, KeyError, TypeError):
        pass

    # ── Fallback: text-based parsing ──
    # Look for VERDICT: line
    for line in reversed(lines):
        low = line.lower().strip()
        if low.startswith("verdict:"):
            v = low.split(":", 1)[1].strip()
            if "approve" in v:
                verdict = "approve"
            elif "request" in v or "change" in v:
                verdict = "request_changes"
            break

    # Parse COMMENTS section if request_changes
    if verdict == "request_changes":
        in_comments = False
        for line in lines:
            if line.strip().upper().startswith("COMMENTS"):
                in_comments = True
                continue
            if in_comments and line.strip().startswith("- "):
                parts = line.strip()[2:].split("|")
                if len(parts) >= 3:
                    file_part = parts[0].strip()
                    line_num = 0
                    if ":" in file_part:
                        try:
                            line_num = int(file_part.split(":")[-1])
                        except (ValueError, IndexError):
                            pass
                    comments.append({
                        "file": file_part.split(":")[0],
                        "line": line_num,
                        "severity": parts[1].strip(),
                        "text": parts[2].strip(),
                    })
                else:
                    comments.append({"file": "", "line": 0, "severity": "warning", "text": line.strip()[2:]})

    return verdict, comments


def _build_patterns_block(patterns, peer_learnings, max_chars=600):
    """Build a concise context block from patterns and peer learnings."""
    parts = []
    if patterns:
        lines = [f"  - {p['pattern'][:80]} (score:{p['score']})" for p in patterns[:5]]
        parts.append("PROVEN PATTERNS:\n" + "\n".join(lines))
    if peer_learnings:
        lines = []
        for l in peer_learnings[:3]:
            agent = l.get('agent', '?')
            role = l.get('_role', '')
            expertise = l.get('_expertise', 0)
            profile = f"{agent}"
            if role:
                profile += f", {role[:20]}"
            if expertise > 0:
                profile += f", score:{expertise}"
            lines.append(f"  - [{profile}] {l.get('learning', '')[:80]}")
        parts.append("PEER INSIGHTS:\n" + "\n".join(lines))
    block = "\n".join(parts)
    return block[:max_chars] if block else ""


def _refresh_ecosystem(ctx):
    """Runtime tool discovery: sync new subagents/commands/skills from ecosystem dir."""
    try:
        from ecosystem.setup_ecosystem import refresh_agent_tools
        new_tools = refresh_agent_tools(ctx.AGENT_CWD, ctx.MA_DIR)
        if new_tools:
            log(ctx, f"🔧 New tools discovered: {', '.join(new_tools)}")
            _broadcast_ecosystem_update(ctx, "tool_effective", {
                "tools": new_tools, "agent": ctx.AGENT_NAME,
            })
    except ImportError:
        pass
    except Exception as e:
        log(ctx, f"⚠ Eco refresh: {e}")
    # Refresh MCP server names from registry (may have changed since boot)
    try:
        from ecosystem.mcp.setup_mcp import MCP_SERVERS
        _mcp_names = set(MCP_SERVERS.keys())
        ctx._ECO_MCP_NAMES = _mcp_names | {f"mcp__{n}" for n in _mcp_names}
    except ImportError:
        pass


def _process_ecosystem_updates(ctx, msgs):
    """Filter and process ecosystem_update messages. Returns non-eco messages."""
    normal = []
    for m in msgs:
        if m.get("msg_type") != "ecosystem_update":
            normal.append(m)
            continue
        try:
            data = json.loads(m.get("content", "{}"))
            subtype = data.get("subtype", "")
            if subtype == "agent_joined":
                d = data.get("data", {})
                log(ctx, f"👋 New agent joined: {d.get('agent', '?')} — {d.get('role', 'no role')[:60]}")
            elif subtype == "pattern_discovered":
                log(ctx, f"📐 Peer pattern: {data.get('data', {}).get('preview', '')[:60]}")
            elif subtype == "tool_effective":
                _refresh_ecosystem(ctx)
            elif subtype == "new_mcp_found":
                mcp_name = data.get("data", {}).get("mcp_name", "")
                if mcp_name:
                    from .mcp_manager import ensure_mcp
                    ensure_mcp(ctx, [mcp_name])
        except (json.JSONDecodeError, Exception):
            pass
    return normal


def _build_file_index(ctx, project):
    """Build a lightweight project structure overview to reduce agent discovery tokens."""
    if not project:
        return ""
    proj_dir = os.path.join(ctx.WORKSPACE, project)
    if not os.path.isdir(proj_dir):
        return ""
    from lib.config import SKIP_DIRS
    lines = []
    try:
        for entry in sorted(os.listdir(proj_dir)):
            if entry.startswith(".") or entry in SKIP_DIRS:
                continue
            full = os.path.join(proj_dir, entry)
            if os.path.isdir(full):
                try:
                    sub = len([f for f in os.listdir(full) if not f.startswith(".")])
                except OSError:
                    sub = 0
                lines.append(f"  {entry}/ ({sub} items)")
            else:
                lines.append(f"  {entry}")
    except OSError:
        return ""
    return "\nPROJECT STRUCTURE:\n" + "\n".join(lines[:30]) if lines else ""


def _analyze_likely_files(ctx, task_content, project):
    """Lightweight analysis to identify likely files from task description + git state."""
    files = []
    # Extract explicit file paths from task content
    file_refs = re.findall(r'[\w/.-]+\.(?:js|ts|tsx|jsx|vue|py|php|go|css|scss|html|json)', task_content)
    files.extend(file_refs[:20])

    # Check git diff for already-changed files in the branch
    proj_dir = os.path.join(ctx.WORKSPACE, project)
    if os.path.isdir(os.path.join(proj_dir, ".git")):
        try:
            import subprocess
            r = subprocess.run(["git", "diff", "--name-only", "HEAD"],
                               cwd=proj_dir, capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip():
                files.extend(r.stdout.strip().split("\n")[:20])
        except Exception:
            pass

    return list(set(files))[:30]


def _smart_project_detect(ctx, content):
    """Detect project from task content using multiple signals: name, stack keywords, package names."""
    c = content.lower()
    # Strip URLs — they contain domain names that cause false matches (e.g. "atlassian.net" → "atlas" project)
    c_clean = re.sub(r'https?://\S+', '', c)
    # Strip [USE X MCP] prefixes and MCP/tool service names — not project references
    c_clean = re.sub(r'\[use\s+\w+\s+mcp\]', '', c_clean)
    _svc_noise = ['atlassian', 'github', 'figma', 'sentry', 'google', 'jira', 'confluence',
                  'playwright', 'notion', 'linear', 'slack', 'mcp', 'oauth']
    for svc in _svc_noise:
        c_clean = re.sub(r'\b' + re.escape(svc) + r'\b', '', c_clean)
    c_clean = re.sub(r'[/\\]', ' ', c_clean)
    scores = {}
    # Single-project workspace: root has .git OR project markers → it IS the project
    from lib.config import _PROJECT_MARKERS
    has_git = os.path.isdir(os.path.join(ctx.WORKSPACE, ".git"))
    has_marker = any(os.path.exists(os.path.join(ctx.WORKSPACE, m)) for m in _PROJECT_MARKERS)
    if has_git or has_marker:
        return "."
    try:
        projects = [n for n in os.listdir(ctx.WORKSPACE)
                    if os.path.isdir(os.path.join(ctx.WORKSPACE, n, ".git"))
                    and any(os.path.exists(os.path.join(ctx.WORKSPACE, n, m)) for m in _PROJECT_MARKERS)]
    except OSError:
        return ""
    if not projects:
        return ""
    # Single project → always use it
    if len(projects) == 1:
        return projects[0]
    for name in projects:
        scores[name] = 0
        # 1. Exact name match (strongest signal)
        if re.search(r'\b' + re.escape(name.lower()) + r'\b', c_clean):
            scores[name] += 10
        # 2. Name parts match (e.g. "smart-recommender-fe" → "recommender", "smart")
        parts = re.split(r'[-_.]', name.lower())
        for part in parts:
            if len(part) > 3 and part in c_clean:
                scores[name] += 3
        # 3. Stack keywords from stack.json
        try:
            with open(os.path.join(ctx.MA_DIR, "stack.json")) as f:
                stacks = json.load(f)
            st = stacks.get(name, {})
            for kw in st.get("fw", []) + st.get("lang", []):
                if kw.lower() in c_clean:
                    scores[name] += 2
        except (OSError, json.JSONDecodeError):
            pass
        # 4. package.json name match
        pkg_path = os.path.join(ctx.WORKSPACE, name, "package.json")
        if os.path.exists(pkg_path):
            try:
                with open(pkg_path) as f:
                    pkg = json.load(f)
                pkg_name = pkg.get("name", "").lower()
                if pkg_name and pkg_name in c_clean:
                    scores[name] += 5
            except (OSError, json.JSONDecodeError):
                pass
    best = max(scores, key=scores.get)
    return best if scores[best] >= 3 else ""


# ── URL → MCP mapping for auto-install ──
_URL_MCP_MAP = [
    (r'figma\.com', 'figma'),
    (r'github\.com', 'github'),
    (r'sentry\.io', 'sentry'),
    (r'atlassian\.net|jira\.', 'atlassian'),
    (r'docs\.google\.com|sheets\.google\.com|slides\.google\.com|drive\.google\.com', 'google'),
    (r'linear\.app', 'linear'),
    (r'notion\.so|notion\.site', 'notion'),
    (r'playwright\.dev', 'playwright'),
]

def _prefetch_url_content(ctx, task_text):
    """Pre-fetch URL content for architect so it doesn't need MCP calls.
    Returns (content_str, external_id) — content to inject into prompt."""
    from .log_utils import log
    urls = re.findall(r'https?://\S+', task_text)
    if not urls:
        return "", ""

    results = []
    external_id = ""

    for url in urls[:3]:  # max 3 URLs
        url_clean = url.rstrip(".,;:!?)")

        # ── Jira / Atlassian ──
        jira_match = re.search(r'atlassian\.net/browse/([A-Z]+-\d+)', url_clean)
        if jira_match:
            issue_key = jira_match.group(1)
            external_id = external_id or issue_key
            try:
                # Use Atlassian REST API v2 via MCP fetch
                resp = hub_post(ctx, "/cache", {"key": f"jira_{issue_key}", "source": "jira"})
                # Check if already cached
                cached = hub_get(ctx, f"/cache/jira_{issue_key}")
                if cached and cached.get("content"):
                    results.append(f"[JIRA {issue_key}]\n{cached['content']}")
                    continue
            except Exception:
                pass
            # SSE MCP (atlassian) uses OAuth — can't pre-fetch via subprocess.
            # Inject a note so the agent reads it via MCP in its own session.
            results.append(f"[JIRA {issue_key}]\nUse mcp__atlassian__getJiraIssue(issueIdOrKey=\"{issue_key}\") to read this ticket's details.")
            log(ctx, f"📋 Jira {issue_key} — agent will fetch via MCP")
            continue

        # ── GitHub ──
        gh_issue = re.search(r'github\.com/([^/]+/[^/]+)/(?:issues|pull)/(\d+)', url_clean)
        if gh_issue:
            repo, num = gh_issue.group(1), gh_issue.group(2)
            external_id = external_id or f"GH-{num}"
            try:
                kind = "pr" if "/pull/" in url_clean else "issue"
                proc = subprocess.run(
                    ["gh", kind, "view", num, "-R", repo, "--json",
                     "title,body,state,labels,assignees"],
                    capture_output=True, text=True, timeout=15)
                if proc.returncode == 0 and proc.stdout.strip():
                    data = json.loads(proc.stdout)
                    body = (data.get("body") or "")[:1500]
                    labels = ", ".join(l.get("name", "") for l in data.get("labels", []))
                    content = f"Title: {data.get('title', '')}\nState: {data.get('state', '')}\nLabels: {labels}\n\n{body}"
                    results.append(f"[GITHUB {repo}#{num}]\n{content}")
                    hub_post(ctx, "/cache", {"key": f"gh_{num}", "content": content,
                                             "source": "github", "description": f"GitHub {kind} #{num}"})
                    log(ctx, f"📋 Pre-fetched GitHub {kind} #{num}")
                    continue
            except Exception as e:
                log(ctx, f"⚠ GitHub pre-fetch failed: {e}")

        # ── Figma ──
        figma_match = re.search(r'figma\.com/(?:design|file)/([^/]+)', url_clean)
        if figma_match:
            # Figma requires OAuth via MCP — include URL for architect to reference
            results.append(f"[FIGMA URL] {url_clean}\nNote: Agents will read this via Figma MCP. Include full URL in step description with [USE FIGMA MCP] prefix.")
            continue

        # ── Sentry ──
        if "sentry.io" in url_clean:
            results.append(f"[SENTRY URL] {url_clean}\nNote: Agents will read this via Sentry MCP. Include full URL in step description with [USE SENTRY MCP] prefix.")
            continue

        # ── Unknown URL — pass through ──
        results.append(f"[URL] {url_clean}")

    if not results:
        return "", external_id

    content = "\n\n".join(results)
    return content, external_id


def _load_creds_env(ctx):
    """Load credentials as env dict for subprocess calls."""
    from .credentials import load_credentials
    return load_credentials(ctx)


def _extract_plan_json(ctx):
    """Extract plan proposal JSON from architect's output lines."""
    text = "\n".join(ctx._last_output_lines)
    for pattern in [
        r"-d\s*'(\{.*?plan_steps.*?\})'",   # curl -d '{...}'
        r'(\{"sender".*?"plan_steps".*?\})',  # raw JSON
    ]:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
                if "plan_steps" in data and data.get("msg_type") == "plan_proposal":
                    return data
            except (json.JSONDecodeError, IndexError):
                continue
    return None


def _detect_needed_mcps(task_text):
    """Detect which MCP servers are needed based on URLs/keywords in task text."""
    needed = set()
    low = task_text.lower()
    for pattern, mcp_name in _URL_MCP_MAP:
        if re.search(pattern, low):
            needed.add(mcp_name)
    # Also detect from [USE X MCP] prefix
    mcp_prefix = re.search(r'\[USE\s+(\w+)\s+MCP\]', task_text, re.IGNORECASE)
    if mcp_prefix:
        hint = mcp_prefix.group(1).lower()
        # Map hint to MCP server name
        hint_map = {'figma': 'figma', 'github': 'github', 'sentry': 'sentry',
                    'atlassian': 'atlassian', 'jira': 'atlassian', 'google': 'google',
                    'linear': 'linear', 'notion': 'notion',
                    'playwright': 'playwright', 'chrome': 'chrome-devtools'}
        if hint in hint_map:
            needed.add(hint_map[hint])
    return list(needed)

# ── Initialize context ──
ctx = AgentContext()

# ── Set agent role from name ──
_KNOWN_ROLES = {"architect", "frontend", "backend", "qa", "devops", "security", "reviewer",
                 "reviewer-logic", "reviewer-style", "reviewer-arch"}
ctx.AGENT_ROLE = ctx.AGENT_NAME if ctx.AGENT_NAME in _KNOWN_ROLES else ""

# ── Start log streaming ──
start_log_thread(ctx)

# ── Signal handlers ──
def cancel_handler(sig, frame):
    set_status(ctx, "offline", "shutdown")
    unlock_all(ctx)
    if ctx.current_proc:
        try:
            ctx.current_proc.terminate()
        except OSError:
            pass
    flush_logs(ctx)
    sys.exit(0)

signal.signal(signal.SIGINT, cancel_handler)
signal.signal(signal.SIGTERM, cancel_handler)

# ════════════════════════════════════════
#  BOOT
# ════════════════════════════════════════
log(ctx, f"=== {ctx.AGENT_NAME.upper()} ===")
_ss = ctx.MODEL_SONNET.split("-")[1] if "-" in ctx.MODEL_SONNET else ctx.MODEL_SONNET
_os2 = ctx.MODEL_OPUS.split("-")[1] if "-" in ctx.MODEL_OPUS else ctx.MODEL_OPUS
log(ctx, f"  thinking: {_ss} | coding: {_os2}" + (f" | override: {ctx.MODEL_OVERRIDE}" if ctx.MODEL_OVERRIDE else ""))

for i in range(30):
    if hub_get(ctx, "/health"):
        log(ctx, "hub ok")
        break
    time.sleep(1)
else:
    log(ctx, "hub unreachable")
    sys.exit(1)

pd = os.path.join(ctx.AGENT_CWD, ".claude")
os.makedirs(pd, exist_ok=True)
pf = os.path.join(pd, "settings.json")
if not os.path.exists(pf):
    # Build explicit MCP permission patterns (mcp__*  wildcard doesn't work)
    try:
        from ecosystem.mcp.setup_mcp import MCP_SERVERS as _MCP_REG
        _mcp_perms = [f"mcp__{n}__*" for n in _MCP_REG]
    except ImportError:
        _mcp_perms = []
    if ctx.AGENT_NAME == "architect":
        perms = {"allow": ["Glob", "Bash(curl*)"] + _mcp_perms, "deny": ["Edit", "Write", "Read"]}
    else:
        perms = {"allow": ["Edit", "Write", "Read", "Bash(*)"] + _mcp_perms, "deny": []}
    with open(pf, "w") as f:
        json.dump({"permissions": perms}, f)

# MCP setup — register ALL servers from ecosystem registry (including HTTP/OAuth)
try:
    from ecosystem.mcp.setup_mcp import MCP_SERVERS as _REGISTRY
    _extra = [n for n in _REGISTRY if n not in ctx.MCP_SERVERS]
except ImportError:
    _extra = []
setup_mcp(ctx, extra_names=_extra)
start_mcp_watcher(ctx)

set_status(ctx, "booting")
# Send role description at register so hub and peers know what we do
_boot_role = read_file(ctx.ROLE_FILE)
_role_summary = ""
if _boot_role:
    # Extract first meaningful line as role summary
    for _rl in _boot_role.split("\n"):
        _rl = _rl.strip().lstrip("#").strip()
        if _rl and len(_rl) > 5 and not _rl.startswith("You are"):
            _role_summary = _rl[:200]
            break
    if not _role_summary:
        _role_summary = _boot_role.split("\n")[0].strip().lstrip("#").strip()[:200]
hub_post(ctx, "/agents/register", {"agent_name": ctx.AGENT_NAME, "status": "alive", "role": _role_summary, "pid": os.getpid()})
_fetch_workspaces(ctx)

# CLI health check
try:
    _v = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=10)
    if _v.returncode == 0:
        log(ctx, f"CLI: {_v.stdout.strip()}")
    else:
        log(ctx, f"⚠ CLI check failed: {_v.stderr.strip()[:100]}")
except Exception as e:
    log(ctx, f"✗ claude CLI not available: {e}")

# Fetch team roster for awareness
roster = get_agent_roster(ctx)

# Skip boot call_claude — context is injected per-task, boot call wastes tokens + 10-20s
# Just clear any stale session so first task starts fresh
old_session = load_session(ctx)
if old_session:
    try:
        os.remove(ctx.SESSION_FILE)
    except OSError:
        pass
ctx.SESSION_ID = None
log(ctx, "✓ ONLINE")

set_status(ctx, "idle")
_setup_stop_signal(ctx)

# ════════════════════════════════════════
#  MAIN LOOP
# ════════════════════════════════════════
_consecutive_failures = 0
while True:
    try:
        # Adaptive polling: backoff when idle, reset on activity
        if ctx.idle_count > 20:
            poll_interval = min(15, 2 + ctx.idle_count * 0.5)
        elif ctx.idle_count > 5:
            poll_interval = min(8, 2 + ctx.idle_count * 0.3)
        else:
            poll_interval = 2
        # Add jitter to prevent thundering herd
        time.sleep(poll_interval + random.uniform(0, 1))

        # Refresh workspaces periodically
        if time.time() - ctx._workspace_refresh_at > 60:
            _fetch_workspaces(ctx)

        poll_resp = hub_post(ctx, f"/poll/{ctx.AGENT_NAME}", {}, timeout=5)
        if not poll_resp:
            if is_degraded():
                log(ctx, "⚠ Hub unreachable, waiting 30s before retry")
                time.sleep(30)
                continue
            continue

        if poll_resp.get("stop"):
            with ctx._stop_lock:
                ctx._should_stop = True
            stop_chat_handler(ctx)
            if ctx.current_proc:
                try:
                    ctx.current_proc.terminate()
                except OSError:
                    pass
            log(ctx, "⛔ STOPPED by user")
            if ctx.current_project:
                git_rollback(ctx, ctx.current_project)
            if ctx.current_task_id:
                update_task_status(ctx, ctx.current_task_id, "cancelled", "stopped by user")
            unlock_all(ctx)
            set_status(ctx, "stopped", "stopped by user")
            ctx.current_task_id = None
            # Stay stopped until resume signal from hub
            log(ctx, "⏸ Agent paused — waiting for resume signal")
            while True:
                time.sleep(5)
                resp = hub_post(ctx, f"/poll/{ctx.AGENT_NAME}", {}, timeout=5)
                if not resp:
                    continue
                if resp.get("resume"):
                    log(ctx, "▶ Resumed by user")
                    with ctx._stop_lock:
                        ctx._should_stop = False
                    # Drain stale messages accumulated while stopped
                    try:
                        stale = hub_get(ctx, f"/messages/{ctx.AGENT_NAME}")
                        if stale and isinstance(stale, list) and len(stale) > 0:
                            log(ctx, f"🗑 Drained {len(stale)} stale messages from stop period")
                    except Exception:
                        pass
                    set_status(ctx, "idle", "resumed")
                    break
                if resp.get("stop"):
                    # Another stop while already stopped — ignore
                    continue
            continue

        cnt = poll_resp.get("count", 0)
        if ctx.idle_count % 30 == 0:
            try:
                if os.path.exists(ctx.LOG_FILE) and os.path.getsize(ctx.LOG_FILE) > ctx.MAX_LOG:
                    for i in range(3, 1, -1):
                        src = f"{ctx.LOG_FILE}.{i-1}"
                        dst = f"{ctx.LOG_FILE}.{i}"
                        if os.path.exists(src):
                            try:
                                os.replace(src, dst)
                            except OSError:
                                pass
                    os.rename(ctx.LOG_FILE, f"{ctx.LOG_FILE}.1")
            except OSError:
                pass

        _auto_task = None
        if cnt == 0:
            ctx.idle_count += 1
            # With long-polling, check auto-assign every few cycles (not every poll)
            if ctx.idle_count % 3 == 0:
                auto = hub_post(ctx, f"/tasks/auto-assign/{ctx.AGENT_NAME}", {})
                if auto and auto.get("status") == "ok" and auto.get("task"):
                    _auto_task = auto["task"]
                    log(ctx, f"📥 Auto-assigned #{_auto_task['id']}: {_auto_task.get('description', '')[:60]}")
                    ctx.idle_count = 0
            if not _auto_task:
                continue

        if _auto_task:
            # Enrich with parent task context if available
            _task_content = f"#{_auto_task['id']} {_auto_task.get('description', '')}"
            _parent_id = _auto_task.get("parent_id")
            if _parent_id:
                _parent = hub_get(ctx, f"/tasks/{_parent_id}")
                if _parent and isinstance(_parent, dict):
                    _task_content += f"\n\n--- PARENT TASK #{_parent_id} CONTEXT ---\n{_parent.get('description', '')[:500]}"
                    # Inherit project/branch from parent if not set
                    if not _auto_task.get("project") and _parent.get("project"):
                        _auto_task["project"] = _parent["project"]
                    if not _auto_task.get("branch") and _parent.get("branch"):
                        _auto_task["branch"] = _parent["branch"]
                    if not _auto_task.get("task_external_id") and _parent.get("task_external_id"):
                        _auto_task["task_external_id"] = _parent["task_external_id"]
            msgs = [{"sender": _auto_task.get("created_by", "system"), "msg_type": "task",
                     "content": _task_content,
                     "task_id": str(_auto_task["id"]), "project": _auto_task.get("project", ""),
                     "branch": _auto_task.get("branch", ""),
                     "task_external_id": _auto_task.get("task_external_id", "")}]
        else:
            all_msgs = hub_get(ctx, f"/messages/{ctx.AGENT_NAME}")
            if not all_msgs:
                continue
            all_msgs = [m for m in all_msgs if m.get("msg_type") not in ("ack", "heartbeat", "session_reset")]
            if not all_msgs:
                continue

            # Process ecosystem updates (pattern_discovered, tool_effective, new_mcp_found)
            all_msgs = _process_ecosystem_updates(ctx, all_msgs)
            if not all_msgs:
                continue

            task_msgs = [m for m in all_msgs if m.get("msg_type") in ("task",) and m.get("sender") == "user"]
            other_msgs = [m for m in all_msgs if m not in task_msgs]

            if len(task_msgs) > 1:
                msgs = [task_msgs[0]] + other_msgs
                for requeue_msg in task_msgs[1:]:
                    requeue_data = {
                        "sender": requeue_msg.get("sender", "user"),
                        "receiver": ctx.AGENT_NAME,
                        "content": requeue_msg.get("content", ""),
                        "msg_type": requeue_msg.get("msg_type", "task"),
                    }
                    # Preserve all metadata fields
                    for field in ("task_external_id", "task_id", "project", "branch",
                                  "parent_id", "depends_on", "priority"):
                        if requeue_msg.get(field):
                            requeue_data[field] = requeue_msg[field]
                    hub_post(ctx, "/messages", requeue_data)
                log(ctx, f"📋 {len(task_msgs) - 1} task(s) re-queued, processing 1")
            else:
                msgs = all_msgs
        ctx.idle_count = 0
        ctx.task_calls = 0
        msgs = [m for m in msgs if m.get("msg_type") not in ("ack", "heartbeat", "session_reset")]
        if not msgs:
            continue

        # ── Deduplicate messages by content (prevent duplicate system notifications) ──
        _seen_content = set()
        _deduped = []
        for m in msgs:
            _key = (m.get("sender", ""), m.get("content", "")[:500], m.get("msg_type", ""))
            if _key not in _seen_content:
                _seen_content.add(_key)
                _deduped.append(m)
        if len(_deduped) < len(msgs):
            log(ctx, f"↳ dedup: {len(msgs) - len(_deduped)} duplicate(s) dropped")
        msgs = _deduped
        if not msgs:
            continue

        log(ctx, f"← {len(msgs)} msg(s)")
        for m in msgs:
            sender = m['sender']
            content = m.get('content', '')
            if sender == 'user' and len(content) > 40:
                log(ctx, f"📨 [{sender}] ─────────────────────")
                for pline in content.split('\n'):
                    pline = pline.rstrip()
                    if pline:
                        log(ctx, f"📨 {pline[:300]}")
                log(ctx, "📨 ─────────────────────────────────")
            else:
                log(ctx, f"  [{sender}] {content[:120]}")

        # ── Handle credential messages ──
        cred_saved = False
        for m in msgs:
            content = m.get("content", "")
            if m.get("sender") == "user" and m.get("msg_type") in ("credential", "message"):
                cred_lines = re.findall(r'\b([A-Z][A-Z0-9_]{2,50})=(\S+)', content)
                for key, value in cred_lines:
                    if any(kw in key.upper() for kw in ("TOKEN", "KEY", "SECRET", "PASSWORD", "API", "AUTH", "COOKIE")):
                        save_credential(ctx, key, value)
                        cred_saved = True
            if m.get("msg_type") == "credential":
                creds_data = m.get("credentials", {})
                for key, value in creds_data.items():
                    save_credential(ctx, key, value)
                    cred_saved = True
        if cred_saved:
            reload_mcp(ctx)
            hub_msg(ctx, "user", "🔑 Credentials saved and MCP servers reloaded.", "info")
            non_cred = [m for m in msgs if m.get("msg_type") != "credential"]
            if not non_cred or all(re.match(r'^[A-Z][A-Z0-9_]+=\S+$', m.get("content", "").strip()) for m in msgs):
                set_status(ctx, "idle")
                continue

        _TASK_MSG_TYPES = {"task", "qa_feedback", "review_feedback", "uat_feedback"}
        is_task = any(m.get("msg_type") in _TASK_MSG_TYPES or
                     (m.get("msg_type") == "message" and len(m.get("content", "")) > 10) for m in msgs)
        _is_rework = any(m.get("msg_type") in ("qa_feedback", "review_feedback", "uat_feedback") for m in msgs)
        is_chat_only = all(m.get("msg_type") == "chat" for m in msgs if m.get("sender") == "user")

        # ── System "task unblocked" notifications → fetch real task from hub ──
        _all_system_info = all(
            m.get("sender") == "system" and m.get("msg_type") == "info" for m in msgs
        )
        if _all_system_info and not is_task:
            _handled = False
            for m in msgs:
                unblock_match = re.search(r'Your task #(\d+)\b', m.get("content", ""))
                if unblock_match:
                    _tid = unblock_match.group(1)
                    _td = hub_get(ctx, f"/tasks/{_tid}")
                    if _td and isinstance(_td, dict) and _td.get("assigned_to") == ctx.AGENT_NAME:
                        _task_status = _td.get("status", "")
                        if _task_status in ("done", "failed", "cancelled"):
                            log(ctx, f"ℹ Task #{_tid} already {_task_status}, skipping")
                            continue
                        log(ctx, f"📋 Auto-fetching unblocked task #{_tid}")
                        msgs = [{"sender": _td.get("created_by", "system"), "msg_type": "task",
                                 "content": f"#{_tid} {_td.get('description', '')}",
                                 "task_id": _tid, "project": _td.get("project", ""),
                                 "branch": _td.get("branch", ""),
                                 "task_external_id": _td.get("task_external_id", "")}]
                        is_task = True
                        _handled = True
                        break
            if not _handled:
                log(ctx, "ℹ System notification acknowledged (no actionable task)")
                set_status(ctx, "idle")
                continue

        # Chat messages when idle → quick response via existing session
        # Skip chat shortcut if agent has an active task — treat as task follow-up instead
        _has_active_task = False
        if ctx.current_task_id:
            _atd = hub_get(ctx, f"/tasks/{ctx.current_task_id}")
            _has_active_task = _atd and isinstance(_atd, dict) and _atd.get("status") in ("in_progress", "to_do")
        if is_chat_only and not is_task and not _has_active_task:
            chat_content = " ".join(m.get("content", "") for m in msgs if m.get("msg_type") == "chat")
            if chat_content.strip():
                set_status(ctx, "working", "💬 chat")
                log(ctx, f"💬 Chat: {chat_content[:80]}")
                try:
                    chat_prompt = f"User sent a quick message. Answer briefly, same language.\n\nUSER: {chat_content}"
                    ctx._last_output_lines = []
                    call_claude(ctx, chat_prompt, force_model=ctx.MODEL_SONNET)
                    reply = " ".join(ctx._last_output_lines[-3:])[:300] if ctx._last_output_lines else "Message received."
                    hub_msg(ctx, "user", reply, "chat")
                except Exception as e:
                    log(ctx, f"⚠ Chat error: {e}")
                    hub_msg(ctx, "user", "Message received, an error occurred.", "chat")
                set_status(ctx, "idle")
                continue

        # Save previous task context for follow-up detection
        _prev_task_id = ctx.current_task_id
        _prev_project = ctx.current_project
        _prev_task_calls = ctx.task_calls
        _prev_session_tokens = ctx.session_tokens
        _prev_task_start = ctx._task_start_time

        ctx.current_task_id = None
        ctx.current_project = None
        ctx._review_parent_id = None
        # Architect/QA/reviewers: fresh session each task (independent tasks, no carry-over)
        # Dev agents: keep session for multi-call conversation continuity
        # Rework: keep session so agent remembers previous implementation context
        _fresh_session_roles = {"architect", "qa", "reviewer-logic", "reviewer-style", "reviewer-arch"}
        if ctx.AGENT_NAME in _fresh_session_roles:
            ctx.SESSION_ID = None
        elif ctx.SESSION_ID and not ctx.valid_sid(ctx.SESSION_ID):
            ctx.SESSION_ID = None
        ctx.task_calls = 0
        ctx.session_tokens = 0
        ctx._task_start_time = time.time()
        for m in msgs:
            if m.get("task_id"):
                ctx.current_task_id = m["task_id"]
                break
            tid_match = re.search(r'(?:TASK-|#)(\d+)', m.get("content", ""))
            if tid_match:
                ctx.current_task_id = tid_match.group(1)
                break

        # Follow-up detection: if no task_id in messages but agent was recently
        # working on a task, treat as continuation (e.g., user answering "yes"
        # after architect asked for confirmation, or credential refresh mid-task).
        # This ensures claude_runner uses get_task_session() to resume the same
        # Claude CLI session, preserving conversation context.
        _is_followup = False
        if not ctx.current_task_id and _prev_task_id:
            _prev_td = hub_get(ctx, f"/tasks/{_prev_task_id}")
            if _prev_td and isinstance(_prev_td, dict) and _prev_td.get("status") in ("in_progress", "to_do"):
                ctx.current_task_id = _prev_task_id
                ctx.current_project = _prev_project
                ctx.task_calls = _prev_task_calls
                ctx.session_tokens = _prev_session_tokens
                ctx._task_start_time = _prev_task_start
                _is_followup = True
                log(ctx, f"🔗 Continuing task #{_prev_task_id} (follow-up)")

        # Detect review parent ID from message or task data
        for m in msgs:
            if m.get("_review_parent_id"):
                ctx._review_parent_id = str(m["_review_parent_id"])
                break
        if not ctx._review_parent_id and ctx.current_task_id:
            _td = hub_get(ctx, f"/tasks/{ctx.current_task_id}")
            if _td and isinstance(_td, dict) and _td.get("_review_parent_id"):
                ctx._review_parent_id = str(_td["_review_parent_id"])

        # Resolve workspace for task
        for m in msgs:
            task_workspace = m.get("workspace", "") or ""
            if task_workspace:
                ws_path = ctx.get_workspace_path(task_workspace)
                if ws_path and ws_path != ctx.WORKSPACE:
                    log(ctx, f"📁 Task workspace: {ws_path}")
                break

        # Auto-create kanban task (skip for reviewer agents with a review subtask)
        _is_reviewer = ctx.AGENT_NAME.startswith("reviewer-")
        if is_task and not ctx.current_task_id and not (_is_reviewer and ctx._review_parent_id):
            desc = next((m["content"] for m in msgs if m.get("sender") in ("user", "system")
                         and m.get("msg_type") in ("task", "message")), "")
            if desc and len(desc) > 10:
                result = hub_post(ctx, "/tasks", {
                    "description": desc[:500],
                    "assigned_to": ctx.AGENT_NAME,
                    "status": "to_do",
                    "created_by": next((m["sender"] for m in msgs), "user"),
                    "project": ctx.current_project or "",
                    "priority": 5
                })
                if result and result.get("id"):
                    ctx.current_task_id = str(result["id"])
                    log(ctx, f"📌 Task #{ctx.current_task_id} created in kanban")

        # All agents set their own task to in_progress (reviewers/QA set their subtask, devs set main task)
        if ctx.current_task_id:
            update_task_status(ctx, ctx.current_task_id, "in_progress")
        ctx.reset_eco_tracking()
        _refresh_ecosystem(ctx)
        # Load model policy from config for smart model selection
        try:
            _cfg_data = hub_get(ctx, "/config")
            if isinstance(_cfg_data, dict):
                ctx._model_policy = _cfg_data.get("model_policy", {})
        except Exception:
            pass

        for m in msgs:
            c = m.get("content", "").lower()
            if m.get("project"):
                ctx.current_project = m["project"]
                break
            tid = m.get("task_id", "")
            if tid and str(tid).isdigit():
                task_data = hub_get(ctx, f"/tasks/{tid}")
                if task_data and isinstance(task_data, dict) and task_data.get("project"):
                    ctx.current_project = task_data["project"]
                    break
            if not ctx.current_project:
                ctx.current_project = _smart_project_detect(ctx, c)
            if ctx.current_project:
                break

        if is_task:
            run_hook(ctx, "pre-task", {"project": ctx.current_project or ""})

        if ctx.current_task_id and ctx.current_project:
            hub_post(ctx, f"/tasks/{ctx.current_task_id}", {"project": ctx.current_project})

        # ── Branch management ──
        branch_info = ""
        current_branch = None
        task_external_id = None
        if is_task and ctx.current_project:
            for m in msgs:
                eid = m.get("task_external_id", "")
                if eid and not re.match(r'^(feature/)?TASK-\d+$', eid):
                    task_external_id = eid
                    break

            if not task_external_id:
                for m in msgs:
                    br = m.get("branch", "")
                    if br and not re.match(r'^(feature/)?TASK-\d+$', br):
                        task_external_id = br
                        break

            if not task_external_id and ctx.current_task_id:
                td = hub_get(ctx, f"/tasks/{ctx.current_task_id}")
                if td and isinstance(td, dict):
                    candidate = td.get("branch", "") or td.get("task_external_id", "")
                    # Skip auto-generated TASK-{n} IDs — prefer extracting real ticket IDs from URLs below
                    if candidate and not re.match(r'^(feature/)?TASK-\d+$', candidate):
                        task_external_id = candidate

            if not task_external_id:
                for m in msgs:
                    bm = re.search(r'(?:Branch|branch|BRANCH)[:\s]+[`]?(feature/\S+?)[`]?\s', m.get("content", "") + " ")
                    if bm:
                        task_external_id = bm.group(1)
                        break

            if not task_external_id:
                for m in msgs:
                    c = m.get("content", "")
                    url_match = re.search(r'atlassian\.net/browse/([A-Z]{2,10}-\d+)', c)
                    if url_match:
                        task_external_id = url_match.group(1)
                        break
                    linear_match = re.search(r'linear\.app/[^/]+/issue/([A-Z]+-\d+)', c)
                    if linear_match:
                        task_external_id = linear_match.group(1)
                        break
                    gh_match = re.search(r'github\.com/[^/]+/[^/]+/(?:issues|pull)/(\d+)', c)
                    if gh_match:
                        task_external_id = f"GH-{gh_match.group(1)}"
                        break
                    sentry_match = re.search(r'sentry\.io/issues/(\d+)', c)
                    if sentry_match:
                        task_external_id = f"SENTRY-{sentry_match.group(1)}"
                        break
                    jira_match = re.search(r'\b([A-Z]{2,10}-\d{1,6})\b', c)
                    if jira_match:
                        task_external_id = jira_match.group(1)
                        break

            if task_external_id:
                current_branch = git_branch(ctx, ctx.current_project, branch_name=task_external_id)
                if current_branch:
                    branch_info = f"\nGit: You are on branch `{current_branch}`. ALL agents share this branch. Commit when done."
                    if ctx.current_task_id:
                        hub_post(ctx, f"/tasks/{ctx.current_task_id}", {"branch": current_branch, "task_external_id": task_external_id})
            else:
                log(ctx, "⚠ No Task ID — working without branch (on current branch)")

        set_status(ctx, "working", msgs[0].get("content", "")[:60])
        mtxt = "\n".join(f"[{m['sender']}] ({m['msg_type']}): {m['content']}" for m in msgs)
        # Wrap task content to prevent prompt injection
        if is_task:
            mtxt = f"<user_task>\n{mtxt}\n</user_task>"
        contracts = read_file(os.path.join(ctx.MA_DIR, "memory", "contracts.md"))

        # ── Token optimization: lazy roster ──
        # Architects get full roster (roles/expertise/status) for routing decisions
        # Dev agents get minimal roster (just names) — they rarely need team details
        _is_architect = ctx.AGENT_NAME == "architect"
        if _is_architect:
            roster = get_agent_roster(ctx)
        else:
            try:
                _cfg_data = hub_get(ctx, "/config")
                _team_names = _cfg_data.get("agents", []) if isinstance(_cfg_data, dict) else []
                roster = f"\nTEAM: {', '.join(_team_names)}"
            except Exception:
                roster = ""

        # ── Token optimization: filtered project context ──
        project_dir = f"{ctx.WORKSPACE}/{ctx.current_project}" if ctx.current_project else ctx.WORKSPACE
        stack_info = get_project_stack(ctx, ctx.current_project) if ctx.current_project else ""
        project_ctx = ""
        if ctx.current_project:
            project_ctx = f"\nPROJECT: {ctx.current_project}\nPROJECT DIR: {project_dir}\ncd {project_dir} before any work."
            if stack_info:
                project_ctx += f"\nSTACK:\n{stack_info}"
            # Pre-task file index: reduces agent ls/find discovery tokens
            _file_idx = _build_file_index(ctx, ctx.current_project)
            if _file_idx:
                project_ctx += _file_idx
        else:
            # Only include full project index when project is unknown
            proj_index = get_project_index(ctx)
            project_ctx = f"\nWORKSPACE: {ctx.WORKSPACE}"
            if proj_index:
                project_ctx += f"\nAVAILABLE PROJECTS:\n{proj_index}"
                project_ctx += "\nIMPORTANT: Identify the correct project first. Read its CLAUDE.md or README.md. Then cd into that project directory before doing any work. Do NOT scan all projects."
            else:
                project_ctx += "\nNo git projects found. Ask user which project if unclear."

        saved_creds = load_credentials(ctx)

        # Pre-flight: detect needed MCPs from task content
        task_text = " ".join(m.get("content", "") for m in msgs)
        _needed_mcps = _detect_needed_mcps(task_text)

        # Pre-flight credential check — architect skips (delegates to agents who will check)
        missing_creds = check_missing_credentials(task_text, saved_creds) if not _is_architect else []
        if missing_creds:
            missing_list = ", ".join(f"{s['service']} ({', '.join(s['keys'])})" for s in missing_creds)
            # Include service_id for targeted wizard opening on dashboard
            svc_ids = [s.get("service_id", s["service"].lower().split("/")[0].split("(")[0].strip()) for s in missing_creds]
            log(ctx, f"🔐 Missing credentials: {missing_list}")
            hub_msg(ctx, "user", f"🔐 Credentials needed before I can work on this task:\n\n{missing_list}\n\nI'll wait and auto-retry when credentials are saved.", "blocker",
                    extra={"missing_services": svc_ids})
            cred_wait_start = time.time()
            got_creds = False
            while time.time() - cred_wait_start < 300:
                time.sleep(10)
                cred_msgs = hub_get(ctx, f"/messages/{ctx.AGENT_NAME}?consume=false")
                if cred_msgs:
                    for cm in cred_msgs:
                        if cm.get("msg_type") == "credential":
                            # Consume the credential message so it's not re-processed
                            hub_get(ctx, f"/messages/{ctx.AGENT_NAME}")
                            reload_mcp(ctx)
                            got_creds = True
                            break
                if got_creds:
                    saved_creds = load_credentials(ctx)
                    log(ctx, "🔑 Credentials received, continuing task")
                    hub_msg(ctx, "user", "🔑 Credentials received! Continuing with the task.", "info")
                    break
                fresh_creds = load_credentials(ctx)
                still_missing = check_missing_credentials(task_text, fresh_creds)
                if not still_missing:
                    reload_mcp(ctx)
                    saved_creds = fresh_creds
                    log(ctx, "🔑 Credentials detected (file), continuing task")
                    hub_msg(ctx, "user", "🔑 Credentials detected! Continuing with the task.", "info")
                    break
            else:
                log(ctx, "⏱ Credential wait timeout — returning task to queue")
                hub_msg(ctx, "user", f"⏱ {ctx.AGENT_NAME}: credentials not received after 5 min. Task #{ctx.current_task_id or '?'} returned to queue — provide credentials and retry.", "blocker")
                if ctx.current_task_id:
                    update_task_status(ctx, ctx.current_task_id, "to_do", detail="Credentials not provided within timeout — retryable")
                set_status(ctx, "idle")
                ctx.current_task_id = None
                ctx._task_start_time = 0
                continue

        # Auto-install needed MCPs AFTER credentials are available
        if _needed_mcps:
            from .mcp_manager import ensure_mcp
            ensure_mcp(ctx, _needed_mcps)

        # Pre-flight MCP health check: warn agent about unreachable servers
        _mcp_health_warning = ""
        if _needed_mcps:
            _healthy = check_mcp_health(ctx, _needed_mcps)
            _unhealthy = set(_needed_mcps) - _healthy
            if _unhealthy:
                _mcp_health_warning = f"\n⚠ UNAVAILABLE MCP SERVERS: {', '.join(_unhealthy)} — use REST API fallback instead."
                log(ctx, f"⚠ MCP health: {', '.join(_unhealthy)} unreachable")

        mcp_list = get_available_mcp(ctx)

        mcp_ctx = ""
        if mcp_list and _is_architect:
            # Architect only needs a brief MCP summary — agents get full details
            mcp_ctx = f"\nAVAILABLE MCP TOOLS: {', '.join(mcp_list)}\nWhen delegating tasks with URLs, prefix with [USE X MCP] so agents know which tool to use."
        elif mcp_list:
            # ── Token optimization: only include detailed docs for MCPs relevant to this task ──
            _needed_mcp_docs = {"context7"}  # always useful for docs lookup
            for nm in _needed_mcps:
                _needed_mcp_docs.add(nm)
            # Also detect from task text keywords
            task_lower = task_text.lower()
            if "figma" in task_lower or "figma.com" in task_lower:
                _needed_mcp_docs.add("figma")
            if "github" in task_lower or "github.com" in task_lower:
                _needed_mcp_docs.add("github")
            if "sentry" in task_lower or "sentry.io" in task_lower:
                _needed_mcp_docs.add("sentry")
            if "atlassian" in task_lower or "jira" in task_lower or "confluence" in task_lower:
                _needed_mcp_docs.add("atlassian")
            if "google" in task_lower or "docs.google" in task_lower or "sheets.google" in task_lower:
                _needed_mcp_docs.add("google")
            if "playwright" in task_lower:
                _needed_mcp_docs.add("playwright")
            if "chrome" in task_lower and "devtools" in task_lower:
                _needed_mcp_docs.add("chrome-devtools")

            mcp_tools_detail = []
            if "figma" in mcp_list and "figma" in _needed_mcp_docs:
                mcp_tools_detail.append("""  FIGMA MCP — for any figma.com URL or design reference:
    • mcp__figma__get_design_context(nodeId, fileKey) — PRIMARY TOOL. Returns code + screenshot + design tokens. Extract fileKey and nodeId from URL: figma.com/design/:fileKey/:fileName?node-id=:nodeId (convert "-" to ":" in nodeId)
    • mcp__figma__get_screenshot(nodeId, fileKey) — Get a visual screenshot of a design node
    • mcp__figma__get_metadata(nodeId, fileKey) — Get structure/layers overview in XML
    • mcp__figma__get_variable_defs(nodeId, fileKey) — Get design tokens (colors, spacing, fonts)
    Example: URL figma.com/design/ABC123/MyFile?node-id=45-67 → fileKey="ABC123", nodeId="45:67"
    WORKFLOW: get_design_context first → adapt returned code to project stack → use screenshot for visual reference""")
            if "github" in mcp_list and "github" in _needed_mcp_docs:
                mcp_tools_detail.append("""  GITHUB MCP — for any github.com URL, PR, issue, or repo reference:
    • Use 'gh' CLI commands: gh pr view URL, gh issue view URL, gh api repos/OWNER/REPO/...
    • gh pr list, gh issue list, gh pr diff URL, gh pr checks URL
    • gh pr create --title "..." --body "...", gh pr comment URL --body "..."
    Example: "fix github.com/org/repo/issues/42" → gh issue view 42 -R org/repo → read details → implement fix""")
            if "context7" in mcp_list and "context7" in _needed_mcp_docs:
                mcp_tools_detail.append("""  CONTEXT7 MCP — for library/framework documentation lookup:
    • mcp__context7__resolve-library-id(libraryName, query) — Find library ID first
    • mcp__context7__query-docs(libraryId, query) — Then query docs with the ID
    Example: Need React docs → resolve-library-id("react","how to use useEffect") → get ID → query-docs(ID,"useEffect cleanup")
    Use this INSTEAD of WebFetch for any library documentation.""")
            if "atlassian" in mcp_list and "atlassian" in _needed_mcp_docs:
                mcp_tools_detail.append("""  ATLASSIAN MCP (Official) — for any atlassian.net URL, Jira ticket, Confluence page:
    • Uses Atlassian's official remote MCP server (OAuth authenticated)
    • Available tools: getAccessibleAtlassianResources, getJiraIssue, searchJiraIssues, createJiraIssue, updateJiraIssue, transitionJiraIssue, getConfluencePage, searchConfluencePages
    • PREFER high-level tools: Use mcp__atlassian__getJiraIssue(issueKey="KEY-123") — NOT mcp__atlassian__fetch with raw REST paths
    • Extract issue key from URL: atlassian.net/browse/PA-36376 → issue key "PA-36376"
    • First call getAccessibleAtlassianResources to get your cloudId, then use it in subsequent calls
    • Search: searchJiraIssuesUsingJql with JQL query (e.g. "project=PA AND status='To Do'")
    • WORKFLOW: Extract issue key → getJiraIssue → read description/AC → implement → updateJiraIssue/transitionJiraIssue
    • IMPORTANT: If using mcp__atlassian__fetch with raw REST paths, ALWAYS use API v2 (/rest/api/2/), NEVER v3
    CURL FALLBACK (if MCP fails): curl -s -u "$ATLASSIAN_EMAIL:$ATLASSIAN_API_TOKEN" "$JIRA_BASE_URL/rest/api/2/issue/KEY" """)
            if "sentry" in mcp_list and "sentry" in _needed_mcp_docs:
                mcp_tools_detail.append("""  SENTRY MCP — for error tracking and production debugging:
    • Access Sentry issues, events, and stack traces via MCP
    • Use for any sentry.io URL or error investigation task""")
            if "google" in mcp_list and "google" in _needed_mcp_docs:
                mcp_tools_detail.append("""  GOOGLE WORKSPACE MCP — for Google Docs, Sheets, Slides, Drive URLs:
    • DOCS: readGoogleDoc(documentId), appendToGoogleDoc, insertText, applyTextStyle, formatMatchingText
    • SHEETS: readSpreadsheet(spreadsheetId, range), writeSpreadsheet, appendSpreadsheetRows, getSpreadsheetInfo, createSpreadsheet
    • SLIDES: readPresentation(presentationId), createPresentation, addSlide, addTextToSlide, listPresentations
    • DRIVE: listGoogleDocs, searchGoogleDocs, getDocumentInfo, listFolderContents, createFolder
    • Extract document ID from URL: docs.google.com/document/d/:documentId/... → use documentId
    • sheets.google.com/spreadsheets/d/:spreadsheetId/... → use spreadsheetId
    • docs.google.com/presentation/d/:presentationId/... → use presentationId
    WORKFLOW: Extract ID from URL → readGoogleDoc/readSpreadsheet/readPresentation → process content → make changes if needed""")
            if "sequentialthinking" in mcp_list and "sequentialthinking" in _needed_mcp_docs:
                mcp_tools_detail.append("""  SEQUENTIAL THINKING MCP — for complex task decomposition:
    • Use when a task is ambiguous or requires multi-step planning
    • Helps break down complex problems into structured steps""")
            if "playwright" in mcp_list and "playwright" in _needed_mcp_docs:
                mcp_tools_detail.append("""  PLAYWRIGHT MCP — for browser automation and E2E test generation:
    • Navigate to URLs, interact with page elements, take screenshots
    • Generate Playwright test code from browser interactions
    • Use for any playwright.dev URL or browser testing task""")
            if "chrome-devtools" in mcp_list and "chrome-devtools" in _needed_mcp_docs:
                mcp_tools_detail.append("""  CHROME DEVTOOLS MCP — for page inspection and debugging:
    • Inspect DOM elements, query selectors, monitor network requests
    • Access browser console, evaluate JavaScript in page context
    • Use for debugging frontend issues, checking element states""")

            mcp_detail_str = "\n".join(mcp_tools_detail)
            mcp_ctx = f"""
AVAILABLE MCP TOOLS: {', '.join(mcp_list)}

MCP TOOL USAGE (MANDATORY — use these INSTEAD of WebFetch):
{mcp_detail_str}

CRITICAL: When you see a URL from a known MCP domain (figma.com, github.com, sentry.io, atlassian.net, docs.google.com, sheets.google.com, slides.google.com, drive.google.com, etc.), you MUST use the corresponding MCP tool or REST API. Do NOT use WebFetch for these domains. WebFetch is ONLY for URLs that no MCP tool handles."""
            if _mcp_health_warning:
                mcp_ctx += _mcp_health_warning

        # ── Context that only dev agents need (architect skips) ──
        lock_ctx = ""
        url_instruction = ""
        eco_hints = ""
        learned_patterns_ctx = ""
        eco_discovery = ""
        # task_text already computed above (line ~672)

        prefetched_content = ""
        if _is_architect:
            # Architect: pre-fetch URL content so it doesn't need MCP calls
            _prefetched, _ext_id = _prefetch_url_content(ctx, task_text)
            if _prefetched:
                prefetched_content = f"\n=== PRE-FETCHED CONTENT (already read — do NOT re-fetch) ===\n{_prefetched}\n=== END PRE-FETCHED CONTENT ==="
                if _ext_id:
                    prefetched_content += f"\nEXTERNAL_ID: {_ext_id}"
            eco_hints = get_smart_hints(task_text, ctx.current_project, ctx.MA_DIR, ctx.HUB_URL, role="architect")
            ctx._active_pattern_ids = []
        else:
            # Dev agents: full context
            try:
                locks = hub_get(ctx, "/files/locks")
                if locks and isinstance(locks, dict):
                    other_locks = {path: info["agent"] for path, info in locks.items()
                                   if info.get("agent") != ctx.AGENT_NAME}
                    if other_locks:
                        lock_list = "\n".join(f"  - {path} (locked by {agent})" for path, agent in other_locks.items())
                        lock_ctx = f"\n⚠️ LOCKED FILES — DO NOT EDIT these files, they are being worked on by other agents:\n{lock_list}\nIf you need to modify a locked file, message the agent who locked it first."
            except Exception:
                pass

            # Only include MCP auth boilerplate when task actually needs MCP tools
            _task_needs_mcp = bool(_needed_mcp_docs - {"context7"})  # context7 doesn't need auth
            if mcp_list and _task_needs_mcp:
                if saved_creds:
                    cred_keys = [k for k in saved_creds.keys() if any(w in k.upper() for w in ("TOKEN", "KEY", "API", "AUTH"))]
                    if cred_keys:
                        mcp_ctx += f"\nSaved credentials: {', '.join(cred_keys)}"
                mcp_ctx += """
MCP AUTH: If an MCP tool fails with authentication/authorization error:
1. Tell user which tool failed and what credentials are needed (e.g. SERVICE_API_TOKEN, SERVICE_EMAIL)
2. The user can provide credentials via the dashboard's Service Connect wizard or chat
3. Once provided, credentials auto-save and MCP servers reload
4. Do NOT try to work around auth failures by using WebFetch — ask for credentials instead.

RESILIENT FALLBACK CHAIN — NEVER give up, always try the next approach:
  1. MCP TOOL → Use the matching MCP tool first (mcp__figma__*, mcp__atlassian__*, mcp__google__*, etc.)
  2. REST API via curl → If MCP fails or isn't available, use curl with stored credentials from env vars:
     • Jira: curl -s -u "$ATLASSIAN_EMAIL:$ATLASSIAN_API_TOKEN" "$JIRA_BASE_URL/rest/api/2/issue/KEY"
     • GitHub: gh api repos/OWNER/REPO/issues/NUM (uses $GITHUB_TOKEN)
     • Figma: curl -s -H "X-Figma-Token: $FIGMA_ACCESS_TOKEN" "https://api.figma.com/v1/files/KEY"
     • Sentry: curl -s -H "Authorization: Bearer $SENTRY_AUTH_TOKEN" "https://sentry.io/api/0/issues/ID/"
     • Google: Use WebFetch on the doc/sheet URL
  3. WebFetch → If no credentials, try WebFetch on the URL to extract what you can
  4. Ask user → If everything fails, message the user with exactly what you need (credentials, access, content)
  NEVER say "I can't access this" or "I'm unable to" without trying ALL fallbacks first.
  If you truly cannot complete the task after trying everything, say EXACTLY: "TASK_FAILED: <reason>" — never say "done" or "completed" if the work isn't actually done."""

            has_url = bool(re.search(r'https?://\S+', task_text))
            if has_url and not ctx.current_project:
                url_instruction = """
URL TASK WORKFLOW:
1. First READ the URL content to understand what the task requires
2. Identify which project this relates to from the available projects list
3. cd into that project directory
4. Read CLAUDE.md or README.md to understand the project structure
5. Then implement the required changes
Do NOT scan the entire workspace. Do NOT guess the project from the URL domain."""

            eco_hints = get_smart_hints(task_text, ctx.current_project, ctx.MA_DIR, ctx.HUB_URL, role=getattr(ctx, 'AGENT_ROLE', ''))

            # Pattern injection: fetch proven patterns + peer learnings
            try:
                task_category = classify_learning_category(task_text)
                patterns = get_relevant_patterns(ctx, task_category, min_score=2, limit=5)
                peer_learnings = get_peer_learnings(ctx, top=3)
                learned_patterns_ctx = _build_patterns_block(patterns, peer_learnings)
                ctx._active_pattern_ids = [p["id"] for p in (patterns or [])[:3]]
                # Cross-agent memory: load shared codebase learnings
                _shared_mem_file = os.path.join(ctx.MA_DIR, "memory", "learnings", f"{ctx.current_project or 'general'}.md")
                if os.path.exists(_shared_mem_file):
                    try:
                        _mem_content = read_file(_shared_mem_file)
                        if _mem_content and len(_mem_content) > 10:
                            learned_patterns_ctx += f"\nSHARED CODEBASE KNOWLEDGE:\n{_mem_content[:500]}"
                    except Exception:
                        pass
            except Exception:
                ctx._active_pattern_ids = []

            # Discover project ecosystem (MCP, commands, hooks, configs)
            if ctx.current_project:
                try:
                    from ecosystem.setup_ecosystem import discover_project_ecosystem
                    from .mcp_manager import adopt_project_mcp
                    proj_dir = os.path.join(ctx.WORKSPACE, ctx.current_project)
                    findings = discover_project_ecosystem(proj_dir)
                    if findings:
                        parts = []
                        if findings.get("commands"):
                            parts.append(f"Project slash commands: {', '.join(findings['commands'])}")
                        if findings.get("subagents"):
                            parts.append(f"Project subagents: {', '.join(findings['subagents'])}")
                        if findings.get("hooks"):
                            parts.append(f"Project hooks: {', '.join(findings['hooks'])}")
                        if findings.get("skills"):
                            parts.append(f"Project skills: {', '.join(findings['skills'])} (invoke with /skill_name)")
                        if findings.get("config_files"):
                            parts.append(f"Config files: {', '.join(findings['config_files'])}")
                        if findings.get("mcp"):
                            mcp_names = list(findings["mcp"].get("mcpServers", {}).keys())
                            if mcp_names:
                                parts.append(f"Project MCP servers: {', '.join(mcp_names)}")
                                adopt_project_mcp(ctx, proj_dir)
                        if parts:
                            eco_discovery = "\nPROJECT ECOSYSTEM:\n" + "\n".join(f"  • {p}" for p in parts)
                            eco_discovery += "\nYou MUST use listed project tools when applicable. Skills: /skill_name. Subagents: @agent-name."
                            if getattr(ctx, 'AGENT_ROLE', '') == 'qa':
                                eco_discovery += "\nQA MANDATORY: Run /test and use @code-reviewer before marking done."
                except Exception:
                    pass

        role = read_file(ctx.ROLE_FILE)
        role_ctx = f"\nROLE: {role.strip()}" if role and role.strip() else ""

        # ── Architect gets a lean, fast-delegation prompt ──
        if ctx.AGENT_NAME == "architect":
            task_tpl = load_template(ctx, "task_architect", """You are {{agent}} — the team coordinator.{{role_ctx}}
=== TASK ===
{{messages}}
{{prefetched}}
{{contracts}}
{{roster}}
{{project_ctx}}
YOUR ONLY TASK: Execute the curl below with the plan using Bash. NOTHING ELSE.
NEVER ask questions. NEVER ask for confirmation. NEVER say "Would you like me to...".
NEVER use AskUserQuestion. NEVER use ToolSearch. NEVER read files or explore code.
If URL content is pre-fetched above, use it. If not, include the raw URL in step descriptions with [USE X MCP] prefix.
Execute this curl using Bash — no text before or after it:

curl -s -X POST {{hub}}/messages -H 'Content-Type: application/json' -d '{
  "sender":"{{agent}}","receiver":"user","msg_type":"plan_proposal",
  "content":"Brief summary of the plan",
  "task_id":"{{task_id}}",
  "plan_steps":[
    {"description":"FULL DESCRIPTION with ALL context, URLs, acceptance criteria. Agent knows NOTHING else.","assigned_to":"AGENT_NAME","priority":5,"depends_on_step":null,"task_external_id":"JIRA-123 or GH-45 or empty"},
    {"description":"FULL DESCRIPTION...","assigned_to":"AGENT_NAME","priority":5,"depends_on_step":0,"task_external_id":"same external ID"}
  ],
  "project":"{{current_project}}"
}'

RULES:
1. SIMPLE TASK (one scope) → ONE step to the right specialist. MULTI-SCOPE → max 2-3 steps.
2. Copy ALL context (URLs, requirements, acceptance criteria) into each step description verbatim.
3. depends_on_step uses 0-based index. Chain: dev(0) → QA(1, depends_on_step:0).
4. If task mentions a specific agent ("frontend fix X"), single step to that agent.
5. Use EXTERNAL_ID from pre-fetched content in EVERY step's task_external_id. No ID? Leave empty.
6. ALWAYS add a QA step (depends_on dev step) for verification.
7. NEVER ask questions, NEVER explore. Execute the curl and STOP.
{{branch_info}}{{hints}}""")
        else:
            task_tpl = load_template(ctx, "task", """You are {{agent}}.{{role_ctx}}
=== MESSAGES ===
{{messages}}
{{contracts}}
{{roster}}
{{project_ctx}}{{mcp_tools}}{{eco_discovery}}{{url_workflow}}
RULES:
- ROLE: Do the actual work yourself. NEVER delegate to other agents.
  * frontend: Write and fix frontend code (Vue, React, CSS, HTML, JS). Implement UI changes.
  * backend: Write and fix backend code (API, database, controllers, models, middleware).
  * qa: Run ALL test suites, linters, verify requirements. Write missing tests. Report DETAILED results.
  * friday / devops: Handle Sentry errors, deployment, monitoring, CI/CD. Fix root cause, verify with tests.
  * reviewer: Review code for quality, patterns, security. Provide specific feedback with file paths.
- ITERATION — NEVER SUBMIT BROKEN CODE:
  * After EVERY change, run test/lint/build commands immediately
  * If tests fail → read FULL error → fix → re-run. Keep iterating (up to 8 cycles) until ALL pass.
  * NEVER mark done if any test is failing.
  * If stuck after 5+ attempts, message user with what's failing and what you tried.
  * Report progress: curl -s -X POST {{hub}}/messages -H 'Content-Type: application/json' -d '{"sender":"{{agent}}","receiver":"user","content":"MSG","msg_type":"info"}'
- IMPLEMENTATION:
  * Read the code first. Understand codebase structure.
  * Task with URL → read URL first to understand requirements, then implement.
  * Task prefixed with [USE X MCP] → MUST use that MCP tool.
  * ONLY work in the project directory. Do NOT scan other projects.
  * If something fails → try EVERY alternative (curl, WebFetch, different API). Try 3+ approaches before reporting inability.
  * Lock files before editing: curl -s -X POST {{hub}}/files/lock -H 'Content-Type: application/json' -d '{"file_path":"PATH","agent_name":"{{agent}}"}'{{locked_files}}
- CACHED CONTENT — check cache BEFORE calling MCP tools:
  * If task mentions [CACHED:key], read cached content first: curl -s {{hub}}/cache/KEY_NAME
  * List all cached content: curl -s {{hub}}/cache
  * This avoids redundant MCP calls (Figma, Jira, GitHub, etc.)
  * After reading NEW MCP content, cache it for other agents:
    curl -s -X POST {{hub}}/cache -H 'Content-Type: application/json' -d '{"key":"SOURCE_KEY","content":"CONTENT","source":"figma|jira|github"}'
- Save credentials: curl -s -X POST {{hub}}/credentials -H 'Content-Type: application/json' -d '{"KEY_NAME":"value"}'
- TOOLS: Use ecosystem tools actively. Skills: /skill_name. Subagents: @agent-name.
  * QA agents: MUST run project test commands AND use @code-reviewer before marking done.
  * After implementation: run project lint/test/build commands immediately.
  * PEER REVIEW: When you receive a review request, use @code-reviewer subagent to analyze changes.
    Check code quality, error handling, test coverage, security. Report result to hub.{{branch_info}}{{hints}}
{{learned_patterns}}""")

        # ── File plan: submit intended files for conflict detection (BEFORE render_template so lock_ctx is complete) ──
        if is_task and ctx.current_project and not _is_architect:
            try:
                _file_plan = _analyze_likely_files(ctx, task_text, ctx.current_project)
                if _file_plan:
                    _conflict_check = hub_post(ctx, "/files/check-conflicts", {
                        "agent_name": ctx.AGENT_NAME, "files": _file_plan,
                    })
                    if _conflict_check and _conflict_check.get("has_conflicts"):
                        for c in _conflict_check.get("conflicts", [])[:3]:
                            log(ctx, f"⚠ File conflict: {', '.join(c.get('conflicting_files', [])[:3])} (by {c.get('agent', '?')})")
                            lock_ctx += f"\n⚠️ FILE CONFLICT: {', '.join(c.get('conflicting_files', [])[:3])} planned by {c.get('agent', '?')} — avoid editing these."
                    hub_post(ctx, "/files/plan", {
                        "agent_name": ctx.AGENT_NAME, "files": _file_plan,
                        "task_id": ctx.current_task_id or "",
                    })
            except Exception:
                pass

        prompt = render_template(task_tpl, messages=mtxt, workspace=ctx.WORKSPACE, hub=ctx.HUB_URL,
                                 agent=ctx.AGENT_NAME, role_ctx=role_ctx, branch_info=branch_info,
                                 roster=roster, project_ctx=project_ctx, mcp_tools=mcp_ctx,
                                 eco_discovery=eco_discovery,
                                 url_workflow=url_instruction, hints=eco_hints,
                                 locked_files=lock_ctx,
                                 learned_patterns=learned_patterns_ctx,
                                 prefetched=prefetched_content,
                                 current_branch=current_branch or "", current_project=ctx.current_project or "",
                                 task_id=ctx.current_task_id or "",
                                 contracts=f'=== CONTRACTS ===\n{contracts}' if contracts else '')

        # ── Auto-inject cached MCP content referenced in task ──
        _cache_refs = re.findall(r'\[CACHED:([^\]]+)\]', prompt)
        if _cache_refs:
            _cache_parts = []
            for _ckey in _cache_refs[:10]:  # cap at 10 to avoid bloat
                try:
                    import urllib.request
                    _cresp = urllib.request.urlopen(f"{ctx.HUB_URL}/cache/{_ckey}", timeout=5)
                    _ccontent = _cresp.read().decode("utf-8", errors="replace")
                    if _ccontent and len(_ccontent) > 10:
                        _cache_parts.append(f"=== CACHED: {_ckey} ===\n{_ccontent[:5000]}")
                        log(ctx, f"📦 Cache hit: {_ckey} ({len(_ccontent)} chars)")
                except Exception:
                    log(ctx, f"📦 Cache miss: {_ckey}")
            if _cache_parts:
                prompt += "\n\n" + "\n\n".join(_cache_parts)

        # Save task summary for verify/follow-up calls (survives session resets)
        ctx._task_summary = task_text[:500]
        start_chat_handler(ctx, task_text[:500])
        task_cwd = os.path.join(ctx.WORKSPACE, ctx.current_project) if ctx.current_project and os.path.isdir(os.path.join(ctx.WORKSPACE, ctx.current_project)) else None
        # Reviewer agents: use JSON schema for structured verdict output
        _call_schema = _REVIEW_SCHEMA if ctx.AGENT_NAME.startswith("reviewer-") else None
        # System prompt: static context (role, contracts, roster) — only on first call
        # When session exists, these are already in context, saving ~1-2K tokens per call
        _sys_prompt = None
        if not ctx.valid_sid(ctx.SESSION_ID):
            _sys_parts = [p for p in [role_ctx, contracts, roster] if p and p.strip()]
            if _sys_parts:
                _sys_prompt = "\n".join(_sys_parts)
        task_result = call_claude(ctx, prompt, cwd=task_cwd, json_schema=_call_schema,
                                  system_prompt=_sys_prompt)
        stop_chat_handler(ctx)

        # ── Architect fallback: if plan curl wasn't executed, extract and submit ──
        if _is_architect and is_task and ctx._last_output_lines:
            _plan_submitted = False
            if ctx.current_task_id:
                try:
                    _plans = hub_get(ctx, f"/pending-plans?creator={ctx.AGENT_NAME}&task_id={ctx.current_task_id}")
                    _plan_submitted = bool(_plans and isinstance(_plans, list) and len(_plans) > 0)
                except Exception:
                    pass
            if not _plan_submitted:
                _plan_json = _extract_plan_json(ctx)
                if _plan_json:
                    try:
                        hub_post(ctx, "/messages", _plan_json)
                        log(ctx, "📋 Plan proposal submitted (fallback extraction)")
                    except Exception as e:
                        log(ctx, f"⚠ Fallback plan submission failed: {e}")

        if task_result is False and ctx._task_start_time and ctx.TASK_TIMEOUT > 0 and (time.time() - ctx._task_start_time) > ctx.TASK_TIMEOUT:
            if ctx.current_task_id:
                update_task_status(ctx, ctx.current_task_id, "failed", detail=f"Timed out after {ctx.TASK_TIMEOUT // 60} min")
                hub_msg(ctx, "user", f"⏱ {ctx.AGENT_NAME}: task #{ctx.current_task_id} timed out after {ctx.TASK_TIMEOUT // 60} min", "info")
            set_status(ctx, "idle")
            ctx._task_start_time = 0
            ctx.current_task_id = None
            continue

        # ── Detect agent self-reported failure ──
        _agent_gave_up = False
        if is_task and ctx._last_output_lines:
            _out_text = " ".join(ctx._last_output_lines[-10:]).lower()
            # Use regex word-boundary match for TASK_FAILED to avoid false positives
            # on phrases like "the task_failed event" in documentation
            _keyword_match = re.search(r'\bTASK_FAILED:', _out_text, re.IGNORECASE)
            _phrase_signals = ["i'm unable to", "i cannot access", "i can't access",
                               "unable to complete", "could not complete", "cannot complete this task",
                               "i was unable", "not able to access", "i couldn't"]
            if _keyword_match or any(sig in _out_text for sig in _phrase_signals):
                _agent_gave_up = True
                log(ctx, "⚠ Agent reported inability — marking task as FAILED")

        task_ok = True
        if _agent_gave_up:
            task_ok = False
        elif is_task and ctx.current_project and not _is_architect:
            vr = verify_loop(ctx, ctx.current_project, call_claude_fn=lambda *a, **kw: call_claude(ctx, *a, **kw))
            if not vr:
                task_ok = False
                log(ctx, f"⚠ verify failed on {ctx.current_project} — changes preserved (use Retry to reattempt)")
                hub_msg(ctx, "user", f"⚠️ {ctx.AGENT_NAME}: task on {ctx.current_project} finished but verify failed. Changes are preserved — you can Retry the task or manually review.", "info")
            else:
                # Stage changes and send for user review instead of auto-committing
                proj_dir = os.path.join(ctx.WORKSPACE, ctx.current_project)
                git_changed_files(ctx, ctx.current_project)
                _, diff_stat = git(ctx, ["diff", "--stat"], proj_dir)
                _, diff_cached = git(ctx, ["diff", "--cached", "--stat"], proj_dir)
                _, untracked = git(ctx, ["ls-files", "--others", "--exclude-standard"], proj_dir)
                summary = (diff_stat + "\n" + diff_cached + "\n" + untracked).strip()
                # Stage everything for review (exclude .claude/, .multiagent/ etc.)
                from .git_ops import git_add_safe
                git_add_safe(ctx, proj_dir)
                # Collect full diff for review panel
                collect_changes(ctx, msgs[0].get("content", "")[:200], ctx.current_project)
                # Build suggested commit message: TASK-ID | Clean Title
                _, cur_br = git(ctx, ["branch", "--show-current"], proj_dir)
                # Get task title from hub if available
                task_title = ""
                if ctx.current_task_id:
                    td = hub_get(ctx, f"/tasks/{ctx.current_task_id}")
                    if td and isinstance(td, dict):
                        task_title = td.get("description", "")
                if not task_title:
                    task_title = msgs[0].get("content", "")
                # Clean title: first line only, strip prompt/role artifacts
                task_title = task_title.split("\n")[0]
                task_title = re.sub(r'\[.*?\]\s*', '', task_title)
                task_title = re.sub(r'^#\d+\s*', '', task_title)
                task_title = re.sub(r'https?://\S+\s*', '', task_title)
                # Strip role/prompt text that may leak into task descriptions
                task_title = re.sub(r'^You are \w+[\s—–\-].*$', '', task_title)
                task_title = re.sub(r'(?:ROLE|TASK|MESSAGES|RULES|CONTRACTS)[:\s].*', '', task_title, flags=re.IGNORECASE)
                task_title = task_title.strip()
                if not task_title or len(task_title) < 5:
                    # Last resort: scan messages for first user/system content
                    for _m in msgs:
                        _mc = _m.get("content", "").split("\n")[0].strip()
                        _mc = re.sub(r'^#\d+\s*', '', _mc).strip()
                        if _mc and len(_mc) > 5 and not _mc.startswith("You are"):
                            task_title = _mc[:80]
                            break
                task_title = task_title[:80]
                # Build: branch-ref | title (preferred) or TASK-ID | title
                if cur_br and cur_br.startswith("feature/"):
                    task_ref = cur_br.replace("feature/", "")
                    suggested_msg = f"{task_ref} | {task_title}"
                elif ctx.current_task_id:
                    suggested_msg = f"TASK-{ctx.current_task_id} | {task_title}"
                else:
                    suggested_msg = f"{ctx.AGENT_NAME} | {task_title}"
                log(ctx, f"📋 Changes staged for review ({ctx.current_project})")
                hub_msg(ctx, "user",
                    f"✅ {ctx.AGENT_NAME}: task complete, changes ready for review.\n\n"
                    f"Project: {ctx.current_project} | Branch: {cur_br or 'N/A'}\n"
                    f"Files:\n{summary[:500]}",
                    "review_request",
                    extra={
                        "project": ctx.current_project,
                        "branch": cur_br or "",
                        "suggested_commit_msg": suggested_msg,
                        "agent": ctx.AGENT_NAME,
                        "task_id": ctx.current_task_id or "",
                    })

        # Lock cleanup deferred — released after status is determined (skip for code_review)
        _skip_unlock = False
        # Resource cleanup
        ctx.session_tokens = 0
        ctx.task_calls = 0
        ctx._last_output_lines = []
        # Clear file plan after task completion
        try:
            hub_post(ctx, "/files/plan", {"agent_name": ctx.AGENT_NAME, "files": [], "task_id": ""})
        except Exception:
            pass

        if ctx.current_task_id:
            if _agent_gave_up:
                fail_reason = "Agent could not complete task — " + " ".join(ctx._last_output_lines[-3:])[:200]
            elif not task_ok:
                fail_reason = "Verification failed (tests/lint/build)"
            else:
                fail_reason = ""
            # Reviewer/QA agents must NOT change the original task status —
            # they submit verdicts via POST /tasks/{tid}/review or set status via curl.
            # The hub manages the task lifecycle for reviewed/tested tasks.
            if _is_reviewer_or_qa:
                if ctx.AGENT_NAME.startswith("reviewer-"):
                    # Parse verdict from Claude's output and auto-submit
                    verdict, comments = _parse_review_verdict(ctx)
                    # Submit to parent task (not the reviewer's own subtask)
                    _review_target = ctx._review_parent_id or ctx.current_task_id
                    if verdict:
                        hub_post(ctx, f"/tasks/{_review_target}/review", {
                            "agent": ctx.AGENT_NAME,
                            "verdict": verdict,
                            "comments": comments,
                        })
                        log(ctx, f"📝 Auto-submitted review verdict: {verdict} ({len(comments)} comments) → parent #{_review_target}")
                    else:
                        # No clear verdict found — fail the subtask so timeout handles it
                        log(ctx, f"⚠ No clear verdict parsed — failing subtask (parent #{_review_target} handled by timeout)")
                        if ctx._review_parent_id and ctx.current_task_id != ctx._review_parent_id:
                            update_task_status(ctx, ctx.current_task_id, "failed",
                                               detail="Could not parse review verdict from output")
                    # Mark own subtask as done (if it's a review subtask)
                    if ctx._review_parent_id and ctx.current_task_id != ctx._review_parent_id:
                        _sub_status = "done" if (verdict == "approve" or not verdict) else "failed"
                        update_task_status(ctx, ctx.current_task_id, _sub_status)
                elif ctx.AGENT_NAME == "qa" or any(h in ctx.AGENT_NAME.lower() for h in ("qa", "test", "quality")):
                    # QA agent: update parent task status + mark own task done
                    _parent_status = "uat" if task_ok else "failed"
                    _parent_id = ctx._review_parent_id or ctx.current_task_id
                    if _parent_id and _parent_id != ctx.current_task_id:
                        update_task_status(ctx, _parent_id, _parent_status,
                                           detail="" if task_ok else "QA tests failed")
                    update_task_status(ctx, ctx.current_task_id, "done" if task_ok else "failed",
                                       detail="" if task_ok else "QA tests failed")
                    log(ctx, f"📝 QA result: {'pass → uat' if task_ok else 'fail'}")
                else:
                    log(ctx, f"ℹ {ctx.AGENT_NAME}: review/QA complete (not changing task status)")
            else:
                # Dev agents go to code_review, architect goes to done
                _SKIP_REVIEW_ROLES = {"architect"}
                # Check skip_review flag from task
                _task_skip_review = False
                if ctx.current_task_id:
                    try:
                        _task_data = hub_get(ctx, f"/tasks/{ctx.current_task_id}")
                        if _task_data and isinstance(_task_data, dict):
                            _task_skip_review = _task_data.get("skip_review", False)
                    except Exception:
                        pass
                if task_ok and ctx.AGENT_NAME not in _SKIP_REVIEW_ROLES and not _task_skip_review:
                    _final_status = "code_review"
                    _skip_unlock = True
                else:
                    _final_status = "done" if task_ok else "failed"
                # Architect with pending plan → keep task in_progress (plan approve will set done)
                if ctx.AGENT_NAME == "architect" and task_ok and ctx.current_task_id:
                    try:
                        _plans = hub_get(ctx, f"/pending-plans?creator={ctx.AGENT_NAME}&task_id={ctx.current_task_id}")
                        if _plans and isinstance(_plans, list) and len(_plans) > 0:
                            log(ctx, "📋 Plan proposal pending — task stays in_progress until approved")
                            _final_status = "in_progress"
                    except Exception:
                        pass
                update_task_status(ctx, ctx.current_task_id, _final_status, detail=fail_reason)
            # Classify and report task type for adaptive routing
            _task_type = ""
            _task_desc_lower = (getattr(ctx, '_task_summary', '') or '').lower()
            for _cat, _kws in [("frontend", ["frontend","ui","css","component","vue","react","page","layout","style"]),
                               ("backend", ["api","endpoint","database","migration","model","controller","middleware","sql"]),
                               ("testing", ["test","spec","e2e","playwright","cypress","coverage","lint"]),
                               ("devops", ["deploy","docker","ci","pipeline","k8s","infrastructure"]),
                               ("docs", ["docs","readme","documentation"])]:
                if any(kw in _task_desc_lower for kw in _kws):
                    _task_type = _cat
                    break
            hub_post(ctx, "/agents/specialization", {"agent_name": ctx.AGENT_NAME,
                "task_type": ctx.current_project or "general", "success": task_ok,
                "classified_type": _task_type})

        if not _skip_unlock:
            unlock_all(ctx)

        if is_task:
            run_hook(ctx, "post-task", {"success": task_ok, "project": ctx.current_project or ""})

        # Post-task voting on patterns
        if is_task and hasattr(ctx, '_active_pattern_ids') and ctx._active_pattern_ids:
            vote_val = 1 if task_ok else -1
            for pid in ctx._active_pattern_ids[:3]:
                try:
                    hub_post(ctx, f"/patterns/{pid}/vote",
                             {"agent_name": ctx.AGENT_NAME, "vote": vote_val})
                except Exception:
                    pass
            ctx._active_pattern_ids = []

        if is_task and task_ok and msgs:
            try:
                extract_learning(ctx, msgs[0].get("content", "")[:200])
            except Exception:
                pass

        # Calculate task metrics
        elapsed = int(time.time() - ctx._task_start_time) if ctx._task_start_time else 0
        elapsed_str = f"{elapsed // 60}m {elapsed % 60}s" if elapsed >= 60 else f"{elapsed}s"

        # Update task with metrics
        if ctx.current_task_id:
            hub_post(ctx, f"/tasks/{ctx.current_task_id}", {
                "elapsed_seconds": elapsed,
                "tokens_used": ctx.session_tokens,
                "claude_calls": ctx.claude_calls,
            })

        # Track consecutive failures for backoff
        if is_task:
            if task_ok:
                _consecutive_failures = 0
            else:
                _consecutive_failures += 1
                if _consecutive_failures >= 3:
                    _fail_backoff = min(120, 10 * 2 ** (_consecutive_failures - 3))
                    log(ctx, f"⚠ {_consecutive_failures} consecutive failures — backing off {_fail_backoff}s")
                    time.sleep(_fail_backoff)

        # Send final progress with correct token count before going idle
        report_progress(ctx, "task_done", f"{ctx.session_tokens:,} tok, {ctx.claude_calls} calls")
        set_status(ctx, "idle")
        ctx._task_start_time = 0
        ctx.message_count += 1
        update_session(ctx)
        log(ctx, f"✓ done — {elapsed_str}, {ctx.session_tokens:,} tokens ({ctx.claude_calls} calls)")
        ctx.current_task_id = None

        # Hook: on-agent-idle (fires after task completion)
        if is_task:
            run_hook(ctx, "on-agent-idle", {"completed_task": task_ok})

        # Immediate next-task pickup — don't wait for idle cycles
        if is_task:
            try:
                _next = hub_post(ctx, f"/tasks/auto-assign/{ctx.AGENT_NAME}", {})
                if _next and _next.get("status") == "ok" and _next.get("task"):
                    _nt = _next["task"]
                    log(ctx, f"📥 Next task #{_nt['id']}: {_nt.get('description', '')[:60]}")
                    # Re-queue as message so normal processing handles it
                    hub_post(ctx, "/messages", {
                        "sender": _nt.get("created_by", "system"),
                        "receiver": ctx.AGENT_NAME,
                        "content": f"#{_nt['id']} {_nt.get('description', '')}",
                        "msg_type": "task",
                        "task_id": str(_nt["id"]),
                        "project": _nt.get("project", ""),
                        "branch": _nt.get("branch", ""),
                        "task_external_id": _nt.get("task_external_id", ""),
                    })
                    ctx.idle_count = 0  # Skip idle wait on next iteration
            except Exception:
                pass

    except KeyboardInterrupt:
        stop_chat_handler(ctx)
        flush_logs(ctx)
        unlock_all(ctx)
        break
    except ConnectionError as e:
        stop_chat_handler(ctx)
        log(ctx, f"CONNECTION ERROR: {e} — will retry hub connection")
        if ctx.current_task_id:
            # Auto-retry logic
            should_retry, retry_hint = _should_auto_retry(ctx, ctx.current_task_id, str(e))
            if should_retry:
                log(ctx, f"↻ Auto-retrying task #{ctx.current_task_id}: {retry_hint}")
                try:
                    _t = hub_get(ctx, f"/tasks/{ctx.current_task_id}") or {}
                    hub_post(ctx, f"/tasks/{ctx.current_task_id}", {
                        "_retry_count": (_t.get("_retry_count", 0) + 1), "status": "to_do"})
                    hub_msg(ctx, "user", f"↻ Auto-retrying task #{ctx.current_task_id} ({_classify_error(str(e))}): {retry_hint}", "info")
                except Exception:
                    pass
            else:
                update_task_status(ctx, ctx.current_task_id, "failed", detail=f"Connection lost: {str(e)[:200]}")
        time.sleep(5)
        continue
    except TimeoutError as e:
        stop_chat_handler(ctx)
        log(ctx, f"TIMEOUT ERROR: {e}")
        if ctx.current_task_id:
            # Auto-retry logic
            should_retry, retry_hint = _should_auto_retry(ctx, ctx.current_task_id, str(e))
            if should_retry:
                log(ctx, f"↻ Auto-retrying task #{ctx.current_task_id}: {retry_hint}")
                try:
                    _t = hub_get(ctx, f"/tasks/{ctx.current_task_id}") or {}
                    hub_post(ctx, f"/tasks/{ctx.current_task_id}", {
                        "_retry_count": (_t.get("_retry_count", 0) + 1), "status": "to_do"})
                    hub_msg(ctx, "user", f"↻ Auto-retrying task #{ctx.current_task_id} ({_classify_error(str(e))}): {retry_hint}", "info")
                except Exception:
                    pass
            else:
                update_task_status(ctx, ctx.current_task_id, "failed", detail=f"Timed out: {str(e)[:200]}")
                hub_msg(ctx, "user", f"⏱ {ctx.AGENT_NAME}: task #{ctx.current_task_id} timed out: {str(e)[:100]}", "info")
        unlock_all(ctx)
        set_status(ctx, "idle")
        ctx.current_task_id = None
        ctx._task_start_time = 0
        time.sleep(3)
        continue
    except json.JSONDecodeError as e:
        log(ctx, f"JSON ERROR: {e} — skipping malformed data")
        time.sleep(1)
        continue
    except Exception as e:
        stop_chat_handler(ctx)
        import traceback
        tb = traceback.format_exc()
        log(ctx, f"LOOP ERROR: {e}\n{tb[-500:]}")
        if ctx.current_task_id:
            # Auto-retry logic
            should_retry, retry_hint = _should_auto_retry(ctx, ctx.current_task_id, str(e))
            if should_retry:
                log(ctx, f"↻ Auto-retrying task #{ctx.current_task_id}: {retry_hint}")
                try:
                    _t = hub_get(ctx, f"/tasks/{ctx.current_task_id}") or {}
                    hub_post(ctx, f"/tasks/{ctx.current_task_id}", {
                        "_retry_count": (_t.get("_retry_count", 0) + 1), "status": "to_do"})
                    hub_msg(ctx, "user", f"↻ Auto-retrying task #{ctx.current_task_id} ({_classify_error(str(e))}): {retry_hint}", "info")
                except Exception:
                    pass
            else:
                update_task_status(ctx, ctx.current_task_id, "failed", detail=f"Crashed: {str(e)[:300]}")
                hub_msg(ctx, "user", f"❌ {ctx.AGENT_NAME}: task #{ctx.current_task_id} crashed: {str(e)[:200]}", "info")
        hub_post(ctx, "/health/crash", {"agent_name": ctx.AGENT_NAME, "error": str(e),
                                        "exit_code": -1, "context": f"task={ctx.current_task_id}"})
        unlock_all(ctx)
        # Session recovery: reset session on crash to get fresh state
        ctx.SESSION_ID = None
        try:
            os.remove(ctx.SESSION_FILE)
        except OSError:
            pass
        set_status(ctx, "idle")
        ctx.current_task_id = None
        ctx._task_start_time = 0
        # Brief pause before next loop iteration to avoid crash loops
        time.sleep(3)
