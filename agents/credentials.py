"""agents/credentials.py — Credential management (load, save, check).

E6 fix: credentials are encrypted at rest using a machine-local key derived
from hostname + username.  Legacy plaintext values are auto-migrated on load.
E7 fix: optional expiration tracking via .credentials_meta.json.
"""
import base64
import getpass
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone


# ---------------------------------------------------------------------------
# Encryption helpers (XOR with PBKDF2-derived machine-local key)
# ---------------------------------------------------------------------------

def _derive_key():
    """Derive a machine-local encryption key (deterministic, not stored)."""
    material = f"{os.uname().nodename}:{getpass.getuser()}:multiagent-creds-v1"
    return hashlib.pbkdf2_hmac("sha256", material.encode(), b"ma-salt-2026", 100000)


_ENC_PREFIX = "ENC:"


def _encrypt(plaintext: str, key: bytes) -> str:
    """XOR encrypt and base64 encode."""
    data = plaintext.encode("utf-8")
    encrypted = bytes(b ^ key[i % len(key)] for i, b in enumerate(data))
    return base64.b64encode(encrypted).decode("ascii")


def _decrypt(ciphertext: str, key: bytes) -> str:
    """Base64 decode and XOR decrypt."""
    data = base64.b64decode(ciphertext)
    decrypted = bytes(b ^ key[i % len(key)] for i, b in enumerate(data))
    return decrypted.decode("utf-8")


def load_credentials(ctx):
    """Load saved credentials from .multiagent/credentials.env

    Encrypted values (prefixed with ``ENC:``) are decrypted transparently.
    Any legacy plaintext values found are auto-migrated to encrypted form.
    """
    creds = {}
    if not ctx.CREDS_FILE or not os.path.exists(ctx.CREDS_FILE):
        return creds

    key = _derive_key()
    has_plaintext = False

    try:
        with open(ctx.CREDS_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip()
                    if v.startswith(_ENC_PREFIX):
                        # Decrypt encrypted value
                        creds[k] = _decrypt(v[len(_ENC_PREFIX):], key)
                    else:
                        # Legacy plaintext — flag for migration
                        creds[k] = v
                        has_plaintext = True
    except (OSError, ValueError):
        pass

    # Auto-migrate: re-save with encryption if any plaintext values were found
    if has_plaintext and creds:
        _save_credentials_raw(ctx, creds)

    return creds


def _save_credentials_raw(ctx, creds):
    """Write *all* credentials to disk, encrypting every value.

    This is the single write-path so encryption is always applied.
    """
    if not ctx.CREDS_FILE:
        return False
    key = _derive_key()
    try:
        with open(ctx.CREDS_FILE, "w") as f:
            f.write("# Multi-Agent Credentials (auto-saved, encrypted)\n")
            for k, v in sorted(creds.items()):
                enc_value = _ENC_PREFIX + _encrypt(v, key)
                f.write(f"{k}={enc_value}\n")
        os.chmod(ctx.CREDS_FILE, 0o600)
        return True
    except Exception:
        return False


def save_credential(ctx, key, value, expires_at=None):
    """Save a credential to the credentials store (encrypted at rest).

    *expires_at* is an optional ISO-8601 timestamp (str or datetime).  When
    provided the expiry is recorded in ``.credentials_meta.json`` next to
    ``credentials.env`` so that ``check_expiring_credentials()`` can warn
    about tokens that are about to expire (E7 fix).
    """
    from .log_utils import log
    if not ctx.CREDS_FILE:
        return False
    creds = load_credentials(ctx)
    creds[key] = value
    try:
        if not _save_credentials_raw(ctx, creds):
            raise OSError("_save_credentials_raw failed")
        log(ctx, f"🔑 Saved credential: {key}")

        # E7 fix: persist expiration metadata if provided
        if expires_at is not None:
            _save_credential_expiry(ctx, key, expires_at)

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


# ---------------------------------------------------------------------------
# E7 fix: credential expiration tracking
# ---------------------------------------------------------------------------

def _meta_path(ctx):
    """Path to the credentials metadata file (next to credentials.env)."""
    if not ctx.CREDS_FILE:
        return ""
    return os.path.join(os.path.dirname(ctx.CREDS_FILE), ".credentials_meta.json")


def _load_meta(ctx):
    """Load the credentials metadata dict (expiry info, etc.)."""
    path = _meta_path(ctx)
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_meta(ctx, meta):
    """Persist the credentials metadata dict."""
    path = _meta_path(ctx)
    if not path:
        return
    try:
        with open(path, "w") as f:
            json.dump(meta, f, indent=2)
        os.chmod(path, 0o600)
    except OSError:
        pass


def _save_credential_expiry(ctx, key, expires_at):
    """Store an expiration timestamp for *key* in the meta file."""
    if isinstance(expires_at, datetime):
        expires_at = expires_at.isoformat()
    meta = _load_meta(ctx)
    meta.setdefault("expiry", {})[key] = expires_at
    _save_meta(ctx, meta)


def check_expiring_credentials(ctx, warn_hours=24):
    """E7 fix: log warnings about credentials expiring within *warn_hours*.

    Designed to be called periodically from the worker main loop (e.g. at
    each task start).  Returns a list of (key, expires_at_str) pairs for
    credentials that are about to expire.
    """
    from .log_utils import log
    meta = _load_meta(ctx)
    expiry_map = meta.get("expiry", {})
    if not expiry_map:
        return []

    now = datetime.now(timezone.utc)
    threshold = now + timedelta(hours=warn_hours)
    expiring = []

    for key, exp_str in expiry_map.items():
        try:
            exp_dt = datetime.fromisoformat(exp_str)
            # Ensure timezone-aware comparison
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            if exp_dt <= now:
                log(ctx, f"⚠ Credential EXPIRED: {key} (expired {exp_str})")
                expiring.append((key, exp_str))
            elif exp_dt <= threshold:
                hours_left = (exp_dt - now).total_seconds() / 3600
                log(ctx, f"⚠ Credential expiring soon: {key} ({hours_left:.1f}h left, expires {exp_str})")
                expiring.append((key, exp_str))
        except (ValueError, TypeError):
            # Malformed date — skip silently
            pass

    return expiring


def get_mcp_env(ctx):
    """Build environment dict for MCP processes with credentials injected."""
    env = os.environ.copy()
    env.update(load_credentials(ctx))
    return env
