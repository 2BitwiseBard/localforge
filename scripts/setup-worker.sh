#!/bin/bash
# Setup a new Linux device as a LocalForge mesh worker.
#
# Recommended (enrollment flow):
#   curl -fsSL 'http://ai-hub:8100/api/mesh/install-script?platform=linux&token=<TOK>' | \
#       bash -s -- --hub http://ai-hub:8100 --token <TOK>
#
# Direct (legacy, already have a worker key):
#   ./scripts/setup-worker.sh --hub http://ai-hub:8100 --key <WORKER_KEY>
#
# What this does:
#   1. Installs localforge[worker] (pip from git until PyPI publish)
#   2. If --token: exchanges enrollment token for a long-lived worker API key
#   3. Detects hardware, creates systemd --user service
#   4. Enables + starts the service
set -euo pipefail

HUB_URL=""
API_KEY=""
ENROLL_TOKEN=""
WORKER_PORT=8200
GIT_REPO="https://github.com/2BitwiseBard/localforge"

while [[ $# -gt 0 ]]; do
    case $1 in
        --hub)     HUB_URL="$2"; shift 2 ;;
        --key)     API_KEY="$2"; shift 2 ;;
        --token)   ENROLL_TOKEN="$2"; shift 2 ;;
        --port)    WORKER_PORT="$2"; shift 2 ;;
        --repo)    GIT_REPO="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: $0 --hub HOST:PORT (--token TOK | --key API_KEY) [--port PORT]"
            exit 0 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

echo "=== LocalForge Linux Worker Setup ==="
[[ -z "$HUB_URL" ]]  && { echo "Error: --hub required"; exit 1; }
[[ -z "$API_KEY" && -z "$ENROLL_TOKEN" ]] && { echo "Error: --token or --key required"; exit 1; }

# Python 3.11+ check. localforge's pyproject.toml enforces this, but a clear
# error here is friendlier than pip's "Requires-Python" message.
PY=""
for cmd in python3.13 python3.12 python3.11 python3; do
    if command -v "$cmd" >/dev/null; then
        if "$cmd" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
            PY="$cmd"; break
        fi
    fi
done
if [[ -z "$PY" ]]; then
    echo "Error: Python 3.11+ is required. Found:"
    for cmd in python3 python3.10 python3.11 python3.12 python3.13; do
        command -v "$cmd" >/dev/null && echo "  $cmd -> $($cmd --version 2>&1)"
    done
    echo "Install a newer Python (apt/dnf/pacman python3.11 or pyenv) and re-run."
    exit 1
fi
echo "Python: $($PY --version)"

# Stop any existing worker service so pip can replace its binary cleanly.
if systemctl --user is-active --quiet localforge-worker 2>/dev/null; then
    echo "Stopping existing localforge-worker service for upgrade..."
    systemctl --user stop localforge-worker || true
fi

# --- 1. Install localforge[worker] ---------------------------------------
echo ""
echo "Installing localforge[worker]..."
if command -v uv >/dev/null; then
    uv pip install --python "$PY" --user "localforge[worker] @ git+$GIT_REPO"
else
    "$PY" -m pip install --user --upgrade pip
    "$PY" -m pip install --user "localforge[worker] @ git+$GIT_REPO"
fi

# Ensure ~/.local/bin is on PATH for this shell (the service uses an absolute path anyway)
export PATH="$HOME/.local/bin:$PATH"
WORKER_BIN="$(command -v localforge-worker || echo "$HOME/.local/bin/localforge-worker")"
[[ -x "$WORKER_BIN" ]] || { echo "Error: localforge-worker not on PATH after install"; exit 1; }

# --- 2. Detect hardware --------------------------------------------------
echo ""
echo "Detecting hardware..."
HW_JSON=$("$PY" -c "import json; from localforge.workers.detect import detect; print(json.dumps(detect().to_dict()))")
PLATFORM=$(echo "$HW_JSON" | "$PY" -c "import sys,json; print(json.load(sys.stdin)['platform'])")
TIER=$(echo "$HW_JSON"     | "$PY" -c "import sys,json; print(json.load(sys.stdin)['tier'])")
echo "  Platform: $PLATFORM"
echo "  Tier:     $TIER"

# --- 3. Exchange enrollment token for worker API key ---------------------
if [[ -n "$ENROLL_TOKEN" ]]; then
    echo ""
    echo "Registering with hub..."
    REG_BODY=$("$PY" - <<EOF
import json, os
print(json.dumps({
    "enrollment_token": "$ENROLL_TOKEN",
    "hostname": os.uname().nodename,
    "platform": "linux",
    "hardware": json.loads('''$HW_JSON'''),
}))
EOF
)
    REG_RESP=$(curl -fsSL -X POST "$HUB_URL/api/mesh/register" \
                    -H "Content-Type: application/json" \
                    -d "$REG_BODY")
    API_KEY=$(echo "$REG_RESP" | "$PY" -c "import sys,json; print(json.load(sys.stdin)['api_key'])")
    WORKER_ID=$(echo "$REG_RESP" | "$PY" -c "import sys,json; print(json.load(sys.stdin)['worker_id'])")
    echo "  Registered as: $WORKER_ID"
fi

# --- 4. Persist env file (0600) ------------------------------------------
ENV_DIR="$HOME/.config/localforge"
mkdir -p "$ENV_DIR"
ENV_FILE="$ENV_DIR/worker.env"
cat > "$ENV_FILE" <<EOF
LOCALFORGE_HUB_URL=$HUB_URL
LOCALFORGE_API_KEY=$API_KEY
LOCALFORGE_WORKER_PORT=$WORKER_PORT
EOF
chmod 600 "$ENV_FILE"

# --- 5. systemd --user service ------------------------------------------
echo ""
echo "Installing systemd service..."
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/localforge-worker.service" <<EOF
[Unit]
Description=LocalForge Mesh Worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=$ENV_FILE
ExecStart=$WORKER_BIN --port \${LOCALFORGE_WORKER_PORT} --hub \${LOCALFORGE_HUB_URL}
Restart=on-failure
RestartSec=5s
StartLimitIntervalSec=60
StartLimitBurst=3

# Security hardening
PrivateTmp=true
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=%h

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now localforge-worker
sleep 2

echo ""
echo "=== Setup Complete ==="
systemctl --user status localforge-worker --no-pager || true
echo ""
echo "Logs:    journalctl --user -u localforge-worker -f"
echo "Health:  curl http://localhost:$WORKER_PORT/health"
echo "Hub:     $HUB_URL/api/mesh/status  (worker should appear within 30s)"
