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
    # stdout is redirected to log file by start.py, and hub also writes to the same file
    # via _append_log_disk — only write to buffer (hub push) to avoid duplicate lines
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


# ── MCP Tool Humanization ──

_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)

# (display_name, category) — category drives frontend badge color
_MCP_LABELS = {
    # Atlassian / Jira
    "mcp__atlassian__getJiraIssue": ("Jira: Get Issue", "jira"),
    "mcp__atlassian__searchJiraIssuesUsingJql": ("Jira: Search Issues", "jira"),
    "mcp__atlassian__createJiraIssue": ("Jira: Create Issue", "jira"),
    "mcp__atlassian__editJiraIssue": ("Jira: Edit Issue", "jira"),
    "mcp__atlassian__addCommentToJiraIssue": ("Jira: Add Comment", "jira"),
    "mcp__atlassian__getTransitionsForJiraIssue": ("Jira: Get Transitions", "jira"),
    "mcp__atlassian__transitionJiraIssue": ("Jira: Transition Issue", "jira"),
    "mcp__atlassian__addWorklogToJiraIssue": ("Jira: Add Worklog", "jira"),
    "mcp__atlassian__getJiraIssueRemoteIssueLinks": ("Jira: Remote Links", "jira"),
    "mcp__atlassian__getJiraIssueTypeMetaWithFields": ("Jira: Issue Type Meta", "jira"),
    "mcp__atlassian__getJiraProjectIssueTypesMetadata": ("Jira: Project Types", "jira"),
    "mcp__atlassian__getVisibleJiraProjects": ("Jira: List Projects", "jira"),
    "mcp__atlassian__lookupJiraAccountId": ("Jira: Lookup User", "jira"),
    "mcp__atlassian__search": ("Atlassian: Search", "jira"),
    # Atlassian / Confluence
    "mcp__atlassian__getConfluencePage": ("Confluence: Get Page", "confluence"),
    "mcp__atlassian__createConfluencePage": ("Confluence: Create Page", "confluence"),
    "mcp__atlassian__updateConfluencePage": ("Confluence: Update Page", "confluence"),
    "mcp__atlassian__searchConfluenceUsingCql": ("Confluence: Search", "confluence"),
    "mcp__atlassian__getConfluenceSpaces": ("Confluence: List Spaces", "confluence"),
    "mcp__atlassian__getPagesInConfluenceSpace": ("Confluence: Space Pages", "confluence"),
    "mcp__atlassian__getConfluencePageDescendants": ("Confluence: Descendants", "confluence"),
    "mcp__atlassian__getConfluencePageFooterComments": ("Confluence: Footer Comments", "confluence"),
    "mcp__atlassian__getConfluencePageInlineComments": ("Confluence: Inline Comments", "confluence"),
    "mcp__atlassian__getConfluenceCommentChildren": ("Confluence: Comment Children", "confluence"),
    "mcp__atlassian__createConfluenceFooterComment": ("Confluence: Add Footer Comment", "confluence"),
    "mcp__atlassian__createConfluenceInlineComment": ("Confluence: Add Inline Comment", "confluence"),
    "mcp__atlassian__atlassianUserInfo": ("Atlassian: User Info", "jira"),
    "mcp__atlassian__getAccessibleAtlassianResources": ("Atlassian: Resources", "jira"),
    "mcp__atlassian__fetch": ("Atlassian: Fetch", "jira"),
    # GitHub
    "mcp__github__get_issue": ("GitHub: Get Issue", "github"),
    "mcp__github__list_issues": ("GitHub: List Issues", "github"),
    "mcp__github__create_issue": ("GitHub: Create Issue", "github"),
    "mcp__github__update_issue": ("GitHub: Update Issue", "github"),
    "mcp__github__add_issue_comment": ("GitHub: Comment on Issue", "github"),
    "mcp__github__search_issues": ("GitHub: Search Issues", "github"),
    "mcp__github__get_pull_request": ("GitHub: Get PR", "github"),
    "mcp__github__list_pull_requests": ("GitHub: List PRs", "github"),
    "mcp__github__create_pull_request": ("GitHub: Create PR", "github"),
    "mcp__github__get_pull_request_files": ("GitHub: PR Files", "github"),
    "mcp__github__get_pull_request_comments": ("GitHub: PR Comments", "github"),
    "mcp__github__get_pull_request_reviews": ("GitHub: PR Reviews", "github"),
    "mcp__github__get_pull_request_status": ("GitHub: PR Status", "github"),
    "mcp__github__create_pull_request_review": ("GitHub: Review PR", "github"),
    "mcp__github__merge_pull_request": ("GitHub: Merge PR", "github"),
    "mcp__github__update_pull_request_branch": ("GitHub: Update PR Branch", "github"),
    "mcp__github__get_file_contents": ("GitHub: Get File", "github"),
    "mcp__github__create_or_update_file": ("GitHub: Write File", "github"),
    "mcp__github__push_files": ("GitHub: Push Files", "github"),
    "mcp__github__list_commits": ("GitHub: List Commits", "github"),
    "mcp__github__create_branch": ("GitHub: Create Branch", "github"),
    "mcp__github__search_code": ("GitHub: Search Code", "github"),
    "mcp__github__search_repositories": ("GitHub: Search Repos", "github"),
    "mcp__github__search_users": ("GitHub: Search Users", "github"),
    "mcp__github__create_repository": ("GitHub: Create Repo", "github"),
    "mcp__github__fork_repository": ("GitHub: Fork Repo", "github"),
    # Figma
    "mcp__figma__get_design_context": ("Figma: Get Design", "figma"),
    "mcp__figma__get_screenshot": ("Figma: Screenshot", "figma"),
    "mcp__figma__get_metadata": ("Figma: Metadata", "figma"),
    "mcp__figma__get_figjam": ("Figma: Get FigJam", "figma"),
    "mcp__figma__generate_diagram": ("Figma: Generate Diagram", "figma"),
    "mcp__figma__generate_figma_design": ("Figma: Generate Design", "figma"),
    "mcp__figma__get_code_connect_map": ("Figma: Code Connect Map", "figma"),
    "mcp__figma__get_code_connect_suggestions": ("Figma: Code Suggestions", "figma"),
    "mcp__figma__add_code_connect_map": ("Figma: Add Code Map", "figma"),
    "mcp__figma__send_code_connect_mappings": ("Figma: Send Mappings", "figma"),
    "mcp__figma__create_design_system_rules": ("Figma: Design Rules", "figma"),
    "mcp__figma__get_variable_defs": ("Figma: Variables", "figma"),
    "mcp__figma__whoami": ("Figma: Who Am I", "figma"),
    # Google
    "mcp__google__searchDrive": ("Google: Search Drive", "google"),
    "mcp__google__readGoogleDoc": ("Google: Read Doc", "google"),
    "mcp__google__appendToGoogleDoc": ("Google: Append Doc", "google"),
    "mcp__google__editGoogleDoc": ("Google: Edit Doc", "google"),
    "mcp__google__createDocument": ("Google: Create Doc", "google"),
    "mcp__google__readSpreadsheet": ("Google: Read Sheet", "google"),
    "mcp__google__writeSpreadsheet": ("Google: Write Sheet", "google"),
    "mcp__google__createSpreadsheet": ("Google: Create Sheet", "google"),
    "mcp__google__searchGmail": ("Google: Search Gmail", "google"),
    "mcp__google__readGmailMessage": ("Google: Read Email", "google"),
    "mcp__google__createGmailDraft": ("Google: Draft Email", "google"),
    "mcp__google__listCalendarEvents": ("Google: List Events", "google"),
    "mcp__google__createCalendarEvent": ("Google: Create Event", "google"),
    "mcp__google__listRecentFiles": ("Google: Recent Files", "google"),
    # Sentry
    "mcp__sentry__search_issues": ("Sentry: Search Issues", "sentry"),
    "mcp__sentry__get_issue_details": ("Sentry: Issue Details", "sentry"),
    "mcp__sentry__search_events": ("Sentry: Search Events", "sentry"),
    "mcp__sentry__search_issue_events": ("Sentry: Issue Events", "sentry"),
    "mcp__sentry__get_trace_details": ("Sentry: Trace Details", "sentry"),
    "mcp__sentry__get_issue_tag_values": ("Sentry: Tag Values", "sentry"),
    "mcp__sentry__get_event_attachment": ("Sentry: Attachment", "sentry"),
    "mcp__sentry__analyze_issue_with_seer": ("Sentry: AI Analysis", "sentry"),
    "mcp__sentry__find_organizations": ("Sentry: Organizations", "sentry"),
    "mcp__sentry__find_projects": ("Sentry: Projects", "sentry"),
    "mcp__sentry__find_releases": ("Sentry: Releases", "sentry"),
    "mcp__sentry__find_teams": ("Sentry: Teams", "sentry"),
    "mcp__sentry__whoami": ("Sentry: Who Am I", "sentry"),
    # Context7
    "mcp__context7__resolve-library-id": ("Context7: Resolve Library", "context7"),
    "mcp__context7__query-docs": ("Context7: Query Docs", "context7"),
    # Playwright
    "mcp__playwright__browser_navigate": ("Browser: Navigate", "playwright"),
    "mcp__playwright__browser_click": ("Browser: Click", "playwright"),
    "mcp__playwright__browser_fill_form": ("Browser: Fill Form", "playwright"),
    "mcp__playwright__browser_snapshot": ("Browser: Snapshot", "playwright"),
    "mcp__playwright__browser_take_screenshot": ("Browser: Screenshot", "playwright"),
    "mcp__playwright__browser_evaluate": ("Browser: Evaluate", "playwright"),
    "mcp__playwright__browser_type": ("Browser: Type", "playwright"),
    "mcp__playwright__browser_press_key": ("Browser: Press Key", "playwright"),
    "mcp__playwright__browser_hover": ("Browser: Hover", "playwright"),
    "mcp__playwright__browser_select_option": ("Browser: Select", "playwright"),
    "mcp__playwright__browser_tabs": ("Browser: Tabs", "playwright"),
    "mcp__playwright__browser_close": ("Browser: Close", "playwright"),
    "mcp__playwright__browser_wait_for": ("Browser: Wait", "playwright"),
    "mcp__playwright__browser_console_messages": ("Browser: Console", "playwright"),
    "mcp__playwright__browser_network_requests": ("Browser: Network", "playwright"),
    "mcp__playwright__browser_navigate_back": ("Browser: Back", "playwright"),
    "mcp__playwright__browser_drag": ("Browser: Drag", "playwright"),
    "mcp__playwright__browser_file_upload": ("Browser: Upload", "playwright"),
    "mcp__playwright__browser_handle_dialog": ("Browser: Dialog", "playwright"),
    "mcp__playwright__browser_resize": ("Browser: Resize", "playwright"),
    "mcp__playwright__browser_install": ("Browser: Install", "playwright"),
    "mcp__playwright__browser_run_code": ("Browser: Run Code", "playwright"),
    # Sequential thinking
    "mcp__sequentialthinking__sequentialthinking": ("Sequential Thinking", "thinking"),
}

