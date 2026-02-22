"""Pattern Registry routes: register, query, vote, delete."""

from datetime import datetime
from fastapi import APIRouter

from hub.state import (
    lock, logger, pattern_registry, PATTERN_SCORE_CAP, PATTERN_PRUNE_AT,
    PATTERN_CATEGORIES, save_state, bump_version,
)
import hub.state as _state

router = APIRouter(tags=["patterns"])


@router.post("/patterns")
def register_pattern(data: dict):
    """Register a new pattern (score starts at 1)."""
    pattern_text = data.get("pattern", "").strip()
    if not pattern_text:
        return {"status": "error", "message": "Empty pattern"}
    category = data.get("category", "general")
    if category not in PATTERN_CATEGORIES:
        category = "general"
    with lock:
        _state._pattern_id_counter += 1
        pid = f"pat_{_state._pattern_id_counter}"
        pattern_registry[pid] = {
            "id": pid,
            "pattern": pattern_text[:500],
            "category": category,
            "score": 1,
            "source_agent": data.get("source_agent", ""),
            "confirmed_by": [],
            "rejected_by": [],
            "task_context": data.get("task_context", "")[:200],
            "created_at": datetime.now().isoformat(),
            "last_voted_at": datetime.now().isoformat(),
        }
        save_state()
    return {"status": "ok", "id": pid}


@router.get("/patterns")
def query_patterns(category: str = "", min_score: int = 0, limit: int = 20):
    """Query patterns — lock-free read."""
    results = list(pattern_registry.values())
    if category:
        results = [p for p in results if p.get("category") == category]
    if min_score:
        results = [p for p in results if p.get("score", 0) >= min_score]
    results.sort(key=lambda p: p.get("score", 0), reverse=True)
    return results[:limit]


@router.post("/patterns/{pid}/vote")
def vote_pattern(pid: str, data: dict):
    """Vote on a pattern. +1 or -1. Double-vote prevented."""
    agent_name = data.get("agent_name", "")
    vote = data.get("vote", 0)
    if vote not in (1, -1) or not agent_name:
        return {"status": "error", "message": "Invalid vote or agent_name"}
    with lock:
        pat = pattern_registry.get(pid)
        if not pat:
            return {"status": "error", "message": "Pattern not found"}
        # Double-vote check
        if agent_name in pat["confirmed_by"] or agent_name in pat["rejected_by"]:
            return {"status": "error", "message": "Already voted"}
        if vote == 1:
            pat["confirmed_by"].append(agent_name)
        else:
            pat["rejected_by"].append(agent_name)
        pat["score"] = min(PATTERN_SCORE_CAP[0], max(PATTERN_SCORE_CAP[1], pat["score"] + vote))
        pat["last_voted_at"] = datetime.now().isoformat()
        # Auto-prune
        if pat["score"] <= PATTERN_PRUNE_AT:
            logger.info(f"Pattern {pid} pruned (score={pat['score']})")
            del pattern_registry[pid]
        save_state()
    return {"status": "ok", "score": pat.get("score", 0)}


@router.delete("/patterns/{pid}")
def delete_pattern(pid: str):
    """Manually delete a pattern."""
    with lock:
        if pid not in pattern_registry:
            return {"status": "error", "message": "Not found"}
        del pattern_registry[pid]
        save_state()
    return {"status": "ok"}
