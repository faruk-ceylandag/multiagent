"""agents/mcp_manager.py — MCP server setup, reload, availability, and file watching."""
import os
import json
import fcntl
import subprocess
import threading
import time
from .log_utils import log
from .credentials import load_credentials

# Track file modification times for change detection
_mcp_file_mtimes = {}
_creds_mtime = 0

# E3 fix: persistent debounce timestamp at module level (survives thread restarts)
_last_reload_time = 0

# RC-7 fix: lock to prevent concurrent reload from watcher thread and main thread
_reload_lock = threading.Lock()

# E5 fix: flag indicating credentials have changed and MCP servers need re-initialization
# Set by the file watcher when credentials.env changes; checked at task start.
_mcp_reinit_needed = False

# E2 fix: path for file-level lock on ~/.claude.json
_CLAUDE_JSON_LOCK = os.path.expanduser("~/.claude/.claude.json.lock")


def _resolve_mcp_env(spec, creds):
    """Resolve environment variables for an MCP server, handling aliases and derivations."""
    env = {}
    aliases = spec.get("env_aliases", {})
    for var in spec.get("required_env", []):
        val = creds.get(var, "") or os.environ.get(var, "")
        # Try alias
        if not val and var in aliases:
            val = creds.get(aliases[var], "") or os.environ.get(aliases[var], "")
        # Fallback: ATLASSIAN_URL ← JIRA_BASE_URL (legacy credential name)
        if not val and var == "ATLASSIAN_URL":
            val = creds.get("JIRA_BASE_URL", "") or os.environ.get("JIRA_BASE_URL", "")
        # Fallback: GITHUB_PERSONAL_ACCESS_TOKEN ← GITHUB_TOKEN (legacy)
        if not val and var == "GITHUB_PERSONAL_ACCESS_TOKEN":
            val = creds.get("GITHUB_TOKEN", "") or os.environ.get("GITHUB_TOKEN", "")
        if val:
            env[var] = val
    return env


def _clean_stale_project_mcps(ctx, desired_servers):
    """Remove stale project-level MCP entries from ALL projects in ~/.claude.json.
    Project-level entries shadow user-level ones, so ANY project with a stale entry
    (e.g. atlassian registered as stdio instead of sse) will break MCP tool discovery.
    Directly edits JSON to preserve OAuth tokens (unlike 'claude mcp remove').
    Uses file locking (E2 fix) to prevent concurrent writes from multiple agents."""
    claude_json = os.path.expanduser("~/.claude.json")
    if not os.path.exists(claude_json):
        return

    # E2 fix: acquire exclusive file lock for read-modify-write
    os.makedirs(os.path.dirname(_CLAUDE_JSON_LOCK), exist_ok=True)
    try:
        lock_fh = open(_CLAUDE_JSON_LOCK, "w")
    except OSError:
        return
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        try:
            with open(claude_json) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return

        projects = data.get("projects", {})
        if not projects:
            return

        modified = False
        cleaned_paths = []

        # Clean ALL project entries — any stale entry in any project can shadow user-level
        for path_key, entry in projects.items():
            project_mcps = entry.get("mcpServers", {})
            if not project_mcps:
                continue

            for name in list(desired_servers.keys()):
                if name not in project_mcps:
                    continue
                proj_srv = project_mcps[name]
                desired = desired_servers[name]
                proj_type = proj_srv.get("type", "stdio")
                desired_type = desired.get("type", "stdio")
                proj_url = proj_srv.get("url", "")
                desired_url = desired.get("url", "")
                proj_cmd = proj_srv.get("command", "")
                desired_cmd = desired.get("command", "")
                stale = False
                if proj_type != desired_type:
                    stale = True
                elif desired_url and proj_url != desired_url:
                    stale = True
                elif desired_cmd and proj_cmd != desired_cmd:
                    stale = True
                if stale:
                    del project_mcps[name]
                    modified = True
                    cleaned_paths.append(f"{name}@{os.path.basename(path_key)}")

        if modified:
            try:
                with open(claude_json, "w") as f:
                    json.dump(data, f, indent=2)
                log(ctx, f"🧹 Cleaned {len(cleaned_paths)} stale project MCPs: {', '.join(cleaned_paths[:5])}")
            except OSError as e:
                log(ctx, f"⚠ Failed to write cleaned ~/.claude.json: {e}")
    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()


