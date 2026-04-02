"""Agent supervisor: manages lifecycle, scheduling, and state persistence for agents.

Reads agent definitions from agents.yaml, spawns them on schedule (cron-like),
and exposes status via the MCP gateway. Supports triggers: cron, file_watch,
webhook, chain, and manual.

Extended with:
  - MessageBus for inter-agent communication
  - TaskQueue for persistent task management
  - Worker pool for processing queued tasks
  - Dynamic agent creation and pause/resume
  - Agent metrics and health tracking
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import yaml

from .base import BaseAgent, TrustLevel, TriggerType, allowed_tools
from .message_bus import MessageBus, Message
from .task_queue import TaskQueue

log = logging.getLogger("agent-supervisor")

AGENTS_CONFIG = Path(__file__).parent.parent / "agents.yaml"
STATE_DIR = Path(__file__).parent.parent / "agent_state"

# Registry of agent classes
_agent_classes: dict[str, type] = {}


def register_agent(cls):
    """Decorator to register an agent class."""
    _agent_classes[cls.name] = cls
    return cls


class AgentSupervisor:
    def __init__(self, gateway_url: str, api_key: str):
        self.gateway_url = gateway_url
        self.api_key = api_key
        self._agents: dict[str, BaseAgent] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._configs: dict[str, dict] = {}
        self._running = False
        self._observer = None  # watchdog Observer
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._paused: set[str] = set()

        # Shared infrastructure
        self.bus = MessageBus()
        self.task_queue = TaskQueue()

        # Worker pool
        self._worker_count = 2
        self._worker_tasks: list[asyncio.Task] = []

    def load_config(self) -> dict:
        if AGENTS_CONFIG.exists():
            with open(AGENTS_CONFIG) as f:
                return yaml.safe_load(f) or {}
        return {}

    async def start(self):
        """Load config and start all enabled agents."""
        self._running = True
        self._loop = asyncio.get_running_loop()
        STATE_DIR.mkdir(exist_ok=True)

        config = self.load_config()
        agents_cfg = config.get("agents", {})

        # Subscribe supervisor to the bus for spawn requests and notifications
        await self.bus.subscribe("__supervisor__")
        self.bus.on_topic("agent.spawn_request", self._handle_spawn_request)
        self.bus.on_topic("agent.notification", self._handle_notification)

        # Notification callbacks (set by gateway for SSE push)
        self._notification_callbacks: list = []

        for agent_id, acfg in agents_cfg.items():
            self._configs[agent_id] = acfg
            if acfg.get("enabled", True) is False:
                continue
            await self.spawn_agent(agent_id, acfg)

        # Start file watchers for agents with file_watch triggers
        self._setup_file_watchers(agents_cfg)

        # Start worker pool for task queue processing
        for i in range(self._worker_count):
            task = asyncio.create_task(self._worker_loop(f"worker-{i}"))
            self._worker_tasks.append(task)

        # Start bus listener
        asyncio.create_task(self._bus_listener())

        log.info(f"Supervisor started with {len(self._agents)} agents, "
                 f"{self._worker_count} workers")

    async def stop(self):
        """Stop all agents, workers, and save state."""
        self._running = False

        # Stop file watcher
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None

        # Stop workers
        for task in self._worker_tasks:
            task.cancel()
        self._worker_tasks.clear()

        for agent_id in list(self._tasks.keys()):
            await self.stop_agent(agent_id)

        self.task_queue.close()
        log.info("Supervisor stopped")

    async def spawn_agent(self, agent_id: str, config: dict) -> bool:
        """Spawn a single agent from its config."""
        agent_type = config.get("type", agent_id)
        cls = _agent_classes.get(agent_type)
        if cls is None:
            log.error(f"Unknown agent type: {agent_type}")
            return False

        trust = TrustLevel(config.get("trust", "monitor"))
        agent = cls(
            agent_id=agent_id,
            config=config.get("config", {}),
            gateway_url=self.gateway_url,
            api_key=self.api_key,
        )
        agent.trust_level = trust
        agent._allowed = allowed_tools(trust)

        # Inject shared infrastructure
        agent._bus = self.bus
        agent._task_queue = self.task_queue
        agent._supervisor = self

        # Subscribe agent to bus
        await self.bus.subscribe(agent_id)

        self._agents[agent_id] = agent
        self._configs.setdefault(agent_id, config)

        # Load persisted state
        state_file = STATE_DIR / f"{agent_id}.json"
        if state_file.exists():
            try:
                saved = json.loads(state_file.read_text())
                agent.state.data = saved.get("data", {})
                agent.state.run_count = saved.get("run_count", 0)
                agent.state.total_duration = saved.get("total_duration", 0)
            except Exception:
                pass

        # Schedule
        schedule = config.get("schedule", "")
        if schedule:
            self._tasks[agent_id] = asyncio.create_task(
                self._schedule_loop(agent, schedule)
            )
            agent.state.log(f"Started with schedule: {schedule}")
        elif not config.get("ephemeral"):
            # One-shot for non-ephemeral agents
            self._tasks[agent_id] = asyncio.create_task(self._run_once(agent))

        return True

    async def stop_agent(self, agent_id: str):
        """Stop a running agent and save its state."""
        task = self._tasks.pop(agent_id, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        agent = self._agents.pop(agent_id, None)
        if agent:
            # Stop child agents
            for child_id in agent._children:
                await self.stop_agent(child_id)

            self._save_state(agent)
            agent.state.status = "stopped"
            agent.state.log("Stopped")

            # Unsubscribe from bus
            await self.bus.unsubscribe(agent_id)

        self._paused.discard(agent_id)

    def pause_agent(self, agent_id: str) -> bool:
        """Pause an agent's scheduling loop (stays loaded)."""
        if agent_id in self._agents:
            self._paused.add(agent_id)
            self._agents[agent_id].state.status = "paused"
            self._agents[agent_id].state.log("Paused")
            return True
        return False

    def resume_agent(self, agent_id: str) -> bool:
        """Resume a paused agent."""
        if agent_id in self._paused:
            self._paused.discard(agent_id)
            agent = self._agents.get(agent_id)
            if agent:
                agent.state.status = "idle"
                agent.state.log("Resumed")
            return True
        return False

    async def create_agent(self, agent_id: str, agent_type: str,
                           trust: str = "monitor", schedule: str = "",
                           config: dict = None, persist: bool = True) -> bool:
        """Create a new agent at runtime, optionally persisting to agents.yaml."""
        if agent_id in self._agents:
            return False

        acfg = {
            "type": agent_type,
            "trust": trust,
            "schedule": schedule,
            "enabled": True,
            "config": config or {},
        }

        if persist:
            # Write to agents.yaml
            full_config = self.load_config()
            full_config.setdefault("agents", {})[agent_id] = acfg
            with open(AGENTS_CONFIG, "w") as f:
                yaml.dump(full_config, f, default_flow_style=False)

        return await self.spawn_agent(agent_id, acfg)

    def _save_state(self, agent: BaseAgent):
        state_file = STATE_DIR / f"{agent.agent_id}.json"
        state_file.write_text(json.dumps({
            "data": agent.state.data,
            "run_count": agent.state.run_count,
            "last_run": agent.state.last_run,
            "logs": agent.state.logs[-100:],
            "status": agent.state.status,
            "last_error": agent.state.last_error,
            "last_duration": agent.state.last_duration,
            "total_duration": agent.state.total_duration,
        }))

    async def _run_once(self, agent: BaseAgent):
        await agent.execute()
        self._save_state(agent)

    async def _wait_for_gateway(self, max_wait: int = 60):
        """Wait for the MCP gateway to be reachable before running agents."""
        import httpx
        for i in range(max_wait):
            try:
                async with httpx.AsyncClient(timeout=3) as client:
                    resp = await client.get(f"{self.gateway_url.rstrip('/')}/health")
                    if resp.status_code == 200:
                        if i > 0:
                            log.info(f"Gateway ready after {i}s")
                        return True
            except Exception:
                pass
            await asyncio.sleep(1)
        log.warning(f"Gateway not reachable after {max_wait}s, proceeding anyway")
        return False

    async def _schedule_loop(self, agent: BaseAgent, schedule: str):
        """Simple cron-like schedule loop with initial delay for daily schedules."""
        interval = self._parse_schedule(schedule)
        initial_delay = self._initial_delay(schedule)
        # Wait for gateway on first run
        await self._wait_for_gateway()
        # For daily schedules, wait until the target time before first run
        if initial_delay > 0:
            agent.state.log(f"Waiting {initial_delay // 60:.0f}m until target time")
            await asyncio.sleep(initial_delay)
        while self._running:
            if agent.agent_id not in self._paused:
                await agent.execute()
                self._save_state(agent)
            await asyncio.sleep(interval)

    @staticmethod
    def _parse_schedule(schedule: str) -> int:
        """Parse a simple cron-like schedule to an interval in seconds.

        Supports:
          '*/N * * * *' → every N minutes
          '0 H * * *'   → daily (86400s, initial delay computed to target hour)
          '0 */H * * *' → every H hours
        Falls back to 3600s (1 hour) for unrecognized patterns.
        """
        parts = schedule.strip().split()
        if not parts:
            return 3600

        minute_part = parts[0]

        # Every N minutes: */N * * * *
        if minute_part.startswith("*/"):
            try:
                return int(minute_part[2:]) * 60
            except ValueError:
                return 3600

        # Daily at specific hour: 0 H * * *  or  M H * * *
        if len(parts) >= 2:
            hour_part = parts[1]
            # Every H hours: 0 */H * * *
            if hour_part.startswith("*/"):
                try:
                    return int(hour_part[2:]) * 3600
                except ValueError:
                    return 3600
            # Fixed hour → daily interval
            try:
                int(hour_part)  # Validate it's a number
                return 86400  # 24 hours
            except ValueError:
                pass

        return 3600

    @staticmethod
    def _initial_delay(schedule: str) -> int:
        """Compute seconds to wait before first run for daily schedules.

        For '0 20 * * *' at 15:00, returns 5 hours (18000s).
        For interval-based schedules (*/N), returns 0 (run immediately).
        """
        parts = schedule.strip().split()
        if len(parts) < 2:
            return 0
        minute_part, hour_part = parts[0], parts[1]
        # Only compute delay for fixed-hour schedules
        if minute_part.startswith("*/") or hour_part.startswith("*/"):
            return 0
        try:
            target_minute = int(minute_part)
            target_hour = int(hour_part)
        except ValueError:
            return 0
        import datetime
        now = datetime.datetime.now()
        target = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
        if target <= now:
            target += datetime.timedelta(days=1)
        return int((target - now).total_seconds())

    # -----------------------------------------------------------------------
    # Worker pool (processes tasks from the TaskQueue)
    # -----------------------------------------------------------------------

    async def _worker_loop(self, worker_id: str):
        """Worker that picks up tasks from the queue and executes them."""
        log.info(f"Worker {worker_id} started")
        while self._running:
            try:
                task = self.task_queue.dequeue(agent_id=worker_id)
                if task is None:
                    await asyncio.sleep(5)  # Poll interval
                    continue

                task_id = task["id"]
                payload = task["payload"]
                log.info(f"Worker {worker_id} processing task {task_id}")

                try:
                    # Execute task based on payload type
                    result = await self._execute_task(payload)
                    self.task_queue.complete(task_id, result)
                    log.info(f"Worker {worker_id} completed task {task_id}")
                except Exception as e:
                    self.task_queue.fail(task_id, str(e))
                    log.error(f"Worker {worker_id} failed task {task_id}: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Worker {worker_id} error: {e}")
                await asyncio.sleep(5)

    async def _execute_task(self, payload: dict) -> dict:
        """Execute a task payload. Supports different task types."""
        task_type = payload.get("type", "chat")

        if task_type == "chat":
            # Simple chat completion
            import httpx
            prompt = payload.get("prompt", "")
            backend_url = payload.get("backend_url", "http://localhost:5000/v1")
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(f"{backend_url}/chat/completions", json={
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": payload.get("max_tokens", 2048),
                })
                data = resp.json()
                return {"response": data["choices"][0]["message"]["content"]}

        elif task_type == "tool_call":
            # Call an MCP tool
            tool_name = payload.get("tool_name", "")
            arguments = payload.get("arguments", {})
            # Create a temporary agent for the call
            tmp = BaseAgent("task-worker", {}, self.gateway_url, self.api_key)
            result = await tmp.call_tool(tool_name, arguments,
                                         timeout=payload.get("timeout", 60))
            return {"result": tmp.extract_text(result)}

        else:
            return {"error": f"Unknown task type: {task_type}"}

    # -----------------------------------------------------------------------
    # Message bus listener
    # -----------------------------------------------------------------------

    async def _bus_listener(self):
        """Listen for supervisor-targeted messages on the bus."""
        queue = await self.bus.subscribe("__supervisor__")
        while self._running:
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=5)
                if isinstance(msg, Message):
                    await self._handle_bus_message(msg)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Bus listener error: {e}")

    async def _handle_bus_message(self, msg: Message):
        """Handle a message directed at the supervisor."""
        if msg.topic == "agent.spawn_request":
            payload = msg.payload
            child_id = payload.get("child_id", "")
            if child_id:
                await self.spawn_agent(child_id, payload)
                # Reply to sender
                reply = Message(
                    sender="__supervisor__",
                    topic="agent.spawn_reply",
                    payload={"child_id": child_id, "status": "spawned"},
                    recipients=[msg.sender],
                    reply_to=msg.id,
                )
                await self.bus.publish(reply)

    def _handle_spawn_request(self, msg: Message):
        """Sync handler for spawn requests (called by bus topic handler)."""
        # The async handling is done in _handle_bus_message via the listener
        pass

    def _handle_notification(self, msg: Message):
        """Handle agent.notification messages — push to SSE callbacks."""
        payload = msg.payload
        log.info(f"Notification from {payload.get('agent_id', '?')}: "
                 f"[{payload.get('level', 'info')}] {payload.get('title', '')}")
        # Fire registered callbacks (e.g., gateway SSE push)
        for callback in getattr(self, "_notification_callbacks", []):
            try:
                result = callback(payload)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception as e:
                log.error(f"Notification callback error: {e}")

    def on_notification(self, callback):
        """Register a callback for agent notifications.

        Callback receives: {"title": str, "body": str, "level": str,
                            "agent_id": str, "agent_type": str, "timestamp": float}
        """
        if not hasattr(self, "_notification_callbacks"):
            self._notification_callbacks = []
        self._notification_callbacks.append(callback)

    # -----------------------------------------------------------------------
    # Trigger system
    # -----------------------------------------------------------------------

    async def trigger_agent(self, agent_id: str, trigger_type: str = "manual",
                            payload: dict | None = None) -> str:
        """Trigger an agent to run immediately, regardless of schedule."""
        agent = self._agents.get(agent_id)
        if not agent:
            # Try to spawn it if it exists in config but isn't running
            acfg = self._configs.get(agent_id)
            if acfg:
                await self.spawn_agent(agent_id, acfg)
                agent = self._agents.get(agent_id)
            if not agent:
                return f"Agent '{agent_id}' not found"

        if agent.state.status == "running":
            return f"Agent '{agent_id}' is already running"

        async def _run():
            try:
                await agent.on_trigger(trigger_type, payload)
                self._save_state(agent)
                # Check for chain triggers
                await self._process_chains(agent_id)
                # Notify via bus
                await self.bus.publish(Message(
                    sender="__supervisor__",
                    topic="agent.completed",
                    payload={"agent_id": agent_id, "trigger_type": trigger_type},
                ))
            except Exception as e:
                agent.state.log(f"Trigger error: {e}")
                log.exception(f"Trigger error for {agent_id}")

        asyncio.create_task(_run())
        return f"Agent '{agent_id}' triggered via {trigger_type}"

    async def _process_chains(self, completed_agent_id: str):
        """After an agent completes, check if it chains to another agent."""
        acfg = self._configs.get(completed_agent_id, {})
        triggers = acfg.get("triggers", [])
        for trigger in triggers:
            if trigger.get("type") == "chain":
                target = trigger.get("target")
                if target and target in self._configs:
                    log.info(f"Chain trigger: {completed_agent_id} → {target}")
                    await self.trigger_agent(target, "chain", {"source": completed_agent_id})

    # -----------------------------------------------------------------------
    # File watch triggers (watchdog)
    # -----------------------------------------------------------------------

    def _setup_file_watchers(self, agents_cfg: dict):
        """Set up filesystem watchers for agents with file_watch triggers."""
        watch_configs = []
        for agent_id, acfg in agents_cfg.items():
            if not acfg.get("enabled", True):
                continue
            for trigger in acfg.get("triggers", []):
                if trigger.get("type") == "file_watch":
                    paths = trigger.get("paths", [])
                    patterns = trigger.get("patterns", ["*"])
                    debounce = trigger.get("debounce", 10)
                    for p in paths:
                        expanded = os.path.expanduser(p)
                        if os.path.isdir(expanded):
                            watch_configs.append({
                                "agent_id": agent_id,
                                "path": expanded,
                                "patterns": patterns,
                                "debounce": debounce,
                            })

        if not watch_configs:
            return

        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler
        except ImportError:
            log.warning("watchdog not installed, file_watch triggers disabled")
            return

        class _DebouncedHandler(FileSystemEventHandler):
            def __init__(self, agent_id: str, patterns: list[str], debounce: int,
                         supervisor: "AgentSupervisor"):
                super().__init__()
                self.agent_id = agent_id
                self.patterns = patterns
                self.debounce = debounce
                self.supervisor = supervisor
                self._last_trigger = 0

            def on_any_event(self, event):
                if event.is_directory:
                    return
                # Check glob patterns
                path = event.src_path
                import fnmatch
                if not any(fnmatch.fnmatch(os.path.basename(path), p) for p in self.patterns):
                    return
                # Debounce
                now = time.time()
                if now - self._last_trigger < self.debounce:
                    return
                self._last_trigger = now
                # Trigger agent from watchdog thread → asyncio loop
                if self.supervisor._loop and self.supervisor._running:
                    asyncio.run_coroutine_threadsafe(
                        self.supervisor.trigger_agent(
                            self.agent_id, "file_watch",
                            {"path": path, "event": event.event_type}
                        ),
                        self.supervisor._loop,
                    )

        self._observer = Observer()
        for wc in watch_configs:
            handler = _DebouncedHandler(
                wc["agent_id"], wc["patterns"], wc["debounce"], self
            )
            self._observer.schedule(handler, wc["path"], recursive=True)
            log.info(f"File watcher: {wc['path']} → {wc['agent_id']} "
                     f"(patterns={wc['patterns']}, debounce={wc['debounce']}s)")

        self._observer.daemon = True
        self._observer.start()

    # -----------------------------------------------------------------------
    # Status
    # -----------------------------------------------------------------------

    def list_agents(self) -> list[dict]:
        result = []
        for agent_id, agent in self._agents.items():
            acfg = self._configs.get(agent_id, {})
            triggers = [t.get("type", "unknown") for t in acfg.get("triggers", [])]
            result.append({
                "id": agent_id,
                "type": agent.name,
                "trust": agent.trust_level.value,
                "status": agent.state.status,
                "run_count": agent.state.run_count,
                "last_run": agent.state.last_run,
                "last_error": agent.state.last_error,
                "last_duration": agent.state.last_duration,
                "avg_duration": (agent.state.total_duration / agent.state.run_count
                                 if agent.state.run_count else 0),
                "triggers": triggers,
                "children": agent._children,
                "paused": agent_id in self._paused,
            })
        # Also include disabled agents from config
        for agent_id, acfg in self._configs.items():
            if agent_id not in self._agents:
                result.append({
                    "id": agent_id,
                    "type": acfg.get("type", agent_id),
                    "trust": acfg.get("trust", "monitor"),
                    "status": "disabled",
                    "run_count": 0,
                    "last_run": 0,
                    "last_error": "",
                    "last_duration": 0,
                    "avg_duration": 0,
                    "triggers": [t.get("type", "unknown") for t in acfg.get("triggers", [])],
                    "children": [],
                    "paused": False,
                })
        return result

    def get_agent_logs(self, agent_id: str, lines: int = 50) -> list[str]:
        agent = self._agents.get(agent_id)
        if not agent:
            return [f"Agent '{agent_id}' not found"]
        return agent.state.logs[-lines:]

    def get_metrics(self) -> dict:
        """Return supervisor-level metrics."""
        return {
            "total_agents": len(self._agents),
            "running": sum(1 for a in self._agents.values() if a.state.status == "running"),
            "paused": len(self._paused),
            "task_queue_depth": self.task_queue.queue_depth(),
            "bus_subscribers": self.bus.subscriber_count,
            "workers": self._worker_count,
            "agents": [a.metrics() for a in self._agents.values()],
        }
