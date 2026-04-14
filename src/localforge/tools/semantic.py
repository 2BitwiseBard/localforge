"""Semantic search tools — embed, dense search, hybrid search, reranking."""

import json
import logging
from pathlib import Path

from localforge.chunking import load_index, tokenize_bm25
from localforge.embeddings import (
    DENSE_MODEL,
    colbert_embed_texts,
    colbert_maxsim,
    cosine_similarity,
    embed_texts,
    get_reranker,
    sparse_embed_texts,
    sparse_similarity,
)
from localforge.paths import indexes_dir
from localforge.tools import tool_handler

log = logging.getLogger("localforge")

INDEXES_DIR = indexes_dir()


def _sanitize_topic(raw: str) -> str:
    import re
    return re.sub(r"[^a-zA-Z0-9_-]", "_", raw.strip())[:80]


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
        vectors = embed_texts(texts)
        return json.dumps({
            "model": DENSE_MODEL,
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
    entry = load_index(name)
    if entry is None:
        return f"Index '{name}' not found."

    embeddings = entry.get("embeddings")
    if not embeddings:
        return f"Index '{name}' has no embeddings. Rebuild with embed=true."

    query = args["query"]
    top_k = args.get("top_k", 5)

    try:
        query_vec = embed_texts([query])[0]
    except Exception as e:
        return f"Error embedding query: {e}"

    scored = []
    for i, vec in enumerate(embeddings):
        sim = cosine_similarity(query_vec, vec)
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
        "SPLADE sparse + ColBERT late-interaction. Best retrieval quality."
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
    entry = load_index(name)
    if entry is None:
        return f"Index '{name}' not found."

    query = args["query"]
    top_k = args.get("top_k", 5)
    candidate_k = top_k * 3

    query_tokens = tokenize_bm25(query)
    chunks = entry["chunks"]
    bm25 = entry.get("bm25")
    embeddings = entry.get("embeddings")
    sparse_embeddings = entry.get("sparse_embeddings")
    colbert_embeddings = entry.get("colbert_embeddings")

    k = 60  # RRF constant

    active_signals = ["BM25"]
    if embeddings:
        active_signals.append("dense")
    if sparse_embeddings:
        active_signals.append("SPLADE")
    if colbert_embeddings:
        active_signals.append("ColBERT")
    weight = 1.0 / len(active_signals)

    rrf_scores: dict[int, float] = {}

    # Signal 1: BM25
    if bm25 and query_tokens:
        bm25_results = bm25.search(query_tokens, top_k=candidate_k)
        for rank, (idx, _score) in enumerate(bm25_results):
            rrf_scores[idx] = rrf_scores.get(idx, 0) + weight / (k + rank + 1)

    # Signal 2: Dense embeddings
    if embeddings:
        try:
            query_vec = embed_texts([query])[0]
            dense_scored = []
            for i, vec in enumerate(embeddings):
                sim = cosine_similarity(query_vec, vec)
                dense_scored.append((i, sim))
            dense_scored.sort(key=lambda x: x[1], reverse=True)
            for rank, (idx, _sim) in enumerate(dense_scored[:candidate_k]):
                rrf_scores[idx] = rrf_scores.get(idx, 0) + weight / (k + rank + 1)
        except Exception as e:
            log.warning("Dense scoring failed: %s", e)

    # Signal 3: SPLADE sparse
    if sparse_embeddings:
        try:
            query_sparse = sparse_embed_texts([query])[0]
            sparse_scored = []
            for i, svec in enumerate(sparse_embeddings):
                sim = sparse_similarity(query_sparse, svec)
                sparse_scored.append((i, sim))
            sparse_scored.sort(key=lambda x: x[1], reverse=True)
            for rank, (idx, _sim) in enumerate(sparse_scored[:candidate_k]):
                rrf_scores[idx] = rrf_scores.get(idx, 0) + weight / (k + rank + 1)
        except Exception as e:
            log.warning("SPLADE scoring failed: %s", e)

    # Signal 4: ColBERT
    if colbert_embeddings:
        try:
            query_colbert = colbert_embed_texts([query])[0]
            colbert_scored = []
            for i, doc_vecs in enumerate(colbert_embeddings):
                sim = colbert_maxsim(query_colbert, doc_vecs)
                colbert_scored.append((i, sim))
            colbert_scored.sort(key=lambda x: x[1], reverse=True)
            for rank, (idx, _sim) in enumerate(colbert_scored[:candidate_k]):
                rrf_scores[idx] = rrf_scores.get(idx, 0) + weight / (k + rank + 1)
        except Exception as e:
            log.warning("ColBERT scoring failed: %s", e)

    if not rrf_scores:
        return f"No results for '{query}' in index '{name}'."

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
        "(Xenova/ms-marco-MiniLM-L-6-v2, CPU-only). Returns chunks sorted by relevance score."
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
        reranker = get_reranker()
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
