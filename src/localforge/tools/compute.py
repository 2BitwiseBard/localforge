"""Compute mesh tools — status, routing, and task dispatch."""

import json

from localforge.tools import tool_handler

# Set by gateway.py after startup
_gpu_pool = None


@tool_handler(
    name="compute_status",
    description=(
        "Show all connected devices in the compute mesh, their capabilities, "
        "load, health, and tier. Includes GPU backends, worker agents, and "
        "mesh nodes registered via heartbeat."
    ),
    schema={"type": "object", "properties": {}, "required": []},
)
async def compute_status_tool(args: dict) -> str:
    parts = []

    if _gpu_pool:
        backends = _gpu_pool.status()
        if backends:
            parts.append("## GPU Backends (text-gen-webui)")
            for b in backends:
                status = "healthy" if b["healthy"] else "unhealthy"
                parts.append(
                    f"  {b['name']}: {status}, model={b['model_name'] or '(none)'}, "
                    f"type={b['model_type']}, active={b['active_requests']}"
                )

    if _gpu_pool and hasattr(_gpu_pool, "_compute_nodes"):
        nodes = _gpu_pool.compute_status()
        if nodes:
            parts.append("\n## Compute Mesh Nodes")
            for n in nodes:
                caps = n.get("capabilities", {})
                cap_flags = [k for k, v in caps.items() if isinstance(v, bool) and v]
                parts.append(
                    f"  {n['name']}: tier={n['tier']}, "
                    f"{'healthy' if n['healthy'] else 'unhealthy'}, "
                    f"tasks={n['active_tasks']}, "
                    f"caps=[{', '.join(cap_flags)}]"
                )

    # Include mesh workers registered via heartbeat
    mesh_workers = _get_mesh_workers()
    if mesh_workers:
        parts.append("\n## Mesh Workers (heartbeat)")
        for w in mesh_workers:
            caps = w.get("capabilities", {})
            cap_flags = [k for k, v in caps.items() if isinstance(v, bool) and v]
            health = "healthy" if w.get("healthy") else "stale"
            stats = w.get("stats", {})
            completed = stats.get("tasks_completed", 0)
            parts.append(
                f"  {w['key']}: tier={w['tier']}, {health}, "
                f"active={w['active_tasks']}, completed={completed}, "
                f"caps=[{', '.join(cap_flags)}]"
            )

    if not parts:
        return "No backends or compute nodes registered."
    return "\n".join(parts)


@tool_handler(
    name="compute_route",
    description=(
        "Preview where a task would be routed in the compute mesh. "
        "Task types: inference, embeddings, tts, stt, reranking, classification, rerank."
    ),
    schema={
        "type": "object",
        "properties": {
            "task_type": {"type": "string", "description": "Type of task to route"},
            "min_vram": {"type": "integer", "description": "Minimum VRAM required (MB)"},
        },
        "required": ["task_type"],
    },
)
async def compute_route_tool(args: dict) -> str:
    task_type = args["task_type"]
    requirements = {}
    if args.get("min_vram"):
        requirements["min_vram"] = args["min_vram"]

    # Try GPU pool first
    if _gpu_pool:
        url = _gpu_pool.route_task(task_type, requirements)
        if url:
            return f"Task '{task_type}' would be routed to: {url}"

    # Try mesh workers
    mesh_workers = _get_mesh_workers()
    for w in mesh_workers:
        if not w.get("healthy"):
            continue
        caps = w.get("capabilities", {})
        # Check capability match
        cap_map = {
            "inference": "inference",
            "chat": "inference",
            "embeddings": "embeddings",
            "tts": "tts",
            "stt": "stt",
            "reranking": "reranking",
            "rerank": "reranking",
            "classification": "classification",
            "classify": "classification",
        }
        needed_cap = cap_map.get(task_type, task_type)
        if caps.get(needed_cap):
            # Check VRAM requirement
            min_vram = requirements.get("min_vram", 0)
            if min_vram and caps.get("vram_mb", 0) < min_vram:
                continue
            return f"Task '{task_type}' would be routed to mesh worker: {w['key']} (tier={w['tier']})"

    return f"No suitable device found for task type '{task_type}'"


