"""lib/config.py — Config system + stack detection + routing"""
import os, json, glob

DEFAULT_PORT = 8040
DEFAULT_AGENTS = [
    {"name": "architect", "role": "system architect & team lead", "model": ""},
    {"name": "frontend", "role": "frontend developer", "model": ""},
    {"name": "backend", "role": "backend developer", "model": ""},
    {"name": "qa", "role": "quality assurance & testing", "model": ""},
]

ROUTE_MAP = {
    "frontend": ["frontend", "vue", "react", "css", "scss", "blade", "component", "ui",
                  "tailwind", "template", "page", "layout", "style", "html", "svelte", "next", "nuxt",
                  "jsx", "tsx", "ux", "button", "form", "modal", "sidebar", "navbar", "responsive",
                  "figma", "design", "mockup", "prototype", "wireframe"],
    "backend":  ["backend", "api", "endpoint", "database", "migration", "laravel", "golang",
                  "model", "controller", "route", "middleware", "queue", "job", "artisan",
                  "schema", "sql", "prisma", "django", "fastapi", "express", "nest",
                  "php", "python", "node", "redis", "graphql", "rest", "webhook"],
    "qa":       ["test", "review", "bug", "broken", "failing", "lint", "quality",
                  "coverage", "regression", "e2e", "cypress", "playwright", "verify", "check", "spec"],
    "friday":   ["sentry", "error tracking", "crash", "exception", "monitoring", "alert",
                  "deploy", "devops", "infrastructure", "ci/cd", "pipeline", "docker",
                  "k8s", "kubernetes", "server", "sentry.io", "error report", "stack trace"],
}

SKIP_DIRS = {".multiagent",".claude","node_modules","__pycache__","vendor",
             "dist","build",".next","coverage",".cache",".output",
             ".vscode",".idea","tmp","temp",".DS_Store",".git",".github",
             "logs",".env",".nuxt",".turbo","multiagent","multiagent-release","multiagent-v4"}


def load_config(workspace: str) -> dict:
    """Load config from multiagent.json or defaults."""
    cfg = {
        "port": DEFAULT_PORT,
        "agents": DEFAULT_AGENTS,
        "focus_project": "",
        "max_retries": 2,
        "auto_verify": True,
        "auto_gitignore": True,
        "notifications": True,
        "thinking_model": "claude-sonnet-4-5-20250929",
        "coding_model": "claude-opus-4-6",
        "boot_stagger": 2,
        "max_context": 12000,
        "mcp_servers": {},
        "budget_limit": 0,
        "budget_per_agent": 0,
        "notifications_webhook": {},
        "auto_scale": {"enabled": False, "min_agents": 2, "max_agents": 8, "queue_threshold": 3},
    }
    for name in ["multiagent.json", ".multiagent/config.json"]:
        path = os.path.join(workspace, name)
        if os.path.exists(path):
            try:
                with open(path) as f:
                    user_cfg = json.load(f)
                cfg.update(user_cfg)
                # Normalize agents
                if "agents" in user_cfg:
                    agents = []
                    for a in user_cfg["agents"]:
                        if isinstance(a, str):
                            agents.append({"name": a, "role": "", "model": ""})
                        elif isinstance(a, dict):
                            entry = {"name": a.get("name","agent"), "role": a.get("role",""),
                                     "model": a.get("model","")}
                            if a.get("hidden"):
                                entry["hidden"] = True
                            agents.append(entry)
                    cfg["agents"] = agents
            except Exception as e:
                import logging
                logging.getLogger("config").warning(f"Config parse error: {e}")
            break
    return cfg


def save_default_config(workspace: str):
    """Create a sample config file."""
    path = os.path.join(workspace, "multiagent.json")
    if os.path.exists(path): return
    sample = {
        "port": 8040,
        "thinking_model": "claude-sonnet-4-5-20250929",
        "coding_model": "claude-opus-4-6",
        "agents": [
            {"name": "architect", "model": ""},
            {"name": "frontend", "model": ""},
            {"name": "backend", "model": ""},
            {"name": "qa", "model": ""},
        ],
        "focus_project": "",
        "auto_verify": True,
        "notifications": True,
        "boot_stagger": 2,
        "max_context": 12000,
        "mcp_servers": {},
        "budget_limit": 0,
    }
    try:
        with open(path, "w") as f: json.dump(sample, f, indent=2)
    except Exception as e:
        import logging
        logging.getLogger("config").warning(f"Config write error: {e}")


