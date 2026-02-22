"""Task management routes: CRUD, auto-assign, dependency graph, queue."""

import re
from datetime import datetime
from fastapi import APIRouter

from hub.state import (
    lock, tasks, agents, pipeline, ALL_AGENTS, TASK_STATES, VALID_TRANSITIONS,
    analytics_log, MAX_ANALYTICS, add_activity, save_state, send_notification,
    messages, bump_version, pending_plans, logger,
    task_comments, task_reviews, HIDDEN_AGENTS, STATUS_MIGRATION, MAX_REWORK_LOOPS,
)

MAX_REWORK_ITERATIONS = 5
_QA_AGENT_HINTS = {"qa", "review", "reviewer", "test", "tester", "quality"}

router = APIRouter(tags=["tasks"])

def _has_dependency_cycle(task_id, deps, all_tasks):
    """DFS cycle detection in dependency graph."""
    def _visit(tid, visited):
        if tid in visited:
            return True
        visited.add(tid)
        t = all_tasks.get(tid, {})
        for dep in t.get("depends_on", []):
            if _visit(dep, visited):
                return True
        visited.discard(tid)
        return False

    # Check if adding these deps would create a cycle
    # Temporarily consider task_id as depending on deps
    for dep in deps:
        visited = {task_id}
        if _visit(dep, visited):
            return True
    return False

@router.post("/tasks")
def create_task(data: dict):
    desc = data.get("description", "")
    if len(desc) > 50000:
        data["description"] = desc[:50000]
    p = data.get("priority", 5)
    try:
        data["priority"] = max(1, min(10, int(p)))
    except (TypeError, ValueError):
        data["priority"] = 5
    with lock:
        tid = max(tasks.keys(), default=0) + 1
        deps = data.get("depends_on", [])
        deps = [d for d in deps if d in tasks]
        if deps:
            for d in list(deps):
                if d == tid:
                    deps.remove(d)
        # Check for circular dependencies
        if deps and _has_dependency_cycle(tid, deps, tasks):
            return {"status": "error", "message": "Circular dependency detected", "code": "CIRCULAR_DEP"}
        raw_status = data.get("status", "to_do")
        # Migrate legacy status values
        raw_status = STATUS_MIGRATION.get(raw_status, raw_status)
        task = {
            "id": tid, "description": data.get("description", ""),
            "assigned_to": data.get("assigned_to", ""),
            "status": raw_status,
            "depends_on": deps,
            "project": data.get("project", ""),
            "branch": data.get("branch", ""),
            "task_external_id": data.get("task_external_id", ""),
            "parent_id": data.get("parent_id", None),
            "priority": data.get("priority", 5),
            "created": datetime.now().isoformat(),
            "started_at": "", "completed_at": "",
            "created_by": data.get("created_by", "user"),
            "review_status": "",  # pending_review, approved, needs_changes, auto_approved
            "reviewer": "",
        }
        creator = task["created_by"]
        # Inherit project/branch/external_id from parent task if not specified
        parent = tasks.get(task["parent_id"]) if task["parent_id"] else None
        if not parent and creator and creator != "user":
            # Find creator's current in-progress task as implicit parent
            for t in tasks.values():
                if (t.get("assigned_to") == creator
                        and t.get("status") == "in_progress"):
                    parent = t
                    break
        if parent:
            if not task["project"] and parent.get("project"):
                task["project"] = parent["project"]
            if not task["branch"] and parent.get("branch"):
                task["branch"] = parent["branch"]
            if not task["task_external_id"] and parent.get("task_external_id"):
                task["task_external_id"] = parent["task_external_id"]
            if not task["parent_id"]:
                task["parent_id"] = parent.get("id")
        tasks[tid] = task
        from hub.state import add_audit
        add_audit(task["created_by"], "task_create", {"task_id": tid, "description": task["description"][:100]})
        add_activity(task["created_by"], task["assigned_to"] or "?", "task_create", task["description"][:100])
        save_state()
    return {"status": "ok", "id": tid}

@router.post("/tasks/{tid}")
def update_task_post(tid: int, data: dict):
    return update_task(tid, data)

