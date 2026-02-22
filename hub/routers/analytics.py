"""Analytics, export, replay, activity, dashboard data, projects, autoscale, users."""

import os, re, json, time
from datetime import datetime
from collections import deque
from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from hub.state import (
    lock, logger, WORKSPACE, ALL_AGENTS, HIDDEN_AGENTS, agents, pipeline, sessions, tasks,
    usage_log, file_locks, messages, changes, activity, analytics_log,
    rate_limited_agents, sse_clients, agent_progress, agent_specialization,
    agent_learnings, test_results, auto_scale_config, user_sessions,
    BUDGET_LIMIT, calc_agent_cost, calc_total_cost, git_cmd, save_state,
    get_dashboard_snapshot,
)

router = APIRouter(tags=["analytics"])

# ── Activity ──
@router.get("/activity")
def get_activity(limit: int = 80):
    return list(activity)[-limit:]

# ── Analytics ──
@router.get("/analytics")
def get_analytics():
    # No lock — read-only display data. Quick copies prevent RuntimeError.
    usage_snap = dict(usage_log)
    tasks_snap = list(tasks.values())
    rl_snap = dict(rate_limited_agents)
    by_agent = {}
    all_names = sorted((set(ALL_AGENTS) | set(usage_snap.keys())) - HIDDEN_AGENTS)
    for a in all_names:
        u = usage_snap.get(a, {})
        td = len([t for t in tasks_snap if t.get("assigned_to") == a and t.get("status") == "done"])
        tf = len([t for t in tasks_snap if t.get("assigned_to") == a and t.get("status") == "failed"])
        spec = agent_specialization.get(a, {})
        by_agent[a] = {
            "tokens_in": u.get("tokens_in", 0), "tokens_out": u.get("tokens_out", 0),
            "requests": u.get("requests", 0), "tasks_done": td, "tasks_failed": tf,
            "sonnet_in": u.get("sonnet_in", 0), "sonnet_out": u.get("sonnet_out", 0),
            "opus_in": u.get("opus_in", 0), "opus_out": u.get("opus_out", 0),
            "cost": calc_agent_cost(a), "expertise_score": spec.get("score", 0),
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
    return {
        "by_agent": by_agent, "durations": durations, "total_tasks": len(tasks),
        "tasks_done": len([t for t in tasks_snap if t.get("status") == "done"]),
        "tasks_pending": len([t for t in tasks_snap if t.get("status") in ("created", "assigned", "in_progress")]),
        "rate_limited": {k: v for k, v in rl_snap.items() if v > time.time()},
        "sse_clients": dict(sse_clients),
        "budget": {
            "total_spent": calc_total_cost(), "limit": BUDGET_LIMIT,
            "remaining": max(0, BUDGET_LIMIT - calc_total_cost()) if BUDGET_LIMIT else None,
        },
        "tests": test_summary,
    }

# ── Export ──
@router.get("/export")
def export_session(fmt: str = "md"):
    if fmt == "json":
        return {
            "workspace": WORKSPACE, "generated": datetime.now().isoformat(),
            "total_cost": calc_total_cost(),
            "agents": {a: {"cost": calc_agent_cost(a), "usage": usage_log.get(a, {}),
                           "specialization": agent_specialization.get(a, {})} for a in ALL_AGENTS},
            "tasks": list(tasks.values()), "test_results": list(test_results[-100:]),
            "learnings": list(agent_learnings[-100:]), "activity": list(activity)[-200:],
        }
    lines = [
        f"# Multi-Agent Session Report",
        f"**Workspace:** {WORKSPACE}",
        f"**Generated:** {datetime.now().isoformat()}",
        f"**Total Cost:** ${calc_total_cost():.2f}", "",
    ]
    lines.append("## Agents")
    for a in ALL_AGENTS:
        u = usage_log.get(a, {})
        spec = agent_specialization.get(a, {})
        lines.append(f"- **{a}**: {u.get('requests', 0)} calls, ${calc_agent_cost(a):.2f}, "
                     f"expertise: {spec.get('score', 0)}/10")
    lines.extend(["", "## Tasks"])
    for t in sorted(tasks.values(), key=lambda x: x.get("id", 0)):
        p = f"P{t.get('priority', 5)}" if t.get('priority') else ""
        lines.append(f"- #{t['id']} [{t.get('status', '?')}] {p} {t.get('description', '')} \u2192 {t.get('assigned_to', '?')}")
    lines.extend(["", "## Test Results"])
    for tr in test_results[-20:]:
        lines.append(f"- {tr.get('agent', '?')}: \u2713{tr.get('tests_passed', 0)} \u2717{tr.get('tests_failed', 0)} lint:{tr.get('lint_errors', 0)}")
    lines.extend(["", "## Learnings"])
    for l in agent_learnings[-30:]:
        lines.append(f"- [{l.get('agent', '')}] {l.get('learning', '')[:100]}")
    return PlainTextResponse("\n".join(lines), media_type="text/markdown",
                             headers={"Content-Disposition": "attachment; filename=session-report.md"})

# ── Dashboard Data ──
@router.get("/dashboard")
def dashboard_data():
    """Uses cached snapshot — same data as WebSocket, no lock needed."""
    return get_dashboard_snapshot()

# ── Projects ──
_PROJECT_MARKERS = {"package.json", "composer.json", "go.mod", "Cargo.toml",
                    "pyproject.toml", "requirements.txt", "Gemfile", "pom.xml",
                    "build.gradle", "Makefile", "CMakeLists.txt", "setup.py"}

@router.get("/projects")
def list_projects():
    result = []
    try:
        # Single-project: workspace root has project markers OR .git
        _has_marker = any(os.path.exists(os.path.join(WORKSPACE, m)) for m in _PROJECT_MARKERS)
        _has_git = os.path.isdir(os.path.join(WORKSPACE, ".git"))
        if _has_marker or _has_git:
            is_git = os.path.isdir(os.path.join(WORKSPACE, ".git"))
            branch, changes_count = "", 0
            if is_git:
                _, branch = git_cmd(["branch", "--show-current"], WORKSPACE)
                _, st = git_cmd(["status", "--short"], WORKSPACE)
                changes_count = len([l for l in st.split("\n") if l.strip()])
            task_count = len([t for t in tasks.values() if t.get("project") == "."])
            active_agents = set()
            for t in tasks.values():
                if t.get("project") == "." and t.get("status") == "in_progress":
                    active_agents.add(t.get("assigned_to", ""))
            result.append({
                "name": os.path.basename(WORKSPACE), "git": is_git, "branch": branch,
                "changes": changes_count, "tasks": task_count,
                "active_agents": list(active_agents), "single_project": True,
            })
            return result

        # Multi-project: scan subdirectories for actual projects
        skip = {".multiagent", ".claude", "node_modules", "__pycache__", "vendor", "dist", "build",
                ".next", ".cache", ".git", ".github", "logs", ".vscode", ".idea"}
        for name in sorted(os.listdir(WORKSPACE)):
            path = os.path.join(WORKSPACE, name)
            if not os.path.isdir(path) or name.startswith(".") or name in skip:
                continue
            is_git = os.path.isdir(os.path.join(path, ".git"))
            has_marker = any(os.path.exists(os.path.join(path, m)) for m in _PROJECT_MARKERS)
            if not (is_git and has_marker):
                continue
            branch, changes_count = "", 0
            if is_git:
                _, branch = git_cmd(["branch", "--show-current"], path)
                _, st = git_cmd(["status", "--short"], path)
                changes_count = len([l for l in st.split("\n") if l.strip()])
            task_count = len([t for t in tasks.values() if t.get("project") == name])
            active_agents = set()
            for t in tasks.values():
                if t.get("project") == name and t.get("status") == "in_progress":
                    active_agents.add(t.get("assigned_to", ""))
            result.append({
                "name": name, "git": is_git, "branch": branch,
                "changes": changes_count, "tasks": task_count,
                "active_agents": list(active_agents),
            })
    except Exception as e:
        logger.warning(f"Project list error: {e}")
    return result

# ── Auto-Scaling ──
@router.get("/autoscale/status")
def autoscale_status():
    # No lock — read-only
    pending = len([t for t in tasks.values() if t.get("status") in ("created", "assigned")])
    idle_agents = [n for n in ALL_AGENTS if pipeline.get(n, {}).get("status") == "idle"]
    working_agents = [n for n in ALL_AGENTS if pipeline.get(n, {}).get("status") == "working"]
    return {
        "enabled": auto_scale_config.get("enabled", False),
        "pending_tasks": pending, "idle_agents": len(idle_agents),
        "working_agents": len(working_agents), "total_agents": len(ALL_AGENTS),
        "min": auto_scale_config.get("min_agents", 2),
        "max": auto_scale_config.get("max_agents", 8),
        "recommendation": (
            "scale_up" if pending > len(ALL_AGENTS) * 2 and len(ALL_AGENTS) < auto_scale_config.get("max_agents", 8)
            else "scale_down" if len(idle_agents) > 2 and len(ALL_AGENTS) > auto_scale_config.get("min_agents", 2)
            else "ok"
        ),
    }

@router.post("/autoscale/config")
def set_autoscale(data: dict):
    with lock:
        auto_scale_config.update(data)
        return {"status": "ok", "config": dict(auto_scale_config)}

# ── Replay / Timeline ──
@router.get("/replay")
def get_replay(since: str = "", until: str = ""):
    # No lock — read-only
    events = list(activity)
    if since:
        try:
            events = [e for e in events if e.get("time", "") >= since]
        except (ValueError, TypeError):
            pass
    if until:
        try:
            events = [e for e in events if e.get("time", "") <= until]
        except (ValueError, TypeError):
            pass
    task_events = []
    for e in events:
        entry = dict(e)
        if "task" in e.get("type", ""):
            tid_match = re.search(r'#(\d+)', e.get("preview", ""))
            if tid_match:
                tid = int(tid_match.group(1))
                if tid in tasks:
                    entry["task_snapshot"] = {
                        "id": tid, "status": tasks[tid].get("status"),
                        "agent": tasks[tid].get("assigned_to", ""),
                    }
        task_events.append(entry)
    return {
        "events": task_events[-500:],
        "duration_sec": _calc_session_duration(),
        "summary": {
            "total_events": len(task_events),
            "tasks_created": len([e for e in task_events if e.get("type") == "task_create"]),
            "tasks_completed": len([e for e in task_events if e.get("type") == "task_update" and "done" in e.get("preview", "")]),
            "messages_sent": len([e for e in task_events if e.get("type") in ("message", "task")]),
        },
    }

def _calc_session_duration():
    if not activity:
        return 0
    try:
        first = datetime.fromisoformat(list(activity)[0]["time"])
        last = datetime.fromisoformat(list(activity)[-1]["time"])
        return int((last - first).total_seconds())
    except (ValueError, TypeError):
        return 0

# ── Users ──
@router.post("/users/identify")
def identify_user(data: dict):
    uid = data.get("user_id", "default")
    name = data.get("display_name", uid)
    with lock:
        user_sessions[uid] = {"name": name, "last_seen": datetime.now().isoformat(),
                              "color": data.get("color", "#8b5cf6")}
    return {"status": "ok", "user_id": uid}

@router.get("/users")
def list_users():
    return dict(user_sessions)

@router.get("/metrics")
def get_metrics():
    """Basic request metrics."""
    from hub.state import request_counts, error_counts
    return {
        "requests": dict(sorted(request_counts.items(), key=lambda x: -x[1])[:30]),
        "errors": dict(sorted(error_counts.items(), key=lambda x: -x[1])[:20]),
        "total_requests": sum(request_counts.values()),
        "total_errors": sum(error_counts.values()),
    }
