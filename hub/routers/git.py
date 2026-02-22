"""Git operations, changes, and file lock routes."""

import os
import re
from datetime import datetime
from fastapi import APIRouter

from hub.state import (
    lock, logger, WORKSPACE, agents, tasks, file_locks, file_plans, changes,
    FileLock, git_cmd, safe_project_dir, add_activity, save_state, bump_version,
)

router = APIRouter(tags=["git"])

# Paths that should never be committed via dashboard
GIT_EXCLUDE = [".claude/", ".multiagent/", ".mcp.json", "hub_state.json"]


def git_add_safe(d, files=None):
    """Stage changes. If files given, stage only those; otherwise stage all except excluded."""
    if files:
        git_cmd(["reset", "HEAD"], d)
        for f in files:
            if ".." in f or f.startswith("/"):
                continue
            git_cmd(["add", "--", f], d)
    else:
        git_cmd(["add", "-A"], d)
        for excl in GIT_EXCLUDE:
            git_cmd(["reset", "HEAD", "--", excl], d)

# ── Git Panel ──
@router.get("/git/branches")
def git_branches(project: str = ""):
    result = {}
    if project:
        d = safe_project_dir(project)
        dirs = [d] if d else []
    else:
        dirs = [os.path.join(WORKSPACE, d) for d in os.listdir(WORKSPACE)
                if os.path.isdir(os.path.join(WORKSPACE, d, ".git"))]
    for d in dirs:
        name = os.path.basename(d)
        ok, out = git_cmd(["branch", "-a", "--format=%(refname:short) %(upstream:short)"], d)
        if ok:
            branches = []
            _, current = git_cmd(["branch", "--show-current"], d)
            for line in out.split("\n"):
                parts = line.strip().split()
                if parts:
                    branches.append({
                        "name": parts[0], "current": parts[0] == current,
                        "upstream": parts[1] if len(parts) > 1 else ""
                    })
            result[name] = branches
    return result

@router.get("/git/log")
def git_log(project: str = "", n: int = 10):
    d = safe_project_dir(project) if project else WORKSPACE
    ok, out = git_cmd(["log", "--oneline", f"-{n}", "--format=%h|%s|%an|%ar"], d)
    if not ok:
        return []
    commits = []
    for line in out.split("\n"):
        parts = line.split("|", 3)
        if len(parts) >= 4:
            commits.append({"hash": parts[0], "message": parts[1], "author": parts[2], "when": parts[3]})
    return commits

@router.post("/git/rollback")
def git_rollback_api(data: dict):
    project = data.get("project", "")
    commit = data.get("commit", "")
    mode = data.get("mode", "discard")
    d = safe_project_dir(project) if project else WORKSPACE
    if not d:
        return {"status": "error", "message": "Invalid project"}
    if d and not os.path.realpath(d).startswith(os.path.realpath(WORKSPACE)):
        return {"status": "error", "message": "Invalid project path"}
    if not os.path.isdir(os.path.join(d, ".git")):
        return {"status": "error", "message": "No git repo"}
    if commit:
        ok, out = git_cmd(["reset", "--hard", commit], d)
    elif mode == "discard":
        ok1, _ = git_cmd(["checkout", "--", "."], d)
        ok2, _ = git_cmd(["clean", "-fd"], d)
        ok = ok1 or ok2
        out = ""
    else:
        ok, out = git_cmd(["reset", "--hard", "HEAD~1"], d)
    if ok:
        add_activity("user", "system", "git_rollback",
                     f"{'Discarded changes' if mode == 'discard' else 'Reset commit'}: {project or 'workspace'}")
        return {"status": "ok"}
    return {"status": "error", "message": f"Rollback failed: {out[:100]}"}

