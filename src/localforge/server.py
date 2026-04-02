#!/usr/bin/env python3
"""MCP server wrapping text-generation-webui as a general-purpose local AI agent.

Requires an OpenAI-compatible backend (e.g., text-generation-webui with --api flag).
See docs/quickstart.md for setup instructions.

The OpenAI-compatible endpoint is served at http://localhost:5000/v1

Tools (80):
  Context:        set_context, check_model
  Config:         get_generation_params, set_generation_params, reload_config
  Infrastructure: health_check, token_count, swap_model, stop_generation, encode_tokens, benchmark,
                  unload_model, list_loras, load_lora, unload_loras, decode_tokens, slot_info,
                  warm_model, cache_stats, session_stats
  Analysis:       analyze_code, batch_review, summarize_file, explain_error, file_qa, analyze_image, classify_task
  Generation:     generate_test_stubs, suggest_refactor, draft_docs, draft_commit_message, structured_output,
                  text_complete, get_logits, preview_prompt, set_sampling, validated_chat
  General:        local_chat, multi_turn_chat, review_diff, diff_explain, translate_code, generate_regex, optimize_query
  Parallel:       fan_out, parallel_file_review, quality_sweep
  Memory:         scratchpad, save_note, recall_note, list_notes, delete_note
  Sessions:       save_session, load_session, list_sessions, delete_session
  RAG/Search:     index_directory, search_index, rag_query, list_indexes, delete_index, ingest_document,
                  incremental_index, diff_rag
  Semantic:       embed_text, semantic_search, hybrid_search, rerank_chunks
  Presets:        list_presets, load_preset, list_grammars, load_grammar
  Orchestration:  auto_route, workflow, pipeline, save_pipeline, list_pipelines
  Knowledge:      knowledge_base, doc_lookup
  Git:            git_context
"""
import asyncio
import base64
import hashlib
import json
import logging
import math
import os
import re
import shutil
import sys
import time
from collections import Counter
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Awaitable

import httpx
import yaml
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("local-model")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
TGWUI_BASE = "http://localhost:5000/v1"
TGWUI_INTERNAL = "http://localhost:5000/v1/internal"
MODEL: str | None = None  # auto-detected on first call
NOTES_DIR = Path(os.path.expanduser("~/.claude/mcp-servers/local-model/notes"))
INDEXES_DIR = Path(os.path.expanduser("~/.claude/mcp-servers/local-model/indexes"))
SESSIONS_DIR = Path(os.path.expanduser("~/.claude/mcp-servers/local-model/sessions"))
CONFIG_PATH = Path(os.path.expanduser("~/.claude/mcp-servers/local-model/config.yaml"))

# Multi-backend state
_backends: dict[str, dict[str, Any]] = {}  # name -> {url, priority, optional, healthy}
_active_backend: str | None = None         # name of the currently active backend

# ---------------------------------------------------------------------------
# Generation config system
# ---------------------------------------------------------------------------
# Resolved params: webui settings → config defaults → model overrides → runtime
_config: dict[str, Any] = {}          # loaded from config.yaml
_webui_settings: dict[str, Any] = {}  # loaded from webui settings.yaml
_runtime_overrides: dict[str, Any] = {}  # live overrides via set_generation_params

# ---------------------------------------------------------------------------
# Response cache — avoids redundant model calls for identical prompts
# ---------------------------------------------------------------------------
_response_cache: dict[str, tuple[str, float]] = {}  # hash -> (response, timestamp)
_CACHE_TTL = 300  # 5 minutes
_cache_hits = 0
_cache_misses = 0


def _cache_key(prompt: str, system: str | None, model: str | None, **kwargs: Any) -> str:
    """Generate a cache key from prompt + system + model + gen_params."""
    key_data = f"{model}:{system}:{prompt}:{sorted(kwargs.items())}"
    return hashlib.sha256(key_data.encode()).hexdigest()[:16]


def _cache_get(key: str) -> str | None:
    global _cache_hits, _cache_misses
    if key in _response_cache:
        response, ts = _response_cache[key]
        if time.time() - ts < _CACHE_TTL:
            _cache_hits += 1
            return response
        else:
            del _response_cache[key]
    _cache_misses += 1
    return None


def _cache_put(key: str, response: str):
    _response_cache[key] = (response, time.time())
    # Evict oldest if cache gets too large
    if len(_response_cache) > 200:
        oldest = min(_response_cache, key=lambda k: _response_cache[k][1])
        del _response_cache[oldest]


# ---------------------------------------------------------------------------
# Session statistics — track cumulative token usage per session
# ---------------------------------------------------------------------------
_session_stats: dict[str, Any] = {
    "total_calls": 0,
    "total_tokens_in_approx": 0,
    "total_tokens_out_approx": 0,
    "tool_calls": {},
    "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
}

# Pipeline templates directory
PIPELINES_DIR = Path(os.path.expanduser("~/.claude/mcp-servers/local-model/pipelines"))

# Embedding vectors directory (for semantic search indexes)
VECTORS_DIR = Path(os.path.expanduser("~/.claude/mcp-servers/local-model/vectors"))
FASTEMBED_CACHE = Path(os.path.expanduser("~/.cache/fastembed"))

# ---------------------------------------------------------------------------
# Lazy-loaded embedding models + reranker (all CPU-only, no GPU competition)
# ---------------------------------------------------------------------------
# Dense: code-tuned embeddings for semantic similarity
# Sparse: SPLADE learned sparse vectors for keyword importance
# ColBERT: late-interaction per-token vectors for precise matching
# Reranker: cross-encoder for final re-ordering
_embedding_model = None
_sparse_model = None
_colbert_model = None
_reranker_model = None
_EMBEDDING_MODEL_NAME = "jinaai/jina-embeddings-v2-base-code"  # 768 dims, code-tuned
_SPARSE_MODEL_NAME = "Qdrant/bm42-all-minilm-l6-v2-attentions"  # SPLADE sparse
_COLBERT_MODEL_NAME = "colbert-ir/colbertv2.0"  # late interaction
_RERANKER_MODEL_NAME = "Xenova/ms-marco-MiniLM-L-6-v2"


def _get_embedding_model():
    """Lazy-load the dense embedding model on first use."""
    global _embedding_model
    if _embedding_model is None:
        from fastembed import TextEmbedding
        log.info("Loading dense embedding model: %s", _EMBEDDING_MODEL_NAME)
        _embedding_model = TextEmbedding(_EMBEDDING_MODEL_NAME, cache_dir=str(FASTEMBED_CACHE))
        log.info("Dense embedding model loaded.")
    return _embedding_model


def _get_sparse_model():
    """Lazy-load the SPLADE sparse embedding model on first use."""
    global _sparse_model
    if _sparse_model is None:
        from fastembed import SparseTextEmbedding
        log.info("Loading sparse model: %s", _SPARSE_MODEL_NAME)
        _sparse_model = SparseTextEmbedding(_SPARSE_MODEL_NAME, cache_dir=str(FASTEMBED_CACHE))
        log.info("Sparse model loaded.")
    return _sparse_model


def _get_colbert_model():
    """Lazy-load the ColBERT late-interaction model on first use."""
    global _colbert_model
    if _colbert_model is None:
        from fastembed import LateInteractionTextEmbedding
        log.info("Loading ColBERT model: %s", _COLBERT_MODEL_NAME)
        _colbert_model = LateInteractionTextEmbedding(_COLBERT_MODEL_NAME, cache_dir=str(FASTEMBED_CACHE))
        log.info("ColBERT model loaded.")
    return _colbert_model


def _get_reranker():
    """Lazy-load the reranker model on first use."""
    global _reranker_model
    if _reranker_model is None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder
        log.info("Loading reranker: %s", _RERANKER_MODEL_NAME)
        _reranker_model = TextCrossEncoder(_RERANKER_MODEL_NAME, cache_dir=str(FASTEMBED_CACHE))
        log.info("Reranker loaded.")
    return _reranker_model


def _embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a list of texts with the dense code model. Returns list of vectors."""
    model = _get_embedding_model()
    return [v.tolist() for v in model.embed(texts)]


def _sparse_embed_texts(texts: list[str]) -> list[dict]:
    """Embed texts with SPLADE sparse model. Returns list of {indices: [...], values: [...]}."""
    model = _get_sparse_model()
    results = []
    for sparse_vec in model.embed(texts):
        results.append({
            "indices": sparse_vec.indices.tolist(),
            "values": sparse_vec.values.tolist(),
        })
    return results


def _colbert_embed_texts(texts: list[str]) -> list[list[list[float]]]:
    """Embed texts with ColBERT. Returns list of per-token embedding matrices."""
    model = _get_colbert_model()
    return [v.tolist() for v in model.embed(texts)]


def _colbert_maxsim(query_vecs: list[list[float]], doc_vecs: list[list[float]]) -> float:
    """ColBERT MaxSim scoring: for each query token, find max similarity to any doc token."""
    if not query_vecs or not doc_vecs:
        return 0.0
    total = 0.0
    for qv in query_vecs:
        best = max(_cosine_similarity(qv, dv) for dv in doc_vecs)
        total += best
    return total / len(query_vecs)


def _sparse_similarity(a: dict, b: dict) -> float:
    """Dot product between two sparse vectors (indices+values dicts)."""
    a_map = dict(zip(a["indices"], a["values"]))
    b_map = dict(zip(b["indices"], b["values"]))
    common = set(a_map.keys()) & set(b_map.keys())
    return sum(a_map[k] * b_map[k] for k in common)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two dense vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# Tree-sitter code-aware chunking
# ---------------------------------------------------------------------------
# Map file extensions to tree-sitter language names
_TREESITTER_LANG_MAP: dict[str, str] = {
    ".rs": "rust", ".py": "python", ".ts": "typescript", ".tsx": "tsx",
    ".js": "javascript", ".jsx": "javascript", ".go": "go", ".java": "java",
    ".c": "c", ".cpp": "cpp", ".h": "c", ".hpp": "cpp",
    ".rb": "ruby", ".php": "php", ".swift": "swift",
    ".scala": "scala", ".sh": "bash", ".bash": "bash",
    ".lua": "lua", ".hs": "haskell", ".ex": "elixir", ".exs": "elixir",
    ".toml": "toml", ".yaml": "yaml", ".yml": "yaml", ".json": "json",
    ".html": "html", ".css": "css",
}

# Node types that represent top-level semantic units per language
_TOPLEVEL_NODES: dict[str, set[str]] = {
    "rust": {"function_item", "struct_item", "enum_item", "impl_item",
             "trait_item", "mod_item", "type_item", "const_item", "static_item",
             "use_declaration", "macro_definition"},
    "python": {"function_definition", "class_definition", "decorated_definition"},
    "typescript": {"function_declaration", "class_declaration", "interface_declaration",
                   "type_alias_declaration", "export_statement", "lexical_declaration"},
    "javascript": {"function_declaration", "class_declaration", "export_statement",
                   "lexical_declaration", "variable_declaration"},
    "go": {"function_declaration", "method_declaration", "type_declaration",
           "var_declaration", "const_declaration"},
    "java": {"class_declaration", "interface_declaration", "enum_declaration",
             "method_declaration"},
    "c": {"function_definition", "struct_specifier", "enum_specifier",
          "type_definition", "declaration"},
    "cpp": {"function_definition", "class_specifier", "struct_specifier",
            "enum_specifier", "namespace_definition", "template_declaration"},
}


def _chunk_file_treesitter(path: Path, max_chunk_lines: int = 80) -> list[dict[str, Any]]:
    """Chunk a file using tree-sitter for semantic boundaries.

    Falls back to line-based chunking if tree-sitter doesn't support the language.
    """
    suffix = path.suffix.lower()
    lang_name = _TREESITTER_LANG_MAP.get(suffix)
    if not lang_name:
        return _chunk_file(path)  # fallback to line-based

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    if not content.strip():
        return []

    try:
        import tree_sitter_languages as tsl
        parser = tsl.get_parser(lang_name)
    except Exception:
        return _chunk_file(path)  # fallback

    tree = parser.parse(content.encode("utf-8"))
    root = tree.root_node

    toplevel_types = _TOPLEVEL_NODES.get(lang_name, set())
    lines = content.splitlines()
    chunks: list[dict[str, Any]] = []

    # Collect top-level nodes
    nodes: list[tuple[int, int]] = []  # (start_line, end_line) 0-indexed
    for child in root.children:
        if child.type in toplevel_types or not toplevel_types:
            start = child.start_point[0]
            end = child.end_point[0]
            nodes.append((start, end))

    if not nodes:
        # No recognized top-level nodes — fallback
        return _chunk_file(path)

    # Group small adjacent nodes into chunks, split large nodes
    current_start = nodes[0][0]
    current_end = nodes[0][1]

    for start, end in nodes[1:]:
        node_lines = end - start + 1
        chunk_lines = current_end - current_start + 1

        if chunk_lines + node_lines <= max_chunk_lines:
            # Merge with current chunk
            current_end = end
        else:
            # Flush current chunk
            chunk_content = "\n".join(lines[current_start:current_end + 1])
            non_empty = sum(1 for l in lines[current_start:current_end + 1] if l.strip())
            if non_empty >= 2:
                chunks.append({
                    "file": str(path),
                    "start_line": current_start + 1,
                    "end_line": current_end + 1,
                    "content": chunk_content,
                    "tokens": _tokenize_bm25(chunk_content),
                })
            current_start = start
            current_end = end

    # Flush last chunk
    chunk_content = "\n".join(lines[current_start:current_end + 1])
    non_empty = sum(1 for l in lines[current_start:current_end + 1] if l.strip())
    if non_empty >= 2:
        chunks.append({
            "file": str(path),
            "start_line": current_start + 1,
            "end_line": current_end + 1,
            "content": chunk_content,
            "tokens": _tokenize_bm25(chunk_content),
        })

    # Handle any content between/before/after nodes (imports, comments at top, etc.)
    # by checking gaps > 3 lines
    if chunks and nodes:
        # Content before first node
        if nodes[0][0] > 2:
            preamble = "\n".join(lines[:nodes[0][0]])
            non_empty = sum(1 for l in lines[:nodes[0][0]] if l.strip())
            if non_empty >= 3:
                chunks.insert(0, {
                    "file": str(path),
                    "start_line": 1,
                    "end_line": nodes[0][0],
                    "content": preamble,
                    "tokens": _tokenize_bm25(preamble),
                })

    return chunks if chunks else _chunk_file(path)


# Keys we recognise as generation params (used for both settings.yaml and preset fallback)
_WEBUI_GEN_KEYS = {"temperature", "top_p", "top_k", "min_p", "repetition_penalty",
                    "frequency_penalty", "presence_penalty", "do_sample",
                    "enable_thinking", "mode", "typical_p", "tfs", "top_a",
                    "mirostat_mode", "mirostat_tau", "mirostat_eta",
                    "repetition_penalty_range", "encoder_repetition_penalty",
                    "no_repeat_ngram_size", "penalty_alpha",
                    "dynatemp_low", "dynatemp_high", "dynatemp_exponent",
                    "smoothing_factor", "smoothing_curve",
                    "xtc_threshold", "xtc_probability",
                    "dry_multiplier", "dry_allowed_length", "dry_base",
                    "top_n_sigma", "dynamic_temperature", "temperature_last",
                    "guidance_scale",
                    "seed", "custom_token_bans", "ban_eos_token",
                    "reasoning_effort", "prompt_lookup_num_tokens",
                    "max_tokens_second"}


def _load_config() -> dict[str, Any]:
    """Load config.yaml if it exists."""
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            log.warning("Failed to load config.yaml: %s", e)
    return {}


def _load_webui_settings_from_disk(config: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Load generation params from text-gen-webui's settings.yaml + active preset file.

    Returns (params_dict, preset_name).
    """
    path_str = config.get("webui_settings", "")
    if not path_str:
        return {}, None
    path = Path(os.path.expanduser(path_str))
    if not path.exists():
        log.info("webui settings not found at %s", path)
        return {}, None
    try:
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
    except Exception as e:
        log.warning("Failed to load webui settings: %s", e)
        return {}, None

    preset_name = raw.get("preset")
    params = {k: v for k, v in raw.items() if k in _WEBUI_GEN_KEYS}

    # Load preset file to get the *real* params (settings.yaml is incomplete)
    if preset_name and preset_name not in ("None", ""):
        # Validate preset name against path traversal
        if ".." in preset_name or "/" in preset_name or "\\" in preset_name:
            log.warning("Invalid preset name (path component detected): %s", preset_name)
            return params, preset_name
        webui_root = path.parent.parent  # settings.yaml lives in user_data/
        preset_path = webui_root / "user_data" / "presets" / f"{preset_name}.yaml"
        if not preset_path.exists():
            # Try relative to settings.yaml dir
            preset_path = path.parent / "presets" / f"{preset_name}.yaml"
        if preset_path.exists():
            try:
                with open(preset_path) as f:
                    preset_data = yaml.safe_load(f) or {}
                # Preset values override settings.yaml values
                for k, v in preset_data.items():
                    if k in _WEBUI_GEN_KEYS:
                        params[k] = v
                log.info("Loaded preset '%s' from disk: %s", preset_name, list(preset_data.keys()))
            except Exception as e:
                log.warning("Failed to load preset '%s' from disk: %s", preset_name, e)
        else:
            log.info("Preset file not found: %s", preset_path)

    return params, preset_name


def _load_webui_settings(config: dict[str, Any]) -> dict[str, Any]:
    """Load generation params — try API first, fall back to disk.

    Returns a dict of generation-relevant keys.
    """
    params, preset_name = _load_webui_settings_from_disk(config)
    return params


_webui_preset_name: str | None = None   # name of the active preset


def _reload_config():
    """Reload config and webui settings from disk."""
    global _config, _webui_settings, _webui_preset_name
    _config = _load_config()
    _webui_settings = _load_webui_settings(_config)
    # Also store the preset name for display purposes
    path_str = _config.get("webui_settings", "")
    if path_str:
        path = Path(os.path.expanduser(path_str))
        if path.exists():
            try:
                with open(path) as f:
                    raw = yaml.safe_load(f) or {}
                _webui_preset_name = raw.get("preset")
            except Exception:
                pass
    # Load backend configuration
    _load_backends(_config)

    log.info("Config loaded. preset: %s, webui params: %s, config defaults: %s",
             _webui_preset_name,
             list(_webui_settings.keys()),
             list(_config.get("defaults", {}).keys()))


def _load_backends(config: dict[str, Any]):
    """Load backend configuration from config.yaml."""
    global _backends, _active_backend, TGWUI_BASE, TGWUI_INTERNAL
    backends_cfg = config.get("backends", {})
    if not backends_cfg:
        # No backends configured — use defaults
        _backends = {"local": {"url": TGWUI_BASE, "priority": 1, "optional": False, "healthy": None}}
        _active_backend = "local"
        return

    _backends = {}
    for name, cfg in backends_cfg.items():
        url = cfg.get("url", "")
        if not url:
            continue
        _backends[name] = {
            "url": url,
            "priority": cfg.get("priority", 99),
            "optional": cfg.get("optional", False),
            "healthy": None,  # unknown until checked
        }

    # Set active backend to highest priority
    if _backends:
        _active_backend = min(_backends, key=lambda n: _backends[n]["priority"])
        TGWUI_BASE = _backends[_active_backend]["url"]
        TGWUI_INTERNAL = TGWUI_BASE.rstrip("/") + "/internal"
        log.info("Active backend: %s (%s)", _active_backend, TGWUI_BASE)


async def _check_backend_health(name: str) -> bool:
    """Check if a backend is reachable and has a model loaded."""
    info = _backends.get(name)
    if not info:
        return False
    url = info["url"].rstrip("/") + "/internal/health"
    try:
        resp = await _client.get(url, timeout=3)
        resp.raise_for_status()
        _backends[name]["healthy"] = True
        return True
    except Exception:
        _backends[name]["healthy"] = False
        return False


async def _select_backend() -> str | None:
    """Select the best available backend. Returns backend name or None."""
    global _active_backend, TGWUI_BASE, TGWUI_INTERNAL
    # Sort by priority
    ordered = sorted(_backends.items(), key=lambda x: x[1]["priority"])
    for name, info in ordered:
        if info.get("healthy") is True:
            if name != _active_backend:
                _active_backend = name
                TGWUI_BASE = info["url"]
                TGWUI_INTERNAL = TGWUI_BASE.rstrip("/") + "/internal"
                log.info("Switched to backend: %s (%s)", name, TGWUI_BASE)
            return name
    # Nothing known healthy — try each
    for name, info in ordered:
        if await _check_backend_health(name):
            _active_backend = name
            TGWUI_BASE = info["url"]
            TGWUI_INTERNAL = TGWUI_BASE.rstrip("/") + "/internal"
            log.info("Switched to backend: %s (%s)", name, TGWUI_BASE)
            return name
    return _active_backend  # fall back to whatever was set


async def _fetch_resolved_params_from_api() -> tuple[dict[str, Any], str | None] | None:
    """Try to fetch resolved generation params from the webui API endpoint.

    Returns (params_dict, preset_name) or None if the API is unavailable.
    """
    try:
        resp = await _client.get(f"{TGWUI_INTERNAL}/generation-params", timeout=5)
        resp.raise_for_status()
        data = resp.json()
        preset_name = data.get("preset_name")
        raw_params = data.get("params", {})
        # Filter to known generation keys
        params = {k: v for k, v in raw_params.items() if k in _WEBUI_GEN_KEYS}
        return params, preset_name
    except Exception as e:
        log.debug("Could not fetch params from API (falling back to disk): %s", e)
        return None


async def _reload_webui_params_from_api():
    """Refresh webui settings from the live API, updating the in-memory state."""
    global _webui_settings, _webui_preset_name
    result = await _fetch_resolved_params_from_api()
    if result is not None:
        _webui_settings, _webui_preset_name = result
        log.info("Refreshed webui params from API. preset: %s, keys: %s",
                 _webui_preset_name, list(_webui_settings.keys()))


def get_generation_params(model_name: str | None = None) -> dict[str, Any]:
    """Resolve generation params for the current model.

    Merge order (later wins): webui_settings → config defaults → model match → runtime
    """
    params: dict[str, Any] = {}

    # 1. webui settings baseline
    params.update(_webui_settings)

    # 2. config.yaml defaults
    defaults = _config.get("defaults", {})
    for k, v in defaults.items():
        if k != "system_suffix":  # system_suffix handled separately
            params[k] = v

    # 3. model-specific overrides
    model_overrides = _config.get("models", {})
    if model_name:
        for pattern, overrides in model_overrides.items():
            if pattern in model_name:
                for k, v in overrides.items():
                    if k != "system_suffix":
                        params[k] = v
                break  # first match wins

    # 4. runtime overrides
    for k, v in _runtime_overrides.items():
        if k != "system_suffix":
            params[k] = v

    # Remove keys that are config-only (not sent to the API)
    params.pop("ctx_size", None)
    params.pop("gpu_layers", None)

    return params


def get_system_suffix(model_name: str | None = None) -> str:
    """Get the system_suffix for the current model (merged config)."""
    # Default from config
    suffix = _config.get("defaults", {}).get("system_suffix", "")

    # Model-specific override
    model_overrides = _config.get("models", {})
    if model_name:
        for pattern, overrides in model_overrides.items():
            if pattern in model_name:
                if "system_suffix" in overrides:
                    suffix = overrides["system_suffix"]
                break

    # Runtime override
    if "system_suffix" in _runtime_overrides:
        suffix = _runtime_overrides["system_suffix"]

    return suffix


def _trace_param_source(key: str, value: Any, matched_pattern: str | None) -> str:
    """Determine which layer a resolved param value came from (for display).

    Checks layers in highest-to-lowest priority order so the *winning* layer
    is identified correctly even when multiple layers share the same value.
    """
    # 4. runtime (highest priority)
    if key in _runtime_overrides:
        return "runtime"
    # 3. model override
    if matched_pattern:
        model_overrides = _config.get("models", {}).get(matched_pattern, {})
        if key in model_overrides:
            return f"model:{matched_pattern}"
    # 2. config defaults
    defaults = _config.get("defaults", {})
    if key in defaults:
        return "config"
    # 1. webui/preset (lowest priority among configured sources)
    if key in _webui_settings:
        return "preset" if _webui_preset_name else "webui"
    return "default"


# Load on import
_reload_config()


# ---------------------------------------------------------------------------
# Context system — replaces hardcoded Rust preamble
# ---------------------------------------------------------------------------
PREAMBLE_REGISTRY: dict[str, str] = {
    "rust/quant-platform": (
        "You are reviewing code for the quant-platform project, a high-performance "
        "algorithmic trading system written in Rust.\n\n"
        "Project rules you MUST enforce:\n"
        "- No unwrap() or expect() in runtime code. Use thiserror for library crates, "
        "anyhow::Result with .context() for application code.\n"
        "- CPU-intensive work (Polars, backtesting, ML) MUST use tokio::task::spawn_blocking.\n"
        "- Crate structure: quant-core (types only, no logic) -> quant-feed (I/O), "
        "quant-strategy (pure signals, no I/O), quant-data (storage/backtest/ML), "
        "quant-exec (orders/risk), quant-ui (TUI), quant-chart (web).\n"
        "- Zero clippy warnings: code must pass cargo clippy --workspace -- -D warnings.\n"
        "- Prefer &[Candle] slices over Vec<Candle> for zero-copy paths.\n"
        "- Use compact_str for small string optimizations where applicable."
    ),
}

_context: dict[str, str] = {}  # mutable session context


def get_system_preamble() -> str | None:
    """Build system preamble from current context, character, and mode."""
    parts = []

    # Character system prompt takes priority
    if _current_character and _current_character.get("system_prompt"):
        parts.append(_current_character["system_prompt"].strip())

    # Context-based preamble
    if _context:
        lang = _context.get("language", "")
        project = _context.get("project", "")
        rules = _context.get("rules", "")

        # Check registry for known combos
        key = f"{lang}/{project}".lower().strip("/")
        if key in PREAMBLE_REGISTRY:
            parts.append(PREAMBLE_REGISTRY[key])
        else:
            ctx_parts = []
            if lang:
                ctx_parts.append(f"Language: {lang}")
            if project:
                ctx_parts.append(f"Project: {project}")
            if rules:
                ctx_parts.append(f"Rules:\n{rules}")
            if ctx_parts:
                parts.append("Context for this session:\n" + "\n".join(ctx_parts))

    if not parts:
        return None
    return "\n\n".join(parts)