@tool_handler(
    name="mesh_dispatch",
    description=(
        "Dispatch a task to a specific mesh worker or let the router choose. "
        "Returns the task result. Task types: chat, embeddings, tts, stt, classify, rerank."
    ),
    schema={
        "type": "object",
        "properties": {
            "task_type": {
                "type": "string",
                "description": "Type of task: chat, embeddings, tts, stt, classify, rerank",
            },
            "payload": {
                "type": "object",
                "description": "Task payload (e.g. {prompt, max_tokens} for chat, {texts} for embeddings)",
            },
            "target": {
                "type": "string",
                "description": "Target worker (hostname:port). Leave empty for auto-routing.",
            },
        },
        "required": ["task_type", "payload"],
    },
)
async def mesh_dispatch_tool(args: dict) -> str:
    import httpx

    task_type = args["task_type"]
    payload = args.get("payload", {})
    target = args.get("target", "")
    payload["type"] = task_type

    # Resolve target
    candidates = []
    if not target:
        mesh_workers = _get_mesh_workers()
        cap_map = {
            "chat": "inference",
            "embeddings": "embeddings",
            "tts": "tts",
            "stt": "stt",
            "classify": "classification",
            "rerank": "reranking",
        }
        needed = cap_map.get(task_type, task_type)
        for w in mesh_workers:
            if w.get("healthy") and w.get("capabilities", {}).get(needed):
                candidates.append(w["key"])
        if not candidates:
            return f"No available worker for task type '{task_type}'"
        target = candidates[0]
    else:
        candidates = [target]

    # Try target, then fall back to other candidates (retry with failover)
    last_error = ""
    for attempt, worker_key in enumerate(candidates[:3]):  # max 3 attempts
        url = f"http://{worker_key}/task"
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(url, json=payload)
                result = resp.json()
                if "error" in result:
                    last_error = f"Worker {worker_key}: {result['error']}"
                    # Record failure in circuit breaker if gpu_pool available
                    if _gpu_pool:
                        _gpu_pool.record_failure(url)
                    continue
                # Record success
                if _gpu_pool:
                    _gpu_pool.record_success(url)
                return json.dumps(result, indent=2)
        except Exception as e:
            last_error = f"Failed to dispatch to {worker_key}: {e}"
            if _gpu_pool:
                _gpu_pool.record_failure(url)

    return last_error or f"All workers failed for task type '{task_type}'"


@tool_handler(
    name="mesh_fan_out",
    description=(
        "Distribute multiple prompts across mesh workers in parallel. "
        "Each prompt goes to the least-loaded healthy worker. "
        "Falls back to hub for any that fail. Returns all results in order."
    ),
    schema={
        "type": "object",
        "properties": {
            "prompts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of prompts to distribute across mesh workers",
            },
            "max_tokens": {"type": "integer", "description": "Max tokens per response (default: 1024)"},
        },
        "required": ["prompts"],
    },
)
async def mesh_fan_out_tool(args: dict) -> str:
    import asyncio

    import httpx

    prompts = args["prompts"]
    max_tokens = args.get("max_tokens", 1024)

    if not prompts:
        return "Error: provide at least one prompt"
    if len(prompts) > 50:
        return f"Error: max 50 prompts (got {len(prompts)})"

    # Get available workers
    workers = _get_healthy_worker_urls()
    if not workers:
        return "No mesh workers available. Use fan_out for hub-only parallel dispatch."

    async def _dispatch_one(idx: int, prompt: str, worker_url: str) -> tuple[int, str, str]:
        """Dispatch a single prompt to a worker. Returns (idx, result, worker_url)."""
        payload = {
            "type": "chat",
            "prompt": prompt,
            "max_tokens": max_tokens,
        }
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(f"{worker_url}/task", json=payload)
                result = resp.json()
                if "error" in result:
                    return (idx, f"Error: {result['error']}", worker_url)
                return (idx, result.get("response", ""), worker_url)
        except Exception as e:
            return (idx, f"Error: {e}", worker_url)

    # Round-robin distribute across workers
    tasks = []
    for i, prompt in enumerate(prompts):
        worker_url = workers[i % len(workers)]
        tasks.append(_dispatch_one(i, prompt, worker_url))

    results = await asyncio.gather(*tasks)
    results_sorted = sorted(results, key=lambda x: x[0])

    parts = []
    for idx, result, worker_url in results_sorted:
        preview = prompts[idx][:60] + "..." if len(prompts[idx]) > 60 else prompts[idx]
        worker_name = worker_url.split("//")[1] if "//" in worker_url else worker_url
        parts.append(f"--- [{idx + 1}] {preview} ({worker_name}) ---\n{result}")

    return f"Distributed {len(prompts)} prompts across {len(workers)} workers\n\n" + "\n\n".join(parts)