@router.post("/git/checkout")
def git_checkout_branch(data: dict):
    project = data.get("project", "")
    branch = data.get("branch", "")
    branch = re.sub(r'[^a-zA-Z0-9/_.\-]', '', branch)
    if not branch:
        return {"status": "error", "message": "No branch specified"}
    d = safe_project_dir(project) if project else WORKSPACE
    if not d:
        return {"status": "error", "message": "Invalid project"}
    if not os.path.isdir(os.path.join(d, ".git")):
        return {"status": "error", "message": "No git repo"}
    git_cmd(["stash"], d)
    ok, out = git_cmd(["checkout", branch], d)
    if ok:
        pop_ok, pop_out = git_cmd(["stash", "pop"], d)
        if not pop_ok and pop_out and "conflict" in pop_out.lower():
            git_cmd(["checkout", "--theirs", "."], d)
            git_cmd(["stash", "drop"], d)
        add_activity("user", "system", "git_checkout", f"Switched to {branch} in {project}")
        return {"status": "ok", "branch": branch}
    git_cmd(["stash", "pop"], d)
    return {"status": "error", "message": f"Checkout failed: {out[:100]}"}

@router.post("/git/commit")
def git_commit_api(data: dict):
    """Commit staged changes with user-provided message."""
    project = data.get("project", "")
    message = data.get("message", "").strip()
    if not project:
        return {"status": "error", "message": "No project specified"}
    if not message:
        return {"status": "error", "message": "No commit message"}
    # Sanitize commit message — remove shell metacharacters
    message = re.sub(r'[`$\\]', '', message)
    d = safe_project_dir(project)
    if not d:
        return {"status": "error", "message": "Invalid project"}
    if not os.path.isdir(os.path.join(d, ".git")):
        return {"status": "error", "message": "No git repo"}
    # Ensure changes are staged
    _, st = git_cmd(["status", "--short"], d)
    if not st:
        return {"status": "error", "message": "No changes to commit"}
    files = data.get("files")
    git_add_safe(d, files=files)
    ok, out = git_cmd(["commit", "-m", message], d)
    if ok:
        _, hash_out = git_cmd(["rev-parse", "--short", "HEAD"], d)
        _, branch = git_cmd(["branch", "--show-current"], d)
        add_activity("user", "system", "git_commit",
                     f"Committed to {project}/{branch}: {message[:60]}")
        logger.info(f"Commit {hash_out} on {project}/{branch}: {message[:60]}")
        # Mark related pending changes as approved
        with lock:
            for c in changes:
                if c.get("project") == project and c.get("status") == "pending":
                    c["status"] = "approved"
            save_state()
        bump_version()
        return {"status": "ok", "hash": hash_out, "branch": branch, "message": message}
    return {"status": "error", "message": f"Commit failed: {out[:200]}"}

@router.post("/git/push")
def git_push_api(data: dict):
    """Push current branch to remote."""
    project = data.get("project", "")
    d = safe_project_dir(project)
    if not d:
        return {"status": "error", "message": "Invalid project"}
    _, branch = git_cmd(["branch", "--show-current"], d)
    if not branch:
        return {"status": "error", "message": "No branch"}
    ok, out = git_cmd(["push", "-u", "origin", branch], d)
    if ok:
        add_activity("user", "system", "git_push", f"Pushed {project}/{branch}")
        bump_version()
        return {"status": "ok", "branch": branch}
    return {"status": "error", "message": f"Push failed: {out[:200]}"}

@router.get("/git/status")
def git_status(project: str = ""):
    result = {}
    if project:
        d = safe_project_dir(project)
        dirs = [d] if d else []
    else:
        dirs = [os.path.join(WORKSPACE, d) for d in os.listdir(WORKSPACE)
                if os.path.isdir(os.path.join(WORKSPACE, d, ".git"))]
    for d in dirs[:10]:
        name = os.path.basename(d)
        ok, out = git_cmd(["status", "--short"], d)
        _, branch = git_cmd(["branch", "--show-current"], d)
        result[name] = {
            "branch": branch, "changes": len([l for l in out.strip().split("\n") if l.strip()]) if out.strip() else 0,
            "status": out[:500] if ok else ""
        }
    return result

# ── Changes ──
@router.post("/changes")
def submit_change(data: dict):
    from hub.state import change_counter as _cc
    import hub.state as _st
    with lock:
        _st.change_counter += 1
        agent_name = data.get("agent", "?")
        branch = ""
        for t in tasks.values():
            if t.get("assigned_to") == agent_name and t.get("status") == "in_progress":
                branch = t.get("branch", "")
                break
        entry = {
            "id": _st.change_counter, "agent": agent_name,
            "project": data.get("project", ""), "description": data.get("description", ""),
            "diff": data.get("diff", ""), "files": data.get("files", []),
            "branch": branch,
            "timestamp": datetime.now().isoformat(), "status": "pending"
        }
        changes.append(entry)
        if len(changes) > 200:
            del changes[:100]
        save_state()
    return {"status": "ok", "id": _st.change_counter}

