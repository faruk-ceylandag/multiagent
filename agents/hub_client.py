"""agents/hub_client.py — Hub API communication (post, get, msg, status) with resilience."""
import json, time, os as _os
from urllib.request import Request, urlopen

# Track hub connectivity for graceful degradation
_hub_failures = 0
_hub_last_ok = 0
_HUB_MAX_FAILURES = 10  # After this many consecutive failures, log warning
_hub_last_recovery_attempt = 0


def _get_auth_headers():
    """Get auth headers if hub_token is configured."""
    # Check environment or config
    ma_dir = _os.environ.get("MA_DIR", "")
    workspace = _os.environ.get("WORKSPACE", "")
    for p in [_os.path.join(workspace, "multiagent.json"),
              _os.path.join(ma_dir, "config.json") if ma_dir else ""]:
        if p and _os.path.exists(p):
            try:
                with open(p) as f:
                    cfg = json.load(f)
                token = cfg.get("hub_token", "")
                if token:
                    return {"Authorization": f"Bearer {token}"}
            except Exception:
                pass
    return {}

_auth_headers = None

def _get_cached_auth():
    global _auth_headers
    if _auth_headers is None:
        _auth_headers = _get_auth_headers()
    return _auth_headers


def hub_post(ctx, path, data, retries=1, timeout=5):
    global _hub_failures, _hub_last_ok, _hub_last_recovery_attempt
    for attempt in range(retries + 1):
        try:
            body = json.dumps(data).encode()
            headers = {"Content-Type": "application/json"}
            headers.update(_get_cached_auth())
            req = Request(f"{ctx.HUB_URL}{path}", data=body, headers=headers)
            result = json.loads(urlopen(req, timeout=timeout).read())
            _hub_failures = 0
            _hub_last_ok = time.time()
            return result
        except Exception:
            _hub_failures += 1
            if attempt < retries:
                time.sleep(1)
            else:
                if _hub_failures == _HUB_MAX_FAILURES:
                    from .log_utils import log
                    log(ctx, f"⚠ Hub unreachable ({_hub_failures} failures), entering degraded mode")
                if _hub_failures >= _HUB_MAX_FAILURES:
                    now = time.time()
                    # Try recovery every 30s instead of full exponential backoff
                    if now - _hub_last_recovery_attempt > 30:
                        _hub_last_recovery_attempt = now
                        try:
                            req = Request(f"{ctx.HUB_URL}/health", headers=headers)
                            urlopen(req, timeout=3)
                            _hub_failures = 0
                            _hub_last_ok = now
                            from .log_utils import log
                            log(ctx, "✓ Hub recovered from degraded mode")
                            return None  # Caller will retry
                        except Exception:
                            pass
                    backoff = min(60, 5 * (2 ** min(_hub_failures - _HUB_MAX_FAILURES, 4)))
                    time.sleep(backoff)
                return None


def hub_get(ctx, path, retries=1):
    global _hub_failures, _hub_last_ok, _hub_last_recovery_attempt
    for attempt in range(retries + 1):
        try:
            req = Request(f"{ctx.HUB_URL}{path}", headers=_get_cached_auth())
            result = json.loads(urlopen(req, timeout=5).read())
            _hub_failures = 0
            _hub_last_ok = time.time()
            return result
        except Exception:
            _hub_failures += 1
            if attempt < retries:
                time.sleep(1)
            else:
                if _hub_failures >= _HUB_MAX_FAILURES:
                    now = time.time()
                    # Try recovery every 30s instead of full exponential backoff
                    if now - _hub_last_recovery_attempt > 30:
                        _hub_last_recovery_attempt = now
                        try:
                            headers = _get_cached_auth()
                            req = Request(f"{ctx.HUB_URL}/health", headers=headers)
                            urlopen(req, timeout=3)
                            _hub_failures = 0
                            _hub_last_ok = now
                            from .log_utils import log
                            log(ctx, "✓ Hub recovered from degraded mode")
                            return None  # Caller will retry
                        except Exception:
                            pass
                    backoff = min(60, 5 * (2 ** min(_hub_failures - _HUB_MAX_FAILURES, 4)))
                    time.sleep(backoff)
                return None


def hub_healthy():
    """Check if hub communication is healthy."""
    return _hub_failures < _HUB_MAX_FAILURES


def is_degraded():
    """Check if hub communication is in degraded mode."""
    return _hub_failures >= _HUB_MAX_FAILURES