def _sanitize_topic(raw: str) -> str:
    """Sanitize a note topic to a safe filename component."""
    # Strip whitespace, replace path separators and dots
    topic = raw.strip().replace("/", "-").replace("\\", "-").replace(" ", "-")
    # Remove .. components to prevent path traversal
    topic = re.sub(r'\.{2,}', '.', topic)
    # Remove any remaining non-safe characters
    topic = re.sub(r'[^\w\-.]', '', topic)
    if not topic:
        topic = "unnamed"
    return topic


def _safe_note_path(topic: str) -> Path | None:
    """Return a safe path within NOTES_DIR, or None if traversal detected."""
    path = (NOTES_DIR / f"{topic}.md").resolve()
    if not str(path).startswith(str(NOTES_DIR.resolve())):
        return None
    return path


# ---------------------------------------------------------------------------
# BM25 search engine (inline — no external dependency)
# ---------------------------------------------------------------------------

class _BM25:
    """Okapi BM25 ranking. Built from tokenized documents (lists of strings)."""

    def __init__(self, corpus: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.corpus = corpus
        self.n = len(corpus)
        self.doc_len = [len(doc) for doc in corpus]
        self.avgdl = sum(self.doc_len) / self.n if self.n else 0
        self.df: dict[str, int] = {}
        for doc in corpus:
            for term in set(doc):
                self.df[term] = self.df.get(term, 0) + 1

    def _idf(self, term: str) -> float:
        df = self.df.get(term, 0)
        return math.log((self.n - df + 0.5) / (df + 0.5) + 1)

    def _score_one(self, query_tokens: list[str], doc_idx: int) -> float:
        doc = self.corpus[doc_idx]
        tf = Counter(doc)
        score = 0.0
        dl = self.doc_len[doc_idx]
        for term in query_tokens:
            if term not in tf:
                continue
            f = tf[term]
            idf = self._idf(term)
            num = f * (self.k1 + 1)
            den = f + self.k1 * (1 - self.b + self.b * dl / self.avgdl)
            score += idf * num / den
        return score

    def search(self, query_tokens: list[str], top_k: int = 5) -> list[tuple[int, float]]:
        scores = [(i, self._score_one(query_tokens, i)) for i in range(self.n)]
        scores.sort(key=lambda x: x[1], reverse=True)
        return [(i, s) for i, s in scores[:top_k] if s > 0]


def _tokenize_bm25(text: str) -> list[str]:
    """Simple word tokenizer for BM25: lowercase, split on non-alphanumeric, drop short tokens."""
    return [t for t in re.sub(r'[^a-z0-9_]', ' ', text.lower()).split() if len(t) > 1]


_TEXT_EXTENSIONS = {
    ".py", ".rs", ".ts", ".tsx", ".js", ".jsx", ".go", ".java", ".c", ".cpp",
    ".h", ".hpp", ".rb", ".php", ".swift", ".kt", ".scala", ".sh", ".bash",
    ".zsh", ".fish", ".toml", ".yaml", ".yml", ".json", ".xml", ".html",
    ".css", ".scss", ".md", ".txt", ".rst", ".tex", ".sql", ".r",
    ".lua", ".vim", ".el", ".clj", ".hs", ".ml", ".ex", ".exs", ".erl",
    ".nix", ".tf", ".cfg", ".ini", ".conf", ".lock", ".svg",
}


def _chunk_file(path: Path, chunk_lines: int = 50, overlap: int = 10) -> list[dict[str, Any]]:
    """Chunk a file into overlapping line-based segments."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    lines = content.splitlines()
    if not lines:
        return []

    chunks: list[dict[str, Any]] = []
    start = 0
    while start < len(lines):
        end = min(start + chunk_lines, len(lines))
        chunk_content = "\n".join(lines[start:end])
        non_empty = sum(1 for l in lines[start:end] if l.strip())
        if non_empty >= 3:
            chunks.append({
                "file": str(path),
                "start_line": start + 1,
                "end_line": end,
                "content": chunk_content,
                "tokens": _tokenize_bm25(chunk_content),
            })
        start += chunk_lines - overlap
        if start >= len(lines):
            break

    return chunks


# In-memory index cache: name -> {meta, chunks, bm25}
_index_cache: dict[str, dict[str, Any]] = {}


def _save_index(
    name: str,
    meta: dict,
    chunks: list[dict],
    embeddings: list[list[float]] | None = None,
    sparse_embeddings: list[dict] | None = None,
    colbert_embeddings: list[list[list[float]]] | None = None,
) -> Path:
    """Save an index to disk. Optionally saves dense, sparse, and ColBERT vectors."""
    index_dir = INDEXES_DIR / name
    index_dir.mkdir(parents=True, exist_ok=True)
    with open(index_dir / "meta.json", "w") as f:
        json.dump(meta, f, indent=2)
    save_chunks = [{k: v for k, v in c.items() if k != "tokens"} for c in chunks]
    with open(index_dir / "chunks.json", "w") as f:
        json.dump(save_chunks, f)
    if embeddings is not None:
        with open(index_dir / "vectors.json", "w") as f:
            json.dump(embeddings, f)
    if sparse_embeddings is not None:
        with open(index_dir / "sparse_vectors.json", "w") as f:
            json.dump(sparse_embeddings, f)
    if colbert_embeddings is not None:
        with open(index_dir / "colbert_vectors.json", "w") as f:
            json.dump(colbert_embeddings, f)
    return index_dir


def _load_index(name: str) -> dict[str, Any] | None:
    """Load an index from disk into cache. Returns the cache entry or None."""
    if name in _index_cache:
        return _index_cache[name]
    index_dir = INDEXES_DIR / name
    meta_path = index_dir / "meta.json"
    chunks_path = index_dir / "chunks.json"
    vectors_path = index_dir / "vectors.json"
    if not meta_path.exists() or not chunks_path.exists():
        return None
    try:
        with open(meta_path) as f:
            meta = json.load(f)
        with open(chunks_path) as f:
            chunks = json.load(f)
    except Exception:
        return None
    for chunk in chunks:
        chunk["tokens"] = _tokenize_bm25(chunk["content"])
    corpus = [c["tokens"] for c in chunks]
    bm25 = _BM25(corpus) if corpus else None
    # Load vector types if available
    embeddings = None
    sparse_embeddings = None
    colbert_embeddings = None
    if vectors_path.exists():
        try:
            with open(vectors_path) as f:
                embeddings = json.load(f)
        except Exception:
            log.warning("Failed to load dense vectors for index '%s'", name)
    sparse_path = index_dir / "sparse_vectors.json"
    if sparse_path.exists():
        try:
            with open(sparse_path) as f:
                sparse_embeddings = json.load(f)
        except Exception:
            log.warning("Failed to load sparse vectors for index '%s'", name)
    colbert_path = index_dir / "colbert_vectors.json"
    if colbert_path.exists():
        try:
            with open(colbert_path) as f:
                colbert_embeddings = json.load(f)
        except Exception:
            log.warning("Failed to load ColBERT vectors for index '%s'", name)
    entry = {
        "meta": meta, "chunks": chunks, "bm25": bm25,
        "embeddings": embeddings, "sparse_embeddings": sparse_embeddings,
        "colbert_embeddings": colbert_embeddings,
    }
    _index_cache[name] = entry
    return entry


# ---------------------------------------------------------------------------
# Built-in GBNF grammars for constrained generation
# ---------------------------------------------------------------------------

_BUILTIN_GRAMMARS: dict[str, str] = {
    "json": r'''root   ::= object
value  ::= object | array | string | number | ("true" | "false" | "null") ws

object ::=
  "{" ws (
            string ":" ws value
    ("," ws string ":" ws value)*
  )? "}" ws

array  ::=
  "[" ws (
            value
    ("," ws value)*
  )? "]" ws

string ::=
  "\"" (
    [^\\"\x7F\x00-\x1F] |
    "\\" (["\\/bfnrt] | "u" [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F])
  )* "\"" ws

number ::= ("-"? ([0-9] | [1-9] [0-9]*)) ("." [0-9]+)? (("e" | "E") ("+" | "-")? [0-9]+)? ws

ws ::= ([ \t\n] ws)?
''',
    "json_array": r'''root ::= "[" ws (value ("," ws value)*)? "]" ws
value  ::= object | array | string | number | ("true" | "false" | "null") ws
object ::= "{" ws (string ":" ws value ("," ws string ":" ws value)*)? "}" ws
array  ::= "[" ws (value ("," ws value)*)? "]" ws
string ::= "\"" ([^\\"\x7F\x00-\x1F] | "\\" (["\\/bfnrt] | "u" [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F]))* "\"" ws
number ::= ("-"? ([0-9] | [1-9] [0-9]*)) ("." [0-9]+)? (("e" | "E") ("+" | "-")? [0-9]+)? ws
ws ::= ([ \t\n] ws)?
''',
    "boolean": r'''root ::= ("true" | "false")''',
}


# ---------------------------------------------------------------------------
# Connection pool + retry
# ---------------------------------------------------------------------------
_client = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=10, read=120, write=10, pool=10),
    limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
)


async def resolve_model() -> str:
    """Get the currently loaded model name from text-gen-webui's internal API."""
    log.info("Resolving model from %s/model/info", TGWUI_INTERNAL)
    resp = await _client.get(f"{TGWUI_INTERNAL}/model/info", timeout=5)
    resp.raise_for_status()
    info = resp.json()
    model_name = info.get("model_name", "")
    if not model_name or model_name == "None":
        raise RuntimeError("No model loaded in text-generation-webui")
    log.info("Resolved model: %s", model_name)
    return model_name


async def _chat_to_backend(base_url: str, body: dict[str, Any]) -> str:
    """Send a chat completion to a specific backend URL. Returns response text."""
    resp = await _client.post(f"{base_url}/chat/completions", json=body)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type((httpx.ConnectError, httpx.ReadTimeout)),
    reraise=True,
)
async def chat(prompt: str, system: str | None = None, **kwargs: Any) -> str:
    """Send a chat completion request with retry, caching, and backend fallback."""
    global MODEL
    if MODEL is None:
        MODEL = await resolve_model()

    # Cache check — pop use_cache before it reaches gen_params
    use_cache = kwargs.pop("use_cache", True)
    cache_k = None
    if use_cache:
        cache_k = _cache_key(prompt, system, MODEL, **kwargs)
        cached = _cache_get(cache_k)
        if cached is not None:
            log.debug("Cache hit (prompt_len=%d)", len(prompt))
            return cached

    # Track session stats
    _session_stats["total_calls"] += 1
    _session_stats["total_tokens_in_approx"] += len(prompt) // 4  # rough estimate

    # Build system message: preamble + system_suffix
    suffix = get_system_suffix(MODEL)
    effective_system = system
    if suffix:
        if effective_system:
            effective_system = f"{effective_system}\n\n{suffix}"
        else:
            effective_system = suffix

    messages: list[dict[str, str]] = []
    if effective_system:
        messages.append({"role": "system", "content": effective_system})
    messages.append({"role": "user", "content": prompt})

    # Merge generation params: config → kwargs (caller overrides win)
    gen_params = get_generation_params(MODEL)
    gen_params.update(kwargs)

    body: dict[str, Any] = {
        "model": MODEL,
        "messages": messages,
        "stream": False,
        **gen_params,
    }

    log.debug("Chat request: model=%s, prompt_len=%d", MODEL, len(prompt))

    # Try primary backend
    try:
        result = await _chat_to_backend(TGWUI_BASE, body)
        log.debug("Chat response: len=%d", len(result))
        _session_stats["total_tokens_out_approx"] += len(result) // 4
        if cache_k:
            _cache_put(cache_k, result)
        return result
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (400, 404):
            log.warning("HTTP %d — clearing stale MODEL for re-resolution", e.response.status_code)
            MODEL = None
        raise
    except (httpx.ConnectError, httpx.ReadTimeout) as primary_err:
        # Try fallback backends
        if len(_backends) <= 1:
            raise
        _backends.get(_active_backend, {})["healthy"] = False
        ordered = sorted(_backends.items(), key=lambda x: x[1]["priority"])
        for name, info in ordered:
            if name == _active_backend:
                continue
            fallback_url = info["url"]
            log.info("Primary backend failed, trying fallback: %s (%s)", name, fallback_url)
            try:
                result = await _chat_to_backend(fallback_url, body)
                log.info("Fallback %s succeeded (len=%d)", name, len(result))
                info["healthy"] = True
                _session_stats["total_tokens_out_approx"] += len(result) // 4
                if cache_k:
                    _cache_put(cache_k, result)
                return result
            except Exception as fallback_err:
                log.warning("Fallback %s also failed: %s", name, fallback_err)
                info["healthy"] = False
        # All backends failed — raise the original error
        raise primary_err


# ---------------------------------------------------------------------------
# Tool handler registry
# ---------------------------------------------------------------------------
_tool_definitions: list[Tool] = []
_tool_handlers: dict[str, Callable[..., Awaitable[str]]] = {}


def tool_handler(
    name: str,
    description: str,
    schema: dict[str, Any],
):
    """Decorator to register a tool with its definition and handler."""
    def decorator(fn: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
        _tool_definitions.append(Tool(name=name, description=description, inputSchema=schema))
        _tool_handlers[name] = fn

        @wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> str:
            return await fn(*args, **kwargs)
        return wrapper
    return decorator


def error_response(msg: str) -> list[TextContent]:
    log.error(msg)
    return [TextContent(type="text", text=f"Error: {msg}")]


# ---------------------------------------------------------------------------
# MCP app
# ---------------------------------------------------------------------------
app = Server("local-model")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return _tool_definitions


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    handler = _tool_handlers.get(name)
    if handler is None:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]
    # Track tool usage
    _session_stats["tool_calls"][name] = _session_stats["tool_calls"].get(name, 0) + 1
    try:
        result = await handler(arguments)
    except httpx.ConnectError:
        return error_response(
            f"Cannot connect to text-generation-webui at {TGWUI_BASE}. "
            f"Is it running? Check your backend configuration in config.yaml."
        )
    except httpx.HTTPStatusError as e:
        return error_response(f"HTTP error from text-generation-webui: {e}")
    except httpx.ReadTimeout:
        return error_response("Request timed out (120s). The model may be overloaded or the prompt too long.")
    except (KeyError, IndexError) as e:
        return error_response(f"Unexpected response format: {e}")
    except Exception as e:
        return error_response(f"Unexpected error: {type(e).__name__}: {e}")
    return [TextContent(type="text", text=result)]


# ===========================================================================
# Tool definitions (25 total)
# ===========================================================================

# Session scratchpad (in-memory, resets on server restart)
_scratchpad: dict[str, str] = {}

# Multi-turn conversation state (in-memory, resets on server restart)
_conversations: dict[str, list[dict[str, str]]] = {}
MAX_CONVERSATION_TURNS = 20

# --- Context tools ---

@tool_handler(
    name="set_context",
    description=(
        "Configure the project context for all subsequent tool calls. "
        "Set language/project/rules to get tailored responses, or call with no arguments to reset to generic mode. "
        "Known combos (e.g. language='rust', project='quant-platform') auto-activate specialized preambles."
    ),
    schema={
        "type": "object",
        "properties": {
            "language": {"type": "string", "description": "Programming language (e.g. 'rust', 'python', 'typescript')"},
            "project": {"type": "string", "description": "Project name for known preamble lookup (e.g. 'quant-platform')"},
            "rules": {"type": "string", "description": "Freeform project rules/conventions to enforce"},
        },
        "required": [],
    },
)
async def set_context(args: dict) -> str:
    _context.clear()
    if args.get("language"):
        _context["language"] = args["language"]
    if args.get("project"):
        _context["project"] = args["project"]
    if args.get("rules"):
        _context["rules"] = args["rules"]

    preamble = get_system_preamble()
    if preamble:
        return f"Context set. Active preamble:\n\n{preamble}"
    return "Context cleared. Operating in generic mode (no language/project-specific rules)."


# --- Hub mode and character state ---
_current_mode: dict = {}  # {"name": "development", ...config}
_current_character: dict = {}  # {"name": "code-reviewer", ...config}


@tool_handler(
    name="set_mode",
    description=(
        "Switch the hub's operational mode. Each mode configures temperature, "
        "preferred model, system suffix, and max tokens. Available modes: "
        "development, research, creative, review, ops, learning. "
        "Use get_mode() to see current mode."
    ),
    schema={
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["development", "research", "creative", "review", "ops", "learning"],
                "description": "Mode to activate",
            },
        },
        "required": ["mode"],
    },
)
async def set_mode(args: dict) -> str:
    global _current_mode
    mode_name = args["mode"]
    modes = _config.get("modes", {})
    if mode_name not in modes:
        return f"Unknown mode: {mode_name}. Available: {', '.join(modes.keys())}"

    mode_cfg = modes[mode_name]
    _current_mode = {"name": mode_name, **mode_cfg}

    # Apply generation param overrides
    if mode_cfg.get("temperature") is not None:
        _runtime_overrides["temperature"] = mode_cfg["temperature"]
    if mode_cfg.get("max_tokens"):
        _runtime_overrides["max_tokens"] = mode_cfg["max_tokens"]
    if mode_cfg.get("system_suffix") is not None:
        _runtime_overrides["system_suffix"] = mode_cfg["system_suffix"]

    # Auto-swap model if configured
    swap_msg = ""
    if mode_cfg.get("auto_swap") and mode_cfg.get("prefer_model"):
        # Check if current model matches any preferred model
        current = MODEL or ""
        preferred = mode_cfg["prefer_model"]
        if not any(p.lower() in current.lower() for p in preferred):
            swap_msg = f"\nPreferred model: {preferred[0]} (use swap_model to load it)"

    return (
        f"Mode: {mode_name}\n"
        f"Temperature: {mode_cfg.get('temperature', 'default')}\n"
        f"Max tokens: {mode_cfg.get('max_tokens', 'default')}\n"
        f"System suffix: {mode_cfg.get('system_suffix', '(default)')[:60]}..."
        f"{swap_msg}"
    )


@tool_handler(
    name="get_mode",
    description="Show the current hub mode and its settings.",
    schema={"type": "object", "properties": {}, "required": []},
)
async def get_mode(args: dict) -> str:
    if not _current_mode:
        return "No mode set. Using default settings. Available modes: " + \
               ", ".join(_config.get("modes", {}).keys())
    char_info = ""
    if _current_character:
        char_info = f"\nCharacter: {_current_character.get('name', 'default')}"
    return (
        f"Current mode: {_current_mode.get('name', 'unknown')}\n"
        f"Temperature: {_current_mode.get('temperature', 'default')}\n"
        f"Max tokens: {_current_mode.get('max_tokens', 'default')}\n"
        f"Preferred model: {_current_mode.get('prefer_model', ['any'])}\n"
        f"System suffix: {_current_mode.get('system_suffix', '(none)')[:80]}"
        f"{char_info}"
    )


@tool_handler(
    name="set_character",
    description=(
        "Set an agent character/persona that applies to all chat interactions. "
        "Characters provide a system prompt that shapes the model's behavior. "
        "Combinable with modes. Available: default, code-reviewer, architect, "
        "brainstorm, teacher, devops, security."
    ),
    schema={
        "type": "object",
        "properties": {
            "character": {
                "type": "string",
                "description": "Character name to activate",
            },
        },
        "required": ["character"],
    },
)
async def set_character(args: dict) -> str:
    global _current_character
    char_name = args["character"]
    characters = _config.get("characters", {})
    if char_name not in characters:
        return f"Unknown character: {char_name}. Available: {', '.join(characters.keys())}"

    char_cfg = characters[char_name]
    _current_character = {"name": char_name, **char_cfg}

    # Apply temperature override if character defines one
    if char_cfg.get("temperature_override") is not None:
        _runtime_overrides["temperature"] = char_cfg["temperature_override"]

    prompt_preview = char_cfg.get("system_prompt", "(none)")[:100]
    return f"Character: {char_cfg.get('name', char_name)}\nSystem prompt: {prompt_preview}..."


@tool_handler(
    name="list_characters",
    description="List all available agent characters/personas.",
    schema={"type": "object", "properties": {}, "required": []},
)
async def list_characters(args: dict) -> str:
    characters = _config.get("characters", {})
    if not characters:
        return "No characters configured in config.yaml."
    parts = []
    for key, cfg in characters.items():
        active = " (active)" if _current_character.get("name") == key else ""
        temp = f", temp={cfg['temperature_override']}" if cfg.get("temperature_override") else ""
        parts.append(f"  {key}: {cfg.get('name', key)}{temp}{active}")
    return "Available characters:\n" + "\n".join(parts)


@tool_handler(
    name="list_modes",
    description="List all available hub modes.",
    schema={"type": "object", "properties": {}, "required": []},
)
async def list_modes(args: dict) -> str:
    modes = _config.get("modes", {})
    if not modes:
        return "No modes configured in config.yaml."
    parts = []
    for key, cfg in modes.items():
        active = " (active)" if _current_mode.get("name") == key else ""
        parts.append(
            f"  {key}: temp={cfg.get('temperature', '?')}, "
            f"model={cfg.get('prefer_model', ['any'])[0]}, "
            f"max_tokens={cfg.get('max_tokens', '?')}{active}"
        )
    return "Available modes:\n" + "\n".join(parts)


@tool_handler(
    name="check_model",
    description=(
        "Check which model is currently loaded in text-generation-webui and verify connectivity. "
        "Also re-resolves the model name, so call this after swapping models in the webUI."
    ),
    schema={"type": "object", "properties": {}, "required": []},
)
async def check_model(args: dict) -> str:
    global MODEL
    MODEL = None
    MODEL = await resolve_model()
    preamble = get_system_preamble()
    ctx_status = "Active context preamble set" if preamble else "Generic mode (no context set)"
    return f"Connected. Model: {MODEL}\nEndpoint: {TGWUI_BASE}\nContext: {ctx_status}"


# --- Config tools ---

@tool_handler(
    name="get_generation_params",
    description=(
        "Show the current generation parameters (temperature, max_tokens, top_p, etc.) "
        "and system_suffix for the loaded model. Shows the full merge chain: "
        "webui settings → config defaults → model overrides → runtime overrides."
    ),
    schema={"type": "object", "properties": {}, "required": []},
)
async def get_generation_params_tool(args: dict) -> str:
    global MODEL
    if MODEL is None:
        try:
            MODEL = await resolve_model()
        except Exception:
            pass

    # Try to refresh from API for the freshest values
    await _reload_webui_params_from_api()

    lines = [f"Model: {MODEL or '(none loaded)'}"]

    # Show preset info
    if _webui_preset_name:
        lines.append(f"Active preset: {_webui_preset_name}")

    # Show each layer with source tracking
    lines.append("\n--- webui preset/settings ---")
    if _webui_settings:
        # Only show non-default values for readability
        for k, v in sorted(_webui_settings.items()):
            lines.append(f"  {k}: {v}")
    else:
        lines.append("  (empty or not found)")

    lines.append("\n--- config.yaml defaults ---")
    defaults = _config.get("defaults", {})
    if defaults:
        for k, v in sorted(defaults.items()):
            lines.append(f"  {k}: {v}")
    else:
        lines.append("  (none)")

    # Model match
    matched_pattern = None
    model_overrides = _config.get("models", {})
    if MODEL:
        for pattern, overrides in model_overrides.items():
            if pattern in MODEL:
                matched_pattern = pattern
                lines.append(f"\n--- model override (matched: '{pattern}') ---")
                for k, v in sorted(overrides.items()):
                    lines.append(f"  {k}: {v}")
                break
    if not matched_pattern:
        lines.append("\n--- model override ---")
        lines.append("  (no match)")

    if _runtime_overrides:
        lines.append("\n--- runtime overrides ---")
        for k, v in sorted(_runtime_overrides.items()):
            lines.append(f"  {k}: {v}")

    # Final resolved with source annotations
    resolved = get_generation_params(MODEL)
    suffix = get_system_suffix(MODEL)
    lines.append("\n--- resolved (sent to model) ---")
    for k, v in sorted(resolved.items()):
        # Annotate where each value came from
        source = _trace_param_source(k, v, matched_pattern)
        lines.append(f"  {k}: {v}  ({source})")
    lines.append(f"  system_suffix: {suffix!r}")

    return "\n".join(lines)


@tool_handler(
    name="set_generation_params",
    description=(
        "Override generation parameters at runtime. These take highest priority and "
        "persist until cleared or the MCP server restarts. "
        "Set a key to null/empty to clear that override. "
        "Supports: temperature, max_tokens, top_p, top_k, min_p, "
        "repetition_penalty, enable_thinking, mode, system_suffix. "
        "Call with no arguments to clear all runtime overrides."
    ),
    schema={
        "type": "object",
        "properties": {
            "temperature": {"type": "number", "description": "Sampling temperature (0.0-2.0)"},
            "max_tokens": {"type": "integer", "description": "Max output tokens"},
            "top_p": {"type": "number", "description": "Nucleus sampling threshold"},
            "top_k": {"type": "integer", "description": "Top-k sampling"},
            "min_p": {"type": "number", "description": "Min-p sampling threshold"},
            "repetition_penalty": {"type": "number", "description": "Repetition penalty (1.0 = none)"},
            "enable_thinking": {"type": "boolean", "description": "Enable model thinking/reasoning (true/false). Controls <think> block generation."},
            "mode": {"type": "string", "description": "Chat mode: 'instruct', 'chat', or 'chat-instruct'"},
            "system_suffix": {"type": "string", "description": "System instruction appended to all calls"},
        },
        "required": [],
    },
)
async def set_generation_params_tool(args: dict) -> str:
    if not args:
        _runtime_overrides.clear()
        return "All runtime overrides cleared. Falling back to config.yaml + webui settings."

    allowed = {"temperature", "max_tokens", "top_p", "top_k", "min_p",
               "repetition_penalty", "enable_thinking", "mode", "system_suffix",
               "seed", "custom_token_bans", "ban_eos_token", "reasoning_effort",
               "prompt_lookup_num_tokens", "max_tokens_second"}
    changed = []
    for k, v in args.items():
        if k not in allowed:
            continue
        if v is None or v == "":
            _runtime_overrides.pop(k, None)
            changed.append(f"  {k}: (cleared)")
        else:
            _runtime_overrides[k] = v
            changed.append(f"  {k}: {v}")

    if not changed:
        return "No valid parameters provided."

    return "Runtime overrides updated:\n" + "\n".join(changed)


