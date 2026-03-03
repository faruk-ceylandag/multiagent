#!/usr/bin/env python3
"""
Multi-Agent System — by Ö. Faruk Ceylandağ
Usage: python3 start.py [/path/to/workspace]
"""
import os
import sys
import time
import shutil
import subprocess
import signal
import json
import socket
import argparse
import threading

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

_parser = argparse.ArgumentParser(description="Multi-Agent System", add_help=False)
_parser.add_argument("workspace", nargs="?", default="")
_parser.add_argument("--standalone", action="store_true", help="Run with central MA_DIR (~/.multiagent/)")
_parser.add_argument("--add-workspace", metavar="PATH", help="Add workspace to running hub")
_parser.add_argument("--daemon", action="store_true", help="Run as background daemon")
_args, _extra = _parser.parse_known_args()

# Handle --add-workspace (just POST to hub and exit)
if _args.add_workspace:
    import urllib.request
    _path = os.path.realpath(_args.add_workspace)
    # Find running hub port
    _port_file = os.path.expanduser("~/.multiagent/.hub.port")
    if not os.path.exists(_port_file):
        _port_file = os.path.join(os.getcwd(), ".multiagent", ".hub.port")
    _port = 8040
    if os.path.exists(_port_file):
        with open(_port_file) as f:
            _port = int(f.read().strip())
    try:
        payload = json.dumps({"path": _path}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{_port}/workspaces/add",
                                      data=payload, headers={"Content-Type": "application/json"})
        resp = json.loads(urllib.request.urlopen(req, timeout=5).read())
        if resp.get("status") == "ok":
            print(f"  \u2713 Workspace added: {_path} (id: {resp.get('ws_id')})")
            print(f"    Projects: {', '.join(resp.get('projects', []))}")
        else:
            print(f"  \u2717 {resp.get('message', 'Failed')}")
    except Exception as e:
        print(f"  \u2717 Could not reach hub: {e}")
    sys.exit(0)

if _args.standalone:
    _central_dir = os.path.expanduser("~/.multiagent")
    os.makedirs(_central_dir, exist_ok=True)
    WORKSPACE = _central_dir
elif _args.workspace:
    WORKSPACE = os.path.realpath(_args.workspace)
else:
    WORKSPACE = os.path.realpath(os.getcwd())

from lib.config import load_config, scan_projects, detect_stack, save_default_config

cfg = load_config(WORKSPACE)
HUB_PORT = cfg.get("port", 8040)
HUB_URL = f"http://127.0.0.1:{HUB_PORT}"
AGENTS = cfg.get("agents", [])

# ── Auto-inject hidden reviewer agents (required for code review pipeline) ──
_REVIEWER_AGENTS = [
    {"name": "reviewer-logic", "role": "code reviewer — logic & correctness", "model": "sonnet", "hidden": True},
    {"name": "reviewer-style", "role": "code reviewer — style & readability", "model": "haiku", "hidden": True},
    {"name": "reviewer-arch", "role": "code reviewer — architecture & design", "model": "haiku", "hidden": True},
]
_existing_names = {a["name"] if isinstance(a, dict) else a for a in AGENTS}
for ra in _REVIEWER_AGENTS:
    if ra["name"] not in _existing_names:
        AGENTS.append(ra)
cfg["agents"] = AGENTS

AGENT_NAMES = [a["name"] if isinstance(a, dict) else a for a in AGENTS]
if _args.standalone:
    MA_DIR = os.path.expanduser("~/.multiagent")
else:
    MA_DIR = os.path.join(WORKSPACE, ".multiagent")

# ── Daemon mode (double-fork for proper daemonization) ──
if _args.daemon:
    # First fork — parent exits, child becomes orphan
    try:
        pid = os.fork()
        if pid > 0:
            print(f"  Multi-Agent daemon starting...")
            print(f"  Logs: {os.path.join(MA_DIR, 'logs', 'hub.log')}")
            sys.exit(0)
    except OSError as e:
        print(f"  Fork failed: {e}", file=sys.stderr)
        sys.exit(1)
    # First child — become session leader, detach from terminal
    os.setsid()
    # Second fork — prevent reacquiring a controlling terminal
    try:
        pid = os.fork()
        if pid > 0:
            sys.exit(0)  # First child exits
    except OSError as e:
        sys.exit(1)
    # Grandchild — the actual daemon process
    # Redirect stdout/stderr to log file
    os.makedirs(os.path.join(MA_DIR, "logs"), exist_ok=True)
    _daemon_log = os.path.join(MA_DIR, "logs", "daemon.log")
    _daemon_fd = open(_daemon_log, "a")
    os.dup2(_daemon_fd.fileno(), sys.stdout.fileno())
    os.dup2(_daemon_fd.fileno(), sys.stderr.fileno())
    _daemon_fd.close()  # FD has been duplicated; close original to avoid leak
    # Write daemon PID file
    with open(os.path.join(MA_DIR, ".daemon.pid"), "w") as f:
        f.write(str(os.getpid()))

