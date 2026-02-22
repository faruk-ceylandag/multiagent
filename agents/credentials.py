"""agents/credentials.py — Credential management (load, save, check)."""
import os


def load_credentials(ctx):
    """Load saved credentials from .multiagent/credentials.env"""
    creds = {}
    if not ctx.CREDS_FILE or not os.path.exists(ctx.CREDS_FILE):
        return creds
    try:
        with open(ctx.CREDS_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    creds[k.strip()] = v.strip()
    except (OSError, ValueError):
        pass
    return creds


def save_credential(ctx, key, value):
    """Save a credential to the credentials store."""
    from .log_utils import log
    if not ctx.CREDS_FILE:
        return False
    creds = load_credentials(ctx)
    creds[key] = value
    try:
        with open(ctx.CREDS_FILE, "w") as f:
            f.write("# Multi-Agent Credentials (auto-saved)\n")
            for k, v in sorted(creds.items()):
                f.write(f"{k}={v}\n")
        os.chmod(ctx.CREDS_FILE, 0o600)
        log(ctx, f"🔑 Saved credential: {key}")
        # Hook: on-credential-save
        from .learning import run_hook
        run_hook(ctx, "on-credential-save", {"key": key})
        return True
    except Exception as e:
        log(ctx, f"⚠ Failed to save credential: {e}")
        return False


def check_missing_credentials(task_text, creds):
    """Check if task requires service credentials that are not yet saved."""
    text_lower = task_text.lower()
    cred_keys_upper = {k.upper() for k in creds}

    service_checks = [
        {"service": "GitHub", "service_id": "github", "patterns": ["github.com", "github issue", "github pr", "pull request"],
         "keys": ["GITHUB_PERSONAL_ACCESS_TOKEN"], "check": lambda: any("GITHUB" in k for k in cred_keys_upper)},
        {"service": "Jira/Atlassian", "service_id": "atlassian", "patterns": ["atlassian.net", "jira.com", "jira ticket", "jira issue"],
         "keys": [], "check": lambda: True},  # OAuth — no credentials needed
        {"service": "Linear", "service_id": "linear", "patterns": ["linear.app", "linear issue"],
         "keys": ["LINEAR_API_KEY"], "check": lambda: any("LINEAR" in k for k in cred_keys_upper)},
        {"service": "Sentry", "service_id": "sentry", "patterns": ["sentry.io", "sentry error", "sentry issue"],
         "keys": [], "check": lambda: True},  # OAuth — no credentials needed
        {"service": "Figma", "service_id": "figma", "patterns": ["figma.com", "figma design", "figma file"],
         "keys": [], "check": lambda: True},  # OAuth — no credentials needed
        {"service": "Slack", "service_id": "slack", "patterns": ["slack.com", "slack channel", "slack message"],
         "keys": ["SLACK_BOT_TOKEN"], "check": lambda: any("SLACK" in k for k in cred_keys_upper)},
        {"service": "Notion", "service_id": "notion", "patterns": ["notion.so", "notion page", "notion database"],
         "keys": ["NOTION_TOKEN"], "check": lambda: any("NOTION" in k for k in cred_keys_upper)},
        {"service": "Supabase", "service_id": "supabase", "patterns": ["supabase.co", "supabase"],
         "keys": ["SUPABASE_ACCESS_TOKEN"], "check": lambda: any("SUPABASE" in k for k in cred_keys_upper)},
        {"service": "Google Workspace", "service_id": "google", "patterns": ["docs.google.com", "sheets.google.com", "slides.google.com",
         "drive.google.com", "google doc", "google sheet", "google slide", "spreadsheet"],
         "keys": ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"],
         "check": lambda: all(k in cred_keys_upper for k in ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"])},
    ]

    missing = []
    for svc in service_checks:
        if any(p in text_lower for p in svc["patterns"]):
            if not svc["check"]():
                missing.append({"service": svc["service"], "keys": svc["keys"], "service_id": svc["service_id"]})
    return missing


def get_mcp_env(ctx):
    """Build environment dict for MCP processes with credentials injected."""
    env = os.environ.copy()
    env.update(load_credentials(ctx))
    return env
