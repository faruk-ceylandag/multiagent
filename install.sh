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

for d in hub/routers hub/dashboard agents lib ecosystem/mcp ecosystem/hooks ecosystem/templates ecosystem/subagents ecosystem/commands; do mkdir -p "$INSTALL_DIR/$d"; done

cp "$SRC/start.py" "$INSTALL_DIR/"

# Hub package (full modular structure)
cp "$SRC/hub/"*.py "$INSTALL_DIR/hub/"
cp "$SRC/hub/routers/"*.py "$INSTALL_DIR/hub/routers/"
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
[ -f "$_PORT_FILE" ] && _PORT=$(cat "$_PORT_FILE") || _PORT=8040
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
    icon={'created':'📋','assigned':'👤','in_progress':'⚙️','done':'✅','failed':'❌','cancelled':'🚫'}.get(s,'•')
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
    pkill -f "agent_worker.py" 2>/dev/null
    pkill -f "hub_server:app" 2>/dev/null
    pkill -f "start.py" 2>/dev/null
    echo "  ✓ All stopped"
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
    echo "  export             Export session report"
    echo "  kill               Stop everything"
    echo ""
    echo "Examples:"
    echo "  ma                           # start in current dir"
    echo "  ma send backend 'add API'    # send task to backend"
    echo "  ma send all 'refactor auth'  # broadcast to all"
    echo "  ma stop frontend             # cancel frontend task"
    echo "  ma tail qa                   # follow QA logs"
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