@router.put("/tasks/{tid}")
def update_task(tid: int, data: dict):
    with lock:
        if tid not in tasks:
            return {"status": "not_found"}
        old_status = tasks[tid].get("status", "")
        new_status = data.get("status", old_status)
        if new_status != old_status and new_status in TASK_STATES:
            allowed = VALID_TRANSITIONS.get(old_status, set())
            if allowed and new_status not in allowed:
                return {"status": "error", "message": f"Invalid transition: {old_status} \u2192 {new_status}"}
        tasks[tid].update(data)
        # Cycle check on dependency update
        new_deps = data.get("depends_on")
        if new_deps is not None:
            if _has_dependency_cycle(tid, new_deps, tasks):
                # Rollback the dependency update
                tasks[tid]["depends_on"] = []
                return {"status": "error", "message": "Circular dependency detected"}
        new_status = tasks[tid].get("status", "")
        if new_status == "in_progress" and not tasks[tid].get("started_at"):
            tasks[tid]["started_at"] = datetime.now().isoformat()
        if new_status in ("done", "failed", "cancelled") and not tasks[tid].get("completed_at"):
            tasks[tid]["completed_at"] = datetime.now().isoformat()
            analytics_log.append({
                "task_id": tid, "agent": tasks[tid].get("assigned_to", ""),
                "status": new_status, "started": tasks[tid].get("started_at", ""),
                "completed": tasks[tid]["completed_at"],
            })
            if len(analytics_log) > MAX_ANALYTICS:
                del analytics_log[:len(analytics_log) - MAX_ANALYTICS]
            desc = tasks[tid].get("description", "")[:60]
            agent = tasks[tid].get("assigned_to", "?")
            if new_status == "done":
                send_notification("task_done", f"\u2705 #{tid} done by {agent}: {desc}")
            elif new_status == "failed":
                send_notification("task_failed", f"\u274c #{tid} failed ({agent}): {desc}")
                if data.get("detail"):
                    tasks[tid]["error_message"] = data["detail"][:500]
        if old_status != new_status:
            add_activity("system", tasks[tid].get("assigned_to", "?"), "task_update",
                         f"Task #{tid}: {old_status} \u2192 {new_status}")
            from hub.state import add_audit
            add_audit(tasks[tid].get("assigned_to", "system"), "task_update",
                      {"task_id": tid, "old_status": old_status, "new_status": new_status})

        # ── Auto-notification: only on actual status transitions ──
        if new_status == "code_review" and old_status != "code_review":
            _dispatch_code_review(tid)
        elif new_status == "done" and old_status != "done":
            _auto_notify_dependents(tid)
        elif new_status == "failed" and old_status != "failed":
            _auto_notify_blocker(tid)
            _maybe_create_rework_cycle(tid)

        save_state()
    return {"status": "ok"}

def _auto_notify_dependents(completed_tid):
    """When a task completes, check if any dependent tasks are now unblocked and notify their agents."""
    for t in tasks.values():
        deps = t.get("depends_on", [])
        if completed_tid not in deps:
            continue
        # Check if ALL dependencies are now done
        all_done = all(tasks.get(d, {}).get("status") == "done" for d in deps)
        if not all_done:
            continue
        target_agent = t.get("assigned_to", "")
        if not target_agent:
            continue
        desc = t.get("description", "")[:80]
        completed_agent = tasks.get(completed_tid, {}).get("assigned_to", "?")
        # Send notification to the dependent task's agent
        ts = datetime.now().isoformat()
        messages.setdefault(target_agent, []).append({
            "sender": "system", "receiver": target_agent,
            "content": f"Task #{completed_tid} completed by {completed_agent}. "
                       f"Your task #{t['id']} ({desc}) is now unblocked and ready to start.",
            "msg_type": "info", "timestamp": ts,
        })
        # Also notify user
        messages.setdefault("user", []).append({
            "sender": "system", "receiver": "user",
            "content": f"🔗 Task #{t['id']} unblocked → {target_agent} (dependency #{completed_tid} done)",
            "msg_type": "info", "timestamp": ts,
        })
        add_activity("system", target_agent, "task_unblocked",
                     f"Task #{t['id']} unblocked (dep #{completed_tid} done)")


def _auto_notify_blocker(failed_tid):
    """When a task fails, notify agents of dependent tasks about the blocker."""
    failed_task = tasks.get(failed_tid, {})
    failed_agent = failed_task.get("assigned_to", "?")
    failed_desc = failed_task.get("description", "")[:60]
    for t in tasks.values():
        deps = t.get("depends_on", [])
        if failed_tid not in deps:
            continue
        target_agent = t.get("assigned_to", "")
        if not target_agent:
            continue
        ts = datetime.now().isoformat()
        messages.setdefault(target_agent, []).append({
            "sender": "system", "receiver": target_agent,
            "content": f"⚠️ Blocker: Task #{failed_tid} ({failed_desc}) by {failed_agent} has failed. "
                       f"Your task #{t['id']} depends on it and is blocked.",
            "msg_type": "blocker", "timestamp": ts,
        })
        # Mark dependent task as blocked_by_failure
        if t.get("status") in ("to_do", "created", "assigned"):
            t["status"] = "blocked_by_failure"
            t["blocked_reason"] = f"Dependency #{failed_tid} failed"
        # Notify architect to handle the blocker
        if "architect" in ALL_AGENTS and target_agent != "architect":
            messages.setdefault("architect", []).append({
                "sender": "system", "receiver": "architect",
                "content": f"⚠️ Task #{failed_tid} failed ({failed_agent}). "
                           f"Downstream task #{t['id']} assigned to {target_agent} is now blocked. "
                           f"Consider reassigning or investigating.",
                "msg_type": "blocker", "timestamp": ts,
            })