G = "\033[0;32m"; R = "\033[0;31m"; B = "\033[0;34m"
BOLD = "\033[1m"; NC = "\033[0m"; DIM = "\033[2m"; Y = "\033[0;33m"

def log(msg): print(f"{G}  ✓ {msg}{NC}")
def warn(msg): print(f"{Y}  ⚠ {msg}{NC}")
def err(msg): print(f"{R}  ✗ {msg}{NC}"); sys.exit(1)

print(f"""
{B}    __  ___      ____  _       ___                    __
   /  |/  /_  __/ / /_(_)     /   | ____ ____  ____  / /_
  / /|_/ / / / / / __/ /_____/ /| |/ __ `/ _ \\/ __ \\/ __/
 / /  / / /_/ / / /_/ /_____/ ___ / /_/ /  __/ / / / /_
/_/  /_/\\__,_/_/\\__/_/     /_/  |_\\__, /\\___/_/ /_/\\__/
                                 /____/{NC} {DIM}by Ö. Faruk Ceylandağ{NC}
""")

# ── Checks ──
if not shutil.which("claude"): err("claude CLI not found — install: npm i -g @anthropic-ai/claude-code")
if not os.path.isdir(WORKSPACE): err(f"Not found: {WORKSPACE}")
log(f"Workspace: {WORKSPACE}")
_visible = [a["name"] if isinstance(a, dict) else a for a in AGENTS if not (isinstance(a, dict) and a.get("hidden"))]
_hidden = [a["name"] if isinstance(a, dict) else a for a in AGENTS if isinstance(a, dict) and a.get("hidden")]
log(f"Agents: {', '.join(_visible)} ({len(_visible)} visible" + (f", +{len(_hidden)} hidden reviewers)" if _hidden else ")"))
_tm = cfg.get("thinking_model", "claude-sonnet-4-5-20250929")
_cm = cfg.get("coding_model", "claude-opus-4-6")
_ts = _tm.split("-")[1] if "-" in _tm else _tm
_cs = _cm.split("-")[1] if "-" in _cm else _cm
log(f"Models: thinking={_ts} | coding={_cs}")
_av = cfg.get("auto_verify", True)
log(f"Auto-verify: {'on' if _av else 'off'}")
_bl = cfg.get("budget_limit", 0)
if _bl: log(f"Budget: ${_bl}")
_mcp = cfg.get("mcp_servers", {})
if _mcp: log(f"MCP servers: {', '.join(_mcp.keys())}")

# ── Port management ──
def port_free(port):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        s.close()
        return True
    except OSError:
        return False

def is_own_hub(port, ma_dir):
    """Check if the process on this port belongs to THIS workspace's hub."""
    try:
        pid_file = os.path.join(ma_dir, ".hub.pid")
        if os.path.exists(pid_file):
            with open(pid_file) as f:
                old_pid = int(f.read().strip())
            # Check if that PID is still alive and using this port
            r = subprocess.run(["lsof", "-ti", f":{port}"], capture_output=True, text=True)
            pids = [p.strip() for p in r.stdout.strip().split("\n") if p.strip()]
            return str(old_pid) in pids
    except (OSError, ValueError):
        pass  # expected: pid file missing or stale
    return False

def find_free_port(start_port, max_tries=20):
    """Find a free port starting from start_port."""
    for offset in range(max_tries):
        port = start_port + offset
        if port_free(port):
            return port
    return None

def _port_owner_info(port):
    """Get details about what process is using a port."""
    try:
        r = subprocess.run(["lsof", "-i", f":{port}", "-P", "-n"], capture_output=True, text=True, timeout=5)
        if r.stdout.strip():
            lines = r.stdout.strip().split("\n")
            # Return header + first few process lines for clarity
            return "\n    ".join(lines[:5])
    except (OSError, subprocess.TimeoutExpired):
        pass
    return f"(could not identify process on port {port})"

if not port_free(HUB_PORT):
    port_info = _port_owner_info(HUB_PORT)
    warn(f"Port {HUB_PORT} is in use:\n    {port_info}")
    if is_own_hub(HUB_PORT, MA_DIR):
        # Same workspace — kill old instance and reuse port
        r = subprocess.run(["lsof", "-ti", f":{HUB_PORT}"], capture_output=True, text=True)
        for pid in r.stdout.strip().split("\n"):
            if pid.strip():
                try: os.kill(int(pid.strip()), signal.SIGTERM)
                except OSError: pass  # expected: process already exited
        # Wait with timeout for port to become free
        _port_wait_start = time.time()
        _port_wait_timeout = 10  # seconds
        while not port_free(HUB_PORT):
            if time.time() - _port_wait_start > _port_wait_timeout:
                err(f"Port {HUB_PORT} still busy after {_port_wait_timeout}s — old hub won't release port.\n"
                    f"    Try: kill -9 $(lsof -ti :{HUB_PORT})")
            time.sleep(0.5)
        log(f"Old hub released port {HUB_PORT}")
    else:
        # Different workspace is using this port — find another one
        new_port = find_free_port(HUB_PORT + 1)
        if not new_port:
            err(f"Port {HUB_PORT} busy (another instance) and no free ports found in range {HUB_PORT+1}-{HUB_PORT+20}.\n"
                f"    Process on port:\n    {port_info}")
        warn(f"Port {HUB_PORT} in use by another instance → using {new_port}")
        HUB_PORT = new_port
        HUB_URL = f"http://127.0.0.1:{HUB_PORT}"

