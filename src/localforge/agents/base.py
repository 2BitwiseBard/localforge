"""Base agent class for autonomous agents.

Trust levels control which MCP tools an agent can call:
  - monitor: read-only tools (health_check, search_index, file_qa, list_notes, list_indexes)
  - safe:    + index_directory, save_note, review_diff, local_chat, incremental_index, rag_query
  - full:    all tools (with approval gates for destructive actions)

Extended with:
  - Message bus integration for inter-agent communication
  - Sub-agent spawning via the supervisor
  - Task queue integration
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

import httpx

log = logging.getLogger("agent-base")


class TrustLevel(str, Enum):
    MONITOR = "monitor"
    SAFE = "safe"
    FULL = "full"


class TriggerType(str, Enum):
    CRON = "cron"
    FILE_WATCH = "file_watch"
    WEBHOOK = "webhook"
    CHAIN = "chain"
    MANUAL = "manual"


# Tool whitelists by trust level (cumulative)
TRUST_WHITELISTS: dict[TrustLevel, set[str]] = {
    TrustLevel.MONITOR: {
        "health_check",
        "check_model",
        "get_generation_params",
        "search_index",
        "semantic_search",
        "hybrid_search",
        "file_qa",
        "list_notes",
        "recall_note",
        "save_note",  # Low-risk write — needed for alerts and notifications
        "list_indexes",
        "list_sessions",
        "session_stats",
        "classify_task",
        "slot_info",
        "cache_stats",
        "compute_status",
        "compute_route",
    },
    TrustLevel.SAFE: {
        # Includes all MONITOR tools plus:
        "index_directory",
        "incremental_index",
        "ingest_document",
        "save_note",
        "delete_note",
        "review_diff",
        "diff_explain",
        "analyze_code",
        "batch_review",
        "local_chat",
        "multi_turn_chat",
        "rag_query",
        "diff_rag",
        "summarize_file",
        "explain_error",
        "knowledge_base",
        "doc_lookup",
        "embed_text",
        "rerank_chunks",
        "git_context",
        "web_search",
        "web_fetch",
        "deep_research",
        "kg_add",
        "kg_relate",
        "kg_query",
        "compute_status",
        "compute_route",
        "mesh_dispatch",
        "fs_read",
        "fs_list",
        "fs_glob",
        "fs_grep",
    },
    TrustLevel.FULL: set(),  # All tools allowed
}


def allowed_tools(trust: TrustLevel) -> set[str]:
    """Return the set of allowed tool names for a trust level."""
    if trust == TrustLevel.FULL:
        return set()  # Empty means all allowed
    tools = set()
    for level in TrustLevel:
        tools |= TRUST_WHITELISTS[level]
        if level == trust:
            break
    return tools


@dataclass
class AgentState:
    """Persistent state for an agent."""

    agent_id: str
    status: str = "idle"  # idle, running, stopped, error, paused
    last_run: float = 0
    run_count: int = 0
    last_error: str = ""
    last_duration: float = 0
    total_duration: float = 0
    logs: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    def log(self, msg: str):
        entry = f"[{time.strftime('%H:%M:%S')}] {msg}"
        self.logs.append(entry)
        if len(self.logs) > 200:
            self.logs = self.logs[-100:]
        log.info(f"[{self.agent_id}] {msg}")


class BaseAgent:
    """Base class for autonomous agents with messaging and sub-agent support."""

    name: str = "base"
    trust_level: TrustLevel = TrustLevel.MONITOR
    description: str = ""

    def __init__(self, agent_id: str, config: dict, gateway_url: str, api_key: str):
        self.agent_id = agent_id
        self.config = config
        self.gateway_url = gateway_url.rstrip("/")
        self.api_key = api_key
        self.state = AgentState(agent_id=agent_id)
        self._allowed = allowed_tools(self.trust_level)
        # Set by supervisor after creation
        self._bus = None  # MessageBus
        self._task_queue = None  # TaskQueue
        self._approval_queue = None  # ApprovalQueue
        self._supervisor = None  # AgentSupervisor (weak reference for spawning)
        self._children: list[str] = []  # child agent IDs

    async def call_tool(self, name: str, arguments: dict, timeout: float = 60) -> dict:
        """Call an MCP tool, gated by trust level and approval queue."""
        if self._allowed and name not in self._allowed:
            msg = f"Tool '{name}' not allowed at trust level '{self.trust_level.value}'"
            self.state.log(f"BLOCKED: {msg}")
            return {"error": msg}

        # Approval gate for destructive actions at FULL trust
        if self.trust_level == TrustLevel.FULL and self._approval_queue and self._approval_queue.needs_approval(name):
            self.state.log(f"Requesting approval for: {name}")
            req_id = self._approval_queue.request_approval(
                self.agent_id,
                name,
                arguments,
                reason=f"Agent {self.agent_id} wants to call {name}",
            )
            await self.notify(
                f"Approval needed: {name}",
                f"Agent {self.agent_id} wants to call {name}({arguments})",
                level="warning",
            )
            approved = await self._approval_queue.wait_for_approval(req_id, timeout=300)
            if not approved:
                msg = f"Action {name} was denied or timed out"
                self.state.log(f"DENIED: {msg}")
                return {"error": msg}
            self.state.log(f"APPROVED: {name}")

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.api_key}",
        }
        url = f"{self.gateway_url}/mcp/"

        async with httpx.AsyncClient(timeout=timeout) as client:
            # Initialize
            await client.post(
                url,
                json={
                    "jsonrpc": "2.0",
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": f"agent-{self.agent_id}", "version": "1.0"},
                    },
                    "id": 1,
                },
                headers=headers,
            )

            # Call tool
            resp = await client.post(
                url,
                json={
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                    "id": 2,
                },
                headers=headers,
            )

        # Parse SSE
        for line in resp.text.splitlines():
            if line.startswith("data: "):
                try:
                    import json

                    data = json.loads(line[6:])
                    if "result" in data:
                        return data["result"]
                    if "error" in data:
                        return data["error"]
                except Exception:
                    continue
        return {"error": f"Unparseable response: {resp.text[:200]}"}

    def extract_text(self, result: dict) -> str:
        """Extract text from MCP tool result."""
        if not isinstance(result, dict):
            return str(result) if result else ""
        # Check for error responses
        if "error" in result:
            err = result["error"]
            if isinstance(err, dict):
                return f"Error: {err.get('message', str(err))}"
            return f"Error: {err}"
        content = result.get("content", [])
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item["text"])
        return "\n".join(parts) if parts else ""

    # --- Messaging ---

    async def send_message(self, topic: str, payload: dict, recipients: Optional[list[str]] = None):
        """Send a message via the bus."""
        if not self._bus:
            self.state.log("Warning: no message bus configured")
            return
        from .message_bus import Message

        msg = Message(
            sender=self.agent_id,
            topic=topic,
            payload=payload,
            recipients=recipients or [],
        )
        await self._bus.publish(msg)

    async def receive_messages(self, timeout: float = 0) -> list:
        """Receive pending messages from the bus. Non-blocking if timeout=0."""
        if not self._bus:
            return []
        queue = await self._bus.subscribe(self.agent_id)
        messages = []
        while True:
            try:
                if timeout > 0 and not messages:
                    msg = await asyncio.wait_for(queue.get(), timeout=timeout)
                else:
                    msg = queue.get_nowait()
                messages.append(msg)
            except (asyncio.QueueEmpty, asyncio.TimeoutError):
                break
        return messages

    # --- Sub-agent spawning ---

    async def spawn_child(self, agent_type: str, config: dict, trust: Optional[TrustLevel] = None) -> Optional[str]:
        """Request the supervisor to spawn a child agent. Returns child agent_id."""
        if not self._supervisor:
            self.state.log("Warning: no supervisor reference for spawning")
            return None

        child_id = f"{self.agent_id}.child.{int(time.time())}"
        child_config = {
            "type": agent_type,
            "trust": (trust or self.trust_level).value,
            "config": config,
            "parent": self.agent_id,
            "ephemeral": True,  # Cleaned up when parent stops
        }

        success = await self._supervisor.spawn_agent(child_id, child_config)
        if success:
            self._children.append(child_id)
            self.state.log(f"Spawned child agent: {child_id} ({agent_type})")
            return child_id
        return None

    # --- Task queue ---

    def enqueue_task(self, payload: dict, queue: str = "default", priority: int = 5) -> Optional[str]:
        """Enqueue a task to the shared task queue."""
        if not self._task_queue:
            self.state.log("Warning: no task queue configured")
            return None
        return self._task_queue.enqueue(payload, queue=queue, priority=priority)

    def dequeue_task(self, queue: str = "default") -> Optional[dict]:
        """Dequeue a task from the shared task queue."""
        if not self._task_queue:
            return None
        return self._task_queue.dequeue(queue=queue, agent_id=self.agent_id)

    # --- Mesh dispatch ---

    async def call_mesh(self, task_type: str, payload: dict, target: str = "") -> dict:
        """Dispatch a task to a mesh worker.

        Args:
            task_type: chat, embeddings, tts, stt, classify, rerank
            payload: task-specific payload dict
            target: specific worker (hostname:port) or empty for auto-routing

        Returns:
            Result dict from the worker, or {"error": "..."} on failure.
        """
        return await self.call_tool(
            "mesh_dispatch",
            {
                "task_type": task_type,
                "payload": payload,
                "target": target,
            },
        )

    # --- Notifications ---

    async def notify(self, title: str, body: str, level: str = "info"):
        """Emit a notification via the message bus.

        Levels: info, warning, critical.
        The supervisor routes these to:
          1. SSE push to dashboard
          2. Persistent note (alerts-{date})
        """
        self.state.log(f"NOTIFY [{level}]: {title}")
        await self.send_message(
            "agent.notification",
            {
                "title": title,
                "body": body,
                "level": level,
                "agent_id": self.agent_id,
                "agent_type": self.name,
                "timestamp": time.time(),
            },
        )
        # Also save as a note for persistence
        date_str = time.strftime("%Y-%m-%d")
        try:
            await self.call_tool(
                "save_note",
                {
                    "topic": f"alerts-{date_str}",
                    "content": f"[{level.upper()}] {title}\n{body}\n— {self.agent_id} at {time.strftime('%H:%M:%S')}",
                },
            )
        except (KeyError, OSError, httpx.HTTPError) as exc:
            self.state.log(f"Note save failed (non-fatal): {exc}")

    # --- Lifecycle ---

    async def run(self):
        """Override in subclasses. Called on each scheduled execution."""
        raise NotImplementedError

    async def on_trigger(self, trigger_type: str, payload: dict | None = None):
        """Called when the agent is triggered by an event. Default: run()."""
        self.state.log(f"Triggered via {trigger_type}")
        await self.run()

    async def execute(self):
        """Wrapper around run() with state tracking and error handling."""
        self.state.status = "running"
        self.state.last_run = time.time()
        self.state.run_count += 1
        start = time.time()
        try:
            await self.run()
            self.state.status = "idle"
        except Exception as e:
            self.state.status = "error"
            self.state.last_error = str(e)
            self.state.log(f"ERROR: {e}")
            log.exception(f"Agent {self.agent_id} failed")
        finally:
            duration = time.time() - start
            self.state.last_duration = duration
            self.state.total_duration += duration

    def metrics(self) -> dict:
        """Return agent performance metrics."""
        avg_duration = self.state.total_duration / self.state.run_count if self.state.run_count else 0
        return {
            "agent_id": self.agent_id,
            "type": self.name,
            "trust": self.trust_level.value,
            "status": self.state.status,
            "run_count": self.state.run_count,
            "last_run": self.state.last_run,
            "last_duration_s": round(self.state.last_duration, 2),
            "avg_duration_s": round(avg_duration, 2),
            "total_duration_s": round(self.state.total_duration, 2),
            "last_error": self.state.last_error,
            "children": self._children,
        }
