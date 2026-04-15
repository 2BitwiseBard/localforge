#!/bin/bash
# Setup a macOS device as a LocalForge mesh worker.
#
# Recommended (enrollment flow):
#   curl -fsSL 'http://ai-hub:8100/api/mesh/install-script?platform=darwin&token=<TOK>' | \
#       bash -s -- --hub http://ai-hub:8100 --token <TOK>
#
# What this does:
#   1. Checks for Python 3.11+ (offers brew install if absent)
#   2. Creates venv at ~/Library/Application Support/LocalForge/venv
#   3. Installs localforge[worker]; if MLX is importable, logs it on first run
#   4. Exchanges enrollment token for a worker API key
#   5. Writes ~/Library/LaunchAgents/com.localforge.worker.plist
#   6. Bootstraps the agent via launchctl
set -euo pipefail

HUB_URL=""
API_KEY=""
ENROLL_TOKEN=""
WORKER_PORT=8200
GIT_REPO="https://github.com/2BitwiseBard/localforge"
APP_SUPPORT="$HOME/Library/Application Support/LocalForge"

while [[ $# -gt 0 ]]; do
    case $1 in
        --hub)   HUB_URL="$2"; shift 2 ;;
        --key)   API_KEY="$2"; shift 2 ;;
        --token) ENROLL_TOKEN="$2"; shift 2 ;;
        --port)  WORKER_PORT="$2"; shift 2 ;;
        --repo)  GIT_REPO="$2"; shift 2 ;;
        --help|-h) echo "Usage: $0 --hub URL (--token TOK | --key KEY) [--port PORT]"; exit 0 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

echo "=== LocalForge macOS Worker Setup ==="
[[ -z "$HUB_URL" ]] && { echo "Error: --hub required"; exit 1; }
[[ -z "$API_KEY" && -z "$ENROLL_TOKEN" ]] && { echo "Error: --token or --key required"; exit 1; }

