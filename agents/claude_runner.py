"""agents/claude_runner.py — Claude CLI execution with streaming, retries, model selection."""
import os
import json
import subprocess
import threading
import time
import re
import random
from .log_utils import log, humanize_bash, short_path, flush_logs, humanize_mcp_tool, is_noise_tool, format_tokens_comma
from .hub_client import hub_post, hub_msg, report_progress, update_session
from .credentials import load_credentials
from .git_ops import collect_changes, lock_file
from .learning import truncate_context, save_session, track_ecosystem_use


# ── Model Selection ──
# Complexity scoring: higher score → use Opus (powerful), lower → Sonnet (fast/cheap)
_OPUS_SIGNALS = [
    # Heavy coding tasks
    "implement", "create file", "write code", "build", "refactor", "add feature",
    "fix bug", "write test", "add endpoint", "create component", "migration", "scaffold",
    "generate", "setup", "install", "edit file", "modify", "update code", "add function",
    "add method", "write class", "add route", "create model", "css", "html", "database",
    "schema", "api endpoint", "controller", "docker", "deploy",
]
_SONNET_SIGNALS = [
    # Lighter tasks: planning, reviewing, simple checks
    "resuming", "ready?", "confirm", "plan", "design", "architect", "break down",
    "assign", "review", "what should", "how should", "which approach",
    "explain", "describe", "summarize", "question", "check", "list",
]
_HAIKU_SIGNALS = [
    # Cheapest: verification, simple lookups, status checks
    "verify", 'echo "pass"', 'echo "fail"', "verify_result",
]


def _score_complexity(prompt):
    """Score task complexity 0-100. Higher = needs more powerful model."""
    low = prompt.lower()
    score = 0

    # Signal matching
    opus_hits = sum(1 for s in _OPUS_SIGNALS if f" {s}" in f" {low}" or low.startswith(s))
    sonnet_hits = sum(1 for s in _SONNET_SIGNALS if f" {s}" in f" {low}" or low.startswith(s))
    haiku_hits = sum(1 for s in _HAIKU_SIGNALS if f" {s}" in f" {low}" or low.startswith(s))

    if haiku_hits > 0 and opus_hits == 0:
        return 5  # Verification tasks → very low complexity

    score += opus_hits * 8
    score -= sonnet_hits * 5

    # Context size: large prompts usually need more capability
    if len(prompt) > 8000:
        score += 15
    elif len(prompt) > 4000:
        score += 8

    # Multi-file references suggest complex task
    file_refs = len(re.findall(r'[\w/]+\.\w{1,5}\b', low))
    if file_refs > 5:
        score += 15
    elif file_refs > 2:
        score += 8

    # URLs suggest external context (Jira, GitHub, Sentry) — more complex
    url_count = len(re.findall(r'https?://', low))
    if url_count > 0:
        score += 10

    # Multi-scope indicators
    multi_scope = ["and also", "frontend and backend", "full stack", "end to end",
                   "multiple files", "across", "entire", "whole project"]
    if any(ms in low for ms in multi_scope):
        score += 15

    # Messages bundle — only boost if actual task content has opus-level signals
    if "=== MESSAGES ===" in prompt and len(prompt) > 200:
        msg_idx = prompt.find("=== MESSAGES ===")
        task_chunk = prompt[msg_idx:msg_idx + 500].lower() if msg_idx >= 0 else ""
        task_opus_hits = sum(1 for s in _OPUS_SIGNALS if s in task_chunk)
        score += 10 if task_opus_hits > 0 else 5

    # Subtask from architect → could be either, use moderate score
    if "subtask" in low or "delegated" in low:
        score += 5

    return max(0, min(100, score))


def _is_docs_only_task(prompt):
    """Check if task only affects documentation/config files."""
    low = prompt.lower()
    doc_signals = ["readme", "changelog", "documentation", ".md file", "update docs",
                   "add comment", "docstring", "jsdoc", "typedoc", "config file",
                   ".env", ".yaml", ".yml", ".toml", ".json config"]
    code_signals = ["implement", "create file", "write code", "add endpoint", "fix bug",
                    "refactor", "migration", "add feature", "build", "test"]
    has_doc = any(s in low for s in doc_signals)
    has_code = any(s in low for s in code_signals)
    return has_doc and not has_code


