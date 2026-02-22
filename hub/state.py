"""hub/state.py — Shared state, models, persistence, config, and utility functions."""

import os, json, time, logging, threading, re, subprocess, shutil
from datetime import datetime, timedelta
from typing import Dict, List
from collections import deque
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("hub")

lock = threading.Lock()
_shutdown_event = threading.Event()

MA_DIR = os.environ.get("MA_DIR", "")
WORKSPACE = os.environ.get("WORKSPACE", "")

# ── Config ──
_cfg = {}
for _p in [os.path.join(WORKSPACE, "multiagent.json"),
           os.path.join(MA_DIR, "config.json") if MA_DIR else ""]:
    if _p and os.path.exists(_p):
        try:
            with open(_p) as _f:
                _cfg = json.load(_f)
        except Exception as e:
            logger.warning(f"Config load error: {e}")
        break

ALL_AGENTS = [a["name"] if isinstance(a, dict) else a for a in _cfg.get("agents", ["architect", "frontend", "backend", "qa"])]
HIDDEN_AGENTS = set()
for _a in _cfg.get("agents", []):
    if isinstance(_a, dict) and _a.get("hidden"):
        HIDDEN_AGENTS.add(_a["name"])
VISIBLE_AGENTS = [a for a in ALL_AGENTS if a not in HIDDEN_AGENTS]

# ── Ensure reviewers always exist (even if multiagent.json omits them) ──
_REQUIRED_REVIEWERS = ["reviewer-logic", "reviewer-style", "reviewer-arch"]
for _rn in _REQUIRED_REVIEWERS:
    if _rn not in ALL_AGENTS:
        ALL_AGENTS.append(_rn)
    HIDDEN_AGENTS.add(_rn)
VISIBLE_AGENTS[:] = [a for a in ALL_AGENTS if a not in HIDDEN_AGENTS]

MAX_TASKS = 500
BUDGET_LIMIT = float(_cfg.get("budget_limit", 0))
BUDGET_PER_AGENT = float(_cfg.get("budget_per_agent", 0))

# ── Models ──
class Message(BaseModel):
    model_config = {"extra": "allow"}
    sender: str; receiver: str; content: str; msg_type: str = "message"
    task_external_id: str = ""
    task_id: str = ""

class AgentStatus(BaseModel):
    agent_name: str; status: str; detail: str = ""

class SessionInfo(BaseModel):
    agent_name: str; session_id: str = ""; message_count: int = 0
    started_at: str = ""; claude_calls: int = 0

class CostEntry(BaseModel):
    agent_name: str; tokens_in: int = 0; tokens_out: int = 0; model: str = ""

class FileLock(BaseModel):
    file_path: str; agent_name: str

# ── State ──
messages: Dict[str, List[dict]] = {}
agents: Dict[str, dict] = {}
pipeline: Dict[str, dict] = {}
sessions: Dict[str, dict] = {}
usage_log: Dict[str, dict] = {}
file_locks: Dict[str, dict] = {}
tasks: Dict[int, dict] = {}
changes: List[dict] = []
change_counter = 0
activity: deque = deque(maxlen=500)
stop_signals: Dict[str, bool] = {}
analytics_log: List[dict] = []
MAX_ANALYTICS = 1000
rate_limited_agents: Dict[str, float] = {}
sse_clients: Dict[str, int] = {}
agent_progress: Dict[str, dict] = {}
test_results: List[dict] = []
agent_specialization: Dict[str, dict] = {}
agent_learnings: List[dict] = []
agent_roles: Dict[str, str] = {}  # {agent_name: role_description}
file_plans: Dict[str, dict] = {}  # {agent_name: {task_id, files, timestamp}} — transient, no persistence

# ── Plan Proposals ──
pending_plans: Dict[int, dict] = {}  # {plan_id: {steps, project, branch, status, created_by, created}}
_plan_counter = 0

# ── Pattern Registry ──
pattern_registry: Dict[str, dict] = {}
_pattern_id_counter = 0
PATTERN_SCORE_CAP = (10, -5)  # (max, min)
PATTERN_PRUNE_AT = -3
PATTERN_CATEGORIES = ["playwright", "figma", "vue", "react", "testing", "i18n",
                      "routing", "mcp", "backend", "database", "devops", "security", "general"]

log_buffers: Dict[str, deque] = {a: deque(maxlen=3000) for a in ALL_AGENTS}
log_counters: Dict[str, int] = {a: 0 for a in ALL_AGENTS}
msg_rate: Dict[str, list] = {}
MSG_RATE_LIMIT = 60

chat_queue: Dict[str, list] = {name: [] for name in ALL_AGENTS}

crash_log: List[dict] = []
user_sessions: Dict[str, dict] = {}

audit_log: deque = deque(maxlen=1000)

# ── Metrics ──
request_counts: Dict[str, int] = {}  # {endpoint: count}
error_counts: Dict[str, int] = {}  # {endpoint_status: count}

# ── Change Detection ──
_state_version = 0  # Monotonically increasing counter, bumped on any state mutation

def bump_version():
    """Increment state version counter. Call after any state mutation."""
    global _state_version
    _state_version += 1

def get_version():
    """Get current state version for change detection."""
    return _state_version

def request_shutdown():
    """Signal all background threads to stop."""
    _shutdown_event.set()

def is_shutting_down():
    """Check if shutdown has been requested."""
    return _shutdown_event.is_set()