def _dispatch_code_review(tid):
    """Dispatch code review to 3 hidden reviewer agents (logic, style, architecture)."""
    task = tasks.get(tid, {})
    if not task:
        return

    tid_str = str(tid)
    reviewers = ["reviewer-logic", "reviewer-style", "reviewer-arch"]

    # Track rework loop count
    rework_count = task.get("_review_cycle", 0)
    if rework_count >= MAX_REWORK_LOOPS:
        # Auto-approve after max rework cycles
        task["status"] = "in_testing"
        task_reviews[tid_str] = {r: {"verdict": "approve", "comments": [],
                                      "timestamp": datetime.now().isoformat(), "auto": True}
                                  for r in reviewers}
        add_activity("system", task.get("assigned_to", "?"), "review_auto_approved",
                     f"Code review #{tid} auto-approved (max {MAX_REWORK_LOOPS} rework cycles)")
        _dispatch_qa(tid)
        bump_version()
        return

    # Clear previous reviews for re-review
    task_reviews[tid_str] = {}
    task["review_dispatched_at"] = datetime.now().isoformat()

    desc = task.get("description", "")[:200]
    project = task.get("project", "")
    branch = task.get("branch", "")
    author = task.get("assigned_to", "")
    ts = datetime.now().isoformat()

    specializations = {
        "reviewer-logic": "LOGIC correctness: algorithm bugs, edge cases, null handling, race conditions, error propagation",
        "reviewer-style": "CODE STYLE: naming, formatting, readability, DRY, function length, comment quality",
        "reviewer-arch": "ARCHITECTURE: design patterns, separation of concerns, SOLID, scalability, coupling",
    }

    for r in reviewers:
        review_msg = (
            f"CODE REVIEW REQUEST — Task #{tid}\n"
            f"Author: {author} | Project: {project} | Branch: {branch}\n"
            f"Your focus: {specializations[r]}\n\n"
            f"Task description:\n{desc}\n\n"
            f"Review the code changes and respond by calling:\n"
            f"curl -s -X POST $HUB/tasks/{tid}/review -H 'Content-Type: application/json' "
            f"-d '{{\"agent\": \"{r}\", \"verdict\": \"approve\"}}'\n"
            f"OR with issues:\n"
            f"-d '{{\"agent\": \"{r}\", \"verdict\": \"request_changes\", \"comments\": ["
            f"{{\"file\": \"path/file.js\", \"line\": 42, \"text\": \"Issue description\", \"severity\": \"warning\"}}]}}'"
        )
        messages.setdefault(r, []).append({
            "sender": "system", "receiver": r,
            "content": review_msg,
            "msg_type": "task", "task_id": tid_str,
            "timestamp": ts,
        })

    add_activity("system", "code_review", "review_dispatched",
                 f"Code review #{tid} dispatched to {len(reviewers)} reviewers")
    bump_version()


def _dispatch_qa(tid):
    """After all reviewers approve, dispatch task to QA for testing."""
    task = tasks.get(tid, {})
    if not task:
        return
    qa_agent = next((a for a in ALL_AGENTS if _is_qa_agent(a) and a not in HIDDEN_AGENTS), None)
    if not qa_agent:
        # No QA agent → skip to UAT
        task["status"] = "uat"
        ts = datetime.now().isoformat()
        messages.setdefault("user", []).append({
            "sender": "system", "receiver": "user",
            "content": f"Task #{tid} passed code review (3/3 approved). No QA agent found — moved to UAT for your approval.",
            "msg_type": "info", "timestamp": ts,
        })
        bump_version()
        return

    desc = task.get("description", "")[:200]
    project = task.get("project", "")
    branch = task.get("branch", "")
    ts = datetime.now().isoformat()

    qa_msg = (
        f"QA TEST REQUEST — Task #{tid}\n"
        f"Code review passed (3/3 approved). Now verify with tests.\n"
        f"Author: {task.get('assigned_to', '?')} | Project: {project} | Branch: {branch}\n\n"
        f"Task: {desc}\n\n"
        f"Run ALL test suites, linters, and verify the changes. If tests pass, update task status:\n"
        f"curl -s -X PUT $HUB/tasks/{tid} -H 'Content-Type: application/json' -d '{{\"status\": \"uat\"}}'\n"
        f"If tests fail:\n"
        f"curl -s -X PUT $HUB/tasks/{tid} -H 'Content-Type: application/json' -d '{{\"status\": \"failed\", \"detail\": \"Test failures: ...\"}}'"
    )
    messages.setdefault(qa_agent, []).append({
        "sender": "system", "receiver": qa_agent,
        "content": qa_msg,
        "msg_type": "task", "task_id": str(tid),
        "timestamp": ts,
    })
    add_activity("system", qa_agent, "qa_dispatched", f"QA test #{tid} dispatched to {qa_agent}")
    bump_version()


def _is_qa_agent(name):
    """Check if an agent name looks like a QA/review agent."""
    low = name.lower()
    return any(h in low for h in _QA_AGENT_HINTS)


