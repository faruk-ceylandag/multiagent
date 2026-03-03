"""Task management routes: CRUD, auto-assign, dependency graph, queue."""

import re
from datetime import datetime
from fastapi import APIRouter

from hub.state import (
    lock, tasks, agents, pipeline, ALL_AGENTS, TASK_STATES, VALID_TRANSITIONS,
    analytics_log, MAX_ANALYTICS, add_activity, save_state, send_notification,
    messages, bump_version, pending_plans, logger, file_locks,
    task_comments, task_reviews, HIDDEN_AGENTS, STATUS_MIGRATION, MAX_REWORK_LOOPS,
    MAX_QA_REWORK_LOOPS, _cfg,
)

MAX_REWORK_ITERATIONS = MAX_QA_REWORK_LOOPS  # Alias for backward compat
MAX_TOTAL_REWORK_CYCLES = 5  # Combined cap across code_review + QA + UAT rework cycles
_QA_AGENT_HINTS = {"qa", "test", "tester", "quality"}

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
            "workspace": data.get("workspace", ""),
            "task_external_id": data.get("task_external_id", ""),
            "parent_id": data.get("parent_id", None),
            "priority": data.get("priority", 5),
            "created": datetime.now().isoformat(),
            "started_at": "", "completed_at": "",
            "created_by": data.get("created_by", "user"),
            "review_status": "",  # pending_review, approved, needs_changes, auto_approved
            "reviewer": "",
            "skip_review": bool(data.get("skip_review", False)),
            "skip_qa": bool(data.get("skip_qa", False)),
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
        save_state(force=True)  # H2: Force save on task creation to prevent data loss
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
            allowed = VALID_TRANSITIONS.get(old_status)
            if allowed is not None and new_status not in allowed:
                return {"status": "error", "message": f"Invalid transition: {old_status} \u2192 {new_status}"}
        # Validate dependencies BEFORE applying any changes
        new_deps = data.get("depends_on")
        if new_deps is not None:
            if _has_dependency_cycle(tid, new_deps, tasks):
                return {"status": "error", "message": "Circular dependency detected"}

        # Intercept QA failure: in_testing → failed → redirect to in_progress (same task rework)
        if old_status == "in_testing" and new_status == "failed":
            detail = data.get("detail", "")
            if _handle_qa_failure(tid, old_status, detail=detail):
                # Task was redirected to in_progress — apply non-status fields from data
                non_status = {k: v for k, v in data.items() if k != "status"}
                if non_status:
                    tasks[tid].update(non_status)
                add_activity("system", tasks[tid].get("assigned_to", "?"), "task_update",
                             f"Task #{tid}: {old_status} → in_progress (QA rework)")
                save_state()
                return {"status": "ok"}

        tasks[tid].update(data)
        new_status = tasks[tid].get("status", "")

        # Track phase transitions for timeline
        if new_status and new_status != old_status:
            if "_transitions" not in tasks[tid]:
                tasks[tid]["_transitions"] = []
            if len(tasks[tid]["_transitions"]) < 20:
                tasks[tid]["_transitions"].append({
                    "from": old_status, "to": new_status,
                    "at": datetime.now().isoformat()
                })

        if new_status == "in_progress" and not tasks[tid].get("started_at"):
            tasks[tid]["started_at"] = datetime.now().isoformat()
        if new_status == "in_progress" and not tasks[tid].get("_base_commit"):
            project = tasks[tid].get("project", "")
            if project:
                from hub.state import git_cmd, safe_project_dir
                proj_dir = safe_project_dir(project)
                if proj_dir:
                    _, head = git_cmd(["rev-parse", "HEAD"], cwd=proj_dir)
                    if head:
                        tasks[tid]["_base_commit"] = head.strip()
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

        # Calculate elapsed_seconds when task reaches a terminal state
        if new_status in ("done", "failed") and old_status != new_status:
            started = tasks[tid].get("started_at", "")
            if started:
                try:
                    started_dt = datetime.fromisoformat(started)
                    elapsed = (datetime.now() - started_dt).total_seconds()
                    tasks[tid]["elapsed_seconds"] = round(elapsed, 1)
                except (ValueError, TypeError):
                    pass

        # Release file locks held by this task when it reaches a terminal state
        if new_status in ("done", "failed", "cancelled") and old_status != new_status:
            for path in list(file_locks.keys()):
                if file_locks[path].get("task_id") == tid:
                    del file_locks[path]

        # Cancel active review/QA subtasks and clean up when parent is cancelled/failed
        if new_status in ("cancelled", "failed") and old_status != new_status:
            for sub_id in tasks[tid].get("_review_subtask_ids", []):
                sub = tasks.get(sub_id)
                if sub and sub.get("status") not in ("done", "failed", "cancelled"):
                    sub["status"] = "cancelled"
                    sub["completed_at"] = datetime.now().isoformat()
            # Also cancel QA subtask
            qa_sub_id = tasks[tid].get("_qa_subtask_id")
            if qa_sub_id:
                qa_sub = tasks.get(qa_sub_id)
                if qa_sub and qa_sub.get("status") not in ("done", "failed", "cancelled"):
                    qa_sub["status"] = "cancelled"
                    qa_sub["completed_at"] = datetime.now().isoformat()
            # Immediately clean comments/reviews for cancelled tasks
            if new_status == "cancelled":
                tid_str = str(tid)
                task_comments.pop(tid_str, None)
                task_reviews.pop(tid_str, None)

        # ── Auto-notification: only on actual status transitions ──
        if new_status == "code_review" and old_status != "code_review":
            if tasks[tid].get("skip_review"):
                # Skip code review → go to in_testing (or uat if skip_qa too)
                if tasks[tid].get("skip_qa"):
                    if _cfg.get("auto_uat", False):
                        tasks[tid]["status"] = "done"
                        tasks[tid]["completed_at"] = datetime.now().isoformat()
                        ts = datetime.now().isoformat()
                        messages.setdefault("user", []).append({
                            "sender": "system", "receiver": "user",
                            "content": f"Task #{tid} skipped review+QA → auto-UAT approved → done.",
                            "msg_type": "info", "timestamp": ts,
                        })
                        add_activity("system", tasks[tid].get("assigned_to", "?"), "auto_uat",
                                     f"Task #{tid} auto-UAT approved (skip_review+skip_qa)")
                        _auto_notify_dependents(tid)
                    else:
                        tasks[tid]["status"] = "uat"
                        tasks[tid]["_uat_entered_at"] = datetime.now().isoformat()
                        ts = datetime.now().isoformat()
                        messages.setdefault("user", []).append({
                            "sender": "system", "receiver": "user",
                            "content": f"Task #{tid} skipped review+QA → moved to UAT.",
                            "msg_type": "info", "timestamp": ts,
                        })
                else:
                    tasks[tid]["status"] = "in_testing"
                    tasks[tid]["_testing_started_at"] = datetime.now().isoformat()
                    _dispatch_qa(tid)
                add_activity("system", tasks[tid].get("assigned_to", "?"), "review_skipped",
                             f"Code review #{tid} skipped (skip_review flag)")
                bump_version()
            else:
                _dispatch_code_review(tid)
        elif new_status == "done" and old_status != "done":
            _auto_notify_dependents(tid)
            _check_plan_parent_completion(tid)
            # QA subtask done → auto-transition parent to uat/done
            if tasks[tid].get("_is_qa_subtask"):
                parent_tid = tasks[tid].get("_review_parent_id")
                parent = tasks.get(parent_tid)
                if parent and parent.get("status") == "in_testing":
                    if _cfg.get("auto_uat", False):
                        parent["status"] = "done"
                        parent["completed_at"] = datetime.now().isoformat()
                        analytics_log.append({
                            "task_id": parent_tid, "agent": parent.get("assigned_to", ""),
                            "status": "done", "started": parent.get("started_at", ""),
                            "completed": parent["completed_at"],
                        })
                        messages.setdefault("user", []).append({
                            "sender": "system", "receiver": "user",
                            "content": f"✅ Task #{parent_tid} QA passed → auto-UAT approved → done.",
                            "msg_type": "info", "timestamp": datetime.now().isoformat(),
                        })
                        add_activity("system", parent.get("assigned_to", "?"), "auto_uat",
                                     f"Task #{parent_tid} auto-UAT approved (QA subtask #{tid} done)")
                        _auto_notify_dependents(parent_tid)
                    else:
                        parent["status"] = "uat"
                        parent["_uat_entered_at"] = datetime.now().isoformat()
                        messages.setdefault("user", []).append({
                            "sender": "system", "receiver": "user",
                            "content": f"✅ Task #{parent_tid} QA passed (subtask #{tid} done). Moved to UAT.",
                            "msg_type": "info", "timestamp": datetime.now().isoformat(),
                        })
                    bump_version()
        elif new_status == "failed" and old_status != "failed":
            _auto_notify_blocker(tid)
            # QA subtask failed → trigger parent rework cycle
            if tasks[tid].get("_is_qa_subtask"):
                parent_tid = tasks[tid].get("_review_parent_id")
                parent = tasks.get(parent_tid)
                if parent and parent.get("status") == "in_testing":
                    detail = tasks[tid].get("error_message", "") or data.get("detail", "")
                    _handle_qa_failure(parent_tid, "in_testing", detail=detail)
            # Failure escalation
            _failure_count = tasks[tid].get("_failure_count", 0) + 1
            tasks[tid]["_failure_count"] = _failure_count
            threshold = _cfg.get("escalation_threshold", 3)
            if threshold > 0 and _failure_count >= threshold and "architect" in ALL_AGENTS:
                ts = datetime.now().isoformat()
                desc = tasks[tid].get("description", "")[:200]
                agent = tasks[tid].get("assigned_to", "?")
                messages.setdefault("architect", []).append({
                    "sender": "system", "receiver": "architect",
                    "content": f"ESCALATION — Task #{tid} failed {_failure_count} times\n"
                               f"Assigned to: {agent}\n"
                               f"Description: {desc}\n\n"
                               f"This task has failed {_failure_count} times (threshold: {threshold}). "
                               f"Please analyze the problem and propose a different approach.",
                    "msg_type": "task", "task_id": str(tid),
                    "timestamp": ts,
                })
                messages.setdefault("user", []).append({
                    "sender": "system", "receiver": "user",
                    "content": f"⚠️ Task #{tid} failed {_failure_count} times — escalated to architect.",
                    "msg_type": "blocker", "timestamp": ts,
                })
                add_activity("system", "architect", "escalation",
                             f"Task #{tid} escalated after {_failure_count} failures")

        # Track UAT entry time for timeout feature
        if new_status == "uat" and old_status != "uat":
            tasks[tid]["_uat_entered_at"] = datetime.now().isoformat()

        # H2: Force save on terminal status changes to prevent data loss on crash
        _is_terminal = new_status in ("done", "failed", "cancelled") and old_status != new_status
        save_state(force=_is_terminal)
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