@tool_handler(
    name="reload_config",
    description=(
        "Reload config.yaml and webui settings.yaml from disk. "
        "Use after editing config.yaml or changing webui settings. "
        "Does NOT clear runtime overrides (use set_generation_params with no args for that)."
    ),
    schema={"type": "object", "properties": {}, "required": []},
)
async def reload_config_tool(args: dict) -> str:
    _reload_config()
    # Try to refresh from the live API (overrides disk values if available)
    await _reload_webui_params_from_api()
    defaults = _config.get("defaults", {})
    model_count = len(_config.get("models", {}))
    webui_count = len(_webui_settings)
    preset_info = f"  preset: {_webui_preset_name or '(none)'}\n" if _webui_preset_name else ""
    return (
        f"Config reloaded.\n"
        f"{preset_info}"
        f"  webui params: {webui_count} keys\n"
        f"  config defaults: {list(defaults.keys())}\n"
        f"  model profiles: {model_count}\n"
        f"  runtime overrides: {len(_runtime_overrides)} (unchanged)"
    )


# --- Existing analysis tools (updated for any language) ---

@tool_handler(
    name="analyze_code",
    description="Analyze a code snippet for issues, patterns, or improvements using the local model",
    schema={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Source code to analyze"},
            "query": {"type": "string", "description": "What to look for (e.g. 'error handling gaps', 'performance issues')"},
            "language": {"type": "string", "description": "Language hint (optional, overrides context)"},
        },
        "required": ["code", "query"],
    },
)
async def analyze_code(args: dict) -> str:
    lang = args.get("language", _context.get("language", ""))
    lang_hint = f" ({lang})" if lang else ""
    prompt = (
        f"Analyze the following code{lang_hint} for: {args['query']}\n\n"
        f"Be concise. List specific line numbers and issues.\n\n"
        f"```\n{args['code']}\n```"
    )
    return await chat(prompt, system=get_system_preamble())


@tool_handler(
    name="batch_review",
    description="Review multiple code snippets for a consistent concern. Results returned together.",
    schema={
        "type": "object",
        "properties": {
            "snippets": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of code snippets to review",
            },
            "concern": {"type": "string", "description": "What to check each snippet for"},
        },
        "required": ["snippets", "concern"],
    },
)
async def batch_review(args: dict) -> str:
    snippets = args["snippets"]
    concern = args["concern"]

    # Find healthy backends for parallel dispatch
    healthy_backends: list[str] = []
    for name in sorted(_backends, key=lambda n: _backends[n]["priority"]):
        if await _check_backend_health(name):
            healthy_backends.append(name)

    # If only one backend (or one snippet), use the simple path
    if len(healthy_backends) <= 1 or len(snippets) <= 1:
        numbered = "\n\n---\n\n".join(
            f"Snippet {i+1}:\n```\n{s}\n```" for i, s in enumerate(snippets)
        )
        prompt = (
            f"For each snippet below, check for: {concern}\n"
            f"Label each response 'Snippet N:' and be concise.\n\n{numbered}"
        )
        return await chat(prompt, system=get_system_preamble())

    # Split snippets across backends and dispatch in parallel
    log.info("Parallel batch_review: %d snippets across %d backends", len(snippets), len(healthy_backends))
    n_backends = len(healthy_backends)
    chunks: list[list[tuple[int, str]]] = [[] for _ in range(n_backends)]
    for i, snippet in enumerate(snippets):
        chunks[i % n_backends].append((i, snippet))

    preamble = get_system_preamble()
    suffix = get_system_suffix(MODEL)
    gen_params = get_generation_params(MODEL)

    async def _review_chunk(backend_name: str, chunk: list[tuple[int, str]]) -> list[tuple[int, str]]:
        """Review a chunk of snippets on a specific backend."""
        numbered = "\n\n---\n\n".join(
            f"Snippet {idx+1}:\n```\n{s}\n```" for idx, s in chunk
        )
        prompt = (
            f"For each snippet below, check for: {concern}\n"
            f"Label each response 'Snippet N:' and be concise.\n\n{numbered}"
        )
        effective_system = preamble
        if suffix:
            effective_system = f"{preamble}\n\n{suffix}" if preamble else suffix

        messages: list[dict[str, str]] = []
        if effective_system:
            messages.append({"role": "system", "content": effective_system})
        messages.append({"role": "user", "content": prompt})

        body = {"model": MODEL or "", "messages": messages, "stream": False, **gen_params}
        url = _backends[backend_name]["url"]
        try:
            result = await _chat_to_backend(url, body)
            return [(chunk[0][0], result)]  # return with first snippet index for ordering
        except Exception as e:
            log.warning("Backend %s failed during batch_review: %s", backend_name, e)
            return [(chunk[0][0], f"(Backend {backend_name} failed: {e})")]

    # Dispatch all chunks in parallel
    tasks = [
        _review_chunk(healthy_backends[i], chunk)
        for i, chunk in enumerate(chunks)
        if chunk  # skip empty chunks
    ]
    results = await asyncio.gather(*tasks)

    # Merge results in order
    all_results = []
    for result_list in results:
        all_results.extend(result_list)
    all_results.sort(key=lambda x: x[0])

    return "\n\n".join(text for _, text in all_results)


@tool_handler(
    name="generate_test_stubs",
    description="Generate test stub functions for the public API in the given source code",
    schema={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Source code to generate test stubs for"},
            "language": {"type": "string", "description": "Language hint (optional, overrides context)"},
            "module_name": {"type": "string", "description": "Module/crate name for imports (optional)"},
        },
        "required": ["code"],
    },
)
async def generate_test_stubs(args: dict) -> str:
    lang = args.get("language", _context.get("language", ""))
    module = args.get("module_name", "")
    prompt = (
        f"Generate test stub functions for the public API in this {lang or 'code'}. "
        f"Use the idiomatic test framework for the language. "
        f"Stubs should have empty/todo bodies.\n"
    )
    if module:
        prompt += f"Module/crate name for imports: {module}\n"
    prompt += f"\n```\n{args['code']}\n```\n\nOutput only the test code."
    return await chat(prompt, system=get_system_preamble())


@tool_handler(
    name="explain_error",
    description="Explain a compiler/linter/runtime error in plain English and suggest a fix",
    schema={
        "type": "object",
        "properties": {
            "error": {"type": "string", "description": "The error message"},
            "context": {"type": "string", "description": "Optional surrounding source code for context"},
        },
        "required": ["error"],
    },
)
async def explain_error(args: dict) -> str:
    context_block = ""
    if args.get("context"):
        context_block = f"\n\nRelevant code:\n```\n{args['context']}\n```"
    prompt = (
        f"Explain this error in plain English, then suggest a concrete fix.\n\n"
        f"Error:\n```\n{args['error']}\n```{context_block}\n\n"
        f"Format: 1) What it means  2) Why it happens  3) How to fix it (with code)"
    )
    return await chat(prompt, system=get_system_preamble())


@tool_handler(
    name="suggest_refactor",
    description="Given code and a refactoring goal, return a refactored version",
    schema={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Source code to refactor"},
            "goal": {"type": "string", "description": "Refactoring goal (e.g. 'extract method', 'improve error handling')"},
        },
        "required": ["code", "goal"],
    },
)
async def suggest_refactor(args: dict) -> str:
    prompt = (
        f"Refactor the following code to achieve this goal: {args['goal']}\n\n"
        f"```\n{args['code']}\n```\n\n"
        f"Output the refactored code with brief comments explaining changes."
    )
    return await chat(prompt, system=get_system_preamble())


@tool_handler(
    name="summarize_file",
    description="Generate a structural summary of a source file: types, functions, signatures, etc.",
    schema={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Source code to summarize"},
            "file_path": {"type": "string", "description": "Optional file path for context"},
        },
        "required": ["code"],
    },
)
async def summarize_file(args: dict) -> str:
    path_note = f" ({args['file_path']})" if args.get("file_path") else ""
    prompt = (
        f"Summarize the structure of this source file{path_note}. List:\n"
        f"- Public types (structs/classes/enums) with field counts\n"
        f"- Interfaces/traits and their methods\n"
        f"- Public methods and functions (with signatures)\n"
        f"- Notable constants, type aliases, or exports\n\n"
        f"Be concise. Use a structured format.\n\n"
        f"```\n{args['code']}\n```"
    )
    return await chat(prompt, system=get_system_preamble())


# --- New general-purpose tools ---

@tool_handler(
    name="local_chat",
    description=(
        "Freeform prompt to the local model — brainstorming, explanations, Q&A, anything. "
        "Uses the current context if set. Supports optional GBNF grammar constraints."
    ),
    schema={
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Your prompt or question"},
            "system": {"type": "string", "description": "Optional system message override (replaces context preamble)"},
            "grammar": {"type": "string", "description": "Optional GBNF grammar constraint. Built-in: 'json', 'json_array', 'boolean'. Or provide a custom GBNF string."},
        },
        "required": ["prompt"],
    },
)
async def local_chat(args: dict) -> str:
    system = args.get("system") or get_system_preamble()
    kwargs: dict[str, Any] = {}
    grammar = args.get("grammar")
    if grammar:
        kwargs["grammar_string"] = _BUILTIN_GRAMMARS.get(grammar, grammar)
    return await chat(args["prompt"], system=system, **kwargs)


@tool_handler(
    name="review_diff",
    description="Review a git diff for bugs, security issues, and style problems (any language)",
    schema={
        "type": "object",
        "properties": {
            "diff": {"type": "string", "description": "Git diff output to review"},
            "focus": {"type": "string", "description": "Optional focus area (e.g. 'security', 'performance', 'correctness')"},
        },
        "required": ["diff"],
    },
)
async def review_diff(args: dict) -> str:
    focus = args.get("focus", "bugs, security issues, and style problems")
    prompt = (
        f"Review this git diff for: {focus}\n\n"
        f"For each issue found, state:\n"
        f"- File and line (from the diff)\n"
        f"- Severity (critical / warning / nit)\n"
        f"- What's wrong and how to fix it\n\n"
        f"If the diff looks clean, say so.\n\n"
        f"```diff\n{args['diff']}\n```"
    )
    return await chat(prompt, system=get_system_preamble())


@tool_handler(
    name="draft_commit_message",
    description="Generate a conventional commit message from a git diff",
    schema={
        "type": "object",
        "properties": {
            "diff": {"type": "string", "description": "Git diff output"},
            "style": {"type": "string", "description": "Commit style (default: 'conventional')"},
        },
        "required": ["diff"],
    },
)
async def draft_commit_message(args: dict) -> str:
    style = args.get("style", "conventional")
    prompt = (
        f"Generate a {style} commit message for this diff.\n\n"
        f"Rules:\n"
        f"- First line: type(scope): description (max 72 chars)\n"
        f"- Types: feat, fix, refactor, docs, test, chore, perf, style, ci\n"
        f"- Body: brief explanation of why, not what (the diff shows what)\n"
        f"- Output ONLY the commit message, no extra commentary\n\n"
        f"```diff\n{args['diff']}\n```"
    )
    return await chat(prompt, system=get_system_preamble())


@tool_handler(
    name="draft_docs",
    description="Generate documentation: doc comments, README sections, or API docs",
    schema={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Source code to document"},
            "style": {"type": "string", "description": "Doc style: 'inline' (doc comments), 'readme' (README section), 'api' (API reference)"},
            "language": {"type": "string", "description": "Language hint (optional, overrides context)"},
        },
        "required": ["code"],
    },
)
async def draft_docs(args: dict) -> str:
    lang = args.get("language", _context.get("language", ""))
    style = args.get("style", "inline")
    style_map = {
        "inline": "Add idiomatic doc comments to each public item. Output the code with docs added.",
        "readme": "Write a README section documenting the public API. Use markdown.",
        "api": "Write API reference documentation covering all public items, parameters, return types, and examples.",
    }
    instruction = style_map.get(style, style_map["inline"])
    prompt = (
        f"{instruction}\n\n"
        f"Language: {lang or 'auto-detect'}\n\n"
        f"```\n{args['code']}\n```"
    )
    return await chat(prompt, system=get_system_preamble())


@tool_handler(
    name="translate_code",
    description="Translate code between programming languages idiomatically",
    schema={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Source code to translate"},
            "from_lang": {"type": "string", "description": "Source language (optional, auto-detect if omitted)"},
            "to_lang": {"type": "string", "description": "Target language"},
        },
        "required": ["code", "to_lang"],
    },
)
async def translate_code(args: dict) -> str:
    from_lang = args.get("from_lang", "auto-detect")
    prompt = (
        f"Translate this code from {from_lang} to {args['to_lang']}.\n\n"
        f"Requirements:\n"
        f"- Use idiomatic patterns for the target language\n"
        f"- Preserve the logic and behavior\n"
        f"- Add brief comments where the translation is non-obvious\n\n"
        f"```\n{args['code']}\n```\n\n"
        f"Output only the translated code."
    )
    return await chat(prompt, system=get_system_preamble())


@tool_handler(
    name="generate_regex",
    description="Generate and explain a regex pattern from a natural language description",
    schema={
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "Natural language description of what to match"},
            "flavor": {"type": "string", "description": "Regex flavor: 'pcre', 'python', 'javascript', 'rust' (default: 'pcre')"},
            "examples": {"type": "string", "description": "Optional example strings that should/shouldn't match"},
        },
        "required": ["description"],
    },
)
async def generate_regex(args: dict) -> str:
    flavor = args.get("flavor", "pcre")
    examples_block = ""
    if args.get("examples"):
        examples_block = f"\n\nExamples:\n{args['examples']}"
    prompt = (
        f"Generate a {flavor} regex that matches: {args['description']}{examples_block}\n\n"
        f"Output:\n"
        f"1. The regex pattern\n"
        f"2. A breakdown explaining each part\n"
        f"3. Edge cases to watch out for"
    )
    return await chat(prompt)


@tool_handler(
    name="optimize_query",
    description="Optimize a SQL, Polars, or DuckDB query for performance",
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The query to optimize"},
            "engine": {"type": "string", "description": "Query engine: 'sql', 'polars', 'duckdb' (default: auto-detect)"},
            "context": {"type": "string", "description": "Optional schema/table info or performance constraints"},
        },
        "required": ["query"],
    },
)
async def optimize_query(args: dict) -> str:
    engine = args.get("engine", "auto-detect")
    context_block = ""
    if args.get("context"):
        context_block = f"\n\nSchema/context:\n{args['context']}"
    prompt = (
        f"Optimize this {engine} query for performance.{context_block}\n\n"
        f"```\n{args['query']}\n```\n\n"
        f"Output:\n"
        f"1. The optimized query\n"
        f"2. What changed and why\n"
        f"3. Expected performance impact"
    )
    return await chat(prompt, system=get_system_preamble())


# --- Infrastructure tools ---

@tool_handler(
    name="health_check",
    description=(
        "Check text-generation-webui connectivity, loaded model, loader type, and LoRA status. "
        "Quick way to verify everything is working."
    ),
    schema={"type": "object", "properties": {}, "required": []},
)
async def health_check(args: dict) -> str:
    # Check basic connectivity
    try:
        health_resp = await _client.get(f"{TGWUI_INTERNAL}/health", timeout=5)
        health_resp.raise_for_status()
    except (httpx.ConnectError, httpx.HTTPStatusError, httpx.ReadTimeout) as e:
        return f"UNHEALTHY: Cannot reach text-generation-webui at {TGWUI_BASE}\nError: {e}"

    # Get model info
    try:
        info_resp = await _client.get(f"{TGWUI_INTERNAL}/model/info", timeout=5)
        info_resp.raise_for_status()
        info = info_resp.json()
    except Exception as e:
        return f"Connected but cannot get model info: {e}"

    model_name = info.get("model_name", "None")
    loader = info.get("loader", "unknown")
    loras = info.get("lora_names", [])
    lora_status = ", ".join(loras) if loras else "none"

    global MODEL
    MODEL = model_name if model_name and model_name != "None" else None

    preamble = get_system_preamble()
    ctx_status = "active" if preamble else "generic (no context set)"

    lines = [
        f"Status: HEALTHY",
        f"Endpoint: {TGWUI_BASE}",
        f"Model: {model_name}",
        f"Loader: {loader}",
        f"LoRAs: {lora_status}",
        f"Context: {ctx_status}",
    ]

    # Show all backends if multi-backend configured
    if len(_backends) > 1:
        lines.append(f"\nBackends ({_active_backend} active):")
        for name, info_b in sorted(_backends.items(), key=lambda x: x[1]["priority"]):
            healthy = await _check_backend_health(name)
            marker = " <- active" if name == _active_backend else ""
            status_str = "HEALTHY" if healthy else "UNREACHABLE"
            opt = " (optional)" if info_b.get("optional") else ""
            lines.append(f"  {name}: {info_b['url']} [{status_str}]{opt}{marker}")

    return "\n".join(lines)


@tool_handler(
    name="token_count",
    description="Count tokens in text using the currently loaded model's tokenizer",
    schema={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text to count tokens for"},
        },
        "required": ["text"],
    },
)
async def token_count(args: dict) -> str:
    resp = await _client.post(
        f"{TGWUI_INTERNAL}/token-count",
        json={"text": args["text"]},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    count = data.get("length") or data.get("tokens")
    if count is None:
        return f"Token count unavailable. Response: {data}"
    text_len = len(args["text"])
    return f"Tokens: {count}\nCharacters: {text_len}\nRatio: {text_len / max(int(count), 1):.1f} chars/token"


@tool_handler(
    name="encode_tokens",
    description=(
        "Tokenize text and return the actual token IDs. "
        "Useful for inspecting tokenization, debugging context usage, or checking special tokens."
    ),
    schema={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text to tokenize"},
        },
        "required": ["text"],
    },
)
async def encode_tokens(args: dict) -> str:
    resp = await _client.post(
        f"{TGWUI_INTERNAL}/encode",
        json={"text": args["text"]},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    tokens = data.get("tokens", [])
    length = data.get("length", len(tokens))
    # Show first/last tokens if too many
    if len(tokens) > 50:
        preview = f"{tokens[:25]} ... {tokens[-25:]}"
    else:
        preview = str(tokens)
    return f"Tokens ({length}): {preview}"


@tool_handler(
    name="swap_model",
    description=(
        "List available models or load a specific one without using the webUI. "
        "Call with no arguments to list all models (current model marked). "
        "Call with model_name to load that model. "
        "Use ctx_size to set context window (default 32768). "
        "Use gpu_layers to control GPU offloading (-1 = auto). "
        "Loading takes time — timeout is 180s."
    ),
    schema={
        "type": "object",
        "properties": {
            "model_name": {"type": "string", "description": "Model filename to load (e.g. 'Qwen3.5-35B-A3B-UD-Q5_K_XL.gguf'). Omit to list available models."},
            "ctx_size": {"type": "integer", "description": "Context window size in tokens (default: 32768). Sets both llama.cpp ctx_size and truncation_length."},
            "gpu_layers": {"type": "integer", "description": "Number of layers to offload to GPU (-1 = auto/all). Default: -1."},
        },
        "required": [],
    },
)
async def swap_model(args: dict) -> str:
    global MODEL

    if not args.get("model_name"):
        # List available models
        resp = await _client.get(f"{TGWUI_INTERNAL}/model/list", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        available = data.get("model_names", [])

        # Get current model
        info_resp = await _client.get(f"{TGWUI_INTERNAL}/model/info", timeout=5)
        info_resp.raise_for_status()
        current = info_resp.json().get("model_name", "")

        lines = []
        for m in sorted(available):
            marker = " \u2190 loaded" if m == current else ""
            lines.append(f"  {m}{marker}")
        return f"Available models ({len(available)}):\n" + "\n".join(lines)

    # Load a specific model — check config for model-specific defaults
    model_name = args["model_name"]
    model_config = {}
    for pattern, overrides in _config.get("models", {}).items():
        if pattern in model_name:
            model_config = overrides
            break
    ctx_size = args.get("ctx_size") or model_config.get("ctx_size", 32768)
    gpu_layers = args.get("gpu_layers") if args.get("gpu_layers") is not None else model_config.get("gpu_layers", -1)

    load_request: dict[str, Any] = {
        "model_name": model_name,
        "args": {
            "ctx_size": ctx_size,
            "gpu_layers": gpu_layers,
        },
        "settings": {
            "truncation_length": ctx_size,
        },
    }

    resp = await _client.post(
        f"{TGWUI_INTERNAL}/model/load",
        json=load_request,
        timeout=180,
    )
    resp.raise_for_status()

    # Verify load succeeded (webui returns "OK" on success)
    resp_text = resp.text.strip().strip('"')
    if resp_text != "OK":
        return f"Model load may have failed. Response: {resp_text}"

    # Re-resolve after load, preserving previous MODEL on failure
    previous_model = MODEL
    MODEL = None
    try:
        MODEL = await resolve_model()
    except Exception as e:
        MODEL = previous_model
        return f"Model load request sent but verification failed (still using {MODEL or 'none'}): {e}"

    # Update state file so claude-local knows the current ctx_size
    try:
        Path("/tmp/claude-local-ctx-state").write_text(f"{MODEL}:{ctx_size}")
    except OSError:
        pass  # non-critical

    return f"Model loaded: {MODEL}\nContext size: {ctx_size}\nGPU layers: {gpu_layers}"


@tool_handler(
    name="stop_generation",
    description="Interrupt a running text generation. Useful if a response is taking too long.",
    schema={"type": "object", "properties": {}, "required": []},
)
async def stop_generation(args: dict) -> str:
    resp = await _client.post(f"{TGWUI_INTERNAL}/stop-generation", timeout=5)
    resp.raise_for_status()
    return "Generation stopped."


# --- Memory tools ---

@tool_handler(
    name="scratchpad",
    description=(
        "In-memory session scratchpad for stashing intermediate results, plans, or snippets. "
        "Resets on server restart. Actions: write (set key=value), read (get key), list (all keys), clear (wipe all)."
    ),
    schema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["write", "read", "list", "clear"], "description": "Action to perform"},
            "key": {"type": "string", "description": "Key name (for write/read)"},
            "value": {"type": "string", "description": "Value to store (for write)"},
        },
        "required": ["action"],
    },
)
async def scratchpad(args: dict) -> str:
    action = args["action"]

    if action == "write":
        key = args.get("key", "")
        if not key:
            return "Error: 'key' is required for write"
        _scratchpad[key] = args.get("value", "")
        return f"Stored key '{key}' ({len(_scratchpad[key])} chars). Total keys: {len(_scratchpad)}"

    elif action == "read":
        key = args.get("key", "")
        if not key:
            return "Error: 'key' is required for read"
        if key not in _scratchpad:
            return f"Key '{key}' not found. Available: {', '.join(sorted(_scratchpad.keys())) or '(empty)'}"
        return _scratchpad[key]

    elif action == "list":
        if not _scratchpad:
            return "Scratchpad is empty."
        lines = [f"  {k} ({len(v)} chars)" for k, v in sorted(_scratchpad.items())]
        return f"Scratchpad keys ({len(_scratchpad)}):\n" + "\n".join(lines)

    elif action == "clear":
        count = len(_scratchpad)
        _scratchpad.clear()
        return f"Cleared {count} keys from scratchpad."

    return f"Unknown action: {action}"


@tool_handler(
    name="save_note",
    description=(
        "Persist a note to disk under ~/.claude/mcp-servers/local-model/notes/<topic>.md. "
        "Survives server restarts. Use for accumulating project knowledge, patterns, decisions."
    ),
    schema={
        "type": "object",
        "properties": {
            "topic": {"type": "string", "description": "Note topic (used as filename, e.g. 'architecture-decisions')"},
            "content": {"type": "string", "description": "Markdown content to save"},
            "append": {"type": "boolean", "description": "Append to existing note instead of overwriting (default: false)"},
        },
        "required": ["topic", "content"],
    },
)
async def save_note(args: dict) -> str:
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    topic = _sanitize_topic(args["topic"])
    path = _safe_note_path(topic)
    if path is None:
        return "Error: invalid topic name"

    if args.get("append") and path.exists():
        existing = path.read_text(encoding="utf-8")
        path.write_text(existing + "\n\n---\n\n" + args["content"], encoding="utf-8")
    else:
        path.write_text(args["content"], encoding="utf-8")
    return f"Saved note '{topic}' ({len(args['content'])} chars) to {path}"


@tool_handler(
    name="recall_note",
    description="Read a saved note by topic",
    schema={
        "type": "object",
        "properties": {
            "topic": {"type": "string", "description": "Note topic to recall"},
        },
        "required": ["topic"],
    },
)
async def recall_note(args: dict) -> str:
    topic = _sanitize_topic(args["topic"])
    path = _safe_note_path(topic)
    if path is None:
        return "Error: invalid topic name"
    if not path.exists():
        available = [f.stem for f in NOTES_DIR.glob("*.md")] if NOTES_DIR.exists() else []
        return f"Note '{topic}' not found. Available: {', '.join(sorted(available)) or '(none)'}"
    return path.read_text(encoding="utf-8")


@tool_handler(
    name="list_notes",
    description="List all saved note topics",
    schema={"type": "object", "properties": {}, "required": []},
)
async def list_notes(args: dict) -> str:
    if not NOTES_DIR.exists():
        return "No notes directory yet. Use save_note to create your first note."
    notes = sorted(NOTES_DIR.glob("*.md"))
    if not notes:
        return "No notes saved yet."
    lines = []
    for f in notes:
        size = f.stat().st_size
        lines.append(f"  {f.stem} ({size} bytes)")
    return f"Saved notes ({len(notes)}):\n" + "\n".join(lines)


@tool_handler(
    name="delete_note",
    description="Remove a saved note by topic",
    schema={
        "type": "object",
        "properties": {
            "topic": {"type": "string", "description": "Note topic to delete"},
        },
        "required": ["topic"],
    },
)
async def delete_note(args: dict) -> str:
    topic = _sanitize_topic(args["topic"])
    path = _safe_note_path(topic)
    if path is None:
        return "Error: invalid topic name"
    if not path.exists():
        return f"Note '{topic}' not found."
    path.unlink()
    return f"Deleted note '{topic}'."


# ===========================================================================
# New tools (multi_turn_chat, file_qa, analyze_image, classify_task,
#            structured_output, benchmark, diff_explain)
# ===========================================================================

