"""hub/state.py — Shared state, models, persistence, config, and utility functions."""

import os
import json
import time
import logging
import threading
import subprocess
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

lock = threading.RLock()
_shutdown_event = threading.Event()
_state_initialized = False

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
MAX_COMMENTS_PER_TASK = 100
MAX_PENDING_PLANS = 200
MAX_CACHE_ENTRIES = 500
MAX_LEARNINGS = 500
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
    model_config = {"extra": "allow"}
    agent_name: str; tokens_in: int = 0; tokens_out: int = 0; model: str = ""
    task_id: str = ""

class FileLock(BaseModel):
    file_path: str; agent_name: str; task_id: int | None = None

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
resume_signals: Dict[str, bool] = {}
agent_pids = {}  # agent_name → PID
analytics_log: List[dict] = []
MAX_ANALYTICS = 1000
rate_limited_agents: Dict[str, float] = {}
sse_clients: Dict[str, int] = {}
agent_progress: Dict[str, dict] = {}
workspace_registry: Dict[str, dict] = {}  # {ws_id: {path, name, projects, stacks, added_at, active}}
test_results: List[dict] = []
agent_specialization: Dict[str, dict] = {}
pending_oauth: Dict[str, dict] = {}  # {mcp_name: {reported_by, reported_at}} — transient, recalculated from auth cache
agent_learnings: List[dict] = []
agent_roles: Dict[str, str] = {}  # {agent_name: role_description}
file_plans: Dict[str, dict] = {}  # {agent_name: {task_id, files, timestamp}} — transient, no persistence

# ── Tool Stream (transient, no persistence) ──
tool_events: Dict[str, deque] = {}   # {agent_name: deque(maxlen=200)}
tool_counters: Dict[str, int] = {}   # {agent_name: int}

# ── Rate Pool (transient) ──
rate_pool = {"bucket_tokens": 100, "bucket_max": 100, "refill_rate": 2.0, "last_refill": time.time()}

# ── Circuit Breakers (transient) ──
circuit_breakers: Dict[str, dict] = {}  # {model_key: {state, failures, last_failure, cooldown, threshold, success_count}}

