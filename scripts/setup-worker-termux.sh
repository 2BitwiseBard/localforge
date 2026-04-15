#!/data/data/com.termux/files/usr/bin/bash
# Setup a Samsung Android device (via Termux) as a LocalForge mesh worker.
# Target: older Samsung phones — the S25 Ultra stays a pure dashboard client.
#
# Prereqs (install once, via F-Droid — NOT Play Store for Termux:Boot):
#   - Termux        (https://f-droid.org/packages/com.termux/)
#   - Termux:Boot   (https://f-droid.org/packages/com.termux.boot/)
#   - Samsung battery optimization exemption:
#       Settings -> Battery -> Background usage limits -> Never sleeping apps -> Termux
#
# Usage:
#   curl -fsSL 'http://ai-hub:8100/api/mesh/install-script?platform=android&token=<TOK>' | \
#       bash -s -- --hub http://ai-hub:8100 --token <TOK>
#
# Light compute only: embeddings / classification / rerank / autocomplete.
# No LLM inference on phone workers.
set -euo pipefail

HUB_URL=""
API_KEY=""
ENROLL_TOKEN=""
WORKER_PORT=8200
GIT_REPO="https://github.com/2BitwiseBard/localforge"
INSTALL_DIR="$HOME/.localforge"

while [[ $# -gt 0 ]]; do
    case $1 in
        --hub)   HUB_URL="$2"; shift 2 ;;
        --key)   API_KEY="$2"; shift 2 ;;
        --token) ENROLL_TOKEN="$2"; shift 2 ;;
        --port)  WORKER_PORT="$2"; shift 2 ;;
        --repo)  GIT_REPO="$2"; shift 2 ;;
        --help|-h) echo "Usage: $0 --hub URL (--token TOK | --key KEY)"; exit 0 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

echo "=== LocalForge Termux Worker Setup ==="
[[ -z "$HUB_URL" ]] && { echo "Error: --hub required"; exit 1; }
[[ -z "$API_KEY" && -z "$ENROLL_TOKEN" ]] && { echo "Error: --token or --key required"; exit 1; }

# --- 1. Termux packages --------------------------------------------------
echo ""
echo "Installing Termux packages..."
pkg update -y >/dev/null
pkg install -y python git curl termux-api termux-tools

# termux-wake-lock keeps CPU awake for network heartbeats
command -v termux-wake-lock >/dev/null && termux-wake-lock

# --- 2. Install localforge (no venv on Termux — system Python is per-user) ---
mkdir -p "$INSTALL_DIR"
echo "Installing localforge[worker]..."
pip install --upgrade pip >/dev/null
pip install "localforge[worker] @ git+$GIT_REPO"

WORKER_BIN="$(command -v localforge-worker || echo "$PREFIX/bin/localforge-worker")"
[[ -x "$WORKER_BIN" ]] || { echo "Error: localforge-worker not on PATH"; exit 1; }

# --- 3. Detect hardware --------------------------------------------------
echo ""
echo "Detecting hardware..."
HW_JSON=$(python -c "import json; from localforge.workers.detect import detect; print(json.dumps(detect().to_dict()))")
python -c "
import json, sys
hw = json.loads('''$HW_JSON''')
for k in ('platform','gpu_type','ram_mb','cpu_cores','tier','battery_pct'):
    print(f'  {k}: {hw.get(k)}')
"

# --- 4. Register ---------------------------------------------------------
if [[ -n "$ENROLL_TOKEN" ]]; then
    echo "Registering with hub..."
    REG_BODY=$(python - <<EOF
import json, os, socket
print(json.dumps({
    "enrollment_token": "$ENROLL_TOKEN",
    "hostname": socket.gethostname(),
    "platform": "android",
    "hardware": json.loads('''$HW_JSON'''),
}))
EOF
)
    REG_RESP=$(curl -fsSL -X POST "$HUB_URL/api/mesh/register" \
                    -H "Content-Type: application/json" \
                    -d "$REG_BODY")
    API_KEY=$(echo "$REG_RESP" | python -c "import sys,json; print(json.load(sys.stdin)['api_key'])")
    WORKER_ID=$(echo "$REG_RESP" | python -c "import sys,json; print(json.load(sys.stdin)['worker_id'])")
    echo "  Registered as: $WORKER_ID"
fi

# --- 5. Env file (0600) --------------------------------------------------
ENV_FILE="$INSTALL_DIR/env"
cat > "$ENV_FILE" <<EOF
export LOCALFORGE_HUB_URL="$HUB_URL"
export LOCALFORGE_API_KEY="$API_KEY"
export LOCALFORGE_WORKER_PORT="$WORKER_PORT"
# Phone worker — light compute only, battery-aware throttling
export LOCALFORGE_MIN_BATTERY=25
export LOCALFORGE_MAX_CONCURRENT=1
EOF
chmod 600 "$ENV_FILE"

# --- 6. Termux:Boot auto-start script -----------------------------------
BOOT_DIR="$HOME/.termux/boot"
mkdir -p "$BOOT_DIR"
BOOT_SCRIPT="$BOOT_DIR/localforge-worker"
cat > "$BOOT_SCRIPT" <<EOF
#!/data/data/com.termux/files/usr/bin/bash
# Auto-start the LocalForge worker when Termux:Boot fires on device boot.
termux-wake-lock
source "$ENV_FILE"
# Re-exec into a proper login shell so PATH picks up pip-installed binary
exec "$WORKER_BIN" \\
    --port "\$LOCALFORGE_WORKER_PORT" \\
    --hub "\$LOCALFORGE_HUB_URL" \\
    --platform android \\
    > "$INSTALL_DIR/worker.log" 2>&1
EOF
chmod +x "$BOOT_SCRIPT"

# --- 7. Start right now --------------------------------------------------
echo ""
echo "Starting worker now..."
# shellcheck source=/dev/null
source "$ENV_FILE"
nohup "$WORKER_BIN" \
    --port "$WORKER_PORT" \
    --hub "$HUB_URL" \
    --platform android \
    > "$INSTALL_DIR/worker.log" 2>&1 &

sleep 3
if curl -fsS "http://localhost:$WORKER_PORT/health" >/dev/null 2>&1; then
    echo "  Worker is up on :$WORKER_PORT"
else
    echo "  Warning: worker health check failed — see $INSTALL_DIR/worker.log"
fi

echo ""
echo "=== Setup Complete ==="
echo "Samsung: confirm Termux is listed under Settings -> Battery ->"
echo "         Background usage limits -> Never sleeping apps."
echo ""
echo "Logs:    tail -f $INSTALL_DIR/worker.log"
echo "Boot:    $BOOT_SCRIPT (runs via Termux:Boot)"
echo "Health:  curl http://localhost:$WORKER_PORT/health"
echo "Hub:     $HUB_URL/api/mesh/status"
