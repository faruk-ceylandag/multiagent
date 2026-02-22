"""Log routes: push, get, stream (SSE), merged."""

import json, re, asyncio, threading
from fastapi import APIRouter
from starlette.responses import StreamingResponse

from hub.state import (
    ALL_AGENTS, log_buffers, log_counters, sse_clients, _append_log_disk,
    bump_version,
)

router = APIRouter(tags=["logs"])
_log_counter_lock = threading.Lock()

@router.post("/logs/{name}/push")
def push_log(name: str, data: dict):
    lines = data.get("lines", [])
    buf = log_buffers.get(name)
    if buf is not None:
        with _log_counter_lock:
            for line in lines:
                buf.append(line)
                log_counters[name] = log_counters.get(name, 0) + 1
    if lines:
        _append_log_disk(name, lines)
        bump_version()
    return {"status": "ok"}

@router.get("/logs/{name}")
def get_log_lines(name: str, lines: int = 500, after: int = 0, search: str = ""):
    # No lock needed — deque reads are GIL-safe
    buf = list(log_buffers.get(name, []))
    counter = log_counters.get(name, 0)
    if search:
        buf = [l for l in buf if search.lower() in l.lower()]
    if after > 0:
        skip = max(0, len(buf) - (counter - after))
        return {"lines": buf[skip:], "cursor": counter}
    return {"lines": buf[-lines:], "cursor": counter}

@router.get("/logs/{name}/stream")
async def stream_log(name: str):
    sse_clients[name] = sse_clients.get(name, 0) + 1

    async def gen():
        try:
            last = log_counters.get(name, 0)
            idle_ticks = 0
            while True:
                await asyncio.sleep(1)
                cur = log_counters.get(name, 0)
                if cur > last:
                    buf = log_buffers.get(name, [])
                    n = cur - last
                    new_lines = list(buf[-n:]) if n <= len(buf) else list(buf)
                    last = cur
                    idle_ticks = 0
                    if new_lines:
                        yield f"data: {json.dumps(new_lines)}\n\n"
                else:
                    idle_ticks += 1
                    if idle_ticks % 15 == 0:
                        yield f": keepalive\n\n"
                    if idle_ticks > 300:
                        return
        finally:
            sse_clients[name] = max(0, sse_clients.get(name, 1) - 1)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@router.get("/logs/merged")
def merged_logs(lines: int = 200, search: str = ""):
    # No lock needed — read-only
    all_lines = []
    for name in ALL_AGENTS:
        for line in list(log_buffers.get(name, []))[-200:]:
            all_lines.append({"agent": name, "line": line})

    def sort_key(x):
        m = re.match(r'\[(\d{2}:\d{2}:\d{2})\]', x["line"])
        return m.group(1) if m else "99:99:99"

    all_lines.sort(key=sort_key)
    if search:
        all_lines = [l for l in all_lines if search.lower() in l["line"].lower()]
    return all_lines[-lines:]