def hub_msg(ctx, receiver, content, msg_type="message", extra=None):
    payload = {"sender": ctx.AGENT_NAME, "receiver": receiver,
               "content": content, "msg_type": msg_type}
    if extra and isinstance(extra, dict):
        payload.update(extra)
    hub_post(ctx, "/messages", payload)


def set_status(ctx, status, detail=""):
    hub_post(ctx, "/agents/status", {"agent_name": ctx.AGENT_NAME, "status": status,
                                     "detail": detail}, retries=2)


def update_session(ctx):
    hub_post(ctx, "/sessions/update", {
        "agent_name": ctx.AGENT_NAME, "session_id": ctx.SESSION_ID or "init",
        "message_count": ctx.message_count, "started_at": ctx.BOOT_TIME,
        "claude_calls": ctx.claude_calls}, retries=2)


def update_task_status(ctx, tid, status, detail=""):
    if not tid:
        return
    hub_post(ctx, f"/tasks/{tid}", {"status": status, "detail": detail}, retries=2)


def report_progress(ctx, event_type, detail=""):
    """Stream real-time progress to hub for dashboard display."""
    import time as _t
    elapsed = int(_t.time() - ctx._task_start_time) if ctx._task_start_time else 0
    hub_post(ctx, "/agents/progress", {
        "agent_name": ctx.AGENT_NAME, "event": event_type,
        "detail": detail[:200], "task_id": ctx.current_task_id or "",
        "task_tokens": ctx.session_tokens,
        "task_calls": ctx.task_calls,
        "elapsed": elapsed,
        "project": ctx.current_project or "",
    })


def get_agent_roster(ctx):
    """Fetch current agent list with roles, expertise, and statuses from hub."""
    try:
        profiles = hub_get(ctx, "/agents/profiles")
        if not profiles or not isinstance(profiles, dict):
            # Fallback to basic dashboard data
            d = hub_get(ctx, "/dashboard")
            if not d:
                return ""
            names = d.get("agent_names", [])
            agents = d.get("agents", {})
            lines = ["TEAM:"]
            for n in names:
                a = agents.get(n, {})
                status = a.get("pipeline", "offline")
                lines.append(f"  - {n}: {status}" + (f" ({a['detail'][:30]})" if a.get('detail') else ""))
            return "\n".join(lines)
        lines = ["TEAM:"]
        for name, p in profiles.items():
            status = p.get("status", "offline")
            role = p.get("role", "")
            expertise = p.get("expertise", 0)
            done = p.get("total_done", 0)
            parts = [f"{name}"]
            if role:
                parts.append(f"({role[:50]})")
            parts.append(f"— {status}")
            if expertise > 0:
                parts.append(f"[score:{expertise}]")
            if done > 0:
                parts.append(f"({done} tasks done)")
            if p.get("detail"):
                parts.append(f"| {p['detail'][:30]}")
            lines.append(f"  - {' '.join(parts)}")
        return "\n".join(lines)
    except Exception:
        return ""


def get_relevant_patterns(ctx, category, min_score=2, limit=5):
    """Fetch proven patterns for a category from hub."""
    params = f"?category={category}&min_score={min_score}&limit={limit}"
    return hub_get(ctx, f"/patterns{params}") or []


def get_peer_learnings(ctx, top=5):
    """Fetch recent learnings from other agents, enriched with profiles."""
    result = hub_get(ctx, f"/agents/learnings?top={top}")
    if not result or not isinstance(result, list):
        return []
    peers = [l for l in result if l.get("agent") != ctx.AGENT_NAME][:top]
    # Enrich with profiles for context
    if peers:
        profiles = hub_get(ctx, "/agents/profiles")
        if profiles and isinstance(profiles, dict):
            for l in peers:
                p = profiles.get(l.get("agent", ""), {})
                l["_role"] = p.get("role", "")
                l["_expertise"] = p.get("expertise", 0)
    return peers


def check_budget(ctx):
    """Check if we've exceeded the cost budget."""
    from .log_utils import log
    if not ctx.BUDGET_LIMIT:
        return True
    r = hub_get(ctx, f"/costs/budget/{ctx.AGENT_NAME}")
    if r and r.get("remaining", 1) <= 0:
        log(ctx, f"💰 Budget exhausted! Limit: ${ctx.BUDGET_LIMIT}")
        hub_msg(ctx, "user", f"⚠️ {ctx.AGENT_NAME} budget limit reached (${ctx.BUDGET_LIMIT})", "blocker")
        return False
    return True