def _check_plan_parent_completion(completed_tid):
    """When a plan subtask completes, check if all siblings are done → mark parent done."""
    for t in tasks.values():
        subtask_ids = t.get("_plan_subtask_ids")
        if not subtask_ids or completed_tid not in subtask_ids:
            continue
        # Check if ALL plan subtasks are done
        if all(tasks.get(sid, {}).get("status") == "done" for sid in subtask_ids):
            t["status"] = "done"
            t["completed_at"] = datetime.now().isoformat()
            add_activity("system", t.get("assigned_to", "?"), "task_update",
                         f"Task #{t['id']} auto-completed (all plan subtasks done)")
            bump_version()
        break  # A subtask belongs to at most one parent


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
    """Dispatch code review by creating 3 reviewer subtasks (persistent, tracked)."""
    task = tasks.get(tid, {})
    if not task:
        return

    tid_str = str(tid)
    reviewers = [r for r in ["reviewer-logic", "reviewer-style", "reviewer-arch"] if r in ALL_AGENTS]
    if not reviewers:
        # No reviewer agents available → auto-approve and skip to testing
        task["status"] = "in_testing"
        task["_testing_started_at"] = datetime.now().isoformat()
        add_activity("system", task.get("assigned_to", "?"), "review_auto_approved",
                     f"Code review #{tid} auto-approved (no reviewer agents)")
        _dispatch_qa(tid)
        bump_version()
        return

    # Track rework loop count (per-phase and total)
    rework_count = task.get("_review_cycle", 0)
    total_rework = task.get("_total_rework_cycles", 0)
    if rework_count >= MAX_REWORK_LOOPS or total_rework >= MAX_TOTAL_REWORK_CYCLES:
        # Auto-approve after max rework cycles (per-phase or total cap)
        reason = (f"total {total_rework}/{MAX_TOTAL_REWORK_CYCLES} rework cycles"
                  if total_rework >= MAX_TOTAL_REWORK_CYCLES
                  else f"max {MAX_REWORK_LOOPS} review rework cycles")
        task["status"] = "in_testing"
        task["_testing_started_at"] = datetime.now().isoformat()
        task_reviews[tid_str] = {r: {"verdict": "approve", "comments": [],
                                      "timestamp": datetime.now().isoformat(), "auto": True}
                                  for r in reviewers}
        add_activity("system", task.get("assigned_to", "?"), "review_auto_approved",
                     f"Code review #{tid} auto-approved ({reason})")
        logger.info(f"Auto-approved review #{tid}: {reason}")
        _dispatch_qa(tid)
        bump_version()
        return

    # Cancel old review subtasks from previous rework cycles
    old_subtask_ids = task.get("_review_subtask_ids", [])
    for old_sid in old_subtask_ids:
        old_sub = tasks.get(old_sid)
        if old_sub and old_sub.get("status") not in ("done", "failed", "cancelled"):
            old_sub["status"] = "cancelled"
            old_sub["completed_at"] = datetime.now().isoformat()

    # Clear previous reviews for re-review
    task_reviews[tid_str] = {}
    task["review_dispatched_at"] = datetime.now().isoformat()

    desc = task.get("description", "")[:200]
    project = task.get("project", "")
    branch = task.get("branch", "")
    author = task.get("assigned_to", "")
    ts = datetime.now().isoformat()

    # Collect git diff so reviewers can see actual code changes
    diff_text = ""
    if project and branch:
        from hub.state import git_cmd, safe_project_dir
        proj_dir = safe_project_dir(project)
        if proj_dir:
            base = task.get("_base_commit") or "main"
            _, raw_diff = git_cmd(["diff", f"{base}...{branch}", "--stat"], cwd=proj_dir)
            _, detailed = git_cmd(["diff", f"{base}...{branch}"], cwd=proj_dir)
            if detailed:
                diff_text = f"\n\nDIFF SUMMARY:\n{raw_diff}\n\nFULL DIFF:\n{detailed[:8000]}"
            else:
                # Branch might not exist yet or no diff — try uncommitted
                _, raw_diff = git_cmd(["diff", "--stat"], cwd=proj_dir)
                _, detailed = git_cmd(["diff"], cwd=proj_dir)
                if detailed:
                    diff_text = f"\n\nUNCOMMITTED CHANGES:\n{raw_diff}\n\nFULL DIFF:\n{detailed[:8000]}"

    specializations = {
        "reviewer-logic": "LOGIC correctness: algorithm bugs, edge cases, null handling, race conditions, error propagation",
        "reviewer-style": "CODE STYLE: naming, formatting, readability, DRY, function length, comment quality",
        "reviewer-arch": "ARCHITECTURE: design patterns, separation of concerns, SOLID, scalability, coupling",
    }

    # Create 3 reviewer subtasks
    subtask_ids = []
    for r in reviewers:
        sub_tid = max(tasks.keys(), default=0) + 1
        review_desc = (
            f"CODE REVIEW REQUEST — Task #{tid}\n"
            f"Author: {author} | Project: {project} | Branch: {branch}\n"
            f"Your focus: {specializations[r]}\n\n"
            f"Task description:\n{desc}{diff_text}\n\n"
            f"After reviewing, respond with your verdict in this EXACT format at the END of your response:\n"
            f"VERDICT: approve\n"
            f"or\n"
            f"VERDICT: request_changes\n"
            f"COMMENTS:\n"
            f"- file.py:42 | warning | Issue description here\n"
            f"- other.py:10 | error | Another issue"
        )
        tasks[sub_tid] = {
            "id": sub_tid,
            "description": review_desc,
            "assigned_to": r,
            "status": "to_do",
            "depends_on": [],
            "project": project,
            "branch": branch,
            "task_external_id": task.get("task_external_id", ""),
            "parent_id": tid,
            "priority": task.get("priority", 5),
            "created": ts, "started_at": "", "completed_at": "",
            "created_by": "system",
            "_is_review_subtask": True,
            "_review_parent_id": tid,
        }
        subtask_ids.append(sub_tid)

        # Also send message for fast pickup (agent polls messages more frequently)
        messages.setdefault(r, []).append({
            "sender": "system", "receiver": r,
            "content": review_desc,
            "msg_type": "task", "task_id": str(sub_tid),
            "_review_parent_id": tid,
            "timestamp": ts,
        })

    # Store subtask IDs on parent for tracking
    task["_review_subtask_ids"] = subtask_ids

    add_activity("system", "code_review", "review_dispatched",
                 f"Code review #{tid} dispatched to {len(reviewers)} reviewers (subtasks: {subtask_ids})")
    bump_version()