# ── Projects & Stack ──
focus = cfg.get("focus_project", "")
projects = scan_projects(WORKSPACE, focus)
if projects == ["."]:
    print(f"  {DIM}Project: {os.path.basename(WORKSPACE)} (single project){NC}")
else:
    pstr = ', '.join(projects[:15])
    if len(projects) > 15: pstr += f' (+{len(projects)-15} more)'
    print(f"  {DIM}Projects: {pstr or '(none detected)'}{NC}")

stacks = {}
for p in projects[:10]:
    st = detect_stack(os.path.join(WORKSPACE, p))
    if st["lang"]:
        stacks[p] = st
        langs = ", ".join(st["lang"][:3])
        pname = os.path.basename(WORKSPACE) if p == "." else p
        fws = ", ".join(st["fw"][:3])
        print(f"  {DIM}  └ {pname}: {langs}{' / ' + fws if fws else ''}{NC}")

# ── Init dirs ──
for d in ["logs", "sessions", "skills", "memory", "memory/learnings",
          "hooks", "templates"] + [f"sessions/{a}" for a in AGENT_NAMES]:
    os.makedirs(os.path.join(MA_DIR, d), exist_ok=True)

from lib.memory import init_memory
init_memory(MA_DIR, agents=AGENT_NAMES)

# Save stack + config
with open(os.path.join(MA_DIR, "stack.json"), "w") as f: json.dump(stacks, f, indent=2)
with open(os.path.join(MA_DIR, "config.json"), "w") as f: json.dump(cfg, f, indent=2)

save_default_config(WORKSPACE)

from lib.roles import generate_roles
generate_roles(MA_DIR, WORKSPACE, HUB_URL, stacks, AGENTS)

# ── Auto .gitignore ──
if cfg.get("auto_gitignore", True):
    for p in projects:
        gi = os.path.join(WORKSPACE, p, ".gitignore")
        if os.path.exists(gi):
            with open(gi) as f: content = f.read()
            if ".multiagent" not in content:
                with open(gi, "a") as f: f.write("\n# Multi-Agent\n.multiagent/\n")
                log(f"Added .multiagent to {p}/.gitignore")
    gi = os.path.join(WORKSPACE, ".gitignore")
    if os.path.exists(gi):
        with open(gi) as f: content = f.read()
        if ".multiagent" not in content:
            with open(gi, "a") as f: f.write("\n# Multi-Agent\n.multiagent/\nmultiagent.json\n")

# ── Ecosystem Setup (MCP, Subagents, Hooks, Commands, CLAUDE.md) ──
# Copy ecosystem package to MA_DIR so worker.py can also import it
_eco_src = os.path.join(SCRIPT_DIR, "ecosystem")
_eco_dst = os.path.join(MA_DIR, "ecosystem")
if os.path.isdir(_eco_src):
    if os.path.isdir(_eco_dst):
        shutil.rmtree(_eco_dst)
    shutil.copytree(_eco_src, _eco_dst, ignore=shutil.ignore_patterns("__pycache__"))
    log("Ecosystem package synced")

sys.path.insert(0, MA_DIR)

# ── Permissions helper (must be at module level for check_new_agents / auto_scale) ──
PERMS = {"permissions": {"allow": ["Edit", "Write", "Read", "Bash(*)"], "deny": []}}
def ensure_perms(d):
    cf = os.path.join(d, ".claude"); sf = os.path.join(cf, "settings.json")
    if os.path.exists(sf): return
    os.makedirs(cf, exist_ok=True)
    with open(sf, "w") as f: json.dump(PERMS, f, indent=2)