def _maybe_create_rework_cycle(failed_tid):
    """When a QA/review task fails, auto-create rework for dev + re-verify for QA.

    This creates a continuous iteration loop: dev → QA → dev → QA → ...
    until QA passes or MAX_REWORK_ITERATIONS is hit.
    """
    failed_task = tasks.get(failed_tid, {})
    failed_agent = failed_task.get("assigned_to", "")

    # Only trigger for QA/review agents — dev failures don't auto-rework
    if not _is_qa_agent(failed_agent):
        return

    # Find the dev task this QA task was checking (from depends_on)
    deps = failed_task.get("depends_on", [])
    if not deps:
        return  # No dependency chain → can't determine what to rework

    # The first dependency is typically the dev task
    dev_tid = deps[0]
    dev_task = tasks.get(dev_tid, {})
    if not dev_task:
        return

    dev_agent = dev_task.get("assigned_to", "")
    if not dev_agent:
        return

    # Track iteration count
    iteration = failed_task.get("iteration", 1) + 1
    if iteration > MAX_REWORK_ITERATIONS:
        ts = datetime.now().isoformat()
        messages.setdefault("user", []).append({
            "sender": "system", "receiver": "user",
            "content": f"🔄 Max iterations ({MAX_REWORK_ITERATIONS}) reached for task chain "
                       f"#{dev_tid} ↔ #{failed_tid}. Manual intervention needed.",
            "msg_type": "blocker", "timestamp": ts,
        })
        logger.info(f"Rework cycle max iterations reached: #{dev_tid} ↔ #{failed_tid}")
        return

    # Get QA feedback from the failed task
    qa_feedback = failed_task.get("error_message", "") or failed_task.get("detail", "")
    if not qa_feedback:
        qa_feedback = "QA check failed — see QA agent logs for details"

    ts = datetime.now().isoformat()

    # Create rework task for dev
    rework_tid = max(tasks.keys(), default=0) + 1
    original_desc = dev_task.get("description", "")
    rework_desc = (
        f"REWORK (iteration {iteration}/{MAX_REWORK_ITERATIONS}) — "
        f"QA #{failed_tid} found issues:\n\n"
        f"QA FEEDBACK:\n{qa_feedback}\n\n"
        f"ORIGINAL TASK:\n{original_desc}\n\n"
        f"Fix ALL issues reported by QA. Run tests/lint before marking done."
    )
    tasks[rework_tid] = {
        "id": rework_tid,
        "description": rework_desc,
        "assigned_to": dev_agent,
        "status": "to_do",
        "depends_on": [],
        "project": dev_task.get("project", ""),
        "branch": dev_task.get("branch", ""),
        "task_external_id": dev_task.get("task_external_id", ""),
        "parent_id": dev_tid,
        "priority": dev_task.get("priority", 5),
        "created": ts, "started_at": "", "completed_at": "",
        "created_by": "system",
        "iteration": iteration,
        "rework_of": dev_tid,
    }

    # Create re-verify task for QA (depends on rework)
    reverify_tid = rework_tid + 1
    qa_desc = (
        f"RE-VERIFY (iteration {iteration}/{MAX_REWORK_ITERATIONS}) — "
        f"Check that {dev_agent}'s rework #{rework_tid} fixed ALL issues.\n\n"
        f"PREVIOUS QA ISSUES:\n{qa_feedback}\n\n"
        f"ORIGINAL TASK:\n{original_desc[:500]}\n\n"
        f"Run ALL tests, lint, and verify each issue is resolved. "
        f"If ANY issue remains, mark FAILED with detailed feedback."
    )
    tasks[reverify_tid] = {
        "id": reverify_tid,
        "description": qa_desc,
        "assigned_to": failed_agent,
        "status": "to_do",
        "depends_on": [rework_tid],
        "project": dev_task.get("project", ""),
        "branch": dev_task.get("branch", ""),
        "task_external_id": dev_task.get("task_external_id", ""),
        "parent_id": failed_tid,
        "priority": dev_task.get("priority", 5),
        "created": ts, "started_at": "", "completed_at": "",
        "created_by": "system",
        "iteration": iteration,
        "rework_of": failed_tid,
    }

    add_activity("system", dev_agent, "rework_cycle",
                 f"Iteration {iteration}: rework #{rework_tid} → re-verify #{reverify_tid}")

    # Notify user
    messages.setdefault("user", []).append({
        "sender": "system", "receiver": "user",
        "content": f"🔄 Iteration {iteration}: QA #{failed_tid} failed → "
                   f"rework #{rework_tid} ({dev_agent}) → re-verify #{reverify_tid} ({failed_agent})",
        "msg_type": "info", "timestamp": ts,
    })

    logger.info(f"Rework cycle iteration {iteration}: #{rework_tid} ({dev_agent}) → #{reverify_tid} ({failed_agent})")
    bump_version()