# --- 1. Python -----------------------------------------------------------
PY=""
for cmd in python3.12 python3.11 python3; do
    if command -v "$cmd" >/dev/null; then
        ver=$("$cmd" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
        major=${ver%%.*}; minor=${ver##*.}
        if [[ $major -eq 3 && $minor -ge 11 ]]; then PY="$cmd"; break; fi
    fi
done

if [[ -z "$PY" ]]; then
    if command -v brew >/dev/null; then
        echo "Python 3.11+ not found. Installing via brew..."
        brew install python@3.12
        PY="$(brew --prefix)/bin/python3.12"
    else
        echo "Error: Python 3.11+ required. Install from python.org or Homebrew."
        exit 1
    fi
fi
echo "Python: $("$PY" --version)"

# Stop any existing launchd agent so pip can cleanly replace the binary.
UID_NUM=$(id -u)
if launchctl print "gui/$UID_NUM/com.localforge.worker" >/dev/null 2>&1; then
    echo "Stopping existing com.localforge.worker agent for upgrade..."
    launchctl bootout "gui/$UID_NUM/com.localforge.worker" 2>/dev/null || true
fi

# --- 2. Venv + install ---------------------------------------------------
mkdir -p "$APP_SUPPORT"
VENV="$APP_SUPPORT/venv"
VENV_PY="$VENV/bin/python"

# If an existing venv is pinned to Python <3.11, rebuild it. Prior failed
# runs may have baked an older interpreter in before the host got upgraded.
if [[ -x "$VENV_PY" ]]; then
    if ! "$VENV_PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
        echo "Existing venv uses $("$VENV_PY" --version 2>&1) (need 3.11+). Rebuilding."
        rm -rf "$VENV"
    fi
fi
if [[ ! -d "$VENV" ]]; then
    echo "Creating venv at $VENV"
    "$PY" -m venv "$VENV"
fi
VENV_PIP="$VENV/bin/pip"
WORKER_BIN="$VENV/bin/localforge-worker"

"$VENV_PIP" install --quiet --upgrade pip
echo "Installing localforge[worker]..."
"$VENV_PIP" install --quiet "localforge[worker] @ git+$GIT_REPO"

# MLX is optional — Apple Silicon only, wheels are fat; try but don't block
if "$VENV_PY" -c "import platform; assert platform.machine() == 'arm64'" 2>/dev/null; then
    echo "Apple Silicon detected — attempting MLX install (optional)..."
    "$VENV_PIP" install --quiet mlx mlx-lm || echo "  MLX install skipped; worker will run without it."
fi

# --- 3. Detect hardware --------------------------------------------------
HW_JSON=$("$VENV_PY" -c "import json; from localforge.workers.detect import detect; print(json.dumps(detect().to_dict()))")
echo "Hardware:"
echo "$HW_JSON" | "$VENV_PY" -c "
import json, sys
hw = json.load(sys.stdin)
for k in ('platform','gpu_name','gpu_type','vram_mb','ram_mb','cpu_cores','tier','mlx_available'):
    print(f'  {k}: {hw.get(k)}')
"

# --- 4. Register ---------------------------------------------------------
if [[ -n "$ENROLL_TOKEN" ]]; then
    echo "Registering with hub..."
    REG_BODY=$("$VENV_PY" - <<EOF
import json, os, socket
print(json.dumps({
    "enrollment_token": "$ENROLL_TOKEN",
    "hostname": socket.gethostname(),
    "platform": "darwin",
    "hardware": json.loads('''$HW_JSON'''),
}))
EOF
)
    REG_RESP=$(curl -fsSL -X POST "$HUB_URL/api/mesh/register" \
                    -H "Content-Type: application/json" \
                    -d "$REG_BODY")
    API_KEY=$(echo "$REG_RESP" | "$VENV_PY" -c "import sys,json; print(json.load(sys.stdin)['api_key'])")
    WORKER_ID=$(echo "$REG_RESP" | "$VENV_PY" -c "import sys,json; print(json.load(sys.stdin)['worker_id'])")
    echo "  Registered as: $WORKER_ID"
fi

# --- 5. Env file (0600) --------------------------------------------------
ENV_FILE="$APP_SUPPORT/env"
cat > "$ENV_FILE" <<EOF
LOCALFORGE_HUB_URL=$HUB_URL
LOCALFORGE_API_KEY=$API_KEY
LOCALFORGE_WORKER_PORT=$WORKER_PORT
EOF
chmod 600 "$ENV_FILE"

# --- 6. launchd plist ----------------------------------------------------
PLIST_DIR="$HOME/Library/LaunchAgents"
mkdir -p "$PLIST_DIR"
PLIST="$PLIST_DIR/com.localforge.worker.plist"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>                <string>com.localforge.worker</string>
  <key>ProgramArguments</key>     <array>
    <string>$WORKER_BIN</string>
    <string>--port</string><string>$WORKER_PORT</string>
    <string>--hub</string><string>$HUB_URL</string>
  </array>
  <key>EnvironmentVariables</key> <dict>
    <key>LOCALFORGE_HUB_URL</key>      <string>$HUB_URL</string>
    <key>LOCALFORGE_API_KEY</key>      <string>$API_KEY</string>
    <key>LOCALFORGE_WORKER_PORT</key>  <string>$WORKER_PORT</string>
  </dict>
  <key>RunAtLoad</key>            <true/>
  <key>KeepAlive</key>            <true/>
  <key>StandardOutPath</key>      <string>$APP_SUPPORT/worker.out.log</string>
  <key>StandardErrorPath</key>    <string>$APP_SUPPORT/worker.err.log</string>
  <key>ThrottleInterval</key>     <integer>5</integer>
</dict>
</plist>
EOF
chmod 600 "$PLIST"

# --- 7. Bootstrap --------------------------------------------------------
# macOS 11+: prefer launchctl bootstrap; older: launchctl load.
# $UID_NUM was set above before we pip-installed, so it's already in scope.
launchctl bootstrap "gui/$UID_NUM" "$PLIST"
launchctl enable "gui/$UID_NUM/com.localforge.worker"

sleep 2

echo ""
echo "=== Setup Complete ==="
echo "Status:  launchctl print gui/$UID_NUM/com.localforge.worker"
echo "Stop:    launchctl bootout gui/$UID_NUM/com.localforge.worker"
echo "Logs:    tail -f '$APP_SUPPORT/worker.err.log'"
echo "Health:  curl http://localhost:$WORKER_PORT/health"