# Detail extraction: which input key to show per service
_MCP_DETAIL_KEYS = {
    "jira": ["issueIdOrKey", "issueKey", "jql", "query", "projectKey"],
    "confluence": ["pageId", "spaceKey", "cql", "query", "title"],
    "github": ["issue_number", "pull_number", "repo", "owner", "query", "path", "branch"],
    "figma": ["fileKey", "nodeId", "url"],
    "google": ["query", "documentId", "spreadsheetId", "messageId", "calendarId"],
    "sentry": ["issue_id", "query", "organization_slug", "project_slug"],
    "context7": ["libraryName", "query", "topic"],
    "playwright": ["url", "selector", "value", "text"],
    "thinking": ["thought"],
}


def _auto_parse_mcp_name(tool_name):
    """Fallback: parse mcp__service__method into ('Service: Method', 'service')."""
    parts = tool_name.split("__")
    if len(parts) >= 3 and parts[0] == "mcp":
        service = parts[1].capitalize()
        method = "_".join(parts[2:]).replace("_", " ").title()
        return (f"{service}: {method}", parts[1])
    return (tool_name, "mcp")


def humanize_mcp_tool(tool_name, inp=None):
    """Return (display_name, detail, category) for an MCP tool call."""
    if tool_name in _MCP_LABELS:
        display, category = _MCP_LABELS[tool_name]
    else:
        display, category = _auto_parse_mcp_name(tool_name)

    detail = ""
    if inp and isinstance(inp, dict):
        keys = _MCP_DETAIL_KEYS.get(category, [])
        for k in keys:
            v = inp.get(k)
            if v and isinstance(v, str) and not _UUID_RE.match(v):
                detail = v[:120]
                break
        # Fallback: first non-UUID string value
        if not detail:
            for v in inp.values():
                if isinstance(v, str) and len(v) > 2 and not _UUID_RE.match(v):
                    detail = v[:120]
                    break
    return display, detail, category


# Noise tools: suppress from logs entirely
_NOISE_TOOLS = {"ToolSearch"}


def is_noise_tool(tool_name):
    """Return True if this tool call should be hidden from logs."""
    return tool_name in _NOISE_TOOLS


def format_tokens_comma(n):
    """Format token count with commas: 1234 → '1,234'."""
    return f"{n:,}" if isinstance(n, int) else str(n)
