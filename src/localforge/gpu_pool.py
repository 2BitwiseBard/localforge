"""GPU pool manager and compute mesh: auto-discover, health-check, and route to
multiple backends on the Tailscale mesh.

Supports two types of backends:
  - text-generation-webui instances on :5000 (legacy GPU backends)
  - Device worker agents on :8200 (heterogeneous compute nodes)

The pool:
  - Discovers peers via tailscale status --json (probes :5000 and :8200)
  - Health-checks all backends on a configurable interval
  - Routes requests by model type or task capability
  - Fails over to the next healthy backend on error
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import httpx

log = logging.getLogger("gpu-pool")


# ---------------------------------------------------------------------------
# Circuit breaker states
# ---------------------------------------------------------------------------

class CircuitState(Enum):
    CLOSED = "closed"       # Normal operation, requests flow through
    OPEN = "open"           # Failures exceeded threshold, skip until cooldown
    HALF_OPEN = "half_open"  # Cooldown expired, try one probe


@dataclass
class CircuitBreaker:
    """Per-backend circuit breaker to avoid hammering failing backends."""
    failure_threshold: int = 5
    cooldown_s: float = 60.0
    state: CircuitState = CircuitState.CLOSED
    failures: int = 0
    last_failure: float = 0.0
    last_success: float = 0.0

    def record_success(self):
        self.failures = 0
        self.state = CircuitState.CLOSED
        self.last_success = time.time()

    def record_failure(self):
        self.failures += 1
        self.last_failure = time.time()
        if self.failures >= self.failure_threshold:
            self.state = CircuitState.OPEN
            log.warning("Circuit breaker OPEN after %d consecutive failures", self.failures)

    def should_attempt(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            if time.time() - self.last_failure >= self.cooldown_s:
                self.state = CircuitState.HALF_OPEN
                return True
            return False
        # HALF_OPEN: allow one attempt
        return True


# ---------------------------------------------------------------------------
# Device capabilities for the compute mesh
# ---------------------------------------------------------------------------

@dataclass
class DeviceCapabilities:
    """What a device on the mesh can do."""
    inference: bool = False
    max_model_params: int = 0      # Largest model (in B params)
    vram_mb: int = 0
    ram_mb: int = 0
    embeddings: bool = False
    reranking: bool = False
    tts: bool = False
    stt: bool = False
    vision: bool = False
    classification: bool = False
    platform: str = ""             # linux, darwin, android
    gpu_type: str = ""             # nvidia, apple_silicon, adreno, amd, none
    cpu_cores: int = 0
    battery_pct: int = -1
    battery_charging: bool = False
    thermal_throttled: bool = False

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}

    @classmethod
    def from_dict(cls, data: dict) -> "DeviceCapabilities":
        valid = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in valid})


@dataclass
class ComputeNode:
    """A device on the compute mesh with capability-based routing."""
    name: str
    url: str
    capabilities: DeviceCapabilities = field(default_factory=DeviceCapabilities)
    tier: str = "lightweight"      # gpu-primary, gpu-secondary, cpu-capable, lightweight
    healthy: bool = False
    last_check: float = 0
    missed_pings: int = 0
    active_tasks: int = 0
    model_name: str = ""

    @property
    def load(self) -> int:
        return self.active_tasks

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "url": self.url,
            "tier": self.tier,
            "healthy": self.healthy,
            "model_name": self.model_name,
            "active_tasks": self.active_tasks,
            "missed_pings": self.missed_pings,
            "capabilities": self.capabilities.to_dict(),
        }


# ---------------------------------------------------------------------------
# Legacy Backend (text-gen-webui on :5000)
# ---------------------------------------------------------------------------

@dataclass
class Backend:
    name: str
    url: str
    model_name: str = ""
    model_type: str = "default"  # code, vision, reasoning, fast, default
    healthy: bool = False
    last_check: float = 0
    missed_pings: int = 0
    active_requests: int = 0
    active_slots: int = 0       # from /slots endpoint
    total_slots: int = 1
    circuit: CircuitBreaker = field(default_factory=CircuitBreaker)

    @property
    def load(self) -> float:
        """Load score: lower is better. Considers active requests and slot utilization."""
        if self.total_slots > 0:
            return self.active_slots / self.total_slots
        return self.active_requests


class GPUPool:
    def __init__(self, config: dict):
        self._config = config.get("gpu_pool", {})
        self._backends: dict[str, Backend] = {}
        self._routing_rules: dict[str, list[str]] = self._config.get("model_routing", {})
        self._health_interval = self._config.get("health_check_interval", 30)
        self._discovery_interval = self._config.get("discovery_interval", 60)
        self._auto_discover = self._config.get("auto_discover", True)
        self._health_task: Optional[asyncio.Task] = None
        self._discovery_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        # Multi-node mesh config
        self._backend_probe_ports: list[int] = self._config.get("backend_probe_ports", [5000])
        self._worker_probe_ports: list[int] = self._config.get("worker_probe_ports", [8200])
        self._max_discovery_failures = self._config.get("max_discovery_failures", 5)
        self._discovery_failures: dict[str, int] = {}  # peer_name -> consecutive failure count
        # Unified heartbeat registry (moved from routes.py)
        self._heartbeat_nodes: dict[str, dict] = {}  # key = "hostname:port" -> raw heartbeat data
        self._max_heartbeat_nodes = 100  # Prevent unbounded growth

    # --- Lifecycle ---

    async def start(self):
        """Start background health and discovery loops."""
        self._health_task = asyncio.create_task(self._health_loop())
        if self._auto_discover:
            self._discovery_task = asyncio.create_task(self._discovery_loop())
        log.info("GPU pool started")

    async def stop(self):
        for task in [self._health_task, self._discovery_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        log.info("GPU pool stopped")

    # --- Registration ---

    def register_backend(self, name: str, url: str, model_type: str = "default"):
        url = url.rstrip("/")
        self._backends[name] = Backend(name=name, url=url, model_type=model_type)
        log.info(f"Registered backend: {name} at {url}")

    def register_from_config(self, backends_config: dict):
        """Register backends from the config.yaml 'backends' section."""
        for name, cfg in backends_config.items():
            if cfg.get("optional") and not cfg.get("url"):
                continue
            self.register_backend(name, cfg["url"])

    def remove_backend(self, name: str):
        self._backends.pop(name, None)
        log.info(f"Removed backend: {name}")

    # --- Health ---

    async def check_health(self, backend: Backend) -> bool:
        """Probe a single backend's health, respecting circuit breaker."""
        if not backend.circuit.should_attempt():
            return False

        try:
            async with httpx.AsyncClient(timeout=5) as client:
                # Check model info
                resp = await client.get(f"{backend.url}/internal/model/info")
                if resp.status_code == 200:
                    data = resp.json()
                    model_name = data.get("model_name", "")
                    if not model_name or model_name == "None":
                        # Backend is up but no model loaded
                        backend.model_name = ""
                        backend.healthy = False
                        backend.circuit.record_failure()
                        backend.last_check = time.time()
                        return False

                    backend.model_name = model_name
                    backend.model_type = self._classify_model(model_name)

                    # Also check slot utilization if available
                    try:
                        slots_resp = await client.get(
                            f"{backend.url.rstrip('/v1')}/slots",
                            timeout=3,
                        )
                        if slots_resp.status_code == 200:
                            slots = slots_resp.json()
                            if isinstance(slots, list):
                                backend.total_slots = len(slots)
                                backend.active_slots = sum(
                                    1 for s in slots
                                    if s.get("state", 0) != 0
                                )
                    except httpx.HTTPError:
                        pass  # /slots is optional (llama.cpp only)

                    backend.healthy = True
                    backend.missed_pings = 0
                    backend.circuit.record_success()
                    backend.last_check = time.time()
                    return True
        except (httpx.HTTPError, OSError) as e:
            log.debug("Health check failed for %s: %s", backend.url, e)

        backend.missed_pings += 1
        backend.circuit.record_failure()
        if backend.missed_pings >= 3:
            if backend.healthy:
                log.warning("Backend %s marked unhealthy after %d missed pings",
                            backend.name, backend.missed_pings)
            backend.healthy = False
        backend.last_check = time.time()
        return False

    def _classify_model(self, model_name: str) -> str:
        """Classify a model name into a routing type based on config rules."""
        lower = model_name.lower()
        for mtype, patterns in self._routing_rules.items():
            for pattern in patterns:
                if pattern.lower() in lower:
                    return mtype
        return "default"

    async def _health_loop(self):
        while True:
            for backend in list(self._backends.values()):
                await self.check_health(backend)
            # Also health-check compute mesh workers
            if hasattr(self, "_compute_nodes"):
                for node in list(self._compute_nodes.values()):
                    await self.check_worker_health(node)
            await asyncio.sleep(self._health_interval)

    # --- Discovery ---

    async def discover_peers(self):
        """Discover text-gen-webui instances on the Tailscale mesh."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "tailscale", "status", "--json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                return

            data = json.loads(stdout)
            peers = data.get("Peer", {})

            for peer_id, peer in peers.items():
                if not peer.get("Online"):
                    continue
                ips = peer.get("TailscaleIPs", [])
                if not ips:
                    continue
                ip = ips[0]
                hostname = peer.get("HostName", ip)
                name = f"tailscale-{hostname}"

                # Skip peers that have failed too many times (backoff)
                if self._discovery_failures.get(name, 0) >= self._max_discovery_failures:
                    continue

                async with self._lock:
                    if name in self._backends:
                        continue

                # Probe for text-gen-webui on configured ports
                found = False
                for port in self._backend_probe_ports:
                    url = f"http://{ip}:{port}/v1"
                    test_backend = Backend(name=name, url=url)
                    if await self.check_health(test_backend):
                        async with self._lock:
                            self._backends[name] = test_backend
                        self._discovery_failures.pop(name, None)
                        log.info(f"Auto-discovered backend: {name} ({test_backend.model_name})")
                        found = True
                        break
                if not found:
                    self._discovery_failures[name] = self._discovery_failures.get(name, 0) + 1

        except FileNotFoundError:
            log.debug("tailscale not installed, skipping peer discovery")
        except Exception as e:
            log.debug(f"Peer discovery failed: {e}")

    async def _discovery_loop(self):
        while True:
            await self.discover_peers()
            await self.discover_workers()
            await asyncio.sleep(self._discovery_interval)

    # --- Routing ---

    def route_request(self, task_type: str = "default") -> Optional[str]:
        """Pick the best backend URL for a given task type.

        Returns the backend URL or None if no healthy backend is available.
        Considers both legacy backends (text-gen-webui on :5000) AND heartbeat-
        registered mesh workers (device_worker on :8200).

        Prefers backends/workers whose loaded model matches the task type,
        then falls back to any healthy one. Among matches, picks least-loaded.
        """
        candidates = []
        fallbacks = []

        # Legacy backends (text-gen-webui)
        for b in self._backends.values():
            if not b.healthy:
                continue
            if b.model_type == task_type:
                candidates.append((b.url, b.load, b.model_name))
            else:
                fallbacks.append((b.url, b.load, b.model_name))

        # Heartbeat-registered mesh workers with inference capability
        self.__init_compute()
        for node in self._get_heartbeat_workers():
            if not node.healthy:
                continue
            if not getattr(node.capabilities, "inference", False):
                continue
            # Classify the worker's model to see if it matches the task type
            worker_model_type = self._classify_model(node.model_name) if node.model_name else "default"
            if worker_model_type == task_type:
                candidates.append((node.url, node.load, node.model_name))
            else:
                fallbacks.append((node.url, node.load, node.model_name))

        # Also include Tailscale-discovered compute nodes with inference
        for node in self._compute_nodes.values() if hasattr(self, "_compute_nodes") else []:
            if not node.healthy:
                continue
            if not getattr(node.capabilities, "inference", False):
                continue
            # Avoid duplicates (same URL already in candidates/fallbacks)
            existing_urls = {u for u, _, _ in candidates + fallbacks}
            if node.url in existing_urls:
                continue
            worker_model_type = self._classify_model(node.model_name) if node.model_name else "default"
            if worker_model_type == task_type:
                candidates.append((node.url, node.load, node.model_name))
            else:
                fallbacks.append((node.url, node.load, node.model_name))

        pool = candidates or fallbacks
        if not pool:
            return None

        # Least-loaded
        best = min(pool, key=lambda x: x[1])
        source = "model-match" if candidates else "fallback"
        log.debug(
            "route_request(%s): chose %s (model=%s, load=%.1f, source=%s, "
            "candidates=%d, fallbacks=%d)",
            task_type, best[0], best[2] or "?", best[1], source,
            len(candidates), len(fallbacks),
        )
        return best[0]

    def get_backend_by_url(self, url: str) -> Optional[Backend]:
        url = url.rstrip("/")
        for b in self._backends.values():
            if b.url == url:
                return b
        return None

    # --- Status ---

    def status(self) -> list[dict]:
        return [
            {
                "name": b.name,
                "url": b.url,
                "healthy": b.healthy,
                "model_name": b.model_name,
                "model_type": b.model_type,
                "active_requests": b.active_requests,
                "active_slots": b.active_slots,
                "total_slots": b.total_slots,
                "missed_pings": b.missed_pings,
                "circuit": b.circuit.state.value,
            }
            for b in self._backends.values()
        ]

    def record_failure(self, url: str) -> None:
        """Record a failure for a backend/worker by URL (for external callers)."""
        for b in self._backends.values():
            if b.url == url:
                b.circuit.record_failure()
                return

    def record_success(self, url: str) -> None:
        """Record a success for a backend/worker by URL (for external callers)."""
        for b in self._backends.values():
            if b.url == url:
                b.circuit.record_success()
                return

    # ===================================================================
    # Compute Mesh — heterogeneous device pool
    # ===================================================================

    def __init_compute(self):
        """Lazy-init compute mesh state (called from __init__ if compute_pool config exists)."""
        if not hasattr(self, "_compute_nodes"):
            self._compute_nodes: dict[str, ComputeNode] = {}
            compute_cfg = self._config  # Reuse gpu_pool config section
            self._worker_port = compute_cfg.get("worker_port", 8200)
            self._task_routing = compute_cfg.get("task_routing", {})

    def register_compute_node(self, name: str, url: str,
                               capabilities: dict = None,
                               tier: str = "lightweight"):
        """Register a compute node on the mesh."""
        self.__init_compute()
        caps = DeviceCapabilities.from_dict(capabilities or {})
        node = ComputeNode(
            name=name, url=url.rstrip("/"),
            capabilities=caps, tier=tier,
        )
        self._compute_nodes[name] = node
        log.info(f"Registered compute node: {name} ({tier}) at {url}")

    async def check_worker_health(self, node: ComputeNode) -> bool:
        """Probe a worker agent's /health endpoint."""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{node.url}/health")
                if resp.status_code == 200:
                    data = resp.json()
                    node.capabilities = DeviceCapabilities.from_dict(
                        data.get("capabilities", {}))
                    node.tier = data.get("tier", node.tier)
                    node.model_name = data.get("model_name", "")
                    node.healthy = True
                    node.missed_pings = 0
                    node.last_check = time.time()
                    return True
        except (httpx.HTTPError, OSError) as e:
            log.debug("Compute node %s health check failed: %s", node.name, e)

        node.missed_pings += 1
        if node.missed_pings >= 3:
            if node.healthy:
                log.warning(f"Compute node {node.name} marked unhealthy")
            node.healthy = False
        node.last_check = time.time()
        return False

    def route_task(self, task_type: str, requirements: dict = None) -> Optional[str]:
        """Route to the best device for a task type.

        task_type: inference, embeddings, tts, stt, reranking, classification
        requirements: {"min_vram": 4000, "min_params": 7} etc.

        Considers both Tailscale-discovered compute nodes AND heartbeat-registered
        mesh workers.
        """
        self.__init_compute()
        requirements = requirements or {}

        candidates = []
        # Gather candidates from Tailscale-discovered nodes
        for node in self._compute_nodes.values():
            if not node.healthy:
                continue
            if hasattr(node.capabilities, task_type):
                if not getattr(node.capabilities, task_type, False):
                    continue
            if requirements.get("min_vram") and node.capabilities.vram_mb < requirements["min_vram"]:
                continue
            if requirements.get("min_params") and node.capabilities.max_model_params < requirements["min_params"]:
                continue
            candidates.append(node)

        # Also gather candidates from heartbeat-registered workers
        for node in self._get_heartbeat_workers():
            if not node.healthy:
                continue
            if hasattr(node.capabilities, task_type):
                if not getattr(node.capabilities, task_type, False):
                    continue
            if requirements.get("min_vram") and node.capabilities.vram_mb < requirements["min_vram"]:
                continue
            if requirements.get("min_params") and node.capabilities.max_model_params < requirements["min_params"]:
                continue
            if not any(c.url == node.url for c in candidates):
                candidates.append(node)

        if not candidates:
            all_nodes = list(self._compute_nodes.values()) + self._get_heartbeat_workers()
            seen_urls = set()
            deduped = []
            for n in all_nodes:
                if n.healthy and n.url not in seen_urls:
                    seen_urls.add(n.url)
                    deduped.append(n)
            candidates = deduped

        if not candidates:
            return None

        # Sort by: model match (sticky) > tier preference > load, with thermal/battery penalty
        tier_order = {"gpu-primary": 0, "gpu-secondary": 1, "cpu-capable": 2, "lightweight": 3}
        routing_cfg = self._task_routing.get(task_type, {})
        prefer_tiers = routing_cfg.get("prefer_tier", [])
        prefer_model = routing_cfg.get("prefer_model", "")  # e.g. "Qwen3-Coder"

        def sort_key(n):
            # Model-aware sticky routing: prefer workers with the right model loaded
            # to avoid expensive model swaps (30-120s each)
            model_rank = 1  # no match
            if prefer_model and n.model_name:
                if prefer_model.lower() in n.model_name.lower():
                    model_rank = 0  # exact/substring match — strong preference

            tier_rank = 99
            for i, t in enumerate(prefer_tiers):
                if n.tier == t:
                    tier_rank = i
                    break
            if tier_rank == 99:
                tier_rank = tier_order.get(n.tier, 50)
            caps = n.capabilities
            if getattr(caps, "thermal_throttled", False):
                tier_rank += 10
            battery = getattr(caps, "battery_pct", -1)
            if 0 <= battery < 20 and not getattr(caps, "battery_charging", False):
                tier_rank += 10
            return (model_rank, tier_rank, n.load)

        candidates.sort(key=sort_key)
        chosen = candidates[0]
        log.debug(
            "route_task(%s): chose %s (tier=%s, model=%s, load=%.1f, "
            "model_match=%s, candidates=%d)",
            task_type, chosen.url, chosen.tier, chosen.model_name or "?",
            chosen.load,
            bool(prefer_model and chosen.model_name and
                 prefer_model.lower() in chosen.model_name.lower()),
            len(candidates),
        )
        return candidates[0].url

    def _get_heartbeat_workers(self) -> list[ComputeNode]:
        """Convert heartbeat-registered mesh workers into ComputeNode objects.

        Reads from the unified _heartbeat_nodes registry (no cross-module import).
        """
        nodes = []
        now = time.time()
        for key, w in self._heartbeat_nodes.items():
            age = now - w.get("last_heartbeat", 0)
            healthy = age < 120
            caps = DeviceCapabilities.from_dict(w.get("capabilities", {}))
            node = ComputeNode(
                name=f"heartbeat-{key}",
                url=f"http://{key}",
                capabilities=caps,
                tier=w.get("tier", "lightweight"),
                healthy=healthy,
                last_check=w.get("last_heartbeat", 0),
                active_tasks=w.get("active_tasks", 0),
                model_name=w.get("model_name", ""),
            )
            nodes.append(node)
        return nodes

    # --- Heartbeat registry (unified, replaces routes.py _mesh_workers) ---

    def register_heartbeat(self, data: dict) -> tuple[str, bool]:
        """Register or update a worker from heartbeat data.

        Returns (key, accepted). accepted=False if at capacity for new workers.
        """
        hostname = data.get("hostname", "")
        port = data.get("port", 8200)
        if not hostname:
            return "", False

        key = f"{hostname}:{port}"

        # Reject new registrations if at capacity (existing workers can still update)
        if key not in self._heartbeat_nodes and len(self._heartbeat_nodes) >= self._max_heartbeat_nodes:
            return key, False

        self._heartbeat_nodes[key] = {
            "hostname": hostname,
            "port": port,
            "tier": data.get("tier", "unknown"),
            "capabilities": data.get("capabilities", {}),
            "model_name": data.get("model_name", ""),
            "active_tasks": data.get("active_tasks", 0),
            "stats": data.get("stats", {}),
            "uptime_s": data.get("uptime_s", 0),
            "last_heartbeat": time.time(),
        }
        return key, True

    def get_mesh_workers(self) -> list[dict]:
        """Return all heartbeat-registered workers with health status.

        Cleans up stale entries (>10 min without heartbeat).
        """
        now = time.time()
        workers = []
        stale_keys = []
        for key, w in self._heartbeat_nodes.items():
            age = now - w.get("last_heartbeat", 0)
            healthy = age < 120  # stale after 2 min without heartbeat
            if age > 600:  # remove after 10 min
                stale_keys.append(key)
                continue
            workers.append({
                **w,
                "key": key,
                "healthy": healthy,
                "heartbeat_age_s": round(age),
            })
        for k in stale_keys:
            del self._heartbeat_nodes[k]
        return workers

    def get_all_healthy_workers(self) -> list[ComputeNode]:
        """Return all healthy workers from both discovery and heartbeat sources."""
        self.__init_compute()
        all_nodes = []
        seen_urls = set()
        for node in self._compute_nodes.values():
            if node.healthy and node.url not in seen_urls:
                all_nodes.append(node)
                seen_urls.add(node.url)
        for node in self._get_heartbeat_workers():
            if node.healthy and node.url not in seen_urls:
                all_nodes.append(node)
                seen_urls.add(node.url)
        return all_nodes

    async def discover_workers(self):
        """Discover worker agents on the Tailscale mesh (port 8200)."""
        self.__init_compute()
        try:
            proc = await asyncio.create_subprocess_exec(
                "tailscale", "status", "--json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                return

            data = json.loads(stdout)
            peers = data.get("Peer", {})

            for peer_id, peer in peers.items():
                if not peer.get("Online"):
                    continue
                ips = peer.get("TailscaleIPs", [])
                if not ips:
                    continue
                ip = ips[0]
                hostname = peer.get("HostName", ip)
                name = f"worker-{hostname}"

                if self._discovery_failures.get(name, 0) >= self._max_discovery_failures:
                    continue

                async with self._lock:
                    if name in self._compute_nodes:
                        continue

                # Probe for worker agent on configured ports
                found = False
                for port in self._worker_probe_ports:
                    url = f"http://{ip}:{port}"
                    node = ComputeNode(name=name, url=url)
                    if await self.check_worker_health(node):
                        async with self._lock:
                            self._compute_nodes[name] = node
                        self._discovery_failures.pop(name, None)
                        log.info(f"Auto-discovered worker: {name} ({node.tier}, "
                                 f"caps={node.capabilities.to_dict()})")
                        found = True
                        break
                if not found:
                    self._discovery_failures[name] = self._discovery_failures.get(name, 0) + 1

        except FileNotFoundError:
            pass
        except Exception as e:
            log.debug(f"Worker discovery failed: {e}")

    def compute_status(self) -> list[dict]:
        """Return status of all compute nodes (discovered + heartbeat)."""
        self.__init_compute()
        nodes = [node.to_dict() for node in self._compute_nodes.values()]
        seen_urls = {n["url"] for n in nodes}
        for node in self._get_heartbeat_workers():
            if node.url not in seen_urls:
                nodes.append(node.to_dict())
                seen_urls.add(node.url)
        return nodes