def _build_mcp_add_cmd(name, spec, env_vars):
    """Build 'claude mcp add' command with -e flags for env vars.
    Uses --scope user so MCP servers + OAuth tokens are shared across all agent sessions."""
    if spec.get("type") in ("http", "sse") and spec.get("url"):
        # Remote MCP server (http=Streamable HTTP, sse=Server-Sent Events)
        transport = spec["type"]
        return ["claude", "mcp", "add", "--scope", "user", "--transport", transport, name, spec["url"]]
    cmd_parts = ["claude", "mcp", "add", "--scope", "user", name]
    # Add -e flags for each env var so Claude passes them to the MCP process
    for k, v in env_vars.items():
        cmd_parts.extend(["-e", f"{k}={v}"])
    # Add -- separator and the actual command
    cmd_parts.append("--")
    cmd_parts.append(spec.get("command", "npx"))
    cmd_parts.extend(spec.get("args", []))
    return cmd_parts


def _get_registered_mcps(cwd):
    """Read already-registered MCP servers from .claude.json (local config).
    Returns dict of {name: server_config} for comparison.
    Uses shared file lock on ~/.claude.json (E2 fix) to avoid reading partial writes."""
    registered = {}
    user_claude_json = os.path.expanduser("~/.claude.json")
    # Check local (agent CWD), user (~), and project configs
    paths = [os.path.join(cwd, ".claude.json"), user_claude_json]
    for cj in paths:
        if os.path.exists(cj):
            try:
                # E2 fix: use shared lock when reading ~/.claude.json
                if cj == user_claude_json:
                    os.makedirs(os.path.dirname(_CLAUDE_JSON_LOCK), exist_ok=True)
                    with open(_CLAUDE_JSON_LOCK, "w") as lock_fh:
                        fcntl.flock(lock_fh, fcntl.LOCK_SH)
                        try:
                            with open(cj) as f:
                                data = json.load(f)
                        finally:
                            fcntl.flock(lock_fh, fcntl.LOCK_UN)
                else:
                    with open(cj) as f:
                        data = json.load(f)
                for name, srv in data.get("mcpServers", {}).items():
                    if name not in registered:
                        registered[name] = srv
            except (OSError, json.JSONDecodeError):
                pass
    return registered


def setup_mcp(ctx, extra_names=None):
    """Register MCP servers at boot with credentials.
    Also installs any extra_names from the registry that aren't in ctx.MCP_SERVERS."""
    creds = load_credentials(ctx)
    try:
        from ecosystem.mcp.setup_mcp import MCP_SERVERS as REGISTRY
    except ImportError:
        REGISTRY = {}

    # Merge ctx.MCP_SERVERS + any extra names from the registry
    all_servers = dict(ctx.MCP_SERVERS) if ctx.MCP_SERVERS else {}
    if extra_names:
        for name in extra_names:
            if name not in all_servers and name in REGISTRY:
                all_servers[name] = REGISTRY[name]

    if not all_servers:
        return

    # Check what's already registered to avoid "already exists" spam
    already = _get_registered_mcps(ctx.AGENT_CWD)

    # Clean stale project-level entries that shadow user-level registrations
    _clean_stale_project_mcps(ctx, all_servers)

    added, skipped = 0, 0

    for name, cfg in all_servers.items():
        # If config is empty/minimal, try to fill from registry
        if not cfg.get("command") and not cfg.get("url") and name in REGISTRY:
            cfg = REGISTRY[name]
        # Skip servers with neither command (stdio) nor url (http)
        if not cfg.get("command") and not cfg.get("url"):
            continue

        # Check if registered but stale (type or URL changed)
        if name in already:
            old = already[name]
            new_type = cfg.get("type", "stdio")
            old_type = old.get("type", "stdio")
            new_url = cfg.get("url", "")
            old_url = old.get("url", "")
            if new_type != old_type or (new_url and new_url != old_url):
                # Config changed — remove old, re-add
                log(ctx, f"🔄 MCP {name}: {old_type}→{new_type}" + (f" url:{old_url}→{new_url}" if new_url != old_url else ""))
                try:
                    subprocess.run(["claude", "mcp", "remove", "--scope", "user", name],
                                   cwd=ctx.AGENT_CWD, capture_output=True, text=True, timeout=10)
                except Exception:
                    pass
            else:
                skipped += 1
                continue

        # Get spec from registry for alias/derivation support
        spec = REGISTRY.get(name, cfg)
        env_vars = _resolve_mcp_env(spec, creds)
        # Also include any static env from config
        static_env = cfg.get("env", {})
        env_vars.update(static_env)

        try:
            full = _build_mcp_add_cmd(name, cfg, env_vars)
            run_env = os.environ.copy()
            run_env.update(creds)
            run_env.update(env_vars)
            r = subprocess.run(full, cwd=ctx.AGENT_CWD, capture_output=True, text=True,
                              timeout=15, env=run_env)
            if r.returncode == 0:
                log(ctx, f"🔗 MCP: {name}" + (f" ({len(env_vars)} env)" if env_vars else ""))
                added += 1
            elif "already exists" in (r.stderr or ""):
                skipped += 1
            else:
                log(ctx, f"⚠ MCP {name}: {r.stderr[:80]}")
        except Exception as e:
            log(ctx, f"MCP err {name}: {e}")

    total = added + skipped
    if total:
        log(ctx, f"🔗 MCP: {total} servers ({added} new, {skipped} existing)")


