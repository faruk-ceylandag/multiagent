#!/usr/bin/env python3
"""
Multi-Agent System — by Ö. Faruk Ceylandağ
Usage: python3 start.py [/path/to/workspace]
"""
import os, sys, time, shutil, subprocess, signal, json, socket

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.realpath(sys.argv[1] if len(sys.argv) > 1 else os.getcwd())
sys.path.insert(0, SCRIPT_DIR)

from lib.config import load_config, scan_projects, detect_stack, save_default_config

cfg = load_config(WORKSPACE)
HUB_PORT = cfg.get("port", 8040)
HUB_URL = f"http://127.0.0.1:{HUB_PORT}"
AGENTS = cfg.get("agents", [])

# ── Auto-inject hidden reviewer agents (required for code review pipeline) ──
_REVIEWER_AGENTS = [
    {"name": "reviewer-logic", "role": "code reviewer — logic & correctness", "model": "haiku", "hidden": True},
    {"name": "reviewer-style", "role": "code reviewer — style & readability", "model": "haiku", "hidden": True},
    {"name": "reviewer-arch", "role": "code reviewer — architecture & design", "model": "haiku", "hidden": True},
]
_existing_names = {a["name"] if isinstance(a, dict) else a for a in AGENTS}
for ra in _REVIEWER_AGENTS:
    if ra["name"] not in _existing_names:
        AGENTS.append(ra)
cfg["agents"] = AGENTS

AGENT_NAMES = [a["name"] if isinstance(a, dict) else a for a in AGENTS]
MA_DIR = os.path.join(WORKSPACE, ".multiagent")

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
        s.settimeout(1); s.connect(("127.0.0.1", port)); s.close(); return False
    except OSError: return True

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
        pass
    return False

def find_free_port(start_port, max_tries=20):
    """Find a free port starting from start_port."""
    for offset in range(max_tries):
        port = start_port + offset
        if port_free(port):
            return port
    return None

if not port_free(HUB_PORT):
    if is_own_hub(HUB_PORT, MA_DIR):
        # Same workspace — kill old instance and reuse port
        r = subprocess.run(["lsof", "-ti", f":{HUB_PORT}"], capture_output=True, text=True)
        for pid in r.stdout.strip().split("\n"):
            if pid.strip():
                try: os.kill(int(pid.strip()), signal.SIGTERM)
                except OSError: pass
        time.sleep(1)
        if not port_free(HUB_PORT): err(f"Port {HUB_PORT} busy (own hub won't stop)")
    else:
        # Different workspace is using this port — find another one
        new_port = find_free_port(HUB_PORT + 1)
        if not new_port: err(f"Port {HUB_PORT} busy (another instance) and no free ports found")
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
    from ecosystem.setup_ecosystem import setup_agent_ecosystem, setup_workspace_claudemd

    # Setup ecosystem for each agent
    _eco_summary = {"agents": 0, "subagents": 0, "commands": 0, "hooks": 0, "mcp": 0}
    for a in AGENT_NAMES:
        agent_cwd = os.path.join(MA_DIR, "sessions", a)
        results = setup_agent_ecosystem(a, agent_cwd, MA_DIR, WORKSPACE, HUB_URL, stacks)
        _eco_summary["agents"] += 1
        for r in results:
            r = r.strip()
            for key in ("subagents", "commands", "hooks", "mcp", "skills"):
                if r.startswith(f"{key}:"):
                    try:
                        _eco_summary[key] = max(_eco_summary.get(key, 0), int(r.split(":")[1].strip().split()[0]))
                    except (ValueError, IndexError):
                        pass

    _parts = []
    if _eco_summary.get("subagents"): _parts.append(f"{_eco_summary['subagents']} subagents")
    if _eco_summary.get("commands"): _parts.append(f"{_eco_summary['commands']} commands")
    if _eco_summary.get("hooks"): _parts.append(f"{_eco_summary['hooks']} hooks")
    if _eco_summary.get("mcp"): _parts.append(f"{_eco_summary['mcp']} MCP servers")
    log(f"Ecosystem: {', '.join(_parts)} → {_eco_summary['agents']} agents")

    # Generate CLAUDE.md for projects that don't have one
    written = setup_workspace_claudemd(WORKSPACE, MA_DIR)
    if written:
        log(f"CLAUDE.md: {', '.join(written[:5])}{'...' if len(written)>5 else ''}")
except Exception as e:
    log(f"⚠ Ecosystem setup: {e} — using basic permissions")
    for a in AGENT_NAMES: ensure_perms(os.path.join(MA_DIR, "sessions", a))

# ── Deps ──
try:
    import fastapi, uvicorn
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