@tool_handler(
    name="multi_turn_chat",
    description=(
        "Stateful multi-turn conversation with the local model. "
        "Unlike local_chat (one-shot), this maintains conversation history across calls. "
        "Actions: 'new' starts a fresh conversation, 'continue' adds a message, "
        "'history' shows the conversation, 'list' shows all sessions, 'clear' removes a session."
    ),
    schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["new", "continue", "history", "list", "clear"],
                "description": "Action to perform",
            },
            "session_id": {"type": "string", "description": "Session ID (auto-generated if omitted for 'new')"},
            "message": {"type": "string", "description": "User message (for 'new' and 'continue')"},
            "system": {"type": "string", "description": "System message override (for 'new' only)"},
        },
        "required": ["action"],
    },
)
async def multi_turn_chat(args: dict) -> str:
    global MODEL
    action = args["action"]
    session_id = args.get("session_id", "")

    if action == "new":
        if not session_id:
            session_id = f"session-{len(_conversations) + 1}"
        message = args.get("message", "")
        if not message:
            return "Error: 'message' is required for 'new'"

        if MODEL is None:
            MODEL = await resolve_model()

        system = args.get("system") or get_system_preamble()
        suffix = get_system_suffix(MODEL)
        effective_system = system
        if suffix:
            effective_system = f"{system}\n\n{suffix}" if system else suffix

        messages: list[dict[str, str]] = []
        if effective_system:
            messages.append({"role": "system", "content": effective_system})
        messages.append({"role": "user", "content": message})

        gen_params = get_generation_params(MODEL)
        body: dict[str, Any] = {"model": MODEL or "", "messages": messages, "stream": False, **gen_params}
        result = await _chat_to_backend(TGWUI_BASE, body)

        _conversations[session_id] = [
            {"role": "user", "content": message},
            {"role": "assistant", "content": result},
        ]
        return f"[Session: {session_id}]\n\n{result}"

    elif action == "continue":
        if not session_id or session_id not in _conversations:
            available = list(_conversations.keys())
            return f"Session '{session_id}' not found. Available: {', '.join(available) or '(none)'}"
        message = args.get("message", "")
        if not message:
            return "Error: 'message' is required for 'continue'"

        history = _conversations[session_id]
        if len(history) >= MAX_CONVERSATION_TURNS * 2:
            return f"Session '{session_id}' reached max {MAX_CONVERSATION_TURNS} turns. Start a new session."

        if MODEL is None:
            MODEL = await resolve_model()

        system = get_system_preamble()
        suffix = get_system_suffix(MODEL)
        effective_system = system
        if suffix:
            effective_system = f"{system}\n\n{suffix}" if system else suffix

        messages = []
        if effective_system:
            messages.append({"role": "system", "content": effective_system})
        messages.extend(history)
        messages.append({"role": "user", "content": message})

        gen_params = get_generation_params(MODEL)
        body = {"model": MODEL or "", "messages": messages, "stream": False, **gen_params}
        result = await _chat_to_backend(TGWUI_BASE, body)

        history.append({"role": "user", "content": message})
        history.append({"role": "assistant", "content": result})
        return f"[Session: {session_id}, turn {len(history) // 2}]\n\n{result}"

    elif action == "history":
        if not session_id or session_id not in _conversations:
            return f"Session '{session_id}' not found."
        history = _conversations[session_id]
        lines = []
        for msg in history:
            role = msg["role"].upper()
            content = msg["content"][:200] + "..." if len(msg["content"]) > 200 else msg["content"]
            lines.append(f"[{role}]: {content}")
        return f"Session '{session_id}' ({len(history) // 2} turns):\n\n" + "\n\n".join(lines)

    elif action == "list":
        if not _conversations:
            return "No active conversations."
        lines = []
        for sid, hist in _conversations.items():
            turns = len(hist) // 2
            preview = hist[0]["content"][:60] + "..." if hist else ""
            lines.append(f"  {sid}: {turns} turns - {preview}")
        return f"Active sessions ({len(_conversations)}):\n" + "\n".join(lines)

    elif action == "clear":
        if not session_id:
            count = len(_conversations)
            _conversations.clear()
            return f"Cleared all {count} sessions."
        if session_id in _conversations:
            del _conversations[session_id]
            return f"Cleared session '{session_id}'."
        return f"Session '{session_id}' not found."

    return f"Unknown action: {action}"


# --- Session persistence tools ---

@tool_handler(
    name="save_session",
    description=(
        "Save a multi-turn conversation to disk so it survives server restarts. "
        "Sessions are stored as JSON in ~/.claude/mcp-servers/local-model/sessions/."
    ),
    schema={
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "Session ID to save (must exist in memory)"},
        },
        "required": ["session_id"],
    },
)
async def save_session(args: dict) -> str:
    session_id = args["session_id"]
    if session_id not in _conversations:
        available = list(_conversations.keys())
        return f"Session '{session_id}' not found in memory. Available: {', '.join(available) or '(none)'}"

    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    safe_id = _sanitize_topic(session_id)
    path = SESSIONS_DIR / f"{safe_id}.json"

    data = {
        "session_id": session_id,
        "messages": _conversations[session_id],
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "turns": len(_conversations[session_id]) // 2,
        "model": MODEL,
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    return f"Saved session '{session_id}' ({data['turns']} turns) to {path}"


@tool_handler(
    name="load_session",
    description=(
        "Load a previously saved multi-turn conversation from disk into memory. "
        "After loading, use multi_turn_chat with action='continue' to resume."
    ),
    schema={
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "Session ID to load"},
        },
        "required": ["session_id"],
    },
)
async def load_session(args: dict) -> str:
    safe_id = _sanitize_topic(args["session_id"])
    path = SESSIONS_DIR / f"{safe_id}.json"

    if not path.exists():
        available = [f.stem for f in SESSIONS_DIR.glob("*.json")] if SESSIONS_DIR.exists() else []
        return f"Session not found on disk. Available: {', '.join(sorted(available)) or '(none)'}"

    with open(path) as f:
        data = json.load(f)

    session_id = data["session_id"]
    _conversations[session_id] = data["messages"]
    turns = data.get("turns", len(data["messages"]) // 2)
    saved_at = data.get("saved_at", "unknown")
    saved_model = data.get("model", "unknown")

    return (
        f"Loaded session '{session_id}' ({turns} turns, saved {saved_at}, model: {saved_model}). "
        f"Use multi_turn_chat(action='continue', session_id='{session_id}', message='...') to resume."
    )


@tool_handler(
    name="list_sessions",
    description="List all saved multi-turn conversation sessions (on disk and in memory).",
    schema={"type": "object", "properties": {}, "required": []},
)
async def list_sessions(args: dict) -> str:
    # In-memory sessions
    in_mem = list(_conversations.keys())

    # On-disk sessions
    on_disk = []
    if SESSIONS_DIR.exists():
        for f in sorted(SESSIONS_DIR.glob("*.json")):
            try:
                with open(f) as fp:
                    data = json.load(fp)
                sid = data.get("session_id", f.stem)
                turns = data.get("turns", "?")
                saved = data.get("saved_at", "?")
                loaded = " (loaded)" if sid in _conversations else ""
                on_disk.append(f"  {sid}: {turns} turns, saved {saved}{loaded}")
            except Exception:
                on_disk.append(f"  {f.stem}: (corrupt)")

    lines = []
    if on_disk:
        lines.append(f"Saved sessions ({len(on_disk)}):")
        lines.extend(on_disk)
    else:
        lines.append("No saved sessions on disk.")

    mem_only = [s for s in in_mem if not any(s in d for d in on_disk)]
    if mem_only:
        lines.append(f"\nIn-memory only (not saved): {', '.join(mem_only)}")

    return "\n".join(lines)


@tool_handler(
    name="delete_session",
    description="Delete a saved session from disk (and optionally from memory).",
    schema={
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "Session ID to delete"},
            "from_memory": {"type": "boolean", "description": "Also remove from in-memory conversations (default: true)"},
        },
        "required": ["session_id"],
    },
)
async def delete_session(args: dict) -> str:
    session_id = args["session_id"]
    safe_id = _sanitize_topic(session_id)
    path = SESSIONS_DIR / f"{safe_id}.json"

    deleted = []
    if path.exists():
        path.unlink()
        deleted.append("disk")

    if args.get("from_memory", True) and session_id in _conversations:
        del _conversations[session_id]
        deleted.append("memory")

    if not deleted:
        return f"Session '{session_id}' not found on disk or in memory."
    return f"Deleted session '{session_id}' from {' and '.join(deleted)}."


@tool_handler(
    name="file_qa",
    description=(
        "Read a file from disk and ask the local model a question about it. "
        "Saves you from pasting file contents. Supports text files up to 100KB."
    ),
    schema={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Path to the file (absolute or ~ relative)"},
            "question": {"type": "string", "description": "Question to ask about the file"},
            "line_range": {"type": "string", "description": "Optional line range, e.g. '10-50'"},
        },
        "required": ["file_path", "question"],
    },
)
async def file_qa(args: dict) -> str:
    file_path = Path(os.path.expanduser(args["file_path"])).resolve()

    home = Path.home().resolve()
    allowed_prefixes = [str(home), "/tmp"]
    if not any(str(file_path).startswith(p) for p in allowed_prefixes):
        return f"Error: file must be under {home} or /tmp"

    if not file_path.exists():
        return f"Error: file not found: {file_path}"
    if not file_path.is_file():
        return f"Error: not a file: {file_path}"

    size = file_path.stat().st_size
    if size > 100_000:
        return f"Error: file too large ({size:,} bytes, max 100KB). Use line_range to read a section."

    content = file_path.read_text(encoding="utf-8", errors="replace")

    if args.get("line_range"):
        lines = content.splitlines()
        try:
            parts = args["line_range"].split("-")
            start = int(parts[0]) - 1
            end = int(parts[1]) if len(parts) > 1 else start + 1
            content = "\n".join(lines[start:end])
        except (ValueError, IndexError):
            return f"Error: invalid line_range '{args['line_range']}'. Use format: '10-50'"

    prompt = (
        f"File: {file_path.name}\n\n"
        f"```\n{content}\n```\n\n"
        f"Question: {args['question']}"
    )
    return await chat(prompt, system=get_system_preamble())


@tool_handler(
    name="analyze_image",
    description=(
        "Send an image to the local vision model for analysis. "
        "Requires a vision-capable model (e.g. Qwen3-VL). "
        "Supports PNG, JPG, WEBP up to 10MB."
    ),
    schema={
        "type": "object",
        "properties": {
            "image_path": {"type": "string", "description": "Path to the image file"},
            "question": {"type": "string", "description": "Question about the image (default: describe it)"},
        },
        "required": ["image_path"],
    },
)
async def analyze_image(args: dict) -> str:
    global MODEL
    if MODEL is None:
        MODEL = await resolve_model()

    vision_keywords = ["vl", "vision", "visual"]
    is_vision = any(kw in (MODEL or "").lower() for kw in vision_keywords)
    if not is_vision:
        return (
            f"Current model ({MODEL}) is not vision-capable. "
            f"Load a vision model first, e.g.: swap_model(model_name='Qwen3-VL-30B-A3B-Instruct-UD-Q4_K_XL.gguf')"
        )

    image_path = Path(os.path.expanduser(args["image_path"])).resolve()

    home = Path.home().resolve()
    if not (str(image_path).startswith(str(home)) or str(image_path).startswith("/tmp")):
        return f"Error: image must be under {home} or /tmp"
    if not image_path.exists():
        return f"Error: image not found: {image_path}"

    size = image_path.stat().st_size
    if size > 10_000_000:
        return f"Error: image too large ({size / 1_000_000:.1f} MB, max 10MB)"

    suffix = image_path.suffix.lower()
    mime_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".webp": "image/webp", ".gif": "image/gif"}
    mime_type = mime_map.get(suffix)
    if not mime_type:
        return f"Error: unsupported format '{suffix}'. Use PNG, JPG, or WEBP."

    image_data = base64.b64encode(image_path.read_bytes()).decode("utf-8")
    question = args.get("question", "Describe this image in detail.")

    system = get_system_preamble()
    sys_suffix = get_system_suffix(MODEL)
    effective_system = system
    if sys_suffix:
        effective_system = f"{system}\n\n{sys_suffix}" if system else sys_suffix

    messages: list[dict[str, Any]] = []
    if effective_system:
        messages.append({"role": "system", "content": effective_system})
    messages.append({
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_data}"}},
            {"type": "text", "text": question},
        ],
    })

    gen_params = get_generation_params(MODEL)
    body: dict[str, Any] = {"model": MODEL, "messages": messages, "stream": False, **gen_params}
    return await _chat_to_backend(TGWUI_BASE, body)


@tool_handler(
    name="classify_task",
    description=(
        "Classify a task and recommend the best model from your inventory. "
        "Fast heuristic — no model call needed. Tells you which model to load."
    ),
    schema={
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "Description of the task"},
        },
        "required": ["task"],
    },
)
async def classify_task(args: dict) -> str:
    task = args["task"].lower()

    categories = {
        "code": {
            "keywords": ["code", "function", "implement", "debug", "refactor", "test",
                         "compile", "build", "fix bug", "write a", "programming", "api",
                         "endpoint", "class", "method", "syntax", "coder", "coding"],
            "models": [
                ("Qwen3-Coder-30B-A3B-Instruct-1M-UD-Q5_K_XL.gguf", "Best code model, 1M ctx, MoE"),
                ("Devstral-Small-2-24B-Instruct-2512-UD-Q5_K_XL.gguf", "Strong code, 24B dense"),
            ],
        },
        "reasoning": {
            "keywords": ["think", "reason", "plan", "architect", "design", "analyze",
                         "complex", "strategy", "trade-off", "compare", "evaluate",
                         "decision", "step by step", "chain of thought"],
            "models": [
                ("Qwen3.5-27B-UD-Q5_K_XL.gguf", "Dense 27B, best multi-step reasoning"),
                ("Qwen3-30B-A3B-Thinking-2507-UD-Q6_K_XL.gguf", "Thinking model with <think> blocks"),
            ],
        },
        "vision": {
            "keywords": ["image", "picture", "screenshot", "photo", "visual", "diagram",
                         "chart", "ui", "design", "look at", "see", "ocr"],
            "models": [
                ("Qwen3-VL-30B-A3B-Instruct-UD-Q4_K_XL.gguf", "Vision + instruction, MoE"),
                ("Qwen3-VL-30B-A3B-Thinking-UD-Q5_K_XL.gguf", "Vision + thinking"),
            ],
        },
        "quick": {
            "keywords": ["quick", "simple", "short", "fast", "brief", "summary",
                         "tldr", "one-liner", "small", "tiny"],
            "models": [
                ("google_gemma-3n-E4B-it-Q8_0.gguf", "Fast, small footprint"),
                ("Qwen3.5-35B-A3B-UD-Q5_K_XL.gguf", "Primary model, MoE (fast enough)"),
            ],
        },
        "general": {
            "keywords": [],
            "models": [
                ("Qwen3.5-35B-A3B-UD-Q5_K_XL.gguf", "Primary model, good all-rounder"),
                ("Qwen3-30B-A3B-UD-Q4_K_XL.gguf", "Lighter Qwen3 chat"),
            ],
        },
    }

    scores = {cat: sum(1 for kw in info["keywords"] if kw in task)
              for cat, info in categories.items()}
    best_cat = max(scores, key=scores.get) if max(scores.values()) > 0 else "general"
    recommendations = categories[best_cat]["models"]

    current = MODEL or "(none)"
    current_match = any(m[0] in current for m in recommendations)

    lines = [f"Task type: {best_cat}"]
    if current_match:
        lines.append(f"Current model ({current}) is already a good fit!")
    else:
        lines.append(f"Current model: {current}")
        lines.append("\nRecommended:")
        for model_name, reason in recommendations:
            lines.append(f"  -> {model_name}")
            lines.append(f"     {reason}")
        lines.append(f"\nSwap: swap_model(model_name='{recommendations[0][0]}')")

    return "\n".join(lines)


@tool_handler(
    name="structured_output",
    description=(
        "Get a JSON-structured response from the local model. "
        "Useful for extracting structured data, generating configs, or tool chaining. "
        "Supports GBNF grammar constraints for guaranteed valid output."
    ),
    schema={
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "What to generate as JSON"},
            "schema_hint": {"type": "string", "description": "Optional JSON schema or example of expected structure"},
            "grammar": {"type": "string", "description": "GBNF grammar constraint. Built-in: 'json', 'json_array', 'boolean'. Or provide a custom GBNF string. Omit for prompt-only mode."},
        },
        "required": ["prompt"],
    },
)
async def structured_output(args: dict) -> str:
    schema_block = ""
    if args.get("schema_hint"):
        schema_block = f"\n\nExpected JSON structure:\n```json\n{args['schema_hint']}\n```"

    prompt = (
        f"{args['prompt']}{schema_block}\n\n"
        f"IMPORTANT: Output ONLY valid JSON. No markdown fences, no explanations, just the JSON object."
    )
    kwargs: dict[str, Any] = {}
    grammar = args.get("grammar")
    if grammar:
        kwargs["grammar_string"] = _BUILTIN_GRAMMARS.get(grammar, grammar)
    result = await chat(prompt, system=get_system_preamble(), **kwargs)

    # Clean up common model output artifacts
    result = result.strip()
    if result.startswith("```json"):
        result = result[7:]
    if result.startswith("```"):
        result = result[3:]
    if result.endswith("```"):
        result = result[:-3]
    result = result.strip()

    try:
        parsed = json.loads(result)
        return json.dumps(parsed, indent=2)
    except json.JSONDecodeError as e:
        return f"Warning: output is not valid JSON.\n\nRaw output:\n{result}\n\nParse error: {e}"


@tool_handler(
    name="benchmark",
    description=(
        "Quick benchmark of the currently loaded model. "
        "Measures generation speed in tokens/second."
    ),
    schema={
        "type": "object",
        "properties": {
            "prompt_length": {
                "type": "string",
                "enum": ["short", "medium", "long"],
                "description": "Prompt complexity (default: short)",
            },
        },
        "required": [],
    },
)
async def benchmark_tool(args: dict) -> str:
    global MODEL
    if MODEL is None:
        MODEL = await resolve_model()

    length = args.get("prompt_length", "short")
    prompts = {
        "short": "Write a haiku about programming.",
        "medium": "Explain the difference between a stack and a queue, with Python examples.",
        "long": (
            "Compare quicksort, mergesort, and heapsort: time complexity (best/avg/worst), "
            "space complexity, stability, use cases, and pseudocode for each."
        ),
    }
    prompt = prompts.get(length, prompts["short"])

    start = time.perf_counter()
    result = await chat(prompt, max_tokens=256)
    elapsed = time.perf_counter() - start

    # Get accurate token count from the loaded tokenizer
    output_tokens = len(result) / 4  # rough fallback
    try:
        resp = await _client.post(
            f"{TGWUI_INTERNAL}/token-count",
            json={"text": result},
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        actual = data.get("length") or data.get("tokens")
        if actual:
            output_tokens = actual
    except Exception:
        pass

    tps = output_tokens / elapsed if elapsed > 0 else 0

    return (
        f"Model: {MODEL}\n"
        f"Prompt: {length} ({len(prompt)} chars)\n"
        f"Output: {int(output_tokens)} tokens ({len(result)} chars)\n"
        f"Time: {elapsed:.1f}s\n"
        f"Speed: {tps:.1f} tok/s"
    )


@tool_handler(
    name="diff_explain",
    description=(
        "Explain what a git diff does in plain English. "
        "Unlike review_diff (which checks for issues), this just explains the changes."
    ),
    schema={
        "type": "object",
        "properties": {
            "diff": {"type": "string", "description": "Git diff output to explain"},
        },
        "required": ["diff"],
    },
)
async def diff_explain(args: dict) -> str:
    prompt = (
        "Explain what this git diff does in plain English. "
        "Focus on what changed and why (if inferrable), not on code review.\n\n"
        f"```diff\n{args['diff']}\n```"
    )
    return await chat(prompt, system=get_system_preamble())


# ===========================================================================
# Parallel local-only tools — zero Claude API, asyncio fan-out
# ===========================================================================

async def _local_analyze_one(file_path: str, concern: str, preamble: str | None,
                              gen_params: dict[str, Any]) -> dict[str, str]:
    """Analyze a single file for a concern. Returns {file, verdict, details}."""
    path = Path(os.path.expanduser(file_path)).resolve()
    home = Path.home().resolve()
    if not (str(path).startswith(str(home)) or str(path).startswith("/tmp")):
        return {"file": file_path, "verdict": "SKIP", "details": "outside allowed paths"}
    if not path.exists():
        return {"file": file_path, "verdict": "SKIP", "details": "file not found"}
    if not path.is_file():
        return {"file": file_path, "verdict": "SKIP", "details": "not a file"}

    size = path.stat().st_size
    if size > 100_000:
        return {"file": file_path, "verdict": "SKIP", "details": f"too large ({size:,} bytes)"}

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"file": file_path, "verdict": "SKIP", "details": str(e)}

    prompt = (
        f"Analyze the following code for: {concern}\n"
        f"File: {path.name}\n\n"
        f"Be concise. List specific issues with line numbers.\n"
        f"If no issues found, say 'No issues found.'\n\n"
        f"```\n{content}\n```"
    )

    suffix = get_system_suffix(MODEL)
    effective_system = preamble
    if suffix:
        effective_system = f"{preamble}\n\n{suffix}" if preamble else suffix

    messages: list[dict[str, str]] = []
    if effective_system:
        messages.append({"role": "system", "content": effective_system})
    messages.append({"role": "user", "content": prompt})

    body: dict[str, Any] = {"model": MODEL or "", "messages": messages, "stream": False, **gen_params}

    try:
        result = await _chat_to_backend(TGWUI_BASE, body)
        has_issues = "no issues" not in result.lower()
        return {
            "file": path.name,
            "verdict": "FAIL" if has_issues else "PASS",
            "details": result,
        }
    except Exception as e:
        return {"file": file_path, "verdict": "ERROR", "details": str(e)}


@tool_handler(
    name="fan_out",
    description=(
        "Run multiple prompts through the local model in parallel. "
        "100%% local — zero API costs. Uses asyncio.gather for concurrent execution. "
        "Returns all results labeled by index."
    ),
    schema={
        "type": "object",
        "properties": {
            "prompts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of prompts to run in parallel",
            },
            "system": {"type": "string", "description": "Optional shared system message for all prompts"},
        },
        "required": ["prompts"],
    },
)
async def fan_out(args: dict) -> str:
    global MODEL
    if MODEL is None:
        MODEL = await resolve_model()

    prompts = args["prompts"]
    if not prompts:
        return "Error: provide at least one prompt"
    if len(prompts) > 20:
        return f"Error: max 20 prompts (got {len(prompts)}). Split into batches."

    system = args.get("system") or get_system_preamble()
    suffix = get_system_suffix(MODEL)
    effective_system = system
    if suffix:
        effective_system = f"{system}\n\n{suffix}" if system else suffix

    gen_params = get_generation_params(MODEL)

    async def _run_one(idx: int, prompt: str) -> tuple[int, str]:
        messages: list[dict[str, str]] = []
        if effective_system:
            messages.append({"role": "system", "content": effective_system})
        messages.append({"role": "user", "content": prompt})
        body: dict[str, Any] = {"model": MODEL or "", "messages": messages, "stream": False, **gen_params}
        try:
            result = await _chat_to_backend(TGWUI_BASE, body)
            return (idx, result)
        except Exception as e:
            return (idx, f"Error: {e}")

    log.info("fan_out: dispatching %d prompts in parallel", len(prompts))
    tasks = [_run_one(i, p) for i, p in enumerate(prompts)]
    results = await asyncio.gather(*tasks)
    results_sorted = sorted(results, key=lambda x: x[0])

    parts = []
    for idx, result in results_sorted:
        prompt_preview = prompts[idx][:60] + "..." if len(prompts[idx]) > 60 else prompts[idx]
        parts.append(f"--- [{idx + 1}] {prompt_preview} ---\n{result}")

    return "\n\n".join(parts)


@tool_handler(
    name="parallel_file_review",
    description=(
        "Review multiple files for a specific concern — all in parallel on the local model. "
        "100%% local, zero API costs. Reads each file, analyzes it, returns pass/fail per file."
    ),
    schema={
        "type": "object",
        "properties": {
            "file_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of file paths to review",
            },
            "concern": {"type": "string", "description": "What to check for (e.g. 'error handling', 'security issues')"},
        },
        "required": ["file_paths", "concern"],
    },
)
async def parallel_file_review(args: dict) -> str:
    global MODEL
    if MODEL is None:
        MODEL = await resolve_model()

    file_paths = args["file_paths"]
    concern = args["concern"]

    if not file_paths:
        return "Error: provide at least one file path"
    if len(file_paths) > 30:
        return f"Error: max 30 files (got {len(file_paths)}). Split into batches."

    preamble = get_system_preamble()
    gen_params = get_generation_params(MODEL)

    log.info("parallel_file_review: %d files for '%s'", len(file_paths), concern)
    tasks = [_local_analyze_one(fp, concern, preamble, gen_params) for fp in file_paths]
    results = await asyncio.gather(*tasks)

    # Build summary table
    passes = sum(1 for r in results if r["verdict"] == "PASS")
    fails = sum(1 for r in results if r["verdict"] == "FAIL")
    skips = sum(1 for r in results if r["verdict"] in ("SKIP", "ERROR"))

    lines = [f"Reviewed {len(results)} files for: {concern}",
             f"Results: {passes} PASS, {fails} FAIL, {skips} SKIP\n"]

    for r in results:
        marker = {"PASS": "ok", "FAIL": "ISSUE", "SKIP": "skip", "ERROR": "err"}.get(r["verdict"], "?")
        lines.append(f"[{marker}] {r['file']}")
        if r["verdict"] == "FAIL":
            # Indent the details
            for detail_line in r["details"].splitlines()[:5]:
                lines.append(f"     {detail_line}")
            if len(r["details"].splitlines()) > 5:
                lines.append(f"     ... ({len(r['details'].splitlines()) - 5} more lines)")

    return "\n".join(lines)