def get_validated_tools(ctx):
    """Return list of successfully registered MCP tool names."""
    available = get_available_mcp(ctx)
    if not available:
        log(ctx, "⚠ No MCP tools available")
    return available


def ensure_mcp(ctx, needed_names):
    """Auto-install MCP servers that are needed but not yet registered.
    Called before task execution when URL-based MCP detection triggers."""
    if not needed_names:
        return
    available = get_available_mcp(ctx)
    creds = load_credentials(ctx)
    try:
        from ecosystem.mcp.setup_mcp import MCP_SERVERS
    except ImportError:
        return
    # For OAuth servers pending auth, notify hub instead of silently skipping
    auth_cache_path = os.path.expanduser("~/.claude/mcp-needs-auth-cache.json")
    auth_cache = {}
    if os.path.exists(auth_cache_path):
        try:
            with open(auth_cache_path) as f:
                auth_cache = json.load(f)
        except (OSError, json.JSONDecodeError):
            pass

    installed_any = False
    for name in needed_names:
        if name in available:
            continue  # Already installed
        spec = MCP_SERVERS.get(name)
        if not spec:
            continue
        if not spec.get("command") and not spec.get("url"):
            continue

        # Check if this is an OAuth server stuck in pending auth
        if spec.get("type") in ("http", "sse") and name in auth_cache:
            log(ctx, f"\u26a0 MCP {name}: OAuth pending — needs browser authentication")
            try:
                from .hub_client import hub_post
                hub_post(ctx, "/services/oauth_needed", {"mcp_name": name, "agent": ctx.AGENT_NAME})
            except Exception:
                pass
            continue

        # Resolve env vars with aliases and derivations
        env_vars = _resolve_mcp_env(spec, creds)
        missing_env = [v for v in spec.get("required_env", []) if v not in env_vars]
        if missing_env:
            log(ctx, f"⚠ MCP {name}: skipped, missing {', '.join(missing_env)}")
            continue

        log(ctx, f"📦 Auto-installing MCP: {name}...")
        try:
            full = _build_mcp_add_cmd(name, spec, env_vars)
            run_env = os.environ.copy()
            run_env.update(creds)
            run_env.update(env_vars)
            r = subprocess.run(full, cwd=ctx.AGENT_CWD, capture_output=True, text=True,
                              timeout=30, env=run_env)
            if r.returncode == 0:
                log(ctx, f"✓ MCP {name} installed ({len(env_vars)} env vars)")
                installed_any = True
                # Broadcast to peers
                try:
                    from .learning import _broadcast_ecosystem_update
                    _broadcast_ecosystem_update(ctx, "new_mcp_found", {"mcp_name": name, "agent": ctx.AGENT_NAME})
                except Exception:
                    pass
            else:
                log(ctx, f"⚠ MCP {name} install: {r.stderr[:80]}")
        except Exception as e:
            log(ctx, f"⚠ MCP {name} install error: {e}")

    # Regenerate .mcp.json so credentials are in the file too
    if installed_any:
        try:
            from ecosystem.mcp.setup_mcp import write_mcp_json
            cfg_path = os.path.join(ctx.MA_DIR, "multiagent.json")
            cfg = {}
            if os.path.exists(cfg_path):
                with open(cfg_path) as f:
                    cfg = json.load(f)
            write_mcp_json(ctx.AGENT_CWD, cfg, creds)
            log(ctx, "✓ .mcp.json regenerated with credentials")
        except Exception as e:
            log(ctx, f"⚠ .mcp.json regen: {e}")


