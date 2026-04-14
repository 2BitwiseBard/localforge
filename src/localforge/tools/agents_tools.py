"""Agent management tools — list and view logs."""

import json

from localforge.paths import agent_state_dir
from localforge.tools import tool_handler

_agent_supervisor = None


@tool_handler(
    name="agent_list",
    description="List all configured autonomous agents and their status (running, idle, error).",
    schema={"type": "object", "properties": {}, "required": []},
)
async def agent_list(args: dict) -> str:
    try:
        import yaml as _yaml

        # Look for agents.yaml relative to config or data dir
        from localforge.paths import config_path
        agents_yaml = config_path().parent / "agents.yaml"
        if not agents_yaml.exists():
            return "No agents.yaml found."
        with open(agents_yaml) as f:
            agent_cfg = _yaml.safe_load(f) or {}

        agents = agent_cfg.get("agents", {})
        if not agents:
            return "No agents configured."

        parts = []
        for agent_id, acfg in agents.items():
            enabled = "enabled" if acfg.get("enabled", True) else "disabled"
            parts.append(
                f"  {agent_id}: type={acfg.get('type', agent_id)}, "
                f"trust={acfg.get('trust', 'monitor')}, "
                f"schedule={acfg.get('schedule', 'manual')}, "
                f"{enabled}"
            )
        return f"Configured agents ({len(agents)}):\n" + "\n".join(parts)
    except Exception as e:
        return f"Error: {e}"


@tool_handler(
    name="agent_logs",
    description="View recent log entries for an autonomous agent.",
    schema={
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "Agent ID to view logs for"},
            "lines": {"type": "integer", "description": "Number of log lines (default: 50)"},
        },
        "required": ["agent_id"],
    },
)
async def agent_logs(args: dict) -> str:
    import time as _time

    agent_id = args["agent_id"]
    lines = args.get("lines", 50)
    state_dir = agent_state_dir()
    state_file = state_dir / f"{agent_id}.json"

    if not state_file.exists():
        return f"No state found for agent '{agent_id}'. It may not have run yet."

    try:
        data = json.loads(state_file.read_text())
    except Exception:
        return f"Cannot read state for agent '{agent_id}'."

    parts = [
        f"Agent: {agent_id}",
        f"Status: {data.get('status', 'unknown')}",
        f"Run count: {data.get('run_count', 0)}",
    ]
    last_run = data.get("last_run", 0)
    if last_run:
        parts.append(f"Last run: {_time.strftime('%Y-%m-%d %H:%M:%S', _time.localtime(last_run))}")
    if data.get("last_error"):
        parts.append(f"Last error: {data['last_error']}")
    logs = data.get("logs", [])
    if logs:
        parts.append(f"\n── Recent logs ({len(logs)} entries) ──")
        for entry in logs[-lines:]:
            parts.append(entry)
    else:
        parts.append("\nNo log entries yet.")

    return "\n".join(parts)
