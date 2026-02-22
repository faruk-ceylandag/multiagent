"""Agent management routes: register, add, remove, edit, status, progress, specialization, learning."""

import os, json, re, time
from datetime import datetime
from fastapi import APIRouter

from hub.state import (
    lock, logger, MA_DIR, WORKSPACE, ALL_AGENTS, agents, pipeline, log_buffers, log_counters,
    stop_signals, rate_limited_agents, agent_progress, agent_specialization,
    agent_learnings, agent_roles, messages, tasks, AgentStatus, add_activity, save_state,
    ROUTE_MAP, MULTI_SCOPE_KEYWORDS, bump_version,
)

router = APIRouter(tags=["agents"])

# ── Routing ──
@router.get("/route")
def detect_route(msg: str = ""):
    low = msg.lower().strip()

    intent = "task"
    _CHAT_PATTERNS = [
        r'^(hi|hello|hey|merhaba|selam|naber|nas\u0131l)',
        r'^(thanks|te\u015fekk\u00fcr|sa\u011fol|eyvallah|ok|tamam|anlad\u0131m|evet|hay\u0131r)',
        r'^(what|how|why|where|when|who|which|ne |nas\u0131l |neden |nerede |kim |hangi )',
        r'\?$',
        r'^(status|durum|ne oldu|neredesin|bitir?di mi|bitti mi)',
        r'^(show|list|g\u00f6ster|listele|ka\u00e7 tane)',
        r'^(stop|dur|cancel|iptal|bekle|wait)',
    ]
    _TASK_PATTERNS = [
        r'https?://',
        r'\.(js|ts|py|css|html|jsx|tsx|vue|go|rs)\b',
        r'^(fix|implement|create|build|deploy|refactor|update|add|remove|delete|write|make|d\u00fczelt|yap|ekle|olu\u015ftur|sil|g\u00fcncelle)',
        r'(bug|feature|issue|ticket|PR|pull request|merge|branch)',
        r'(deploy|release|test|lint|build|compile)',
    ]

    is_chat = any(re.search(p, low) for p in _CHAT_PATTERNS)
    is_task = any(re.search(p, low) for p in _TASK_PATTERNS)

    if is_task and not is_chat:
        intent = "task"
    elif is_chat and not is_task:
        intent = "chat"
    elif is_chat and is_task:
        intent = "task" if ("http" in low or re.search(r'\.\w{2,4}\b', low)) else "chat"
    else:
        # Default to chat — only create tasks when clearly a task
        intent = "chat"

    for a in ALL_AGENTS:
        if low.startswith(f"@{a} ") or low.startswith(f"{a}: ") or low.startswith(f"{a},") or low.startswith(f"{a} "):
            return {"target": a, "confidence": "explicit", "intent": intent}
    for a in ALL_AGENTS:
        if f" {a} " in f" {low} " or f"'{a}'" in low or f'"{a}"' in low:
            return {"target": a, "confidence": "high", "intent": intent}
    if "sentry" in low or "sentry.io" in low:
        friday_agent = next((a for a in ALL_AGENTS if "friday" in a.lower()), None)
        if friday_agent:
            return {"target": friday_agent, "confidence": "high", "intent": intent}

    # ── URL-domain-aware routing ──
    url_match = re.search(r'https?://([^\s/]+)', low)
    if url_match:
        domain = url_match.group(1)
        mcp_hint = None
        if "figma.com" in domain:
            mcp_hint = "figma"
            # Figma URLs → frontend agent (design implementation)
            if "frontend" in ALL_AGENTS:
                return {"target": "frontend", "confidence": "high", "intent": "task", "mcp_hint": mcp_hint}
        elif "github.com" in domain:
            mcp_hint = "github"
            # GitHub PR/issue URLs → detect scope from path
            gh_path = re.search(r'github\.com/[^/]+/[^/]+/(pull|issue|compare)', low)
            if gh_path:
                # PR/issue → architect to triage, or direct agent if mentioned
                if "architect" in ALL_AGENTS:
                    return {"target": "architect", "confidence": "high", "intent": "task", "mcp_hint": mcp_hint}
        elif "linear.app" in domain:
            mcp_hint = "linear"
        elif "notion.so" in domain or "notion.site" in domain:
            mcp_hint = "notion"
        elif any(x in domain for x in ["atlassian.net", "jira.", "confluence."]):
            mcp_hint = "atlassian"
        elif any(x in domain for x in ["docs.google.com", "sheets.google.com", "slides.google.com", "drive.google.com"]):
            mcp_hint = "google"
        # Attach mcp_hint to route result for downstream use
        if mcp_hint:
            intent = "task"  # URLs with known MCP domains are tasks

    scope_hits = {}
    for role, kws in ROUTE_MAP.items():
        if role in ALL_AGENTS:
            hits = sum(1 for kw in kws if kw in low)
            if hits > 0:
                scope_hits[role] = hits
    if len(scope_hits) == 1:
        agent = list(scope_hits.keys())[0]
        return {"target": agent, "confidence": "high", "intent": intent}
    if len(scope_hits) >= 2 or any(kw in low for kw in MULTI_SCOPE_KEYWORDS):
        return {"target": "architect" if "architect" in ALL_AGENTS else ALL_AGENTS[0], "confidence": "high", "intent": intent}
    scores = {a: 0 for a in ALL_AGENTS}
    for agent, kws in ROUTE_MAP.items():
        if agent in scores:
            for kw in kws:
                if kw in low:
                    scores[agent] += 2
    # Factor in expertise
    for a in ALL_AGENTS:
        spec = agent_specialization.get(a, {})
        if spec.get("score", 0) > 5:
            scores[a] = scores.get(a, 0) + 1
    # Factor in availability: boost idle agents, penalize busy/rate-limited
    for a in ALL_AGENTS:
        p = pipeline.get(a, {})
        status = p.get("status", "offline")
        if status == "idle":
            scores[a] = scores.get(a, 0) + 2  # prefer idle agents
        elif status == "working":
            scores[a] = scores.get(a, 0) - 1  # penalize busy agents
        if rate_limited_agents.get(a, 0) > time.time():
            scores[a] = scores.get(a, 0) - 5  # strongly avoid rate-limited
        if status == "offline":
            scores[a] = scores.get(a, 0) - 3  # penalize offline
        # Penalize agents with large task queues
        agent_tasks = sum(1 for t in tasks.values()
                         if t.get("assigned_to") == a and t.get("status") in ("created", "assigned", "in_progress"))
        if agent_tasks >= 3:
            scores[a] = scores.get(a, 0) - 2

    best = max(scores, key=scores.get) if scores else ALL_AGENTS[0]
    if scores.get(best, 0) >= 2:
        return {"target": best, "confidence": "high", "intent": intent}
    if scores.get(best, 0) == 1 and sum(1 for v in scores.values() if v > 0) == 1:
        return {"target": best, "confidence": "medium", "intent": intent}
    if len(msg) > 100:
        return {"target": "architect" if "architect" in ALL_AGENTS else ALL_AGENTS[0], "confidence": "default", "intent": intent}
    return {"target": "architect" if "architect" in ALL_AGENTS else ALL_AGENTS[0], "confidence": "default", "intent": intent}