# ── Routing ──
ROUTE_MAP = {
    "frontend": ["frontend", "vue", "react", "css", "scss", "blade", "component", "ui", "tailwind", "template", "page", "layout", "style", "html", "svelte", "next", "nuxt", "jsx", "tsx", "ux", "button", "form", "modal", "sidebar", "navbar", "responsive", "figma", "design", "mockup", "prototype", "wireframe"],
    "backend": ["backend", "api", "endpoint", "database", "migration", "laravel", "golang", "model", "controller", "route", "middleware", "queue", "job", "artisan", "schema", "sql", "prisma", "django", "fastapi", "express", "nest", "php", "python", "node", "redis", "graphql", "rest", "webhook"],
    "qa": ["test", "review", "bug", "broken", "failing", "lint", "quality", "coverage", "regression", "e2e", "cypress", "playwright", "verify", "check", "spec"],
    "friday": ["sentry", "error tracking", "crash", "exception", "monitoring", "alert", "deploy", "devops", "infrastructure", "ci/cd", "pipeline", "docker", "k8s", "kubernetes", "server", "sentry.io", "error report", "stack trace", "500 error", "timeout error", "memory leak"],
}
MULTI_SCOPE_KEYWORDS = ["and also", "frontend and backend", "full stack", "both", "refactor", "redesign", "migration", "feature", "epic", "story", "end to end", "fullstack", "google doc", "google sheet", "google slide", "spreadsheet", "presentation"]

# ── Task State Machine ──
TASK_STATES = ["to_do", "in_progress", "code_review", "in_testing", "uat", "done", "failed",
               # Legacy states kept for backward compat transition validation
               "created", "assigned", "in_review", "cancelled", "blocked_by_failure"]
VALID_TRANSITIONS = {
    "to_do": {"in_progress", "failed", "cancelled"},
    "in_progress": {"code_review", "done", "failed", "cancelled"},
    "code_review": {"in_progress", "in_testing", "failed"},
    "in_testing": {"in_progress", "uat", "failed"},
    "uat": {"done", "in_progress", "failed"},
    "done": {"to_do"},
    "failed": {"to_do", "in_progress"},
    # Legacy states — allow transitions out
    "created": {"to_do", "assigned", "in_progress", "cancelled"},
    "assigned": {"in_progress", "cancelled", "to_do"},
    "in_review": {"done", "failed", "in_progress", "code_review"},
    "cancelled": {"to_do"},
    "blocked_by_failure": {"to_do", "assigned", "cancelled"},
}

# ── Status Migration (legacy → new) ──
STATUS_MIGRATION = {"created": "to_do", "assigned": "in_progress", "in_review": "code_review"}

# ── Review / Comment State ──
task_comments: Dict[str, list] = {}    # {task_id_str: [{id, agent, text, timestamp, resolved}]}
task_reviews: Dict[str, dict] = {}     # {task_id_str: {agent_name: {verdict, comments, timestamp}}}
_comment_counter = 0
MAX_REWORK_LOOPS = 3                   # Max code_review rework cycles before auto-approve

# ── Notifications ──
notification_config = _cfg.get("notifications_webhook", {})

# ── Auto-Scale ──
auto_scale_config = _cfg.get("auto_scale", {"enabled": False, "min_agents": 2, "max_agents": 8, "queue_threshold": 3})

