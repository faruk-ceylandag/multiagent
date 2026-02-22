"""agents/git_ops.py — Git operations (branch, commit, rollback, changes, PR)."""
import os, re, time, subprocess
from .log_utils import log
from .hub_client import hub_post, hub_msg

# Paths that should never be git-added by agents
GIT_EXCLUDE = [".claude/", ".multiagent/", ".mcp.json", "hub_state.json"]

# Paths that must be protected from git stash (MCP config, agent infra)
_STASH_PROTECT = [".multiagent/", ".gitignore"]


def git_add_safe(ctx, cwd):
    """Stage all changes EXCEPT excluded paths."""
    git(ctx, ["add", "-A"], cwd)
    for excl in GIT_EXCLUDE:
        git(ctx, ["reset", "HEAD", "--", excl], cwd)


# ── File Locking ──

def lock_file(ctx, path):
    r = hub_post(ctx, "/files/lock", {"file_path": path, "agent_name": ctx.AGENT_NAME})
    if r and r.get("status") == "ok":
        ctx._locked_files.add(path)
        return True
    if r and r.get("status") == "locked":
        log(ctx, f"🔒 {path} locked by {r.get('by')}")
        return False
    return True  # if hub unreachable, proceed


def unlock_file(ctx, path):
    hub_post(ctx, "/files/unlock", {"file_path": path, "agent_name": ctx.AGENT_NAME})
    ctx._locked_files.discard(path)


def unlock_all(ctx):
    for f in list(ctx._locked_files):
        unlock_file(ctx, f)


BINARY_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.ico', '.svg', '.woff', '.woff2',
               '.ttf', '.eot', '.mp3', '.mp4', '.zip', '.tar', '.gz', '.pdf',
               '.exe', '.dll', '.so', '.dylib', '.pyc', '.class', '.o'}


def git(ctx, args, cwd=None):
    try:
        r = subprocess.run(["git"] + args, cwd=cwd or ctx.WORKSPACE,
                           capture_output=True, text=True, timeout=15)
        return r.returncode == 0, r.stdout.strip()
    except Exception:
        return False, ""


def git_critical(ctx, args, cwd=None, operation="git"):
    """Run a critical git command — logs warning on failure."""
    ok, out = git(ctx, args, cwd)
    if not ok:
        cmd_str = " ".join(args[:3])
        log(ctx, f"⚠ {operation} failed: git {cmd_str} → {out[:100]}")
    return ok, out


def _ensure_multiagent_ignored(d):
    """Ensure .multiagent/ is in .gitignore so git stash/checkout won't touch it."""
    gitignore = os.path.join(d, ".gitignore")
    marker = ".multiagent/"
    try:
        content = ""
        if os.path.exists(gitignore):
            with open(gitignore) as f:
                content = f.read()
        if marker in content:
            return
        with open(gitignore, "a" if content else "w") as f:
            if content and not content.endswith("\n"):
                f.write("\n")
            f.write(f"{marker}\n")
    except OSError:
        pass


def _safe_stash(ctx, d, msg=None):
    """Stash changes but protect .multiagent/ and .gitignore from being stashed."""
    _ensure_multiagent_ignored(d)
    # Remove .multiagent from tracking if it was somehow tracked
    git(ctx, ["rm", "-r", "--cached", "--ignore-unmatch", "--quiet", ".multiagent"], d)
    # Reset .gitignore from staging so it doesn't get stashed
    git(ctx, ["reset", "HEAD", "--quiet", "--", ".gitignore"], d)
    # Backup .gitignore content before stash
    gi_path = os.path.join(d, ".gitignore")
    gi_backup = None
    if os.path.exists(gi_path):
        try:
            with open(gi_path) as f:
                gi_backup = f.read()
        except OSError:
            pass
    # Now stash — .multiagent/ is gitignored (untouched), .gitignore is unstaged
    args = ["stash", "push"]
    if msg:
        args += ["-m", msg]
    git(ctx, args, d)
    # Restore .gitignore if stash ate it (shouldn't happen, but safety net)
    if gi_backup and not os.path.exists(gi_path):
        try:
            with open(gi_path, "w") as f:
                f.write(gi_backup)
        except OSError:
            pass
    # Re-ensure .multiagent/ is still ignored after stash
    _ensure_multiagent_ignored(d)


def git_stash_save(ctx, project):
    d = os.path.join(ctx.WORKSPACE, project)
    if not os.path.isdir(os.path.join(d, ".git")):
        return
    _safe_stash(ctx, d, msg=f"rollback-{ctx.AGENT_NAME}-{int(time.time())}")
    log(ctx, f"💾 rollback point for {project}")