_PROJECT_MARKERS = {"package.json", "composer.json", "go.mod", "Cargo.toml",
                    "pyproject.toml", "requirements.txt", "Gemfile", "pom.xml",
                    "build.gradle", "Makefile", "CMakeLists.txt", "setup.py"}

def scan_projects(workspace: str, focus: str = "") -> list[str]:
    if focus:
        fp = os.path.join(workspace, focus)
        return [focus] if os.path.isdir(fp) else []
    # Single-project detection: workspace root has project markers OR .git → it IS the project
    has_marker = any(os.path.exists(os.path.join(workspace, m)) for m in _PROJECT_MARKERS)
    has_git = os.path.isdir(os.path.join(workspace, ".git"))
    if has_marker or has_git:
        return ["."]
    # Multi-project: scan subdirectories for actual projects
    # Must have BOTH .git AND a project marker (prevents listing random subdirs)
    projects = []
    for name in sorted(os.listdir(workspace)):
        path = os.path.join(workspace, name)
        if not os.path.isdir(path) or name.startswith(".") or name in SKIP_DIRS:
            continue
        sub_git = os.path.isdir(os.path.join(path, ".git"))
        sub_marker = any(os.path.exists(os.path.join(path, m)) for m in _PROJECT_MARKERS)
        if sub_git and sub_marker:
            projects.append(name)
    return projects


def detect_stack(project_dir: str) -> dict:
    s = {"lang":[],"fw":[],"tools":[],"test":[],"lint":[],"build":[]}
    def has(p): return os.path.exists(os.path.join(project_dir, p))
    if has("package.json"):
        s["lang"].append("javascript")
        try:
            with open(os.path.join(project_dir, "package.json")) as f:
                pkg = json.load(f)
            deps = {**pkg.get("dependencies",{}), **pkg.get("devDependencies",{})}
            sc = pkg.get("scripts",{})
            if "vue" in deps: s["fw"].append("vue")
            if "nuxt" in deps: s["fw"].append("nuxt")
            if "react" in deps: s["fw"].append("react")
            if "next" in deps: s["fw"].append("next")
            if "svelte" in deps: s["fw"].append("svelte")
            if "express" in deps: s["fw"].append("express")
            if "@nestjs/core" in deps: s["fw"].append("nest")
            if "tailwindcss" in deps: s["tools"].append("tailwind")
            if "typescript" in deps:
                s["lang"].append("typescript"); s["lint"].append("npx tsc --noEmit")
            if "vitest" in deps: s["tools"].append("vitest")
            elif "jest" in deps: s["tools"].append("jest")
            if "test" in sc: s["test"].append("npm test")
            if "lint" in sc: s["lint"].append("npm run lint")
            if "build" in sc: s["build"].append("npm run build")
        except (json.JSONDecodeError, OSError): pass
    if has("composer.json"):
        s["lang"].append("php")
        try:
            with open(os.path.join(project_dir, "composer.json")) as f:
                deps = json.load(f).get("require",{})
            if "laravel/framework" in deps: s["fw"].append("laravel")
        except (json.JSONDecodeError, OSError): pass
        s["test"].append("php artisan test"); s["lint"].append("vendor/bin/pint --test")
    if has("go.mod"):
        s["lang"].append("go"); s["test"].append("go test ./... -v"); s["lint"].append("go vet ./...")
    if has("requirements.txt") or has("pyproject.toml"):
        s["lang"].append("python"); s["test"].append("python -m pytest -v"); s["lint"].append("python -m ruff check .")
    if has("Cargo.toml"):
        s["lang"].append("rust"); s["test"].append("cargo test")
    if has("Dockerfile"): s["tools"].append("docker")
    for k in s: s[k] = list(dict.fromkeys(s[k]))
    return s


def detect_target(msg: str, agent_names: list) -> str:
    low = msg.lower()
    for a in agent_names:
        if low.startswith(f"@{a} ") or low.startswith(f"{a}: "): return a
    scores = {a: 0 for a in agent_names}
    for agent, keywords in ROUTE_MAP.items():
        if agent in scores:
            for kw in keywords:
                if kw in low: scores[agent] += 1
    best = max(scores, key=scores.get)
    if scores[best] >= 2: return best
    if scores[best] == 1 and sum(1 for v in scores.values() if v > 0) == 1: return best
    return agent_names[0] if agent_names else "architect"