# ── Service Registry ──
SERVICE_REGISTRY = [
    {
        "id": "atlassian", "name": "Atlassian (Jira/Confluence)", "mcp": "atlassian",
        "icon": "\U0001f537", "color": "#0052CC",
        "auth_url": "https://mcp.atlassian.com",
        "credentials": [],
        "connected": True,
        "docs": "https://www.atlassian.com/platform/remote-mcp-server",
        "setup_note": "Official remote MCP server — authenticates via OAuth in browser. No API tokens needed.",
    },
    {
        "id": "figma", "name": "Figma", "mcp": "figma",
        "icon": "\U0001f3a8", "color": "#F24E1E",
        "auth_url": "https://mcp.figma.com",
        "credentials": [],
        "connected": True,
        "docs": "https://help.figma.com/hc/en-us/articles/32132956003351-Guide-to-the-Figma-MCP-Server",
        "setup_note": "Official remote MCP server — authenticates via OAuth in browser. No API tokens needed.",
    },
    {
        "id": "github", "name": "GitHub", "mcp": "github",
        "icon": "\U0001f419", "color": "#24292e",
        "auth_url": "https://github.com/settings/tokens/new",
        "credentials": [
            {"key": "GITHUB_PERSONAL_ACCESS_TOKEN", "label": "Personal Access Token", "type": "password",
             "help": "Settings → Developer settings → Personal access tokens → Fine-grained tokens"},
        ],
        "docs": "https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens",
        "fallback_hint": "gh api repos/OWNER/REPO/issues/NUM (uses $GITHUB_PERSONAL_ACCESS_TOKEN)",
    },
    {
        "id": "sentry", "name": "Sentry", "mcp": "sentry",
        "icon": "\U0001f534", "color": "#362D59",
        "auth_url": "https://mcp.sentry.dev",
        "credentials": [],
        "connected": True,
        "docs": "https://docs.sentry.io/organization/integrations/integration-platform/internal-integration/",
        "setup_note": "Official remote MCP server — authenticates via OAuth in browser. No API tokens needed.",
    },
    {
        "id": "google", "name": "Google Workspace", "mcp": "google",
        "icon": "\U0001f4e7", "color": "#4285F4",
        "auth_url": "https://console.cloud.google.com/apis/credentials",
        "credentials": [
            {"key": "GOOGLE_CLIENT_ID", "label": "OAuth Client ID", "type": "text",
             "placeholder": "123456789.apps.googleusercontent.com",
             "help": "Cloud Console → APIs → Credentials → Create OAuth Client ID (Desktop app)"},
            {"key": "GOOGLE_CLIENT_SECRET", "label": "OAuth Client Secret", "type": "password",
             "placeholder": "GOCSPX-...",
             "help": "Same page as Client ID, under Client Secret"},
            {"key": "GOOGLE_REFRESH_TOKEN", "label": "Refresh Token", "type": "password",
             "placeholder": "1//0...",
             "help": "After creating OAuth credentials, run: GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... npx google-workspace-mcp get-token"},
        ],
        "docs": "https://github.com/VolksRat71/google-workspace-mcp#setup",
        "fallback_hint": "WebFetch the Google Doc/Sheet URL directly",
        "setup_note": "1) Enable Docs/Sheets/Slides/Drive APIs in Cloud Console\n2) Create OAuth Desktop client\n3) Run get-token to get refresh token",
    },
    {
        "id": "gitlab", "name": "GitLab",
        "icon": "\U0001f98a", "color": "#FC6D26",
        "auth_url": "https://gitlab.com/-/user_settings/personal_access_tokens",
        "credentials": [
            {"key": "GITLAB_TOKEN", "label": "Personal Access Token", "type": "password",
             "help": "Preferences → Access Tokens → Add new token"},
        ],
        "docs": "https://docs.gitlab.com/ee/user/profile/personal_access_tokens.html",
    },
    {
        "id": "slack", "name": "Slack",
        "icon": "\U0001f4ac", "color": "#4A154B",
        "auth_url": "https://api.slack.com/apps",
        "credentials": [
            {"key": "SLACK_BOT_TOKEN", "label": "Bot Token (xoxb-...)", "type": "password",
             "help": "Create app → OAuth & Permissions → Bot User OAuth Token"},
        ],
        "docs": "https://api.slack.com/authentication/token-types",
    },
    {
        "id": "linear", "name": "Linear",
        "icon": "\U0001f4d0", "color": "#5E6AD2",
        "auth_url": "https://linear.app/settings/api",
        "credentials": [
            {"key": "LINEAR_API_KEY", "label": "API Key", "type": "password",
             "help": "Settings → API → Personal API keys → Create key"},
        ],
        "docs": "https://developers.linear.app/docs/graphql/working-with-the-graphql-api#personal-api-keys",
    },
    {
        "id": "notion", "name": "Notion",
        "icon": "\U0001f4dd", "color": "#000000",
        "auth_url": "https://www.notion.so/my-integrations",
        "credentials": [
            {"key": "NOTION_TOKEN", "label": "Integration Token", "type": "password",
             "help": "My Integrations → New integration → Internal integration secret"},
        ],
        "docs": "https://developers.notion.com/docs/create-a-notion-integration",
    },
    {
        "id": "vercel", "name": "Vercel",
        "icon": "\u25b2", "color": "#000000",
        "auth_url": "https://vercel.com/account/tokens",
        "credentials": [
            {"key": "VERCEL_TOKEN", "label": "Access Token", "type": "password",
             "help": "Account Settings → Tokens → Create"},
        ],
        "docs": "https://vercel.com/docs/rest-api#authentication",
    },
    {
        "id": "supabase", "name": "Supabase",
        "icon": "\u26a1", "color": "#3ECF8E",
        "auth_url": "https://supabase.com/dashboard/account/tokens",
        "credentials": [
            {"key": "SUPABASE_ACCESS_TOKEN", "label": "Access Token", "type": "password",
             "help": "Account → Access Tokens → Generate new token"},
            {"key": "SUPABASE_PROJECT_URL", "label": "Project URL", "type": "text",
             "help": "e.g. https://xxx.supabase.co", "placeholder": "https://xxx.supabase.co"},
        ],
        "docs": "https://supabase.com/docs/guides/api#api-url-and-keys",
    },
    {
        "id": "custom", "name": "Custom Service",
        "icon": "\U0001f527", "color": "#666666",
        "auth_url": "",
        "credentials": [],
        "docs": "",
    },
]

# ── MCP Content Cache ──
CACHE_DIR = os.path.join(MA_DIR, "cache") if MA_DIR else ""
if CACHE_DIR:
    os.makedirs(CACHE_DIR, exist_ok=True)
cache_registry: Dict[str, dict] = {}  # {key: {path, source, content_type, description, created, size}}

# ── Log Disk Persistence ──
LOG_DIR = os.path.join(MA_DIR, "logs") if MA_DIR else ""
if LOG_DIR:
    os.makedirs(LOG_DIR, exist_ok=True)

def _log_file(name):
    return os.path.join(LOG_DIR, f"{name}.log") if LOG_DIR else ""