def _dispatch_qa(tid):
    """After all reviewers approve, dispatch task to QA for testing."""
    task = tasks.get(tid, {})
    if not task:
        return
    # Find a QA agent that is both configured AND registered (online/idle)
    qa_agent = None
    for a in ALL_AGENTS:
        if _is_qa_agent(a) and a not in HIDDEN_AGENTS:
            agent_info = agents.get(a, {})
            agent_status = agent_info.get("status", "")
            if agent_info and agent_status not in ("offline", ""):
                qa_agent = a
                break
    if not qa_agent:
        # Check if there's a configured QA agent that just isn't registered yet
        configured_qa = next((a for a in ALL_AGENTS if _is_qa_agent(a) and a not in HIDDEN_AGENTS), None)
        if configured_qa:
            logger.warning(f"QA agent '{configured_qa}' is configured but not registered/online — skipping QA for task #{tid}")
        else:
            logger.warning(f"No QA agent configured — skipping QA for task #{tid}")
    if not qa_agent:
        # No QA agent → skip to UAT (or done if auto_uat)
        if _cfg.get("auto_uat", False):
            task["status"] = "done"
            task["completed_at"] = datetime.now().isoformat()
            ts = datetime.now().isoformat()
            analytics_log.append({
                "task_id": tid, "agent": task.get("assigned_to", ""),
                "status": "done", "started": task.get("started_at", ""),
                "completed": task["completed_at"],
            })
            messages.setdefault("user", []).append({
                "sender": "system", "receiver": "user",
                "content": f"Task #{tid} passed code review. No QA agent — auto-UAT approved → done.",
                "msg_type": "info", "timestamp": ts,
            })
            add_activity("system", task.get("assigned_to", "?"), "auto_uat",
                         f"Task #{tid} auto-UAT approved (no QA agent)")
            _auto_notify_dependents(tid)
        else:
            task["status"] = "uat"
            task["_uat_entered_at"] = datetime.now().isoformat()
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

    # Determine changed files scope
    changed_files_hint = ""
    if project:
        from hub.state import git_cmd, safe_project_dir
        proj_dir = safe_project_dir(project)
        if proj_dir:
            base = task.get("_base_commit") or "main"
            if branch:
                _, files_raw = git_cmd(["diff", f"{base}...{branch}", "--name-only"], cwd=proj_dir)
            else:
                _, files_raw = git_cmd(["diff", "--name-only", base], cwd=proj_dir)
            if files_raw:
                file_list = [f.strip() for f in files_raw.strip().split("\n") if f.strip()][:30]
                if file_list:
                    changed_files_hint = f"\n\nChanged files (scope your testing to these):\n" + "\n".join(f"  - {f}" for f in file_list)

    qa_msg = (
        f"QA TEST REQUEST — Task #{tid}\n"
        f"Code review passed (3/3 approved). Now verify with tests.\n"
        f"Author: {task.get('assigned_to', '?')} | Project: {project} | Branch: {branch}\n\n"
        f"Task: {desc}{changed_files_hint}\n\n"
        f"Scope your testing to the changed files listed above.\n"
        f"Run relevant test suites, linters, and verify the changes work correctly.\n"
        f"If tests pass, report SUCCESS. If tests fail, report FAILURE with details."
    )

    # Create QA subtask (like review subtasks)
    sub_tid = max(tasks.keys(), default=0) + 1
    tasks[sub_tid] = {
        "id": sub_tid,
        "description": qa_msg,
        "assigned_to": qa_agent,
        "status": "to_do",
        "depends_on": [],
        "project": project,
        "branch": branch,
        "task_external_id": task.get("task_external_id", ""),
        "parent_id": tid,
        "priority": task.get("priority", 5),
        "created": ts, "started_at": "", "completed_at": "",
        "created_by": "system",
        "_is_qa_subtask": True,
        "_review_parent_id": tid,
    }
    task["_qa_subtask_id"] = sub_tid

    messages.setdefault(qa_agent, []).append({
        "sender": "system", "receiver": qa_agent,
        "content": qa_msg,
        "msg_type": "task", "task_id": str(sub_tid),
        "_review_parent_id": tid,
        "timestamp": ts,
    })
    add_activity("system", qa_agent, "qa_dispatched", f"QA test #{tid} dispatched to {qa_agent} (subtask #{sub_tid})")
    bump_version()