# ── Start Hub ──
hub_log_path = os.path.join(MA_DIR, "logs", "hub.log")
hub_log = open(hub_log_path, "w")
hub_env = os.environ.copy()
hub_env["MA_DIR"] = MA_DIR; hub_env["WORKSPACE"] = WORKSPACE
hub_proc = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "hub.hub_server:app",
     "--host", "127.0.0.1", "--port", str(HUB_PORT), "--log-level", "info"],
    cwd=MA_DIR, stdout=hub_log, stderr=hub_log, env=hub_env)

for i in range(30):
    time.sleep(0.5)
    try:
        from urllib.request import urlopen
        urlopen(f"{HUB_URL}/health", timeout=2); break
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

# ── OAuth Pre-Auth (only if mcp-needs-auth-cache has pending auths) ──
_auth_cache = os.path.expanduser("~/.claude/mcp-needs-auth-cache.json")
if os.path.exists(_auth_cache):
    try:
        with open(_auth_cache) as f:
            _needs_auth = json.load(f)
        if _needs_auth:
            _names = list(_needs_auth.keys())
            log(f"OAuth pending: {', '.join(_names)}")
            for _n in _names:
                print(f"  {Y}🔐 {_n}: browser will open for OAuth...{NC}", flush=True)
                try:
                    subprocess.run(
                        ["claude", "-p", f"Call any mcp__{_n} tool. Reply OK.",
                         "--allowedTools", f"mcp__{_n}__*", "--max-turns", "2"],
                        timeout=120, stdin=None)
                except (subprocess.TimeoutExpired, Exception):
                    warn(f"  {_n}: auth not completed")
    except (json.JSONDecodeError, OSError):
        pass

# ── Launch Workers (staggered to avoid rate limits) ──
python = sys.executable
workers = {}
BOOT_STAGGER = cfg.get("boot_stagger", 1)  # seconds between agent boots (no API call at boot, so minimal stagger)

_MODEL_ALIASES = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-5-20250929",
    "opus": "claude-opus-4-6",
}

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
    worker_env["MA_THINKING_MODEL"] = cfg.get("thinking_model", "claude-sonnet-4-5-20250929")
    worker_env["MA_CODING_MODEL"] = cfg.get("coding_model", "claude-opus-4-6")
    worker_env["MA_AUTO_VERIFY"] = "1" if cfg.get("auto_verify", True) else "0"
    worker_env["MA_MAX_CONTEXT"] = str(cfg.get("max_context", 12000))
    _mcp_cfg = cfg.get("mcp_servers", {})
    if not _mcp_cfg:
        # Empty → pass full registry specs so agents can register them
        try:
            from ecosystem.mcp.setup_mcp import MCP_SERVERS
            _mcp_cfg = {k: {kk: vv for kk, vv in v.items() if kk not in ("description", "install_cmd", "required_env", "env_aliases")}
                        for k, v in MCP_SERVERS.items()}
        except ImportError:
            pass
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

# ── Browser ──
url = f"http://localhost:{HUB_PORT}"
try: import webbrowser; webbrowser.open(url)
except Exception: pass

# ── Cleanup ──
def cleanup(sig=None, frame=None):
    print(f"\n{DIM}Shutting down...{NC}")
    # Signal hub to save state before terminating
    try:
        import urllib.request
        urllib.request.urlopen(f"{HUB_URL}/health", timeout=2)  # check if hub is alive
    except Exception:
        pass
    for name, w in workers.items():
        try:
            # Kill entire process group (including MCP subprocesses)
            pgid = os.getpgid(w["proc"].pid)
            os.killpg(pgid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            try: w["proc"].terminate()
            except OSError: pass
    for name, w in workers.items():
        try: w["proc"].wait(timeout=5)
        except Exception:
            try: w["proc"].kill()
            except OSError: pass
        try: w["log_fh"].close()
        except OSError: pass
    try: hub_proc.terminate(); hub_proc.wait(timeout=5)
    except Exception:
        try: hub_proc.kill()
        except OSError: pass
    try: hub_log.close()
    except OSError: pass
    # Clean up PID/port files
    for pf in [".hub.pid", ".hub.port"]:
        try: os.remove(os.path.join(MA_DIR, pf))
        except OSError: pass
    print(f"{G}✓ All stopped{NC}")
    sys.exit(0)

signal.signal(signal.SIGINT, cleanup)
signal.signal(signal.SIGTERM, cleanup)

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
            if w["proc"].poll() is not None:
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
                    except Exception: pass
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
                except Exception: pass
                time.sleep(BOOT_STAGGER)
                try: w["log_fh"].close()
                except OSError: pass
                pid = launch_worker(agent)
                workers[name]["restarts"] = restarts + 1
                log(f"{name} restarted (PID {pid})")
    except KeyboardInterrupt: cleanup()