def _load_logs_from_disk():
    if not LOG_DIR:
        return
    try:
        if not os.path.isdir(LOG_DIR):
            return
    except OSError:
        return
    for name in ALL_AGENTS:
        lf = _log_file(name)
        try:
            if not os.path.exists(lf):
                continue
            sz = os.path.getsize(lf)
            if sz > 5_000_000:
                with open(lf, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(max(0, sz - 500_000))
                    f.readline()
                    lines = f.readlines()
            else:
                with open(lf, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
            for line in lines[-2000:]:
                log_buffers[name].append(line.rstrip("\n"))
                log_counters[name] += 1
        except Exception as e:
            logger.warning(f"load log {name}: {e}")

def _append_log_disk(name, lines):
    lf = _log_file(name)
    if not lf:
        return
    try:
        with open(lf, "a", encoding="utf-8", errors="replace") as f:
            for line in lines:
                f.write(str(line) + "\n")
        try:
            if os.path.getsize(lf) > 2_000_000:
                bak = lf + ".1"
                try:
                    os.remove(bak)
                except OSError:
                    pass
                os.rename(lf, bak)
        except OSError:
            pass
    except Exception as e:
        logger.warning(f"Log write error for {name}: {e}")

try:
    _load_logs_from_disk()
except Exception as e:
    logger.warning(f"Failed to load logs from disk: {e}")

# ── State Persistence ──
STATE_FILE = os.path.join(MA_DIR, "hub_state.json") if MA_DIR else ""
_save_pending = False
_last_save = 0

def save_state():
    global _save_pending, _last_save
    bump_version()
    if not STATE_FILE:
        return
    if time.time() - _last_save < 5:
        _save_pending = True
        return
    _do_save()

def _rotate_backups():
    """Rotate state backups: .bak.3 → delete, .bak.2 → .bak.3, .bak.1 → .bak.2, current → .bak.1"""
    if not STATE_FILE:
        return
    try:
        for i in range(3, 1, -1):
            src = f"{STATE_FILE}.bak.{i-1}"
            dst = f"{STATE_FILE}.bak.{i}"
            if os.path.exists(src):
                try:
                    os.replace(src, dst)
                except OSError:
                    pass
        # Copy current to .bak.1
        if os.path.exists(STATE_FILE):
            import shutil
            try:
                shutil.copy2(STATE_FILE, f"{STATE_FILE}.bak.1")
            except OSError:
                pass
    except Exception as e:
        logger.warning(f"Backup rotation error: {e}")

def _do_save():
    global _save_pending, _last_save
    if not STATE_FILE:
        return
    _save_pending = False
    _last_save = time.time()
    try:
        # Task cleanup under lock (rare path — only when > MAX_TASKS)
        task_snapshot = dict(tasks)
        if len(task_snapshot) > MAX_TASKS:
            with lock:
                task_list = sorted(tasks.items(), key=lambda x: x[0])
                active = {k: v for k, v in task_list if v.get("status") not in ("done", "failed", "cancelled")}
                recent = dict(task_list[-MAX_TASKS:])
                active.update(recent)
                tasks.clear()
                tasks.update(active)
                task_snapshot = dict(tasks)
        # Build snapshot without lock — GIL ensures atomic dict/list reads
        snapshot = {
            "tasks": task_snapshot, "usage_log": dict(usage_log), "sessions": dict(sessions),
            "changes": list(changes[-100:]), "change_counter": change_counter,
            "user_messages": list(messages.get("user", []))[-200:],
            "analytics": list(analytics_log[-500:]),
            "test_results": list(test_results[-200:]),
            "specialization": dict(agent_specialization),
            "learnings": list(agent_learnings[-200:]),
            "agent_roles": dict(agent_roles),
            "pending_plans": dict(pending_plans),
            "plan_counter": _plan_counter,
            "cache_registry": dict(cache_registry),
            "patterns": dict(pattern_registry),
            "pattern_id_counter": _pattern_id_counter,
            "messages": {k: list(v)[-150:] for k, v in dict(messages).items()},
            "task_comments": dict(task_comments),
            "task_reviews": dict(task_reviews),
            "comment_counter": _comment_counter,
        }
        # Atomic write: temp file + rename
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(snapshot, f, default=str)
        os.replace(tmp, STATE_FILE)  # atomic on POSIX
        # Rotate backups: keep last 3
        _rotate_backups()
    except Exception as e:
        logger.warning(f"save: {e}")

_last_backup = 0

def _save_timer():
    global _last_backup
    while not _shutdown_event.is_set():
        _shutdown_event.wait(10)
        if _shutdown_event.is_set():
            break
        if _save_pending:
            _do_save()
        # Periodic backup every 5 minutes
        now = time.time()
        if now - _last_backup > 300:
            _last_backup = now
            _rotate_backups()

def load_state():
    global tasks, usage_log, sessions, changes, change_counter, analytics_log
    global test_results, agent_specialization, agent_learnings
    global pattern_registry, _pattern_id_counter
    global pending_plans, _plan_counter, cache_registry
    global task_comments, task_reviews, _comment_counter
    if not STATE_FILE or not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE) as f:
            raw = f.read()
        if not raw.strip():
            raise ValueError("Empty state file")
        s = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"Corrupt state file: {e}, trying backups...")
        s = None
        for i in range(1, 4):
            bak = f"{STATE_FILE}.bak.{i}"
            if os.path.exists(bak):
                try:
                    with open(bak) as f:
                        s = json.load(f)
                    logger.info(f"Restored from backup {bak}")
                    break
                except (json.JSONDecodeError, OSError):
                    continue
        if s is None:
            logger.error("All state backups corrupt or missing, starting fresh")
            return
    try:
        tasks.update({int(k): v for k, v in s.get("tasks", {}).items()})
        usage_log.update(s.get("usage_log", {}))
        sessions.update(s.get("sessions", {}))
        changes.extend(s.get("changes", []))
        change_counter = s.get("change_counter", 0)
        um = s.get("user_messages", [])
        if um:
            messages["user"] = um
        saved_msgs = s.get("messages", {})
        for k, v in saved_msgs.items():
            if k not in messages or not messages[k]:
                messages[k] = v
        analytics_log.extend(s.get("analytics", []))
        test_results.extend(s.get("test_results", []))
        agent_specialization.update(s.get("specialization", {}))
        agent_learnings.extend(s.get("learnings", []))
        agent_roles.update(s.get("agent_roles", {}))
        pending_plans.update({int(k): v for k, v in s.get("pending_plans", {}).items()})
        _plan_counter = s.get("plan_counter", 0)
        cache_registry.update(s.get("cache_registry", {}))
        pattern_registry.update(s.get("patterns", {}))
        _pattern_id_counter = s.get("pattern_id_counter", 0)
        task_comments.update(s.get("task_comments", {}))
        task_reviews.update(s.get("task_reviews", {}))
        _comment_counter = s.get("comment_counter", 0)
        # ── Migrate legacy task statuses ──
        for tid, task in tasks.items():
            old_status = task.get("status", "")
            if old_status in STATUS_MIGRATION:
                task["status"] = STATUS_MIGRATION[old_status]
                logger.info(f"Migrated task #{tid}: {old_status} → {task['status']}")
        logger.info(f"Restored: {len(tasks)} tasks, {len(pattern_registry)} patterns, {len(um)} inbox")
    except Exception as e:
        logger.warning(f"load: {e}")


