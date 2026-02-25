"""Cache routes: store and retrieve MCP content to avoid redundant fetches."""

import os
import hashlib
import base64
from datetime import datetime
from fastapi import APIRouter
from fastapi.responses import PlainTextResponse, Response

from hub.state import (
    lock, CACHE_DIR, cache_registry, add_activity, save_state, bump_version, logger,
)

router = APIRouter(tags=["cache"])


def _safe_key(key: str) -> str:
    """Sanitize cache key for filesystem use."""
    # Replace dangerous chars, keep readable
    safe = key.replace("/", "_").replace("\\", "_").replace("..", "_")
    safe = "".join(c for c in safe if c.isalnum() or c in "-_.")
    return safe[:200] if safe else "unnamed"


@router.post("/cache")
def store_cache(data: dict):
    """Store content in the MCP cache.

    Body: {key, content, source?, content_type?, description?}
    - key: unique identifier (e.g. "figma_ABC123_45-67", "jira_PA-123")
    - content: text content to cache (for binary, use base64 + content_type: "base64")
    - source: where the content came from (e.g. "figma", "jira", "github")
    - content_type: "text" (default), "base64" (for images/binary)
    - description: human-readable description
    """
    key = data.get("key", "")
    content = data.get("content", "")
    if not key or not content:
        return {"status": "error", "message": "key and content required"}
    if not CACHE_DIR:
        return {"status": "error", "message": "cache dir not configured"}

    safe_key = _safe_key(key)
    source = data.get("source", "unknown")
    content_type = data.get("content_type", "text")
    description = data.get("description", "")

    # Path containment check
    _test_path = os.path.join(CACHE_DIR, safe_key + ".test")
    if not os.path.realpath(_test_path).startswith(os.path.realpath(CACHE_DIR)):
        return {"status": "error", "message": "invalid cache key (path traversal)"}

    # Determine file extension
    if content_type == "base64":
        # Try to detect image type from content or use .bin
        ext = ".png" if "image/png" in description.lower() else ".bin"
        file_path = os.path.join(CACHE_DIR, safe_key + ext)
        try:
            raw = base64.b64decode(content)
            with open(file_path, "wb") as f:
                f.write(raw)
            size = len(raw)
        except Exception as e:
            return {"status": "error", "message": f"base64 decode failed: {e}"}
    else:
        ext = ".md"
        file_path = os.path.join(CACHE_DIR, safe_key + ext)
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
            size = len(content)
        except Exception as e:
            return {"status": "error", "message": f"write failed: {e}"}

    with lock:
        cache_registry[key] = {
            "key": key,
            "path": file_path,
            "source": source,
            "content_type": content_type,
            "description": description[:200],
            "created": datetime.now().isoformat(),
            "size": size,
        }
        bump_version()

    add_activity("system", "cache", "cache_store", f"Cached {source}: {key} ({size} bytes)")
    logger.info(f"Cache stored: {key} ({size} bytes) → {file_path}")
    save_state()
    return {"status": "ok", "path": file_path, "key": key}


@router.get("/cache/{key:path}")
def get_cache(key: str):
    """Retrieve cached content by key."""
    entry = cache_registry.get(key)
    if not entry:
        return {"status": "not_found"}

    file_path = entry["path"]
    if not os.path.exists(file_path):
        return {"status": "not_found", "message": "cache file missing"}

    if entry.get("content_type") == "base64":
        with open(file_path, "rb") as f:
            raw = f.read()
        return Response(content=raw, media_type="application/octet-stream")

    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    return PlainTextResponse(content)


@router.get("/cache")
def list_cache():
    """List all cached entries."""
    return list(cache_registry.values())


@router.delete("/cache/{key:path}")
def delete_cache(key: str):
    """Delete a cached entry."""
    entry = cache_registry.get(key)
    if not entry:
        return {"status": "not_found"}
    try:
        os.remove(entry["path"])
    except OSError:
        pass
    with lock:
        del cache_registry[key]
        bump_version()
    save_state()
    return {"status": "ok"}