# ── Register / CRUD ──
@router.post("/agents/register")
def register(data: dict):
    name = data.get("agent_name", "")
    role = data.get("role", "")
    with lock:
        agents[name] = {"status": data.get("status", "alive"), "last_seen": datetime.now().isoformat()}
        pipeline[name] = {"status": "booting", "detail": "", "since": datetime.now().isoformat()}
        if name not in log_buffers:
            log_buffers[name] = __import__("collections").deque(maxlen=3000)
            log_counters[name] = 0
        if role:
            agent_roles[name] = role[:200]
        # Broadcast agent_joined to peers
        for other in ALL_AGENTS:
            if other != name:
                messages.setdefault(other, []).append({
                    "sender": "system", "receiver": other,
                    "content": json.dumps({"subtype": "agent_joined", "data": {
                        "agent": name, "role": agent_roles.get(name, ""),
                    }}),
                    "msg_type": "ecosystem_update",
                })
        from hub.state import add_audit
        add_audit(name, "agent_register", {"role": role[:50]})
        bump_version()
    return {"status": "ok"}

@router.post("/agents/add")
def add_agent(data: dict):
    name = data.get("name", "").strip().lower()
    if not name or not re.match(r'^[a-z][a-z0-9_-]{0,19}$', name):
        return {"status": "error", "message": "Invalid name"}
    role = data.get("role", "")
    model = data.get("model", "")
    with lock:
        if name in ALL_AGENTS:
            return {"status": "error", "message": f"Agent '{name}' exists"}
        ALL_AGENTS.append(name)
        agents[name] = {"status": "pending", "last_seen": datetime.now().isoformat()}
        pipeline[name] = {"status": "pending", "detail": "waiting for spawn", "since": datetime.now().isoformat()}
        log_buffers[name] = __import__("collections").deque(maxlen=3000)
        log_counters[name] = 0
    _save_agent_config(name, role, model, action="add")
    _generate_role_file(name, role)
    add_activity("system", name, "agent_add", f"Agent {name} added")
    return {"status": "ok", "agent": name, "needs_restart": True}

