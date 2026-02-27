"""Message routes: send, consume, dismiss, broadcast, chat, sessions."""

import logging
from datetime import datetime
from fastapi import APIRouter

from hub.state import (
    lock, messages, chat_queue, pipeline, ALL_AGENTS, sessions,
    tasks, usage_log, changes, activity, analytics_log, agent_specialization,
    agent_learnings, pending_plans, Message, SessionInfo,
    rate_ok, add_activity, save_state, send_notification, WORKSPACE,
    bump_version,
)

_log = logging.getLogger("hub.messages")

router = APIRouter(tags=["messages"])

# Dedup: recent messages keyed by (content_hash, msg_type, receiver) → {senders, timestamp, entry}
_recent_msgs = {}  # key → {"senders": set, "ts": float, "entry": dict}
_DEDUP_WINDOW = 60  # seconds

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

    # Flags for post-lock auto-plan approval (avoid deadlock with approve_plan)
    _should_auto_approve = False
    _auto_plan_id = None
    _auto_plan_steps = []

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
                "task_id": entry.get("task_id", ""),
            }
            entry["plan_id"] = plan_id
            # Route to user inbox (always, so user sees what happened)
            entry["receiver"] = "user"
            messages.setdefault("user", []).append(entry)
            if len(messages["user"]) > 200:
                messages["user"] = messages["user"][-150:]

            # Check auto-plan approval config
            auto_all = _st._cfg.get("auto_plan_approval", False)
            auto_single = _st._cfg.get("auto_plan_single_step", True)
            _should_auto_approve = auto_all or (auto_single and len(plan_steps) == 1)

            add_activity(msg.sender, "user", "plan_proposal", msg.content[:80])
            bump_version()
            save_state()
            if _should_auto_approve and plan_steps:
                # Set flags for post-lock auto-approval (fall through out of lock)
                _auto_plan_id = plan_id
                _auto_plan_steps = list(range(len(plan_steps)))
            else:
                return {"status": "ok", "plan_id": plan_id}
        else:
            # ── Normal (non-plan) message handling ──
            if msg.msg_type == "chat" and msg.sender == "user":
                p = pipeline.get(msg.receiver, {})
                if p.get("status") in ("working", "booting"):
                    chat_queue.setdefault(msg.receiver, []).append(entry)
                    messages.setdefault("user", []).append(entry)
                    add_activity(msg.sender, msg.receiver, "chat", msg.content[:80])
                    return {"status": "ok", "queued": "chat"}

            # ── Dedup: merge identical messages from multiple agents within window ──
            import hashlib as _hl
            import time as _time
            _now = _time.time()
            # Prune stale dedup entries
            stale = [k for k, v in _recent_msgs.items() if _now - v["ts"] > _DEDUP_WINDOW]
            for k in stale:
                del _recent_msgs[k]

            _dedup_key = None
            _is_dup = False
            # Only dedup agent→user messages (not user→agent, not task/chat)
            if msg.sender != "user" and msg.receiver == "user" and msg.msg_type not in ("chat", "task", "plan_proposal"):
                content_hash = _hl.md5((msg.content[:500] + "|" + (msg.msg_type or "")).encode()).hexdigest()[:12]
                _dedup_key = (content_hash, msg.msg_type, msg.receiver)
                if _dedup_key in _recent_msgs:
                    prev = _recent_msgs[_dedup_key]
                    prev["senders"].add(msg.sender)
                    # Update the existing message in the queue to show all senders
                    prev["entry"]["sender"] = ", ".join(sorted(prev["senders"]))
                    _is_dup = True
                else:
                    _recent_msgs[_dedup_key] = {"senders": {msg.sender}, "ts": _now, "entry": entry}

            if not _is_dup:
                messages.setdefault(msg.receiver, []).append(entry)
                if len(messages[msg.receiver]) > 200:
                    messages[msg.receiver] = messages[msg.receiver][-150:]
            if msg.sender == "user" and msg.receiver != "user":
                messages.setdefault("user", []).append(entry)
                if len(messages["user"]) > 200:
                    messages["user"] = messages["user"][-150:]
            add_activity(msg.sender, msg.receiver, msg.msg_type, msg.content[:80])
            from hub.state import add_audit
            add_audit(msg.sender, "message_send", {"receiver": msg.receiver, "type": msg.msg_type})

    # ── Auto-approve plan (outside lock to avoid deadlock with approve_plan) ──
    if _should_auto_approve and _auto_plan_id is not None:
        try:
            from hub.routers.tasks import approve_plan
            result = approve_plan({
                "plan_id": _auto_plan_id,
                "selected_steps": _auto_plan_steps,
            })
            _log.info("Auto-approved plan #%d: %s", _auto_plan_id, result)
            return {"status": "ok", "plan_id": _auto_plan_id, "auto_approved": True}
        except Exception as exc:
            _log.warning("Auto-approve plan #%d failed: %s", _auto_plan_id, exc)
        return {"status": "ok", "plan_id": _auto_plan_id}

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
