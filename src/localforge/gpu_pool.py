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
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

log = logging.getLogger("gpu-pool")


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
    gpu_type: str = ""             # nvidia, apple_silicon, adreno, none
    cpu_cores: int = 0

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

    @property
    def load(self) -> int:
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
        """Probe a single backend's health."""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{backend.url}/internal/model/info")
                if resp.status_code == 200:
                    data = resp.json()
                    backend.model_name = data.get("model_name", "")
                    backend.model_type = self._classify_model(backend.model_name)
                    backend.healthy = True
                    backend.missed_pings = 0
                    backend.last_check = time.time()
                    return True
        except Exception:
            pass

        backend.missed_pings += 1
        if backend.missed_pings >= 3:
            if backend.healthy:
                log.warning(f"Backend {backend.name} marked unhealthy after 3 missed pings")
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

                if name in self._backends:
                    continue

                # Probe for text-gen-webui on :5000
                url = f"http://{ip}:5000/v1"
                test_backend = Backend(name=name, url=url)
                if await self.check_health(test_backend):
                    self._backends[name] = test_backend
                    log.info(f"Auto-discovered backend: {name} ({test_backend.model_name})")

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
        Prefers backends whose loaded model matches the task type,
        then falls back to any healthy backend. Among matches, picks least-loaded.
        """
        candidates = []
        fallbacks = []

        for b in self._backends.values():
            if not b.healthy:
                continue
            if b.model_type == task_type:
                candidates.append(b)
            else:
                fallbacks.append(b)

        pool = candidates or fallbacks
        if not pool:
            return None

        # Least-loaded
        best = min(pool, key=lambda b: b.load)
        return best.url

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
                "missed_pings": b.missed_pings,
            }
            for b in self._backends.values()
        ]

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
        except Exception:
            pass

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
        """
        self.__init_compute()
        requirements = requirements or {}

        candidates = []
        for node in self._compute_nodes.values():
            if not node.healthy:
                continue
            # Check capability
            if hasattr(node.capabilities, task_type):
                if not getattr(node.capabilities, task_type, False):
                    continue
            # Check requirements
            if requirements.get("min_vram") and node.capabilities.vram_mb < requirements["min_vram"]:
                continue
            if requirements.get("min_params") and node.capabilities.max_model_params < requirements["min_params"]:
                continue
            candidates.append(node)

        if not candidates:
            # Fallback: try any healthy node
            candidates = [n for n in self._compute_nodes.values() if n.healthy]

        if not candidates:
            return None

        # Sort by tier preference, then load
        tier_order = {"gpu-primary": 0, "gpu-secondary": 1, "cpu-capable": 2, "lightweight": 3}
        routing_cfg = self._task_routing.get(task_type, {})
        prefer_tiers = routing_cfg.get("prefer_tier", [])

        def sort_key(n):
            # Preferred tier gets priority
            tier_rank = 99
            for i, t in enumerate(prefer_tiers):
                if n.tier == t:
                    tier_rank = i
                    break
            if tier_rank == 99:
                tier_rank = tier_order.get(n.tier, 50)
            return (tier_rank, n.load)

        candidates.sort(key=sort_key)
        return candidates[0].url

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

                if name in self._compute_nodes:
                    continue

                # Probe for worker agent on configured port
                url = f"http://{ip}:{self._worker_port}"
                node = ComputeNode(name=name, url=url)
                if await self.check_worker_health(node):
                    self._compute_nodes[name] = node
                    log.info(f"Auto-discovered worker: {name} ({node.tier}, "
                             f"caps={node.capabilities.to_dict()})")

        except FileNotFoundError:
            pass
        except Exception as e:
            log.debug(f"Worker discovery failed: {e}")

    def compute_status(self) -> list[dict]:
        """Return status of all compute nodes."""
        self.__init_compute()
        return [node.to_dict() for node in self._compute_nodes.values()]