@tool_handler(
    name="quality_sweep",
    description=(
        "Sweep a directory for a quality criterion. Finds files matching a glob pattern, "
        "analyzes each in parallel on the local model, returns pass/fail summary. "
        "100%% local, zero API costs. Good for 'check all .rs files for unwrap()' type checks."
    ),
    schema={
        "type": "object",
        "properties": {
            "directory": {"type": "string", "description": "Directory to sweep (absolute or ~ relative)"},
            "glob_pattern": {"type": "string", "description": "Glob pattern for files (e.g. '*.rs', '*.py', '**/*.ts')"},
            "criterion": {"type": "string", "description": "Quality criterion to check (e.g. 'no unwrap()', 'proper error handling')"},
            "max_files": {"type": "integer", "description": "Max files to check (default: 20)"},
        },
        "required": ["directory", "glob_pattern", "criterion"],
    },
)
async def quality_sweep(args: dict) -> str:
    global MODEL
    if MODEL is None:
        MODEL = await resolve_model()

    directory = Path(os.path.expanduser(args["directory"])).resolve()
    home = Path.home().resolve()
    if not (str(directory).startswith(str(home)) or str(directory).startswith("/tmp")):
        return f"Error: directory must be under {home} or /tmp"
    if not directory.exists():
        return f"Error: directory not found: {directory}"

    glob_pattern = args["glob_pattern"]
    criterion = args["criterion"]
    max_files = args.get("max_files", 20)

    # Find matching files
    files = sorted(directory.glob(glob_pattern))
    # Filter to actual files, skip hidden dirs
    files = [f for f in files if f.is_file() and "/." not in str(f)]

    if not files:
        return f"No files matching '{glob_pattern}' in {directory}"

    if len(files) > max_files:
        files = files[:max_files]
        truncated = True
    else:
        truncated = False

    preamble = get_system_preamble()
    gen_params = get_generation_params(MODEL)

    log.info("quality_sweep: %d files in %s for '%s'", len(files), directory, criterion)
    tasks = [_local_analyze_one(str(f), criterion, preamble, gen_params) for f in files]
    results = await asyncio.gather(*tasks)

    passes = sum(1 for r in results if r["verdict"] == "PASS")
    fails = sum(1 for r in results if r["verdict"] == "FAIL")
    skips = sum(1 for r in results if r["verdict"] in ("SKIP", "ERROR"))

    lines = [
        f"Quality sweep: {criterion}",
        f"Directory: {directory}",
        f"Pattern: {glob_pattern} ({len(files)} files{', truncated' if truncated else ''})",
        f"Results: {passes} PASS, {fails} FAIL, {skips} SKIP",
        "",
    ]

    # List failures first
    for r in results:
        if r["verdict"] == "FAIL":
            lines.append(f"FAIL {r['file']}")
            for detail_line in r["details"].splitlines()[:3]:
                lines.append(f"     {detail_line}")

    # Then passes (compact)
    pass_files = [r["file"] for r in results if r["verdict"] == "PASS"]
    if pass_files:
        lines.append(f"\nPASS: {', '.join(pass_files)}")

    skip_files = [f"{r['file']} ({r['details']})" for r in results if r["verdict"] in ("SKIP", "ERROR")]
    if skip_files:
        lines.append(f"\nSKIP: {', '.join(skip_files)}")

    return "\n".join(lines)


# ===========================================================================
# RAG / Code Indexing tools — BM25 search + LLM retrieval-augmented generation
# ===========================================================================

@tool_handler(
    name="index_directory",
    description=(
        "Build a searchable index from files in a directory. "
        "Uses tree-sitter for code-aware semantic chunking (function/struct boundaries) "
        "with fallback to line-based chunking. BM25 index is always built. "
        "embed=true adds dense (jina-code) + SPLADE sparse vectors. "
        "colbert=true additionally adds ColBERT per-token vectors (heavier, best precision)."
    ),
    schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Index name (e.g. 'platform_next', 'rust-docs')"},
            "directory": {"type": "string", "description": "Directory to index (absolute or ~ relative)"},
            "glob_pattern": {"type": "string", "description": "File pattern (default: '**/*.*'). Use '**/*.rs' for Rust, '**/*.py' for Python, etc."},
            "chunk_lines": {"type": "integer", "description": "Lines per chunk for line-based fallback (default: 50)."},
            "overlap": {"type": "integer", "description": "Overlap lines between chunks for line-based fallback (default: 10)"},
            "max_files": {"type": "integer", "description": "Max files to index (default: 500)"},
            "embed": {"type": "boolean", "description": "Compute dense + SPLADE sparse embeddings (default: false). Enables hybrid_search."},
            "colbert": {"type": "boolean", "description": "Also compute ColBERT per-token vectors (default: false). Best precision but heavier on storage. Requires embed=true."},
        },
        "required": ["name", "directory"],
    },
)
async def index_directory(args: dict) -> str:
    name = _sanitize_topic(args["name"])
    directory = Path(os.path.expanduser(args["directory"])).resolve()

    home = Path.home().resolve()
    if not (str(directory).startswith(str(home)) or str(directory).startswith("/tmp")):
        return f"Error: directory must be under {home} or /tmp"
    if not directory.exists():
        return f"Error: directory not found: {directory}"

    glob_pattern = args.get("glob_pattern", "**/*.*")
    chunk_lines = args.get("chunk_lines", 50)
    overlap = args.get("overlap", 10)
    max_files = args.get("max_files", 500)
    do_embed = args.get("embed", False)
    do_colbert = args.get("colbert", False) and do_embed

    # Find indexable files
    files = sorted(directory.glob(glob_pattern))
    files = [
        f for f in files
        if f.is_file()
        and "/." not in str(f)
        and f.stat().st_size < 200_000
        and (f.suffix.lower() in _TEXT_EXTENSIONS or not f.suffix)
    ]

    if len(files) > max_files:
        files = files[:max_files]

    if not files:
        return f"No indexable text files found in {directory} with pattern '{glob_pattern}'"

    # Chunk all files — tree-sitter for code, line-based for everything else
    all_chunks: list[dict[str, Any]] = []
    ts_count = 0
    for f in files:
        chunks = _chunk_file_treesitter(f, max_chunk_lines=chunk_lines)
        if chunks and chunks != _chunk_file(f, chunk_lines=chunk_lines, overlap=overlap):
            ts_count += 1
        all_chunks.extend(chunks)

    if not all_chunks:
        return "No content to index (all files were empty or too small)."

    # Build BM25
    corpus = [c["tokens"] for c in all_chunks]
    bm25 = _BM25(corpus)

    # Optional: compute embeddings (dense + sparse, optionally ColBERT)
    texts = [c["content"] for c in all_chunks]
    embeddings = None
    sparse_embeddings = None
    colbert_embeddings = None

    if do_embed:
        try:
            embeddings = _embed_texts(texts)
            log.info("Computed %d dense embeddings for index '%s'", len(embeddings), name)
        except Exception as e:
            log.warning("Dense embedding failed: %s", e)

        try:
            sparse_embeddings = _sparse_embed_texts(texts)
            log.info("Computed %d SPLADE sparse vectors for index '%s'", len(sparse_embeddings), name)
        except Exception as e:
            log.warning("SPLADE embedding failed: %s", e)

        if do_colbert:
            try:
                colbert_embeddings = _colbert_embed_texts(texts)
                log.info("Computed %d ColBERT multi-vectors for index '%s'", len(colbert_embeddings), name)
            except Exception as e:
                log.warning("ColBERT embedding failed: %s", e)

    # Persist to disk
    meta = {
        "name": name,
        "directory": str(directory),
        "glob_pattern": glob_pattern,
        "chunk_lines": chunk_lines,
        "overlap": overlap,
        "file_count": len(files),
        "chunk_count": len(all_chunks),
        "treesitter_files": ts_count,
        "has_dense": embeddings is not None,
        "has_sparse": sparse_embeddings is not None,
        "has_colbert": colbert_embeddings is not None,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    _save_index(name, meta, all_chunks, embeddings, sparse_embeddings, colbert_embeddings)

    # Cache in memory
    _index_cache[name] = {
        "meta": meta, "chunks": all_chunks, "bm25": bm25,
        "embeddings": embeddings, "sparse_embeddings": sparse_embeddings,
        "colbert_embeddings": colbert_embeddings,
    }

    signals = ["BM25"]
    if embeddings:
        signals.append(f"dense ({len(embeddings[0])}d)")
    if sparse_embeddings:
        signals.append("SPLADE")
    if colbert_embeddings:
        signals.append("ColBERT")

    return (
        f"Index '{name}' built.\n"
        f"  Directory: {directory}\n"
        f"  Pattern: {glob_pattern}\n"
        f"  Files indexed: {len(files)} ({ts_count} tree-sitter, {len(files) - ts_count} line-based)\n"
        f"  Chunks: {len(all_chunks)}\n"
        f"  Signals: {' + '.join(signals)}"
    )


@tool_handler(
    name="search_index",
    description=(
        "Search a BM25 index with a natural language query. "
        "Returns the top matching code/text chunks with file paths and line numbers. "
        "Pure search — no LLM call. Use rag_query for search + answer."
    ),
    schema={
        "type": "object",
        "properties": {
            "index_name": {"type": "string", "description": "Name of the index to search"},
            "query": {"type": "string", "description": "Search query (natural language or keywords)"},
            "top_k": {"type": "integer", "description": "Number of results to return (default: 5)"},
        },
        "required": ["index_name", "query"],
    },
)
async def search_index_tool(args: dict) -> str:
    name = _sanitize_topic(args["index_name"])
    entry = _load_index(name)
    if entry is None:
        available = [d.name for d in INDEXES_DIR.iterdir() if d.is_dir()] if INDEXES_DIR.exists() else []
        return f"Index '{name}' not found. Available: {', '.join(sorted(available)) or '(none)'}"

    query = args["query"]
    top_k = args.get("top_k", 5)
    query_tokens = _tokenize_bm25(query)

    if not query_tokens:
        return "Query produced no searchable tokens."

    bm25 = entry["bm25"]
    if bm25 is None:
        return "Index is empty."

    results = bm25.search(query_tokens, top_k=top_k)

    if not results:
        return f"No matches for '{query}' in index '{name}'."

    chunks = entry["chunks"]
    parts = []
    for idx, score in results:
        chunk = chunks[idx]
        try:
            rel = str(Path(chunk["file"]).relative_to(entry["meta"]["directory"]))
        except ValueError:
            rel = chunk["file"]
        parts.append(
            f"--- {rel}:{chunk['start_line']}-{chunk['end_line']} (score: {score:.2f}) ---\n"
            f"{chunk['content']}"
        )

    return f"Top {len(results)} matches for '{query}' in index '{name}':\n\n" + "\n\n".join(parts)


async def _cross_project_rag(question: str, top_k: int = 3, do_rerank: bool = False) -> str:
    """Search across all indexed projects, return results tagged by project."""
    if not INDEXES_DIR.exists():
        return "No indexes found."

    all_results: list[tuple[str, float, str]] = []  # (project, score, content_preview)
    query_tokens = _tokenize_bm25(question)
    if not query_tokens:
        return "Question produced no searchable tokens."

    index_dirs = [d.name for d in INDEXES_DIR.iterdir() if d.is_dir()
                  and not d.name.startswith("__")]

    for idx_name in index_dirs:
        entry = _load_index(idx_name)
        if not entry or entry.get("bm25") is None:
            continue

        bm25 = entry["bm25"]
        results = bm25.search(query_tokens, top_k=top_k)
        chunks = entry["chunks"]

        for idx, score in results:
            chunk = chunks[idx]
            try:
                rel = str(Path(chunk["file"]).relative_to(entry["meta"]["directory"]))
            except (ValueError, KeyError):
                rel = chunk["file"]
            preview = f"[{idx_name}] {rel}:{chunk['start_line']}-{chunk['end_line']}\n{chunk['content'][:300]}"
            all_results.append((idx_name, score, preview))

    if not all_results:
        return await chat(question, system=get_system_preamble())

    # Sort by score descending, take top_k
    all_results.sort(key=lambda x: x[1], reverse=True)
    top_results = all_results[:top_k]

    context = "\n\n".join(r[2] for r in top_results)
    projects_found = sorted(set(r[0] for r in top_results))

    answer = await chat(
        f"Question: {question}\n\n"
        f"Context from projects ({', '.join(projects_found)}):\n{context[:6000]}",
        system=get_system_preamble(),
    )
    return f"*Searched {len(index_dirs)} indexes: {', '.join(projects_found)}*\n\n{answer}"


@tool_handler(
    name="rag_query",
    description=(
        "Search an index and ask the local model a question using the matching chunks as context. "
        "Full RAG pipeline: BM25 retrieval → optional re-ranking → context assembly → LLM generation. "
        "Set rerank=true for better relevance (extra model call). 100%% local."
    ),
    schema={
        "type": "object",
        "properties": {
            "index_name": {"type": "string", "description": "Name of the index to search. Use '*' or 'all' to search across all indexed projects."},
            "question": {"type": "string", "description": "Question to answer using retrieved context"},
            "top_k": {"type": "integer", "description": "Number of context chunks to retrieve (default: 3)"},
            "rerank": {"type": "boolean", "description": "Re-rank BM25 results using the model for better relevance (default: false)"},
        },
        "required": ["index_name", "question"],
    },
)
async def rag_query(args: dict) -> str:
    raw_name = args["index_name"]
    question = args["question"]
    top_k = args.get("top_k", 3)
    do_rerank = args.get("rerank", False)

    # Cross-project search: index_name="*" searches all indexes
    if raw_name.strip() in ("*", "all"):
        return await _cross_project_rag(question, top_k, do_rerank)

    name = _sanitize_topic(raw_name)
    entry = _load_index(name)
    if entry is None:
        available = [d.name for d in INDEXES_DIR.iterdir() if d.is_dir()] if INDEXES_DIR.exists() else []
        return f"Index '{name}' not found. Available: {', '.join(sorted(available)) or '(none)'}"
    query_tokens = _tokenize_bm25(question)

    if not query_tokens:
        return "Question produced no searchable tokens."

    bm25 = entry["bm25"]
    if bm25 is None:
        return "Index is empty."

    # Retrieve more candidates if re-ranking (2x top_k, min 6)
    retrieve_k = max(top_k * 2, 6) if do_rerank else top_k
    results = bm25.search(query_tokens, top_k=retrieve_k)

    if not results:
        # Fallback to no-context answer
        return await chat(question, system=get_system_preamble())

    chunks = entry["chunks"]

    # Re-rank using cross-encoder if available, else fall back to BM25 order
    if do_rerank and len(results) > top_k:
        try:
            reranker = _get_reranker()
            # Build (query, chunk_text) pairs for cross-encoder scoring
            pairs = [(question, chunks[idx]["content"][:500]) for idx, _score in results]
            rerank_scores = list(reranker.rerank(question, [p[1] for p in pairs], top_k=top_k))
            # rerank returns list of dicts with 'corpus_id' and 'score', sorted by score desc
            reranked_ids = [r["corpus_id"] for r in rerank_scores[:top_k]]
            results = [results[i] for i in reranked_ids]
            log.info("Cross-encoder re-ranked: kept %d of %d candidates", len(results), retrieve_k)
        except Exception as e:
            log.warning("Cross-encoder rerank failed, using BM25 order: %s", e)
            results = results[:top_k]
    else:
        results = results[:top_k]

    context_parts = []
    for idx, score in results:
        chunk = chunks[idx]
        try:
            rel = str(Path(chunk["file"]).relative_to(entry["meta"]["directory"]))
        except ValueError:
            rel = chunk["file"]
        context_parts.append(f"[{rel}:{chunk['start_line']}-{chunk['end_line']}]\n{chunk['content']}")

    context = "\n\n---\n\n".join(context_parts)
    prompt = (
        f"Use the following code/documentation context to answer the question.\n"
        f"Cite file paths and line numbers when referencing specific code.\n"
        f"If the context doesn't contain enough information, say so.\n\n"
        f"Context ({len(results)} chunks):\n{context}\n\n"
        f"Question: {question}"
    )
    return await chat(prompt, system=get_system_preamble())


@tool_handler(
    name="list_indexes",
    description="List all available search indexes with metadata (file count, chunk count, directory).",
    schema={"type": "object", "properties": {}, "required": []},
)
async def list_indexes(args: dict) -> str:
    if not INDEXES_DIR.exists():
        return "No indexes. Use index_directory to create one."

    dirs = sorted(d for d in INDEXES_DIR.iterdir() if d.is_dir())
    if not dirs:
        return "No indexes. Use index_directory to create one."

    lines = []
    for d in dirs:
        meta_path = d / "meta.json"
        if meta_path.exists():
            try:
                with open(meta_path) as f:
                    meta = json.load(f)
                cached = " (cached)" if d.name in _index_cache else ""
                lines.append(
                    f"  {meta['name']}: {meta['file_count']} files, "
                    f"{meta['chunk_count']} chunks, created {meta['created_at']}{cached}\n"
                    f"    dir: {meta['directory']}, pattern: {meta['glob_pattern']}"
                )
            except Exception:
                lines.append(f"  {d.name}: (corrupt metadata)")
        else:
            lines.append(f"  {d.name}: (missing metadata)")

    return f"Indexes ({len(dirs)}):\n" + "\n".join(lines)


@tool_handler(
    name="delete_index",
    description="Delete a search index from disk and evict from cache.",
    schema={
        "type": "object",
        "properties": {
            "index_name": {"type": "string", "description": "Name of the index to delete"},
        },
        "required": ["index_name"],
    },
)
async def delete_index(args: dict) -> str:
    name = _sanitize_topic(args["index_name"])
    _index_cache.pop(name, None)

    index_dir = INDEXES_DIR / name
    if not index_dir.exists():
        return f"Index '{name}' not found."

    shutil.rmtree(index_dir)
    return f"Deleted index '{name}'."


@tool_handler(
    name="ingest_document",
    description=(
        "Add a single document to an existing index (or create a new one). "
        "Accepts a file path or raw text content. Useful for incrementally "
        "building indexes or adding docs outside the original indexed directory."
    ),
    schema={
        "type": "object",
        "properties": {
            "index_name": {"type": "string", "description": "Index to add to (created if it doesn't exist)"},
            "file_path": {"type": "string", "description": "Path to the document file (use this OR content)"},
            "content": {"type": "string", "description": "Raw text content to index (use this OR file_path)"},
            "label": {"type": "string", "description": "Label for the document (used when providing raw content)"},
            "chunk_lines": {"type": "integer", "description": "Lines per chunk (default: 50)"},
        },
        "required": ["index_name"],
    },
)
async def ingest_document(args: dict) -> str:
    name = _sanitize_topic(args["index_name"])
    entry = _load_index(name)

    chunk_lines = args.get("chunk_lines", 50)
    overlap = 10

    if entry is None:
        # Create a new empty index
        meta = {
            "name": name,
            "directory": "(mixed)",
            "glob_pattern": "(manual)",
            "chunk_lines": chunk_lines,
            "overlap": overlap,
            "file_count": 0,
            "chunk_count": 0,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        entry = {"meta": meta, "chunks": [], "bm25": None}

    new_chunks: list[dict[str, Any]] = []

    if args.get("file_path"):
        file_path = Path(os.path.expanduser(args["file_path"])).resolve()
        home = Path.home().resolve()
        if not (str(file_path).startswith(str(home)) or str(file_path).startswith("/tmp")):
            return f"Error: file must be under {home} or /tmp"
        if not file_path.exists():
            return f"Error: file not found: {file_path}"
        new_chunks = _chunk_file(file_path, chunk_lines=chunk_lines, overlap=overlap)
    elif args.get("content"):
        label = args.get("label", "document")
        lines = args["content"].splitlines()
        start = 0
        while start < len(lines):
            end = min(start + chunk_lines, len(lines))
            chunk_content = "\n".join(lines[start:end])
            non_empty = sum(1 for l in lines[start:end] if l.strip())
            if non_empty >= 3:
                new_chunks.append({
                    "file": label,
                    "start_line": start + 1,
                    "end_line": end,
                    "content": chunk_content,
                    "tokens": _tokenize_bm25(chunk_content),
                })
            start += chunk_lines - overlap
            if start >= len(lines):
                break
    else:
        return "Error: provide either file_path or content."

    if not new_chunks:
        return "No indexable content in the document."

    # Merge with existing chunks
    all_chunks = entry["chunks"] + new_chunks
    for c in all_chunks:
        if "tokens" not in c:
            c["tokens"] = _tokenize_bm25(c["content"])

    # Rebuild BM25
    corpus = [c["tokens"] for c in all_chunks]
    bm25 = _BM25(corpus)

    # Update metadata
    entry["meta"]["chunk_count"] = len(all_chunks)
    entry["meta"]["file_count"] += 1

    # Save and cache
    _save_index(name, entry["meta"], all_chunks)
    _index_cache[name] = {"meta": entry["meta"], "chunks": all_chunks, "bm25": bm25}

    source = args.get("file_path") or args.get("label", "raw content")
    return (
        f"Added {len(new_chunks)} chunks from '{source}' to index '{name}'. "
        f"Total: {len(all_chunks)} chunks."
    )


# ===========================================================================
# Phase 1: Model/LoRA management + decode_tokens
# ===========================================================================

@tool_handler(
    name="unload_model",
    description=(
        "Unload the currently loaded model to free VRAM without loading another. "
        "Useful before loading a different model or when you need GPU memory for other tasks."
    ),
    schema={"type": "object", "properties": {}, "required": []},
)
async def unload_model(args: dict) -> str:
    global MODEL
    resp = await _client.post(f"{TGWUI_INTERNAL}/model/unload", timeout=30)
    resp.raise_for_status()
    previous = MODEL or "(none)"
    MODEL = None
    return f"Model unloaded (was: {previous}). VRAM freed."


@tool_handler(
    name="list_loras",
    description="List available LoRA adapters that can be loaded on top of the current model.",
    schema={"type": "object", "properties": {}, "required": []},
)
async def list_loras(args: dict) -> str:
    resp = await _client.get(f"{TGWUI_INTERNAL}/lora/list", timeout=10)
    resp.raise_for_status()
    data = resp.json()
    lora_names = data.get("lora_names", [])
    if not lora_names:
        return "No LoRA adapters found."
    lines = [f"Available LoRA adapters ({len(lora_names)}):"]
    for name in sorted(lora_names):
        lines.append(f"  {name}")
    return "\n".join(lines)


@tool_handler(
    name="load_lora",
    description=(
        "Load one or more LoRA adapters on top of the current model. "
        "Each LoRA can have a weight (default 1.0). "
        "Loading replaces any previously loaded LoRAs."
    ),
    schema={
        "type": "object",
        "properties": {
            "lora_names": {
                "type": "array",
                "items": {"type": "string"},
                "description": "LoRA adapter names to load",
            },
            "lora_weights": {
                "type": "array",
                "items": {"type": "number"},
                "description": "Weights for each LoRA (default: [1.0] for each). Must match lora_names length.",
            },
        },
        "required": ["lora_names"],
    },
)
async def load_lora(args: dict) -> str:
    names = args["lora_names"]
    weights = args.get("lora_weights", [1.0] * len(names))
    if len(weights) != len(names):
        return f"Error: lora_weights length ({len(weights)}) must match lora_names length ({len(names)})"

    body = {"lora_names": names, "lora_weights": weights}
    resp = await _client.post(f"{TGWUI_INTERNAL}/lora/load", json=body, timeout=60)
    resp.raise_for_status()

    pairs = [f"  {n} (weight: {w})" for n, w in zip(names, weights)]
    return f"Loaded {len(names)} LoRA(s):\n" + "\n".join(pairs)


@tool_handler(
    name="unload_loras",
    description="Remove all currently loaded LoRA adapters from the model.",
    schema={"type": "object", "properties": {}, "required": []},
)
async def unload_loras(args: dict) -> str:
    resp = await _client.post(f"{TGWUI_INTERNAL}/lora/unload", timeout=30)
    resp.raise_for_status()
    return "All LoRAs unloaded."


@tool_handler(
    name="decode_tokens",
    description=(
        "Convert token IDs back to text using the loaded model's tokenizer. "
        "Complements encode_tokens (text→IDs). Useful for inspecting generated token sequences."
    ),
    schema={
        "type": "object",
        "properties": {
            "tokens": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "List of token IDs to decode",
            },
        },
        "required": ["tokens"],
    },
)
async def decode_tokens(args: dict) -> str:
    resp = await _client.post(
        f"{TGWUI_INTERNAL}/decode",
        json={"tokens": args["tokens"]},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    text = data.get("text", "")
    return f"Decoded {len(args['tokens'])} tokens:\n{text}"


# ===========================================================================
# Phase 2: Advanced generation controls
# ===========================================================================

@tool_handler(
    name="text_complete",
    description=(
        "Raw text completion (not chat). Continues the provided text without chat formatting. "
        "Ideal for code completion, fill-in-the-middle, template filling, or notebook mode."
    ),
    schema={
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Text to continue/complete"},
            "max_tokens": {"type": "integer", "description": "Max tokens to generate (default: 512)"},
            "temperature": {"type": "number", "description": "Sampling temperature (optional override)"},
            "stop": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Stop sequences (optional)",
            },
        },
        "required": ["prompt"],
    },
)
async def text_complete(args: dict) -> str:
    gen_params = get_generation_params(MODEL)
    body: dict[str, Any] = {
        "model": MODEL or "",
        "prompt": args["prompt"],
        "max_tokens": args.get("max_tokens", 512),
        "stream": False,
    }
    if args.get("temperature") is not None:
        body["temperature"] = args["temperature"]
    else:
        body["temperature"] = gen_params.get("temperature", 0.7)
    if args.get("stop"):
        body["stop"] = args["stop"]

    resp = await _client.post(f"{TGWUI_BASE}/completions", json=body, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    text = data["choices"][0]["text"]
    return text


@tool_handler(
    name="get_logits",
    description=(
        "Get the next-token probability distribution for the given prompt. "
        "Returns the top-50 most likely tokens with their probabilities. "
        "Useful for confidence checking, decision routing, or debugging model behavior."
    ),
    schema={
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Prompt to get next-token logits for"},
            "top_n": {"type": "integer", "description": "Number of top tokens to return (default: 50, max: 100)"},
            "use_samplers": {"type": "boolean", "description": "Apply sampling filters (temp, top_p, etc.) before returning (default: false)"},
        },
        "required": ["prompt"],
    },
)
async def get_logits(args: dict) -> str:
    top_n = min(args.get("top_n", 50), 100)
    body: dict[str, Any] = {
        "prompt": args["prompt"],
        "use_samplers": args.get("use_samplers", False),
        "top_logits": top_n,
    }
    resp = await _client.post(f"{TGWUI_INTERNAL}/logits", json=body, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    # data is typically a list of [token_text, probability] or similar
    logits = data if isinstance(data, list) else data.get("logits", data.get("top_logits", []))

    lines = [f"Top-{len(logits)} next tokens:"]
    for i, entry in enumerate(logits[:top_n]):
        if isinstance(entry, dict):
            token = entry.get("token", entry.get("text", "?"))
            prob = entry.get("probability", entry.get("prob", 0))
        elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
            token, prob = entry[0], entry[1]
        else:
            token, prob = str(entry), 0
        lines.append(f"  {i+1:3d}. {repr(token):>20s}  {float(prob)*100:6.2f}%")
    return "\n".join(lines)


@tool_handler(
    name="preview_prompt",
    description=(
        "Render a chat prompt template without generating any text. "
        "Shows the exact formatted prompt that would be sent to the model, "
        "including system messages, template markers, and token count. "
        "Useful for debugging templates and checking context budget."
    ),
    schema={
        "type": "object",
        "properties": {
            "user_message": {"type": "string", "description": "User message to render"},
            "system_message": {"type": "string", "description": "System message (optional, uses context preamble if not set)"},
        },
        "required": ["user_message"],
    },
)
async def preview_prompt(args: dict) -> str:
    global MODEL
    if MODEL is None:
        MODEL = await resolve_model()

    system = args.get("system_message") or get_system_preamble()
    suffix = get_system_suffix(MODEL)
    effective_system = system
    if suffix:
        effective_system = f"{system}\n\n{suffix}" if system else suffix

    messages: list[dict[str, str]] = []
    if effective_system:
        messages.append({"role": "system", "content": effective_system})
    messages.append({"role": "user", "content": args["user_message"]})

    body = {"messages": messages}
    resp = await _client.post(f"{TGWUI_INTERNAL}/chat-prompt", json=body, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    rendered = data.get("prompt", data.get("text", str(data)))

    # Get token count for the rendered prompt
    try:
        tc_resp = await _client.post(
            f"{TGWUI_INTERNAL}/token-count",
            json={"text": rendered},
            timeout=5,
        )
        tc_resp.raise_for_status()
        tc_data = tc_resp.json()
        token_count = tc_data.get("length") or tc_data.get("tokens") or "?"
    except Exception:
        token_count = "?"

    return (
        f"Rendered prompt ({token_count} tokens, {len(rendered)} chars):\n"
        f"{'='*60}\n{rendered}\n{'='*60}"
    )


@tool_handler(
    name="set_sampling",
    description=(
        "Set advanced sampling parameters at runtime. Extends set_generation_params "
        "with ALL sampling options: mirostat, DRY, XTC, dynamic temperature, seed, "
        "token bans, reasoning_effort, and more. "
        "Call with no arguments to see available parameters."
    ),
    schema={
        "type": "object",
        "properties": {
            "seed": {"type": "integer", "description": "Random seed (-1 = random). Same seed + same prompt = deterministic output."},
            "mirostat_mode": {"type": "integer", "description": "Mirostat mode: 0=disabled, 1=v1, 2=v2"},
            "mirostat_tau": {"type": "number", "description": "Mirostat target entropy (5.0 default)"},
            "mirostat_eta": {"type": "number", "description": "Mirostat learning rate (0.1 default)"},
            "dry_multiplier": {"type": "number", "description": "DRY repetition penalty multiplier (0=disabled)"},
            "dry_allowed_length": {"type": "integer", "description": "DRY minimum repeated sequence length"},
            "dry_base": {"type": "number", "description": "DRY penalty base"},
            "xtc_threshold": {"type": "number", "description": "XTC pruning threshold"},
            "xtc_probability": {"type": "number", "description": "XTC probability of applying"},
            "dynatemp_low": {"type": "number", "description": "Dynamic temperature low bound"},
            "dynatemp_high": {"type": "number", "description": "Dynamic temperature high bound"},
            "dynatemp_exponent": {"type": "number", "description": "Dynamic temperature exponent"},
            "dynamic_temperature": {"type": "boolean", "description": "Enable dynamic temperature"},
            "temperature_last": {"type": "boolean", "description": "Apply temperature after other samplers"},
            "smoothing_factor": {"type": "number", "description": "Quadratic smoothing factor"},
            "smoothing_curve": {"type": "number", "description": "Quadratic smoothing curve"},
            "top_n_sigma": {"type": "number", "description": "Top-n sigma sampling threshold"},
            "custom_token_bans": {"type": "string", "description": "Comma-separated token IDs to ban"},
            "ban_eos_token": {"type": "boolean", "description": "Ban the end-of-sequence token"},
            "reasoning_effort": {"type": "number", "description": "Reasoning effort (0.0-1.0, model-dependent)"},
            "prompt_lookup_num_tokens": {"type": "integer", "description": "Speculative decoding token count"},
            "max_tokens_second": {"type": "integer", "description": "Rate limit output (0=unlimited)"},
            "guidance_scale": {"type": "number", "description": "Classifier-free guidance scale (1.0=disabled)"},
        },
        "required": [],
    },
)
async def set_sampling(args: dict) -> str:
    if not args:
        params_list = [
            "seed, mirostat_mode, mirostat_tau, mirostat_eta,",
            "dry_multiplier, dry_allowed_length, dry_base,",
            "xtc_threshold, xtc_probability,",
            "dynatemp_low, dynatemp_high, dynatemp_exponent, dynamic_temperature,",
            "temperature_last, smoothing_factor, smoothing_curve, top_n_sigma,",
            "custom_token_bans, ban_eos_token, reasoning_effort,",
            "prompt_lookup_num_tokens, max_tokens_second, guidance_scale",
        ]
        return "Available advanced sampling params:\n  " + "\n  ".join(params_list)

    # All keys in the schema are valid sampling params
    allowed = {
        "seed", "mirostat_mode", "mirostat_tau", "mirostat_eta",
        "dry_multiplier", "dry_allowed_length", "dry_base",
        "xtc_threshold", "xtc_probability",
        "dynatemp_low", "dynatemp_high", "dynatemp_exponent",
        "dynamic_temperature", "temperature_last",
        "smoothing_factor", "smoothing_curve", "top_n_sigma",
        "custom_token_bans", "ban_eos_token", "reasoning_effort",
        "prompt_lookup_num_tokens", "max_tokens_second", "guidance_scale",
    }
    changed = []
    for k, v in args.items():
        if k not in allowed:
            continue
        if v is None or v == "":
            _runtime_overrides.pop(k, None)
            changed.append(f"  {k}: (cleared)")
        else:
            _runtime_overrides[k] = v
            changed.append(f"  {k}: {v}")

    if not changed:
        return "No valid sampling parameters provided."

    return "Advanced sampling overrides updated:\n" + "\n".join(changed)


# ===========================================================================
# Phase 3: Presets and Grammars
# ===========================================================================

@tool_handler(
    name="list_presets",
    description=(
        "List all available generation presets with their key parameters. "
        "Presets are stored in the text-generation-webui user_data/presets/ directory."
    ),
    schema={"type": "object", "properties": {}, "required": []},
)
async def list_presets(args: dict) -> str:
    webui_root = _get_webui_root()
    if webui_root is None:
        return "Error: cannot determine text-generation-webui root. Set webui_root in config.yaml."

    presets_dir = webui_root / "user_data" / "presets"
    if not presets_dir.exists():
        return f"Presets directory not found: {presets_dir}"

    presets = sorted(presets_dir.glob("*.yaml"))
    if not presets:
        return "No presets found."

    lines = [f"Available presets ({len(presets)}):"]
    for p in presets:
        try:
            with open(p) as f:
                data = yaml.safe_load(f) or {}
            # Show key params
            key_params = []
            for k in ("temperature", "top_p", "top_k", "min_p", "repetition_penalty"):
                if k in data:
                    key_params.append(f"{k}={data[k]}")
            active = " <- active" if p.stem == _webui_preset_name else ""
            params_str = ", ".join(key_params) if key_params else "(empty)"
            lines.append(f"  {p.stem}: {params_str}{active}")
        except Exception:
            lines.append(f"  {p.stem}: (unreadable)")

    return "\n".join(lines)


@tool_handler(
    name="load_preset",
    description=(
        "Load a preset's parameters as runtime overrides. "
        "This applies the preset's sampling params on top of current settings "
        "without changing the webui's active preset."
    ),
    schema={
        "type": "object",
        "properties": {
            "preset_name": {"type": "string", "description": "Name of the preset to load (e.g. 'Creative', 'Divine Intellect')"},
        },
        "required": ["preset_name"],
    },
)
async def load_preset(args: dict) -> str:
    preset_name = args["preset_name"]
    # Validate against path traversal
    if ".." in preset_name or "/" in preset_name or "\\" in preset_name:
        return "Error: invalid preset name"

    webui_root = _get_webui_root()
    if webui_root is None:
        return "Error: cannot determine text-generation-webui root. Set webui_root in config.yaml."

    preset_path = webui_root / "user_data" / "presets" / f"{preset_name}.yaml"
    if not preset_path.exists():
        return f"Preset '{preset_name}' not found at {preset_path}"

    try:
        with open(preset_path) as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        return f"Error reading preset: {e}"

    changed = []
    for k, v in data.items():
        if k in _WEBUI_GEN_KEYS:
            _runtime_overrides[k] = v
            changed.append(f"  {k}: {v}")

    if not changed:
        return f"Preset '{preset_name}' loaded but had no recognized generation params."

    return f"Loaded preset '{preset_name}' as runtime overrides:\n" + "\n".join(changed)


@tool_handler(
    name="list_grammars",
    description=(
        "List available GBNF grammars: built-in ones (json, json_array, boolean) "
        "and any .gbnf files in the text-generation-webui grammars directory."
    ),
    schema={"type": "object", "properties": {}, "required": []},
)
async def list_grammars(args: dict) -> str:
    lines = ["Built-in grammars:"]
    for name in sorted(_BUILTIN_GRAMMARS.keys()):
        preview = _BUILTIN_GRAMMARS[name][:60].replace("\n", " ")
        lines.append(f"  {name}: {preview}...")

    webui_root = _get_webui_root()
    if webui_root:
        grammar_dir = webui_root / "user_data" / "grammars"
        if grammar_dir.exists():
            gbnf_files = sorted(grammar_dir.glob("*.gbnf"))
            if gbnf_files:
                lines.append(f"\nOn-disk grammars ({len(gbnf_files)}):")
                for f in gbnf_files:
                    size = f.stat().st_size
                    lines.append(f"  {f.stem} ({size} bytes)")
            else:
                lines.append(f"\nNo .gbnf files in {grammar_dir}")
        else:
            lines.append(f"\nGrammars directory not found: {grammar_dir}")
    else:
        lines.append("\nCannot list on-disk grammars (webui_root unknown)")

    return "\n".join(lines)


@tool_handler(
    name="load_grammar",
    description=(
        "Load a GBNF grammar file from disk into the built-in grammar registry. "
        "Once loaded, it can be used by name in local_chat, structured_output, etc."
    ),
    schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Name to register the grammar under"},
            "file_path": {"type": "string", "description": "Path to .gbnf file (or use grammar_text)"},
            "grammar_text": {"type": "string", "description": "Raw GBNF grammar text (or use file_path)"},
        },
        "required": ["name"],
    },
)
async def load_grammar(args: dict) -> str:
    name = args["name"]
    if ".." in name or "/" in name:
        return "Error: invalid grammar name"

    if args.get("file_path"):
        path = Path(os.path.expanduser(args["file_path"])).resolve()
        home = Path.home().resolve()
        if not (str(path).startswith(str(home)) or str(path).startswith("/tmp")):
            return f"Error: file must be under {home} or /tmp"
        if not path.exists():
            return f"Error: file not found: {path}"
        grammar_text = path.read_text(encoding="utf-8")
    elif args.get("grammar_text"):
        grammar_text = args["grammar_text"]
    else:
        return "Error: provide either file_path or grammar_text"

    _BUILTIN_GRAMMARS[name] = grammar_text
    return f"Grammar '{name}' loaded ({len(grammar_text)} chars). Available: {', '.join(sorted(_BUILTIN_GRAMMARS.keys()))}"