# ── Webhook Registry ──
webhook_registry: List[dict] = []  # [{url, type, events, name, active}]

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
        "auth_type": "oauth",
        "auth_url": "https://admin.atlassian.com",
        "mcp_cmd": "claude mcp add --transport sse atlassian https://mcp.atlassian.com/v1/sse",
        "credentials": [],
        "connected": True,
        "docs": "https://www.atlassian.com/platform/remote-mcp-server",
        "setup_note": "OAuth — run the CLI command below to authenticate via browser. No API tokens needed.",
    },
    {
        "id": "figma", "name": "Figma", "mcp": "figma",
        "icon": "\U0001f3a8", "color": "#F24E1E",
        "auth_type": "oauth",
        "auth_url": "https://www.figma.com/settings",
        "mcp_cmd": "claude mcp add --transport http figma https://mcp.figma.com/mcp",
        "credentials": [],
        "connected": True,
        "docs": "https://help.figma.com/hc/en-us/articles/32132956003351-Guide-to-the-Figma-MCP-Server",
        "setup_note": "OAuth — run the CLI command below to authenticate via browser. No API tokens needed.",
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
        "auth_type": "oauth",
        "auth_url": "https://sentry.io/settings/account/api/auth-tokens/",
        "mcp_cmd": "claude mcp add --transport http sentry https://mcp.sentry.dev/mcp",
        "credentials": [],
        "connected": True,
        "docs": "https://docs.sentry.io/organization/integrations/integration-platform/internal-integration/",
        "setup_note": "OAuth — run the CLI command below to authenticate via browser. No API tokens needed.",
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
                for i in range(3, 1, -1):
                    src = f"{lf}.{i-1}"
                    dst = f"{lf}.{i}"
                    if os.path.exists(src):
                        try:
                            os.replace(src, dst)
                        except OSError:
                            pass
                os.rename(lf, f"{lf}.1")
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
        # Bounded data structure cleanup — under lock to prevent race conditions
        with lock:
            # Clean comments/reviews for completed tasks
            active_tids = {str(k) for k, v in task_snapshot.items() if v.get("status") not in ("done", "failed", "cancelled")}
            for tid_str in list(task_comments.keys()):
                if tid_str not in active_tids:
                    del task_comments[tid_str]
            for tid_str in list(task_reviews.keys()):
                if tid_str not in active_tids:
                    del task_reviews[tid_str]
            # Cap pending_plans
            if len(pending_plans) > MAX_PENDING_PLANS:
                non_pending = [pid for pid, p in pending_plans.items() if p.get("status") != "pending"]
                for pid in sorted(non_pending)[:len(pending_plans) - MAX_PENDING_PLANS]:
                    del pending_plans[pid]
            # Cap cache_registry (LRU by created timestamp)
            if len(cache_registry) > MAX_CACHE_ENTRIES:
                sorted_keys = sorted(cache_registry.keys(), key=lambda k: cache_registry[k].get("created", ""))
                for k in sorted_keys[:len(cache_registry) - MAX_CACHE_ENTRIES]:
                    entry = cache_registry.pop(k, None)
                    if entry and entry.get("path"):
                        try:
                            os.remove(entry["path"])
                        except OSError:
                            pass
            # Cap learnings
            if len(agent_learnings) > MAX_LEARNINGS:
                del agent_learnings[:len(agent_learnings) - MAX_LEARNINGS]
            # Snapshot counters and mutable dicts under lock
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
                "workspace_registry": dict(workspace_registry),
                "webhook_registry": list(webhook_registry),
            }
        # Atomic write: temp file + rename
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(snapshot, f, default=str)
        # Validate tmp before replacing
        try:
            with open(tmp) as f:
                json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"Corrupt tmp state file, skipping save: {e}")
            try:
                os.remove(tmp)
            except OSError:
                pass
            return
        # Rotate backups BEFORE replace (preserve last known good state)
        _rotate_backups()
        os.replace(tmp, STATE_FILE)  # atomic on POSIX
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
    global task_comments, task_reviews, _comment_counter, workspace_registry, webhook_registry
    global _state_initialized
    if not STATE_FILE or not os.path.exists(STATE_FILE):
        _state_initialized = True
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
        _restored_from = None
        for i in range(1, 4):
            bak = f"{STATE_FILE}.bak.{i}"
            if os.path.exists(bak):
                try:
                    with open(bak) as f:
                        s = json.load(f)
                    logger.info(f"Restored from backup {bak}")
                    _restored_from = bak
                    break
                except (json.JSONDecodeError, OSError):
                    continue
        if s is None:
            logger.error("All state backups corrupt or missing, starting fresh")
            # Notify dashboard about total state loss
            messages.setdefault("user", []).append({
                "sender": "system", "receiver": "user",
                "content": "⚠️ State file and all backups were corrupt or missing. Starting with fresh state — previous tasks/data lost.",
                "msg_type": "blocker", "timestamp": datetime.now().isoformat(),
            })
            _state_initialized = True
            return
        # Notify dashboard about backup restore
        messages.setdefault("user", []).append({
            "sender": "system", "receiver": "user",
            "content": f"⚠️ State file was corrupt. Restored from backup ({os.path.basename(_restored_from)}). Some recent changes may be lost.",
            "msg_type": "warning", "timestamp": datetime.now().isoformat(),
        })
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
        workspace_registry.update(s.get("workspace_registry", {}))
        webhook_registry.extend(s.get("webhook_registry", []))
        # ── Migrate legacy task statuses ──
        for tid, task in tasks.items():
            old_status = task.get("status", "")
            if old_status in STATUS_MIGRATION:
                task["status"] = STATUS_MIGRATION[old_status]
                logger.info(f"Migrated task #{tid}: {old_status} → {task['status']}")
        logger.info(f"Restored: {len(tasks)} tasks, {len(pattern_registry)} patterns, {len(um)} inbox")
    except Exception as e:
        logger.warning(f"load: {e}")
    _state_initialized = True


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
        resume_signals.clear()
        agent_progress.clear()
        sessions.clear()
        # Clear MCP cache (stale Jira/Figma/GitHub content from old sessions)
        cache_registry.clear()
        if CACHE_DIR:
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
        # NOTE: workspace_registry is NOT cleared — workspaces persist across sessions
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
                            # Init message queues, log buffers, and counters for new agents
                            for a in ALL_AGENTS:
                                if a not in messages:
                                    messages[a] = []
                                if a not in log_buffers:
                                    log_buffers[a] = deque(maxlen=3000)
                                if a not in log_counters:
                                    log_counters[a] = 0
                                if a not in chat_queue:
                                    chat_queue[a] = []
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
def send_notification(event_type, message, extra=None):
    """Send notifications to all registered webhooks + legacy config."""
    targets = []
    # Legacy single webhook
    url = notification_config.get("url", "")
    if url:
        allowed = notification_config.get("events", ["task_done", "task_failed", "blocker", "budget_warn"])
        if event_type in allowed:
            targets.append({"url": url, "type": notification_config.get("type", "generic"), "events": allowed, "name": "legacy", "active": True})
    # Registry webhooks
    for wh in webhook_registry:
        if not wh.get("active", True):
            continue
        if wh.get("events") and event_type not in wh["events"]:
            continue
        targets.append(wh)
    for t in targets:
        try:
            import urllib.request
            ntype = t.get("type", "generic")
            payload_data = {"event": event_type, "message": message, "timestamp": datetime.now().isoformat()}
            if extra:
                payload_data.update(extra)
            if ntype == "slack":
                payload = json.dumps({"text": f"\U0001f916 Multi-Agent: {message}"}).encode()
            elif ntype == "discord":
                payload = json.dumps({"content": f"\U0001f916 Multi-Agent: {message}"}).encode()
            else:
                payload = json.dumps(payload_data).encode()
            req = urllib.request.Request(t["url"], data=payload, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5)
            add_activity("system", "webhook", "notification", f"{event_type}: {message[:80]}")
        except Exception as e:
            logger.warning(f"Notification to {t.get('name', '?')} failed: {e}")