def reload_mcp(ctx):
    """Reload MCP servers (after new credentials saved).
    Only regenerates .mcp.json — no need to re-run 'claude mcp add' (causes 'already exists' spam).
    RC-7 fix: uses _reload_lock to prevent race between file watcher thread and main thread."""
    with _reload_lock:
        _reload_mcp_inner(ctx)


def _reload_mcp_inner(ctx):
    """Inner reload logic, must be called under _reload_lock."""
    creds = load_credentials(ctx)
    try:
        from ecosystem.mcp.setup_mcp import write_mcp_json
        cfg_path = os.path.join(ctx.MA_DIR, "multiagent.json")
        cfg = {}
        if os.path.exists(cfg_path):
            with open(cfg_path) as f:
                cfg = json.load(f)
        write_mcp_json(ctx.AGENT_CWD, cfg, creds)
        log(ctx, "🔄 MCP config refreshed with new credentials")
    except Exception as e:
        log(ctx, f"MCP json regen: {e}")
    # Also inject credentials into .claude/settings.json for any MCP servers registered there
    sf = os.path.join(ctx.AGENT_CWD, ".claude", "settings.json")
    try:
        with open(sf) as f:
            settings = json.load(f)
        changed = False
        for name, srv in settings.get("mcpServers", {}).items():
            if "env" not in srv:
                srv["env"] = {}
            # Try to resolve env from registry spec
            try:
                from ecosystem.mcp.setup_mcp import MCP_SERVERS as REG
                spec = REG.get(name)
                if spec:
                    resolved = _resolve_mcp_env(spec, creds)
                    if resolved:
                        srv["env"].update(resolved)
                        changed = True
                        continue
            except ImportError:
                pass
            # Fallback: match by name prefix
            name_upper = name.upper().replace("-", "_")
            for k, v in creds.items():
                k_upper = k.upper()
                cred_prefix = k_upper.split("_")[0]
                if (name_upper in k_upper or cred_prefix in name_upper or
                    k_upper.startswith(name_upper) or name_upper.startswith(cred_prefix)):
                    srv["env"][k] = v
                    changed = True
        if changed:
            with open(sf, "w") as f:
                json.dump(settings, f, indent=2)
    except (OSError, json.JSONDecodeError, KeyError):
        pass


def get_available_mcp(ctx):
    """List MCP servers available to this agent."""
    seen = set()
    servers = []
    def _add(name):
        if name not in seen:
            seen.add(name)
            servers.append(name)
    for name in ctx.MCP_SERVERS:
        _add(name)
    # Check agent's settings.json
    sf = os.path.join(ctx.AGENT_CWD, ".claude", "settings.json")
    try:
        with open(sf) as f:
            s = json.load(f)
        for name in s.get("mcpServers", {}):
            _add(name)
    except (OSError, json.JSONDecodeError):
        pass
    # Check .claude.json (claude mcp add writes here) — include user-level
    for cj_dir in [ctx.AGENT_CWD, ctx.WORKSPACE, os.path.expanduser("~")]:
        cj = os.path.join(cj_dir, ".claude.json")
        if os.path.exists(cj):
            try:
                with open(cj) as f:
                    cjd = json.load(f)
                for name in cjd.get("mcpServers", {}):
                    _add(name)
            except (OSError, json.JSONDecodeError):
                pass
    # Check .mcp.json files
    for search_dir in [ctx.AGENT_CWD, ctx.WORKSPACE]:
        for pattern in [".mcp.json", ".mcp/config.json", "mcp.json"]:
            mf = os.path.join(search_dir, pattern)
            if os.path.exists(mf):
                try:
                    with open(mf) as f:
                        mc = json.load(f)
                    for name in mc.get("mcpServers", mc.get("servers", {})):
                        _add(name)
                except (OSError, json.JSONDecodeError):
                    pass
    return servers


