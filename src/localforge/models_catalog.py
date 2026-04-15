"""Curated GGUF model catalog for worker self-install + hot-swap.

Shared source of truth for:
  * the Windows/macOS/Linux bootstrappers, which pick a VRAM-sized default
    on first install (and accept `--model-id <catalog-id>` to pick explicitly)
  * the hub's `GET /api/mesh/models/catalog` endpoint, which the dashboard
    mesh-tab uses to offer one-click model downloads per enrolled worker
  * the worker's `POST /models/download` handler, which validates that a
    requested URL matches a catalog entry before streaming it (prevents
    using the mesh as an open file-download proxy)

URLs resolve to public Hugging Face files — `/resolve/main/<filename>` —
so no auth header is required. Sizes are repo-declared; `min_vram_mb`
bakes in ~1 GB of headroom for 4k ctx cache + desktop compositor draw.
`active_b` is only populated for Mixture-of-Experts models where the
active parameter count differs meaningfully from the total.
"""

from __future__ import annotations

from typing import TypedDict, Literal


class Model(TypedDict, total=False):
    id: str                # stable catalog id, matches --model-id on bootstrappers
    name: str              # human label for the dashboard
    family: str            # "qwen3.5", "qwen3-coder", etc.
    params_b: float        # total parameters in billions
    active_b: float        # active parameters (MoE only)
    quant: str             # e.g. "Q4_K_M", "UD-Q4_K_XL"
    size_gb: float         # GGUF file size on disk
    min_vram_mb: int       # minimum VRAM for full GPU offload at 4k ctx
    filename: str          # target filename on the worker
    url: str               # direct download URL
    tags: list[str]        # "chat", "code", "vision", "reasoning", "moe", ...
    tier: Literal["tiny", "small", "medium", "large", "xl"]