@router.post("/tasks/{tid}/check")
def request_check(tid: int, data: dict):
    """Request a dev-check or qa-check for a task."""
    if tid not in tasks:
        return {"status": "not_found"}
    check_type = data.get("check_type", "dev")
    agent = data.get("agent", "qa")
    task = tasks[tid]

    if check_type == "qa":
        check_prompt = f"""QA CHECK REQUEST — Task #{tid}
Task: {task.get('description', '')}
Branch: {task.get('branch', '')}
Agent: {task.get('assigned_to', '')}
Project: {task.get('project', '')}

COMPREHENSIVE QA CHECK INSTRUCTIONS:
1. Git diff analysis: `git diff main...{task.get('branch', 'HEAD')}` — review ALL changes
2. Impact analysis: What files changed? What modules are affected? What could break?
3. Edge case review: List potential edge cases the changes might miss
4. Test coverage: Are there tests for the changes? What's missing?
5. Regression risk: Could this break existing functionality?
6. Security review: Any security concerns (input validation, auth, injection)?
7. Performance: Any performance implications?

OUTPUT FORMAT — Generate a Jira-compatible comment:
### QA Check Report — Task #{tid}
**Status:** PASS / CONCERNS / FAIL

#### Changes Summary
- [list of changed files with brief description]

#### Impact Analysis
- [affected modules/features]

#### Test Coverage
- [existing tests: pass/fail]
- [missing tests needed]

#### Edge Cases & Risks
- [list of edge cases]

#### Security Review
- [any security concerns]

#### Recommendation
[APPROVE / APPROVE WITH NOTES / REQUEST CHANGES]
[detailed recommendation]

After generating the report, send it to user with: curl -s -X POST $HUB/messages -H 'Content-Type: application/json' -d '{{"sender":"{agent}","receiver":"user","content":"REPORT_HERE","msg_type":"check_report"}}'"""
    else:
        check_prompt = f"""DEV CHECK REQUEST — Task #{tid}
Task: {task.get('description', '')}
Branch: {task.get('branch', '')}
Agent: {task.get('assigned_to', '')}
Project: {task.get('project', '')}

COMPREHENSIVE DEV REVIEW INSTRUCTIONS:
1. Code quality: Review all changes for clean code, proper naming, no dead code
2. Architecture: Do changes follow project patterns? Any anti-patterns?
3. Dependencies: Any new dependencies? Are they justified?
4. Error handling: Are errors properly caught and handled?
5. Type safety: Types correct? Missing type annotations?
6. API compatibility: Any breaking changes to public APIs?
7. Build impact: Does it build cleanly? Any warnings?
8. Documentation: Are changes documented where needed?

OUTPUT FORMAT — Generate a Jira-compatible comment:
### Dev Check Report — Task #{tid}
**Status:** PASS / CONCERNS / FAIL

#### Code Quality
- [code quality observations]

#### Architecture Review
- [pattern adherence, concerns]

#### Changed Files
| File | Changes | Risk |
|------|---------|------|
| file | description | Low/Med/High |

#### Issues Found
- Critical: [if any]
- Warning: [if any]
- Info: [if any]

#### Recommendation
[APPROVE / APPROVE WITH NOTES / REQUEST CHANGES]
[detailed recommendation]

After generating the report, send it to user with: curl -s -X POST $HUB/messages -H 'Content-Type: application/json' -d '{{"sender":"{agent}","receiver":"user","content":"REPORT_HERE","msg_type":"check_report"}}'"""

    with lock:
        ts = datetime.now().isoformat()
        messages.setdefault(agent, []).append({
            "sender": "user", "receiver": agent,
            "content": check_prompt,
            "msg_type": "task", "task_id": str(tid),
            "timestamp": ts,
        })
        add_activity("user", agent, f"{check_type}_check", f"Requested {check_type} check on #{tid}")
        bump_version()
    return {"status": "ok", "agent": agent, "check_type": check_type}

@router.get("/tasks")
def get_tasks(status: str = "", assigned_to: str = ""):
    # No lock — read-only, GIL-safe
    result = list(tasks.values())
    if status:
        result = [t for t in result if t.get("status") == status]
    if assigned_to:
        result = [t for t in result if t.get("assigned_to") == assigned_to]
    result.sort(key=lambda t: (t.get("priority", 5), t.get("id", 0)))
    return result

@router.get("/tasks/queue/{name}")
def task_queue(name: str):
    q = [t for t in tasks.values() if t.get("assigned_to") == name and t.get("status") in ("to_do", "created", "assigned")]
    q.sort(key=lambda t: (t.get("priority", 5), t.get("id", 0)))
    return q

# ── Dependency Graph ──
@router.get("/tasks/graph")
def task_dependency_graph():
    # No lock — read-only
    nodes = []
    edges = []
    for t in tasks.values():
        nodes.append({
            "id": t["id"], "label": f"#{t['id']}", "status": t.get("status", "created"),
            "agent": t.get("assigned_to", ""), "desc": t.get("description", "")[:40],
            "priority": t.get("priority", 5),
        })
        for dep in t.get("depends_on", []):
            if dep in tasks:
                edges.append({"from": dep, "to": t["id"]})
    critical = _compute_critical_path(nodes, edges)
    return {"nodes": nodes, "edges": edges, "critical_path": critical}

def _compute_critical_path(nodes, edges):
    node_map = {n["id"]: n for n in nodes}
    children = {}
    for e in edges:
        children.setdefault(e["from"], []).append(e["to"])

    def depth(nid, visited=None):
        if visited is None:
            visited = set()
        if nid in visited:
            return 0
        visited.add(nid)
        n = node_map.get(nid)
        if not n or n["status"] in ("done", "cancelled"):
            return 0
        kids = children.get(nid, [])
        if not kids:
            return 1
        return 1 + max(depth(c, visited) for c in kids)

    roots = set(n["id"] for n in nodes) - set(e["to"] for e in edges)
    if not roots:
        return []
    start = max(roots, key=lambda r: depth(r))
    path = [start]
    visited_path = {start}
    cur = start
    while children.get(cur):
        nxt = max(children[cur], key=lambda c: depth(c))
        if nxt in visited_path:
            break
        if node_map.get(nxt, {}).get("status") in ("done", "cancelled"):
            break
        path.append(nxt)
        visited_path.add(nxt)
        cur = nxt
    return path

@router.get("/tasks/{tid}")
def get_task(tid: int):
    if tid not in tasks:
        return {"status": "not_found"}
    return dict(tasks[tid])