@tool_handler(
    name="mesh_batch_embed",
    description=(
        "Distribute embedding computation across mesh workers. "
        "Splits texts into batches and sends each batch to a different worker "
        "running fastembed on CPU. Collects and returns all embeddings."
    ),
    schema={
        "type": "object",
        "properties": {
            "texts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Texts to embed",
            },
            "batch_size": {"type": "integer", "description": "Texts per batch (default: 32)"},
        },
        "required": ["texts"],
    },
)
async def mesh_batch_embed_tool(args: dict) -> str:
    import asyncio

    import httpx

    texts = args["texts"]
    batch_size = args.get("batch_size", 32)

    if not texts:
        return "Error: provide at least one text"

    # Get workers with embeddings capability
    workers = _get_healthy_worker_urls(capability="embeddings")
    if not workers:
        return "No workers with embeddings capability available."

    # Split into batches
    batches = [texts[i : i + batch_size] for i in range(0, len(texts), batch_size)]

    async def _embed_batch(batch_idx: int, batch: list[str], worker_url: str) -> tuple[int, list | str]:
        payload = {"type": "embeddings", "texts": batch}
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(f"{worker_url}/task", json=payload)
                result = resp.json()
                if "error" in result:
                    return (batch_idx, f"Error: {result['error']}")
                return (batch_idx, result.get("embeddings", []))
        except Exception as e:
            return (batch_idx, f"Error: {e}")

    tasks = []
    for i, batch in enumerate(batches):
        worker_url = workers[i % len(workers)]
        tasks.append(_embed_batch(i, batch, worker_url))

    results = await asyncio.gather(*tasks)
    results_sorted = sorted(results, key=lambda x: x[0])

    # Collect all embeddings in order
    all_embeddings = []
    errors = []
    for batch_idx, result in results_sorted:
        if isinstance(result, str):
            errors.append(result)
        else:
            all_embeddings.extend(result)

    parts = [f"Embedded {len(all_embeddings)} texts in {len(batches)} batches across {len(workers)} workers"]
    if errors:
        parts.append(f"Errors: {'; '.join(errors)}")
    parts.append(f"Embedding dimension: {len(all_embeddings[0]) if all_embeddings else 'N/A'}")

    return "\n".join(parts)


@tool_handler(
    name="mesh_model_recommend",
    description=(
        "Given device hardware info, recommend the best GGUF model and return "
        "download instructions. Uses the detect.py heuristics."
    ),
    schema={
        "type": "object",
        "properties": {
            "hardware": {
                "type": "object",
                "description": "Hardware info dict (from detect.py or /health endpoint)",
            },
        },
        "required": [],
    },
)
async def mesh_model_recommend_tool(args: dict) -> str:
    from localforge.workers.detect import HardwareInfo

    hw_dict = args.get("hardware", {})
    if hw_dict:
        # Build HardwareInfo from dict
        hw = HardwareInfo(
            platform=hw_dict.get("platform", ""),
            gpu_type=hw_dict.get("gpu_type", "none"),
            gpu_name=hw_dict.get("gpu_name", ""),
            vram_mb=hw_dict.get("vram_mb", 0),
            ram_mb=hw_dict.get("ram_mb", 0),
            cpu_cores=hw_dict.get("cpu_cores", 0),
        )
    else:
        # Use local detection
        from localforge.workers.detect import detect

        hw = detect()

    rec = hw.recommended_model()
    if not rec:
        return (
            f"Device: {hw.gpu_name or 'CPU only'} ({hw.ram_mb}MB RAM)\n"
            f"Tier: {hw.tier()}\n"
            f"Recommendation: Device may not have enough resources for local inference."
        )

    filename, size_gb = rec
    return (
        f"Device: {hw.gpu_name or 'CPU only'} ({hw.ram_mb}MB RAM, {hw.vram_mb}MB VRAM)\n"
        f"Tier: {hw.tier()}\n"
        f"Recommended model: {filename} (~{size_gb}GB)\n"
        f"\nDownload from hub:\n"
        f"  curl -o {filename} http://ai-hub:8100/api/models/download/{filename}\n"
        f"\nOr place any .gguf in ~/.ai-hub-worker/models/ and restart the worker."
    )


