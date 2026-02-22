"""lib/memory.py — Initialize shared memory files with dynamic agent awareness"""
import os, json

def _write_if_missing(path: str, content: str):
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(content)

def init_memory(ma_dir: str, agents=None):
    mem = os.path.join(ma_dir, "memory")
    os.makedirs(mem, exist_ok=True)

    # Load agent names from config if not provided
    if not agents:
        try:
            cfg_path = os.path.join(ma_dir, "config.json")
            if os.path.exists(cfg_path):
                with open(cfg_path) as f:
                    cfg = json.load(f)
                agents = [a["name"] if isinstance(a, dict) else a for a in cfg.get("agents", [])]
        except Exception as e:
            import logging
            logging.getLogger("memory").warning(f"Config load error in memory init: {e}")
        if not agents:
            agents = ["architect", "frontend", "backend", "qa"]

    _write_if_missing(f"{mem}/project-context.md", """# Project Context
## Active Projects
_Architect updates this._
## Requirements
_Current task requirements._
## Key Decisions
_Architecture and tech decisions._
""")

    _write_if_missing(f"{mem}/task-board.md", """# Task Board
| ID | Project | Task | Assigned To | Status | Priority | Notes |
|----|---------|------|-------------|--------|----------|-------|
## Status: backlog → in_progress → in_review → changes_requested → approved → done
## Priority: P1 (critical) → P5 (normal) → P10 (low)
""")

    _write_if_missing(f"{mem}/review-log.md", "# Code Review Log\n_QA logs all reviews here._\n")
    _write_if_missing(f"{mem}/architecture.md", "# Architecture Decisions\n_Architect logs decisions here._\n")

    _write_if_missing(f"{mem}/contracts.md", """# Shared Contracts
SINGLE SOURCE OF TRUTH for all interfaces between agents.
ALL agents MUST read this before coding and update when they change an interface.
## API Endpoints
_Architect defines. Backend implements, Frontend consumes._
## Events / Broadcasts
## Shared Types / Data Shapes
## Form Fields & Validation
## Constants & Enums
---
_Last updated: (timestamp)_
""")

    # Dynamic agent directory
    agent_lines = []
    for name in agents:
        agent_lines.append(f"## {name.title()} (sender: \"{name}\")")
        agent_lines.append(f"- Specialist agent. Check role file for details.")
        agent_lines.append("")

    _write_if_missing(f"{mem}/agents.md", f"""# Agent Directory
{chr(10).join(agent_lines)}
## Communication Protocol
1. Agents communicate DIRECTLY (not everything through architect)
2. Architect defines contracts BEFORE assigning tasks
3. Backend implements contract, notifies frontend when ready
4. Frontend consumes contract, asks backend directly if questions
5. QA verifies BOTH sides match contract, notifies directly
6. Contract CHANGES go through architect first
7. Broadcast for announcements affecting everyone
""")

    # Learnings directory
    learn = os.path.join(mem, "learnings")
    os.makedirs(learn, exist_ok=True)

    _write_if_missing(f"{learn}/patterns.md", """# Cross-Project Patterns & Conventions
_Auto-updated by agents after each task. Shared knowledge base._

## Common Patterns
## Gotchas & Pitfalls
## Reusable Solutions
""")

    _write_if_missing(f"{learn}/shared-knowledge.md", """# Shared Knowledge Base
_Agents write learnings here that benefit the whole team._
_Auto-populated after each successful task._
""")

    for name in agents:
        _write_if_missing(f"{learn}/agent-{name}.md", f"""# {name.title()} — Accumulated Expertise
_Auto-updated after each task. This agent gets smarter over time._

## Skills & Specializations
## Lessons Learned
## Preferred Approaches
""")

    print("✅ Memory files ready")