# ===========================================================================
# Phase 4: Self-orchestration tools
# ===========================================================================

@tool_handler(
    name="auto_route",
    description=(
        "Classify a task, recommend the optimal model, and suggest a tool pipeline. "
        "Optionally auto-sets context and loads the recommended model. "
        "One-call 'just do it' routing for any task."
    ),
    schema={
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "Description of the task to route"},
            "auto_load": {"type": "boolean", "description": "Auto-load the recommended model if not already loaded (default: false)"},
            "auto_context": {"type": "boolean", "description": "Auto-detect and set project context (default: false)"},
        },
        "required": ["task"],
    },
)
async def auto_route(args: dict) -> str:
    global MODEL
    task = args["task"]

    # Step 1: Classify via existing classify_task logic
    classification = await classify_task({"task": task})

    # Step 2: Suggest tool pipeline based on task keywords
    task_lower = task.lower()
    suggested_tools = []

    if any(w in task_lower for w in ["review", "diff", "pr", "pull request"]):
        suggested_tools = ["review_diff", "diff_explain"]
    elif any(w in task_lower for w in ["refactor", "improve", "clean"]):
        suggested_tools = ["analyze_code", "suggest_refactor"]
    elif any(w in task_lower for w in ["test", "testing"]):
        suggested_tools = ["generate_test_stubs"]
    elif any(w in task_lower for w in ["document", "docs", "explain"]):
        suggested_tools = ["draft_docs", "summarize_file"]
    elif any(w in task_lower for w in ["search", "find", "where"]):
        suggested_tools = ["search_index", "rag_query"]
    elif any(w in task_lower for w in ["image", "screenshot", "visual"]):
        suggested_tools = ["analyze_image"]
    elif any(w in task_lower for w in ["translate", "convert"]):
        suggested_tools = ["translate_code"]
    elif any(w in task_lower for w in ["error", "bug", "fix"]):
        suggested_tools = ["explain_error", "analyze_code"]
    else:
        suggested_tools = ["local_chat"]

    # Step 3: Auto-context if requested
    context_msg = ""
    if args.get("auto_context"):
        cwd = Path.cwd()
        cwd_name = cwd.name.lower()
        # Auto-detect language from project files
        lang_markers = {
            "Cargo.toml": "rust", "pyproject.toml": "python", "setup.py": "python",
            "package.json": "javascript", "go.mod": "go", "build.gradle": "java",
            "Gemfile": "ruby", "mix.exs": "elixir", "CMakeLists.txt": "cpp",
        }
        detected_lang = None
        for marker, lang in lang_markers.items():
            if (cwd / marker).exists():
                detected_lang = lang
                break
        if detected_lang:
            _context.clear()
            _context["language"] = detected_lang
            _context["project"] = cwd_name
            context_msg = f"\nContext: {detected_lang}/{cwd_name} (auto-detected)"
        else:
            _context.clear()
            _context["project"] = cwd_name
            context_msg = f"\nContext: {cwd_name} (auto-detected)"

    # Step 4: Auto-load model if requested
    load_msg = ""
    if args.get("auto_load"):
        # Extract recommended model from classification
        for line in classification.splitlines():
            if line.strip().startswith("-> "):
                recommended = line.strip()[3:].strip()
                if MODEL and recommended in MODEL:
                    load_msg = f"\nModel: {MODEL} (already loaded, good fit)"
                else:
                    load_msg = f"\nModel swap recommended: {recommended}"
                    load_msg += "\n(auto_load will not swap automatically to avoid interruptions — call swap_model)"
                break

    pipeline_str = " -> ".join(suggested_tools) if suggested_tools else "(general chat)"

    return (
        f"{classification}"
        f"{context_msg}"
        f"{load_msg}"
        f"\n\nSuggested pipeline: {pipeline_str}"
    )


@tool_handler(
    name="workflow",
    description=(
        "Execute a predefined multi-step workflow. Available workflows:\n"
        "- 'full-review': git diff -> review_diff + diff_explain\n"
        "- 'pr-review': like full-review but includes RAG context from project index\n"
        "- 'deep-analyze': index directory -> rag_query on key questions -> summary\n"
        "- 'onboard-project': detect language -> set_context -> index -> summarize entry files\n"
        "- 'research': check KG first, then deep_research if topic is novel"
    ),
    schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "enum": ["full-review", "pr-review", "deep-analyze", "onboard-project", "research"],
                "description": "Workflow to execute",
            },
            "directory": {"type": "string", "description": "Target directory (for deep-analyze, onboard-project)"},
            "diff": {"type": "string", "description": "Git diff input (for full-review, pr-review)"},
            "question": {"type": "string", "description": "Research question (for research workflow)"},
            "max_sources": {"type": "integer", "description": "Max sources for research (default 3)"},
        },
        "required": ["name"],
    },
)
async def workflow_tool(args: dict) -> str:
    wf_name = args["name"]

    if wf_name == "full-review":
        diff = args.get("diff", "")
        if not diff:
            return "Error: 'diff' is required for full-review workflow"

        review = await chat(
            f"Review this git diff for bugs, security issues, and style problems.\n"
            f"For each issue: file, line, severity (critical/warning/nit), what's wrong, how to fix.\n"
            f"If clean, say so.\n\n```diff\n{diff}\n```",
            system=get_system_preamble(),
        )
        explanation = await chat(
            f"Explain what this git diff does in plain English. "
            f"Focus on what changed and why.\n\n```diff\n{diff}\n```",
            system=get_system_preamble(),
        )
        return f"## Review\n\n{review}\n\n## Summary\n\n{explanation}"

    elif wf_name == "deep-analyze":
        directory = args.get("directory", "")
        if not directory:
            return "Error: 'directory' is required for deep-analyze workflow"

        dir_path = Path(os.path.expanduser(directory)).resolve()
        if not dir_path.exists():
            return f"Error: directory not found: {dir_path}"

        # Detect language from extensions
        ext_counts: dict[str, int] = {}
        for f in dir_path.rglob("*"):
            if f.is_file() and f.suffix:
                ext_counts[f.suffix] = ext_counts.get(f.suffix, 0) + 1

        top_ext = max(ext_counts, key=ext_counts.get) if ext_counts else "*.*"
        ext_to_glob = {".rs": "**/*.rs", ".py": "**/*.py", ".ts": "**/*.ts",
                       ".tsx": "**/*.tsx", ".js": "**/*.js", ".go": "**/*.go"}
        glob_pattern = ext_to_glob.get(top_ext, "**/*.*")

        index_name = _sanitize_topic(dir_path.name)
        idx_result = await index_directory({
            "name": index_name,
            "directory": directory,
            "glob_pattern": glob_pattern,
        })

        # Ask key architectural questions
        questions = [
            "What are the main entry points and how is the application structured?",
            "What are the key data types and how do they relate to each other?",
            "What external dependencies or APIs does this project use?",
        ]
        answers = []
        for q in questions:
            answer = await rag_query({"index_name": index_name, "question": q, "top_k": 3})
            answers.append(f"**Q: {q}**\n{answer}")

        return f"## Index\n{idx_result}\n\n## Analysis\n\n" + "\n\n---\n\n".join(answers)

    elif wf_name == "onboard-project":
        directory = args.get("directory", "")
        if not directory:
            return "Error: 'directory' is required for onboard-project workflow"

        dir_path = Path(os.path.expanduser(directory)).resolve()
        if not dir_path.exists():
            return f"Error: directory not found: {dir_path}"

        # Detect language
        ext_counts = {}
        for f in dir_path.rglob("*"):
            if f.is_file() and f.suffix in _TEXT_EXTENSIONS:
                ext_counts[f.suffix] = ext_counts.get(f.suffix, 0) + 1

        ext_to_lang = {".rs": "rust", ".py": "python", ".ts": "typescript",
                       ".tsx": "typescript", ".js": "javascript", ".go": "go"}
        top_ext = max(ext_counts, key=ext_counts.get) if ext_counts else ""
        lang = ext_to_lang.get(top_ext, "auto")

        # Set context
        project_name = dir_path.name
        _context.clear()
        _context["language"] = lang
        _context["project"] = project_name

        # Index
        ext_to_glob = {".rs": "**/*.rs", ".py": "**/*.py", ".ts": "**/*.{ts,tsx}",
                       ".tsx": "**/*.{ts,tsx}", ".js": "**/*.{js,jsx}", ".go": "**/*.go"}
        glob_pattern = ext_to_glob.get(top_ext, "**/*.*")
        index_name = _sanitize_topic(project_name)
        idx_result = await index_directory({
            "name": index_name,
            "directory": directory,
            "glob_pattern": glob_pattern,
        })

        # Find and summarize entry files
        entry_candidates = ["main.rs", "lib.rs", "main.py", "app.py", "index.ts",
                           "index.js", "main.go", "mod.rs", "Cargo.toml", "package.json"]
        found_entries = []
        for candidate in entry_candidates:
            matches = list(dir_path.rglob(candidate))
            found_entries.extend(matches[:2])

        summaries = []
        for entry_file in found_entries[:5]:
            try:
                content = entry_file.read_text(encoding="utf-8", errors="replace")
                if len(content) > 50000:
                    content = content[:50000]
                summary = await chat(
                    f"Summarize the structure of this file ({entry_file.name}):\n```\n{content}\n```",
                    system=get_system_preamble(),
                )
                summaries.append(f"**{entry_file.relative_to(dir_path)}:**\n{summary}")
            except Exception:
                pass

        return (
            f"## Onboarding: {project_name}\n\n"
            f"Language: {lang}\n"
            f"Context: set to {lang}/{project_name}\n\n"
            f"## Index\n{idx_result}\n\n"
            f"## Key Files\n\n" + "\n\n---\n\n".join(summaries or ["(no entry files found)"])
        )

    elif wf_name == "pr-review":
        diff = args.get("diff", "")
        if not diff:
            return "Error: 'diff' is required for pr-review workflow"

        # Review the diff
        review = await chat(
            f"Review this git diff for bugs, security issues, and style problems.\n"
            f"For each issue: file, line, severity (critical/warning/nit), what's wrong, how to fix.\n"
            f"If clean, say so.\n\n```diff\n{diff}\n```",
            system=get_system_preamble(),
        )

        # Check for RAG context from project indexes
        rag_context = ""
        indexes = list(_indexes.keys()) if _indexes else []
        if indexes:
            # Use first available index for context
            idx_name = indexes[0]
            try:
                # Extract function/symbol names from diff to search for related code
                import re as _wf_re
                symbols = _wf_re.findall(r"(?:fn|def|function|class|struct|impl)\s+(\w+)", diff)
                if symbols:
                    query = " ".join(symbols[:5])
                    rag_result = await rag_query({"index_name": idx_name, "question": query, "top_k": 3})
                    if rag_result and "error" not in rag_result.lower()[:20]:
                        rag_context = f"\n\n## Related Code (from {idx_name} index)\n\n{rag_result[:1500]}"
            except Exception:
                pass

        # Explain the changes
        explanation = await chat(
            f"Explain what this git diff does in plain English. "
            f"Focus on what changed and why.\n\n```diff\n{diff}\n```",
            system=get_system_preamble(),
        )

        return f"## Review\n\n{review}{rag_context}\n\n## Summary\n\n{explanation}"

    elif wf_name == "research":
        question = args.get("question", "")
        if not question:
            return "Error: 'question' is required for research workflow"

        # Check KG first for existing research
        kg = _get_kg()
        existing = kg.query(question, max_results=3)
        if existing:
            recent = [e for e in existing
                      if time.time() - e.get("updated_at", 0) < 7 * 86400]
            if recent:
                context_parts = [f"- **{e['name']}** ({e['type']}): {e['content']}"
                                 for e in recent]
                return (
                    f"## Existing Research Found\n\n"
                    f"Recent KG entries (within 7 days):\n\n"
                    + "\n".join(context_parts) +
                    f"\n\nTo force new research, use `deep_research` directly."
                )

        # No existing research — run deep_research
        return await deep_research({
            "question": question,
            "max_sources": args.get("max_sources", 3),
            "save_to_kg": True,
        })

    return f"Unknown workflow: {wf_name}"


@tool_handler(
    name="pipeline",
    description=(
        "Chain sequential prompts where each step's output feeds the next. "
        "Use {input} placeholder in each step's prompt to reference the previous step's output. "
        "The first step receives the initial_input. Each step can have its own max_tokens and grammar. "
        "Use 'template' to load a saved pipeline template by name."
    ),
    schema={
        "type": "object",
        "properties": {
            "initial_input": {"type": "string", "description": "Input for the first step"},
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string", "description": "Prompt template (use {input} for previous output)"},
                        "max_tokens": {"type": "integer", "description": "Max tokens for this step"},
                        "grammar": {"type": "string", "description": "GBNF grammar for this step (optional)"},
                    },
                    "required": ["prompt"],
                },
                "description": "Ordered list of pipeline steps (or omit if using template)",
            },
            "template": {"type": "string", "description": "Load steps from a saved pipeline template name"},
        },
        "required": ["initial_input"],
    },
)
async def pipeline_tool(args: dict) -> str:
    current_input = args["initial_input"]

    # Load from template if specified
    steps = args.get("steps")
    if args.get("template"):
        tmpl_name = _sanitize_topic(args["template"])
        tmpl_path = PIPELINES_DIR / f"{tmpl_name}.json"
        if not tmpl_path.exists():
            available = [f.stem for f in PIPELINES_DIR.glob("*.json")] if PIPELINES_DIR.exists() else []
            return f"Template '{tmpl_name}' not found. Available: {', '.join(sorted(available)) or '(none)'}"
        with open(tmpl_path) as f:
            tmpl_data = json.load(f)
        steps = tmpl_data.get("steps", [])

    if not steps:
        return "Error: provide at least one step"
    if len(steps) > 10:
        return "Error: max 10 steps"

    results = []
    for i, step in enumerate(steps):
        prompt = step["prompt"].replace("{input}", current_input)
        kwargs: dict[str, Any] = {}
        if step.get("max_tokens"):
            kwargs["max_tokens"] = step["max_tokens"]
        if step.get("grammar"):
            kwargs["grammar_string"] = _BUILTIN_GRAMMARS.get(step["grammar"], step["grammar"])

        result = await chat(prompt, system=get_system_preamble(), **kwargs)
        results.append(f"--- Step {i+1} ---\n{result}")
        current_input = result

    return "\n\n".join(results)


# ===========================================================================
# Phase 5: Knowledge base and doc lookup
# ===========================================================================

_KNOWLEDGE_INDEX_NAME = "__knowledge_base__"