@router.get("/tasks/{tid}/ready")
def task_ready(tid: int):
    if tid not in tasks:
        return {"ready": False, "reason": "not found"}
    deps = tasks[tid].get("depends_on", [])
    for did in deps:
        if did in tasks and tasks[did].get("status") not in ("done",):
            return {"ready": False, "reason": f"waiting on #{did}"}
    return {"ready": True}

# ── Auto-assign ──
@router.post("/tasks/auto-assign/{name}")
def auto_assign_task(name: str):
    with lock:
        # Tasks explicitly assigned to this agent
        candidates = [t for t in tasks.values()
                      if t.get("assigned_to") == name and t.get("status") in ("to_do", "created")]
        # Unassigned tasks — prefer role-matched
        unassigned = [t for t in tasks.values()
                      if not t.get("assigned_to") and t.get("status") in ("to_do", "created")
                      and t.get("created_by") != name]
        # Score unassigned tasks by role match
        from hub.state import ROUTE_MAP, agent_specialization
        agent_spec = agent_specialization.get(name, {})
        for t in unassigned:
            score = 0
            desc_lower = t.get("description", "").lower()
            # Check required_role match
            required = t.get("required_role", "")
            if required and required == name:
                score += 100
            elif required and required != name:
                score -= 50  # Not the right role
            # Check keyword match with agent's routing keywords
            kws = ROUTE_MAP.get(name, [])
            for kw in kws:
                if kw in desc_lower:
                    score += 3
            # Boost by specialization
            if agent_spec.get("score", 0) > 5:
                score += 2
            t["_routing_score"] = score
        # Sort: explicitly assigned first, then by routing score, then priority
        unassigned.sort(key=lambda t: (-t.get("_routing_score", 0), t.get("priority", 5), t.get("id", 0)))
        candidates.sort(key=lambda t: (t.get("priority", 5), t.get("id", 0)))
        candidates += unassigned
        for t in candidates:
            deps = t.get("depends_on", [])
            deps_met = all(tasks.get(d, {}).get("status") == "done" for d in deps)
            if deps_met:
                t["status"] = "in_progress"
                t["assigned_to"] = name
                t["started_at"] = datetime.now().isoformat()
                add_activity("system", name, "task_auto_assign", f"Auto-assigned #{t['id']}")
                t.pop("_routing_score", None)
                save_state()
                return {"status": "ok", "task": t}
        # Clean up routing scores
        for t in candidates:
            t.pop("_routing_score", None)
        return {"status": "none"}

# ── Test Results ──
@router.post("/tests/result")
def submit_test_result(data: dict):
    from hub.state import test_results
    with lock:
        entry = {
            "agent": data.get("agent_name", ""), "project": data.get("project", ""),
            "task_id": data.get("task_id", ""),
            "tests_passed": data.get("tests_passed", 0), "tests_failed": data.get("tests_failed", 0),
            "tests_skipped": data.get("tests_skipped", 0), "lint_errors": data.get("lint_errors", 0),
            "output": data.get("output", "")[:2000],
            "error_output": data.get("error_output", "")[:2000],
            "failed_tests": data.get("failed_tests", [])[:20],
            "command": data.get("command", ""),
            "duration_seconds": data.get("duration_seconds", 0),
            "time": datetime.now().isoformat(),
        }
        test_results.append(entry)
        if len(test_results) > 300:
            del test_results[:100]
        add_activity(entry["agent"], "system", "test_result",
                     f"\u2713{entry['tests_passed']} \u2717{entry['tests_failed']} lint:{entry['lint_errors']}")
        save_state()
    return {"status": "ok"}

@router.get("/tests/results")
def get_test_results(limit: int = 50):
    from hub.state import test_results
    return list(test_results[-limit:])


# ── Task Comments ──
@router.post("/tasks/{tid}/comments")
def add_comment(tid: int, data: dict):
    from hub.state import _comment_counter as _cc
    import hub.state as _st
    tid_str = str(tid)
    if tid not in tasks:
        return {"status": "not_found"}
    agent = data.get("agent", "user")
    text = data.get("text", "").strip()
    if not text:
        return {"status": "error", "message": "text required"}
    with lock:
        _st._comment_counter += 1
        cid = _st._comment_counter
        comment = {
            "id": cid, "agent": agent, "text": text[:2000],
            "timestamp": datetime.now().isoformat(), "resolved": False,
        }
        task_comments.setdefault(tid_str, []).append(comment)
        bump_version()
        save_state()
    return {"status": "ok", "id": cid}


@router.get("/tasks/{tid}/comments")
def get_comments(tid: int):
    return task_comments.get(str(tid), [])


@router.post("/tasks/{tid}/comments/{cid}/resolve")
def resolve_comment(tid: int, cid: int):
    tid_str = str(tid)
    comments = task_comments.get(tid_str, [])
    for c in comments:
        if c.get("id") == cid:
            c["resolved"] = True
            bump_version()
            save_state()
            return {"status": "ok"}
    return {"status": "not_found"}


