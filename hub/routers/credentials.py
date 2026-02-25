"""Credential management, service connect, and notification config routes."""

import os, json
from datetime import datetime
from fastapi import APIRouter

from hub.state import (
    lock, logger, MA_DIR, ALL_AGENTS, messages, creds_file,
    notification_config, send_notification, add_activity, SERVICE_REGISTRY,
)

router = APIRouter(tags=["credentials"])

@router.get("/credentials")
def get_credentials():
    creds = {}
    if creds_file and os.path.exists(creds_file):
        try:
            with open(creds_file) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        creds[k.strip()] = v.strip()[:4] + "***" if len(v.strip()) > 4 else "***"
        except Exception as e:
            logger.warning(f"Credential read error: {e}")
    return {"credentials": creds}

@router.post("/credentials")
def save_credentials(data: dict):
    if not creds_file:
        return {"status": "error", "message": "No MA_DIR configured"}
    existing = {}
    if os.path.exists(creds_file):
        try:
            with open(creds_file) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        existing[k.strip()] = v.strip()
        except Exception as e:
            logger.warning(f"Credential parse error: {e}")
    for k, v in data.items():
        if k in ("status",):
            continue
        existing[k] = v
    try:
        with open(creds_file, "w") as f:
            f.write("# Multi-Agent Credentials\n")
            for k, v in sorted(existing.items()):
                f.write(f"{k}={v}\n")
        os.chmod(creds_file, 0o600)
        with lock:
            for agent in ALL_AGENTS:
                if agent not in messages:
                    messages[agent] = []
                messages[agent].append({
                    "sender": "system", "receiver": agent, "content": "Credentials updated",
                    "msg_type": "credential", "credentials": {k: "***" for k in data.keys()},
                    "time": datetime.now().isoformat()
                })
        add_activity("user", "system", "credentials", f"Updated: {', '.join(data.keys())}")
        return {"status": "ok", "saved": list(data.keys())}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.delete("/credentials/{key}")
def delete_credential(key: str):
    if not creds_file or not os.path.exists(creds_file):
        return {"status": "not_found"}
    existing = {}
    try:
        with open(creds_file) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    existing[k.strip()] = v.strip()
    except Exception as e:
        logger.warning(f"Credential read error: {e}")
    if key not in existing:
        return {"status": "not_found"}
    del existing[key]
    try:
        with open(creds_file, "w") as f:
            f.write("# Multi-Agent Credentials\n")
            for k, v in sorted(existing.items()):
                f.write(f"{k}={v}\n")
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# ── Service Connect ──
@router.get("/services")
def get_services():
    creds = {}
    if creds_file and os.path.exists(creds_file):
        try:
            with open(creds_file) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        creds[k.strip()] = True
        except Exception as e:
            logger.warning(f"Service credential check error: {e}")
    # Check OAuth auth cache for pending authentications
    _needs_auth = {}
    _auth_cache = os.path.expanduser("~/.claude/mcp-needs-auth-cache.json")
    if os.path.exists(_auth_cache):
        try:
            with open(_auth_cache) as f:
                _needs_auth = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    services = []
    for svc in SERVICE_REGISTRY:
        mcp_name = svc.get("mcp", svc["id"])
        if not svc["credentials"]:
            # OAuth server — check if auth is actually completed
            if mcp_name in _needs_auth:
                connected = False
                auth_type = "oauth_pending"
            else:
                connected = True
                auth_type = "oauth"
        else:
            connected = all(c["key"] in creds for c in svc["credentials"])
            auth_type = "credentials"
        entry = {**svc, "connected": connected, "auth_type": auth_type}
        services.append(entry)
    return {"services": services}

# ── Notifications ──
@router.post("/notifications/config")
def set_notification_config(data: dict):
    import hub.state as _st
    with lock:
        _st.notification_config = data
    cfg_path = os.path.join(MA_DIR, "config.json") if MA_DIR else ""
    if cfg_path and os.path.exists(cfg_path):
        try:
            with open(cfg_path) as f:
                cfg = json.load(f)
            cfg["notifications_webhook"] = data
            with open(cfg_path, "w") as f:
                json.dump(cfg, f, indent=2)
        except Exception as e:
            logger.warning(f"Notification config save error: {e}")
    return {"status": "ok"}

@router.get("/notifications/config")
def get_notification_config():
    return dict(notification_config)

@router.post("/notifications/test")
def test_notification():
    send_notification("test", "Test notification from Multi-Agent Dashboard")
    return {"status": "ok"}
