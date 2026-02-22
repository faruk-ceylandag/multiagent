"""Input validation helpers."""
import re

# Agent name: alphanumeric, underscore, hyphen, 1-50 chars
AGENT_NAME_RE = re.compile(r'^[a-zA-Z0-9_-]{1,50}$')
MAX_TASK_DESC = 50000
MAX_MESSAGE_CONTENT = 100000
PRIORITY_RANGE = (1, 10)


def valid_agent_name(name: str) -> bool:
    return bool(name and AGENT_NAME_RE.match(name))


def valid_priority(p) -> int:
    try:
        p = int(p)
        return max(PRIORITY_RANGE[0], min(PRIORITY_RANGE[1], p))
    except (TypeError, ValueError):
        return 5


def sanitize_text(text: str, max_len: int) -> str:
    if not isinstance(text, str):
        return ""
    return text[:max_len]