def _is_qa_agent(name):
    """Check if an agent name looks like a QA/review agent."""
    low = name.lower()
    return any(h in low for h in _QA_AGENT_HINTS)


def _handle_qa_failure(tid, old_status, detail=""):
    """When QA fails a task (in_testing → failed), send it back to in_progress.

    Same task cycles: in_progress → code_review → in_testing → (QA fail) → in_progress → ...
    Returns True if the task was redirected back to in_progress.
    """
    if old_status != "in_testing":
        return False  # Not a QA failure — let normal failure handling proceed

    task = tasks.get(tid)
    if not task:
        return False

    qa_cycle = task.get("_qa_cycle", 0) + 1
    total_rework = task.get("_total_rework_cycles", 0) + 1

    if qa_cycle >= MAX_REWORK_ITERATIONS or total_rework >= MAX_TOTAL_REWORK_CYCLES:
        # Max QA or total rework cycles reached — let task stay failed
        reason = (f"total rework cap ({total_rework}/{MAX_TOTAL_REWORK_CYCLES})"
                  if total_rework >= MAX_TOTAL_REWORK_CYCLES
                  else f"QA cycles ({MAX_REWORK_ITERATIONS})")
        ts = datetime.now().isoformat()
        messages.setdefault("user", []).append({
            "sender": "system", "receiver": "user",
            "content": f"🔄 Max {reason} reached for task #{tid}. "
                       f"Marked as failed — manual intervention needed.",
            "msg_type": "blocker", "timestamp": ts,
        })
        logger.info(f"QA cycle max reached: #{tid} ({qa_cycle} cycles)")
        return False

    # Mark QA subtask as failed
    qa_sub_id = task.get("_qa_subtask_id")
    if qa_sub_id and qa_sub_id in tasks:
        tasks[qa_sub_id]["status"] = "failed"
        tasks[qa_sub_id]["completed_at"] = datetime.now().isoformat()

    # Redirect: set task back to in_progress (same task, new cycle)
    task["status"] = "in_progress"
    task["_qa_cycle"] = qa_cycle
    task["_total_rework_cycles"] = total_rework
    task["_review_cycle"] = 0  # Reset so code review runs fresh
    task.pop("completed_at", None)  # Not completed yet
    qa_feedback = detail or task.pop("error_message", None) or "QA tests failed — see QA agent logs for details"

    dev_agent = task.get("assigned_to", "")
    ts = datetime.now().isoformat()

    # Send QA feedback to dev agent
    if dev_agent:
        messages.setdefault(dev_agent, []).append({
            "sender": "system", "receiver": dev_agent,
            "content": f"QA FAILURE FEEDBACK — Task #{tid}\n"
                       f"QA tests failed (cycle {qa_cycle}/{MAX_REWORK_ITERATIONS}).\n\n"
                       f"FEEDBACK:\n{qa_feedback}\n\n"
                       f"Fix ALL issues. After fixing, your task will go through code_review → in_testing again.",
            "msg_type": "qa_feedback", "task_id": str(tid),
            "timestamp": ts,
        })

    # Notify user
    messages.setdefault("user", []).append({
        "sender": "system", "receiver": "user",
        "content": f"🔄 Task #{tid} QA failed → sent back to {dev_agent or '?'} for rework "
                   f"(QA cycle {qa_cycle}/{MAX_REWORK_ITERATIONS}).",
        "msg_type": "info", "timestamp": ts,
    })

    add_activity("system", dev_agent or "?", "qa_rework",
                 f"QA failed #{tid} → back to in_progress (cycle {qa_cycle})")

    logger.info(f"QA failure → rework: #{tid} assigned to {dev_agent} (cycle {qa_cycle}/{MAX_REWORK_ITERATIONS})")
    bump_version()
    return True


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
        tasks[tid]["_is_check_task"] = True
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