try:
    from ecosystem.setup_ecosystem import setup_shared_ecosystem, setup_agent_ecosystem, setup_workspace_claudemd

    # Shared ecosystem: copy subagents, commands, skills ONCE to MA_DIR/.claude/
    _shared_results = setup_shared_ecosystem(MA_DIR, WORKSPACE)
    _eco_summary = {"agents": 0, "hooks": 0, "mcp": 0}
    for r in _shared_results:
        r = r.strip()
        for key in ("subagents", "commands", "skills"):
            if r.startswith(f"{key}:"):
                try:
                    _eco_summary[key] = int(r.split(":")[1].strip().split()[0])
                except (ValueError, IndexError):
                    pass

    # Per-agent: symlink shared content + generate settings.json, .mcp.json
    for a in AGENT_NAMES:
        agent_cwd = os.path.join(MA_DIR, "sessions", a)
        results = setup_agent_ecosystem(a, agent_cwd, MA_DIR, WORKSPACE, HUB_URL, stacks)
        _eco_summary["agents"] += 1
        for r in results:
            r = r.strip()
            for key in ("hooks", "mcp"):
                if r.startswith(f"{key}:"):
                    try:
                        _eco_summary[key] = max(_eco_summary.get(key, 0), int(r.split(":")[1].strip().split()[0]))
                    except (ValueError, IndexError):
                        pass

    _parts = []
    if _eco_summary.get("subagents"): _parts.append(f"{_eco_summary['subagents']} subagents")
    if _eco_summary.get("commands"): _parts.append(f"{_eco_summary['commands']} commands")
    if _eco_summary.get("skills"): _parts.append(f"{_eco_summary['skills']} skills")
    if _eco_summary.get("hooks"): _parts.append(f"{_eco_summary['hooks']} hooks")
    if _eco_summary.get("mcp"): _parts.append(f"{_eco_summary['mcp']} MCP servers")
    log(f"Ecosystem: {', '.join(_parts)} (shared) → {_eco_summary['agents']} agents")

    # Generate CLAUDE.md for projects that don't have one
    written = setup_workspace_claudemd(WORKSPACE, MA_DIR)
    if written:
        log(f"CLAUDE.md: {', '.join(written[:5])}{'...' if len(written)>5 else ''}")
except Exception as e:
    log(f"⚠ Ecosystem setup: {e} — using basic permissions")
    for a in AGENT_NAMES: ensure_perms(os.path.join(MA_DIR, "sessions", a))

# ── Pre-boot OAuth Authentication ──
_auth_cache_path = os.path.expanduser("~/.claude/mcp-needs-auth-cache.json")
_pending_oauth = {}
if os.path.exists(_auth_cache_path):
    try:
        with open(_auth_cache_path) as f:
            _pending_oauth = json.load(f)
    except (json.JSONDecodeError, OSError):
        pass

# Filter to only our registered OAuth MCPs
try:
    from ecosystem.mcp.setup_mcp import MCP_SERVERS as _MCP_REGISTRY
    _our_oauth = {k: v for k, v in _MCP_REGISTRY.items()
                  if k in _pending_oauth and v.get("type") in ("http", "sse")}
except ImportError:
    _our_oauth = {}

if _our_oauth:
    _names = ", ".join(_our_oauth.keys())
    warn(f"OAuth pending for: {_names}")
    print(f"  {DIM}These MCP servers need browser authentication.{NC}")
    _answer = input("  Authenticate now? (Y/n): ").strip().lower()
    if _answer != "n":
        for _oname, _ospec in _our_oauth.items():
            print(f"\n  \U0001f510 Authenticating {_oname}...")
            # Clear cache entry so Claude retries OAuth
            try:
                with open(_auth_cache_path) as f:
                    _cache = json.load(f)
                _cache.pop(_oname, None)
                with open(_auth_cache_path, "w") as f:
                    json.dump(_cache, f)
            except (OSError, json.JSONDecodeError):
                pass
            # Remove + re-add to trigger OAuth with TTY access
            subprocess.run(["claude", "mcp", "remove", "--scope", "user", _oname],
                           capture_output=True, timeout=10)
            _oauth_result = subprocess.run(
                ["claude", "mcp", "add", "--transport", _ospec["type"], _oname, _ospec["url"]],
                stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr, timeout=120,
            )
            if _oauth_result.returncode == 0:
                log(f"{_oname} OAuth complete")
            else:
                warn(f"{_oname} OAuth may need manual setup")
        print()

# ── Deps ──
try:
    import fastapi
    import uvicorn
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "--break-system-packages", "-q",
                    "fastapi", "uvicorn[standard]", "sse-starlette", "websockets"], capture_output=True)
log("Dependencies OK")

# ── Copy runtime ──
# Hub package (modular)
_hub_src = os.path.join(SCRIPT_DIR, "hub")
_hub_dst = os.path.join(MA_DIR, "hub")
if os.path.isdir(_hub_src):
    if os.path.isdir(_hub_dst):
        shutil.rmtree(_hub_dst)
    shutil.copytree(_hub_src, _hub_dst, ignore=shutil.ignore_patterns("__pycache__", "dashboard"))

# Agents package (modular)
_agents_src = os.path.join(SCRIPT_DIR, "agents")
_agents_dst = os.path.join(MA_DIR, "agents")
if os.path.isdir(_agents_src):
    if os.path.isdir(_agents_dst):
        shutil.rmtree(_agents_dst)
    shutil.copytree(_agents_src, _agents_dst, ignore=shutil.ignore_patterns("__pycache__"))

# Dashboard — copy to both MA_DIR/dashboard and MA_DIR/hub/dashboard
dash_src = os.path.join(SCRIPT_DIR, "hub", "dashboard")
dash_dst = os.path.join(MA_DIR, "dashboard")
if os.path.isdir(dash_src):
    if os.path.exists(dash_dst): shutil.rmtree(dash_dst)
    shutil.copytree(dash_src, dash_dst)
    # Also copy into the hub package so hub_server.py finds it via __file__
    hub_dash_dst = os.path.join(MA_DIR, "hub", "dashboard")
    if os.path.exists(hub_dash_dst): shutil.rmtree(hub_dash_dst)
    shutil.copytree(dash_src, hub_dash_dst)