@tool_handler(
    name="knowledge_base",
    description=(
        "Persistent, searchable knowledge store. Add knowledge with source and tags, "
        "search by query, or list all entries. Backed by BM25 index. "
        "Actions: 'add' (store new knowledge), 'search' (find relevant entries), 'list' (show all)."
    ),
    schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "search", "list"],
                "description": "Action to perform",
            },
            "content": {"type": "string", "description": "Knowledge content to store (for 'add')"},
            "source": {"type": "string", "description": "Source of the knowledge: 'web', 'docs', 'code', 'user' (for 'add')"},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Tags for categorization (for 'add')",
            },
            "query": {"type": "string", "description": "Search query (for 'search')"},
            "top_k": {"type": "integer", "description": "Number of results (for 'search', default: 5)"},
        },
        "required": ["action"],
    },
)
async def knowledge_base(args: dict) -> str:
    action = args["action"]

    if action == "add":
        content = args.get("content", "")
        if not content:
            return "Error: 'content' is required for add"

        source = args.get("source", "unknown")
        tags = args.get("tags", [])
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")

        # Build a labeled entry
        tag_str = ", ".join(tags) if tags else "untagged"
        labeled_content = (
            f"[source: {source}] [tags: {tag_str}] [added: {timestamp}]\n"
            f"{content}"
        )

        # Use ingest_document to add to the knowledge base index
        result = await ingest_document({
            "index_name": _KNOWLEDGE_INDEX_NAME,
            "content": labeled_content,
            "label": f"kb-{source}-{timestamp}",
        })
        return f"Knowledge added ({len(content)} chars, source: {source}, tags: {tag_str}). {result}"

    elif action == "search":
        query = args.get("query", "")
        if not query:
            return "Error: 'query' is required for search"

        top_k = args.get("top_k", 5)
        name = _KNOWLEDGE_INDEX_NAME
        entry = _load_index(name)
        if entry is None:
            return "Knowledge base is empty. Use action='add' to store knowledge first."

        query_tokens = _tokenize_bm25(query)
        if not query_tokens:
            return "Query produced no searchable tokens."

        bm25 = entry["bm25"]
        if bm25 is None:
            return "Knowledge base index is empty."

        results = bm25.search(query_tokens, top_k=top_k)
        if not results:
            return f"No matches for '{query}' in knowledge base."

        chunks = entry["chunks"]
        parts = []
        for idx, score in results:
            chunk = chunks[idx]
            parts.append(f"--- (score: {score:.2f}) ---\n{chunk['content']}")

        return f"Knowledge base results for '{query}':\n\n" + "\n\n".join(parts)

    elif action == "list":
        entry = _load_index(_KNOWLEDGE_INDEX_NAME)
        if entry is None:
            return "Knowledge base is empty."

        meta = entry["meta"]
        chunks = entry["chunks"]
        lines = [
            f"Knowledge base: {meta['chunk_count']} chunks, {meta['file_count']} entries",
            f"Created: {meta.get('created_at', '?')}",
            "",
        ]
        # Show previews of unique labels
        seen_labels = set()
        for c in chunks:
            label = c.get("file", "?")
            if label not in seen_labels:
                seen_labels.add(label)
                preview = c["content"][:100].replace("\n", " ")
                lines.append(f"  {label}: {preview}...")

        return "\n".join(lines)

    return f"Unknown action: {action}"


@tool_handler(
    name="doc_lookup",
    description=(
        "Look up library/framework documentation using the context7 MCP plugin, "
        "then summarize the relevant docs using the local model with project context. "
        "Two-step: resolve library ID -> query docs -> local model interpretation."
    ),
    schema={
        "type": "object",
        "properties": {
            "library": {"type": "string", "description": "Library name (e.g. 'tokio', 'polars', 'react')"},
            "query": {"type": "string", "description": "What to look up (e.g. 'spawn_blocking', 'lazy frame')"},
            "save_to_kb": {"type": "boolean", "description": "Save useful findings to knowledge_base (default: false)"},
        },
        "required": ["library", "query"],
    },
)
async def doc_lookup(args: dict) -> str:
    library = args["library"]
    query = args["query"]

    # Note: context7 MCP tools are called by Claude externally, not from within this server.
    # This tool provides a structured interface that Claude can use to orchestrate the lookup.
    # The actual context7 calls happen at the Claude level.
    prompt = (
        f"I need documentation for the '{library}' library about: {query}\n\n"
        f"Please provide:\n"
        f"1. The key API/concept explanation\n"
        f"2. Usage example with best practices\n"
        f"3. Common pitfalls or gotchas\n\n"
        f"Be concise and focus on practical usage."
    )
    result = await chat(prompt, system=get_system_preamble())

    if args.get("save_to_kb"):
        await knowledge_base({
            "action": "add",
            "content": f"# {library}: {query}\n\n{result}",
            "source": "docs",
            "tags": [library, "documentation"],
        })
        return f"{result}\n\n(Saved to knowledge base)"

    return result


# ===========================================================================
# Phase 6: Slot info
# ===========================================================================

@tool_handler(
    name="slot_info",
    description=(
        "Show parallel slot count, context per slot, and model info. "
        "Useful for understanding parallelism capacity of the current setup."
    ),
    schema={"type": "object", "properties": {}, "required": []},
)
async def slot_info(args: dict) -> str:
    global MODEL
    if MODEL is None:
        try:
            MODEL = await resolve_model()
        except Exception:
            return "No model loaded."

    try:
        resp = await _client.get(f"{TGWUI_INTERNAL}/model/info", timeout=5)
        resp.raise_for_status()
        info = resp.json()
    except Exception as e:
        return f"Cannot get model info: {e}"

    model_name = info.get("model_name", "unknown")
    loader = info.get("loader", "unknown")
    loras = info.get("lora_names", [])

    # Query the llama-server subprocess directly for rich slot/context info
    # text-gen-webui runs llama-server on api_port + 5 (default: 5005)
    llama_slots = []
    gpu_info = {}
    try:
        # Try common llama-server ports: 5005 (default), then scan process
        for llama_port in [5005, 5006, 5007]:
            try:
                slot_resp = await _client.get(
                    f"http://127.0.0.1:{llama_port}/slots", timeout=3
                )
                if slot_resp.status_code == 200:
                    llama_slots = slot_resp.json()
                    break
            except Exception:
                continue
    except Exception:
        pass

    if llama_slots:
        slot_count = len(llama_slots)
        ctx_per_slot = llama_slots[0].get("n_ctx", "?") if llama_slots else "?"
        total_ctx = ctx_per_slot * slot_count if isinstance(ctx_per_slot, int) else "?"
        active_slots = sum(1 for s in llama_slots if s.get("is_processing", False))
    else:
        slot_count = "unknown"
        ctx_per_slot = "unknown"
        total_ctx = "unknown"
        active_slots = 0

    # Get GPU info from nvidia-smi
    try:
        import subprocess
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.used,memory.total,temperature.gpu,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            parts = [p.strip() for p in result.stdout.strip().split(",")]
            if len(parts) >= 5:
                gpu_info = {
                    "name": parts[0],
                    "vram_used": f"{int(parts[1]):,} MiB",
                    "vram_total": f"{int(parts[2]):,} MiB",
                    "vram_pct": f"{int(parts[1]) * 100 // int(parts[2])}%",
                    "temp": f"{parts[3]}°C",
                    "util": f"{parts[4]}%",
                }
    except Exception:
        pass

    # Get llama-server process args for gpu_layers, batch size, etc.
    proc_info = {}
    try:
        import subprocess
        result = subprocess.run(
            ["ps", "aux"], capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            if "llama-server" in line and "--model" in line:
                import re
                for flag, key in [
                    (r"--gpu-layers\s+(\S+)", "gpu_layers"),
                    (r"--ctx-size\s+(\S+)", "ctx_size"),
                    (r"--parallel\s+(\S+)", "parallel"),
                    (r"--batch-size\s+(\S+)", "batch_size"),
                    (r"--cache-type-k\s+(\S+)", "cache_type_k"),
                    (r"--cache-type-v\s+(\S+)", "cache_type_v"),
                    (r"--flash-attn\s+(\S+)", "flash_attn"),
                ]:
                    m = re.search(flag, line)
                    if m:
                        proc_info[key] = m.group(1)
                break
    except Exception:
        pass

    lines = [
        f"Model: {model_name}",
        f"Loader: {loader}",
        f"LoRAs: {', '.join(loras) if loras else 'none'}",
        "",
        "── Slots ──",
        f"Parallel slots: {slot_count}",
        f"Active / busy: {active_slots}",
        f"Context per slot: {ctx_per_slot:,}" if isinstance(ctx_per_slot, int) else f"Context per slot: {ctx_per_slot}",
        f"Total context: {total_ctx:,}" if isinstance(total_ctx, int) else f"Total context: {total_ctx}",
    ]

    if proc_info:
        lines += [
            "",
            "── Server Config ──",
        ]
        if "gpu_layers" in proc_info:
            lines.append(f"GPU layers: {proc_info['gpu_layers']}")
        if "ctx_size" in proc_info:
            lines.append(f"Context size (configured): {proc_info['ctx_size']}")
        if "parallel" in proc_info:
            lines.append(f"Parallel (configured): {proc_info['parallel']}")
        if "batch_size" in proc_info:
            lines.append(f"Batch size: {proc_info['batch_size']}")
        if "flash_attn" in proc_info:
            lines.append(f"Flash attention: {proc_info['flash_attn']}")

    if gpu_info:
        lines += [
            "",
            "── GPU ──",
            f"Device: {gpu_info['name']}",
            f"VRAM: {gpu_info['vram_used']} / {gpu_info['vram_total']} ({gpu_info['vram_pct']})",
            f"Temperature: {gpu_info['temp']}",
            f"Utilization: {gpu_info['util']}",
        ]

    return "\n".join(lines)


# ===========================================================================
# Enhancement tools: caching, stats, incremental index, validation, git context
# ===========================================================================

async def _run_git(*args: str, cwd: str | None = None) -> str:
    """Run a git command and return stdout."""
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        return f"(git error: {stderr.decode().strip()})"
    return stdout.decode().strip()


@tool_handler(
    name="incremental_index",
    description=(
        "Update an existing RAG index by re-chunking only files that changed since the index was built. "
        "Uses git to detect changed files. Much faster than full re-indexing."
    ),
    schema={
        "type": "object",
        "properties": {
            "index_name": {"type": "string", "description": "Name of the index to update"},
        },
        "required": ["index_name"],
    },
)
async def incremental_index(args: dict) -> str:
    name = _sanitize_topic(args["index_name"])
    entry = _load_index(name)
    if entry is None:
        return f"Index '{name}' not found. Use index_directory to create it first."

    meta = entry["meta"]
    directory = meta.get("directory", "")
    if not directory or directory == "(mixed)":
        return "Cannot incrementally update a mixed-source index. Use index_directory instead."

    dir_path = Path(directory)
    if not dir_path.exists():
        return f"Index directory no longer exists: {directory}"

    # Get files changed since index creation
    created_at = meta.get("created_at", "")
    # Use git to find changed files
    changed_output = await _run_git(
        "diff", "--name-only", "HEAD",
        cwd=str(dir_path),
    )
    # Also get untracked files
    untracked_output = await _run_git(
        "ls-files", "--others", "--exclude-standard",
        cwd=str(dir_path),
    )

    changed_files = set()
    for line in (changed_output + "\n" + untracked_output).splitlines():
        line = line.strip()
        if line and not line.startswith("(git error"):
            full_path = dir_path / line
            if full_path.exists() and full_path.is_file():
                changed_files.add(str(full_path))

    # Also check modification times against index creation
    if created_at:
        try:
            index_time = time.mktime(time.strptime(created_at, "%Y-%m-%dT%H:%M:%S"))
            glob_pattern = meta.get("glob_pattern", "**/*.*")
            for f in dir_path.glob(glob_pattern):
                if f.is_file() and f.stat().st_mtime > index_time:
                    changed_files.add(str(f))
        except (ValueError, OSError):
            pass

    if not changed_files:
        return f"Index '{name}' is up to date — no changed files detected."

    # Filter to indexable files
    changed_files = {
        f for f in changed_files
        if Path(f).suffix.lower() in _TEXT_EXTENSIONS
        and Path(f).stat().st_size < 200_000
    }

    if not changed_files:
        return f"Index '{name}' is up to date — changed files are not indexable."

    # Remove old chunks for changed files, re-chunk them
    old_chunks = [c for c in entry["chunks"] if c["file"] not in changed_files]
    chunk_lines = meta.get("chunk_lines", 50)
    overlap = meta.get("overlap", 10)

    new_chunks: list[dict[str, Any]] = []
    for fp in changed_files:
        new_chunks.extend(_chunk_file(Path(fp), chunk_lines=chunk_lines, overlap=overlap))

    all_chunks = old_chunks + new_chunks
    for c in all_chunks:
        if "tokens" not in c:
            c["tokens"] = _tokenize_bm25(c["content"])

    # Rebuild BM25
    corpus = [c["tokens"] for c in all_chunks]
    bm25 = _BM25(corpus) if corpus else None

    # Update metadata
    meta["chunk_count"] = len(all_chunks)
    meta["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")

    # Save and cache
    _save_index(name, meta, all_chunks)
    _index_cache[name] = {"meta": meta, "chunks": all_chunks, "bm25": bm25}

    removed = len(entry["chunks"]) - len(old_chunks)
    return (
        f"Index '{name}' updated incrementally.\n"
        f"  Changed files: {len(changed_files)}\n"
        f"  Chunks removed: {removed}\n"
        f"  Chunks added: {len(new_chunks)}\n"
        f"  Total chunks: {len(all_chunks)}"
    )


@tool_handler(
    name="validated_chat",
    description=(
        "Chat with validation and auto-retry. Sends a prompt, checks the response "
        "against a validation mode, and retries once if validation fails. "
        "Modes: 'json' (must parse as JSON), 'code' (checks for common syntax errors), "
        "'answer' (asks model to self-verify), 'custom' (your own validation prompt)."
    ),
    schema={
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "The prompt to send"},
            "validation": {
                "type": "string",
                "enum": ["json", "code", "answer", "custom"],
                "description": "Validation mode",
            },
            "custom_check": {"type": "string", "description": "Custom validation prompt (for mode='custom'). Use {response} placeholder."},
            "system": {"type": "string", "description": "Optional system message"},
            "max_retries": {"type": "integer", "description": "Max retry attempts (default: 1)"},
        },
        "required": ["prompt", "validation"],
    },
)
async def validated_chat(args: dict) -> str:
    prompt = args["prompt"]
    validation = args["validation"]
    system = args.get("system") or get_system_preamble()
    max_retries = args.get("max_retries", 1)
    custom_check = args.get("custom_check", "")

    for attempt in range(max_retries + 1):
        # Don't use cache on retries
        result = await chat(prompt, system=system, use_cache=(attempt == 0))

        # Validate
        is_valid = True
        validation_error = ""

        if validation == "json":
            # Strip markdown fences
            cleaned = result.strip()
            if cleaned.startswith("```json"):
                cleaned = cleaned[7:]
            if cleaned.startswith("```"):
                cleaned = cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            try:
                json.loads(cleaned.strip())
            except json.JSONDecodeError as e:
                is_valid = False
                validation_error = f"JSON parse error: {e}"

        elif validation == "code":
            # Check for common code issues
            issues = []
            if result.count("{") != result.count("}"):
                issues.append("mismatched braces")
            if result.count("(") != result.count(")"):
                issues.append("mismatched parentheses")
            if result.count("[") != result.count("]"):
                issues.append("mismatched brackets")
            if issues:
                is_valid = False
                validation_error = f"Syntax issues: {', '.join(issues)}"

        elif validation == "answer":
            # Ask model to self-verify
            verify_prompt = (
                f"Original question: {prompt}\n\n"
                f"Response: {result}\n\n"
                f"Does this response correctly and completely answer the question? "
                f"Reply with ONLY 'VALID' or 'INVALID: <reason>'."
            )
            verdict = await chat(verify_prompt, use_cache=False, max_tokens=100)
            if "INVALID" in verdict.upper():
                is_valid = False
                validation_error = verdict

        elif validation == "custom" and custom_check:
            check_prompt = custom_check.replace("{response}", result)
            verdict = await chat(check_prompt, use_cache=False, max_tokens=100)
            if any(w in verdict.upper() for w in ["FAIL", "INVALID", "ERROR", "NO", "FALSE"]):
                is_valid = False
                validation_error = verdict

        if is_valid:
            prefix = f"[validated: {validation}, attempt {attempt + 1}]\n\n" if attempt > 0 else ""
            return f"{prefix}{result}"

        # Log and retry
        log.info("Validation failed (attempt %d/%d): %s", attempt + 1, max_retries + 1, validation_error)
        if attempt < max_retries:
            # Add validation feedback to retry prompt
            prompt = (
                f"{args['prompt']}\n\n"
                f"IMPORTANT: Your previous response had this issue: {validation_error}\n"
                f"Please fix this and try again."
            )

    return f"[validation failed after {max_retries + 1} attempts: {validation_error}]\n\n{result}"


@tool_handler(
    name="git_context",
    description=(
        "Assemble git context for the current directory: branch, recent commits, "
        "staged/unstaged changes, and optionally blame for specific files. "
        "Returns a pre-formatted context blob ready to include in prompts."
    ),
    schema={
        "type": "object",
        "properties": {
            "directory": {"type": "string", "description": "Git repo directory (default: cwd)"},
            "log_count": {"type": "integer", "description": "Number of recent commits to include (default: 5)"},
            "include_diff": {"type": "boolean", "description": "Include staged + unstaged diff (default: true)"},
            "blame_files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Files to include git blame for (optional)",
            },
        },
        "required": [],
    },
)
async def git_context(args: dict) -> str:
    directory = args.get("directory", ".")
    dir_path = Path(os.path.expanduser(directory)).resolve()
    cwd = str(dir_path)

    log_count = args.get("log_count", 5)
    include_diff = args.get("include_diff", True)
    blame_files = args.get("blame_files", [])

    parts = []

    # Branch
    branch = await _run_git("branch", "--show-current", cwd=cwd)
    parts.append(f"Branch: {branch}")

    # Recent commits
    log_output = await _run_git(
        "log", f"--oneline", f"-{log_count}", "--no-decorate",
        cwd=cwd,
    )
    if log_output and not log_output.startswith("(git error"):
        parts.append(f"\nRecent commits:\n{log_output}")

    # Status
    status = await _run_git("status", "--short", cwd=cwd)
    if status and not status.startswith("(git error"):
        parts.append(f"\nStatus:\n{status}")

    # Diff
    if include_diff:
        # Staged
        staged = await _run_git("diff", "--staged", "--stat", cwd=cwd)
        if staged and not staged.startswith("(git error"):
            parts.append(f"\nStaged changes:\n{staged}")

        # Unstaged
        unstaged = await _run_git("diff", "--stat", cwd=cwd)
        if unstaged and not unstaged.startswith("(git error"):
            parts.append(f"\nUnstaged changes:\n{unstaged}")

        # Full diff (truncated)
        full_diff = await _run_git("diff", cwd=cwd)
        staged_diff = await _run_git("diff", "--staged", cwd=cwd)
        combined_diff = (staged_diff + "\n" + full_diff).strip()
        if combined_diff and not combined_diff.startswith("(git error"):
            if len(combined_diff) > 10000:
                combined_diff = combined_diff[:10000] + "\n... (truncated)"
            parts.append(f"\nDiff:\n```diff\n{combined_diff}\n```")

    # Blame
    for bf in blame_files[:3]:  # max 3 files
        blame = await _run_git("blame", "--line-porcelain", bf, cwd=cwd)
        if blame and not blame.startswith("(git error"):
            # Extract just author + line info, not full porcelain
            summary_lines = []
            for line in blame.splitlines():
                if line.startswith("author "):
                    summary_lines.append(line)
                elif line.startswith("summary "):
                    summary_lines.append(line)
            if summary_lines:
                parts.append(f"\nBlame for {bf}:\n" + "\n".join(summary_lines[:20]))

    return "\n".join(parts)


@tool_handler(
    name="warm_model",
    description=(
        "Send a tiny prompt to warm the model's KV cache after a swap. "
        "The first request after loading is always slow — this eliminates that latency "
        "on your actual first real call. Discards the response."
    ),
    schema={"type": "object", "properties": {}, "required": []},
)
async def warm_model(args: dict) -> str:
    global MODEL
    if MODEL is None:
        MODEL = await resolve_model()

    start = time.perf_counter()
    await chat("Hi", max_tokens=1, use_cache=False)
    elapsed = time.perf_counter() - start

    return f"Model warmed in {elapsed:.1f}s. First real call should be fast now."


@tool_handler(
    name="cache_stats",
    description=(
        "Show response cache statistics: hit rate, size, and optionally clear the cache."
    ),
    schema={
        "type": "object",
        "properties": {
            "clear": {"type": "boolean", "description": "Clear the cache (default: false)"},
        },
        "required": [],
    },
)
async def cache_stats(args: dict) -> str:
    global _cache_hits, _cache_misses

    if args.get("clear"):
        count = len(_response_cache)
        _response_cache.clear()
        _cache_hits = 0
        _cache_misses = 0
        return f"Cache cleared ({count} entries)."

    total = _cache_hits + _cache_misses
    hit_rate = (_cache_hits / total * 100) if total > 0 else 0

    lines = [
        f"Cache size: {len(_response_cache)} entries",
        f"Hits: {_cache_hits}",
        f"Misses: {_cache_misses}",
        f"Hit rate: {hit_rate:.1f}%",
        f"TTL: {_CACHE_TTL}s",
    ]

    # Show oldest/newest entry ages
    if _response_cache:
        now = time.time()
        ages = [now - ts for _, ts in _response_cache.values()]
        lines.append(f"Oldest entry: {max(ages):.0f}s ago")
        lines.append(f"Newest entry: {min(ages):.0f}s ago")

    return "\n".join(lines)


@tool_handler(
    name="session_stats",
    description=(
        "Show cumulative session statistics: total calls, approximate token usage, "
        "tool call breakdown, and cache performance."
    ),
    schema={
        "type": "object",
        "properties": {
            "reset": {"type": "boolean", "description": "Reset all stats (default: false)"},
        },
        "required": [],
    },
)
async def session_stats_tool(args: dict) -> str:
    global _cache_hits, _cache_misses

    if args.get("reset"):
        _session_stats["total_calls"] = 0
        _session_stats["total_tokens_in_approx"] = 0
        _session_stats["total_tokens_out_approx"] = 0
        _session_stats["tool_calls"] = {}
        _session_stats["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        _cache_hits = 0
        _cache_misses = 0
        return "Session stats reset."

    lines = [
        f"Session started: {_session_stats['started_at']}",
        f"Total model calls: {_session_stats['total_calls']}",
        f"Approx tokens in: {_session_stats['total_tokens_in_approx']:,}",
        f"Approx tokens out: {_session_stats['total_tokens_out_approx']:,}",
        f"Cache hits: {_cache_hits} / misses: {_cache_misses}",
    ]

    tool_calls = _session_stats["tool_calls"]
    if tool_calls:
        lines.append(f"\nTool calls ({sum(tool_calls.values())} total):")
        for name, count in sorted(tool_calls.items(), key=lambda x: -x[1]):
            lines.append(f"  {name}: {count}")

    return "\n".join(lines)


@tool_handler(
    name="diff_rag",
    description=(
        "Extract symbols from a git diff, search the RAG index for related code, "
        "and return contextually relevant code that might be affected by the changes. "
        "Combines diff parsing with RAG search for context-aware reviews."
    ),
    schema={
        "type": "object",
        "properties": {
            "index_name": {"type": "string", "description": "RAG index to search"},
            "diff": {"type": "string", "description": "Git diff to analyze (or omit to use current unstaged diff)"},
            "directory": {"type": "string", "description": "Git repo directory for auto-diff (default: cwd)"},
            "top_k": {"type": "integer", "description": "Results per symbol query (default: 2)"},
        },
        "required": ["index_name"],
    },
)
async def diff_rag(args: dict) -> str:
    index_name = _sanitize_topic(args["index_name"])
    entry = _load_index(index_name)
    if entry is None:
        return f"Index '{index_name}' not found."

    diff = args.get("diff", "")
    if not diff:
        directory = args.get("directory", ".")
        dir_path = Path(os.path.expanduser(directory)).resolve()
        diff = await _run_git("diff", cwd=str(dir_path))
        staged = await _run_git("diff", "--staged", cwd=str(dir_path))
        diff = (staged + "\n" + diff).strip()

    if not diff or diff.startswith("(git error"):
        return "No diff available."

    # Extract symbols from diff: function names, type names, variable names
    symbols = set()
    # Patterns for common languages
    patterns = [
        r'(?:fn|def|func|function|async fn)\s+(\w+)',  # function definitions
        r'(?:struct|class|enum|type|interface|trait)\s+(\w+)',  # type definitions
        r'(?:impl|extends|implements)\s+(\w+)',  # implementations
        r'(?:use|import|from)\s+[\w:]+::(\w+)',  # imports
        r'(?:pub\s+)?(?:mod|module)\s+(\w+)',  # modules
    ]

    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            for pattern in patterns:
                matches = re.findall(pattern, line)
                symbols.update(matches)

    if not symbols:
        return "No recognizable symbols found in diff. Try rag_query with a manual question instead."

    # Query RAG for each symbol
    top_k = args.get("top_k", 2)
    bm25 = entry["bm25"]
    chunks = entry["chunks"]
    all_results = {}

    for symbol in sorted(symbols):
        query_tokens = _tokenize_bm25(symbol)
        if not query_tokens or not bm25:
            continue
        results = bm25.search(query_tokens, top_k=top_k)
        for idx, score in results:
            if idx not in all_results or score > all_results[idx][1]:
                all_results[idx] = (symbol, score)

    if not all_results:
        return f"Symbols extracted ({', '.join(sorted(symbols))}) but no matching code found in index."

    # Sort by score and format
    sorted_results = sorted(all_results.items(), key=lambda x: -x[1][1])[:10]

    parts = [f"Symbols found in diff: {', '.join(sorted(symbols))}\n"]
    for idx, (symbol, score) in sorted_results:
        chunk = chunks[idx]
        try:
            rel = str(Path(chunk["file"]).relative_to(entry["meta"]["directory"]))
        except ValueError:
            rel = chunk["file"]
        parts.append(
            f"--- {rel}:{chunk['start_line']}-{chunk['end_line']} "
            f"(symbol: {symbol}, score: {score:.2f}) ---\n"
            f"{chunk['content'][:500]}"
        )

    return "\n\n".join(parts)


@tool_handler(
    name="save_pipeline",
    description=(
        "Save a pipeline template for reuse. Pipelines are stored as JSON files "
        "and can be loaded by name with the pipeline tool."
    ),
    schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Template name (e.g. 'classify-and-review')"},
            "description": {"type": "string", "description": "What this pipeline does"},
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string"},
                        "max_tokens": {"type": "integer"},
                        "grammar": {"type": "string"},
                    },
                    "required": ["prompt"],
                },
                "description": "Pipeline steps (same format as pipeline tool)",
            },
        },
        "required": ["name", "steps"],
    },
)
async def save_pipeline_tool(args: dict) -> str:
    name = _sanitize_topic(args["name"])
    PIPELINES_DIR.mkdir(parents=True, exist_ok=True)

    template = {
        "name": name,
        "description": args.get("description", ""),
        "steps": args["steps"],
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    path = PIPELINES_DIR / f"{name}.json"
    with open(path, "w") as f:
        json.dump(template, f, indent=2)

    return f"Pipeline '{name}' saved ({len(args['steps'])} steps) to {path}"


@tool_handler(
    name="list_pipelines",
    description="List all saved pipeline templates.",
    schema={"type": "object", "properties": {}, "required": []},
)
async def list_pipelines(args: dict) -> str:
    if not PIPELINES_DIR.exists():
        return "No saved pipelines. Use save_pipeline to create one."

    files = sorted(PIPELINES_DIR.glob("*.json"))
    if not files:
        return "No saved pipelines."

    lines = [f"Saved pipelines ({len(files)}):"]
    for f in files:
        try:
            with open(f) as fp:
                data = json.load(fp)
            desc = data.get("description", "")
            steps = len(data.get("steps", []))
            lines.append(f"  {data['name']}: {steps} steps — {desc}")
        except Exception:
            lines.append(f"  {f.stem}: (unreadable)")

    return "\n".join(lines)


# ===========================================================================
# Semantic search tools (fastembed + cross-encoder)
# ===========================================================================

@tool_handler(
    name="embed_text",
    description=(
        "Compute embedding vectors for one or more text strings using fastembed "
        "(jinaai/jina-embeddings-v2-base-code, 768 dimensions, code-tuned, CPU-only). "
        "Returns vectors that can be used for cosine similarity search."
    ),
    schema={
        "type": "object",
        "properties": {
            "texts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of text strings to embed (max 50)",
            },
        },
        "required": ["texts"],
    },
)
async def embed_text(args: dict) -> str:
    texts = args["texts"]
    if not texts:
        return "Error: no texts provided."
    if len(texts) > 50:
        return "Error: max 50 texts per call."
    try:
        vectors = _embed_texts(texts)
        return json.dumps({
            "model": _EMBEDDING_MODEL_NAME,
            "dimensions": len(vectors[0]) if vectors else 0,
            "count": len(vectors),
            "vectors": vectors,
        })
    except Exception as e:
        return f"Error computing embeddings: {e}"


