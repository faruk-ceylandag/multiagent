#!/usr/bin/env python3
"""Setup MCP servers for Claude Code agents.

Configures MCP servers at user level (~/.claude.json) so all agent sessions can use them.
Each agent's session also gets project-level .mcp.json in its CWD.
"""
import json, os, subprocess, sys

# ── MCP Server Registry (Official packages only) ──
MCP_SERVERS = {
    "github": {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "description": "GitHub PR/issue/branch management",
        "required_env": ["GITHUB_PERSONAL_ACCESS_TOKEN"],
        "install_cmd": 'claude mcp add github -e GITHUB_PERSONAL_ACCESS_TOKEN -- npx -y @modelcontextprotocol/server-github',
    },
    "context7": {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@upstash/context7-mcp@latest"],
        "description": "Up-to-date library documentation",
        "required_env": [],
        "install_cmd": 'claude mcp add context7 -- npx -y @upstash/context7-mcp@latest',
    },
    "sentry": {
        "type": "http",
        "url": "https://mcp.sentry.dev/mcp",
        "description": "Error tracking & production debugging (OAuth)",
        "required_env": [],
        "install_cmd": 'claude mcp add --transport http sentry https://mcp.sentry.dev/mcp',
    },
    "sequentialthinking": {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
        "description": "Complex task decomposition & planning",
        "required_env": [],
        "install_cmd": 'claude mcp add sequentialthinking -- npx -y @modelcontextprotocol/server-sequential-thinking',
    },
    "figma": {
        "type": "http",
        "url": "https://mcp.figma.com/mcp",
        "description": "Figma design file access, component inspection, style extraction (OAuth)",
        "required_env": [],
        "install_cmd": 'claude mcp add --transport http figma https://mcp.figma.com/mcp',
    },
    "google": {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "google-workspace-mcp", "serve"],
        "description": "Google Docs, Sheets, Slides, Drive, Gmail, Calendar access",
        "required_env": ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"],
        "install_cmd": 'claude mcp add google -- npx -y google-workspace-mcp serve',
    },
    "atlassian": {
        "type": "sse",
        "url": "https://mcp.atlassian.com/v1/sse",
        "description": "Jira & Confluence — issue CRUD, search, transitions, pages (OAuth)",
        "required_env": [],
        "install_cmd": 'claude mcp add --transport sse atlassian https://mcp.atlassian.com/v1/sse',
    },
    "playwright": {
        "type": "stdio",
        "command": "npx",
        "args": ["@playwright/mcp@latest"],
        "description": "Browser automation — navigate, interact, screenshot, generate Playwright tests",
        "required_env": [],
        "install_cmd": 'claude mcp add playwright -- npx @playwright/mcp@latest',
    },
    "chrome-devtools": {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "chrome-devtools-mcp@latest"],
        "description": "Chrome DevTools — page inspection, DOM queries, network monitoring, console access",
        "required_env": [],
        "install_cmd": 'claude mcp add chrome-devtools -- npx -y chrome-devtools-mcp@latest',
    },
    "memory": {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-memory"],
        "description": "Persistent cross-session memory — agents recall decisions, patterns, and context across restarts",
        "required_env": [],
        "install_cmd": 'claude mcp add memory -- npx -y @modelcontextprotocol/server-memory',
    },
}

def get_enabled_mcps(cfg):
    """Get list of enabled MCP server names from config."""
    if "mcp_servers" not in cfg:
        return dict(MCP_SERVERS)  # default: all enabled
    enabled = cfg["mcp_servers"]
    if not enabled:  # empty dict/list → use all defaults
        return dict(MCP_SERVERS)
    if isinstance(enabled, list):
        return {k: v for k, v in MCP_SERVERS.items() if k in enabled}
    if isinstance(enabled, dict):
        return {k: v for k, v in MCP_SERVERS.items() if k in enabled}
    return dict(MCP_SERVERS)