MODELS: list[Model] = [
    # --- TIER: tiny (~2 GB VRAM, phones + low-end laptops) ----------------
    {
        "id": "qwen3.5-2b-q5", "name": "Qwen3.5-2B · Q5_K_M",
        "family": "qwen3.5", "params_b": 2, "quant": "Q5_K_M",
        "size_gb": 1.44, "min_vram_mb": 2500,
        "filename": "Qwen3.5-2B-Q5_K_M.gguf",
        "url": "https://huggingface.co/unsloth/Qwen3.5-2B-GGUF/resolve/main/Qwen3.5-2B-Q5_K_M.gguf",
        "tags": ["chat", "vision"],
        "tier": "tiny",
    },
    {
        "id": "qwen3.5-2b-q8", "name": "Qwen3.5-2B · Q8_0",
        "family": "qwen3.5", "params_b": 2, "quant": "Q8_0",
        "size_gb": 2.01, "min_vram_mb": 3000,
        "filename": "Qwen3.5-2B-Q8_0.gguf",
        "url": "https://huggingface.co/unsloth/Qwen3.5-2B-GGUF/resolve/main/Qwen3.5-2B-Q8_0.gguf",
        "tags": ["chat", "vision"],
        "tier": "tiny",
    },
    # --- TIER: small (3.5-5 GB VRAM, GTX 1650 class) ----------------------
    {
        "id": "qwen3.5-4b-q4", "name": "Qwen3.5-4B · Q4_K_M",
        "family": "qwen3.5", "params_b": 4, "quant": "Q4_K_M",
        "size_gb": 2.74, "min_vram_mb": 3800,
        "filename": "Qwen3.5-4B-Q4_K_M.gguf",
        "url": "https://huggingface.co/unsloth/Qwen3.5-4B-GGUF/resolve/main/Qwen3.5-4B-Q4_K_M.gguf",
        "tags": ["chat", "vision"],
        "tier": "small",
    },
    {
        "id": "qwen3.5-4b-q5", "name": "Qwen3.5-4B · Q5_K_M",
        "family": "qwen3.5", "params_b": 4, "quant": "Q5_K_M",
        "size_gb": 3.14, "min_vram_mb": 4500,
        "filename": "Qwen3.5-4B-Q5_K_M.gguf",
        "url": "https://huggingface.co/unsloth/Qwen3.5-4B-GGUF/resolve/main/Qwen3.5-4B-Q5_K_M.gguf",
        "tags": ["chat", "vision"],
        "tier": "small",
    },
    {
        "id": "qwen3.5-4b-ud-q4", "name": "Qwen3.5-4B · UD-Q4_K_XL (Unsloth dynamic)",
        "family": "qwen3.5", "params_b": 4, "quant": "UD-Q4_K_XL",
        "size_gb": 2.91, "min_vram_mb": 4000,
        "filename": "Qwen3.5-4B-UD-Q4_K_XL.gguf",
        "url": "https://huggingface.co/unsloth/Qwen3.5-4B-GGUF/resolve/main/Qwen3.5-4B-UD-Q4_K_XL.gguf",
        "tags": ["chat", "vision", "unsloth-dynamic"],
        "tier": "small",
    },
    # --- TIER: medium (6-10 GB VRAM) --------------------------------------
    {
        "id": "qwen3.5-9b-q4", "name": "Qwen3.5-9B · Q4_K_M",
        "family": "qwen3.5", "params_b": 9, "quant": "Q4_K_M",
        "size_gb": 5.68, "min_vram_mb": 7000,
        "filename": "Qwen3.5-9B-Q4_K_M.gguf",
        "url": "https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/resolve/main/Qwen3.5-9B-Q4_K_M.gguf",
        "tags": ["chat", "vision"],
        "tier": "medium",
    },
    {
        "id": "qwen3.5-9b-ud-q4", "name": "Qwen3.5-9B · UD-Q4_K_XL (Unsloth dynamic)",
        "family": "qwen3.5", "params_b": 9, "quant": "UD-Q4_K_XL",
        "size_gb": 5.97, "min_vram_mb": 7500,
        "filename": "Qwen3.5-9B-UD-Q4_K_XL.gguf",
        "url": "https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/resolve/main/Qwen3.5-9B-UD-Q4_K_XL.gguf",
        "tags": ["chat", "vision", "unsloth-dynamic"],
        "tier": "medium",
    },
    {
        "id": "qwen3.5-9b-q5", "name": "Qwen3.5-9B · Q5_K_M",
        "family": "qwen3.5", "params_b": 9, "quant": "Q5_K_M",
        "size_gb": 6.58, "min_vram_mb": 8500,
        "filename": "Qwen3.5-9B-Q5_K_M.gguf",
        "url": "https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/resolve/main/Qwen3.5-9B-Q5_K_M.gguf",
        "tags": ["chat", "vision"],
        "tier": "medium",
    },
    # --- TIER: large (16-20 GB VRAM, dense reasoning) ---------------------
    {
        "id": "qwen3.5-27b-ud-iq4", "name": "Qwen3.5-27B · UD-IQ4_XS",
        "family": "qwen3.5", "params_b": 27, "quant": "UD-IQ4_XS",
        "size_gb": 15.0, "min_vram_mb": 16000,
        "filename": "Qwen3.5-27B-UD-IQ4_XS.gguf",
        "url": "https://huggingface.co/unsloth/Qwen3.5-27B-GGUF/resolve/main/Qwen3.5-27B-UD-IQ4_XS.gguf",
        "tags": ["chat", "reasoning", "vision", "unsloth-dynamic"],
        "tier": "large",
    },
    {
        "id": "qwen3.5-27b-q4", "name": "Qwen3.5-27B · Q4_K_M",
        "family": "qwen3.5", "params_b": 27, "quant": "Q4_K_M",
        "size_gb": 16.7, "min_vram_mb": 18000,
        "filename": "Qwen3.5-27B-Q4_K_M.gguf",
        "url": "https://huggingface.co/unsloth/Qwen3.5-27B-GGUF/resolve/main/Qwen3.5-27B-Q4_K_M.gguf",
        "tags": ["chat", "reasoning", "vision"],
        "tier": "large",
    },
    # --- TIER: xl (24+ GB VRAM or MoE on 16 GB) ---------------------------
    # Qwen3.5-35B-A3B is MoE: 35B total, 3B active per token → runs well
    # on GPUs that can fit the weights even if dense 35B wouldn't.
    {
        "id": "qwen3.5-35b-a3b-ud-q4", "name": "Qwen3.5-35B-A3B · UD-Q4_K_XL (MoE)",
        "family": "qwen3.5-moe", "params_b": 35, "active_b": 3,
        "quant": "UD-Q4_K_XL",
        "size_gb": 22.2, "min_vram_mb": 24000,
        "filename": "Qwen3.5-35B-A3B-UD-Q4_K_XL.gguf",
        "url": "https://huggingface.co/unsloth/Qwen3.5-35B-A3B-GGUF/resolve/main/Qwen3.5-35B-A3B-UD-Q4_K_XL.gguf",
        "tags": ["chat", "reasoning", "vision", "moe"],
        "tier": "xl",
    },
    {
        "id": "qwen3.5-35b-a3b-ud-q5", "name": "Qwen3.5-35B-A3B · UD-Q5_K_XL (MoE)",
        "family": "qwen3.5-moe", "params_b": 35, "active_b": 3,
        "quant": "UD-Q5_K_XL",
        "size_gb": 26.4, "min_vram_mb": 28000,
        "filename": "Qwen3.5-35B-A3B-UD-Q5_K_XL.gguf",
        "url": "https://huggingface.co/unsloth/Qwen3.5-35B-A3B-GGUF/resolve/main/Qwen3.5-35B-A3B-UD-Q5_K_XL.gguf",
        "tags": ["chat", "reasoning", "vision", "moe"],
        "tier": "xl",
    },
    # --- SPECIALTY: code (MoE, long context) ------------------------------
    {
        "id": "qwen3-coder-30b-a3b-ud-q4",
        "name": "Qwen3-Coder-30B-A3B · UD-Q4_K_XL (MoE, code)",
        "family": "qwen3-coder-moe", "params_b": 30, "active_b": 3,
        "quant": "UD-Q4_K_XL",
        "size_gb": 17.7, "min_vram_mb": 20000,
        "filename": "Qwen3-Coder-30B-A3B-Instruct-UD-Q4_K_XL.gguf",
        "url": "https://huggingface.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF/resolve/main/Qwen3-Coder-30B-A3B-Instruct-UD-Q4_K_XL.gguf",
        "tags": ["code", "moe"],
        "tier": "xl",
    },
]