@router.post("/agents/remove")
def remove_agent(data: dict):
    name = data.get("name", "").strip().lower()
    with lock:
        if name not in ALL_AGENTS:
            return {"status": "error", "message": "Not found"}
        ALL_AGENTS.remove(name)
        agents.pop(name, None)
        pipeline.pop(name, None)
    _save_agent_config(name, "", "", action="remove")
    add_activity("system", name, "agent_remove", f"Agent {name} removed")
    return {"status": "ok"}

@router.post("/agents/edit")
def edit_agent(data: dict):
    name = data.get("name", "").strip().lower()
    with lock:
        if name not in ALL_AGENTS:
            return {"status": "error", "message": "Not found"}
    role = data.get("role")
    model = data.get("model")
    cfg_path = os.path.join(MA_DIR, "config.json") if MA_DIR else ""
    if cfg_path and os.path.exists(cfg_path):
        try:
            with open(cfg_path) as f:
                cfg = json.load(f)
            for a in cfg.get("agents", []):
                if isinstance(a, dict) and a.get("name") == name:
                    if role is not None:
                        a["role"] = role
                    if model is not None:
                        a["model"] = model
            with open(cfg_path, "w") as f:
                json.dump(cfg, f, indent=2)
        except Exception as e:
            logger.warning(f"Agent edit config error: {e}")
    if role is not None:
        _generate_role_file(name, role)
    with lock:
        add_activity("system", name, "agent_edit", f"Agent {name} updated")
    return {"status": "ok", "message": f"'{name}' updated. Changes on next boot."}

@router.get("/agents/available-roles")
def available_roles():
    return [
        {"name": "architect", "desc": "System architect & team lead"},
        {"name": "frontend", "desc": "Frontend developer"},
        {"name": "backend", "desc": "Backend developer"},
        {"name": "qa", "desc": "Quality assurance & testing"},
        {"name": "devops", "desc": "Docker, CI/CD, infrastructure"},
        {"name": "security", "desc": "Security audit & hardening"},
        {"name": "custom", "desc": "Custom role"},
    ]

def _save_agent_config(name, role, model, action):
    cfg_path = os.path.join(MA_DIR, "config.json") if MA_DIR else ""
    if not cfg_path:
        return
    try:
        with open(cfg_path) as f:
            cfg = json.load(f)
    except Exception as e:
        logger.warning(f"Config read error: {e}")
        cfg = {}
    ac = cfg.get("agents", [])
    if action == "add":
        entry = {"name": name}
        if role:
            entry["role"] = role
        if model:
            entry["model"] = model
        ac.append(entry)
    elif action == "remove":
        ac = [a for a in ac if (a.get("name") if isinstance(a, dict) else a) != name]
    cfg["agents"] = ac
    with open(cfg_path, "w") as f:
        json.dump(cfg, f, indent=2)

