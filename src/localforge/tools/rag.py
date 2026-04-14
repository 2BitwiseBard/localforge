"""RAG, code indexing, and search tools."""

import asyncio
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from localforge import config as cfg
from localforge.chunking import (
    BM25,
    TEXT_EXTENSIONS,
    _index_cache,
    chunk_file_line,
    chunk_file_treesitter,
    load_index,
    save_index,
    tokenize_bm25,
)
from localforge.client import chat
from localforge.embeddings import colbert_embed_texts, embed_texts, sparse_embed_texts
from localforge.paths import indexes_dir
from localforge.tools import tool_handler

log = logging.getLogger("localforge")

INDEXES_DIR = indexes_dir()


def _sanitize_topic(raw: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", raw.strip())[:80]


async def _run_git(*args: str, cwd: str | None = None) -> str:
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
            "glob_pattern": {"type": "string", "description": "File pattern (default: '**/*.*')"},
            "chunk_lines": {"type": "integer", "description": "Lines per chunk for line-based fallback (default: 50)."},
            "overlap": {"type": "integer", "description": "Overlap lines between chunks (default: 10)"},
            "max_files": {"type": "integer", "description": "Max files to index (default: 500)"},
            "embed": {"type": "boolean", "description": "Compute dense + SPLADE sparse embeddings (default: false)."},
            "colbert": {"type": "boolean", "description": "Also compute ColBERT per-token vectors (default: false). Requires embed=true."},
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

    files = sorted(directory.glob(glob_pattern))
    files = [
        f for f in files
        if f.is_file()
        and "/." not in str(f)
        and f.stat().st_size < 200_000
        and (f.suffix.lower() in TEXT_EXTENSIONS or not f.suffix)
    ]

    if len(files) > max_files:
        files = files[:max_files]

    if not files:
        return f"No indexable text files found in {directory} with pattern '{glob_pattern}'"

    all_chunks: list[dict[str, Any]] = []
    ts_count = 0
    for f in files:
        chunks = chunk_file_treesitter(f, max_chunk_lines=chunk_lines)
        if chunks and chunks != chunk_file_line(f, chunk_lines=chunk_lines, overlap=overlap):
            ts_count += 1
        all_chunks.extend(chunks)

    if not all_chunks:
        return "No content to index (all files were empty or too small)."

    corpus = [c["tokens"] for c in all_chunks]
    bm25 = BM25(corpus)

    texts = [c["content"] for c in all_chunks]
    embeddings = None
    sparse_embeddings = None
    colbert_embeddings = None

    if do_embed:
        try:
            embeddings = embed_texts(texts)
            log.info("Computed %d dense embeddings for index '%s'", len(embeddings), name)
        except Exception as e:
            log.warning("Dense embedding failed: %s", e)

        try:
            sparse_embeddings = sparse_embed_texts(texts)
            log.info("Computed %d SPLADE sparse vectors for index '%s'", len(sparse_embeddings), name)
        except Exception as e:
            log.warning("SPLADE embedding failed: %s", e)

        if do_colbert:
            try:
                colbert_embeddings = colbert_embed_texts(texts)
                log.info("Computed %d ColBERT multi-vectors for index '%s'", len(colbert_embeddings), name)
            except Exception as e:
                log.warning("ColBERT embedding failed: %s", e)

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
    save_index(name, meta, all_chunks, embeddings, sparse_embeddings, colbert_embeddings)

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
            "query": {"type": "string", "description": "Search query"},
            "top_k": {"type": "integer", "description": "Number of results to return (default: 5)"},
        },
        "required": ["index_name", "query"],
    },
)
async def search_index_tool(args: dict) -> str:
    name = _sanitize_topic(args["index_name"])
    entry = load_index(name)
    if entry is None:
        available = [d.name for d in INDEXES_DIR.iterdir() if d.is_dir()] if INDEXES_DIR.exists() else []
        return f"Index '{name}' not found. Available: {', '.join(sorted(available)) or '(none)'}"

    query = args["query"]
    top_k = args.get("top_k", 5)
    query_tokens = tokenize_bm25(query)

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
    """Search across all indexed projects."""
    if not INDEXES_DIR.exists():
        return "No indexes found."

    all_results: list[tuple[str, float, str]] = []
    query_tokens = tokenize_bm25(question)
    if not query_tokens:
        return "Question produced no searchable tokens."

    index_dirs = [d.name for d in INDEXES_DIR.iterdir() if d.is_dir()
                  and not d.name.startswith("__")]

    for idx_name in index_dirs:
        entry = load_index(idx_name)
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
        return await chat(question, system=cfg.get_system_preamble())

    all_results.sort(key=lambda x: x[1], reverse=True)
    top_results = all_results[:top_k]

    context = "\n\n".join(r[2] for r in top_results)
    projects_found = sorted(set(r[0] for r in top_results))

    answer = await chat(
        f"Question: {question}\n\n"
        f"Context from projects ({', '.join(projects_found)}):\n{context[:6000]}",
        system=cfg.get_system_preamble(),
    )
    return f"*Searched {len(index_dirs)} indexes: {', '.join(projects_found)}*\n\n{answer}"