def reset_session():
    """Reset volatile state for a fresh session. Keeps patterns, learnings, and completed task data."""
    with lock:
        # Clear agents — they re-register on boot
        agents.clear()
        # Reset in_progress/code_review/in_testing tasks to to_do (agent is gone)
        # Review subtasks get cancelled (not reset to to_do — they'll be re-created)
        for tid, task in tasks.items():
            if task.get("_is_review_subtask"):
                if task.get("status") not in ("done", "failed", "cancelled"):
                    task["status"] = "cancelled"
                    task["completed_at"] = datetime.now().isoformat()
            elif task.get("status") in ("in_progress", "code_review", "in_testing"):
                task["status"] = "to_do"
                task.pop("assigned_to", None)
                task.pop("review_dispatched_at", None)
                task.pop("_review_subtask_ids", None)
        # Dismiss stale pending plans
        for pid, plan in pending_plans.items():
            if plan.get("status") == "pending":
                plan["status"] = "dismissed"
        # Clear volatile state
        task_reviews.clear()
        messages.clear()
        analytics_log.clear()
        activity.clear()
        changes.clear()
        file_locks.clear()
        file_plans.clear()
        stop_signals.clear()
        agent_progress.clear()
        sessions.clear()
        # Clear MCP cache (stale Jira/Figma/GitHub content from old sessions)
        cache_registry.clear()
        if CACHE_DIR:
            import shutil
            for f in os.listdir(CACHE_DIR):
                fp = os.path.join(CACHE_DIR, f)
                try:
                    if os.path.isfile(fp):
                        os.remove(fp)
                except OSError:
                    pass
        # Clear log buffers (old session logs)
        for name in log_buffers:
            log_buffers[name].clear()
            log_counters[name] = 0
        # Truncate log files on disk
        if LOG_DIR:
            for name in ALL_AGENTS:
                lf = _log_file(name)
                if lf and os.path.exists(lf):
                    try:
                        open(lf, "w").close()
                    except OSError:
                        pass
        bump_version()
        save_state()
        logger.info("Session reset: cleared agents, logs, messages; reset stale tasks to to_do")


# ── Config Hot-Reload ──
_cfg_mtime = 0

def _config_reload_timer():
    global _cfg, BUDGET_LIMIT, BUDGET_PER_AGENT, notification_config, _cfg_mtime, auto_scale_config
    global ALL_AGENTS, HIDDEN_AGENTS, VISIBLE_AGENTS
    while not _shutdown_event.is_set():
        _shutdown_event.wait(15)
        if _shutdown_event.is_set():
            break
        for p in [os.path.join(WORKSPACE, "multiagent.json"),
                  os.path.join(MA_DIR, "config.json") if MA_DIR else ""]:
            if not p or not os.path.exists(p):
                continue
            try:
                mtime = os.path.getmtime(p)
                if mtime <= _cfg_mtime:
                    continue
                _cfg_mtime = mtime
                with open(p) as f:
                    new_cfg = json.load(f)
                with lock:
                    BUDGET_LIMIT = float(new_cfg.get("budget_limit", BUDGET_LIMIT))
                    BUDGET_PER_AGENT = float(new_cfg.get("budget_per_agent", BUDGET_PER_AGENT))
                    if "notifications_webhook" in new_cfg:
                        notification_config = new_cfg["notifications_webhook"]
                    if "auto_scale" in new_cfg:
                        auto_scale_config.update(new_cfg["auto_scale"])
                    # Hot-reload agent list (new agents, hidden flags)
                    new_agents_cfg = new_cfg.get("agents", [])
                    if new_agents_cfg:
                        new_all = [a["name"] if isinstance(a, dict) else a for a in new_agents_cfg]
                        new_hidden = set()
                        for a in new_agents_cfg:
                            if isinstance(a, dict) and a.get("hidden"):
                                new_hidden.add(a["name"])
                        # Inject required reviewers into reloaded config
                        for _rn in _REQUIRED_REVIEWERS:
                            if _rn not in new_all:
                                new_all.append(_rn)
                            new_hidden.add(_rn)
                        if set(new_all) != set(ALL_AGENTS) or new_hidden != HIDDEN_AGENTS:
                            ALL_AGENTS[:] = new_all
                            HIDDEN_AGENTS.clear()
                            HIDDEN_AGENTS.update(new_hidden)
                            VISIBLE_AGENTS[:] = [a for a in ALL_AGENTS if a not in HIDDEN_AGENTS]
                            # Init message queues and log buffers for new agents
                            for a in ALL_AGENTS:
                                if a not in messages:
                                    messages[a] = deque(maxlen=200)
                                if a not in log_buffers:
                                    log_buffers[a] = deque(maxlen=2000)
                            logger.info(f"Agents updated: {ALL_AGENTS} (hidden: {HIDDEN_AGENTS})")
                    _cfg.update(new_cfg)
                logger.info(f"Config reloaded: budget=${BUDGET_LIMIT}, notifications={'on' if notification_config.get('url') else 'off'}")
            except Exception as e:
                logger.warning(f"Config reload error: {e}")
            break

