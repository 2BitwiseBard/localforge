#!/usr/bin/env bash
# =============================================================================
# AI Hub Compute Mesh — Device Worker Setup
# =============================================================================
#
# Run on any device on the Tailscale mesh to join the compute pool.
# Detects platform, installs minimal dependencies, deploys the worker agent.
#
# Usage:
#   curl -fsSL http://ai-hub:8100/static/setup-worker.sh | bash
#   # or copy this script to the device and run it:
#   bash setup-worker.sh
#   bash setup-worker.sh --hub ai-hub:8100 --port 8200
#
# Supports: Linux (x86/ARM), macOS (Intel/Apple Silicon), Android (Termux)
# =============================================================================

set -euo pipefail

HUB_URL="${HUB_URL:-http://ai-hub:8100}"
WORKER_PORT="${WORKER_PORT:-8200}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/.ai-hub-worker}"

# --- Parse args ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --hub)   HUB_URL="http://$2"; shift 2 ;;
        --port)  WORKER_PORT="$2"; shift 2 ;;
        --dir)   INSTALL_DIR="$2"; shift 2 ;;
        -h|--help)
            echo "Usage: setup-worker.sh [--hub HOST:PORT] [--port PORT] [--dir DIR]"
            echo ""
            echo "Options:"
            echo "  --hub   Hub gateway address (default: ai-hub:8100)"
            echo "  --port  Worker port (default: 8200)"
            echo "  --dir   Install directory (default: ~/.ai-hub-worker)"
            exit 0
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# --- Detect platform ---
detect_platform() {
    if [[ -d /data/data/com.termux ]]; then
        echo "termux"
    elif [[ "$(uname -s)" == "Darwin" ]]; then
        echo "macos"
    elif [[ "$(uname -s)" == "Linux" ]]; then
        echo "linux"
    else
        echo "unknown"
    fi
}

PLATFORM=$(detect_platform)
echo "=== AI Hub Worker Setup ==="
echo "Platform:    $PLATFORM"
echo "Hub:         $HUB_URL"
echo "Worker port: $WORKER_PORT"
echo "Install dir: $INSTALL_DIR"
echo ""

# --- Check/install Tailscale ---
check_tailscale() {
    if command -v tailscale &>/dev/null; then
        echo "[OK] Tailscale installed"
        if tailscale status &>/dev/null; then
            echo "[OK] Tailscale connected"
        else
            echo "[!!] Tailscale installed but not connected. Run: sudo tailscale up"
            exit 1
        fi
    else
        echo "[!!] Tailscale not installed."
        case "$PLATFORM" in
            linux)
                echo "Install: curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up"
                ;;
            macos)
                echo "Install: brew install tailscale (or download from https://tailscale.com/download/mac)"
                ;;
            termux)
                echo "Install: pkg install tailscale && tailscale up"
                ;;
        esac
        exit 1
    fi
}

check_tailscale

# --- Check/install Python ---
check_python() {
    local py=""
    for cmd in python3 python; do
        if command -v "$cmd" &>/dev/null; then
            py="$cmd"
            break
        fi
    done

    if [[ -z "$py" ]]; then
        echo "[!!] Python 3 not found."
        case "$PLATFORM" in
            linux)   echo "Install: sudo apt install python3 python3-pip python3-venv" ;;
            macos)   echo "Install: brew install python3" ;;
            termux)  echo "Install: pkg install python" ;;
        esac
        exit 1
    fi

    local ver
    ver=$("$py" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    echo "[OK] Python $ver ($py)"
    PYTHON="$py"
}

check_python

# --- Create install directory + venv ---
echo ""
echo "--- Setting up worker environment ---"
mkdir -p "$INSTALL_DIR"

if [[ ! -d "$INSTALL_DIR/venv" ]]; then
    echo "Creating virtual environment..."
    "$PYTHON" -m venv "$INSTALL_DIR/venv"
fi

PIP="$INSTALL_DIR/venv/bin/pip"
PY="$INSTALL_DIR/venv/bin/python"

# --- Install dependencies ---
echo "Installing dependencies..."
"$PIP" install --quiet --upgrade pip
"$PIP" install --quiet starlette uvicorn httpx

# Optional: fastembed for embeddings (CPU, ~200MB)
echo ""
read -rp "Install fastembed for CPU embeddings? (~200MB) [y/N] " install_embed
if [[ "${install_embed,,}" == "y" ]]; then
    "$PIP" install --quiet fastembed
    echo "[OK] fastembed installed"
fi

# Optional: piper-tts
if command -v piper &>/dev/null; then
    echo "[OK] piper TTS found"
fi

# Optional: whisper
if "$PY" -c "import faster_whisper" 2>/dev/null || "$PY" -c "import whisper" 2>/dev/null; then
    echo "[OK] Whisper STT found"
fi

# --- Copy worker files ---
echo ""
echo "--- Deploying worker files ---"

# Try to fetch from hub first, fall back to bundled copy
WORKER_SRC=""
if curl -fs "$HUB_URL/health" >/dev/null 2>&1; then
    echo "Hub reachable, but worker files must be copied manually for now."