@tool_handler(
    name="rag_query",
    description=(
        "Search an index and ask the local model a question using the matching chunks as context. "
        "Full RAG pipeline: BM25 retrieval -> optional re-ranking -> context assembly -> LLM generation. "
        "Set rerank=true for better relevance. 100%% local."
    ),
    schema={
        "type": "object",
        "properties": {
            "index_name": {"type": "string", "description": "Name of the index. Use '*' or 'all' to search across all."},
            "question": {"type": "string", "description": "Question to answer using retrieved context"},
            "top_k": {"type": "integer", "description": "Number of context chunks to retrieve (default: 3)"},
            "rerank": {"type": "boolean", "description": "Re-rank BM25 results using cross-encoder (default: false)"},
        },
        "required": ["index_name", "question"],
    },
)
async def rag_query(args: dict) -> str:
    from localforge.embeddings import get_reranker

    raw_name = args["index_name"]
    question = args["question"]
    top_k = args.get("top_k", 3)
    do_rerank = args.get("rerank", False)

    if raw_name.strip() in ("*", "all"):
        return await _cross_project_rag(question, top_k, do_rerank)

    name = _sanitize_topic(raw_name)
    entry = load_index(name)
    if entry is None:
        available = [d.name for d in INDEXES_DIR.iterdir() if d.is_dir()] if INDEXES_DIR.exists() else []
        return f"Index '{name}' not found. Available: {', '.join(sorted(available)) or '(none)'}"
    query_tokens = tokenize_bm25(question)

    if not query_tokens:
        return "Question produced no searchable tokens."

    bm25 = entry["bm25"]
    if bm25 is None:
        return "Index is empty."

    retrieve_k = max(top_k * 2, 6) if do_rerank else top_k
    results = bm25.search(query_tokens, top_k=retrieve_k)

    if not results:
        return await chat(question, system=cfg.get_system_preamble())

    chunks = entry["chunks"]

    if do_rerank and len(results) > top_k:
        try:
            reranker = get_reranker()
            pairs = [(question, chunks[idx]["content"][:500]) for idx, _score in results]
            rerank_scores = list(reranker.rerank(question, [p[1] for p in pairs], top_k=top_k))
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
    return await chat(prompt, system=cfg.get_system_preamble())


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
        "Accepts a file path or raw text content."
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
    entry = load_index(name)

    chunk_lines = args.get("chunk_lines", 50)
    overlap = 10

    if entry is None:
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
        new_chunks = chunk_file_line(file_path, chunk_lines=chunk_lines, overlap=overlap)
    elif args.get("content"):
        label = args.get("label", "document")
        lines = args["content"].splitlines()
        start = 0
        while start < len(lines):
            end = min(start + chunk_lines, len(lines))
            chunk_content = "\n".join(lines[start:end])
            non_empty = sum(1 for ln in lines[start:end] if ln.strip())
            if non_empty >= 3:
                new_chunks.append({
                    "file": label,
                    "start_line": start + 1,
                    "end_line": end,
                    "content": chunk_content,
                    "tokens": tokenize_bm25(chunk_content),
                })
            start += chunk_lines - overlap
            if start >= len(lines):
                break
    else:
        return "Error: provide either file_path or content."

    if not new_chunks:
        return "No indexable content in the document."

    all_chunks = entry["chunks"] + new_chunks
    for c in all_chunks:
        if "tokens" not in c:
            c["tokens"] = tokenize_bm25(c["content"])

    corpus = [c["tokens"] for c in all_chunks]
    bm25 = BM25(corpus)

    entry["meta"]["chunk_count"] = len(all_chunks)
    entry["meta"]["file_count"] += 1

    save_index(name, entry["meta"], all_chunks)
    _index_cache[name] = {"meta": entry["meta"], "chunks": all_chunks, "bm25": bm25}

    return f"Added {len(new_chunks)} chunks to index '{name}' (total: {len(all_chunks)} chunks)"


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
    entry = load_index(name)
    if entry is None:
        return f"Index '{name}' not found. Use index_directory to create it first."

    meta = entry["meta"]
    directory = meta.get("directory", "")
    if not directory or directory == "(mixed)":
        return "Cannot incrementally update a mixed-source index. Use index_directory instead."

    dir_path = Path(directory)
    if not dir_path.exists():
        return f"Index directory no longer exists: {directory}"

    changed_output = await _run_git("diff", "--name-only", "HEAD", cwd=str(dir_path))
    untracked_output = await _run_git("ls-files", "--others", "--exclude-standard", cwd=str(dir_path))

    changed_files = set()
    for line in (changed_output + "\n" + untracked_output).splitlines():
        line = line.strip()
        if line and not line.startswith("(git error"):
            full_path = dir_path / line
            if full_path.exists() and full_path.is_file():
                changed_files.add(str(full_path))

    created_at = meta.get("created_at", "")
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

    changed_files = {
        f for f in changed_files
        if Path(f).suffix.lower() in TEXT_EXTENSIONS
        and Path(f).stat().st_size < 200_000
    }

    if not changed_files:
        return f"Index '{name}' is up to date — changed files are not indexable."

    old_chunks = [c for c in entry["chunks"] if c["file"] not in changed_files]
    cl = meta.get("chunk_lines", 50)
    ol = meta.get("overlap", 10)

    new_chunks: list[dict[str, Any]] = []
    for fp in changed_files:
        new_chunks.extend(chunk_file_line(Path(fp), chunk_lines=cl, overlap=ol))

    all_chunks = old_chunks + new_chunks
    for c in all_chunks:
        if "tokens" not in c:
            c["tokens"] = tokenize_bm25(c["content"])

    corpus = [c["tokens"] for c in all_chunks]
    bm25 = BM25(corpus) if corpus else None

    meta["chunk_count"] = len(all_chunks)
    meta["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")

    save_index(name, meta, all_chunks)
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
    name="diff_rag",
    description=(
        "Extract symbols from a git diff, search the RAG index for related code, "
        "and return contextually relevant code that might be affected by the changes."
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
    entry = load_index(index_name)
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

    symbols = set()
    patterns = [
        r'(?:fn|def|func|function|async fn)\s+(\w+)',
        r'(?:struct|class|enum|type|interface|trait)\s+(\w+)',
        r'(?:impl|extends|implements)\s+(\w+)',
        r'(?:use|import|from)\s+[\w:]+::(\w+)',
        r'(?:pub\s+)?(?:mod|module)\s+(\w+)',
    ]

    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            for pattern in patterns:
                matches = re.findall(pattern, line)
                symbols.update(matches)

    if not symbols:
        return "No recognizable symbols found in diff. Try rag_query with a manual question instead."

    top_k = args.get("top_k", 2)
    bm25 = entry["bm25"]
    chunks = entry["chunks"]
    all_results = {}

    for symbol in sorted(symbols):
        query_tokens = tokenize_bm25(symbol)
        if not query_tokens or not bm25:
            continue
        results = bm25.search(query_tokens, top_k=top_k)
        for idx, score in results:
            if idx not in all_results or score > all_results[idx][1]:
                all_results[idx] = (symbol, score)

    if not all_results:
        return f"Symbols extracted ({', '.join(sorted(symbols))}) but no matching code found in index."

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
