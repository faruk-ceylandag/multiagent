"""Health, config, diagnostics, and crash reporting routes."""

import json, time
from datetime import datetime
from collections import deque
from fastapi import APIRouter

from hub.state import (
    lock, logger, ALL_AGENTS, WORKSPACE, _cfg, agents, pipeline,
    tasks, messages, changes, analytics_log, log_buffers, activity,
    rate_limited_agents, sse_clients, crash_log, usage_log, bump_version,
)

router = APIRouter(tags=["health"])

@router.get("/health")
def health():
    from hub.state import _last_save, calc_total_cost, get_version
    now = datetime.now()

    # Agent health summary
    total = len(ALL_AGENTS)
    active = 0
    unresponsive = 0
    for n in ALL_AGENTS:
        a = agents.get(n, {})
        p = pipeline.get(n, {})
        try:
            last = datetime.fromisoformat(a.get("last_seen", "2000-01-01"))
            silent = (now - last).total_seconds()
        except (ValueError, TypeError):
            silent = 999
        if silent > 60:
            unresponsive += 1
        elif p.get("status") not in ("offline",):
            active += 1

    # Task summary
    task_list = list(tasks.values())
    pending = len([t for t in task_list if t.get("status") in ("created", "assigned")])
    in_progress = len([t for t in task_list if t.get("status") == "in_progress"])
    completed = len([t for t in task_list if t.get("status") == "done"])

    # Determine overall status
    if unresponsive == total:
        status = "unhealthy"
    elif unresponsive > total // 2:
        status = "degraded"
    else:
        status = "healthy"

    return {
        "status": status,
        "agents": ALL_AGENTS,
        "workspace": WORKSPACE,
        "agent_summary": {"total": total, "active": active, "unresponsive": unresponsive},
        "task_summary": {"pending": pending, "in_progress": in_progress, "completed": completed},
        "total_cost": calc_total_cost(),
        "state_version": get_version(),
        "last_save": _last_save,
    }

@router.get("/health/detailed")
def health_detailed():
    """Comprehensive health check with system metrics."""
    import os
    from hub.state import calc_total_cost, get_version, STATE_FILE
    now = datetime.now()

    # Agent health
    total = len(ALL_AGENTS)
    active = sum(1 for n in ALL_AGENTS
                 if pipeline.get(n, {}).get("status") not in ("offline", None))
    unresponsive_list = []
    for n in ALL_AGENTS:
        a = agents.get(n, {})
        try:
            last = datetime.fromisoformat(a.get("last_seen", "2000-01-01"))
            if (now - last).total_seconds() > 60:
                unresponsive_list.append(n)
        except (ValueError, TypeError):
            unresponsive_list.append(n)

    # Task stats
    task_list = list(tasks.values())

    # Disk usage
    disk_mb = 0
    if STATE_FILE:
        try:
            disk_mb = round(os.path.getsize(STATE_FILE) / 1024 / 1024, 2)
        except OSError:
            pass

    # Uptime
    uptime = 0
    if activity:
        try:
            first = datetime.fromisoformat(list(activity)[0]["time"])
            uptime = int((now - first).total_seconds())
        except (ValueError, TypeError, IndexError):
            pass

    status = "healthy"
    if len(unresponsive_list) > total // 2:
        status = "degraded"
    elif len(unresponsive_list) == total and total > 0:
        status = "unhealthy"

    return {
        "status": status,
        "uptime_seconds": uptime,
        "agents": {
            "total": total,
            "active": active,
            "unresponsive": len(unresponsive_list),
            "unresponsive_names": unresponsive_list,
        },
        "tasks": {
            "total": len(task_list),
            "pending": len([t for t in task_list if t.get("status") in ("created", "assigned")]),
            "in_progress": len([t for t in task_list if t.get("status") == "in_progress"]),
            "completed": len([t for t in task_list if t.get("status") == "done"]),
            "failed": len([t for t in task_list if t.get("status") == "failed"]),
        },
        "disk_usage_mb": disk_mb,
        "total_cost": calc_total_cost(),
        "state_version": get_version(),
        "crashes_recent": len([c for c in crash_log if _crash_age_sec(c, now) < 600]),
    }

@router.get("/config")
def get_config():
    return {"agents": ALL_AGENTS, "workspace": WORKSPACE, **_cfg}

@router.put("/config")
def update_config(data: dict):
    """Update runtime config values."""
    import os
    import hub.state as _st
    allowed_keys = {"auto_uat", "auto_uat_timeout", "auto_plan_approval",
                    "auto_plan_single_step", "escalation_threshold",
                    "budget_limit", "budget_per_agent", "notifications",
                    "add_dirs"}
    updated = {}
    with lock:
        for key, value in data.items():
            if key in allowed_keys:
                _st._cfg[key] = value
                updated[key] = value
        if updated:
            bump_version()
            # Persist to multiagent.json
            cfg_path = os.path.join(WORKSPACE, "multiagent.json") if WORKSPACE else ""
            if cfg_path:
                try:
                    existing = {}
                    if os.path.exists(cfg_path):
                        with open(cfg_path) as f:
                            existing = json.load(f)
                    existing.update(updated)
                    with open(cfg_path, "w") as f:
                        json.dump(existing, f, indent=2)
                except Exception as e:
                    logger.warning(f"Config write error: {e}")
    return {"status": "ok", "updated": updated}