# ── Circuit Breaker Helpers ──
def _init_circuit(key):
    if key not in circuit_breakers:
        circuit_breakers[key] = {"state": "closed", "failures": 0, "last_failure": 0, "cooldown": 120, "threshold": 5, "success_count": 0}
    return circuit_breakers[key]

def check_circuit(key):
    cb = _init_circuit(key)
    if cb["state"] == "closed":
        return True
    if cb["state"] == "open":
        if time.time() - cb["last_failure"] > cb["cooldown"]:
            cb["state"] = "half_open"
            cb["success_count"] = 0
            return True
        return False
    return True  # half_open allows through

def record_circuit_success(key):
    cb = _init_circuit(key)
    if cb["state"] == "half_open":
        cb["success_count"] += 1
        if cb["success_count"] >= 2:
            cb["state"] = "closed"
            cb["failures"] = 0
    elif cb["state"] == "closed":
        cb["failures"] = max(0, cb["failures"] - 1)
    bump_version()

def record_circuit_failure(key):
    cb = _init_circuit(key)
    cb["failures"] += 1
    cb["last_failure"] = time.time()
    if cb["failures"] >= cb["threshold"]:
        cb["state"] = "open"
    bump_version()

# ── Git Helper ──
def git_cmd(args, cwd=None):
    try:
        r = subprocess.run(["git"] + args, cwd=cwd or WORKSPACE, capture_output=True, text=True, timeout=15)
        return r.returncode == 0, r.stdout.strip()
    except Exception as e:
        logger.warning(f"Git command error {args}: {e}")
        return False, ""

