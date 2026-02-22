#!/usr/bin/env python3
"""Generate Claude Code hooks configuration based on project stack.

Hooks are deterministic actions that run at specific points in Claude's lifecycle:
- PreToolUse: Before a tool executes (can block with exit code 2)
- PostToolUse: After a tool executes (auto-format, lint, etc.)
- Notification: Alert when something happens
"""
import json, os, sys

def generate_hooks_config(stack_info, hub_url="http://127.0.0.1:8040", agent_name="agent"):
    """Generate hooks JSON config based on project stack."""
    hooks = {"PreToolUse": [], "PostToolUse": []}

    # ── PreToolUse: File Lock Check ──
    hooks["PreToolUse"].append({
        "matcher": "Write|Edit",
        "hooks": [{
            "type": "command",
            "command": f"""python3 -c "
import sys,json,urllib.request
try:
    inp=json.load(sys.stdin)
    fp=inp.get('tool_input',{{}}).get('file_path','')
    if not fp: sys.exit(0)
    r=json.loads(urllib.request.urlopen('{hub_url}/files/locks',timeout=2).read())
    lock=r.get(fp,{{}})
    if lock and lock.get('agent','')!='{agent_name}':
        print(f'BLOCKED: {{fp}} locked by {{lock[\"agent\"]}}',file=sys.stderr)
        sys.exit(2)
except Exception: pass
" """
        }]
    })

    # ── PreToolUse: Block sensitive file edits ──
    hooks["PreToolUse"].append({
        "matcher": "Write|Edit",
        "hooks": [{
            "type": "command",
            "command": """python3 -c "
import sys,json
try:
    inp=json.load(sys.stdin)
    fp=inp.get('tool_input',{}).get('file_path','')
    if not fp: sys.exit(0)
    blocked = ['credentials.env','.env','secrets','id_rsa','id_ed25519']
    import os
    base = os.path.basename(fp)
    if any(base == b or base.startswith(b) for b in blocked):
        print(f'BLOCKED: {base} is a sensitive file — use /credentials endpoint or dashboard instead',file=sys.stderr)
        sys.exit(2)
except Exception: pass
" """
        }]
    })

    # ── PostToolUse: Auto-Format by file type ──
    formatters = []

    # Python: black
    if _has_lang(stack_info, "python"):
        formatters.append({
            "matcher": "Write(*.py)|Edit(*.py)",
            "hooks": [{"type": "command", "command": 'python3 -m black --quiet "$CLAUDE_FILE_PATH" 2>/dev/null || true'}]
        })

    # JS/TS: prettier
    if _has_lang(stack_info, "javascript", "typescript"):
        formatters.append({
            "matcher": "Write(*.js)|Write(*.ts)|Write(*.jsx)|Write(*.tsx)|Write(*.vue)|Edit(*.js)|Edit(*.ts)|Edit(*.jsx)|Edit(*.tsx)|Edit(*.vue)",
            "hooks": [{"type": "command", "command": 'npx prettier --write "$CLAUDE_FILE_PATH" 2>/dev/null || true'}]
        })

    # PHP: php-cs-fixer or pint
    if _has_lang(stack_info, "php"):
        formatters.append({
            "matcher": "Write(*.php)|Edit(*.php)",
            "hooks": [{"type": "command", "command": 'cd "$(dirname "$CLAUDE_FILE_PATH")" && (./vendor/bin/pint "$CLAUDE_FILE_PATH" 2>/dev/null || php-cs-fixer fix "$CLAUDE_FILE_PATH" 2>/dev/null || true)'}]
        })

    # Go: gofmt
    if _has_lang(stack_info, "go"):
        formatters.append({
            "matcher": "Write(*.go)|Edit(*.go)",
            "hooks": [{"type": "command", "command": 'gofmt -w "$CLAUDE_FILE_PATH" 2>/dev/null || true'}]
        })

    hooks["PostToolUse"].extend(formatters)

    # ── PostToolUse: Auto-Lint ──
    linters = []

    if _has_lang(stack_info, "javascript", "typescript"):
        linters.append({
            "matcher": "Write(*.js)|Write(*.ts)|Write(*.jsx)|Write(*.tsx)|Write(*.vue)",
            "hooks": [{"type": "command", "command": 'npx eslint --fix "$CLAUDE_FILE_PATH" 2>/dev/null || true'}]
        })

    if _has_lang(stack_info, "php"):
        linters.append({
            "matcher": "Write(*.php)|Edit(*.php)",
            "hooks": [{"type": "command", "command": 'cd "$(git -C "$(dirname "$CLAUDE_FILE_PATH")" rev-parse --show-toplevel 2>/dev/null || dirname "$CLAUDE_FILE_PATH")" && ./vendor/bin/phpstan analyse "$CLAUDE_FILE_PATH" --level=5 --no-progress 2>/dev/null | tail -5 || true'}]
        })

    if _has_lang(stack_info, "go"):
        linters.append({
            "matcher": "Write(*.go)|Edit(*.go)",
            "hooks": [{"type": "command", "command": 'cd "$(dirname "$CLAUDE_FILE_PATH")" && go vet ./... 2>&1 | tail -5 || true'}]
        })

    if _has_lang(stack_info, "python"):
        linters.append({
            "matcher": "Write(*.py)|Edit(*.py)",
            "hooks": [{"type": "command", "command": 'python3 -m ruff check --fix "$CLAUDE_FILE_PATH" 2>/dev/null || python3 -m flake8 "$CLAUDE_FILE_PATH" --max-line-length=120 2>/dev/null | tail -5 || true'}]
        })

    hooks["PostToolUse"].extend(linters)

    # ── PostToolUse: Run related tests on edit ──
    if _has_lang(stack_info, "javascript", "typescript"):
        hooks["PostToolUse"].append({
            "matcher": "Write(*.ts)|Write(*.js)|Write(*.tsx)|Write(*.jsx)|Write(*.vue)|Edit(*.ts)|Edit(*.js)|Edit(*.tsx)|Edit(*.jsx)|Edit(*.vue)",
            "hooks": [{
                "type": "command",
                "command": 'FILE="$CLAUDE_FILE_PATH"; case "$FILE" in *.spec.*|*.test.*) exit 0;; esac; DIR="$(git -C "$(dirname "$FILE")" rev-parse --show-toplevel 2>/dev/null || dirname "$FILE")"; cd "$DIR" && npx vitest run --reporter=dot --findRelatedTests "$FILE" 2>/dev/null || npx jest --findRelatedTests "$FILE" --passWithNoTests 2>/dev/null || true',
                "timeout": 30000
            }]
        })

    if _has_lang(stack_info, "python"):
        hooks["PostToolUse"].append({
            "matcher": "Write(*.py)|Edit(*.py)",
            "hooks": [{
                "type": "command",
                "command": 'FILE="$CLAUDE_FILE_PATH"; case "$FILE" in *test*) exit 0;; esac; DIR="$(dirname "$FILE")"; TESTFILE="${FILE%.py}_test.py"; [ -f "$TESTFILE" ] && cd "$DIR" && python3 -m pytest "$TESTFILE" -x -q 2>/dev/null | tail -5 || true',
                "timeout": 30000
            }]
        })

    # ── Notification Hook: Task completion → Hub webhook ──
    hooks["PostToolUse"].append({
        "matcher": "Bash",
        "hooks": [{
            "type": "command",
            "command": f"""python3 -c "
import sys,json
try:
    inp=json.load(sys.stdin)
    stdout=inp.get('tool_output',{{}}).get('stdout','')
    # Only notify on significant completions
    if any(k in stdout.lower() for k in ['all tests passed','build successful','lgtm','✓ pass']):
        import urllib.request
        payload=json.dumps({{'sender':'{agent_name}','receiver':'user','content':'✅ '+stdout[:100],'msg_type':'info'}}).encode()
        urllib.request.urlopen(urllib.request.Request('{hub_url}/messages',data=payload,headers={{'Content-Type':'application/json'}}),timeout=2)
except Exception: pass
" """
        }]
    })

    return hooks