def _generate_role_file(name, role):
    if not MA_DIR:
        return
    # Build team-aware role with roster + stack info (same quality as boot-time generation)
    try:
        from lib.roles import DEFAULT_ROLES, _default_desc
        # Use full role or default
        if role and len(role) > 20:
            content = f"# {name.title()}\n{role}"
        else:
            content = DEFAULT_ROLES.get(name, f"# {name.title()}\nYou are the {name} specialist agent.")
        # Add team roster
        other_agents = [a for a in ALL_AGENTS if a != name]
        if other_agents:
            content += "\n\n## YOUR TEAM\nYou work with these agents. Contact them directly when needed:"
            for a in other_agents:
                role_desc = agent_roles.get(a, "") or _default_desc(a)
                spec = agent_specialization.get(a, {})
                score = spec.get("score", 0)
                score_str = f" (expertise: {score})" if score > 0 else ""
                content += f"\n  - **{a}**: {role_desc[:80]}{score_str}"
        # Add stack info
        stack_path = os.path.join(MA_DIR, "stack.json")
        if os.path.exists(stack_path):
            with open(stack_path) as f:
                stacks = json.load(f)
            if stacks:
                content += "\n\n## DETECTED TECH STACK"
                for proj, st in stacks.items():
                    langs = ", ".join(st.get("lang", []))
                    fws = ", ".join(st.get("fw", []))
                    content += f"\n  {proj}: {langs}{' / ' + fws if fws else ''}"
    except Exception:
        content = f"# {name.title()}\n{role}" if role and len(role) > 10 else f"# {name.title()}\nYou are the {name} specialist agent."
    path = os.path.join(MA_DIR, f"{name}-role.md")
    with open(path, "w") as f:
        f.write(content)
    sd = os.path.join(MA_DIR, "sessions", name)
    os.makedirs(sd, exist_ok=True)
    cd = os.path.join(sd, ".claude")
    os.makedirs(cd, exist_ok=True)
    sf = os.path.join(cd, "settings.json")
    if not os.path.exists(sf):
        with open(sf, "w") as f:
            json.dump({"permissions": {"allow": ["Edit", "Write", "Read", "Bash(*)"], "deny": []}}, f)

# ── Poll & Status ──
@router.post("/poll/{name}")
def combined_poll(name: str, timeout: int = 0):
    """Polling endpoint. Returns immediately.
    last_seen write is protected by lock to avoid race with dashboard snapshot."""
    if name in agents:
        with lock:
            agents[name]["last_seen"] = datetime.now().isoformat()
    count = len(messages.get(name, []))
    should_stop = stop_signals.pop(name, False)
    return {"status": "ok", "count": count, "stop": should_stop}

@router.post("/agents/status")
def agent_status(s: AgentStatus):
    with lock:
        pipeline[s.agent_name] = {"status": s.status, "detail": s.detail, "since": datetime.now().isoformat()}
        bump_version()
    return {"status": "ok"}

@router.post("/agents/rate_limited")
def agent_rate_limited(data: dict):
    with lock:
        rate_limited_agents[data.get("agent_name", "")] = data.get("until", 0)
    return {"status": "ok"}

@router.post("/agents/progress")
def agent_progress_update(data: dict):
    name = data.get("agent_name", "")
    with lock:
        agent_progress[name] = {
            "event": data.get("event", ""), "detail": data.get("detail", ""),
            "task_id": data.get("task_id", ""), "time": datetime.now().isoformat(),
            "task_tokens": data.get("task_tokens", 0),
            "task_calls": data.get("task_calls", 0),
            "elapsed": data.get("elapsed", 0),
            "project": data.get("project", ""),
        }
        bump_version()
    return {"status": "ok"}

@router.get("/agents/progress")
def get_all_progress():
    return dict(agent_progress)

