# Agent Development

LocalForge includes an autonomous agent framework with trust-gated execution.

## Built-in Agents

| Agent | Trust | Schedule | Purpose |
|-------|-------|----------|---------|
| health-monitor | monitor | */5 min | Ping services, alert on failures |
| index-maintainer | safe | */30 min | Keep RAG indexes up to date |
| code-watcher | safe | */60 min | Review recent git diffs |
| research-agent | safe | webhook | Web research on demand |
| news-agent | safe | */360 min | Scrape news by topic |
| daily-digest | safe | daily 20:00 | Aggregate daily summary |

## Trust Levels

- **monitor** — read-only. Can check status, search, and report. Also has `compute_status`/`compute_route`.
- **safe** — can read files, make API calls, save notes, dispatch to mesh workers. Cannot modify code or run shell commands.
- **full** — unrestricted, but destructive actions (swap_model, unload_model, delete_index, etc.) go through the **approval queue**. A human must approve via the dashboard before the action executes.

## Creating a Custom Agent

1. Create a new file in `src/localforge/agents/`:

```python
"""My custom agent."""

import logging
from localforge.agents.base import BaseAgent, agent

log = logging.getLogger("localforge.agents.my_agent")


@agent("my-agent")
class MyAgent(BaseAgent):
    """Description of what this agent does."""

    async def run(self) -> None:
        """Called on each scheduled execution."""
        log.info("My agent is running")
        
        # Use the gateway API to call any MCP tool
        result = await self.call_tool("health_check", {})
        log.info("Health: %s", result)
        
        # Send notification to dashboard
        await self.notify("My agent completed", level="info")
        
        # Save findings to notes
        await self.call_tool("save_note", {
            "topic": "my-agent-findings",
            "content": result,
        })
```

2. Import it in `gateway.py`'s lifespan block:
```python
import localforge.agents.my_agent  # noqa: F401
```

3. Configure in `agents.yaml`:
```yaml
agents:
  my-agent:
    enabled: true
    trust: safe
    schedule: "*/30"  # every 30 minutes
    # Or use triggers:
    # triggers:
    #   - type: webhook
    #   - type: file_watch
    #     paths: ["~/projects/"]
    #   - type: chain
    #     after: health-monitor
```

## Agent API

Agents inherit from `BaseAgent` and have access to:

- `self.call_tool(name, args)` — call any MCP tool via the gateway (trust-gated + approval-gated)
- `self.call_mesh(task_type, payload)` — dispatch work to a mesh worker (auto-routed)
- `self.notify(title, body, level)` — push notification to dashboard (SSE) + save to notes
- `self.send_message(topic, payload)` — publish to the message bus
- `self.receive_messages()` — consume messages from the bus
- `self.spawn_child(agent_type, config)` — spawn a child agent via the supervisor
- `self.enqueue_task(payload)` — add a task to the shared task queue
- `self.dequeue_task()` — claim a task from the queue
- `self.config` — agent-specific config from agents.yaml
- `self.state` — persistent state (status, logs, run count, timing)

## Approval Queue

Tools in the `APPROVAL_REQUIRED` set trigger an approval flow when called by FULL-trust agents:

1. Agent calls `self.call_tool("swap_model", {...})`
2. Request is placed in the approval queue (SQLite-backed, 5-minute TTL)
3. Dashboard SSE notification appears with approve/deny buttons
4. Agent blocks until human decides (or TTL expires → auto-deny)

Default approval-required tools: `swap_model`, `unload_model`, `delete_index`, `delete_note`, `delete_session`, `set_generation_params`, `reload_config`.

## Event Triggers

- **cron** — standard cron expression (`schedule: "*/5"` = every 5 minutes)
- **file_watch** — inotify/watchdog on specified paths
- **webhook** — HTTP POST to `/api/agents/{id}/trigger`
- **chain** — run after another agent completes
- **manual** — triggered via dashboard or `agent_trigger` API