lib_dst = os.path.join(MA_DIR, "lib")
os.makedirs(lib_dst, exist_ok=True)
for f in ["config.py", "roles.py", "memory.py", "__init__.py"]:
    src = os.path.join(SCRIPT_DIR, "lib", f)
    if os.path.exists(src): shutil.copy2(src, os.path.join(lib_dst, f))
if not os.path.exists(os.path.join(lib_dst, "__init__.py")):
    open(os.path.join(lib_dst, "__init__.py"), "w").close()

# ── Cleanup (defined before any subprocess is started) ──
hub_proc = None   # forward declaration; set when hub is launched
hub_log = None    # forward declaration; set when hub log is opened
workers = {}      # forward declaration; populated when workers launch

def _get_all_child_pids(parent_pid):
    """Recursively get all descendant PIDs of a process."""
    children = []
    try:
        r = subprocess.run(["pgrep", "-P", str(parent_pid)], capture_output=True, text=True, timeout=5)
        for line in r.stdout.strip().split("\n"):
            pid = line.strip()
            if pid:
                pid = int(pid)
                children.append(pid)
                children.extend(_get_all_child_pids(pid))
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return children

_cleanup_running = False
_cleanup_lock = threading.Lock()
_force_exit_count = 0
def cleanup(sig=None, frame=None):
    global _cleanup_running, _force_exit_count
    if _cleanup_running:
        # Second Ctrl+C during shutdown → force-exit immediately
        _force_exit_count += 1
        if _force_exit_count >= 1:
            print(f"\n{DIM}Force exit.{NC}")
            os._exit(1)
        return
    with _cleanup_lock:
        if _cleanup_running:
            return
        _cleanup_running = True
    print(f"\n{DIM}Shutting down (Ctrl+C again to force)...{NC}")
    # Signal hub to save state before terminating
    try:
        import urllib.request
        urllib.request.urlopen(f"{HUB_URL}/health", timeout=2)
    except Exception:
        pass  # best-effort: hub may already be unreachable

    # Collect ALL descendant PIDs before killing (workers + their claude subprocesses)
    all_pids = set()
    for name, w in workers.items():
        try:
            pid = w["proc"].pid
            all_pids.add(pid)
            all_pids.update(_get_all_child_pids(pid))
        except (OSError, ProcessLookupError):
            pass  # expected: process already exited

    # Phase 1: SIGTERM — process groups + hub
    for name, w in workers.items():
        try:
            pgid = os.getpgid(w["proc"].pid)
            os.killpg(pgid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            try: w["proc"].terminate()
            except OSError: pass  # expected: process already exited
    if hub_proc:
        try: hub_proc.terminate()
        except OSError: pass  # expected: process already exited

    # Wait briefly for graceful shutdown (use polling loop so signals aren't swallowed)
    for _ in range(20):
        time.sleep(0.1)

    # Phase 2: SIGKILL anything still alive (workers, claude CLIs, MCP servers)
    for name, w in workers.items():
        try:
            pgid = os.getpgid(w["proc"].pid)
            os.killpg(pgid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass  # expected: process group already exited
        try: w["proc"].kill()
        except OSError: pass  # expected: process already exited
    for pid in all_pids:
        try: os.kill(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError): pass  # expected: process already exited

    # Phase 3: Kill hub
    if hub_proc:
        try: hub_proc.kill(); hub_proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired): pass  # expected: process already exited or won't respond

    # Phase 4: Sweep any orphaned processes from our session dirs
    try:
        r = subprocess.run(
            ["pgrep", "-f", f"agents.worker.*{MA_DIR}|claude.*{MA_DIR}|uvicorn.*hub"],
            capture_output=True, text=True, timeout=5)
        my_pid = os.getpid()
        for line in r.stdout.strip().split("\n"):
            pid = line.strip()
            if pid and int(pid) != my_pid:
                try: os.kill(int(pid), signal.SIGKILL)
                except (OSError, ProcessLookupError): pass  # expected: process already exited
    except (OSError, subprocess.TimeoutExpired):
        pass  # best-effort orphan sweep; pgrep may not be available

    # Close log handles
    for name, w in workers.items():
        try: w["log_fh"].close()
        except OSError: pass
    if hub_log:
        try: hub_log.close()
        except OSError: pass
    # Clean up PID/port files
    for pf in [".hub.pid", ".hub.port", ".daemon.pid", ".worker_pids"]:
        try: os.remove(os.path.join(MA_DIR, pf))
        except OSError: pass
    print(f"{G}✓ All stopped{NC}")
    sys.exit(0)

# Install signal handlers BEFORE any subprocess is started
signal.signal(signal.SIGINT, cleanup)
signal.signal(signal.SIGTERM, cleanup)

# ── Start Hub ──
hub_log_path = os.path.join(MA_DIR, "logs", "hub.log")
hub_log = open(hub_log_path, "w")
hub_env = os.environ.copy()
hub_env["MA_DIR"] = MA_DIR; hub_env["WORKSPACE"] = WORKSPACE
hub_proc = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "hub.hub_server:app",
     "--host", "127.0.0.1", "--port", str(HUB_PORT), "--log-level", "info"],
    cwd=MA_DIR, stdout=hub_log, stderr=hub_log, env=hub_env,
    start_new_session=True)