@router.post("/health/crash")
def report_crash(data: dict):
    agent_name = data.get("agent_name", "")
    with lock:
        crash_log.append({
            "agent": agent_name, "error": data.get("error", ""),
            "exit_code": data.get("exit_code", -1), "time": datetime.now().isoformat(),
            "context": data.get("context", "")[:500]
        })
        if len(crash_log) > 200:
            del crash_log[:100]
        from hub.state import add_activity, send_notification
        add_activity("system", agent_name, "crash", data.get("error", "")[:100])

        # Crash pattern detection
        pattern = _detect_crash_pattern(agent_name)
        if pattern:
            logger.warning(f"Crash pattern detected for {agent_name}: {pattern}")
            send_notification("blocker", f"⚠️ {agent_name}: {pattern}")
    return {"status": "ok"}


def _detect_crash_pattern(agent_name):
    """Detect recurring crash patterns for an agent."""
    agent_crashes = [c for c in crash_log if c.get("agent") == agent_name]
    if len(agent_crashes) < 3:
        return None

    # Check for rapid crashes (3+ in 5 minutes)
    recent = agent_crashes[-3:]
    try:
        times = [datetime.fromisoformat(c["time"]) for c in recent]
        span = (times[-1] - times[0]).total_seconds()
        if span < 300:
            # Check if same error repeating
            errors = [c.get("error", "")[:50] for c in recent]
            if len(set(errors)) == 1:
                return f"Repeating crash ({len(recent)}x in {int(span)}s): {errors[0]}"
            return f"Rapid crashes ({len(recent)}x in {int(span)}s) — may need investigation"
    except (ValueError, TypeError):
        pass

    # Check for rate-limit crashes
    rl_crashes = [c for c in agent_crashes[-5:] if "rate limit" in c.get("error", "").lower()]
    if len(rl_crashes) >= 3:
        return "Frequent rate-limit crashes — consider reducing model tier or adding delay"

    return None

@router.get("/health/crashes")
def get_crashes(limit: int = 20):
    return list(crash_log[-limit:])

@router.get("/health/diagnostics")
def get_diagnostics():
    # No lock — read-only diagnostic display
    now = datetime.now()
    agent_health = {}
    for n in ALL_AGENTS:
        a = agents.get(n, {})
        p = pipeline.get(n, {})
        try:
            last = datetime.fromisoformat(a.get("last_seen", "2000-01-01"))
            silent = int((now - last).total_seconds())
        except (ValueError, TypeError):
            silent = 999
        crashes = [c for c in crash_log if c.get("agent") == n]
        recent_crashes = [c for c in crashes
                          if _crash_age_sec(c, now) < 600]
        agent_health[n] = {
            "status": p.get("status", "offline"), "silent_sec": silent,
            "healthy": silent < 300 and p.get("status") != "offline",
            "crashes_total": len(crashes),
            "crashes_recent": len(recent_crashes),
            "last_crash": crashes[-1] if crashes else None,
            "rate_limited": rate_limited_agents.get(n, 0) > time.time(),
            "crash_pattern": _detect_crash_pattern(n),
        }

    recommendations = _generate_health_recommendations(agent_health)

    return {
        "agents": agent_health,
        "hub": {
            "uptime_sec": int((now - datetime.fromisoformat(list(activity)[0]["time"])).total_seconds()) if activity else 0,
            "tasks_total": len(tasks), "sse_connections": sum(sse_clients.values()),
            "memory_usage_mb": _estimate_memory(),
        },
        "crashes_recent": list(crash_log[-5:]),
        "recommendations": recommendations,
    }

def _crash_age_sec(crash, now):
    """Get age of a crash entry in seconds."""
    try:
        return (now - datetime.fromisoformat(crash["time"])).total_seconds()
    except (ValueError, TypeError, KeyError):
        return 99999


def _generate_health_recommendations(agent_health):
    """Generate actionable recommendations based on agent health."""
    recs = []
    for name, h in agent_health.items():
        if h.get("status") == "offline" and h.get("silent_sec", 0) > 600:
            recs.append({"agent": name, "level": "warn",
                         "msg": f"{name} has been offline for {h['silent_sec'] // 60} min — consider restarting"})
        if h.get("crashes_recent", 0) >= 3:
            recs.append({"agent": name, "level": "error",
                         "msg": f"{name} crashed {h['crashes_recent']}x in last 10 min — investigate or restart"})
        if h.get("rate_limited"):
            recs.append({"agent": name, "level": "info",
                         "msg": f"{name} is rate-limited — will auto-recover"})
        if h.get("crash_pattern"):
            recs.append({"agent": name, "level": "error",
                         "msg": h["crash_pattern"]})
    return recs


def _estimate_memory():
    try:
        total = len(json.dumps(tasks, default=str))
        total += len(json.dumps({k: list(v) if isinstance(v, deque) else v for k, v in messages.items()}, default=str))
        total += len(json.dumps(changes[-100:], default=str))
        total += len(json.dumps(analytics_log[-500:], default=str))
        total += sum(len(str(list(buf))) for buf in log_buffers.values())
        return round(total / 1024 / 1024, 2)
    except Exception as e:
        logger.warning(f"Memory estimate error: {e}")
        return 0.0

@router.get("/audit")
def get_audit(limit: int = 100, actor: str = "", action: str = ""):
    """Query audit log."""
    from hub.state import audit_log
    result = list(audit_log)
    if actor:
        result = [e for e in result if e.get("actor") == actor]
    if action:
        result = [e for e in result if e.get("action") == action]
    return result[-limit:]

@router.post("/shutdown")
def shutdown_system():
    """Gracefully shut down the entire system (hub + agents)."""
    import os, signal, threading
    from hub.state import save_state
    logger.info("Shutdown requested from dashboard")
    save_state()
    def _do_shutdown():
        import time; time.sleep(1)
        os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=_do_shutdown, daemon=True).start()
    return {"status": "ok", "message": "Shutting down..."}