@tool_handler(
    name="semantic_search",
    description=(
        "Search an index using embedding cosine similarity instead of BM25. "
        "Requires the index to have been built with embed=true. "
        "Returns chunks ranked by vector similarity to the query."
    ),
    schema={
        "type": "object",
        "properties": {
            "index_name": {"type": "string", "description": "Name of the index to search"},
            "query": {"type": "string", "description": "Natural language query"},
            "top_k": {"type": "integer", "description": "Number of results (default: 5)"},
        },
        "required": ["index_name", "query"],
    },
)
async def semantic_search(args: dict) -> str:
    name = _sanitize_topic(args["index_name"])
    entry = _load_index(name)
    if entry is None:
        return f"Index '{name}' not found."

    embeddings = entry.get("embeddings")
    if not embeddings:
        return f"Index '{name}' has no embeddings. Rebuild with embed=true."

    query = args["query"]
    top_k = args.get("top_k", 5)

    try:
        query_vec = _embed_texts([query])[0]
    except Exception as e:
        return f"Error embedding query: {e}"

    # Score all chunks by cosine similarity
    scored = []
    for i, vec in enumerate(embeddings):
        sim = _cosine_similarity(query_vec, vec)
        scored.append((i, sim))
    scored.sort(key=lambda x: x[1], reverse=True)

    chunks = entry["chunks"]
    parts = []
    for idx, score in scored[:top_k]:
        chunk = chunks[idx]
        try:
            rel = str(Path(chunk["file"]).relative_to(entry["meta"]["directory"]))
        except ValueError:
            rel = chunk["file"]
        parts.append(
            f"--- {rel}:{chunk['start_line']}-{chunk['end_line']} (sim: {score:.3f}) ---\n"
            f"{chunk['content']}"
        )

    return f"Top {len(parts)} semantic matches for '{query}':\n\n" + "\n\n".join(parts)


@tool_handler(
    name="hybrid_search",
    description=(
        "Multi-signal search with reciprocal rank fusion. "
        "Automatically fuses all available signals: BM25 (always) + dense embeddings + "
        "SPLADE sparse + ColBERT late-interaction. Falls back to BM25-only if no embeddings. "
        "Best retrieval quality — state-of-the-art 4-way fusion."
    ),
    schema={
        "type": "object",
        "properties": {
            "index_name": {"type": "string", "description": "Name of the index to search"},
            "query": {"type": "string", "description": "Natural language query"},
            "top_k": {"type": "integer", "description": "Number of results (default: 5)"},
        },
        "required": ["index_name", "query"],
    },
)
async def hybrid_search(args: dict) -> str:
    name = _sanitize_topic(args["index_name"])
    entry = _load_index(name)
    if entry is None:
        return f"Index '{name}' not found."

    query = args["query"]
    top_k = args.get("top_k", 5)
    candidate_k = top_k * 3  # over-retrieve for fusion

    query_tokens = _tokenize_bm25(query)
    chunks = entry["chunks"]
    bm25 = entry.get("bm25")
    embeddings = entry.get("embeddings")
    sparse_embeddings = entry.get("sparse_embeddings")
    colbert_embeddings = entry.get("colbert_embeddings")

    # Reciprocal Rank Fusion constant
    k = 60

    # Count active signals for equal weighting
    active_signals = ["BM25"]  # always active
    if embeddings:
        active_signals.append("dense")
    if sparse_embeddings:
        active_signals.append("SPLADE")
    if colbert_embeddings:
        active_signals.append("ColBERT")
    weight = 1.0 / len(active_signals)

    rrf_scores: dict[int, float] = {}

    # Signal 1: BM25 (keyword matching)
    if bm25 and query_tokens:
        bm25_results = bm25.search(query_tokens, top_k=candidate_k)
        for rank, (idx, _score) in enumerate(bm25_results):
            rrf_scores[idx] = rrf_scores.get(idx, 0) + weight / (k + rank + 1)

    # Signal 2: Dense embeddings (semantic similarity)
    if embeddings:
        try:
            query_vec = _embed_texts([query])[0]
            dense_scored = []
            for i, vec in enumerate(embeddings):
                sim = _cosine_similarity(query_vec, vec)
                dense_scored.append((i, sim))
            dense_scored.sort(key=lambda x: x[1], reverse=True)
            for rank, (idx, _sim) in enumerate(dense_scored[:candidate_k]):
                rrf_scores[idx] = rrf_scores.get(idx, 0) + weight / (k + rank + 1)
        except Exception as e:
            log.warning("Dense scoring failed: %s", e)

    # Signal 3: SPLADE sparse (learned keyword importance)
    if sparse_embeddings:
        try:
            query_sparse = _sparse_embed_texts([query])[0]
            sparse_scored = []
            for i, svec in enumerate(sparse_embeddings):
                sim = _sparse_similarity(query_sparse, svec)
                sparse_scored.append((i, sim))
            sparse_scored.sort(key=lambda x: x[1], reverse=True)
            for rank, (idx, _sim) in enumerate(sparse_scored[:candidate_k]):
                rrf_scores[idx] = rrf_scores.get(idx, 0) + weight / (k + rank + 1)
        except Exception as e:
            log.warning("SPLADE scoring failed: %s", e)

    # Signal 4: ColBERT (per-token late interaction)
    if colbert_embeddings:
        try:
            query_colbert = _colbert_embed_texts([query])[0]
            colbert_scored = []
            for i, doc_vecs in enumerate(colbert_embeddings):
                sim = _colbert_maxsim(query_colbert, doc_vecs)
                colbert_scored.append((i, sim))
            colbert_scored.sort(key=lambda x: x[1], reverse=True)
            for rank, (idx, _sim) in enumerate(colbert_scored[:candidate_k]):
                rrf_scores[idx] = rrf_scores.get(idx, 0) + weight / (k + rank + 1)
        except Exception as e:
            log.warning("ColBERT scoring failed: %s", e)

    if not rrf_scores:
        return f"No results for '{query}' in index '{name}'."

    # Sort by fused score
    ranked = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)[:top_k]

    parts = []
    mode = " + ".join(active_signals)
    for idx, score in ranked:
        chunk = chunks[idx]
        try:
            rel = str(Path(chunk["file"]).relative_to(entry["meta"]["directory"]))
        except ValueError:
            rel = chunk["file"]
        parts.append(
            f"--- {rel}:{chunk['start_line']}-{chunk['end_line']} (rrf: {score:.4f}) ---\n"
            f"{chunk['content']}"
        )

    return f"Top {len(parts)} matches ({mode} fusion) for '{query}':\n\n" + "\n\n".join(parts)


@tool_handler(
    name="rerank_chunks",
    description=(
        "Re-rank a list of text chunks by relevance to a query using a cross-encoder model "
        "(Xenova/ms-marco-MiniLM-L-6-v2, CPU-only). Returns chunks sorted by relevance score. "
        "Use after BM25/semantic search to improve result ordering."
    ),
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The query to rank against"},
            "chunks": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of text chunks to re-rank (max 20)",
            },
            "top_k": {"type": "integer", "description": "Return top K results (default: all)"},
        },
        "required": ["query", "chunks"],
    },
)
async def rerank_chunks(args: dict) -> str:
    query = args["query"]
    input_chunks = args["chunks"]
    top_k = args.get("top_k", len(input_chunks))

    if not input_chunks:
        return "Error: no chunks provided."
    if len(input_chunks) > 20:
        return "Error: max 20 chunks per call."

    try:
        reranker = _get_reranker()
        results = list(reranker.rerank(query, input_chunks, top_k=min(top_k, len(input_chunks))))
        parts = []
        for r in results:
            idx = r["corpus_id"]
            score = r["score"]
            preview = input_chunks[idx][:200]
            parts.append(f"[{idx}] score={score:.4f}: {preview}...")
        return f"Re-ranked {len(results)} chunks:\n" + "\n".join(parts)
    except Exception as e:
        return f"Error during reranking: {e}"


# ===========================================================================
# Knowledge Graph tools (Phase 8)
# ===========================================================================
_kg_instance = None


def _get_kg():
    global _kg_instance
    if _kg_instance is None:
        from knowledge.graph import KnowledgeGraph
        _kg_instance = KnowledgeGraph()
    return _kg_instance


@tool_handler(
    name="kg_add",
    description=(
        "Add an entity to the knowledge graph with auto-embedding. "
        "Types: concept, code_module, decision, learning, person, tool. "
        "If an entity with the same name+type exists, it will be updated."
    ),
    schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Entity name"},
            "type": {"type": "string", "description": "Entity type: concept, code_module, decision, learning, person, tool"},
            "content": {"type": "string", "description": "Entity content/description"},
            "metadata": {"type": "object", "description": "Optional metadata dict"},
        },
        "required": ["name", "type"],
    },
)
async def kg_add(args: dict) -> str:
    kg = _get_kg()
    try:
        entity_id = kg.add_entity(
            name=args["name"],
            type=args["type"],
            content=args.get("content", ""),
            metadata=args.get("metadata"),
        )
        return f"Entity added/updated: {args['name']} (type={args['type']}, id={entity_id})"
    except Exception as e:
        return f"Error: {e}"


@tool_handler(
    name="kg_relate",
    description=(
        "Create a typed relationship between two entities. "
        "Relation types: DEPENDS_ON, DECIDED_BY, RELATED_TO, SUPERSEDES, IMPLEMENTS. "
        "Entities can be specified by name (resolved to ID) or by ID directly."
    ),
    schema={
        "type": "object",
        "properties": {
            "from_name": {"type": "string", "description": "Source entity name (or use from_id)"},
            "to_name": {"type": "string", "description": "Target entity name (or use to_id)"},
            "from_id": {"type": "integer", "description": "Source entity ID (alternative to from_name)"},
            "to_id": {"type": "integer", "description": "Target entity ID (alternative to to_name)"},
            "relation": {"type": "string", "description": "Relation type: DEPENDS_ON, DECIDED_BY, RELATED_TO, SUPERSEDES, IMPLEMENTS"},
        },
        "required": ["relation"],
    },
)
async def kg_relate(args: dict) -> str:
    kg = _get_kg()
    try:
        from_id = args.get("from_id")
        to_id = args.get("to_id")

        if not from_id and args.get("from_name"):
            entity = kg.find_entity(args["from_name"])
            from_id = entity.id if entity else None
        if not to_id and args.get("to_name"):
            entity = kg.find_entity(args["to_name"])
            to_id = entity.id if entity else None

        if not from_id or not to_id:
            return "Error: Could not resolve both entities. Add them first with kg_add."

        kg.add_relation(from_id, to_id, args["relation"])
        return f"Relation created: {args.get('from_name', from_id)} -{args['relation']}-> {args.get('to_name', to_id)}"
    except Exception as e:
        return f"Error: {e}"


@tool_handler(
    name="kg_query",
    description=(
        "Search the knowledge graph using full-text search or semantic search. "
        "Returns matching entities with content previews and scores."
    ),
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "type": {"type": "string", "description": "Filter by entity type (optional)"},
            "semantic": {"type": "boolean", "description": "Use semantic search instead of FTS (default: false)"},
            "max_results": {"type": "integer", "description": "Max results (default: 10)"},
        },
        "required": ["query"],
    },
)
async def kg_query(args: dict) -> str:
    kg = _get_kg()
    try:
        max_r = args.get("max_results", 10)
        if args.get("semantic"):
            results = kg.semantic_search(args["query"], max_results=max_r)
        else:
            results = kg.query(args["query"], max_results=max_r, entity_type=args.get("type"))

        if not results:
            return "No results found."

        parts = []
        for r in results:
            parts.append(f"[{r['id']}] {r['name']} ({r['type']}) score={r.get('score', 0):.3f}\n  {r['content']}")
        return f"Found {len(results)} results:\n\n" + "\n\n".join(parts)
    except Exception as e:
        return f"Error: {e}"


@tool_handler(
    name="kg_context",
    description=(
        "Get full context for a topic: the entity, its relations, and connected graph. "
        "Like a mind-map expansion around a concept."
    ),
    schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Entity/topic name to look up"},
            "depth": {"type": "integer", "description": "Graph traversal depth (default: 2)"},
        },
        "required": ["name"],
    },
)
async def kg_context(args: dict) -> str:
    kg = _get_kg()
    try:
        ctx = kg.context(args["name"], max_depth=args.get("depth", 2))
        if "error" in ctx:
            return ctx["error"]

        entity = ctx["entity"]
        parts = [f"Entity: {entity['name']} ({entity['type']})\n{entity['content']}"]

        if ctx["relations"]:
            parts.append("\nRelations:")
            for r in ctx["relations"]:
                arrow = "->" if r["direction"] == "outgoing" else "<-"
                parts.append(f"  {arrow} {r['relation']} {r['entity_name']} ({r['entity_type']})")

        if len(ctx["graph"]) > 1:
            parts.append(f"\nGraph ({len(ctx['graph'])} nodes within depth {args.get('depth', 2)}):")
            for node in ctx["graph"][1:]:  # skip root
                parts.append(f"  [d={node['depth']}] {node['name']} ({node['type']}): {node['content'][:80]}")

        return "\n".join(parts)
    except Exception as e:
        return f"Error: {e}"


@tool_handler(
    name="kg_timeline",
    description="Get recently updated entities in the knowledge graph, ordered by time.",
    schema={
        "type": "object",
        "properties": {
            "hours": {"type": "number", "description": "Look back N hours (default: 24)"},
            "limit": {"type": "integer", "description": "Max results (default: 20)"},
        },
        "required": [],
    },
)
async def kg_timeline(args: dict) -> str:
    kg = _get_kg()
    try:
        import time as _time
        since = _time.time() - (args.get("hours", 24) * 3600)
        results = kg.timeline(since=since, limit=args.get("limit", 20))

        if not results:
            return "No recent entities."

        parts = []
        for r in results:
            ts = _time.strftime("%Y-%m-%d %H:%M", _time.localtime(r["updated_at"]))
            parts.append(f"[{ts}] {r['name']} ({r['type']}): {r['content'][:80]}")
        return f"Timeline ({len(results)} entities):\n" + "\n".join(parts)
    except Exception as e:
        return f"Error: {e}"


@tool_handler(
    name="kg_import",
    description="Import existing notes and knowledge base entries into the knowledge graph as entities.",
    schema={
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "What to import: 'notes' (default), 'all'"},
        },
        "required": [],
    },
)
async def kg_import(args: dict) -> str:
    kg = _get_kg()
    try:
        from pathlib import Path
        notes_dir = Path(__file__).parent / "notes"
        count = kg.import_notes(notes_dir)
        return f"Imported {count} notes into knowledge graph."
    except Exception as e:
        return f"Error: {e}"


@tool_handler(
    name="kg_stats",
    description="Show knowledge graph statistics: entity counts, relation counts, type breakdown.",
    schema={"type": "object", "properties": {}, "required": []},
)
async def kg_stats(args: dict) -> str:
    kg = _get_kg()
    try:
        stats = kg.stats()
        parts = [
            f"Entities: {stats['total_entities']}",
            f"Relations: {stats['total_relations']}",
        ]
        if stats["entities_by_type"]:
            parts.append("By type:")
            for t, c in stats["entities_by_type"].items():
                parts.append(f"  {t}: {c}")
        return "\n".join(parts)
    except Exception as e:
        return f"Error: {e}"


# ===========================================================================
# Web search + fetch tools
# ===========================================================================


@tool_handler(
    name="web_search",
    description=(
        "Search the web via DuckDuckGo. Returns titles, URLs, and snippets. "
        "Zero API keys required."
    ),
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "max_results": {
                "type": "integer",
                "description": "Maximum results (default 5, max 20)",
            },
            "region": {
                "type": "string",
                "description": "Region code (default 'wt-wt' for worldwide)",
            },
        },
        "required": ["query"],
    },
)
async def web_search(args: dict) -> str:
    query = args["query"]
    max_results = min(args.get("max_results", 5), 20)
    region = args.get("region", "wt-wt")
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS

        def _search():
            ddgs = DDGS()
            return list(ddgs.text(query, max_results=max_results, region=region))

        results = await asyncio.to_thread(_search)

        if not results:
            return f"No results found for: {query}"

        parts = []
        for i, r in enumerate(results, 1):
            parts.append(
                f"[{i}] {r.get('title', 'No title')}\n"
                f"{r.get('href', '')}\n"
                f"{r.get('body', '')}"
            )
        return "\n\n".join(parts)
    except Exception as e:
        return f"Search error: {e}"


@tool_handler(
    name="web_fetch",
    description=(
        "Fetch a URL and extract readable text content. "
        "Uses trafilatura for robust text extraction."
    ),
    schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch"},
            "max_length": {
                "type": "integer",
                "description": "Maximum text length to return (default 5000)",
            },
        },
        "required": ["url"],
    },
)
async def web_fetch(args: dict) -> str:
    url = args["url"]
    max_length = args.get("max_length", 5000)
    try:
        import trafilatura

        def _fetch_and_extract():
            downloaded = trafilatura.fetch_url(url)
            if not downloaded:
                return None
            return trafilatura.extract(downloaded)

        text = await asyncio.wait_for(
            asyncio.to_thread(_fetch_and_extract), timeout=10
        )

        if not text:
            return f"Could not extract text from: {url}"
        if len(text) > max_length:
            text = text[:max_length] + "\n\n[...truncated]"
        return text
    except asyncio.TimeoutError:
        return f"Timeout fetching: {url}"
    except Exception as e:
        return f"Fetch error: {e}"


# ===========================================================================
# Deep research pipeline
# ===========================================================================


@tool_handler(
    name="deep_research",
    description=(
        "Multi-step research pipeline: web search → fetch top pages → "
        "synthesize with local model → optionally save to knowledge graph. "
        "Returns a cited summary."
    ),
    schema={
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "Research question"},
            "max_sources": {
                "type": "integer",
                "description": "Max pages to fetch and read (default 3)",
            },
            "save_to_kg": {
                "type": "boolean",
                "description": "Save findings to knowledge graph (default true)",
            },
        },
        "required": ["question"],
    },
)
async def deep_research(args: dict) -> str:
    question = args["question"]
    max_sources = min(args.get("max_sources", 3), 5)
    save_to_kg = args.get("save_to_kg", True)

    try:
        # Step 1: Web search
        search_result = await web_search({"query": question, "max_results": max_sources + 2})
        if search_result.startswith("Search error") or search_result.startswith("No results"):
            return f"Research failed at search step: {search_result}"

        # Step 2: Parse URLs from search results
        import re as _re

        urls = _re.findall(r"https?://[^\s\n]+", search_result)[:max_sources]
        if not urls:
            return f"No URLs found in search results. Raw results:\n{search_result}"

        # Step 3: Fetch pages in parallel
        fetch_tasks = [web_fetch({"url": u, "max_length": 3000}) for u in urls]
        fetched = await asyncio.gather(*fetch_tasks, return_exceptions=True)

        # Build source material
        sources = []
        source_texts = []
        for url, content in zip(urls, fetched):
            if isinstance(content, Exception):
                continue
            if content and not content.startswith(("Timeout", "Fetch error", "Could not")):
                sources.append(url)
                source_texts.append(f"[Source: {url}]\n{content[:2500]}")

        if not source_texts:
            return f"Could not fetch any pages. Search results:\n{search_result}"

        combined = "\n\n---\n\n".join(source_texts)
        # Budget ~8000 chars for context
        if len(combined) > 8000:
            combined = combined[:8000] + "\n[...truncated]"

        # Step 4: Synthesize with local model
        synthesis_prompt = (
            f"Research question: {question}\n\n"
            f"Source material:\n{combined}\n\n"
            f"Instructions: Provide a comprehensive answer to the research question "
            f"based on the source material above. Include inline citations like [1], [2] "
            f"referring to the sources. Be thorough but concise."
        )
        synthesis = await chat(synthesis_prompt)

        # Build citation footer
        citation_lines = [f"[{i+1}] {url}" for i, url in enumerate(sources)]
        result = f"{synthesis}\n\n---\nSources:\n" + "\n".join(citation_lines)

        # Step 5: Save to knowledge graph
        if save_to_kg:
            try:
                kg = _get_kg()
                # Create concept entity for the topic
                topic_slug = _re.sub(r"[^a-z0-9]+", "-", question.lower())[:50]
                topic_id = kg.add_entity(
                    name=topic_slug,
                    type="concept",
                    content=f"Research: {question}\n\n{synthesis[:500]}",
                )
                # Create tool entities for each source URL
                for url in sources:
                    source_id = kg.add_entity(
                        name=url[:100],
                        type="tool",
                        content=f"Source URL for research on: {question}",
                    )
                    if topic_id and source_id:
                        kg.add_relation(source_id, topic_id, "RELATED_TO")
            except Exception as kg_err:
                result += f"\n\n(KG save warning: {kg_err})"

        return result
    except Exception as e:
        return f"Research error: {e}"


# ===========================================================================
# Compute mesh tools
# ===========================================================================
_gpu_pool = None  # Set by gateway.py after startup


@tool_handler(
    name="compute_status",
    description=(
        "Show all connected devices in the compute mesh, their capabilities, "
        "load, health, and tier. Includes both legacy GPU backends and worker agents."
    ),
    schema={"type": "object", "properties": {}, "required": []},
)
async def compute_status_tool(args: dict) -> str:
    parts = []

    # Legacy GPU backends
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

    # Compute mesh nodes
    if _gpu_pool and hasattr(_gpu_pool, '_compute_nodes'):
        nodes = _gpu_pool.compute_status()
        if nodes:
            parts.append("\n## Compute Mesh Nodes")
            for n in nodes:
                caps = n.get("capabilities", {})
                cap_flags = [k for k, v in caps.items()
                             if isinstance(v, bool) and v]
                parts.append(
                    f"  {n['name']}: tier={n['tier']}, "
                    f"{'healthy' if n['healthy'] else 'unhealthy'}, "
                    f"tasks={n['active_tasks']}, "
                    f"caps=[{', '.join(cap_flags)}]"
                )

    if not parts:
        return "No backends or compute nodes registered."
    return "\n".join(parts)


@tool_handler(
    name="compute_route",
    description=(
        "Preview where a task would be routed in the compute mesh. "
        "Task types: inference, embeddings, tts, stt, reranking, classification."
    ),
    schema={
        "type": "object",
        "properties": {
            "task_type": {
                "type": "string",
                "description": "Type of task to route",
            },
            "min_vram": {"type": "integer", "description": "Minimum VRAM required (MB)"},
        },
        "required": ["task_type"],
    },
)
async def compute_route_tool(args: dict) -> str:
    if not _gpu_pool:
        return "GPU pool not initialized."

    task_type = args["task_type"]
    requirements = {}
    if args.get("min_vram"):
        requirements["min_vram"] = args["min_vram"]

    url = _gpu_pool.route_task(task_type, requirements)
    if url:
        return f"Task '{task_type}' would be routed to: {url}"
    return f"No suitable device found for task type '{task_type}'"


# ===========================================================================
# Agent management tools
# ===========================================================================
_agent_supervisor = None


@tool_handler(
    name="agent_list",
    description="List all configured autonomous agents and their status (running, idle, error).",
    schema={"type": "object", "properties": {}, "required": []},
)
async def agent_list(args: dict) -> str:
    try:
        import yaml as _yaml
        agents_yaml = Path(__file__).parent / "agents.yaml"
        if not agents_yaml.exists():
            return "No agents.yaml found."
        with open(agents_yaml) as f:
            cfg = _yaml.safe_load(f) or {}

        agents = cfg.get("agents", {})
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
    agent_id = args["agent_id"]
    lines = args.get("lines", 50)
    state_dir = Path(__file__).parent / "agent_state"
    state_file = state_dir / f"{agent_id}.json"

    if not state_file.exists():
        return f"No state found for agent '{agent_id}'. It may not have run yet."

    try:
        data = json.loads(state_file.read_text())
    except Exception:
        return f"Cannot read state for agent '{agent_id}'."

    # Build a useful summary
    parts = [
        f"Agent: {agent_id}",
        f"Status: {data.get('status', 'unknown')}",
        f"Run count: {data.get('run_count', 0)}",
    ]
    last_run = data.get("last_run", 0)
    if last_run:
        import time as _time
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


# ===========================================================================
# Helper: resolve webui root directory
# ===========================================================================

def _get_webui_root() -> Path | None:
    """Get the text-generation-webui root directory from config or heuristic."""
    # Check config first
    root = _config.get("webui_root", "")
    if root:
        path = Path(os.path.expanduser(root)).resolve()
        if path.exists():
            return path

    # Heuristic: derive from webui_settings path
    settings_path = _config.get("webui_settings", "")
    if settings_path:
        path = Path(os.path.expanduser(settings_path)).resolve()
        # settings.yaml is in user_data/, so parent.parent is webui root
        webui_root = path.parent.parent
        if webui_root.exists():
            return webui_root

    # Last resort: default location
    default = Path.home() / "Development" / "text-generation-webui"
    if default.exists():
        return default

    return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


if __name__ == "__main__":
    if "--http" in sys.argv:
        from gateway import main as gateway_main
        gateway_main()
    else:
        asyncio.run(main())