@router.post("/agents/specialization")
def update_specialization(data: dict):
    name = data.get("agent_name", "")
    task_type = data.get("task_type", "general")
    success = data.get("success", True)
    with lock:
        if name not in agent_specialization:
            agent_specialization[name] = {"tasks": {}, "total_done": 0, "total_failed": 0}
        s = agent_specialization[name]
        if task_type not in s["tasks"]:
            s["tasks"][task_type] = {"done": 0, "failed": 0}
        if success:
            s["tasks"][task_type]["done"] += 1
            s["total_done"] += 1
        else:
            s["tasks"][task_type]["failed"] += 1
            s["total_failed"] += 1
        total = s["total_done"] + s["total_failed"]
        s["score"] = round(s["total_done"] / max(1, total) * 10, 1)
        save_state()
    return {"status": "ok"}

@router.get("/agents/specialization")
def get_specialization():
    return dict(agent_specialization)

@router.post("/agents/learning")
def add_learning(data: dict):
    with lock:
        agent_learnings.append({
            "agent": data.get("agent_name", ""), "task": data.get("task", ""),
            "learning": data.get("learning", ""), "time": datetime.now().isoformat()
        })
        if len(agent_learnings) > 300:
            del agent_learnings[:100]
        save_state()
    return {"status": "ok"}

@router.get("/agents/learnings")
def get_learnings(agent: str = "", top: int = 0):
    if agent:
        results = [l for l in agent_learnings if l.get("agent") == agent]
    else:
        results = list(agent_learnings[-100:])
    if top > 0:
        # Deduplicate by learning text, keep most recent
        seen = set()
        deduped = []
        for l in reversed(results):
            key = l.get("learning", "")[:100]
            if key not in seen:
                seen.add(key)
                deduped.append(l)
        deduped.reverse()
        results = deduped[-top:]
    return results

@router.get("/agents")
def list_agents():
    return dict(agents)

@router.get("/agents/profiles")
def get_agent_profiles():
    """Return agent profiles: name, role, expertise, status. Lock-free read."""
    profiles = {}
    for name in ALL_AGENTS:
        spec = agent_specialization.get(name, {})
        p = pipeline.get(name, {})
        profiles[name] = {
            "role": agent_roles.get(name, ""),
            "expertise": spec.get("score", 0),
            "total_done": spec.get("total_done", 0),
            "total_failed": spec.get("total_failed", 0),
            "top_tasks": sorted(spec.get("tasks", {}).items(),
                                key=lambda x: x[1].get("done", 0), reverse=True)[:3]
                         if spec.get("tasks") else [],
            "status": p.get("status", "offline"),
            "detail": p.get("detail", ""),
        }
    return profiles

@router.get("/mcp/status")
def get_mcp_status():
    """Return MCP server status per agent."""
    result = {}
    for name in ALL_AGENTS:
        session_dir = os.path.join(MA_DIR, "sessions", name) if MA_DIR else ""
        mcp_file = os.path.join(session_dir, ".mcp.json") if session_dir else ""
        servers = []
        if mcp_file and os.path.exists(mcp_file):
            try:
                with open(mcp_file) as f:
                    mcp_data = json.load(f)
                for srv_name, srv in mcp_data.get("mcpServers", {}).items():
                    servers.append({"name": srv_name, "type": srv.get("type", ""),
                                   "has_env": bool(srv.get("env"))})
            except Exception:
                pass
        result[name] = servers
    return result

# ── Stop / Restart ──
@router.post("/agents/{name}/stop")
def stop_agent(name: str):
    with lock:
        stop_signals[name] = True
        add_activity("user", name, "stop", f"Stop signal sent to {name}")
    return {"status": "ok"}

@router.post("/agents/{name}/restart")
def restart_agent(name: str):
    with lock:
        pipeline[name] = {"status": "restarting", "detail": "restart requested", "since": datetime.now().isoformat()}
        add_activity("user", name, "restart", f"Restart requested for {name}")
    return {"status": "ok"}