def _has_lang(stack_info, *langs):
    """Check if any project in stack uses given language(s)."""
    if isinstance(stack_info, dict):
        for proj, info in stack_info.items():
            proj_langs = info.get("lang", [])
            if isinstance(proj_langs, list):
                for l in langs:
                    if l in proj_langs:
                        return True
    return False


def generate_settings_json(hooks, permissions=None, agent_name=""):
    """Generate a complete settings.json for an agent's .claude directory."""
    # Import MCP server names for permissions and enabledMcpjsonServers
    try:
        from ecosystem.mcp.setup_mcp import MCP_SERVERS
        all_mcp_names = list(MCP_SERVERS.keys())
        # Only stdio servers go in .mcp.json — HTTP/SSE are user-level only
        enabled_mcp = [name for name, spec in MCP_SERVERS.items()
                       if spec.get("type", "stdio") == "stdio"]
    except ImportError:
        all_mcp_names = []
        enabled_mcp = []
    # Build explicit MCP permission patterns: mcp__atlassian__*, mcp__github__*, etc.
    # The wildcard "mcp__*" doesn't work in Claude Code's permission system
    mcp_perms = [f"mcp__{name}__*" for name in all_mcp_names]
    # Architect: read-only, no Edit/Write — forces delegation
    if agent_name == "architect" and not permissions:
        permissions = {
            "allow": ["Read", "Bash(curl*)", "Bash(cat*)", "Bash(ls*)", "Bash(find*)",
                       "Bash(grep*)", "Bash(head*)", "Bash(tail*)", "Bash(wc*)"] + mcp_perms,
            "deny": ["Edit", "Write"]
        }
    settings = {
        "permissions": permissions or {
            "allow": ["Edit", "Write", "Read", "Bash(*)"] + mcp_perms,
            "deny": []
        },
        "hooks": hooks,
    }
    if enabled_mcp:
        settings["enabledMcpjsonServers"] = enabled_mcp
    return settings


def write_agent_hooks(agent_cwd, stack_info, hub_url, agent_name):
    """Write hooks to agent's .claude/settings.json"""
    hooks = generate_hooks_config(stack_info, hub_url, agent_name)
    settings = generate_settings_json(hooks, agent_name=agent_name)

    claude_dir = os.path.join(agent_cwd, ".claude")
    os.makedirs(claude_dir, exist_ok=True)

    settings_path = os.path.join(claude_dir, "settings.json")
    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)

    return settings_path


if __name__ == "__main__":
    # Test: generate hooks for a mixed stack
    test_stack = {
        "frontend": {"lang": ["javascript", "typescript"], "fw": ["vue"]},
        "backend": {"lang": ["php"], "fw": ["laravel"]},
        "service": {"lang": ["go"]},
        "ml": {"lang": ["python"]},
    }
    hooks = generate_hooks_config(test_stack)
    print(json.dumps(hooks, indent=2))
