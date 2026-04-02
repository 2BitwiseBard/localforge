"""Embedding models for LocalForge.

Lazy-loaded CPU-only models for dense, sparse, ColBERT, and reranker operations.
No GPU competition — all run on CPU alongside the primary inference backend.
"""

import logging
import math
from typing import Any

from localforge.paths import fastembed_cache_dir

log = logging.getLogger("localforge")

# ---------------------------------------------------------------------------
# Model names
# ---------------------------------------------------------------------------
DENSE_MODEL = "jinaai/jina-embeddings-v2-base-code"        # 768 dims, code-tuned
SPARSE_MODEL = "Qdrant/bm42-all-minilm-l6-v2-attentions"  # SPLADE sparse
COLBERT_MODEL = "colbert-ir/colbertv2.0"                   # late interaction
RERANKER_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"          # cross-encoder

# ---------------------------------------------------------------------------
# Lazy-loaded singletons
# ---------------------------------------------------------------------------
_embedding_model = None
_sparse_model = None
_colbert_model = None
_reranker_model = None


def get_embedding_model():
    """Lazy-load the dense embedding model on first use."""
    global _embedding_model
    if _embedding_model is None:
        from fastembed import TextEmbedding
        log.info("Loading dense embedding model: %s", DENSE_MODEL)
        _embedding_model = TextEmbedding(DENSE_MODEL, cache_dir=str(fastembed_cache_dir()))
        log.info("Dense embedding model loaded.")
    return _embedding_model


def get_sparse_model():
    """Lazy-load the SPLADE sparse embedding model on first use."""
    global _sparse_model
    if _sparse_model is None:
        from fastembed import SparseTextEmbedding
        log.info("Loading sparse model: %s", SPARSE_MODEL)
        _sparse_model = SparseTextEmbedding(SPARSE_MODEL, cache_dir=str(fastembed_cache_dir()))
        log.info("Sparse model loaded.")
    return _sparse_model


def get_colbert_model():
    """Lazy-load the ColBERT late-interaction model on first use."""
    global _colbert_model
    if _colbert_model is None:
        from fastembed import LateInteractionTextEmbedding
        log.info("Loading ColBERT model: %s", COLBERT_MODEL)
        _colbert_model = LateInteractionTextEmbedding(COLBERT_MODEL, cache_dir=str(fastembed_cache_dir()))
        log.info("ColBERT model loaded.")
    return _colbert_model


def get_reranker():
    """Lazy-load the reranker model on first use."""
    global _reranker_model
    if _reranker_model is None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder
        log.info("Loading reranker: %s", RERANKER_MODEL)
        _reranker_model = TextCrossEncoder(RERANKER_MODEL, cache_dir=str(fastembed_cache_dir()))
        log.info("Reranker loaded.")
    return _reranker_model


# ---------------------------------------------------------------------------
# Embedding operations
# ---------------------------------------------------------------------------

def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a list of texts with the dense code model. Returns list of 768-dim vectors."""
    model = get_embedding_model()
    return [v.tolist() for v in model.embed(texts)]


def sparse_embed_texts(texts: list[str]) -> list[dict]:
    """Embed texts with SPLADE sparse model. Returns list of {indices, values} dicts."""
    model = get_sparse_model()
    results = []
    for sparse_vec in model.embed(texts):
        results.append({
            "indices": sparse_vec.indices.tolist(),
            "values": sparse_vec.values.tolist(),
        })
    return results


def colbert_embed_texts(texts: list[str]) -> list[list[list[float]]]:
    """Embed texts with ColBERT. Returns list of per-token embedding matrices."""
    model = get_colbert_model()
    return [v.tolist() for v in model.embed(texts)]


# ---------------------------------------------------------------------------
# Similarity functions
# ---------------------------------------------------------------------------

def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two dense vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def sparse_similarity(a: dict, b: dict) -> float:
    """Dot product between two sparse vectors (indices+values dicts)."""
    a_map = dict(zip(a["indices"], a["values"]))
    b_map = dict(zip(b["indices"], b["values"]))
    common = set(a_map.keys()) & set(b_map.keys())
    return sum(a_map[k] * b_map[k] for k in common)


def colbert_maxsim(query_vecs: list[list[float]], doc_vecs: list[list[float]]) -> float:
    """ColBERT MaxSim: for each query token, find max similarity to any doc token."""
    if not query_vecs or not doc_vecs:
        return 0.0
    total = 0.0
    for qv in query_vecs:
        best = max(cosine_similarity(qv, dv) for dv in doc_vecs)
        total += best
    return total / len(query_vecs)