def git_rollback(ctx, project):
    d = os.path.join(ctx.WORKSPACE, project)
    if not os.path.isdir(os.path.join(d, ".git")):
        return False
    git(ctx, ["checkout", "."], d)
    git(ctx, ["clean", "-fd"], d)
    ok, stashes = git(ctx, ["stash", "list"], d)
    if ok and f"rollback-{ctx.AGENT_NAME}" in stashes:
        pop_ok, pop_out = git(ctx, ["stash", "pop"], d)
        if not pop_ok:
            log(ctx, f"⚠ Rollback stash pop failed: {pop_out[:80]}")
            git(ctx, ["stash", "drop"], d)
        log(ctx, f"↩ rolled back {project}")
        return True
    return False


def _has_changes(ctx, d):
    """Check if working tree has uncommitted changes (staged or unstaged)."""
    ok_s, staged = git(ctx, ["diff", "--cached", "--quiet"], d)
    ok_u, unstaged = git(ctx, ["diff", "--quiet"], d)
    # Also check for untracked files
    _, untracked = git(ctx, ["ls-files", "--others", "--exclude-standard"], d)
    return not ok_s or not ok_u or bool(untracked.strip())


def git_branch(ctx, project, branch_name=None):
    """Create or checkout a shared task branch.
    Safe: always stashes changes first to prevent data loss during branch switch."""
    d = os.path.join(ctx.WORKSPACE, project)
    if not os.path.isdir(os.path.join(d, ".git")):
        return None
    if not branch_name:
        return None

    clean = re.sub(r'[^a-zA-Z0-9._/-]', '-', branch_name.strip())
    clean = re.sub(r'-+', '-', clean).strip('-')
    if not clean:
        return None

    if clean.startswith("feature/feature/"):
        clean = clean[len("feature/"):]
    branch = clean if clean.startswith("feature/") else f"feature/{clean}"
    branch = branch.rstrip('/-')
    branch = branch[:80]

    # Already on the target branch — do nothing
    ok, cur = git(ctx, ["branch", "--show-current"], d)
    if ok and cur == branch:
        return branch

    _ensure_multiagent_ignored(d)

    # Check if branch already exists (local)
    branch_exists, _ = git(ctx, ["rev-parse", "--verify", branch], d)

    # Stash uncommitted changes BEFORE switching to prevent data loss
    had_changes = _has_changes(ctx, d)
    if had_changes:
        log(ctx, f"💾 Stashing changes before branch switch")
        _safe_stash(ctx, d, msg=f"branch-switch-{ctx.AGENT_NAME}")

    if branch_exists:
        # Branch exists — just switch to it
        ok, out = git(ctx, ["checkout", branch], d)
    else:
        # Branch doesn't exist — create from base
        ok = False
        for base in ["main", "master", "develop"]:
            base_ok, _ = git(ctx, ["rev-parse", "--verify", base], d)
            if base_ok:
                ok, out = git(ctx, ["checkout", "-b", branch, base], d)
                break
        if not ok:
            ok, out = git(ctx, ["checkout", "-b", branch], d)

    # Restore stashed changes on the target branch
    if had_changes:
        pop_ok, pop_out = git(ctx, ["stash", "pop"], d)
        if not pop_ok and pop_out:
            pop_lower = pop_out.lower()
            if "conflict" in pop_lower:
                # Real conflict — changes partially applied, needs manual resolution
                log(ctx, f"⚠ Stash pop conflict — resolve manually (stash kept)")
            elif "no stash" in pop_lower:
                pass  # Nothing to pop, fine
            # else: git stash pop succeeded but returned non-zero (normal — shows status)

    if not ok:
        log(ctx, f"⚠ Failed to checkout {branch}")
        return None

    _ensure_multiagent_ignored(d)

    log(ctx, f"🌿 branch: {branch}")
    # Hook: on-branch-create
    from .learning import run_hook
    run_hook(ctx, "on-branch-create", {"project": project, "branch": branch})
    return branch


def clean_title(raw):
    s = raw.strip()
    while s.startswith("["):
        end = s.find("]")
        if end == -1:
            break
        s = s[end + 1:].strip()
    s = re.sub(r'^[-*]\s+', '', s)
    return s.strip() or raw.strip()


def git_commit(ctx, project, msg):
    d = os.path.join(ctx.WORKSPACE, project)
    if not os.path.isdir(os.path.join(d, ".git")):
        return False
    _, st = git(ctx, ["status", "--short"], d)
    if not st:
        return False
    git_add_safe(ctx, d)
    title = msg.strip()
    title = re.sub(r'^#\d+\s*', '', title)
    title = re.sub(r'https?://\S+\s*', '', title).strip()
    title = clean_title(title)
    title = title[:60] if title else "update"
    _, cur_branch = git(ctx, ["branch", "--show-current"], d)
    if cur_branch and cur_branch.startswith("feature/"):
        task_ref = cur_branch.replace("feature/", "")
        commit_msg = f"{task_ref} | {title}"
    elif ctx.current_task_id:
        commit_msg = f"TASK-{ctx.current_task_id} | {title}"
    else:
        commit_msg = f"{ctx.AGENT_NAME} | {title}"
    ok, _ = git(ctx, ["commit", "-m", commit_msg], d)
    if ok:
        log(ctx, f"📝 commit: {commit_msg}")
        # Hook: on-commit
        from .learning import run_hook
        run_hook(ctx, "on-commit", {"project": project, "message": commit_msg, "branch": cur_branch or ""})
    return ok


