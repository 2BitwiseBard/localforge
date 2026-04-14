# Multi-Device Setup

LocalForge can connect multiple machines into a compute mesh, routing tasks to the best available device.

## Overview

```
┌─────────────────────────────────┐
│  Primary (GPU)                  │
│  LocalForge gateway (:8100)     │
│  text-gen-webui (:5000)         │
│  GPU pool + routing             │
└──────────┬──────────────────────┘
           │  Tailscale mesh
     ┌─────┼─────┐
     │     │     │
  [Laptop] [Phone] [VPS]
  :5000    Dashboard  Agents
```

## Requirements

- [Tailscale](https://tailscale.com) installed on all devices
- Each device joins the same Tailnet

## Hub Setup (Primary Device)

1. Install LocalForge with gateway support:
   ```bash
   pip install "localforge[all]"
   ```

2. Configure backends in `config.yaml`:
   ```yaml
   backends:
     local:
       url: http://localhost:5000/v1
       priority: 1
   
   gpu_pool:
     auto_discover: true
     discovery_interval: 60
   ```

3. Start the gateway:
   ```bash
   localforge-gateway --port 8100
   ```

## Adding a Device

### Option 1: Worker Node (recommended)

Workers are lightweight HTTP servers that join the mesh, push heartbeats, and accept routed tasks.

```bash
# One-command setup on the new device:
./scripts/setup-worker.sh --hub ai-hub:8100 --key YOUR_KEY

# Or manually:
pip install "localforge[gateway]"
localforge-worker --hub ai-hub:8100 --port 8200
```

The worker auto-detects GPU, RAM, CPU, and capabilities (inference, embeddings, TTS, STT, vision). It pushes heartbeats every 30s so the hub always knows what's available.

Workers appear in the dashboard's Compute Mesh section and in `compute_status` output.

### Option 2: Full Backend

Run text-generation-webui on the remote device — the GPU pool auto-discovers it via Tailscale:

1. Install Tailscale and join the mesh
2. Start text-generation-webui with `--api --listen`
3. The GPU pool auto-discovers the device via `tailscale status`

Or add it manually in config.yaml:

```yaml
backends:
  local:
    url: http://localhost:5000/v1
    priority: 1
  second-gpu:
    url: http://second-laptop:5000/v1
    priority: 2
    optional: true
```

## Accessing from Remote Devices

### CLI

```bash
# Install the CLI on the remote device
pip install localforge

# Configure
cat > ~/.config/localforge/config.yaml << EOF
endpoint: http://ai-hub:8100
api_key: "your-key-here"
EOF

# Use
local health
local chat "explain monads"
```

### IDE (Claude Code, Kiro, etc.)

Use HTTP MCP transport:

```json
{
  "local-model": {
    "url": "http://ai-hub:8100/mcp/",
    "headers": {"Authorization": "Bearer YOUR_API_KEY"}
  }
}
```

### Dashboard (Phone/Tablet)

Open `http://ai-hub:8100` in a browser. Install as PWA for app-like experience.

## Model Routing

The GPU pool routes tasks to the best device based on loaded models. As of April 2026,
`client.py chat()` is wired directly into `gpu_pool.route_request()` — all 112 tools
are mesh-aware without any per-tool changes.

Tools set a task type hint via `task_type_context()` before calling `chat()`:

```python
from localforge.client import chat, task_type_context

# In a code analysis tool:
async with task_type_context("code"):
    result = await chat(prompt)
```

The GPU pool uses the task type to pick the best backend:

```yaml
gpu_pool:
  model_routing:
    code: ["Qwen3-Coder", "Devstral"]
    vision: ["Qwen3-VL"]
    reasoning: ["Qwen3.5-27B"]
    fast: ["gemma-3n"]
```

If no backend matches the task type, it falls back to the least-loaded healthy backend.
On failure, it tries all other known backends (config + pool) before raising an error.

Use `compute_status` to see all connected devices, `compute_route` to preview routing decisions, and `mesh_dispatch` to send a task to a specific worker:

```python
# Auto-route an embeddings task to the best available worker
mesh_dispatch(task_type="embeddings", payload={"texts": ["hello world"]})

# Send to a specific worker
mesh_dispatch(task_type="chat", payload={"prompt": "explain monads"}, target="laptop2:8200")
```

### Worker Endpoints

Each worker exposes:

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/health` | GET | Capabilities, tier, load, uptime |
| `/status` | GET | Detailed status + task history |
| `/task` | POST | Execute a task (chat, embeddings, tts, stt, classify, rerank) |
| `/task/cancel` | POST | Cancel a running task |
| `/metrics` | GET | System resource usage (CPU, RAM, GPU) |
