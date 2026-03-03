#!/bin/bash
set -e

G='\033[0;32m'; R='\033[0;31m'; B='\033[0;34m'; NC='\033[0m'; BOLD='\033[1m'

echo -e "${B}${BOLD}Multi-Agent Installer${NC}\n"

# Check deps
command -v python3 >/dev/null || { echo -e "${R}python3 required${NC}"; exit 1; }
command -v claude >/dev/null || { echo -e "${R}claude CLI required: npm i -g @anthropic-ai/claude-code${NC}"; exit 1; }

INSTALL_DIR="${HOME}/.local/share/multiagent"
BIN_DIR="${HOME}/.local/bin"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "$INSTALL_DIR" "$BIN_DIR"

# Copy files — detect source directory
if [ -f "$SCRIPT_DIR/hub/hub_server.py" ]; then
  SRC="$SCRIPT_DIR"
elif [ -d "$SCRIPT_DIR/multiagent" ] && [ -f "$SCRIPT_DIR/multiagent/hub/hub_server.py" ]; then
  SRC="$SCRIPT_DIR/multiagent"
else
  echo -e "${R}✗ Cannot find hub/hub_server.py in $SCRIPT_DIR${NC}"
  ls -la "$SCRIPT_DIR/"
  exit 1
fi
echo -e "${G}✓ Source: $SRC${NC}"

for d in hub/routers hub/middleware hub/dashboard agents lib ecosystem/mcp ecosystem/hooks ecosystem/templates ecosystem/subagents ecosystem/commands ecosystem/skills; do mkdir -p "$INSTALL_DIR/$d"; done

cp "$SRC/start.py" "$INSTALL_DIR/"

# Hub package (full modular structure)
cp "$SRC/hub/"*.py "$INSTALL_DIR/hub/"
cp "$SRC/hub/routers/"*.py "$INSTALL_DIR/hub/routers/"
cp "$SRC/hub/middleware/"*.py "$INSTALL_DIR/hub/middleware/" 2>/dev/null || true
rm -rf "$INSTALL_DIR/hub/dashboard"
cp -R "$SRC/hub/dashboard" "$INSTALL_DIR/hub/dashboard"

# Agents package (full)
cp "$SRC/agents/"*.py "$INSTALL_DIR/agents/"

# Lib
cp "$SRC/lib/"*.py "$INSTALL_DIR/lib/"
touch "$INSTALL_DIR/lib/__init__.py"

# Ecosystem
cp "$SRC/ecosystem/"*.py "$INSTALL_DIR/ecosystem/" 2>/dev/null || true
cp "$SRC/ecosystem/mcp/"*.py "$INSTALL_DIR/ecosystem/mcp/" 2>/dev/null || true
cp "$SRC/ecosystem/hooks/"*.py "$INSTALL_DIR/ecosystem/hooks/" 2>/dev/null || true
cp "$SRC/ecosystem/templates/"*.py "$INSTALL_DIR/ecosystem/templates/" 2>/dev/null || true
cp "$SRC/ecosystem/subagents/"*.md "$INSTALL_DIR/ecosystem/subagents/" 2>/dev/null || true
cp "$SRC/ecosystem/commands/"*.md "$INSTALL_DIR/ecosystem/commands/" 2>/dev/null || true