def _watch_mcp_files(ctx):
    """Background thread: watch .mcp.json and credentials.env for changes, auto-reload."""
    global _mcp_file_mtimes, _creds_mtime

    # Files to watch
    watch_files = []
    for pattern in [".mcp.json", ".mcp/config.json", "mcp.json"]:
        path = os.path.join(ctx.WORKSPACE, pattern)
        watch_files.append(path)
    creds_path = os.path.join(ctx.MA_DIR, "credentials.env")

    # Initialize mtimes
    for f in watch_files:
        try:
            _mcp_file_mtimes[f] = os.path.getmtime(f) if os.path.exists(f) else 0
        except OSError:
            _mcp_file_mtimes[f] = 0
    try:
        _creds_mtime = os.path.getmtime(creds_path) if os.path.exists(creds_path) else 0
    except OSError:
        pass

    # E3 fix: use module-level _last_reload_time instead of local variable
    # so debounce state persists across thread restarts and is shared with main thread
    global _last_reload_time

    while True:
        time.sleep(15)
        try:
            now = time.time()
            need_reload = False

            # Check .mcp.json files
            for f in watch_files:
                try:
                    current_mtime = os.path.getmtime(f) if os.path.exists(f) else 0
                except OSError:
                    current_mtime = 0
                if current_mtime > _mcp_file_mtimes.get(f, 0):
                    _mcp_file_mtimes[f] = current_mtime
                    need_reload = True
                    break

            # Check credentials.env
            # E5 fix: when credentials change, also set _mcp_reinit_needed so
            # MCP servers are fully re-initialized at the next task start.
            global _mcp_reinit_needed
            try:
                current_creds_mtime = os.path.getmtime(creds_path) if os.path.exists(creds_path) else 0
            except OSError:
                current_creds_mtime = 0
            if current_creds_mtime > _creds_mtime:
                _creds_mtime = current_creds_mtime
                need_reload = True
                _mcp_reinit_needed = True

            # Debounce: only reload if >10s since last reload
            if need_reload and (now - _last_reload_time) > 10:
                reload_mcp(ctx)
                _last_reload_time = now
        except Exception as e:
            log(ctx, f"\u26a0 MCP watcher: {e}")
            time.sleep(30)


def _mcp_fingerprint(srv):
    """Return a dedup key for an MCP server: URL for http/sse, (command, args) for stdio."""
    if srv.get("type") in ("http", "sse") and srv.get("url"):
        return (srv["type"], srv["url"])
    cmd = srv.get("command", "")
    args = tuple(srv.get("args", []))
    if cmd:
        return ("stdio", cmd, args)
    return None


def adopt_project_mcp(ctx, project_dir):
    """Merge project-level MCP configs into agent's MCP setup.
    Deduplicates by URL/command+args to avoid duplicate servers."""
    project_mcp = os.path.join(project_dir, ".mcp.json")
    if not os.path.exists(project_mcp):
        return
    try:
        with open(project_mcp) as f:
            proj_cfg = json.load(f)
        agent_mcp = os.path.join(ctx.AGENT_CWD, ".mcp.json")
        agent_cfg = {}
        if os.path.exists(agent_mcp):
            with open(agent_mcp) as f:
                agent_cfg = json.load(f)
        merged = agent_cfg.get("mcpServers", {})
        # Build fingerprint set of existing servers for dedup
        existing_fps = set()
        for srv in merged.values():
            fp = _mcp_fingerprint(srv)
            if fp:
                existing_fps.add(fp)
        for name, srv in proj_cfg.get("mcpServers", {}).items():
            if name in merged:
                continue  # Name already exists
            fp = _mcp_fingerprint(srv)
            if fp and fp in existing_fps:
                log(ctx, f"⏭ MCP dedup: {name} (same URL/command as existing server)")
                continue
            merged[name] = srv
            if fp:
                existing_fps.add(fp)
            log(ctx, f"🔌 Adopted project MCP: {name}")
        agent_cfg["mcpServers"] = merged
        with open(agent_mcp, "w") as f:
            json.dump(agent_cfg, f, indent=2)
    except Exception as e:
        log(ctx, f"MCP adopt error: {e}")