# ── Code Review Verdict ──
@router.post("/tasks/{tid}/review")
def submit_review(tid: int, data: dict):
    """Submit a reviewer verdict: approve or request_changes."""
    tid_str = str(tid)
    if tid not in tasks:
        return {"status": "not_found"}
    agent = data.get("agent", "")
    verdict = data.get("verdict", "")
    if verdict not in ("approve", "request_changes"):
        return {"status": "error", "message": "verdict must be 'approve' or 'request_changes'"}
    if not agent:
        return {"status": "error", "message": "agent required"}

    review_comments = data.get("comments", [])
    with lock:
        # Store verdict
        task_reviews.setdefault(tid_str, {})[agent] = {
            "verdict": verdict,
            "comments": review_comments[:20],
            "timestamp": datetime.now().isoformat(),
        }
        # Add review comments to task_comments
        import hub.state as _st
        for rc in review_comments[:20]:
            _st._comment_counter += 1
            task_comments.setdefault(tid_str, []).append({
                "id": _st._comment_counter,
                "agent": agent,
                "text": f"[{rc.get('severity', 'info')}] {rc.get('file', '')}:{rc.get('line', '')} — {rc.get('text', '')}",
                "timestamp": datetime.now().isoformat(),
                "resolved": False,
            })

        add_activity(agent, "code_review", "review_verdict",
                     f"#{tid} {verdict} by {agent}" + (f" ({len(review_comments)} comments)" if review_comments else ""))

        # Check if all 3 reviewers have responded
        reviewers = ["reviewer-logic", "reviewer-style", "reviewer-arch"]
        reviews = task_reviews.get(tid_str, {})
        all_responded = all(r in reviews for r in reviewers)

        if all_responded:
            all_approved = all(reviews[r].get("verdict") == "approve" for r in reviewers)
            if all_approved:
                # 3/3 approved → advance to in_testing
                tasks[tid]["status"] = "in_testing"
                tasks[tid].pop("review_dispatched_at", None)
                add_activity("system", tasks[tid].get("assigned_to", "?"), "review_approved",
                             f"Code review #{tid} passed (3/3 approved)")
                # Notify user
                ts = datetime.now().isoformat()
                messages.setdefault("user", []).append({
                    "sender": "system", "receiver": "user",
                    "content": f"✅ Task #{tid} code review passed (3/3 approved). Moving to QA testing.",
                    "msg_type": "info", "timestamp": ts,
                })
                _dispatch_qa(tid)
            else:
                # At least one request_changes → send back to dev
                tasks[tid]["status"] = "in_progress"
                tasks[tid].pop("review_dispatched_at", None)
                tasks[tid]["_review_cycle"] = tasks[tid].get("_review_cycle", 0) + 1
                dev_agent = tasks[tid].get("assigned_to", "")
                # Collect all change requests
                all_issues = []
                for r in reviewers:
                    rv = reviews.get(r, {})
                    if rv.get("verdict") == "request_changes":
                        for c in rv.get("comments", []):
                            all_issues.append(f"[{r}] {c.get('file', '')}:{c.get('line', '')} — {c.get('text', '')}")
                feedback = "\n".join(all_issues) if all_issues else "Reviewers requested changes. Check review comments."
                ts = datetime.now().isoformat()
                if dev_agent:
                    messages.setdefault(dev_agent, []).append({
                        "sender": "system", "receiver": dev_agent,
                        "content": f"CODE REVIEW FEEDBACK — Task #{tid}\n"
                                   f"Reviewers requested changes (cycle {tasks[tid].get('_review_cycle', 1)}/{MAX_REWORK_LOOPS}).\n\n"
                                   f"ISSUES:\n{feedback}\n\n"
                                   f"Fix all issues and move task back to code_review:\n"
                                   f"curl -s -X PUT $HUB/tasks/{tid} -H 'Content-Type: application/json' -d '{{\"status\": \"code_review\"}}'",
                        "msg_type": "task", "task_id": tid_str,
                        "timestamp": ts,
                    })
                messages.setdefault("user", []).append({
                    "sender": "system", "receiver": "user",
                    "content": f"🔄 Task #{tid} needs changes. Sent back to {dev_agent} (cycle {tasks[tid].get('_review_cycle', 1)}/{MAX_REWORK_LOOPS}).",
                    "msg_type": "info", "timestamp": ts,
                })
                add_activity("system", dev_agent, "review_changes_requested",
                             f"Review #{tid} needs changes (cycle {tasks[tid].get('_review_cycle', 1)})")

        bump_version()
        save_state()
    return {"status": "ok", "all_responded": all_responded}