# Skills (directory-based: each skill is a subdirectory with SKILL.md)
if [ -d "$SRC/ecosystem/skills" ]; then
  for skill_dir in "$SRC/ecosystem/skills"/*/; do
    skill_name=$(basename "$skill_dir")
    mkdir -p "$INSTALL_DIR/ecosystem/skills/$skill_name"
    cp "$skill_dir"* "$INSTALL_DIR/ecosystem/skills/$skill_name/" 2>/dev/null || true
  done
fi

for d in ecosystem ecosystem/mcp ecosystem/hooks ecosystem/templates; do
  touch "$INSTALL_DIR/$d/__init__.py"
done

echo -e "${G}✓ Files installed to $INSTALL_DIR${NC}"

# Create CLI tool with subcommands
cat > "$BIN_DIR/ma" << 'MAEOF'
#!/bin/bash
INSTALL_DIR="${HOME}/.local/share/multiagent"
# Read port from .hub.port if available, fallback to 8040
_PORT_FILE="${PWD}/.multiagent/.hub.port"
_CENTRAL_PORT_FILE="${HOME}/.multiagent/.hub.port"
if [ -f "$_PORT_FILE" ]; then
  _PORT=$(cat "$_PORT_FILE")
elif [ -f "$_CENTRAL_PORT_FILE" ]; then
  _PORT=$(cat "$_CENTRAL_PORT_FILE")
else
  _PORT=8040
fi
HUB="http://127.0.0.1:${_PORT}"

case "${1:-start}" in
  start|"")
    exec python3 "$INSTALL_DIR/start.py" "${2:-.}"
    ;;
  status)
    curl -sf "$HUB/dashboard" 2>/dev/null | python3 -c "
import sys,json
try:
  d=json.load(sys.stdin)
  for n,a in d.get('agents',{}).items():
    s=a.get('pipeline','offline')
    icon={'idle':'🟢','working':'🟡','booting':'🔵','verifying':'🟡','offline':'⚫','unresponsive':'🔴','rate_limited':'🟠'}.get(s,'⚪')
    print(f'  {icon} {n:12s} {s:14s} {a.get(\"calls\",0)} calls  {a.get(\"detail\",\"\")[:40]}')
  u=d.get('usage',{})
  ti=sum(c.get('tokens_in',0) for c in u.values())
  to=sum(c.get('tokens_out',0) for c in u.values())
  print(f'\n  Tokens: {ti+to:,} ({ti:,} in / {to:,} out)')
except: print('  Hub not running. Start with: ma')
" 2>/dev/null || echo "  Hub not running. Start with: ma"
    ;;
  send)
    if [ -z "$2" ] || [ -z "$3" ]; then
      echo "Usage: ma send <agent|all> \"message\""
      exit 1
    fi
    TARGET="$2"; shift 2; MSG="$*"
    if [ "$TARGET" = "all" ]; then
      python3 -c "
import sys,json
from urllib.request import Request, urlopen
try:
    payload=json.dumps({'sender':'user','receiver':'all','content':sys.argv[1],'msg_type':'task'})
    r=json.loads(urlopen(Request(sys.argv[2]+'/broadcast',data=payload.encode(),headers={'Content-Type':'application/json'}),timeout=5).read())
    print(f'  ✓ Sent to: {r.get(\"sent_to\",[])}' if r.get('status')=='ok' else '  ✗ Failed')
except Exception as e: print(f'  ✗ {e}')
" "$MSG" "$HUB"
    else
      python3 -c "
import sys,json
from urllib.request import Request, urlopen
try:
    payload=json.dumps({'sender':'user','receiver':sys.argv[2],'content':sys.argv[1],'msg_type':'task'})
    r=json.loads(urlopen(Request(sys.argv[3]+'/messages',data=payload.encode(),headers={'Content-Type':'application/json'}),timeout=5).read())
    print('  ✓ Sent' if r.get('status')=='ok' else '  ✗ Failed')
except Exception as e: print(f'  ✗ {e}')
" "$MSG" "$TARGET" "$HUB"
    fi
    ;;
  stop)
    if [ -z "$2" ]; then echo "Usage: ma stop <agent>"; exit 1; fi
    curl -sf -X POST "$HUB/agents/$2/stop" -H 'Content-Type: application/json' -d '{}' | python3 -c "import sys,json;d=json.load(sys.stdin);print(f'  ⛔ {d.get(\"message\",\"\")}' if d.get('status')=='ok' else '  ✗ Failed')"
    ;;
  logs)
    AGENT="${2:-architect}"
    curl -sf "$HUB/logs/$AGENT?lines=50" | python3 -c "
import sys,json
try:
  d=json.load(sys.stdin)
  for l in d.get('lines',[]):print(l)
except: print('No logs')" 2>/dev/null || echo "  Hub not running"
    ;;
  logs-follow|tail)
    AGENT="${2:-architect}"
    echo "  Following $AGENT logs (Ctrl+C to stop)..."
    CURSOR=0
    while true; do
      DATA=$(curl -sf "$HUB/logs/$AGENT?after=$CURSOR" 2>/dev/null)
      if [ -n "$DATA" ]; then
        CURSOR=$(echo "$DATA" | python3 -c "import sys,json;d=json.load(sys.stdin);[print(l) for l in d.get('lines',[])];print(d.get('cursor',0),file=sys.stderr)" 2>&1 1>/dev/tty | tail -1)
      fi
      sleep 1
    done
    ;;
  tasks)
    curl -sf "$HUB/tasks" 2>/dev/null | python3 -c "
import sys,json
try:
  tasks=json.load(sys.stdin)
  if not tasks: print('  No tasks'); sys.exit()
  for t in tasks:
    s=t.get('status','?')
    icon={'to_do':'📋','in_progress':'⚙️','code_review':'🔍','in_testing':'🧪','uat':'👤','done':'✅','failed':'❌','cancelled':'🚫','created':'📋','assigned':'👤'}.get(s,'•')
    print(f'  {icon} #{t[\"id\"]:3d} [{s:12s}] → {t.get(\"assigned_to\",\"?\"):10s} {t.get(\"description\",\"\")[:60]}')
except: print('  Hub not running')
" 2>/dev/null || echo "  Hub not running"
    ;;
  inbox)
    curl -sf "$HUB/messages/user?peek=false&consume=false" 2>/dev/null | python3 -c "
import sys,json
try:
  msgs=json.load(sys.stdin)
  if not msgs: print('  📭 No messages'); sys.exit()
  for m in msgs:
    print(f'  [{m[\"sender\"]}] ({m.get(\"msg_type\",\"?\")}): {m[\"content\"][:100]}')
except: print('  Hub not running')
" 2>/dev/null || echo "  Hub not running"
    ;;
  export)
    curl -sf "$HUB/export" > session-report.md 2>/dev/null && echo "  ✓ Saved to session-report.md" || echo "  ✗ Failed"
    ;;
  kill|shutdown)
    echo "  Stopping all..."
    pkill -f "agents.worker" 2>/dev/null
    pkill -f "hub_server:app" 2>/dev/null
    pkill -f "start.py" 2>/dev/null
    echo "  ✓ All stopped"
    ;;
  add)
    if [ -z "$2" ]; then echo "Usage: ma add /path/to/repo [alias]"; exit 1; fi
    _ADD_PATH="$(cd "$2" 2>/dev/null && pwd || echo "$2")"
    _ALIAS="${3:-}"
    python3 -c "
import sys,json
from urllib.request import Request, urlopen
try:
    payload=json.dumps({'path':sys.argv[1],'alias':sys.argv[2]})
    r=json.loads(urlopen(Request(sys.argv[3]+'/workspaces/add',data=payload.encode(),headers={'Content-Type':'application/json'}),timeout=5).read())
    if r.get('status')=='ok':
        print(f'  \u2713 Workspace added (id: {r.get(\"ws_id\")})')
        print(f'    Projects: {\", \".join(r.get(\"projects\",[]))}')
    else: print(f'  \u2717 {r.get(\"message\",\"Failed\")}')
except Exception as e: print(f'  \u2717 {e}')
" "$_ADD_PATH" "$_ALIAS" "$HUB"
    ;;
  workspaces|ws)
    curl -sf "$HUB/workspaces" 2>/dev/null | python3 -c "
import sys,json
try:
  ws=json.load(sys.stdin)
  if not ws: print('  No workspaces'); sys.exit()
  for w in ws:
    p='\u2605' if w.get('is_primary') else ' '
    projs=', '.join(w.get('projects',[])[:5]) or '(none)'
    print(f'  {p} [{w[\"ws_id\"]:8s}] {w.get(\"name\",\"?\"):15s} {w.get(\"path\",\"\")}')
    print(f'              projects: {projs}')
except: print('  Hub not running')
" 2>/dev/null || echo "  Hub not running"
    ;;
  remove)
    if [ -z "$2" ]; then echo "Usage: ma remove <ws_id>"; exit 1; fi
    curl -sf -X DELETE "$HUB/workspaces/$2" | python3 -c "import sys,json;d=json.load(sys.stdin);print('  \u2713 Removed' if d.get('status')=='ok' else f'  \u2717 {d.get(\"message\",\"Failed\")}')" 2>/dev/null || echo "  Hub not running"
    ;;
  daemon)
    exec python3 "$INSTALL_DIR/start.py" --daemon "${2:-.}"
    ;;
  stop-daemon)
    _PID_FILE="${PWD}/.multiagent/.daemon.pid"
    _CENTRAL_PID="${HOME}/.multiagent/.daemon.pid"
    if [ -f "$_PID_FILE" ]; then
      _PID=$(cat "$_PID_FILE")
    elif [ -f "$_CENTRAL_PID" ]; then
      _PID=$(cat "$_CENTRAL_PID")
    else
      echo "  No daemon PID file found"
      exit 1
    fi
    if kill -0 "$_PID" 2>/dev/null; then
      kill "$_PID"
      echo "  ✓ Daemon stopped (PID $_PID)"
    else
      echo "  Daemon not running (stale PID $_PID)"
    fi
    rm -f "$_PID_FILE" "$_CENTRAL_PID" 2>/dev/null
    ;;
  restart-daemon)
    "$0" stop-daemon 2>/dev/null
    sleep 2
    exec "$0" daemon "${2:-.}"
    ;;
  link)
    _TARGET="${2:-.}"
    _TARGET="$(cd "$_TARGET" 2>/dev/null && pwd || echo "$_TARGET")"
    _CLAUDE_DIR="$_TARGET/.claude"
    mkdir -p "$_CLAUDE_DIR/agents" "$_CLAUDE_DIR/commands" "$_CLAUDE_DIR/skills"
    _LINKED=0
    # Subagents → .claude/agents/
    for f in "$INSTALL_DIR/ecosystem/subagents/"*.md; do
      [ -f "$f" ] || continue
      _NAME=$(basename "$f")
      _DST="$_CLAUDE_DIR/agents/$_NAME"
      [ -L "$_DST" ] && continue  # already linked
      [ -f "$_DST" ] && continue  # project has its own
      ln -s "$f" "$_DST"
      _LINKED=$((_LINKED+1))
    done
    # Commands → .claude/commands/
    for f in "$INSTALL_DIR/ecosystem/commands/"*.md; do
      [ -f "$f" ] || continue
      _NAME=$(basename "$f")
      _DST="$_CLAUDE_DIR/commands/$_NAME"
      [ -L "$_DST" ] && continue
      [ -f "$_DST" ] && continue
      ln -s "$f" "$_DST"
      _LINKED=$((_LINKED+1))
    done
    # Skills → .claude/skills/
    for d in "$INSTALL_DIR/ecosystem/skills"/*/; do
      [ -d "$d" ] || continue
      _NAME=$(basename "$d")
      _DST="$_CLAUDE_DIR/skills/$_NAME"
      [ -L "$_DST" ] && continue
      [ -d "$_DST" ] && continue
      ln -s "$d" "$_DST"
      _LINKED=$((_LINKED+1))
    done
    echo "  ✓ Linked $_LINKED ecosystem tools to $_CLAUDE_DIR"
    echo "    agents:   $(ls "$_CLAUDE_DIR/agents/"*.md 2>/dev/null | wc -l | tr -d ' ') subagents"
    echo "    commands: $(ls "$_CLAUDE_DIR/commands/"*.md 2>/dev/null | wc -l | tr -d ' ') commands"
    echo "    skills:   $(ls -d "$_CLAUDE_DIR/skills/"*/ 2>/dev/null | wc -l | tr -d ' ') skills"
    ;;
  unlink)
    _TARGET="${2:-.}"
    _TARGET="$(cd "$_TARGET" 2>/dev/null && pwd || echo "$_TARGET")"
    _CLAUDE_DIR="$_TARGET/.claude"
    _REMOVED=0
    # Remove only symlinks pointing to our install dir
    for subdir in agents commands skills; do
      _DIR="$_CLAUDE_DIR/$subdir"
      [ -d "$_DIR" ] || continue
      for item in "$_DIR"/*; do
        [ -L "$item" ] || continue
        _LINK_TARGET=$(readlink "$item")
        if echo "$_LINK_TARGET" | grep -q "$INSTALL_DIR"; then
          rm "$item"
          _REMOVED=$((_REMOVED+1))
        fi
      done
    done
    echo "  ✓ Removed $_REMOVED ecosystem symlinks from $_CLAUDE_DIR"
    ;;
  help|--help|-h)
    echo "Multi-Agent CLI"
    echo ""
    echo "Usage: ma [command] [args]"
    echo ""
    echo "Commands:"
    echo "  start [path]       Start agents (default: current dir)"
    echo "  status             Show agent status"
    echo "  send <agent> msg   Send message to agent"
    echo "  stop <agent>       Stop agent's current task"
    echo "  logs [agent]       Show recent logs"
    echo "  tail [agent]       Follow logs in real-time"
    echo "  tasks              List all tasks"
    echo "  inbox              Show user inbox"
    echo "  add <path> [alias] Add workspace to running hub"
    echo "  workspaces|ws      List all workspaces"
    echo "  remove <ws_id>     Remove workspace from hub"
    echo "  export             Export session report"
    echo "  kill               Stop everything"
    echo "  link [path]        Link ecosystem tools to project's .claude/"
    echo "  unlink [path]      Remove ecosystem symlinks from project"
    echo "  daemon [path]      Start as background daemon"
    echo "  stop-daemon        Stop running daemon"
    echo "  restart-daemon     Restart daemon"
    echo ""
    echo "Examples:"
    echo "  ma                           # start in current dir"
    echo "  ma send backend 'add API'    # send task to backend"
    echo "  ma send all 'refactor auth'  # broadcast to all"
    echo "  ma stop frontend             # cancel frontend task"
    echo "  ma tail qa                   # follow QA logs"
    echo "  ma add ~/other-repo myrepo   # add workspace"
    echo "  ma ws                        # list workspaces"
    echo "  ma link                      # link ecosystem to current project"
    echo "  ma link ~/other-project      # link ecosystem to another project"
    echo "  ma unlink                    # remove ecosystem links"
    echo "  ma daemon                    # start as daemon"
    echo "  ma stop-daemon               # stop the daemon"
    ;;
  *)
    echo "Unknown command: $1 (try: ma help)"
    exit 1
    ;;
esac
MAEOF
chmod +x "$BIN_DIR/ma"
echo -e "${G}✓ CLI installed: $BIN_DIR/ma${NC}"

# PATH
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
  for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
    if [ -f "$rc" ] && ! grep -q '.local/bin' "$rc"; then
      echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$rc"
    fi
  done
  echo -e "  ${B}Run: export PATH=\"\$HOME/.local/bin:\$PATH\"${NC}"
fi

# Deps
python3 -c "import fastapi" 2>/dev/null || python3 -m pip install --break-system-packages -q fastapi "uvicorn[standard]" sse-starlette websockets 2>/dev/null
echo -e "${G}✓ Dependencies OK${NC}"

echo -e "\n${BOLD}Done! Usage:${NC}"
echo -e "  ${G}cd /your/project && ma${NC}          # start"
echo -e "  ${G}ma status${NC}                        # check agents"
echo -e "  ${G}ma send backend 'add users API'${NC}  # send task"
echo -e "  ${G}ma help${NC}                          # all commands"