def safe_project_dir(project, workspace_id=None):
    """Resolve project to absolute directory path, optionally within a specific workspace."""
    if not project:
        return None
    ws_path = WORKSPACE
    if workspace_id and workspace_id in workspace_registry:
        ws_path = workspace_registry[workspace_id].get("path", WORKSPACE)
    # Single-project workspace: "." means workspace itself
    if project == ".":
        return ws_path or None
    clean = os.path.basename(project.replace("\\", "/"))
    if not clean or clean.startswith(".") or ".." in clean:
        return None
    d = os.path.join(ws_path, clean)
    # Validate path is under the workspace
    if not os.path.realpath(d).startswith(os.path.realpath(ws_path)):
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
    # Snapshot all mutable dicts to prevent RuntimeError from concurrent modification
    all_agents_snap = list(ALL_AGENTS)
    hidden_snap = set(HIDDEN_AGENTS)
    visible_names = [a for a in all_agents_snap if a not in hidden_snap]
    hidden_names = [a for a in all_agents_snap if a in hidden_snap]
    agent_names = visible_names + hidden_names
    agents_snap = dict(agents)
    sessions_snap = dict(sessions)
    pipeline_snap = dict(pipeline)
    progress_snap = dict(agent_progress)
    spec_snap = dict(agent_specialization)
    rl_snap = dict(rate_limited_agents)
    msgs_snap = dict(messages)
    usage_snap = dict(usage_log)
    locks_snap = dict(file_locks)
    changes_snap = list(changes)
    tasks_snap_dict = dict(tasks)
    tasks_snap = [t for t in tasks_snap_dict.values() if not t.get("_is_review_subtask")]

    ai = {}
    for n in agent_names:
        a = agents_snap.get(n, {})
        try:
            last = datetime.fromisoformat(a.get("last_seen", "2000-01-01"))
            silent = int((now - last).total_seconds())
        except (ValueError, TypeError):
            silent = 999
        ss = sessions_snap.get(n, {})
        p = pipeline_snap.get(n, {"status": "offline", "detail": ""})
        try:
            age = int((now - datetime.fromisoformat(ss["started_at"])).total_seconds() / 60) if ss.get("started_at") else 0
        except (ValueError, TypeError):
            age = 0
        rl_until = rl_snap.get(n, 0)
        is_rl = rl_until > time.time()
        prog = progress_snap.get(n, {})
        spec = spec_snap.get(n, {})
        ai[n] = {
            "status": "rate_limited" if is_rl else ("unresponsive" if silent > 180 else a.get("status", "offline")),
            "pipeline": p.get("status", "offline"), "detail": p.get("detail", ""),
            "silent_sec": silent, "messages": ss.get("message_count", 0),
            "calls": ss.get("claude_calls", 0), "age_min": age,
            "rate_limited_sec": int(rl_until - time.time()) if is_rl else 0,
            "progress": prog, "expertise": spec.get("score", 0),
            "cost": calc_agent_cost(n),
        }
        if n in hidden_snap:
            ai[n]["hidden"] = True
    # Analytics summary (real-time, no HTTP fetch needed)
    all_names = sorted((set(agent_names) | set(usage_snap.keys())) - hidden_snap)
    by_agent = {}
    for a_name in all_names:
        u = usage_snap.get(a_name, {})
        td = len([t for t in tasks_snap if t.get("assigned_to") == a_name and t.get("status") == "done"])
        tf = len([t for t in tasks_snap if t.get("assigned_to") == a_name and t.get("status") == "failed"])
        spec = spec_snap.get(a_name, {})
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
        "workspaces": dict(workspace_registry),
        "analytics": {
            "by_agent": by_agent, "durations": durations,
            "total_tasks": len(tasks_snap_dict),
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
                                    task["_testing_started_at"] = now.isoformat()
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
                    # Fail stuck reviewer subtasks (in_progress > 20 min)
                    for sub_id in task.get("_review_subtask_ids", []):
                        sub = tasks.get(sub_id)
                        if sub and sub.get("status") == "in_progress":
                            sub_started = sub.get("started_at", "")
                            if sub_started:
                                try:
                                    sub_time = datetime.fromisoformat(sub_started)
                                    if (now - sub_time).total_seconds() > 1200:  # 20 min
                                        sub["status"] = "failed"
                                        sub["completed_at"] = now.isoformat()
                                        sub["error_message"] = "Reviewer subtask timeout (20 min)"
                                        logger.info(f"Reviewer subtask #{sub_id} timed out (20 min)")
                                except (ValueError, TypeError):
                                    pass
                elif task.get("status") == "in_testing":
                    started = task.get("_testing_started_at", "")
                    if not started:
                        task["_testing_started_at"] = now.isoformat()
                        continue
                    try:
                        started_time = datetime.fromisoformat(started)
                        if (now - started_time).total_seconds() > 1200:  # 20 min
                            task.pop("_testing_started_at", None)
                            if _cfg.get("auto_uat", False):
                                task["status"] = "done"
                                task["completed_at"] = now.isoformat()
                                analytics_log.append({
                                    "task_id": tid, "agent": task.get("assigned_to", ""),
                                    "status": "done", "started": task.get("started_at", ""),
                                    "completed": task["completed_at"],
                                })
                                messages.setdefault("user", []).append({
                                    "sender": "system", "receiver": "user",
                                    "content": f"Task #{tid} QA testing timed out (20 min). Auto-UAT approved → done.",
                                    "msg_type": "info", "timestamp": now.isoformat(),
                                })
                                add_activity("system", task.get("assigned_to", "?"), "auto_uat",
                                             f"Task #{tid} auto-UAT approved (QA timeout)")
                                from hub.routers.tasks import _auto_notify_dependents
                                _auto_notify_dependents(tid)
                            else:
                                task["status"] = "uat"
                                task["_uat_entered_at"] = now.isoformat()
                                messages.setdefault("user", []).append({
                                    "sender": "system", "receiver": "user",
                                    "content": f"Task #{tid} QA testing timed out (20 min). Moved to UAT for manual review.",
                                    "msg_type": "info", "timestamp": now.isoformat(),
                                })
                                add_activity("system", task.get("assigned_to", "?"), "testing_timeout",
                                             f"QA testing #{tid} timed out (20 min) → UAT")
                            bump_version()
                    except (ValueError, TypeError):
                        pass
                elif task.get("status") == "uat":
                    uat_timeout = _cfg.get("auto_uat_timeout", 0)
                    if uat_timeout > 0:
                        entered = task.get("_uat_entered_at", "")
                        if not entered:
                            task["_uat_entered_at"] = now.isoformat()
                            continue
                        try:
                            entered_time = datetime.fromisoformat(entered)
                            if (now - entered_time).total_seconds() > uat_timeout:
                                task["status"] = "done"
                                task["completed_at"] = now.isoformat()
                                task.pop("_uat_entered_at", None)
                                analytics_log.append({
                                    "task_id": tid, "agent": task.get("assigned_to", ""),
                                    "status": "done", "started": task.get("started_at", ""),
                                    "completed": task["completed_at"],
                                })
                                messages.setdefault("user", []).append({
                                    "sender": "system", "receiver": "user",
                                    "content": f"Task #{tid} auto-approved (UAT timeout: {uat_timeout}s).",
                                    "msg_type": "info", "timestamp": now.isoformat(),
                                })
                                add_activity("system", task.get("assigned_to", "?"), "uat_auto_approved",
                                             f"UAT #{tid} auto-approved (timeout {uat_timeout}s)")
                                from hub.routers.tasks import _auto_notify_dependents
                                _auto_notify_dependents(tid)
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
