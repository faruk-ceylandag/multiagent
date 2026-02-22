"""Message routes: send, consume, dismiss, broadcast, chat, sessions."""

from datetime import datetime
from fastapi import APIRouter

from hub.state import (
    lock, messages, chat_queue, pipeline, ALL_AGENTS, sessions,
    tasks, usage_log, changes, activity, analytics_log, agent_specialization,
    agent_learnings, pending_plans, Message, SessionInfo,
    rate_ok, add_activity, save_state, send_notification, WORKSPACE,
    bump_version,
)

router = APIRouter(tags=["messages"])

@router.get("/messages/{name}")
def get_messages(name: str, peek: bool = False, consume: bool = True):
    # Peek and non-consume are read-only — no lock needed
    if peek:
        return {"count": len(messages.get(name, []))}
    if not consume:
        return list(messages.get(name, []))
    # Consume mode — atomically read + clear, needs lock
    with lock:
        result = list(messages.get(name, []))
        messages[name] = []
    return result

@router.get("/messages/{name}/chat")
def get_chat_messages(name: str):
    with lock:
        msgs = chat_queue.get(name, [])
        if not msgs:
            return []
        chat_queue[name] = []
        return msgs

@router.post("/messages")
def send_message(msg: Message):
    if len(msg.content) > 100000:
        msg.content = msg.content[:100000]
    with lock:
        if not rate_ok(msg.sender):
            return {"status": "rate_limited"}
        ts = datetime.now().isoformat()
        entry = {**msg.model_dump(), "timestamp": ts}

        # ── Plan proposal handling ──
        if msg.msg_type == "plan_proposal":
            import hub.state as _st
            _st._plan_counter += 1
            plan_id = _st._plan_counter
            plan_steps = entry.get("plan_steps", [])
            pending_plans[plan_id] = {
                "plan_id": plan_id,
                "steps": plan_steps,
                "project": entry.get("project", ""),
                "branch": entry.get("branch", ""),
                "status": "pending",
                "created_by": msg.sender,
                "created": ts,
                "summary": msg.content[:500],
            }
            entry["plan_id"] = plan_id
            # Route to user inbox
            entry["receiver"] = "user"
            messages.setdefault("user", []).append(entry)
            if len(messages["user"]) > 200:
                messages["user"] = messages["user"][-150:]
            add_activity(msg.sender, "user", "plan_proposal", msg.content[:80])
            bump_version()
            save_state()
            return {"status": "ok", "plan_id": plan_id}

        if msg.msg_type == "chat" and msg.sender == "user":
            p = pipeline.get(msg.receiver, {})
            if p.get("status") in ("working", "booting"):
                chat_queue.setdefault(msg.receiver, []).append(entry)
                messages.setdefault("user", []).append(entry)
                add_activity(msg.sender, msg.receiver, "chat", msg.content[:80])
                return {"status": "ok", "queued": "chat"}
        messages.setdefault(msg.receiver, []).append(entry)
        # Trim receiver queue to prevent unbounded growth
        if len(messages[msg.receiver]) > 200:
            messages[msg.receiver] = messages[msg.receiver][-150:]
        if msg.sender == "user" and msg.receiver != "user":
            messages.setdefault("user", []).append(entry)
            if len(messages["user"]) > 200:
                messages["user"] = messages["user"][-150:]
        add_activity(msg.sender, msg.receiver, msg.msg_type, msg.content[:80])
        from hub.state import add_audit
        add_audit(msg.sender, "message_send", {"receiver": msg.receiver, "type": msg.msg_type})
    if msg.receiver == "user":
        save_state()
    if msg.sender == "user":
        save_state()
    if msg.msg_type == "blocker":
        send_notification("blocker", f"\U0001f6ab {msg.sender}: {msg.content[:100]}")
    return {"status": "ok"}

@router.post("/messages/{name}/consume")
def consume_one(name: str, match: dict):
    with lock:
        msgs = messages.get(name, [])
        ts, sender = match.get("timestamp", ""), match.get("sender", "")
        messages[name] = [m for m in msgs if not (m.get("timestamp") == ts and m.get("sender") == sender)]
    if name == "user":
        save_state()
    return {"status": "ok"}

@router.post("/messages/{name}/dismiss")
def dismiss_messages(name: str, data: dict):
    with lock:
        if data.get("all"):
            messages[name] = []
        elif data.get("sender"):
            agent = data["sender"]
            messages[name] = [m for m in messages.get(name, [])
                              if not (m.get("sender") == agent or m.get("receiver") == agent)]
        elif data.get("timestamp"):
            messages[name] = [m for m in messages.get(name, []) if m.get("timestamp") != data["timestamp"]]
    if name == "user":
        save_state()
    return {"status": "ok"}

@router.post("/broadcast")
def broadcast(msg: Message):
    sent = []
    with lock:
        if not rate_ok(msg.sender):
            return {"status": "rate_limited"}
        ts = datetime.now().isoformat()
        for a in ALL_AGENTS + ["user"]:
            if a != msg.sender:
                messages.setdefault(a, []).append({**msg.model_dump(), "timestamp": ts})
                sent.append(a)
        add_activity(msg.sender, "all", msg.msg_type, msg.content)
    return {"status": "ok", "sent_to": sent}

# ── Sessions ──
@router.post("/sessions/update")
def update_session(info: SessionInfo):
    with lock:
        sessions[info.agent_name] = info.model_dump()
        save_state()
    return {"status": "ok"}

@router.get("/session/snapshot")
def session_snapshot():
    # Read-only snapshot — no lock needed
    return {
        "version": "latest", "workspace": WORKSPACE,
        "timestamp": datetime.now().isoformat(),
        "tasks": dict(tasks), "usage_log": dict(usage_log), "sessions": dict(sessions),
        "changes": list(changes[-50:]), "analytics": list(analytics_log[-200:]),
        "activity": list(activity)[-200:], "agents": list(ALL_AGENTS),
        "specialization": dict(agent_specialization), "learnings": list(agent_learnings[-100:]),
    }

@router.post("/session/restore")
def session_restore(data: dict):
    with lock:
        if "tasks" in data:
            tasks.update({int(k): v for k, v in data["tasks"].items()})
        if "usage_log" in data:
            usage_log.update(data["usage_log"])
        if "sessions" in data:
            sessions.update(data["sessions"])
        save_state()
    return {"status": "ok"}
