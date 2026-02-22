"""agents/log_utils.py — Logging, log streaming to hub, and humanization."""
import json, re, time, threading
from datetime import datetime
from urllib.request import Request, urlopen


# ── Log Humanization Patterns ──
_HUB_PATTERNS = [
    (r'curl\s.*?/tasks/auto-assign/(\w+)', lambda m: f'→ hub: auto-assign task to {m.group(1)}'),
    (r'curl\s.*?/tasks\s.*?"description"\s*:\s*"([^"]{1,80})', lambda m: f'→ hub: create task "{m.group(1)}"'),
    (r'curl\s.*?/tasks/(\d+)/ready', lambda m: f'→ hub: check deps for task #{m.group(1)}'),
    (r'curl\s.*?/tasks/(\d+)', lambda m: f'→ hub: update task #{m.group(1)}'),
    (r'curl\s.*?/tasks\b', lambda m: '→ hub: list tasks'),
    (r'curl\s.*?/messages\s.*?"receiver"\s*:\s*"(\w+)".*?"content"\s*:\s*"([^"]{1,80})', lambda m: f'→ msg to {m.group(1)}: {m.group(2)}'),
    (r'curl\s.*?/messages/(\w+)/chat', lambda m: f'→ hub: check chat for {m.group(1)}'),
    (r'curl\s.*?/messages/(\w+)', lambda m: f'→ hub: fetch messages for {m.group(1)}'),
    (r'curl\s.*?/messages\s.*?-X\s*POST', lambda m: '→ hub: send message'),
    (r'curl\s.*?/messages\b', lambda m: '→ hub: list messages'),
    (r'curl\s.*?/credentials/raw', lambda m: '→ hub: load credentials'),
    (r'curl\s.*?/credentials\b', lambda m: '→ hub: check credentials'),
    (r'curl\s.*?/agents/status', lambda m: '→ hub: report agent status'),
    (r'curl\s.*?/agents\b', lambda m: '→ hub: list agents'),
    (r'curl\s.*?/files/lock\s.*?"file_path"\s*:\s*"([^"]{1,80})', lambda m: f'→ hub: lock {m.group(1).split("/")[-1]}'),
    (r'curl\s.*?/health', lambda m: '→ hub: health check'),
    (r'curl\s.*?/changes', lambda m: '→ hub: report changes'),
    (r'curl\s.*?/tests', lambda m: '→ hub: report test results'),
    (r'curl\s.*?/route\?', lambda m: '→ hub: route query'),
]


def log(ctx, msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with ctx._log_lock:
        ctx._log_buf.append(line)


def _push_logs_loop(ctx):
    """Background thread: push buffered logs to hub every 1s (batched for efficiency)."""
    while True:
        time.sleep(1)
        with ctx._log_lock:
            if not ctx._log_buf:
                continue
            lines = ctx._log_buf.copy()
            ctx._log_buf.clear()
        try:
            body = json.dumps({"lines": lines}).encode()
            req = Request(f"{ctx.HUB_URL}/logs/{ctx.AGENT_NAME}/push", data=body,
                          headers={"Content-Type": "application/json"})
            urlopen(req, timeout=2)
        except Exception:
            pass


def start_log_thread(ctx):
    """Start the background log push thread."""
    threading.Thread(target=_push_logs_loop, args=(ctx,), daemon=True).start()


def flush_logs(ctx):
    with ctx._log_lock:
        if not ctx._log_buf:
            return
        lines = ctx._log_buf.copy()
        ctx._log_buf.clear()
    try:
        body = json.dumps({"lines": lines}).encode()
        req = Request(f"{ctx.HUB_URL}/logs/{ctx.AGENT_NAME}/push", data=body,
                      headers={"Content-Type": "application/json"})
        urlopen(req, timeout=2)
    except Exception:
        pass


def humanize_bash(cmd):
    """Turn raw bash commands into readable descriptions."""
    first_line = cmd.split("\n")[0].strip()
    for pattern, formatter in _HUB_PATTERNS:
        m = re.search(pattern, first_line, re.IGNORECASE)
        if m:
            return formatter(m)
    if first_line.startswith("cd ") and "&&" in first_line:
        parts = first_line.split("&&", 1)
        dir_part = parts[0].strip()
        cmd_part = parts[1].strip() if len(parts) > 1 else ""
        proj = dir_part.split("/")[-1] if "/" in dir_part else dir_part.replace("cd ", "")
        if cmd_part:
            return f'{cmd_part[:120]} (in {proj}/)'
        return first_line[:200]
    echo_match = re.match(r'echo\s+"?([^">\n]{1,40})"?\s*>\s*(.+)', first_line)
    if echo_match:
        return f'write "{echo_match.group(1)}" → {echo_match.group(2).split("/")[-1]}'
    if first_line.startswith("mkdir"):
        return f'create directory: {first_line.split("/")[-1][:80]}'
    git_match = re.match(r'(?:cd\s+\S+\s*&&\s*)?git\s+(\S+)(.*)', first_line)
    if git_match:
        return f'git {git_match.group(1)}{git_match.group(2)[:100]}'
    return first_line[:200]


def short_path(path):
    """Shorten file paths for readability."""
    if not path:
        return ""
    parts = path.replace("\\", "/").split("/")
    if len(parts) <= 3:
        return path
    return "…/" + "/".join(parts[-3:])