def _get_mesh_workers() -> list[dict]:
    """Get mesh workers from the gpu_pool's unified heartbeat registry."""
    if _gpu_pool is not None:
        return _gpu_pool.get_mesh_workers()
    return []


def _get_healthy_worker_urls(capability: str = "inference") -> list[str]:
    """Return URLs of healthy mesh workers with a given capability."""
    urls = []
    # From gpu_pool (includes both discovered and heartbeat workers)
    if _gpu_pool:
        for node in _gpu_pool.get_all_healthy_workers():
            caps = node.capabilities
            cap_map = {
                "inference": "inference",
                "embeddings": "embeddings",
                "reranking": "reranking",
                "tts": "tts",
                "stt": "stt",
                "classification": "classification",
            }
            needed = cap_map.get(capability, capability)
            if getattr(caps, needed, False):
                urls.append(node.url)
    return urls


@tool_handler(
    name="compute_test",
    description=(
        "End-to-end mesh validation: sends a simple chat task to each healthy "
        "worker, reports latency and success/failure. Like a mesh ping — confirms "
        "the full path works before relying on it. Useful after bringing up a new node."
    ),
    schema={
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Test prompt to send (default: 'Say hello in one word.')",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout per worker in seconds (default: 30)",
            },
        },
        "required": [],
    },
)
async def compute_test_tool(args: dict) -> str:
    import asyncio
    import time

    import httpx

    prompt = args.get("prompt", "Say hello in one word.")
    timeout = args.get("timeout", 30)

    workers = _get_mesh_workers()
    healthy = [w for w in workers if w.get("healthy")]

    if not healthy:
        return "No healthy mesh workers found. Use compute_status to check the mesh."

    results = []

    async def _test_worker(worker: dict) -> dict:
        key = worker.get("key", "unknown")
        url = f"http://{key}/task"
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    url,
                    json={
                        "type": "chat",
                        "prompt": prompt,
                        "max_tokens": 32,
                    },
                )
                elapsed = time.monotonic() - start
                data = resp.json()
                if "error" in data:
                    return {"worker": key, "status": "error", "error": data["error"], "latency_ms": int(elapsed * 1000)}
                response_text = data.get("response", "")[:100]
                return {"worker": key, "status": "ok", "response": response_text, "latency_ms": int(elapsed * 1000)}
        except asyncio.TimeoutError:
            return {"worker": key, "status": "timeout", "latency_ms": int((time.monotonic() - start) * 1000)}
        except Exception as e:
            return {
                "worker": key,
                "status": "error",
                "error": str(e),
                "latency_ms": int((time.monotonic() - start) * 1000),
            }

    tasks = [_test_worker(w) for w in healthy]
    results = await asyncio.gather(*tasks)

    # Format output
    lines = [f'Mesh test: {len(healthy)} healthy worker(s), prompt: "{prompt[:50]}"', ""]
    ok_count = 0
    for r in results:
        status_icon = "✓" if r["status"] == "ok" else "✗"
        if r["status"] == "ok":
            ok_count += 1
        line = f"  {status_icon} {r['worker']}: {r['status']} ({r['latency_ms']}ms)"
        if r.get("response"):
            line += f' → "{r["response"]}"'
        if r.get("error"):
            line += f" — {r['error']}"
        lines.append(line)

    lines.append("")
    lines.append(f"Result: {ok_count}/{len(healthy)} workers responding")
    return "\n".join(lines)