# ── Helpers ──
def add_activity(sender, receiver, msg_type, content):
    activity.append({"time": datetime.now().isoformat(), "sender": sender,
                     "receiver": receiver, "type": msg_type, "preview": content[:200]})
    bump_version()

def add_audit(actor: str, action: str, details: dict = None):
    """Add an audit log entry for state-changing operations."""
    audit_log.append({
        "ts": datetime.now().isoformat(),
        "actor": actor,
        "action": action,
        "details": details or {},
    })

def rate_ok(sender):
    now = datetime.now()
    cutoff = now - timedelta(seconds=60)
    if sender not in msg_rate:
        msg_rate[sender] = []
    msg_rate[sender] = [t for t in msg_rate[sender] if t > cutoff]
    if len(msg_rate[sender]) >= MSG_RATE_LIMIT:
        return False
    msg_rate[sender].append(now)
    return True

# ── Cost Calculations ──
# Pricing per 1M tokens (Feb 2026): Opus 4.6 $5/$25, Sonnet 4.6 $3/$15, Haiku 4.5 $1/$5
_PRICING = {
    "sonnet_in": 3, "sonnet_out": 15,
    "opus_in": 5, "opus_out": 25,
    "haiku_in": 1, "haiku_out": 5,
}

def calc_agent_cost(name):
    u = usage_log.get(name, {})
    si, so = u.get("sonnet_in", 0), u.get("sonnet_out", 0)
    oi, oo = u.get("opus_in", 0), u.get("opus_out", 0)
    hi, ho = u.get("haiku_in", 0), u.get("haiku_out", 0)
    cost = ((si / 1e6) * _PRICING["sonnet_in"] + (so / 1e6) * _PRICING["sonnet_out"] +
            (oi / 1e6) * _PRICING["opus_in"] + (oo / 1e6) * _PRICING["opus_out"] +
            (hi / 1e6) * _PRICING["haiku_in"] + (ho / 1e6) * _PRICING["haiku_out"])
    if not si and not so and not oi and not oo and not hi and not ho:
        ti, to_ = u.get("tokens_in", 0), u.get("tokens_out", 0)
        # Assume 70% opus, 30% sonnet mix when model breakdown unavailable
        cost = ((ti * 0.7 / 1e6) * _PRICING["opus_in"] + (to_ * 0.7 / 1e6) * _PRICING["opus_out"] +
                (ti * 0.3 / 1e6) * _PRICING["sonnet_in"] + (to_ * 0.3 / 1e6) * _PRICING["sonnet_out"])
    return round(cost, 4)

def calc_total_cost():
    all_names = set(ALL_AGENTS) | set(usage_log.keys())
    return sum(calc_agent_cost(n) for n in all_names)

# ── Notifications ──
def send_notification(event_type, message):
    url = notification_config.get("url", "")
    if not url:
        return
    allowed = notification_config.get("events", ["task_done", "task_failed", "blocker", "budget_warn"])
    if event_type not in allowed:
        return
    ntype = notification_config.get("type", "generic")
    try:
        import urllib.request
        if ntype == "slack":
            payload = json.dumps({"text": f"\U0001f916 Multi-Agent: {message}"}).encode()
        elif ntype == "discord":
            payload = json.dumps({"content": f"\U0001f916 Multi-Agent: {message}"}).encode()
        else:
            payload = json.dumps({"event": event_type, "message": message, "timestamp": datetime.now().isoformat()}).encode()
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
        add_activity("system", "webhook", "notification", f"{event_type}: {message[:80]}")
    except Exception as e:
        logger.warning(f"Notification failed: {e}")

# ── Git Helper ──
def git_cmd(args, cwd=None):
    try:
        r = subprocess.run(["git"] + args, cwd=cwd or WORKSPACE, capture_output=True, text=True, timeout=15)
        return r.returncode == 0, r.stdout.strip()
    except Exception as e:
        logger.warning(f"Git command error {args}: {e}")
        return False, ""

def safe_project_dir(project):
    if not project:
        return None
    clean = os.path.basename(project.replace("\\", "/"))
    if not clean or clean.startswith(".") or ".." in clean:
        return None
    d = os.path.join(WORKSPACE, clean)
    if not os.path.realpath(d).startswith(os.path.realpath(WORKSPACE)):
        return None
    return d

# ── Credentials file path ──
creds_file = os.path.join(MA_DIR, "credentials.env") if MA_DIR else ""

# ── Cached Dashboard Snapshot ──
_snapshot_cache = {"data": None, "version": -1}