def _is_single_file_task(prompt):
    """Check if task likely touches only one file."""
    low = prompt.lower()
    file_refs = re.findall(r'[\w/]+\.\w{1,5}\b', low)
    # Unique file references
    unique_files = set(f for f in file_refs if not f.startswith("http"))
    return len(unique_files) <= 1 and len(prompt) < 3000


def classify_prompt(prompt, role="", model_policy=None):
    """Classify prompt into model tier: opus/sonnet/haiku.
    Role-aware: architect always sonnet, reviewers always haiku, system notifications capped at sonnet."""
    # Architect creates task descriptions & ACs — needs sonnet quality
    if role == "architect":
        return "sonnet"
    # Logic reviewer uses sonnet (catches bugs, race conditions, edge cases)
    # Other reviewers use haiku — simple style/arch checks
    if role and role.startswith("reviewer"):
        if "logic" in role:
            return "sonnet"
        return "haiku"

    # Config-driven model policy: docs_only/single_file tasks → cheaper model
    if model_policy:
        if model_policy.get("docs_only") and _is_docs_only_task(prompt):
            return model_policy["docs_only"]  # e.g. "sonnet"
        if model_policy.get("single_file") and _is_single_file_task(prompt):
            return model_policy["single_file"]  # e.g. "sonnet"

    score = _score_complexity(prompt)
    if score <= 5:
        return "haiku"  # Only explicit verification tasks
    elif score <= 40:
        return "sonnet"
    else:
        return "opus"


def pick_model(ctx, prompt, force=None):
    if force:
        return force
    if ctx.MODEL_OVERRIDE:
        return ctx.MODEL_OVERRIDE
    role = getattr(ctx, "AGENT_ROLE", "")
    policy = getattr(ctx, '_model_policy', None)
    tier = classify_prompt(prompt, role=role, model_policy=policy)
    if tier == "haiku":
        # Use haiku if available, otherwise fall back to sonnet
        return getattr(ctx, "MODEL_HAIKU", ctx.MODEL_SONNET)
    elif tier == "opus":
        return ctx.MODEL_OPUS
    else:
        return ctx.MODEL_SONNET


# ── Rate Limit ──

def check_rl(ctx):
    if time.time() < ctx.rate_limited_until:
        return False, f"rate limited ({int(ctx.rate_limited_until - time.time())}s)"
    return True, ""


def handle_rl(ctx, stderr, code):
    indicators = ["rate limit", "429", "too many requests", "quota exceeded", "usage limit", "overloaded"]
    low = stderr.lower()
    if code != 0 and any(i in low for i in indicators):
        backoff = 60
        m = re.search(r'(\d+)\s*(?:seconds?|s)', low)
        if m:
            backoff = max(30, min(300, int(m.group(1))))
        ctx.rate_limited_until = time.time() + backoff
        log(ctx, f"⚠ Rate limited — {backoff}s")
        hub_msg(ctx, "user", f"⚠ {ctx.AGENT_NAME} rate limited ({backoff}s)", "info")
        hub_post(ctx, "/agents/rate_limited", {"agent_name": ctx.AGENT_NAME,
                                               "until": ctx.rate_limited_until, "backoff": backoff})
        # Report to shared rate pool
        try:
            hub_post(ctx, "/agents/rate_pool/report", {})
        except Exception:
            pass
        # Report circuit failure for dashboard tracking
        try:
            hub_post(ctx, "/agents/tool_event", {
                "agent_name": ctx.AGENT_NAME,
                "event": {"type": "circuit_failure", "tool": "claude_api",
                          "detail": f"Rate limited on {getattr(ctx, 'MODEL_OVERRIDE', '') or 'unknown'}",
                          "timestamp": time.time()}
            })
        except Exception:
            pass
        return True
    return False