for i in range(30):
    time.sleep(0.5)
    try:
        from urllib.request import urlopen
        resp = urlopen(f"{HUB_URL}/health", timeout=2)
        health_data = json.loads(resp.read())
        if health_data.get("initialized", False):
            break
        # Hub is up but state not loaded yet — keep waiting
        if i == 29: err("Hub state never initialized — check .multiagent/logs/hub.log")
    except Exception:
        if hub_proc.poll() is not None:
            # Hub process already exited — show error
            hub_log.flush()
            try:
                with open(hub_log_path) as f: crash = f.read()[-500:]
                print(f"\n{R}  Hub crashed:{NC}")
                for line in crash.strip().split("\n")[-10:]:
                    print(f"    {line}")
            except OSError: pass
            err(f"Hub exited with code {hub_proc.returncode}")
        if i == 29: err("Hub failed — check .multiagent/logs/hub.log")

# Save PID + port files for multi-instance detection
with open(os.path.join(MA_DIR, ".hub.pid"), "w") as f: f.write(str(hub_proc.pid))
with open(os.path.join(MA_DIR, ".hub.port"), "w") as f: f.write(str(HUB_PORT))
log(f"Hub on :{HUB_PORT}")

# ── Kill orphaned workers from previous run (PID file + pgrep fallback) ──
_worker_pid_file = os.path.join(MA_DIR, ".worker_pids")
_orphans_killed = 0
# Phase 1: PID file based cleanup (reliable, no pattern matching)
if os.path.exists(_worker_pid_file):
    try:
        with open(_worker_pid_file) as f:
            _old_pids = [line.strip() for line in f if line.strip()]
        for pid_str in _old_pids:
            try:
                pid = int(pid_str)
                if pid == os.getpid():
                    continue
                # Check if process is still alive
                os.kill(pid, 0)
                # Still alive — send SIGTERM
                os.kill(pid, signal.SIGTERM)
                _orphans_killed += 1
                log(f"Killed orphaned worker (PID {pid})")
            except (OSError, ValueError):
                pass  # expected: process already exited or invalid PID
        if _orphans_killed:
            time.sleep(2)  # Give them time to exit gracefully
            # SIGKILL any that didn't respond to SIGTERM
            for pid_str in _old_pids:
                try:
                    pid = int(pid_str)
                    os.kill(pid, 0)  # check if still alive
                    os.kill(pid, signal.SIGKILL)
                    log(f"Force-killed orphaned worker (PID {pid})")
                except (OSError, ValueError):
                    pass  # expected: process already exited
    except (OSError, IOError):
        pass  # expected: PID file unreadable

# Phase 2: pgrep fallback (catches workers not in PID file)
try:
    r = subprocess.run(["pgrep", "-f", f"agents.worker.*{MA_DIR}"], capture_output=True, text=True, timeout=5)
    for pid_str in r.stdout.strip().split("\n"):
        pid_str = pid_str.strip()
        if pid_str and pid_str != str(os.getpid()):
            try:
                os.kill(int(pid_str), signal.SIGTERM)
                log(f"Killed orphaned worker (PID {pid_str})")
                _orphans_killed += 1
            except (OSError, ValueError):
                pass  # expected: orphaned process already exited
    if r.stdout.strip():
        time.sleep(1)  # Give them time to exit
except (OSError, subprocess.TimeoutExpired):
    pass  # best-effort: pgrep may not be available or timed out

# ── Launch Workers (staggered to avoid rate limits) ──
python = sys.executable
BOOT_STAGGER = cfg.get("boot_stagger", 1)  # seconds between agent boots (no API call at boot, so minimal stagger)

from lib.config import MODEL_ALIASES
_MODEL_ALIASES = cfg.get("_model_aliases", MODEL_ALIASES)

