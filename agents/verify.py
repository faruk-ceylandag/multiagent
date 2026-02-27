"""agents/verify.py — Verification loop (lint, test, build) with retry."""
import os, time
from .log_utils import log
from .hub_client import hub_post, hub_msg, set_status
from .git_ops import git, git_changed_files
from .learning import read_file, run_hook, get_verify_cmds

VERIFY_HARD_TIMEOUT = 600  # 10 minutes


def should_skip_verify(ctx, project):
    if not project:
        return True
    files = git_changed_files(ctx, project)
    if not files:
        return True
    if len(files) <= 2:
        trivial = {'.md', '.txt', '.json', '.yaml', '.yml', '.toml', '.cfg', '.env', '.gitignore'}
        if all(os.path.splitext(f)[1].lower() in trivial for f in files):
            log(ctx, f"⏭ skip verify (trivial)")
            return True
    return False


def verify_loop(ctx, project, call_claude_fn):
    """Run verification (lint/test/build) with up to 3 retry cycles."""
    if not ctx.AUTO_VERIFY:
        log(ctx, "⏭ verify disabled")
        return True
    if not project:
        return True
    if should_skip_verify(ctx, project):
        return True
    d = os.path.join(ctx.WORKSPACE, project)
    if not os.path.isdir(d):
        return True
    _, diff = git(ctx, ["diff", "--stat", "HEAD"], d)
    if not diff:
        return True

    cmds = get_verify_cmds(ctx, project)
    changed = git_changed_files(ctx, project)
    cmd_text = "\n".join(f"  {c}" for c in cmds[:6]) if cmds else "  Run: lint, tests, build"
    changed_hint = f"\nChanged files: {', '.join(list(changed)[:10])}" if changed else ""
    # Include task context so agent remembers what it was working on (survives session resets)
    task_ctx = ""
    if getattr(ctx, '_task_summary', ''):
        task_ctx = f"\nTASK CONTEXT (what you just worked on): {ctx._task_summary[:300]}"
    run_hook(ctx, "on-verify", {"project": project, "files": list(changed)[:20]})

    # Adaptive cycle count based on number of changed files
    max_cycles = 3
    if len(changed) <= 1:
        max_cycles = 2
    elif len(changed) <= 5:
        max_cycles = 2

    _verify_start = time.time()
    for i in range(1, max_cycles + 1):
        if time.time() - _verify_start > VERIFY_HARD_TIMEOUT:
            log(ctx, "⏱ VERIFY TIMEOUT (10 min hard limit)")
            hub_msg(ctx, "user", f"⏱ {ctx.AGENT_NAME}: verify timed out on {project} (10 min limit)", "blocker")
            return False
        log(ctx, f"━━ VERIFY {i}/{max_cycles} ━━")
        set_status(ctx, "verifying", f"attempt {i}")
        try:
            os.remove(ctx.VERIFY_FILE)
        except OSError:
            pass
        try:
            os.remove(ctx.TEST_RESULT_FILE)
        except OSError:
            pass

        # Lint-first: first cycle only runs lint; full suite on subsequent cycles
        if i == 1 and max_cycles > 1:
            verify_prompt = f"""VERIFY (lint-only) work in {d}. Run ONLY lint on changed files:\n{cmd_text}{changed_hint}{task_ctx}

IMPORTANT:
- Only run linting/type-checking on changed files. Skip tests and build for now.
- If lint passes, write "PASS" to {ctx.VERIFY_FILE}
- If lint fails, FIX the issues immediately, then re-run lint.
- Write result: echo "PASS" > {ctx.VERIFY_FILE} OR echo "FAIL: reason" > {ctx.VERIFY_FILE}
Also: echo "tests_passed=0 tests_failed=0 tests_skipped=0 lint_errors=N" > {ctx.TEST_RESULT_FILE}"""
        else:
            verify_prompt = f"""VERIFY work in {d}. Run ONLY on changed files if possible:\n{cmd_text}{changed_hint}{task_ctx}

IMPORTANT:
- Only check errors in files YOU changed. Pre-existing errors in other files don't count.
- If lint/test commands exist, run them. If not, do basic syntax checks on changed files.
- Focus: do the changed files compile/parse correctly? Are there obvious bugs?
- If ANY test or lint fails: FIX THE ISSUE IMMEDIATELY, then re-run to confirm the fix works.
- Keep iterating fix→test→fix→test until everything passes.

Write result: echo "PASS" > {ctx.VERIFY_FILE} OR echo "FAIL: reason" > {ctx.VERIFY_FILE}
Also: echo "tests_passed=N tests_failed=N tests_skipped=N lint_errors=N" > {ctx.TEST_RESULT_FILE}
(lint_errors = only NEW errors in changed files, not pre-existing)
If FAIL: fix issues then recheck. Do NOT give up — iterate until tests pass."""

        call_claude_fn(verify_prompt, retries=1, force_model=ctx.MODEL_SONNET, cwd=d,
                       continue_session=True)

        try:
            with open(ctx.VERIFY_FILE) as f:
                result = f.read().strip()
        except OSError:
            result = "UNKNOWN"
            log(ctx, f"⚠ Verify file not written (attempt {i}/{max_cycles}) — Claude may lack tool permissions")

        test_data = {}
        try:
            tr = read_file(ctx.TEST_RESULT_FILE)
            for part in tr.split():
                if "=" in part:
                    k, v = part.split("=", 1)
                    test_data[k] = int(v)
            if test_data:
                hub_post(ctx, "/tests/result", {"agent_name": ctx.AGENT_NAME, "project": project,
                                                "task_id": ctx.current_task_id or "", **test_data})
                log(ctx, f"🧪 tests: {test_data}")
        except (ValueError, KeyError, OSError):
            pass

        if result.startswith("PASS"):
            log(ctx, "✓ VERIFY PASSED")
            return True
        if i == max_cycles:
            log(ctx, "✗ VERIFY FAILED")
            hub_msg(ctx, "user", f"⚠️ {ctx.AGENT_NAME}: verify failed on {project}", "blocker")
            run_hook(ctx, "on-error", {"project": project, "error": "verify_failed"})
            hub_post(ctx, "/agents/specialization", {"agent_name": ctx.AGENT_NAME,
                                                     "task_type": "verify", "project": project,
                                                     "success": False})
            return False
    return False