def generate_mcp_json(cfg, credentials=None):
    """Generate .mcp.json content for agent CWD / project level.
    Only includes stdio servers — HTTP/SSE servers are registered at user level
    via 'claude mcp add --scope user' to avoid duplicate connections."""
    creds = credentials or {}
    enabled = get_enabled_mcps(cfg)
    servers = {}
    for name, spec in enabled.items():
        # Skip HTTP/SSE servers — they're registered at user level (claude mcp add --scope user)
        # Having them in BOTH .mcp.json and ~/.claude.json causes duplicate connections
        # that can break MCP tool discovery (ToolSearch returns nothing)
        if spec["type"] in ("http", "sse"):
            continue
        entry = {"type": spec["type"]}
        if spec["type"] == "stdio":
            entry["command"] = spec["command"]
            entry["args"] = spec["args"]
        # Inject env vars: check credentials dict first, then aliases, then os.environ
        env = {}
        aliases = spec.get("env_aliases", {})
        for var in spec.get("required_env", []):
            val = creds.get(var, "") or os.environ.get(var, "")
            if not val and var in aliases:
                alias = aliases[var]
                val = creds.get(alias, "") or os.environ.get(alias, "")
            # Fallback: ATLASSIAN_URL ← JIRA_BASE_URL (legacy credential name)
            if not val and var == "ATLASSIAN_URL":
                val = creds.get("JIRA_BASE_URL", "") or os.environ.get("JIRA_BASE_URL", "")
            # Fallback: GITHUB_PERSONAL_ACCESS_TOKEN ← GITHUB_TOKEN (legacy)
            if not val and var == "GITHUB_PERSONAL_ACCESS_TOKEN":
                val = creds.get("GITHUB_TOKEN", "") or os.environ.get("GITHUB_TOKEN", "")
            if val:
                env[var] = val
        if env:
            entry["env"] = env
        servers[name] = entry
    return {"mcpServers": servers}

def write_mcp_json(path, cfg, credentials=None):
    """Write .mcp.json to a directory."""
    mcp_data = generate_mcp_json(cfg, credentials)
    mcp_path = os.path.join(path, ".mcp.json")
    with open(mcp_path, "w") as f:
        json.dump(mcp_data, f, indent=2)
    return mcp_path

def setup_user_level_mcp(cfg):
    """Install MCP servers at user level using claude CLI."""
    enabled = get_enabled_mcps(cfg)
    results = []
    for name, spec in enabled.items():
        # Check if required env vars exist
        missing = [v for v in spec.get("required_env", []) if not os.environ.get(v)]
        if missing:
            results.append(f"⚠ {name}: missing {', '.join(missing)} — skipped")
            continue
        try:
            cmd = spec["install_cmd"]
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                results.append(f"✓ {name}: installed")
            else:
                # May already exist, not an error
                results.append(f"~ {name}: {r.stderr.strip()[:80] or 'already configured'}")
        except Exception as e:
            results.append(f"✗ {name}: {e}")
    return results

def get_mcp_tools_hint(cfg):
    """Generate a hint string for agent prompts about available MCP tools."""
    enabled = get_enabled_mcps(cfg)
    if not enabled:
        return ""
    lines = ["MCP TOOLS AVAILABLE:"]
    for name, spec in enabled.items():
        lines.append(f"  • {name}: {spec['description']}")
    lines.append("Use these tools naturally when relevant. Say 'use context7' for docs lookup.")
    return "\n".join(lines)

if __name__ == "__main__":
    # CLI usage: python setup_mcp.py [config.json] [target_dir]
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "multiagent.json"
    target = sys.argv[2] if len(sys.argv) > 2 else "."
    try:
        with open(cfg_path) as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError):
        cfg = {}
    print("Setting up MCP servers...")
    for r in setup_user_level_mcp(cfg):
        print(f"  {r}")
    p = write_mcp_json(target, cfg)
    print(f"Wrote {p}")