def launch_worker(agent_cfg):
    name = agent_cfg["name"] if isinstance(agent_cfg, dict) else agent_cfg
    model = agent_cfg.get("model", "") if isinstance(agent_cfg, dict) else ""
    # Resolve model aliases
    model = _MODEL_ALIASES.get(model, model)
    role_file = os.path.join(MA_DIR, f"{name}-role.md")
    log_fh = open(os.path.join(MA_DIR, "logs", f"{name}.log"), "a")
    cmd = [python, "-m", "agents.worker", name, role_file, MA_DIR, HUB_URL, WORKSPACE]
    if model: cmd.append(model)
    worker_env = os.environ.copy()
    worker_env.pop("CLAUDECODE", None)  # Prevent nested Claude Code session detection
    worker_env["MA_THINKING_MODEL"] = cfg.get("thinking_model", "claude-sonnet-4-5-20250929")
    worker_env["MA_CODING_MODEL"] = cfg.get("coding_model", "claude-opus-4-6")
    worker_env["MA_HAIKU_MODEL"] = cfg.get("haiku_model", "claude-haiku-4-5-20251001")
    worker_env["MA_AUTO_VERIFY"] = "1" if cfg.get("auto_verify", True) else "0"
    worker_env["MA_MAX_CONTEXT"] = str(cfg.get("max_context", 12000))
    _mcp_cfg = cfg.get("mcp_servers", None)
    if _mcp_cfg is None:
        # Key missing entirely → default to full registry
        try:
            from ecosystem.mcp.setup_mcp import MCP_SERVERS
            _mcp_cfg = {k: {kk: vv for kk, vv in v.items() if kk not in ("description", "install_cmd", "required_env", "env_aliases")}
                        for k, v in MCP_SERVERS.items()}
        except ImportError:
            _mcp_cfg = {}
    # Explicit empty dict {} → no MCP servers (user explicitly disabled them)
    worker_env["MA_MCP_SERVERS"] = json.dumps(_mcp_cfg)
    worker_env["MA_BUDGET_LIMIT"] = str(cfg.get("budget_per_agent", cfg.get("budget_limit", 0)))
    worker_env["MA_TASK_TIMEOUT"] = str(cfg.get("task_timeout", 1800))  # 30 min default
    # Add MA_DIR to PYTHONPATH so agents package can be imported
    worker_env["PYTHONPATH"] = MA_DIR + os.pathsep + worker_env.get("PYTHONPATH", "")
    proc = subprocess.Popen(cmd, stdout=log_fh, stderr=log_fh, stdin=subprocess.DEVNULL,
                            cwd=os.path.join(MA_DIR, "sessions", name), env=worker_env,
                            start_new_session=True)
    workers[name] = {"proc": proc, "log_fh": log_fh, "restarts": workers.get(name, {}).get("restarts", 0)}
    return proc.pid

for i, agent in enumerate(AGENTS):
    pid = launch_worker(agent)
    name = agent["name"] if isinstance(agent, dict) else agent
    log(f"{name} started (PID {pid})")
    # Stagger boots to avoid API rate limits
    if i < len(AGENTS) - 1:
        time.sleep(BOOT_STAGGER)

# ── Save worker PIDs for orphan detection on next restart ──
with open(os.path.join(MA_DIR, ".worker_pids"), "w") as f:
    for name, w in workers.items():
        f.write(f"{w['proc'].pid}\n")

# ── Browser ──
url = f"http://localhost:{HUB_PORT}"
try: import webbrowser; webbrowser.open(url)
except Exception: pass  # non-critical: browser open is best-effort

_visible_names = [a["name"] if isinstance(a, dict) else a for a in AGENTS if not (isinstance(a, dict) and a.get("hidden"))]
_hidden_names = [a["name"] if isinstance(a, dict) else a for a in AGENTS if isinstance(a, dict) and a.get("hidden")]
_agent_str = ', '.join(_visible_names)
if _hidden_names:
    _agent_str += f"  (+{len(_hidden_names)} reviewers)"
_dash_url = f"http://localhost:{HUB_PORT}"
_ws_short = os.path.basename(WORKSPACE) or WORKSPACE
_lines = [
    f"  Dashboard : {_dash_url}",
    f"  Workspace : {_ws_short}",
    f"  Agents    : {_agent_str}",
    "",
    "  Press Ctrl+C to stop everything",
]
_term_w = shutil.get_terminal_size((80, 24)).columns
_content_w = max(len(l) for l in _lines) + 4
_w = min(_content_w, _term_w - 2)  # leave room for ║ on both sides
_inner = _w  # inner padding width
print(f"\n{BOLD}╔{'═' * _inner}╗")
for l in _lines:
    if len(l) > _inner:
        l = l[:_inner - 1] + "…"
    print(f"║{l:<{_inner}}║")
print(f"╚{'═' * _inner}╝{NC}\n")

# ── Watchdog ──
MAX_RESTARTS = 5
_last_config_check = 0

def check_new_agents():
    global _last_config_check
    now = time.time()
    if now - _last_config_check < 15: return
    _last_config_check = now
    try:
        cfg_path = os.path.join(MA_DIR, "config.json")
        if not os.path.exists(cfg_path): return
        with open(cfg_path) as f: cfg_now = json.load(f)
        new_agents = []
        for a in cfg_now.get("agents", []):
            name = a["name"] if isinstance(a, dict) else a
            if name not in workers:
                new_agents.append(a)
        if not new_agents:
            return
        # Regenerate ALL role files so existing agents get updated roster
        all_agents = cfg_now.get("agents", [])
        generate_roles(MA_DIR, WORKSPACE, HUB_URL, stacks, all_agents)
        for a in new_agents:
            name = a["name"] if isinstance(a, dict) else a
            log(f"🆕 New agent detected: {name}")
            agent_cfg = a if isinstance(a, dict) else {"name": a}
            sd = os.path.join(MA_DIR, "sessions", name)
            os.makedirs(sd, exist_ok=True)
            ensure_perms(sd)
            pid = launch_worker(agent_cfg)
            log(f"{name} spawned (PID {pid})")
        # Update PID file with new workers
        try:
            with open(os.path.join(MA_DIR, ".worker_pids"), "w") as _pf:
                for _wn, _wv in workers.items():
                    _pf.write(f"{_wv['proc'].pid}\n")
        except OSError:
            pass
    except Exception as e:
        warn(f"New agent check error: {e}")