def git_changed_files(ctx, project):
    d = os.path.join(ctx.WORKSPACE, project)
    all_f = set()
    for cmd in [["diff", "--cached", "--name-only"], ["diff", "--name-only"],
                ["ls-files", "--others", "--exclude-standard"]]:
        _, out = git(ctx, cmd, d)
        for l in out.split("\n"):
            l = l.strip()
            if l:
                all_f.add(l)
    return all_f


def git_branches(ctx, project):
    d = os.path.join(ctx.WORKSPACE, project)
    ok, out = git(ctx, ["branch", "--list", "feature/*"], d)
    branches = [b.strip().lstrip("* ") for b in out.split("\n") if b.strip()] if ok else []
    return branches


def git_log_short(ctx, project, n=5):
    d = os.path.join(ctx.WORKSPACE, project)
    ok, out = git(ctx, ["log", "--oneline", f"-{n}"], d)
    return out if ok else ""


def collect_changes(ctx, desc="", project=None):
    """Collect git changes and report to hub."""
    targets = []
    if project:
        d = os.path.join(ctx.WORKSPACE, project)
        if os.path.isdir(os.path.join(d, ".git")):
            targets = [project]
    else:
        try:
            targets = [n for n in os.listdir(ctx.WORKSPACE)
                       if os.path.isdir(os.path.join(ctx.WORKSPACE, n, ".git"))]
        except OSError:
            pass
    for name in targets:
        d = os.path.join(ctx.WORKSPACE, name)
        if not os.path.isdir(os.path.join(d, ".git")):
            continue
        _, ds = git(ctx, ["diff", "--cached", "--stat"], d)
        _, du = git(ctx, ["diff", "--stat"], d)
        _, ut = git(ctx, ["ls-files", "--others", "--exclude-standard"], d)
        if not ds and not du and not ut:
            continue
        _, full = git(ctx, ["diff", "--cached"], d)
        _, unstaged = git(ctx, ["diff"], d)
        combined = (full + "\n" + unstaged).strip()
        if not combined and ut:
            parts = []
            for uf in ut.split("\n")[:10]:
                uf = uf.strip()
                if not uf:
                    continue
                ext = os.path.splitext(uf)[1].lower()
                if ext in BINARY_EXTS:
                    parts.append(f"+++ b/{uf}\n+[binary]")
                    continue
                fp = os.path.join(d, uf)
                try:
                    with open(fp, "r") as f:
                        c = f.read(4000)
                    parts.append(f"+++ b/{uf}\n" + "\n".join(f"+{l}" for l in c.split("\n")))
                except OSError:
                    parts.append(f"+++ b/{uf}\n+[unreadable]")
            combined = "\n".join(parts)
        if not combined:
            continue
        files = []
        _, fl1 = git(ctx, ["diff", "--cached", "--name-status"], d)
        _, fl2 = git(ctx, ["diff", "--name-status"], d)
        for line in (fl1 + "\n" + fl2 + "\n" + ut).split("\n"):
            line = line.strip()
            if not line:
                continue
            p = line.split("\t", 1)
            if len(p) == 2:
                files.append({"status": p[0], "path": p[1]})
            elif not line.startswith(("M", "A", "D", "R")):
                files.append({"status": "A", "path": line})
        if files:
            hub_post(ctx, "/changes", {"agent": ctx.AGENT_NAME, "project": name,
                                       "description": clean_title(desc), "diff": combined[:50000],
                                       "files": files[:50]})


def create_github_pr(ctx, project, branch, title):
    """Create GitHub PR if gh CLI available and remote exists."""
    d = os.path.join(ctx.WORKSPACE, project)
    if not os.path.isdir(os.path.join(d, ".git")):
        return
    if subprocess.run(["which", "gh"], capture_output=True).returncode != 0:
        return
    ok, remote = git(ctx, ["remote", "get-url", "origin"], d)
    if not ok or "github" not in remote.lower():
        return
    ok, _ = git(ctx, ["push", "-u", "origin", branch], d)
    if not ok:
        log(ctx, "⚠ git push failed")
        return
    try:
        pr_title = f"[{ctx.AGENT_NAME}] {clean_title(title)[:60]}"
        pr_body = f"Automated PR by {ctx.AGENT_NAME} agent.\n\nTask: {title}"
        r = subprocess.run(["gh", "pr", "create", "--title", pr_title, "--body", pr_body,
                           "--head", branch], cwd=d, capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            pr_url = r.stdout.strip()
            log(ctx, f"🔗 PR: {pr_url}")
            hub_msg(ctx, "user", f"📋 PR created: {pr_url}", "info")
        else:
            log(ctx, f"⚠ PR failed: {r.stderr[:80]}")
    except Exception as e:
        log(ctx, f"PR error: {e}")