def check_mcp_health(ctx, needed_names, timeout=3):
    """Quick connectivity check for MCP servers. Returns set of healthy server names.
    E1 fix: for stdio servers, also checks if the process is actually running
    (not just whether the binary exists on PATH).
    E4 fix: after verifying binary exists, performs a basic liveness probe by
    spawning the server briefly and checking it responds on stdio within 2s.
    If liveness fails, attempts a restart."""
    if not needed_names:
        return set()
    healthy = set()
    try:
        from ecosystem.mcp.setup_mcp import MCP_SERVERS as REGISTRY
    except ImportError:
        REGISTRY = {}
    for name in needed_names:
        spec = REGISTRY.get(name, {})
        if spec.get("type") in ("http", "sse") and spec.get("url"):
            try:
                from urllib.request import urlopen, HTTPError
                try:
                    urlopen(spec["url"], timeout=timeout)
                except HTTPError as he:
                    # 401/403 = server reachable, auth handled by Claude OAuth
                    if he.code not in (401, 403):
                        raise
                healthy.add(name)
            except Exception:
                log(ctx, f"⚠ MCP {name} unreachable ({spec.get('url', '')[:60]})")
        elif spec.get("command"):
            import shutil
            cmd_path = shutil.which(spec["command"])
            if not cmd_path:
                log(ctx, f"⚠ MCP {name}: command '{spec['command']}' not found")
                continue

            # E1 fix: check if the stdio MCP process is actually running
            alive = _check_stdio_mcp_alive(name, spec)

            # E4 fix: liveness probe — try to spawn the server and verify it
            # responds on stdio (JSON-RPC initialize handshake) within 2s.
            if alive or not alive:
                liveness_ok = _probe_stdio_liveness(name, spec, timeout=2)
                if liveness_ok:
                    healthy.add(name)
                elif alive:
                    # Process exists but doesn't respond — attempt restart
                    log(ctx, f"⚠ MCP {name}: process alive but unresponsive, attempting restart")
                    _restart_stdio_mcp(ctx, name, spec)
                    # Re-probe after restart
                    if _probe_stdio_liveness(name, spec, timeout=2):
                        healthy.add(name)
                        log(ctx, f"✓ MCP {name}: restarted and responding")
                    else:
                        log(ctx, f"⚠ MCP {name}: restart failed, marking unhealthy")
                else:
                    # Binary exists but no running process and liveness failed —
                    # still mark healthy since Claude CLI will start it on demand
                    healthy.add(name)
                    log(ctx, f"ℹ MCP {name}: binary found, will start on demand")
        else:
            # No spec or unknown type — assume healthy (stdio servers are local)
            healthy.add(name)
    return healthy