@router.get("/changes")
def get_changes(status: str = ""):
    # No lock — read-only
    if status:
        return [c for c in changes if c.get("status") == status]
    return list(changes[-50:])

@router.post("/changes/{cid}/review")
def review_change(cid: int, data: dict):
    with lock:
        for c in changes:
            if c["id"] == cid:
                c["status"] = data.get("status", "approved")
                save_state()
                return {"status": "ok"}
    return {"status": "not_found"}

# ── File Locks ──
@router.post("/files/lock")
def lock_file(fl: FileLock):
    with lock:
        if fl.file_path in file_locks and file_locks[fl.file_path]["agent"] != fl.agent_name:
            return {"status": "locked", "by": file_locks[fl.file_path]["agent"]}
        file_locks[fl.file_path] = {"agent": fl.agent_name, "since": datetime.now().isoformat()}
    return {"status": "ok"}

@router.post("/files/unlock")
def unlock_file(fl: FileLock):
    with lock:
        if fl.file_path in file_locks and file_locks[fl.file_path]["agent"] == fl.agent_name:
            del file_locks[fl.file_path]
    return {"status": "ok"}

@router.get("/files/locks")
def get_locks():
    with lock:
        now = datetime.now()
        stale = []
        for path, info in file_locks.items():
            agent_name = info.get("agent", "")
            a = agents.get(agent_name, {})
            try:
                last = datetime.fromisoformat(a.get("last_seen", "2000-01-01"))
                if (now - last).total_seconds() > 300:
                    stale.append(path)
            except (ValueError, TypeError):
                pass
        for path in stale:
            logger.info(f"Removing stale lock: {path} (agent offline)")
            del file_locks[path]
        return dict(file_locks)

@router.post("/files/locks/cleanup")
def cleanup_stale_locks():
    now = datetime.now()
    cleaned = []
    with lock:
        for path in list(file_locks.keys()):
            agent_name = file_locks[path].get("agent", "")
            a = agents.get(agent_name, {})
            try:
                last = datetime.fromisoformat(a.get("last_seen", "2000-01-01"))
                if (now - last).total_seconds() > 300:
                    cleaned.append({"path": path, "agent": agent_name})
                    del file_locks[path]
            except (ValueError, TypeError):
                cleaned.append({"path": path, "agent": agent_name})
                del file_locks[path]
    return {"status": "ok", "cleaned": cleaned}


# ── File Plans (pre-task coordination) ──

@router.post("/files/plan")
def submit_file_plan(data: dict):
    """Agent declares which files it intends to edit before starting work."""
    agent = data.get("agent_name", "")
    files = data.get("files", [])
    task_id = data.get("task_id", "")
    with lock:
        if not files:
            file_plans.pop(agent, None)
        else:
            file_plans[agent] = {
                "task_id": task_id,
                "files": files[:50],
                "timestamp": datetime.now().isoformat(),
            }
        bump_version()
    return {"status": "ok"}

@router.get("/files/plans")
def get_file_plans():
    """Get all agents' file plans for conflict detection."""
    return dict(file_plans)

@router.post("/files/check-conflicts")
def check_file_conflicts(data: dict):
    """Check if an agent's planned files conflict with other agents' plans or locks."""
    agent = data.get("agent_name", "")
    files = set(data.get("files", []))
    conflicts = []
    for other_agent, plan in file_plans.items():
        if other_agent == agent:
            continue
        overlap = files & set(plan.get("files", []))
        if overlap:
            conflicts.append({
                "agent": other_agent,
                "task_id": plan.get("task_id", ""),
                "conflicting_files": list(overlap)[:10],
            })
    # Also check existing file locks
    for path in files:
        if path in file_locks and file_locks[path].get("agent") != agent:
            conflicts.append({
                "agent": file_locks[path]["agent"],
                "task_id": "",
                "conflicting_files": [path],
            })
    return {"conflicts": conflicts, "has_conflicts": bool(conflicts)}