# Per-tier default (used by bootstrapper `-ModelTier` flag + auto-pick fallback)
TIER_DEFAULTS: dict[str, str] = {
    "tiny":   "qwen3.5-2b-q5",
    "small":  "qwen3.5-4b-q4",
    "medium": "qwen3.5-9b-q4",
    "large":  "qwen3.5-27b-ud-iq4",
    "xl":     "qwen3.5-35b-a3b-ud-q4",
}


# All HF hostnames we consider trusted for `POST /models/download`. The worker
# rejects downloads to any URL not hitting one of these, which keeps the
# authenticated download endpoint from doubling as an open proxy.
TRUSTED_HOSTS: set[str] = {
    "huggingface.co",
    "cdn-lfs.huggingface.co",
    "cdn-lfs-us-1.huggingface.co",
    "cdn-lfs-eu-1.huggingface.co",
}


def by_id(model_id: str) -> Model | None:
    for m in MODELS:
        if m["id"] == model_id:
            return m
    return None


def pick_for_vram(vram_mb: int, purpose: str = "chat") -> Model:
    """Largest catalog model whose min_vram fits, matching `purpose` tag.

    Falls through to the tiny default if nothing fits (e.g. headless
    server, phone worker) — the caller can combine this with --no-llama
    if they want to skip inference entirely.
    """
    candidates = [m for m in MODELS if purpose in m["tags"] and m["min_vram_mb"] <= vram_mb]
    if not candidates:
        default = by_id(TIER_DEFAULTS["tiny"])
        if default is None:
            raise RuntimeError("Catalog has no tiny default — check TIER_DEFAULTS")
        return default
    return max(candidates, key=lambda m: m["params_b"])


def catalog_json() -> dict:
    """Shape served by GET /api/mesh/models/catalog."""
    return {
        "models": MODELS,
        "tier_defaults": TIER_DEFAULTS,
        "trusted_hosts": sorted(TRUSTED_HOSTS),
    }
