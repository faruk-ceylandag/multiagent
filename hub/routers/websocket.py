"""WebSocket endpoint for real-time dashboard updates + log streaming."""

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from hub.state import (
    log_buffers, log_counters, get_version, get_dashboard_snapshot,
)

router = APIRouter(tags=["websocket"])

_ws_clients: set = set()


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.add(ws)
    last_version = -1
    follow_agent = ""
    log_cursors = {}

    try:
        while True:
            # 1) Dashboard state push — uses cached snapshot (no lock, no executor)
            current_version = get_version()
            if current_version != last_version:
                snapshot = get_dashboard_snapshot()
                try:
                    await ws.send_text(json.dumps({
                        "type": "dashboard",
                        "version": current_version,
                        "data": snapshot,
                    }, default=str))
                except Exception:
                    break
                last_version = current_version

            # 2) Log streaming for followed agent (no lock — deque reads are GIL-safe)
            if follow_agent:
                cur = log_counters.get(follow_agent, 0)
                last_cur = log_cursors.get(follow_agent, cur)
                if cur > last_cur:
                    buf = log_buffers.get(follow_agent, [])
                    n = cur - last_cur
                    new_lines = list(buf[-n:]) if n <= len(buf) else list(buf)
                    log_cursors[follow_agent] = cur
                    if new_lines:
                        try:
                            await ws.send_text(json.dumps({
                                "type": "logs",
                                "agent": follow_agent,
                                "lines": new_lines,
                            }))
                        except Exception:
                            break

            # 3) Listen for client messages (1s timeout = poll frequency)
            try:
                msg = await asyncio.wait_for(ws.receive_text(), timeout=1.0)
                if msg == "ping":
                    await ws.send_text(json.dumps({"type": "pong"}))
                else:
                    try:
                        cmd = json.loads(msg)
                        if cmd.get("type") == "follow":
                            follow_agent = cmd.get("agent", "")
                            if follow_agent:
                                log_cursors[follow_agent] = log_counters.get(follow_agent, 0)
                    except (json.JSONDecodeError, AttributeError):
                        pass
            except asyncio.TimeoutError:
                pass
            except WebSocketDisconnect:
                break

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        _ws_clients.discard(ws)


@router.get("/ws/clients")
def ws_client_count():
    """How many WebSocket clients are connected."""
    return {"count": len(_ws_clients)}