def get_dashboard_snapshot():
    """Get cached dashboard snapshot. Rebuilds only when state version changes.
    No lock needed — GIL ensures atomic reads, and dashboard display tolerates
    brief inconsistency. Multiple WebSocket clients share the same cached result."""
    v = _state_version
    if _snapshot_cache["version"] == v and _snapshot_cache["data"] is not None:
        return _snapshot_cache["data"]
    data = _build_dashboard_data()
    _snapshot_cache["data"] = data
    _snapshot_cache["version"] = v
    return data

def _build_dashboard_data():
    """Build full dashboard data dict. Called without lock — safe for display.
    Quick dict() copies prevent RuntimeError from concurrent dict modifications."""
    now = datetime.now()
    # Quick copies to avoid RuntimeError during dict iteration
    # Filter hidden agents from dashboard display
    agent_names = [a for a in ALL_AGENTS if a not in HIDDEN_AGENTS]
    msgs_snap = dict(messages)
    usage_snap = dict(usage_log)
    locks_snap = dict(file_locks)
    changes_snap = list(changes)
    tasks_snap = [t for t in tasks.values() if not t.get("_is_review_subtask")]

    ai = {}
    for n in agent_names:
        a = agents.get(n, {})
        try:
            last = datetime.fromisoformat(a.get("last_seen", "2000-01-01"))
            silent = int((now - last).total_seconds())
        except (ValueError, TypeError):
            silent = 999
        ss = sessions.get(n, {})
        p = pipeline.get(n, {"status": "offline", "detail": ""})
        try:
            age = int((now - datetime.fromisoformat(ss["started_at"])).total_seconds() / 60) if ss.get("started_at") else 0
        except (ValueError, TypeError):
            age = 0
        rl_until = rate_limited_agents.get(n, 0)
        is_rl = rl_until > time.time()
        prog = agent_progress.get(n, {})
        spec = agent_specialization.get(n, {})
        ai[n] = {
            "status": "rate_limited" if is_rl else ("unresponsive" if silent > 180 else a.get("status", "offline")),
            "pipeline": p.get("status", "offline"), "detail": p.get("detail", ""),
            "silent_sec": silent, "messages": ss.get("message_count", 0),
            "calls": ss.get("claude_calls", 0), "age_min": age,
            "rate_limited_sec": int(rl_until - time.time()) if is_rl else 0,
            "progress": prog, "expertise": spec.get("score", 0),
            "cost": calc_agent_cost(n),
        }
    # Analytics summary (real-time, no HTTP fetch needed)
    all_names = sorted(set(agent_names) | set(usage_snap.keys()))
    by_agent = {}
    for a_name in all_names:
        u = usage_snap.get(a_name, {})
        td = len([t for t in tasks_snap if t.get("assigned_to") == a_name and t.get("status") == "done"])
        tf = len([t for t in tasks_snap if t.get("assigned_to") == a_name and t.get("status") == "failed"])
        spec = agent_specialization.get(a_name, {})
        by_agent[a_name] = {
            "tokens_in": u.get("tokens_in", 0), "tokens_out": u.get("tokens_out", 0),
            "requests": u.get("requests", 0), "tasks_done": td, "tasks_failed": tf,
            "sonnet_in": u.get("sonnet_in", 0), "sonnet_out": u.get("sonnet_out", 0),
            "opus_in": u.get("opus_in", 0), "opus_out": u.get("opus_out", 0),
            "haiku_in": u.get("haiku_in", 0), "haiku_out": u.get("haiku_out", 0),
            "cost": calc_agent_cost(a_name), "expertise_score": spec.get("score", 0),
        }
    durations = []
    for entry in list(analytics_log[-50:]):
        try:
            s = datetime.fromisoformat(entry["started"])
            e = datetime.fromisoformat(entry["completed"])
            durations.append({
                "agent": entry["agent"], "task": entry["task_id"],
                "seconds": int((e - s).total_seconds()), "status": entry["status"]
            })
        except (ValueError, TypeError, KeyError):
            pass
    recent_tests = list(test_results[-20:])
    test_summary = {
        "total_passed": sum(t.get("tests_passed", 0) for t in recent_tests),
        "total_failed": sum(t.get("tests_failed", 0) for t in recent_tests),
        "lint_errors": sum(t.get("lint_errors", 0) for t in recent_tests),
    }
    total_cost = calc_total_cost()

    return {
        "workspace": WORKSPACE or "",
        "agents": ai, "agent_names": agent_names,
        "pending": {k: len(v) for k, v in msgs_snap.items() if v},
        "tasks": sorted(tasks_snap,
                        key=lambda t: (0 if t.get("status") in ("in_progress", "to_do", "code_review", "in_testing", "uat", "created", "assigned") else 1,
                                       t.get("priority", 5), -t.get("id", 0)))[:100],
        "usage": usage_snap,
        "total_tokens": sum(c.get("tokens_in", 0) + c.get("tokens_out", 0) for c in usage_snap.values()),
        "locks": locks_snap, "activity": list(activity)[-80:],
        "changes_pending": len([c for c in changes_snap if c.get("status") == "pending"]),
        "budget": {"total_spent": total_cost, "limit": BUDGET_LIMIT},
        "tests": list(test_results[-5:]),
        "inbox": list(msgs_snap.get("user", [])),
        "changes": changes_snap[-50:],
        "pattern_count": len(pattern_registry),
        "top_patterns": sorted(pattern_registry.values(), key=lambda p: p.get("score", 0), reverse=True)[:10],
        "pending_plans": dict(pending_plans),
        "task_comments": dict(task_comments),
        "task_reviews": dict(task_reviews),
        "analytics": {
            "by_agent": by_agent, "durations": durations,
            "total_tasks": len(tasks),
            "tasks_done": len([t for t in tasks_snap if t.get("status") == "done"]),
            "tasks_pending": len([t for t in tasks_snap if t.get("status") in ("to_do", "in_progress", "code_review", "in_testing", "uat", "created", "assigned")]),
            "budget": {"total_spent": total_cost, "limit": BUDGET_LIMIT,
                       "remaining": max(0, BUDGET_LIMIT - total_cost) if BUDGET_LIMIT else None},
            "tests": test_summary,
        },
    }