fi

# Check if running on the hub machine itself (files are local)
HUB_WORKER_DIR="$HOME/.claude/mcp-servers/local-model/workers"
if [[ -f "$HUB_WORKER_DIR/device_worker.py" ]]; then
    cp "$HUB_WORKER_DIR/device_worker.py" "$INSTALL_DIR/"
    cp "$HUB_WORKER_DIR/detect.py" "$INSTALL_DIR/"
    echo "[OK] Copied worker files from local hub"
else
    # For remote devices, the files need to be transferred
    echo "[!!] Worker files not found locally."
    echo "     Copy these from the hub (ai-hub) to $INSTALL_DIR/:"
    echo "       scp ai-hub:~/.claude/mcp-servers/local-model/workers/device_worker.py $INSTALL_DIR/"
    echo "       scp ai-hub:~/.claude/mcp-servers/local-model/workers/detect.py $INSTALL_DIR/"
    echo ""
    read -rp "Have you copied the files? Continue? [y/N] " copied
    if [[ "${copied,,}" != "y" ]]; then
        echo "Setup paused. Copy the files and re-run."
        exit 0
    fi
fi

# Verify files exist
if [[ ! -f "$INSTALL_DIR/device_worker.py" || ! -f "$INSTALL_DIR/detect.py" ]]; then
    echo "[!!] Worker files missing from $INSTALL_DIR/"
    exit 1
fi

# --- Create launch script ---
cat > "$INSTALL_DIR/start.sh" << LAUNCH
#!/usr/bin/env bash
cd "$INSTALL_DIR"
exec "$PY" device_worker.py --port $WORKER_PORT --hub "$HUB_URL" "\$@"
LAUNCH
chmod +x "$INSTALL_DIR/start.sh"

# --- Create systemd service (Linux only) ---
if [[ "$PLATFORM" == "linux" && -d "$HOME/.config/systemd/user" ]]; then
    SERVICE_FILE="$HOME/.config/systemd/user/ai-hub-worker.service"
    cat > "$SERVICE_FILE" << SERVICE
[Unit]
Description=AI Hub Compute Mesh Worker
After=network.target tailscaled.service

[Service]
ExecStart=$PY $INSTALL_DIR/device_worker.py --port $WORKER_PORT --hub $HUB_URL
WorkingDirectory=$INSTALL_DIR
Restart=on-failure
RestartSec=10
Environment=HOME=$HOME

[Install]
WantedBy=default.target
SERVICE

    systemctl --user daemon-reload
    echo "[OK] Systemd service created: ai-hub-worker.service"
    echo "     Enable: systemctl --user enable --now ai-hub-worker"
fi

# --- Create launchd plist (macOS only) ---
if [[ "$PLATFORM" == "macos" ]]; then
    PLIST_DIR="$HOME/Library/LaunchAgents"
    PLIST_FILE="$PLIST_DIR/com.ai-hub.worker.plist"
    mkdir -p "$PLIST_DIR"
    cat > "$PLIST_FILE" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.ai-hub.worker</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PY</string>
        <string>$INSTALL_DIR/device_worker.py</string>
        <string>--port</string>
        <string>$WORKER_PORT</string>
        <string>--hub</string>
        <string>$HUB_URL</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$INSTALL_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$INSTALL_DIR/worker.log</string>
    <key>StandardErrorPath</key>
    <string>$INSTALL_DIR/worker.err</string>
</dict>
</plist>
PLIST

    echo "[OK] LaunchAgent created: com.ai-hub.worker"
    echo "     Enable: launchctl load $PLIST_FILE"
fi

# --- Termux boot script ---
if [[ "$PLATFORM" == "termux" ]]; then
    BOOT_DIR="$HOME/.termux/boot"
    mkdir -p "$BOOT_DIR"
    cat > "$BOOT_DIR/ai-hub-worker.sh" << BOOT
#!/data/data/com.termux/files/usr/bin/bash
termux-wake-lock
cd "$INSTALL_DIR"
"$PY" device_worker.py --port $WORKER_PORT --hub "$HUB_URL" &
BOOT
    chmod +x "$BOOT_DIR/ai-hub-worker.sh"
    echo "[OK] Termux boot script created"
    echo "     Install Termux:Boot app for auto-start on boot"
fi

# --- Test ---
echo ""
echo "--- Testing worker ---"
"$PY" "$INSTALL_DIR/detect.py"

echo ""
echo "=== Setup complete ==="
echo ""
echo "Start the worker:"
echo "  $INSTALL_DIR/start.sh"
echo ""
case "$PLATFORM" in
    linux)
        echo "Auto-start on boot:"
        echo "  systemctl --user enable --now ai-hub-worker"
        ;;
    macos)
        echo "Auto-start on login:"
        echo "  launchctl load ~/Library/LaunchAgents/com.ai-hub.worker.plist"
        ;;
    termux)
        echo "Auto-start on boot:"
        echo "  Install the Termux:Boot app from F-Droid"
        ;;
esac
echo ""
echo "Verify from hub:"
echo "  ~/bin/local call compute_status"
