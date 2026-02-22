"""Cost and budget routes."""

from fastapi import APIRouter

from hub.state import (
    lock, ALL_AGENTS, usage_log, CostEntry, BUDGET_LIMIT, BUDGET_PER_AGENT,
    calc_agent_cost, calc_total_cost, save_state,
)

router = APIRouter(tags=["costs"])

@router.post("/costs/log")
def log_usage(entry: CostEntry):
    with lock:
        if entry.agent_name not in usage_log:
            usage_log[entry.agent_name] = {
                "tokens_in": 0, "tokens_out": 0, "requests": 0,
                "sonnet_in": 0, "sonnet_out": 0, "opus_in": 0, "opus_out": 0,
                "haiku_in": 0, "haiku_out": 0,
            }
        c = usage_log[entry.agent_name]
        c["tokens_in"] += entry.tokens_in
        c["tokens_out"] += entry.tokens_out
        c["requests"] += 1
        m = (entry.model or "").lower()
        if "opus" in m:
            c["opus_in"] = c.get("opus_in", 0) + entry.tokens_in
            c["opus_out"] = c.get("opus_out", 0) + entry.tokens_out
        elif "haiku" in m:
            c["haiku_in"] = c.get("haiku_in", 0) + entry.tokens_in
            c["haiku_out"] = c.get("haiku_out", 0) + entry.tokens_out
        else:
            c["sonnet_in"] = c.get("sonnet_in", 0) + entry.tokens_in
            c["sonnet_out"] = c.get("sonnet_out", 0) + entry.tokens_out
        # Track per-task costs
        task_id = getattr(entry, '__dict__', {}).get('task_id', '')
        if not task_id:
            task_id = ''
        if task_id:
            task_key = f"_task_{task_id}"
            if task_key not in usage_log:
                usage_log[task_key] = {"tokens_in": 0, "tokens_out": 0, "requests": 0}
            usage_log[task_key]["tokens_in"] = usage_log[task_key].get("tokens_in", 0) + entry.tokens_in
            usage_log[task_key]["tokens_out"] = usage_log[task_key].get("tokens_out", 0) + entry.tokens_out
            usage_log[task_key]["requests"] = usage_log[task_key].get("requests", 0) + 1
            # Model breakdown for tasks
            model_lower = (entry.model or "sonnet").lower()
            if "opus" in model_lower:
                usage_log[task_key]["opus_in"] = usage_log[task_key].get("opus_in", 0) + entry.tokens_in
                usage_log[task_key]["opus_out"] = usage_log[task_key].get("opus_out", 0) + entry.tokens_out
            elif "haiku" in model_lower:
                usage_log[task_key]["haiku_in"] = usage_log[task_key].get("haiku_in", 0) + entry.tokens_in
                usage_log[task_key]["haiku_out"] = usage_log[task_key].get("haiku_out", 0) + entry.tokens_out
            else:
                usage_log[task_key]["sonnet_in"] = usage_log[task_key].get("sonnet_in", 0) + entry.tokens_in
                usage_log[task_key]["sonnet_out"] = usage_log[task_key].get("sonnet_out", 0) + entry.tokens_out
        save_state()
    return {"status": "ok"}

@router.get("/costs")
def get_usage():
    # No lock — read-only display data
    ti = sum(c.get("tokens_in", 0) for c in usage_log.values())
    to = sum(c.get("tokens_out", 0) for c in usage_log.values())
    return {"agents": dict(usage_log), "total_in": ti, "total_out": to}

@router.get("/costs/estimate")
def estimate_cost(tokens: int = 0, model: str = "sonnet"):
    from hub.state import _PRICING
    if "opus" in model.lower():
        cost = (tokens / 1e6) * _PRICING["opus_in"] + (tokens * 3 / 1e6) * _PRICING["opus_out"]
    elif "haiku" in model.lower():
        cost = (tokens / 1e6) * _PRICING["haiku_in"] + (tokens * 3 / 1e6) * _PRICING["haiku_out"]
    else:
        cost = (tokens / 1e6) * _PRICING["sonnet_in"] + (tokens * 3 / 1e6) * _PRICING["sonnet_out"]
    return {"estimated_cost": round(cost, 4), "model": model}

@router.get("/costs/budget/{name}")
def get_budget(name: str):
    cost = calc_agent_cost(name)
    limit = BUDGET_PER_AGENT or BUDGET_LIMIT
    return {"spent": cost, "limit": limit, "remaining": max(0, limit - cost) if limit else 999}

@router.post("/budget")
def set_budget(data: dict):
    import hub.state as _state
    limit = float(data.get("limit", 0))
    with lock:
        _state.BUDGET_LIMIT = limit
        save_state()
    return {"status": "ok", "limit": limit}

@router.get("/costs/budget")
def get_total_budget():
    total = calc_total_cost()
    by_agent = {n: calc_agent_cost(n) for n in ALL_AGENTS}
    return {
        "total_spent": total, "limit": BUDGET_LIMIT,
        "remaining": max(0, BUDGET_LIMIT - total) if BUDGET_LIMIT else 999,
        "by_agent": by_agent,
    }


@router.get("/costs/task/{tid}")
def get_task_cost(tid: int):
    """Get cost breakdown for a specific task."""
    task_key = f"_task_{tid}"
    u = usage_log.get(task_key, {})
    if not u:
        return {"task_id": tid, "tokens_in": 0, "tokens_out": 0, "cost": 0}
    from hub.state import _PRICING
    ti, to = u.get("tokens_in", 0), u.get("tokens_out", 0)
    si, so = u.get("sonnet_in", 0), u.get("sonnet_out", 0)
    oi, oo = u.get("opus_in", 0), u.get("opus_out", 0)
    hi, ho = u.get("haiku_in", 0), u.get("haiku_out", 0)
    cost = ((si/1e6)*_PRICING["sonnet_in"] + (so/1e6)*_PRICING["sonnet_out"] +
            (oi/1e6)*_PRICING["opus_in"] + (oo/1e6)*_PRICING["opus_out"] +
            (hi/1e6)*_PRICING["haiku_in"] + (ho/1e6)*_PRICING["haiku_out"])
    if not si and not so and not oi and not oo:
        cost = (ti*0.7/1e6)*_PRICING["opus_in"] + (to*0.7/1e6)*_PRICING["opus_out"] + (ti*0.3/1e6)*_PRICING["sonnet_in"] + (to*0.3/1e6)*_PRICING["sonnet_out"]
    return {"task_id": tid, "tokens_in": ti, "tokens_out": to, "requests": u.get("requests", 0),
            "sonnet_tokens": si+so, "opus_tokens": oi+oo, "haiku_tokens": hi+ho,
            "cost": round(cost, 4)}