def _build_jira_dev_prompt(issue_key: str, jira_url: str, agent: str) -> str:
    return f"""JIRA DEV IMPACT ANALYSIS — {issue_key}
Jira URL: {jira_url}

INSTRUCTIONS:
1. Fetch the Jira ticket using the Atlassian MCP tool:
   Use mcp__atlassian__getJiraIssue with issueIdOrKey="{issue_key}"

2. Extract from the ticket:
   - Title, description, acceptance criteria
   - Subtasks and linked issues
   - Priority, labels, components

3. Codebase impact analysis — search for affected files/modules:
   - Grep for keywords from the ticket (feature names, component names, API endpoints)
   - Check file structure: models, routes, controllers, services, configs
   - Trace import chains for affected modules
   - Identify API endpoints that would change

4. Generate a comprehensive impact report in this format:

### Dev Impact Analysis — {issue_key}
**Ticket:** [title from Jira]
**URL:** {jira_url}

#### Summary
[2-3 sentence overview of what needs to change]

#### Affected Files
| File | Module | Expected Changes | Risk |
|------|--------|-----------------|------|
| path | module | description | Low/Med/High |

#### API Changes
- [endpoints that need modification or creation]

#### Database / Schema Changes
- [any model or migration changes needed]

#### Dependencies
- [new packages, internal module dependencies affected]

#### Architecture Impact
- [how this fits into existing patterns, any refactoring needed]

#### Risk Assessment
- **Complexity:** Low/Medium/High
- **Regression risk:** [areas that could break]
- **Cross-cutting concerns:** [auth, caching, logging, etc.]

#### Implementation Notes
- [suggested approach, gotchas, order of operations]

5. Save the report to: $WORKSPACE/.multiagent/reports/dev-check_{issue_key}_$(date +%Y%m%d_%H%M%S).md
   Create the reports directory if it doesn't exist: mkdir -p "$WORKSPACE/.multiagent/reports"

6. Send the report to user:
   curl -s -X POST $HUB/messages -H 'Content-Type: application/json' -d '{{"sender":"{agent}","receiver":"user","content":"REPORT_HERE","msg_type":"check_report"}}'"""


def _build_jira_qa_prompt(issue_key: str, jira_url: str, agent: str) -> str:
    return f"""JIRA QA ANALYSIS — {issue_key}
Jira URL: {jira_url}

INSTRUCTIONS:
1. Fetch the Jira ticket using the Atlassian MCP tool:
   Use mcp__atlassian__getJiraIssue with issueIdOrKey="{issue_key}"

2. Extract from the ticket:
   - Title, description, acceptance criteria
   - Subtasks and linked issues
   - Priority, labels, components

3. QA analysis:
   - Map each acceptance criterion to concrete test scenarios
   - Search the codebase for existing tests related to this feature
   - Identify test coverage gaps
   - Analyze regression risk areas
   - Check for edge cases, error scenarios, boundary conditions
   - Review security implications (auth, input validation, injection)
   - Review performance implications (N+1 queries, large payloads, caching)

4. Generate a comprehensive QA report in this format:

### QA Analysis — {issue_key}
**Ticket:** [title from Jira]
**URL:** {jira_url}

#### Acceptance Criteria → Test Scenarios
| AC | Test Scenario | Type | Priority |
|----|--------------|------|----------|
| criterion | test description | Unit/Integration/E2E | P0/P1/P2 |

#### Existing Test Coverage
- [relevant test files found]
- [coverage gaps identified]

#### Regression Risk Areas
- [modules/features that could break]
- [integration points to verify]

#### Edge Cases & Boundary Conditions
- [list of edge cases to test]

#### Security Considerations
- [auth, validation, injection risks]

#### Performance Considerations
- [queries, payload sizes, caching concerns]

#### Manual Test Plan
1. [step-by-step manual test scenarios]

#### Recommended Test Strategy
- **Must test:** [critical paths]
- **Should test:** [important but lower risk]
- **Nice to test:** [edge cases if time permits]

5. Save the report to: $WORKSPACE/.multiagent/reports/qa-check_{issue_key}_$(date +%Y%m%d_%H%M%S).md
   Create the reports directory if it doesn't exist: mkdir -p "$WORKSPACE/.multiagent/reports"

6. Send the report to user:
   curl -s -X POST $HUB/messages -H 'Content-Type: application/json' -d '{{"sender":"{agent}","receiver":"user","content":"REPORT_HERE","msg_type":"check_report"}}'"""


