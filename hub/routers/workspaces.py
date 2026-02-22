"""Workspace management routes: add, remove, list, scan."""

import os, hashlib
from datetime import datetime
from fastapi import APIRouter

from hub.state import (
    lock, logger, WORKSPACE, workspace_registry, _cfg,
    add_activity, save_state, bump_version,
)

router = APIRouter(tags=["workspaces"])


def _generate_ws_id(path):
    """Generate a short deterministic workspace ID from path."""
    return hashlib.md5(path.encode()).hexdigest()[:8]


def _scan_workspace(path):
    """Scan a workspace path for projects and tech stacks."""
    from lib.config import scan_projects, detect_stack
    projects = scan_projects(path)
    stacks = {}
    for p in projects[:10]:
        proj_dir = os.path.join(path, p) if p != "." else path
        st = detect_stack(proj_dir)
        if st.get("lang"):
            stacks[p] = st
    return projects, stacks


@router.post("/workspaces/add")
def add_workspace(data: dict):
    """Register a new workspace."""
    path = data.get("path", "").strip()
    alias = data.get("alias", "").strip()
    if not path:
        return {"status": "error", "message": "path required"}
    path = os.path.realpath(os.path.expanduser(path))
    if not os.path.isdir(path):
        return {"status": "error", "message": f"Directory not found: {path}"}

    ws_id = _generate_ws_id(path)

    # Check if already registered
    if ws_id in workspace_registry:
        return {"status": "error", "message": "Workspace already registered", "ws_id": ws_id}

    projects, stacks = _scan_workspace(path)

    with lock:
        workspace_registry[ws_id] = {
            "path": path,
            "name": alias or os.path.basename(path),
            "projects": projects,
            "stacks": stacks,
            "added_at": datetime.now().isoformat(),
            "active": True,
        }
        add_activity("user", "system", "workspace_add", f"Workspace added: {alias or os.path.basename(path)}")
        bump_version()
        save_state()

    return {"status": "ok", "ws_id": ws_id, "projects": projects, "stacks": stacks}


@router.delete("/workspaces/{ws_id}")
def remove_workspace(ws_id: str):
    """Unregister a workspace (files are NOT deleted)."""
    with lock:
        if ws_id not in workspace_registry:
            return {"status": "not_found"}
        name = workspace_registry[ws_id].get("name", ws_id)
        del workspace_registry[ws_id]
        add_activity("user", "system", "workspace_remove", f"Workspace removed: {name}")
        bump_version()
        save_state()
    return {"status": "ok"}


@router.get("/workspaces")
def list_workspaces():
    """List all registered workspaces with stats."""
    result = []
    # Always include primary workspace
    primary = {
        "ws_id": "primary",
        "path": WORKSPACE,
        "name": os.path.basename(WORKSPACE) if WORKSPACE else "default",
        "is_primary": True,
        "active": True,
    }
    result.append(primary)

    for ws_id, ws in workspace_registry.items():
        result.append({
            "ws_id": ws_id,
            "path": ws.get("path", ""),
            "name": ws.get("name", ""),
            "projects": ws.get("projects", []),
            "stacks": ws.get("stacks", {}),
            "added_at": ws.get("added_at", ""),
            "active": ws.get("active", True),
            "is_primary": False,
        })
    return result


@router.get("/workspaces/{ws_id}")
def get_workspace(ws_id: str):
    """Get workspace details including git status."""
    if ws_id == "primary":
        path = WORKSPACE
        name = os.path.basename(WORKSPACE) if WORKSPACE else "default"
    elif ws_id in workspace_registry:
        path = workspace_registry[ws_id].get("path", "")
        name = workspace_registry[ws_id].get("name", "")
    else:
        return {"status": "not_found"}

    # Get git status for all projects
    from hub.state import git_cmd
    git_status = {}
    from lib.config import scan_projects
    projects = scan_projects(path)
    for p in projects[:10]:
        proj_dir = os.path.join(path, p) if p != "." else path
        if os.path.isdir(os.path.join(proj_dir, ".git")):
            _, branch = git_cmd(["branch", "--show-current"], proj_dir)
            _, status = git_cmd(["status", "--short"], proj_dir)
            changes = len([l for l in status.strip().split("\n") if l.strip()]) if status.strip() else 0
            git_status[p] = {"branch": branch, "changes": changes}

    return {
        "ws_id": ws_id, "path": path, "name": name,
        "projects": projects, "git": git_status,
    }


@router.post("/workspaces/{ws_id}/scan")
def scan_workspace(ws_id: str):
    """Re-scan a workspace for projects and stacks."""
    if ws_id == "primary":
        path = WORKSPACE
    elif ws_id in workspace_registry:
        path = workspace_registry[ws_id].get("path", "")
    else:
        return {"status": "not_found"}

    projects, stacks = _scan_workspace(path)

    if ws_id != "primary" and ws_id in workspace_registry:
        with lock:
            workspace_registry[ws_id]["projects"] = projects
            workspace_registry[ws_id]["stacks"] = stacks
            bump_version()
            save_state()

    return {"status": "ok", "projects": projects, "stacks": stacks}