def report_usage(ctx, u, model_name=""):
    if not isinstance(u, dict):
        return
    tin, tout = u.get("input_tokens", 0), u.get("output_tokens", 0)
    # Set final token count (not +=) to avoid double-counting with live tracking
    pre = getattr(ctx, '_pre_call_tokens', ctx.session_tokens)
    ctx.session_tokens = pre + tin + tout
    # Accumulate cost estimate for per-call budget enforcement
    # Approximate pricing: input ~$3/M, output ~$15/M (opus ballpark)
    call_cost = (tin * 3.0 + tout * 15.0) / 1_000_000
    ctx._total_cost_so_far = getattr(ctx, '_total_cost_so_far', 0.0) + call_cost
    if tin or tout:
        _task_id = str(getattr(ctx, 'current_task_id', '') or '')
        hub_post(ctx, "/costs/log", {"agent_name": ctx.AGENT_NAME, "tokens_in": tin,
                                     "tokens_out": tout, "model": model_name,
                                     "task_id": _task_id})


# ── Main Call ──

def call_claude(ctx, prompt, retries=5, force_model=None, cwd=None,
                continue_session=False, json_schema=None, system_prompt=None):
    effective_cwd = cwd or ctx.AGENT_CWD
    # Task-level timeout check
    if ctx._task_start_time and ctx.TASK_TIMEOUT > 0:
        elapsed = time.time() - ctx._task_start_time
        if elapsed > ctx.TASK_TIMEOUT:
            log(ctx, f"⏱ Task timeout ({int(elapsed)}s > {ctx.TASK_TIMEOUT}s) — stopping")
            hub_msg(ctx, "user", f"⏱ {ctx.AGENT_NAME}: task timed out after {int(elapsed / 60)} min. Changes preserved, use Retry to continue.", "info")
            return False
    ok, reason = check_rl(ctx)
    if not ok:
        rl_remaining = int(ctx.rate_limited_until - time.time())
        if rl_remaining > 30 and not force_model:
            # Graceful degradation: use Haiku while rate-limited
            log(ctx, f"⏳ {reason} — falling back to Haiku")
            force_model = ctx.MODEL_HAIKU if hasattr(ctx, 'MODEL_HAIKU') else "claude-haiku-4-5-20251001"
        elif rl_remaining > 5:
            log(ctx, f"⏳ {reason} — waiting {rl_remaining}s")
            time.sleep(min(30, rl_remaining))
            return False
        else:
            pass  # Rate limit nearly expired, proceed normally

    from .hub_client import check_budget
    if not check_budget(ctx):
        return False

    ctx.claude_calls += 1
    ctx.task_calls += 1
    model = pick_model(ctx, prompt, force=force_model)
    model_tag = "opus" if "opus" in model else "sonnet" if "sonnet" in model else "haiku" if "haiku" in model else model.split("-")[0]
    prompt = truncate_context(ctx, prompt)
    _model_failed = False
    succeeded = False

    _baseline_tokens = ctx.session_tokens  # safe baseline before any retries

    for attempt in range(1, retries + 1):
        if _model_failed and "opus" in model:
            model = ctx.MODEL_SONNET
            model_tag = "sonnet"
            log(ctx, f"↓ Falling back to {model_tag}")
            _model_failed = False
        elif _model_failed and "haiku" in model:
            model = ctx.MODEL_SONNET
            model_tag = "sonnet"
            log(ctx, f"↑ Escalating from haiku to {model_tag}")
            _model_failed = False
        try:
            log(ctx, f"▶ claude #{ctx.claude_calls} [{model_tag}] ({len(prompt)} chars)")
            report_progress(ctx, "call_start", f"#{ctx.claude_calls} [{model_tag}]")
            ctx._last_output_lines = []
            # Reset to baseline on retries to avoid double-counting failed call's live tokens
            ctx.session_tokens = _baseline_tokens
            ctx._pre_call_tokens = _baseline_tokens

            cmd = ["claude"]
            if ctx.AGENT_NAME == "architect":
                # Architect delegates fast — only MCP reads + curl for plan proposal
                # Block: Glob, Grep, Task, WebFetch, WebSearch, AskUserQuestion, EnterPlanMode
                cmd.extend(["--allowedTools", "Read,Bash(curl*),Bash(jq*),Bash(cat*),mcp__*"])
                cmd.extend(["--disallowedTools", "Glob,Grep,TaskCreate,TaskUpdate,TaskGet,TaskList,WebFetch,WebSearch,AskUserQuestion,EnterPlanMode,ExitPlanMode"])
            elif ctx.AGENT_NAME.startswith("reviewer-"):
                # Reviewers are read-only — no file editing
                cmd.extend(["--allowedTools", "Read,Bash(curl*),Bash(cat*),Bash(git diff*),Bash(git log*),Bash(git show*),mcp__*"])
            else:
                cmd.extend(["--allowedTools", "Edit,Write,Read,Bash(*),mcp__*"])

            # ── Output format: json-schema forces json, otherwise stream-json ──
            if json_schema:
                cmd.extend(["--output-format", "json"])
                cmd.extend(["--json-schema", json_schema])
            else:
                cmd.extend(["--output-format", "stream-json"])
            cmd.append("--verbose")
            cmd.extend(["--model", model])

            # ── Effort level: lower effort for simple tasks saves tokens ──
            role = getattr(ctx, "AGENT_ROLE", "")
            tier = classify_prompt(prompt, role=role)
            if tier == "haiku" and ctx.AGENT_NAME != "architect":
                cmd.extend(["--effort", "low"])
            elif tier == "sonnet" and "opus" not in model:
                cmd.extend(["--effort", "medium"])
            # opus → default high (omit flag)

            # ── Fallback model: automatic downgrade on overload ──
            if "opus" in model:
                cmd.extend(["--fallback-model", ctx.MODEL_SONNET])
            elif "sonnet" in model:
                _haiku = getattr(ctx, "MODEL_HAIKU", "claude-haiku-4-5-20251001")
                cmd.extend(["--fallback-model", _haiku])
            elif "haiku" in model:
                # Haiku can escalate to sonnet as fallback
                cmd.extend(["--fallback-model", ctx.MODEL_SONNET])

            # ── Per-call budget limit ──
            budget = getattr(ctx, 'BUDGET_LIMIT', 0)
            if budget > 0:
                _total_so_far = getattr(ctx, '_total_cost_so_far', 0)
                remaining = max(0, budget - _total_so_far)
                per_call = max(0.10, remaining * 0.25)
                cmd.extend(["--max-budget-usd", str(round(per_call, 2))])

            # ── System prompt injection (static role/contract context) ──
            if system_prompt:
                cmd.extend(["--append-system-prompt", system_prompt])

            # Always run from AGENT_CWD so .claude/ stays in session dir, not in project
            # Give access to project dir via --add-dir
            if effective_cwd != ctx.AGENT_CWD and os.path.isdir(effective_cwd):
                cmd.extend(["--add-dir", effective_cwd])
            # Multi-workspace: add extra directories
            extra_dirs = getattr(ctx, '_extra_dirs', [])
            for d in extra_dirs:
                if os.path.isdir(d):
                    cmd.extend(["--add-dir", d])

            # ── Session continuity ──
            # Use --resume <id> directly; avoid --session-id which requires --fork-session
            _task_sid = None
            if ctx.current_task_id:
                _task_sid = ctx.get_task_session(ctx.current_task_id)

            if continue_session and ctx.valid_sid(ctx.SESSION_ID):
                # --continue resumes last conversation without re-sending context
                cmd.extend(["--continue", "-p", prompt])
            elif _task_sid:
                # Resume the deterministic task session
                cmd.extend(["--resume", _task_sid, "-p", prompt])
            elif ctx.valid_sid(ctx.SESSION_ID):
                cmd.extend(["--resume", ctx.SESSION_ID, "-p", prompt])
            else:
                cmd.extend(["-p", prompt])

            # Advisory rate pool check
            try:
                rp = hub_post(ctx, "/agents/rate_pool/acquire", {})
                if rp and not rp.get("allowed", True):
                    wait = rp.get("wait", 0)
                    if wait > 0:
                        log(ctx, f"⏳ Rate pool: waiting {wait:.1f}s")
                        time.sleep(min(wait, 10))
            except Exception:
                pass

            # Circuit breaker check
            circuit_key = model or "default"
            try:
                from .hub_client import hub_get
                circuits = hub_get(ctx, "/health/circuits") or {}
                cb = circuits.get(circuit_key, {})
                if cb.get("state") == "open":
                    log(ctx, f"⚠ Circuit open for {circuit_key}, trying fallback")
                    if "opus" in circuit_key:
                        model = model.replace("opus", "sonnet")
                        model_tag = "sonnet"
                    elif "sonnet" in circuit_key:
                        model = model.replace("sonnet", "haiku")
                        model_tag = "haiku"
                    # Update cmd model arg
                    for _ci in range(len(cmd)):
                        if cmd[_ci] == "--model" and _ci + 1 < len(cmd):
                            cmd[_ci + 1] = model
                            break
            except Exception:
                pass

            run_env = os.environ.copy()
            run_env.pop("CLAUDECODE", None)  # Prevent nested session detection
            run_env.update(load_credentials(ctx))
            os.makedirs(effective_cwd, exist_ok=True)
            proc = subprocess.Popen(cmd, cwd=ctx.AGENT_CWD, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, bufsize=0,
                                    env=run_env)
            ctx.current_proc = proc
            stderr_lines = []
            out_lines = 0
            tool_count = 0

            def read_stderr():
                for line in proc.stderr:
                    stderr_lines.append(line.decode("utf-8", errors="replace").strip())
                    if len(stderr_lines) > 500:
                        del stderr_lines[:250]

            t = threading.Thread(target=read_stderr, daemon=True)
            t.start()

            for raw in proc.stdout:
                with ctx._stop_lock:
                    should_stop = ctx._should_stop
                if ctx._task_start_time and ctx.TASK_TIMEOUT > 0 and (time.time() - ctx._task_start_time) > ctx.TASK_TIMEOUT:
                    log(ctx, "⏱ Task timeout mid-call — terminating")
                    should_stop = True
                if should_stop:
                    log(ctx, "⛔ Stop signal mid-call")
                    try:
                        proc.terminate()
                    except OSError:
                        pass
                    with ctx._stop_lock:
                        ctx._should_stop = False
                    ctx.current_proc = None
                    return False

                line = raw.decode("utf-8", errors="replace").rstrip()
                try:
                    evt = json.loads(line)
                    tt = evt.get("type", "")

                    if tt == "assistant":
                        msg = evt.get("message") or {}
                        # Extract intermediate usage for live token updates
                        msg_usage = msg.get("usage") if isinstance(msg, dict) else None
                        if isinstance(msg_usage, dict) and msg_usage:
                            _live_in = msg_usage.get("input_tokens", 0)
                            _live_out = msg_usage.get("output_tokens", 0)
                            # Set (not accumulate) — will be finalized by report_usage at result
                            ctx.session_tokens = ctx._pre_call_tokens + _live_in + _live_out
                        content = msg.get("content", []) if isinstance(msg, dict) else []
                        for b in (content if isinstance(content, list) else []):
                            if not isinstance(b, dict):
                                continue
                            btype = b.get("type", "")

                            if btype == "text":
                                text = b.get("text", "")
                                if text and text.strip():
                                    for tline in text.strip().split("\n"):
                                        tline = tline.rstrip()
                                        if tline:
                                            log(ctx, f"  💬 {tline[:300]}")
                                            out_lines += 1
                                            ctx._last_output_lines.append(tline[:500])
                                            if len(ctx._last_output_lines) > 200:
                                                ctx._last_output_lines = ctx._last_output_lines[-100:]

                            elif btype == "tool_use":
                                tool_name = b.get("name", "?")
                                inp = b.get("input") or {}
                                if not isinstance(inp, dict):
                                    inp = {}
                                # Skip noise tools (ToolSearch etc.)
                                if is_noise_tool(tool_name):
                                    pass  # Don't count or log
                                else:
                                    tool_count += 1
                                    detail = ""
                                    is_mcp = tool_name.startswith("mcp__")
                                    if is_mcp:
                                        display, detail, category = humanize_mcp_tool(tool_name, inp)
                                    elif tool_name == "Bash" and inp.get("command"):
                                        detail = humanize_bash(inp["command"])
                                    elif tool_name in ("Read", "View") and inp.get("file_path"):
                                        detail = short_path(inp["file_path"])
                                    elif tool_name in ("Edit", "Write") and inp.get("file_path"):
                                        detail = short_path(inp["file_path"])
                                    elif tool_name == "WebFetch" and inp.get("url"):
                                        detail = inp["url"][:120]
                                    elif tool_name == "Task" and inp.get("description"):
                                        detail = inp["description"][:100]
                                    elif tool_name == "Skill" and inp.get("name"):
                                        detail = inp["name"]
                                    elif inp:
                                        for v in inp.values():
                                            if isinstance(v, str) and len(v) > 2:
                                                detail = v[:100]
                                                break
                                    if detail and detail.startswith("→"):
                                        log(ctx, f"  📡 {detail[2:]}")
                                        report_progress(ctx, "hub_call", detail[2:])
                                    elif is_mcp:
                                        log(ctx, f"  🔧 [{category}] {display} -- {detail}" if detail else f"  🔧 [{category}] {display}")
                                        report_progress(ctx, "tool_use", f"[{category}] {display}: {detail}" if detail else f"[{category}] {display}")
                                    else:
                                        log(ctx, f"  🔧 {tool_name}: {detail}" if detail else f"  🔧 {tool_name}")
                                        report_progress(ctx, "tool_use", f"{tool_name}: {detail}" if detail else tool_name)
                                    # Report tool event to hub for live dashboard
                                    try:
                                        detail_str = detail or tool_name
                                        hub_post(ctx, "/agents/tool_event", {
                                            "agent_name": ctx.AGENT_NAME,
                                            "event": {"type": "tool_use", "tool": tool_name, "detail": detail_str[:100], "timestamp": time.time()}
                                        })
                                    except Exception:
                                        pass
                                    if tool_name in ("Edit", "Write") and inp.get("file_path"):
                                        lock_file(ctx, inp["file_path"])
                                    track_ecosystem_use(ctx, tool_name, inp)

                    elif tt == "result":
                        # Claude CLI stream-json: usage/session_id are at event top level
                        sid = evt.get("session_id", "")
                        if sid and sid != ctx.SESSION_ID:
                            ctx.SESSION_ID = sid
                            save_session(ctx)
                            # Store actual CLI session for task-specific resume
                            if ctx.current_task_id:
                                ctx.set_task_session(ctx.current_task_id, sid)
                        usage = evt.get("usage") or {}
                        if isinstance(usage, dict) and usage:
                            log(ctx, f"  tokens: {format_tokens_comma(usage.get('input_tokens', 0))} in / {format_tokens_comma(usage.get('output_tokens', 0))} out [{model_tag}]")
                            report_usage(ctx, usage, model)
                            report_progress(ctx, "call_done", f"{tool_count} tools, {usage.get('output_tokens', 0)} out")
                        elif evt.get("cost_usd"):
                            # Fallback: use cost_usd directly if usage missing
                            report_progress(ctx, "call_done", f"{tool_count} tools, ${evt['cost_usd']:.4f}")
                except json.JSONDecodeError:
                    if line.strip():
                        print(line, flush=True)
                        with ctx._log_lock:
                            ctx._log_buf.append(line)
                        out_lines += 1
                        if any(e in line.lower() for e in ["api error", "unable to connect", "econnreset", "connection reset"]):
                            stderr_lines.append(line)

            ctx.current_proc = None
            try:
                code = proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                log(ctx, "⚠ Claude process hung after stdout closed, killing")
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    code = proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    code = -9
            t.join(timeout=5)
            # Fallback: if stderr thread missed data, try direct read
            if not stderr_lines and proc.stderr:
                try:
                    leftover = proc.stderr.read()
                    if leftover:
                        for _sl in leftover.decode("utf-8", errors="replace").strip().split("\n"):
                            if _sl.strip():
                                stderr_lines.append(_sl.strip())
                except Exception:
                    pass
            log(ctx, f"◼ exit={code}, {out_lines} lines, {tool_count} tools")

            if code == 0:
                succeeded = True
                try:
                    if ctx.current_task_id:
                        collect_changes(ctx, prompt[:80], project=ctx.current_project)
                except Exception:
                    pass
                break
            if code in (-15, -9):
                log(ctx, "⛔ CANCELLED")
                break

            stderr_text = "\n".join(stderr_lines)
            if stderr_lines:
                err_preview = " | ".join(stderr_lines[-3:])[:200]
                log(ctx, f"⚠ stderr: {err_preview}")
            else:
                # Check stdout for API errors (500s come through stream-json, not stderr)
                out_text = " ".join(ctx._last_output_lines[-5:]).lower() if ctx._last_output_lines else ""
                is_api_500 = any(s in out_text for s in ["500", "api_error", "internal server error", "overloaded"])
                if is_api_500:
                    log(ctx, "⚠ API server error (500) — transient, retrying")
                    _model_failed = False  # Don't downgrade model for server errors
                else:
                    log(ctx, f"⚠ exit={code} with no stderr — model may be unavailable")
                    # Diagnostic: log the command (redact prompt) for debugging silent failures
                    _cmd_debug = [c for c in cmd if c != prompt]
                    log(ctx, f"  cmd: {' '.join(_cmd_debug[:20])}")
                    if out_lines == 0:
                        log(ctx, f"  ⚠ Zero stdout — CLI crashed before producing any output")
                        # Clear session so retry starts fresh (no --resume)
                        ctx.SESSION_ID = None
                    _model_failed = True

            err_lower = (stderr_text + " " + " ".join(ctx._last_output_lines[-5:] if ctx._last_output_lines else [])).lower()
            is_connection_err = any(e in err_lower for e in [
                "econnreset", "econnrefused", "etimedout", "enotfound",
                "unable to connect", "connection reset", "socket hang up",
                "network error", "fetch failed", "api error",
                "internal server error", "api_error"])
            is_rate_limit = handle_rl(ctx, stderr_text, code)

            if is_rate_limit:
                _rl_wait = max(30, int(ctx.rate_limited_until - time.time()))
                for _ in range(_rl_wait):
                    with ctx._stop_lock:
                        if ctx._should_stop:
                            log(ctx, "⛔ Stop signal during rate limit wait")
                            ctx._should_stop = False
                            return False
                    time.sleep(1)
            elif is_connection_err:
                base_wait = min(60, 10 * (2 ** (attempt - 1)))
                wait = base_wait + random.uniform(0, 5)
                log(ctx, f"⚠ Connection error (attempt {attempt}/{retries}), retry in {int(wait)}s...")
                if attempt == 1:
                    hub_msg(ctx, "user", f"⚠ {ctx.AGENT_NAME} API connection issue (ECONNRESET), retrying in {int(wait)}s...", "info")
                time.sleep(wait)
                # Preserve session on connection errors — server-side session is still valid
                if attempt >= 3 and "opus" in model:
                    _model_failed = True
                else:
                    _model_failed = False
            elif any(k in l.lower() for l in stderr_lines for k in
                      ("invalid session", "already in use", "session not found",
                       "no matching session", "could not find session", "could not resume")):
                log(ctx, "⚠ Session unavailable, starting fresh")
                ctx.SESSION_ID = None
                try:
                    os.remove(ctx.SESSION_FILE)
                except OSError:
                    pass
                if attempt < retries:
                    time.sleep(1)
            elif attempt < retries:
                log(ctx, "⚠ retry...")
                time.sleep(2)
                # Don't reset session — preserve context for retry
            else:
                handle_rl(ctx, "\n".join(stderr_lines), code)
                log(ctx, f"✗ failed after {retries} tries")
                err_summary = " | ".join(stderr_lines[-2:])[:150] if stderr_lines else f"exit code {code}"
                hub_msg(ctx, "user", f"⚠️ {ctx.AGENT_NAME}: Claude CLI failed after {retries} attempts: {err_summary}", "info")
        except Exception as e:
            log(ctx, f"✗ ERROR: {e}")
            if attempt >= retries:
                hub_msg(ctx, "user", f"⚠️ {ctx.AGENT_NAME}: unexpected error: {str(e)[:150]}", "info")
            if attempt < retries:
                time.sleep(3)
        finally:
            ctx.current_proc = None
            if proc.poll() is None:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass

    # Only save session if we have a valid one from the result event
    # Don't generate random UUIDs — let Claude CLI manage session creation
    if ctx.valid_sid(ctx.SESSION_ID):
        save_session(ctx)
    update_session(ctx)
    flush_logs(ctx)
    return succeeded