# ── Background Threads ──
def start_background_threads():
    threading.Thread(target=_save_timer, daemon=True).start()
    threading.Thread(target=_config_reload_timer, daemon=True).start()
    threading.Thread(target=_lock_cleanup_timer, daemon=True).start()
    threading.Thread(target=_review_timeout_timer, daemon=True).start()

def _review_timeout_timer():
    """Auto-approve code reviews pending > 15 minutes."""
    while not _shutdown_event.is_set():
        _shutdown_event.wait(60)
        if _shutdown_event.is_set():
            break
        now = datetime.now()
        with lock:
            for tid, task in dict(tasks).items():
                tid_str = str(tid)
                # New code review timeout: if task is in code_review and reviews are pending > 15 min
                if task.get("status") == "code_review":
                    reviews = task_reviews.get(tid_str, {})
                    # Check if review was dispatched (has a timestamp marker)
                    review_started = task.get("review_dispatched_at", "")
                    if review_started:
                        try:
                            started_time = datetime.fromisoformat(review_started)
                            if (now - started_time).total_seconds() > 900:  # 15 min
                                # Auto-approve missing reviews
                                reviewers = ["reviewer-logic", "reviewer-style", "reviewer-arch"]
                                for r in reviewers:
                                    if r not in reviews:
                                        reviews[r] = {"verdict": "approve", "comments": [],
                                                      "timestamp": now.isoformat(), "auto": True}
                                task_reviews[tid_str] = reviews
                                # Check if now all approved → advance to in_testing
                                if all(reviews.get(r, {}).get("verdict") == "approve" for r in reviewers):
                                    task["status"] = "in_testing"
                                    task.pop("review_dispatched_at", None)
                                    # Mark remaining review subtasks as done (auto-approved)
                                    for sub_id in task.get("_review_subtask_ids", []):
                                        sub = tasks.get(sub_id)
                                        if sub and sub.get("status") not in ("done", "failed", "cancelled"):
                                            sub["status"] = "done"
                                            sub["completed_at"] = now.isoformat()
                                    add_activity("system", task.get("assigned_to", "?"), "review_auto_approved",
                                                 f"Code review #{tid} auto-approved (15 min timeout)")
                                    # Dispatch QA so task doesn't get stuck in in_testing
                                    from hub.routers.tasks import _dispatch_qa
                                    _dispatch_qa(tid)
                                bump_version()
                        except (ValueError, TypeError):
                            pass
                # Legacy peer review timeout
                elif task.get("review_status") == "pending_review":
                    completed_at = task.get("completed_at", "")
                    if completed_at:
                        try:
                            done_time = datetime.fromisoformat(completed_at)
                            if (now - done_time).total_seconds() > 600:
                                tasks[tid]["review_status"] = "auto_approved"
                                add_activity("system", task.get("reviewer", "?"), "review_auto_approved",
                                             f"Review #{tid} auto-approved (10 min timeout)")
                                bump_version()
                        except (ValueError, TypeError):
                            pass

            # Plan-pending timeout: auto-dismiss plans pending > 30 minutes
            for pid, plan in dict(pending_plans).items():
                if plan.get("status") != "pending":
                    continue
                created = plan.get("created", "")
                if not created:
                    continue
                try:
                    created_time = datetime.fromisoformat(created)
                    if (now - created_time).total_seconds() > 1800:  # 30 min
                        pending_plans[pid]["status"] = "dismissed"
                        parent_tid = plan.get("task_id", "")
                        if parent_tid and str(parent_tid).isdigit():
                            ptid = int(parent_tid)
                            if ptid in tasks and tasks[ptid].get("status") == "in_progress":
                                tasks[ptid]["status"] = "to_do"
                                tasks[ptid].pop("assigned_to", None)
                        add_activity("system", plan.get("created_by", "?"), "plan_timeout",
                                     f"Plan #{pid} auto-dismissed (30 min timeout)")
                        bump_version()
                except (ValueError, TypeError):
                    pass

def _lock_cleanup_timer():
    while not _shutdown_event.is_set():
        _shutdown_event.wait(120)
        if _shutdown_event.is_set():
            break
        now = datetime.now()
        with lock:
            for path in list(file_locks.keys()):
                agent_name = file_locks[path].get("agent", "")
                a = agents.get(agent_name, {})
                try:
                    last = datetime.fromisoformat(a.get("last_seen", "2000-01-01"))
                    if (now - last).total_seconds() > 300:
                        del file_locks[path]
                        add_activity("system", agent_name, "lock_cleanup", f"Stale lock removed: {path}")
                except (ValueError, TypeError):
                    del file_locks[path]

def shutdown_save():
    """Final state save during shutdown."""
    logger.info("Shutdown: saving final state...")
    _do_save()
    logger.info("Shutdown: state saved.")