@router.post("/tasks/check/jira")
def request_jira_check(data: dict):
    """Request a dev-check or qa-check for a Jira ticket (no internal task needed)."""
    check_type = data.get("check_type", "dev")
    issue_key = data.get("issue_key", "")
    jira_url = data.get("jira_url", "")
    agent = data.get("agent", "")

    if not issue_key or not re.match(r"^[A-Z]+-\d+$", issue_key):
        return {"status": "error", "message": f"Invalid issue key: {issue_key}"}

    # Pick a sensible agent if none provided
    if not agent:
        visible = [a for a in ALL_AGENTS if a not in HIDDEN_AGENTS]
        if check_type == "qa":
            agent = next((a for a in visible if any(h in a.lower() for h in _QA_AGENT_HINTS)), None)
        else:
            agent = next((a for a in visible if a not in _QA_AGENT_HINTS and a != "architect"), None)
        if not agent and visible:
            agent = visible[0]
        if not agent:
            return {"status": "error", "message": "No available agent"}

    if check_type == "qa":
        prompt = _build_jira_qa_prompt(issue_key, jira_url, agent)
    else:
        prompt = _build_jira_dev_prompt(issue_key, jira_url, agent)

    with lock:
        ts = datetime.now().isoformat()
        messages.setdefault(agent, []).append({
            "sender": "user", "receiver": agent,
            "content": prompt,
            "msg_type": "task",
            "timestamp": ts,
        })
        add_activity("user", agent, f"jira_{check_type}_check",
                     f"Requested Jira {check_type} check on {issue_key}")
        bump_version()
    return {"status": "ok", "agent": agent, "check_type": check_type, "issue_key": issue_key}


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
    t = dict(tasks[tid])
    # Collect subtasks (review + QA)
    subtasks = []
    for st in tasks.values():
        if st.get("parent_id") == tid or st.get("_review_parent_id") == tid:
            subtasks.append({
                "id": st["id"], "assigned_to": st.get("assigned_to", ""),
                "status": st.get("status", ""),
                "type": "review" if st.get("_is_review_subtask") else "qa" if st.get("_is_qa_subtask") else "task",
            })
    if subtasks:
        t["_subtasks"] = subtasks
    # Review verdicts
    tid_str = str(tid)
    if tid_str in task_reviews:
        t["_reviews"] = task_reviews[tid_str]
    return t

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
    # Hidden agents (reviewers) only get work via direct message, never auto-assign
    if name in HIDDEN_AGENTS:
        return {"status": "none"}
    with lock:
        # Tasks explicitly assigned to this agent
        candidates = [t for t in tasks.values()
                      if t.get("assigned_to") == name and t.get("status") in ("to_do", "created")]
        # Unassigned tasks — prefer role-matched (exclude review subtasks — they are pre-assigned)
        unassigned = [t for t in tasks.values()
                      if not t.get("assigned_to") and t.get("status") in ("to_do", "created")
                      and t.get("created_by") != name
                      and not t.get("_is_review_subtask")
                      and not t.get("_is_qa_subtask")]
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
    if len(task_comments.get(tid_str, [])) >= 100:
        return {"status": "error", "message": "comment limit reached (100 per task)"}
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
    agent = data.get("agent", "")
    verdict = data.get("verdict", "")
    if verdict not in ("approve", "request_changes"):
        return {"status": "error", "message": "verdict must be 'approve' or 'request_changes'"}
    if not agent:
        return {"status": "error", "message": "agent required"}

    review_comments = data.get("comments", [])
    if not isinstance(review_comments, list):
        review_comments = []
    with lock:
        if tid not in tasks:
            return {"status": "not_found"}
        # Reject stale verdicts — task must be in code_review to accept reviews (checked under lock)
        if tasks[tid].get("status") != "code_review":
            return {"status": "error", "message": f"Task not in code_review (current: {tasks[tid].get('status')})"}
        # Validate agent is an assigned reviewer for this task
        assigned_reviewers = set()
        for sub_id in tasks[tid].get("_review_subtask_ids", []):
            sub = tasks.get(sub_id)
            if sub:
                assigned_reviewers.add(sub.get("assigned_to", ""))
        if assigned_reviewers and agent not in assigned_reviewers:
            return {"status": "error", "message": f"Agent '{agent}' is not an assigned reviewer for this task"}
        # Store verdict
        task_reviews.setdefault(tid_str, {})[agent] = {
            "verdict": verdict,
            "comments": review_comments[:20],
            "timestamp": datetime.now().isoformat(),
        }
        # Add review comments to task_comments
        import hub.state as _st
        for rc in review_comments[:20]:
            if not isinstance(rc, dict):
                continue
            _st._comment_counter += 1
            task_comments.setdefault(tid_str, []).append({
                "id": _st._comment_counter,
                "agent": agent,
                "text": f"[{rc.get('severity', 'info')}] {rc.get('file', '')}:{rc.get('line', '')} — {rc.get('text', '')}",
                "timestamp": datetime.now().isoformat(),
                "resolved": False,
            })

        # Close the reviewer's subtask
        subtask_ids = tasks[tid].get("_review_subtask_ids", [])
        for sub_id in subtask_ids:
            sub = tasks.get(sub_id)
            if sub and sub.get("assigned_to") == agent and sub.get("status") not in ("done", "failed", "cancelled"):
                sub["status"] = "done" if verdict == "approve" else "failed"
                sub["completed_at"] = datetime.now().isoformat()
                break

        add_activity(agent, "code_review", "review_verdict",
                     f"#{tid} {verdict} by {agent}" + (f" ({len(review_comments)} comments)" if review_comments else ""))

        # Check if all active reviewers have responded
        reviewers = [r for r in ["reviewer-logic", "reviewer-style", "reviewer-arch"] if r in ALL_AGENTS]
        reviews = task_reviews.get(tid_str, {})
        all_responded = all(r in reviews for r in reviewers)

        if all_responded:
            all_approved = all(reviews[r].get("verdict") == "approve" for r in reviewers)
            if all_approved:
                # 3/3 approved → advance to in_testing
                tasks[tid]["status"] = "in_testing"
                tasks[tid]["_testing_started_at"] = datetime.now().isoformat()
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
                if tasks[tid].get("skip_qa"):
                    if _cfg.get("auto_uat", False):
                        tasks[tid]["status"] = "done"
                        tasks[tid]["completed_at"] = datetime.now().isoformat()
                        add_activity("system", tasks[tid].get("assigned_to", "?"), "qa_skipped",
                                     f"QA #{tid} skipped (skip_qa flag)")
                        add_activity("system", tasks[tid].get("assigned_to", "?"), "auto_uat",
                                     f"Task #{tid} auto-UAT approved (skip_qa)")
                        ts_qa = datetime.now().isoformat()
                        analytics_log.append({
                            "task_id": tid, "agent": tasks[tid].get("assigned_to", ""),
                            "status": "done", "started": tasks[tid].get("started_at", ""),
                            "completed": tasks[tid]["completed_at"],
                        })
                        messages.setdefault("user", []).append({
                            "sender": "system", "receiver": "user",
                            "content": f"✅ Task #{tid} code review passed. QA skipped → auto-UAT approved → done.",
                            "msg_type": "info", "timestamp": ts_qa,
                        })
                        _auto_notify_dependents(tid)
                    else:
                        tasks[tid]["status"] = "uat"
                        tasks[tid]["_uat_entered_at"] = datetime.now().isoformat()
                        add_activity("system", tasks[tid].get("assigned_to", "?"), "qa_skipped",
                                     f"QA #{tid} skipped (skip_qa flag)")
                        ts_qa = datetime.now().isoformat()
                        messages.setdefault("user", []).append({
                            "sender": "system", "receiver": "user",
                            "content": f"✅ Task #{tid} code review passed. QA skipped → moved to UAT.",
                            "msg_type": "info", "timestamp": ts_qa,
                        })
                else:
                    _dispatch_qa(tid)
            else:
                # At least one request_changes → send back to dev
                tasks[tid]["status"] = "in_progress"
                tasks[tid].pop("review_dispatched_at", None)
                tasks[tid]["_review_cycle"] = tasks[tid].get("_review_cycle", 0) + 1
                tasks[tid]["_total_rework_cycles"] = tasks[tid].get("_total_rework_cycles", 0) + 1
                dev_agent = tasks[tid].get("assigned_to", "")
                # Collect all change requests
                all_issues = []
                for r in reviewers:
                    rv = reviews.get(r, {})
                    if rv.get("verdict") == "request_changes":
                        for c in rv.get("comments", []):
                            if not isinstance(c, dict):
                                continue
                            all_issues.append(f"[{r}] {c.get('file', '')}:{c.get('line', '')} — {c.get('text', '')}")
                feedback = "\n".join(all_issues) if all_issues else "Reviewers requested changes. Check review comments."
                ts = datetime.now().isoformat()
                if dev_agent:
                    messages.setdefault(dev_agent, []).append({
                        "sender": "system", "receiver": dev_agent,
                        "content": f"CODE REVIEW FEEDBACK — Task #{tid}\n"
                                   f"Reviewers requested changes (cycle {tasks[tid].get('_review_cycle', 1)}/{MAX_REWORK_LOOPS}).\n\n"
                                   f"ISSUES:\n{feedback}\n\n"
                                   f"Fix ALL issues above. After fixing, your task will automatically go back to code_review.",
                        "msg_type": "review_feedback", "task_id": tid_str,
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
            task["_total_rework_cycles"] = task.get("_total_rework_cycles", 0) + 1
            dev_agent = task.get("assigned_to", "")
            if dev_agent:
                messages.setdefault(dev_agent, []).append({
                    "sender": "system", "receiver": dev_agent,
                    "content": f"UAT REJECTED — Task #{tid}\n"
                               f"User feedback: {feedback or 'No specific feedback provided.'}\n\n"
                               f"Please address the feedback and move task back to code_review when done.",
                    "msg_type": "uat_feedback", "task_id": str(tid),
                    "timestamp": ts,
                })
            # Reset QA tasks that depend on this task back to to_do
            for other_tid, other_task in tasks.items():
                if (other_task.get("assigned_to") in {a for a in ALL_AGENTS if _is_qa_agent(a)}
                        and tid in (other_task.get("depends_on") or [])
                        and other_task.get("status") == "done"):
                    other_task["status"] = "to_do"
                    other_task.pop("completed_at", None)
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

    # Form values for {{placeholder}} substitution in step descriptions
    form_values = data.get("form_values", {})

    created_tasks = []
    # Map step index → task ID for dependency resolution
    step_to_tid = {}
    # Shared branch for all tasks in this plan (resolved on first task)
    plan_shared_branch = None
    plan_shared_ext_id = None

    with lock:
        for idx in sorted(selected):
            if idx < 0 or idx >= len(steps):
                continue
            step = steps[idx]
            # Validate that the target agent exists
            step_agent = step.get("assigned_to", "")
            if step_agent and step_agent not in ALL_AGENTS:
                logger.warning(f"Plan #{plan_id} step {idx}: agent '{step_agent}' does not exist — skipping step")
                ts_skip = datetime.now().isoformat()
                messages.setdefault("user", []).append({
                    "sender": "system", "receiver": "user",
                    "content": f"Plan #{plan_id} step {idx} skipped: agent '{step_agent}' does not exist. "
                               f"Description: {step.get('description', '')[:100]}",
                    "msg_type": "warning", "timestamp": ts_skip,
                })
                continue
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
            # Branch resolution: step external_id > plan branch > auto-generate
            # All tasks in a plan share the SAME branch for consistency
            step_ext_id = step.get("task_external_id", "").strip()
            if plan_shared_branch is None:
                # First task determines the shared branch
                plan_branch = plan.get("branch", "").strip()
                if step_ext_id:
                    plan_shared_branch = step_ext_id if step_ext_id.startswith("feature/") else f"feature/{step_ext_id}"
                    plan_shared_ext_id = step_ext_id
                elif plan_branch and plan_branch not in ("main", "master", "develop", ""):
                    plan_shared_branch = plan_branch if plan_branch.startswith("feature/") else f"feature/{plan_branch}"
                    plan_shared_ext_id = plan_branch.replace("feature/", "")
                else:
                    plan_shared_branch = ""
                    plan_shared_ext_id = ""
            # Apply {{placeholder}} substitution from form_values
            step_desc = step.get("description", "")
            for fk, fv in form_values.items():
                step_desc = step_desc.replace("{{" + fk + "}}", str(fv))

            task = {
                "id": tid,
                "description": step_desc,
                "assigned_to": step.get("assigned_to", ""),
                "status": "to_do",
                "depends_on": deps,
                "project": plan.get("project", ""),
                "branch": plan_shared_branch,
                "task_external_id": step_ext_id or plan_shared_ext_id,
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

        # ── QA enforce: if no step is assigned to QA, auto-add QA task ──
        dev_task_ids = [ct["task_id"] for ct in created_tasks]
        has_qa = any(tasks[ct["task_id"]].get("assigned_to") == "qa" for ct in created_tasks)
        if not has_qa and dev_task_ids:
            qa_tid = max(tasks.keys(), default=0) + 1
            qa_task = {
                "id": qa_tid,
                "description": f"QA: Verify and test all changes from plan #{plan_id}. Run lint, tests, review code quality.",
                "assigned_to": "qa",
                "status": "to_do",
                "depends_on": list(dev_task_ids),
                "project": plan.get("project", ""),
                "branch": plan_shared_branch or "",
                "task_external_id": plan_shared_ext_id or "",
                "parent_id": None,
                "priority": 5,
                "created": datetime.now().isoformat(),
                "started_at": "", "completed_at": "",
                "created_by": "system",
                "plan_id": plan_id,
            }
            tasks[qa_tid] = qa_task
            created_tasks.append({"step_index": -1, "task_id": qa_tid})
            add_activity("system", "qa", "task_create", f"Auto QA for plan #{plan_id}")

        # ── Architect supervisor task: track all subtasks ──
        all_task_ids = [ct["task_id"] for ct in created_tasks]
        arch_tid = max(tasks.keys(), default=0) + 1
        plan_summary = plan.get("summary", plan.get("description", ""))[:120]
        arch_task = {
            "id": arch_tid,
            "description": f"Supervise plan #{plan_id}: {plan_summary} — track subtasks, ensure completion",
            "assigned_to": "architect",
            "status": "to_do",
            "depends_on": list(all_task_ids),
            "project": plan.get("project", ""),
            "branch": plan_shared_branch or "",
            "task_external_id": plan_shared_ext_id or "",
            "parent_id": None,
            "priority": 3,
            "created": datetime.now().isoformat(),
            "started_at": "", "completed_at": "",
            "created_by": "system",
            "plan_id": plan_id,
        }
        tasks[arch_tid] = arch_task
        created_tasks.append({"step_index": -2, "task_id": arch_tid})
        add_activity("system", "architect", "task_create", f"Supervisor for plan #{plan_id}")

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

        # Auto-start: activate tasks whose deps are already met
        for ct in created_tasks:
            task = tasks[ct["task_id"]]
            agent = task.get("assigned_to", "")
            if not agent:
                continue
            deps = task.get("depends_on", [])
            deps_met = all(tasks.get(d, {}).get("status") == "done" for d in deps)
            if deps_met:
                # Transition to in_progress so agent picks it up immediately
                task["status"] = "in_progress"
                task["started_at"] = ts
                messages.setdefault(agent, []).append({
                    "sender": "user",
                    "receiver": agent,
                    "content": f"#{ct['task_id']} {task['description']}",
                    "msg_type": "task",
                    "task_id": str(ct["task_id"]),
                    "project": task.get("project", ""),
                    "branch": task.get("branch", ""),
                    "task_external_id": task.get("task_external_id", ""),
                    "timestamp": ts,
                })

        # Store subtask link — parent completes when all subtasks are done
        parent_tid = plan.get("task_id", "")
        if parent_tid and str(parent_tid).isdigit():
            ptid = int(parent_tid)
            if ptid in tasks:
                tasks[ptid]["_plan_subtask_ids"] = [ct["task_id"] for ct in created_tasks]
                tasks[ptid]["_plan_id"] = plan_id

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
        # Return parent task to to_do so it can be re-assigned
        parent_tid = pending_plans[plan_id].get("task_id", "")
        if parent_tid and str(parent_tid).isdigit():
            tid = int(parent_tid)
            if tid in tasks and tasks[tid].get("status") == "in_progress":
                tasks[tid]["status"] = "to_do"
        bump_version()
        save_state()
    return {"status": "ok"}

@router.get("/pending-plans")
def get_pending_plans(creator: str = "", task_id: str = ""):
    """Check for pending plans by creator and/or task_id."""
    results = []
    for pid, plan in pending_plans.items():
        if plan.get("status") != "pending":
            continue
        if creator and plan.get("created_by") != creator:
            continue
        if task_id and str(plan.get("task_id", "")) != str(task_id):
            continue
        results.append({"plan_id": pid, "task_id": plan.get("task_id", ""), "created_by": plan.get("created_by", "")})
    return results


@router.get("/tasks/{tid}/quality")
def task_quality(tid: int):
    """Calculate quality score for a task."""
    if tid not in tasks:
        return {"error": "not found"}
    t = tasks[tid]
    score = 100
    tid_str = str(tid)
    # Deductions
    reviews = task_reviews.get(tid_str, {})
    for agent_name, rv in reviews.items():
        if rv.get("verdict") == "request_changes":
            score -= 15
    rework = t.get("_rework_count", 0)
    score -= rework * 20
    if t.get("_auto_approved"):
        score -= 5
    if t.get("status") == "failed":
        score -= 30
    # Test failures from test_results
    from hub.state import test_results
    for tr in test_results:
        if tr.get("task_id") == tid and tr.get("tests_failed", 0) > 0:
            score -= 10
            break
    score = max(0, min(100, score))
    grade = "A" if score >= 90 else "B" if score >= 75 else "C" if score >= 60 else "D" if score >= 40 else "F"
    return {"task_id": tid, "score": score, "grade": grade}
