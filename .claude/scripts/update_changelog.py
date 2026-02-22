#!/usr/bin/env python3
"""Post-commit hook: appends the latest commit to CHANGELOG.md under today's date."""

import json
import os
import re
import subprocess
import sys
from datetime import date


def main():
    # Read tool input from stdin to check if this was a git commit
    try:
        inp = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    command = inp.get("tool_input", {}).get("command", "")

    # Only trigger on git commit commands (not amend of changelog itself)
    if "git commit" not in command:
        sys.exit(0)

    tool_result = inp.get("tool_result", {})
    stdout = tool_result.get("stdout", "") if isinstance(tool_result, dict) else str(tool_result)

    # Extract commit hash and message from git commit output
    # Format: "[branch hash] message"
    match = re.search(r"\[[\w/.-]+\s+([a-f0-9]+)\]\s+(.+)", stdout)
    if not match:
        sys.exit(0)

    short_hash = match.group(1)
    commit_msg = match.group(2)

    # Skip if this commit is just updating the changelog
    if "changelog" in commit_msg.lower():
        sys.exit(0)

    # Find CHANGELOG.md
    repo_root = subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"], text=True
    ).strip()
    changelog_path = os.path.join(repo_root, "CHANGELOG.md")

    if not os.path.exists(changelog_path):
        sys.exit(0)

    today = date.today().strftime("%Y-%m-%d")
    entry = f"- {commit_msg} (`{short_hash}`)"

    with open(changelog_path) as f:
        content = f.read()

    # Categorize the commit
    msg_lower = commit_msg.lower()
    if msg_lower.startswith("fix"):
        section = "Fixes"
    elif any(msg_lower.startswith(w) for w in ("add", "implement", "create", "new")):
        section = "Features"
    else:
        section = "Improvements"

    today_header = f"## {today}"

    if today_header in content:
        # Today's section exists — find the right subsection
        section_header = f"### {section}"
        # Find position after today's header
        today_pos = content.index(today_header)
        # Find end of today's section (next ## or end of file)
        next_date = content.find("\n## ", today_pos + len(today_header))
        today_block = content[today_pos:next_date] if next_date != -1 else content[today_pos:]

        if section_header in today_block:
            # Subsection exists — append entry after it
            abs_section_pos = content.index(section_header, today_pos)
            insert_pos = content.index("\n", abs_section_pos) + 1
            content = content[:insert_pos] + entry + "\n" + content[insert_pos:]
        else:
            # Add new subsection at end of today's block
            if next_date != -1:
                # Insert before the --- separator or next date
                sep_pos = content.rfind("\n---\n", today_pos, next_date)
                insert_pos = sep_pos if sep_pos != -1 else next_date
            else:
                insert_pos = len(content)
            content = content[:insert_pos] + f"\n{section_header}\n{entry}\n" + content[insert_pos:]
    else:
        # New date section — insert after "# Changelog" header
        header_end = content.index("\n", content.index("# Changelog")) + 1
        new_section = f"\n{today_header}\n\n### {section}\n{entry}\n\n---\n"
        content = content[:header_end] + new_section + content[header_end:]

    with open(changelog_path, "w") as f:
        f.write(content)

    print(f"Changelog updated: {entry}")


if __name__ == "__main__":
    main()