_auto_scale_check = 0

def check_auto_scale():
    global _auto_scale_check
    if not cfg.get("auto_scale", {}).get("enabled"): return
    now = time.time()
    if now - _auto_scale_check < 30: return
    _auto_scale_check = now
    try:
        from urllib.request import urlopen
        data = json.loads(urlopen(f"{HUB_URL}/autoscale/status", timeout=3).read())
        rec = data.get("recommendation", "ok")
        if rec == "scale_up":
            n = len(workers) + 1
            name = f"worker-{n}"
            agent_cfg = {"name": name, "role": "general purpose worker", "model": ""}
            # Add via hub
            import urllib.request
            payload = json.dumps({"name": name, "role": "general purpose worker"}).encode()
            req = urllib.request.Request(f"{HUB_URL}/agents/add", data=payload,
                                         headers={"Content-Type": "application/json"})
            r = json.loads(urllib.request.urlopen(req, timeout=5).read())
            if r.get("status") == "ok":
                sd = os.path.join(MA_DIR, "sessions", name)
                os.makedirs(sd, exist_ok=True)
                ensure_perms(sd)
                pid = launch_worker(agent_cfg)
                log(f"📈 Auto-scaled: {name} (PID {pid})")
                # Update PID file with new worker
                try:
                    with open(os.path.join(MA_DIR, ".worker_pids"), "w") as _pf:
                        for _wn, _wv in workers.items():
                            _pf.write(f"{_wv['proc'].pid}\n")
                except OSError:
                    pass
    except Exception as e:
        warn(f"Auto-scale check error: {e}")

while True:
    try:
        time.sleep(10)
        check_new_agents()
        check_auto_scale()
        for agent in list(AGENTS) + [{"name": n} for n in workers if n not in AGENT_NAMES]:
            name = agent["name"] if isinstance(agent, dict) else agent
            w = workers.get(name)
            if not w: continue
            if w["proc"].poll() is None:
                # Worker still running — reset crash counter if stable for 5 min
                last_crash = w.get("last_crash", 0)
                if last_crash and time.time() - last_crash > 300:
                    w["restarts"] = 0
                    w["last_crash"] = 0
                continue
            if True:  # Worker exited
                rc = w["proc"].returncode
                restarts = w.get("restarts", 0)
                if restarts >= MAX_RESTARTS:
                    warn(f"{name} died {restarts} times — giving up")
                    # Report agent as dead to hub
                    try:
                        import urllib.request
                        payload = json.dumps({"agent_name": name, "status": "offline", "detail": f"max restarts exceeded ({restarts})"}).encode()
                        req = urllib.request.Request(f"{HUB_URL}/agents/status", data=payload,
                                                     headers={"Content-Type": "application/json"})
                        urllib.request.urlopen(req, timeout=3)
                    except Exception as e:
                        warn(f"Failed to report {name} offline to hub: {e}")
                    continue
                warn(f"{name} died (exit={rc}), restart #{restarts+1}...")
                # Report crash to hub
                try:
                    import urllib.request
                    payload = json.dumps({"agent_name": name, "error": f"exit code {rc}",
                                          "exit_code": rc, "context": f"restart #{restarts+1}"}).encode()
                    req = urllib.request.Request(f"{HUB_URL}/health/crash", data=payload,
                                                 headers={"Content-Type": "application/json"})
                    urllib.request.urlopen(req, timeout=3)
                except Exception as e:
                    warn(f"Failed to report {name} crash to hub: {e}")
                backoff = min(60, BOOT_STAGGER * (2 ** min(restarts, 5)))
                warn(f"{name} restarting in {backoff}s (attempt #{restarts+1})")
                time.sleep(backoff)
                try: w["log_fh"].close()
                except OSError: pass  # expected: log handle may already be closed
                pid = launch_worker(agent)
                workers[name]["restarts"] = restarts + 1
                workers[name]["last_crash"] = time.time()
                log(f"{name} restarted (PID {pid})")
                # Update PID file so orphan detection stays current
                try:
                    with open(os.path.join(MA_DIR, ".worker_pids"), "w") as _pf:
                        for _wn, _wv in workers.items():
                            _pf.write(f"{_wv['proc'].pid}\n")
                except OSError:
                    pass
    except KeyboardInterrupt: cleanup()