def _check_stdio_mcp_alive(name, spec):
    """Check if a stdio-based MCP server process is actually running.
    Searches for processes matching the MCP command and checks if PID is alive.
    Returns True if a matching process is found and alive, False otherwise."""
    command = spec.get("command", "")
    args = spec.get("args", [])
    if not command:
        return False

    try:
        # Use pgrep to find processes matching the MCP command
        # Build a search pattern from the command name
        search_term = command
        # For npx-based servers, search by the package name in args
        if command in ("npx", "node") and args:
            search_term = args[0] if args else command

        result = subprocess.run(
            ["pgrep", "-f", search_term],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            # Found matching PIDs — verify at least one is alive
            for pid_str in result.stdout.strip().split("\n"):
                try:
                    pid = int(pid_str.strip())
                    os.kill(pid, 0)  # Signal 0 = check if process exists
                    return True
                except (ValueError, ProcessLookupError, PermissionError):
                    continue
    except (subprocess.TimeoutExpired, OSError):
        pass

    return False


def _probe_stdio_liveness(name, spec, timeout=2):
    """E4 fix: probe a stdio MCP server by spawning it briefly and sending a
    JSON-RPC initialize request.  Returns True if the server responds within
    *timeout* seconds, False otherwise.  The spawned process is always killed
    afterwards — this is a one-shot probe, not a long-lived connection."""
    command = spec.get("command", "")
    args = spec.get("args", [])
    if not command:
        return False

    import shutil
    cmd_path = shutil.which(command)
    if not cmd_path:
        return False

    try:
        full_cmd = [cmd_path] + list(args)
        proc = subprocess.Popen(
            full_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # Send a minimal JSON-RPC initialize request (MCP protocol)
        init_request = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05",
                       "capabilities": {},
                       "clientInfo": {"name": "health-check", "version": "0.1"}}
        }) + "\n"

        try:
            proc.stdin.write(init_request.encode("utf-8"))
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            proc.kill()
            return False

        # Wait for a response line within the timeout
        import selectors
        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ)
        ready = sel.select(timeout=timeout)
        sel.close()

        if ready:
            line = proc.stdout.readline()
            if line and b"jsonrpc" in line:
                proc.kill()
                proc.wait(timeout=1)
                return True

        proc.kill()
        proc.wait(timeout=1)
        return False
    except (OSError, subprocess.SubprocessError):
        return False


def _restart_stdio_mcp(ctx, name, spec):
    """E4 fix: attempt to restart a stdio MCP server by killing existing processes
    and re-registering with 'claude mcp add'."""
    command = spec.get("command", "")
    args = spec.get("args", [])
    if not command:
        return

    # Kill existing processes matching this MCP server
    search_term = command
    if command in ("npx", "node") and args:
        search_term = args[0] if args else command

    try:
        result = subprocess.run(
            ["pkill", "-f", search_term],
            capture_output=True, text=True, timeout=5
        )
    except (subprocess.TimeoutExpired, OSError):
        pass

    # Re-register the MCP server
    try:
        creds = load_credentials(ctx)
        env_vars = _resolve_mcp_env(spec, creds)
        full = _build_mcp_add_cmd(name, spec, env_vars)
        # First remove, then re-add
        subprocess.run(["claude", "mcp", "remove", "--scope", "user", name],
                       cwd=ctx.AGENT_CWD, capture_output=True, text=True, timeout=10)
        run_env = os.environ.copy()
        run_env.update(creds)
        run_env.update(env_vars)
        subprocess.run(full, cwd=ctx.AGENT_CWD, capture_output=True, text=True,
                       timeout=15, env=run_env)
    except Exception as e:
        log(ctx, f"⚠ MCP {name} restart error: {e}")


def check_oauth_pending():
    """Return list of OAuth MCP servers that need browser authentication."""
    cache_path = os.path.expanduser("~/.claude/mcp-needs-auth-cache.json")
    if not os.path.exists(cache_path):
        return []
    try:
        from ecosystem.mcp.setup_mcp import MCP_SERVERS
        with open(cache_path) as f:
            cache = json.load(f)
        return [k for k in cache if k in MCP_SERVERS and MCP_SERVERS[k].get("type") in ("http", "sse")]
    except Exception:
        return []


def check_and_reinit_mcp(ctx):
    """E5 fix: if credentials changed since last MCP init, fully re-initialize
    MCP servers so they pick up new API keys/tokens.  Call at task start."""
    global _mcp_reinit_needed
    if not _mcp_reinit_needed:
        return False
    _mcp_reinit_needed = False
    log(ctx, "🔄 Credentials changed — re-initializing MCP servers")
    setup_mcp(ctx)
    return True


def start_mcp_watcher(ctx):
    """Start background thread to watch MCP config and credential changes."""
    threading.Thread(target=_watch_mcp_files, args=(ctx,), daemon=True).start()
