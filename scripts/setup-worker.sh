#!/bin/bash
# Setup a new device as a LocalForge mesh worker.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/bitwisebard/localforge/main/scripts/setup-worker.sh | bash
#   # or locally:
#   ./scripts/setup-worker.sh [--hub ai-hub:8100] [--key YOUR_API_KEY] [--port 8200]
#
# What this does:
#   1. Installs localforge with gateway deps (pip)
#   2. Detects hardware capabilities
#   3. Creates systemd user service for the worker
#   4. Optionally enables auto-start on boot

set -euo pipefail

# Defaults
HUB_URL=""
API_KEY=""
WORKER_PORT=8200
INSTALL_DIR="$HOME/.local"

# Parse args
while [[ $# -gt 0 ]]; do
    case $1 in
        --hub)     HUB_URL="$2"; shift 2 ;;
        --key)     API_KEY="$2"; shift 2 ;;
        --port)    WORKER_PORT="$2"; shift 2 ;;
        --help|-h) echo "Usage: $0 [--hub HOST:PORT] [--key API_KEY] [--port PORT]"; exit 0 ;;
        *)         echo "Unknown option: $1"; exit 1 ;;
    esac
done

echo "=== LocalForge Worker Setup ==="
echo ""

# Check Python
if ! command -v python3 &>/dev/null; then
    echo "Error: python3 not found. Install Python 3.11+ first."
    exit 1
fi

PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "Python: $PY_VERSION"

# Install localforge
echo ""
echo "Installing localforge..."
if command -v uv &>/dev/null; then
    uv pip install --user "localforge[gateway]"
elif command -v pip3 &>/dev/null; then
    pip3 install --user "localforge[gateway]"
else
    echo "Error: Neither uv nor pip3 found."
    exit 1
fi

# Detect hardware
echo ""
echo "Detecting hardware..."
python3 -c "
from localforge.workers.detect import detect
hw = detect()
print(f'  Platform:     {hw.platform}')
print(f'  GPU:          {hw.gpu_name or \"none\"} ({hw.gpu_type})')
print(f'  VRAM:         {hw.vram_mb} MB')
print(f'  RAM:          {hw.ram_mb} MB')
print(f'  CPU cores:    {hw.cpu_cores}')
print(f'  Tier:         {hw.tier()}')
caps = [k for k in ('inference','embeddings','tts','stt','vision','reranking') if getattr(hw, k)]
print(f'  Capabilities: {\", \".join(caps)}')
"

# Create systemd service
echo ""
echo "Creating systemd service..."
mkdir -p "$HOME/.config/systemd/user"

WORKER_BIN=$(command -v localforge-worker 2>/dev/null || echo "$HOME/.local/bin/localforge-worker")

HUB_FLAG=""
if [ -n "$HUB_URL" ]; then
    HUB_FLAG="--hub $HUB_URL"
fi

KEY_FLAG=""
if [ -n "$API_KEY" ]; then
    KEY_FLAG="--key $API_KEY"
fi

cat > "$HOME/.config/systemd/user/localforge-worker.service" << EOF
[Unit]
Description=LocalForge Mesh Worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$WORKER_BIN --port $WORKER_PORT $HUB_FLAG $KEY_FLAG
Restart=on-failure
RestartSec=5s
StartLimitIntervalSec=60
StartLimitBurst=3
Environment=LOCALFORGE_API_KEY=${API_KEY}

# Security hardening
PrivateTmp=true
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=%h

[Install]
WantedBy=default.target
EOF

echo "  Service file: ~/.config/systemd/user/localforge-worker.service"

# Reload systemd
systemctl --user daemon-reload

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Commands:"
echo "  Start:   systemctl --user start localforge-worker"
echo "  Enable:  systemctl --user enable localforge-worker   (auto-start on boot)"
echo "  Status:  systemctl --user status localforge-worker"
echo "  Logs:    journalctl --user -u localforge-worker -f"
echo ""
echo "Test:    curl http://localhost:$WORKER_PORT/health"
if [ -n "$HUB_URL" ]; then
    echo "Hub:     $HUB_URL (heartbeats every 30s)"
fi
echo ""

read -p "Start the worker now? [Y/n] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]] || [[ -z $REPLY ]]; then
    systemctl --user start localforge-worker
    sleep 2
    systemctl --user status localforge-worker --no-pager || true
fi