# ── UAT (User Acceptance Testing) ──
@router.post("/tasks/{tid}/uat")
def uat_decision(tid: int, data: dict):
    """User approves or rejects a task in UAT status."""
    if tid not in tasks:
        return {"status": "not_found"}
    action = data.get("action", "")
    if action not in ("approve", "reject"):
        return {"status": "error", "message": "action must be 'approve' or 'reject'"}
    feedback = data.get("feedback", "").strip()

    with lock:
        task = tasks[tid]
        if task.get("status") != "uat":
            return {"status": "error", "message": f"Task is not in UAT status (current: {task.get('status')})"}

        ts = datetime.now().isoformat()
        if action == "approve":
            task["status"] = "done"
            task["completed_at"] = ts
            analytics_log.append({
                "task_id": tid, "agent": task.get("assigned_to", ""),
                "status": "done", "started": task.get("started_at", ""),
                "completed": ts,
            })
            add_activity("user", task.get("assigned_to", "?"), "uat_approved",
                         f"UAT #{tid} approved by user")
            send_notification("task_done", f"✅ #{tid} done (UAT approved): {task.get('description', '')[:60]}")
            _auto_notify_dependents(tid)
        else:
            task["status"] = "in_progress"
            task["_review_cycle"] = 0  # Reset review cycle on UAT reject
            dev_agent = task.get("assigned_to", "")
            if dev_agent:
                messages.setdefault(dev_agent, []).append({
                    "sender": "user", "receiver": dev_agent,
                    "content": f"UAT REJECTED — Task #{tid}\n"
                               f"User feedback: {feedback or 'No specific feedback provided.'}\n\n"
                               f"Please address the feedback and move task back to code_review when done.",
                    "msg_type": "task", "task_id": str(tid),
                    "timestamp": ts,
                })
            add_activity("user", dev_agent or "?", "uat_rejected",
                         f"UAT #{tid} rejected: {feedback[:80]}")

        bump_version()
        save_state()
    return {"status": "ok", "new_status": tasks[tid]["status"]}


# ── Plan Approval ──
@router.post("/plan/approve")
def approve_plan(data: dict):
    """Approve a plan proposal — create tasks from selected steps."""
    plan_id = data.get("plan_id")
    selected_steps = data.get("selected_steps", [])  # list of step indices
    if plan_id is None:
        return {"status": "error", "message": "plan_id required"}
    plan_id = int(plan_id)
    if plan_id not in pending_plans:
        return {"status": "error", "message": "plan not found"}
    plan = pending_plans[plan_id]
    if plan.get("status") != "pending":
        return {"status": "error", "message": f"plan already {plan.get('status')}"}

    steps = plan.get("steps", [])
    selected = set(selected_steps)
    if not selected:
        return {"status": "error", "message": "no steps selected"}

    created_tasks = []
    # Map step index → task ID for dependency resolution
    step_to_tid = {}

    with lock:
        for idx in sorted(selected):
            if idx < 0 or idx >= len(steps):
                continue
            step = steps[idx]
            # Resolve depends_on_step → real task IDs (skip if dep was not selected)
            deps = []
            dep_step = step.get("depends_on_step")
            if dep_step is not None:
                if isinstance(dep_step, list):
                    for ds in dep_step:
                        if ds in step_to_tid:
                            deps.append(step_to_tid[ds])
                elif dep_step in step_to_tid:
                    deps.append(step_to_tid[dep_step])

            tid = max(tasks.keys(), default=0) + 1
            task = {
                "id": tid,
                "description": step.get("description", ""),
                "assigned_to": step.get("assigned_to", ""),
                "status": "to_do",
                "depends_on": deps,
                "project": plan.get("project", ""),
                "branch": plan.get("branch", ""),
                "task_external_id": step.get("task_external_id", ""),
                "parent_id": None,
                "priority": step.get("priority", 5),
                "created": datetime.now().isoformat(),
                "started_at": "", "completed_at": "",
                "created_by": plan.get("created_by", "architect"),
                "plan_id": plan_id,
            }
            tasks[tid] = task
            step_to_tid[idx] = tid
            created_tasks.append({"step_index": idx, "task_id": tid})
            add_activity(plan.get("created_by", "architect"), task["assigned_to"] or "?",
                         "task_create", task["description"][:100])

        plan["status"] = "approved"
        plan["approved_at"] = datetime.now().isoformat()
        plan["created_task_ids"] = [ct["task_id"] for ct in created_tasks]

        # Notify user
        ts = datetime.now().isoformat()
        messages.setdefault("user", []).append({
            "sender": "system", "receiver": "user",
            "content": f"Plan #{plan_id} approved — {len(created_tasks)} task(s) created.",
            "msg_type": "info", "timestamp": ts,
        })

        # Auto-start: send task message to agents whose deps are already met
        for ct in created_tasks:
            task = tasks[ct["task_id"]]
            agent = task.get("assigned_to", "")
            if not agent:
                continue
            deps = task.get("depends_on", [])
            deps_met = all(tasks.get(d, {}).get("status") == "done" for d in deps)
            if deps_met:
                messages.setdefault(agent, []).append({
                    "sender": plan.get("created_by", "architect"),
                    "receiver": agent,
                    "content": f"#{ct['task_id']} {task['description']}",
                    "msg_type": "task",
                    "task_id": str(ct["task_id"]),
                    "project": task.get("project", ""),
                    "branch": task.get("branch", ""),
                    "task_external_id": task.get("task_external_id", ""),
                    "timestamp": ts,
                })

        bump_version()
        save_state()

    return {"status": "ok", "tasks_created": created_tasks}


@router.post("/plan/dismiss")
def dismiss_plan(data: dict):
    """Dismiss a plan proposal without creating tasks."""
    plan_id = data.get("plan_id")
    if plan_id is None:
        return {"status": "error", "message": "plan_id required"}
    plan_id = int(plan_id)
    if plan_id not in pending_plans:
        return {"status": "error", "message": "plan not found"}
    with lock:
        pending_plans[plan_id]["status"] = "dismissed"
        bump_version()
        save_state()
    return {"status": "ok"}
