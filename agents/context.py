"""agents/context.py — Shared AgentContext dataclass for all worker modules."""
import os, sys, json, threading, re
from datetime import datetime


class AgentContext:
    """Shared mutable state passed to all worker modules."""

    def __init__(self):
        # ── Identity (from CLI args) ──
        self.AGENT_NAME = sys.argv[1]
        self.ROLE_FILE = sys.argv[2]
        self.MA_DIR = sys.argv[3]
        self.HUB_URL = sys.argv[4]
        self.WORKSPACE = sys.argv[5]
        self.MODEL_OVERRIDE = sys.argv[6] if len(sys.argv) > 6 else ""

        # ── Config from env ──
        self.MODEL_SONNET = os.environ.get("MA_THINKING_MODEL", "claude-sonnet-4-5-20250929")
        self.MODEL_OPUS = os.environ.get("MA_CODING_MODEL", "claude-opus-4-6")
        self.MODEL_HAIKU = os.environ.get("MA_HAIKU_MODEL", "claude-haiku-3-5-20241022")
        self.AUTO_VERIFY = os.environ.get("MA_AUTO_VERIFY", "1") == "1"
        self.MAX_CONTEXT = int(os.environ.get("MA_MAX_CONTEXT", "30000"))
        self.MCP_SERVERS = json.loads(os.environ.get("MA_MCP_SERVERS", "{}"))
        self.BUDGET_LIMIT = float(os.environ.get("MA_BUDGET_LIMIT", "0"))
        self.TASK_TIMEOUT = int(os.environ.get("MA_TASK_TIMEOUT", "1800"))

        # ── Paths ──
        self.CREDS_FILE = os.path.join(self.MA_DIR, "credentials.env") if self.MA_DIR else ""
        self.AGENT_CWD = os.path.join(self.MA_DIR, "sessions", self.AGENT_NAME)
        os.makedirs(self.AGENT_CWD, exist_ok=True)
        self.LOG_FILE = os.path.join(self.MA_DIR, "logs", f"{self.AGENT_NAME}.log")
        self.VERIFY_FILE = os.path.join(self.MA_DIR, "logs", f".verify_{self.AGENT_NAME}")
        self.LEARN_FILE = os.path.join(self.MA_DIR, "logs", f".learn_{self.AGENT_NAME}")
        self.SESSION_FILE = os.path.join(self.AGENT_CWD, ".session_id")
        self.TEST_RESULT_FILE = os.path.join(self.MA_DIR, "logs", f".testresult_{self.AGENT_NAME}")
        self.BOOT_TIME = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        self.MAX_LOG = 5 * 1024 * 1024

        # ── Mutable state ──
        self.idle_count = 0
        self.poll_interval = 2
        self.message_count = 0
        self.claude_calls = 0
        self.task_calls = 0
        self.session_tokens = 0
        self.current_project = None
        self.AGENT_ROLE = ""  # Set during boot (architect, frontend, backend, qa, etc.)
        self.SESSION_ID = None
        self.rate_limited_until = 0
        self.current_proc = None
        self.current_task_id = None
        self._should_stop = False
        self._locked_files = set()
        self._stop_lock = threading.Lock()
        self._log_buf = []
        self._log_lock = threading.Lock()
        self._last_prompt_hash = ""
        self._task_start_time = 0
        self._last_output_lines = []

        # ── Ecosystem tracking (derived from MCP registry) ──
        try:
            from ecosystem.mcp.setup_mcp import MCP_SERVERS
            _mcp_names = set(MCP_SERVERS.keys())
        except ImportError:
            _mcp_names = {"github", "context7", "sentry", "sequentialthinking", "figma",
                          "atlassian", "google", "playwright", "chrome-devtools", "memory"}
        self._ECO_MCP_NAMES = _mcp_names | {f"mcp__{n}" for n in _mcp_names}
        self._ECO_SUBAGENTS = {"code-reviewer", "explorer", "test-writer", "db-reader"}
        self._eco_reported = set()

        # ── Chat handler ──
        self._chat_thread = None
        self._chat_running = False
        self._task_context = ""

        # ── Review subtask tracking ──
        self._review_parent_id = None  # Parent task ID when this agent is working on a review subtask

        # ── Inbox peek (mid-task notification) ──
        self._inbox_count_at_task_start = 0
        self._mid_task_notified = False

        # ── Regex ──
        self._UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)

    def valid_sid(self, sid):
        """Claude CLI requires a valid UUID for --session-id."""
        return bool(sid and self._UUID_RE.match(sid))

    def reset_eco_tracking(self):
        """Reset per-task tracking (call at start of each task)."""
        self._eco_reported = set()
