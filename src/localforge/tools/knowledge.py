"""Knowledge base, doc lookup, and knowledge graph tools."""

import time

from localforge import config as cfg
from localforge.chunking import load_index, tokenize_bm25
from localforge.client import chat
from localforge.tools import tool_handler

_KNOWLEDGE_INDEX_NAME = "__knowledge_base__"

_kg_instance = None


def _get_kg():
    global _kg_instance
    if _kg_instance is None:
        from localforge.knowledge.graph import KnowledgeGraph
        _kg_instance = KnowledgeGraph()
    return _kg_instance


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
            "source": {"type": "string", "description": "Source: 'web', 'docs', 'code', 'user' (for 'add')"},
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
    from localforge.tools.rag import ingest_document
    action = args["action"]

    if action == "add":
        content = args.get("content", "")
        if not content:
            return "Error: 'content' is required for add"

        source = args.get("source", "unknown")
        tags = args.get("tags", [])
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")

        tag_str = ", ".join(tags) if tags else "untagged"
        labeled_content = (
            f"[source: {source}] [tags: {tag_str}] [added: {timestamp}]\n"
            f"{content}"
        )

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
        entry = load_index(_KNOWLEDGE_INDEX_NAME)
        if entry is None:
            return "Knowledge base is empty. Use action='add' to store knowledge first."

        query_tokens = tokenize_bm25(query)
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
        entry = load_index(_KNOWLEDGE_INDEX_NAME)
        if entry is None:
            return "Knowledge base is empty."

        meta = entry["meta"]
        chunks = entry["chunks"]
        lines = [
            f"Knowledge base: {meta['chunk_count']} chunks, {meta['file_count']} entries",
            f"Created: {meta.get('created_at', '?')}",
            "",
        ]
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
        "Look up library/framework documentation, then summarize with the local model. "
        "Provides structured interface for documentation queries."
    ),
    schema={
        "type": "object",
        "properties": {
            "library": {"type": "string", "description": "Library name (e.g. 'tokio', 'polars', 'react')"},
            "query": {"type": "string", "description": "What to look up"},
            "save_to_kb": {"type": "boolean", "description": "Save useful findings to knowledge_base (default: false)"},
        },
        "required": ["library", "query"],
    },
)
async def doc_lookup(args: dict) -> str:
    library = args["library"]
    query = args["query"]

    prompt = (
        f"I need documentation for the '{library}' library about: {query}\n\n"
        f"Please provide:\n"
        f"1. The key API/concept explanation\n"
        f"2. Usage example with best practices\n"
        f"3. Common pitfalls or gotchas\n\n"
        f"Be concise and focus on practical usage."
    )
    result = await chat(prompt, system=cfg.get_system_preamble())

    if args.get("save_to_kb"):
        await knowledge_base({
            "action": "add",
            "content": f"# {library}: {query}\n\n{result}",
            "source": "docs",
            "tags": [library, "documentation"],
        })
        return f"{result}\n\n(Saved to knowledge base)"

    return result


# ---------------------------------------------------------------------------
# Knowledge Graph tools
# ---------------------------------------------------------------------------

@tool_handler(
    name="kg_add",
    description=(
        "Add an entity to the knowledge graph with auto-embedding. "
        "Types: concept, code_module, decision, learning, person, tool."
    ),
    schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Entity name"},
            "type": {"type": "string", "description": "Entity type"},
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
        "Relation types: DEPENDS_ON, DECIDED_BY, RELATED_TO, SUPERSEDES, IMPLEMENTS."
    ),
    schema={
        "type": "object",
        "properties": {
            "from_name": {"type": "string", "description": "Source entity name"},
            "to_name": {"type": "string", "description": "Target entity name"},
            "from_id": {"type": "integer", "description": "Source entity ID (alternative)"},
            "to_id": {"type": "integer", "description": "Target entity ID (alternative)"},
            "relation": {"type": "string", "description": "Relation type"},
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
    description="Search the knowledge graph using full-text search or semantic search.",
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "type": {"type": "string", "description": "Filter by entity type (optional)"},
            "semantic": {"type": "boolean", "description": "Use semantic search (default: false)"},
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
    description="Get full context for a topic: the entity, its relations, and connected graph.",
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
            for node in ctx["graph"][1:]:
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
        since = time.time() - (args.get("hours", 24) * 3600)
        results = kg.timeline(since=since, limit=args.get("limit", 20))

        if not results:
            return "No recent entities."

        parts = []
        for r in results:
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["updated_at"]))
            parts.append(f"[{ts}] {r['name']} ({r['type']}): {r['content'][:80]}")
        return f"Timeline ({len(results)} entities):\n" + "\n".join(parts)
    except Exception as e:
        return f"Error: {e}"


@tool_handler(
    name="kg_import",
    description="Import existing notes into the knowledge graph as entities.",
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
        from localforge.paths import notes_dir
        count = kg.import_notes(notes_dir())
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


@tool_handler(
    name="kg_rebuild_fts",
    description=(
        "Rebuild the knowledge graph FTS5 full-text search index from scratch. "
        "Use this if search results seem stale or incomplete, or after a crash "
        "during a write operation. Re-indexes all entities."
    ),
    schema={"type": "object", "properties": {}, "required": []},
)
async def kg_rebuild_fts(args: dict) -> str:
    kg = _get_kg()
    try:
        count = kg.rebuild_fts_index()
        return f"FTS5 index rebuilt successfully. {count} entities re-indexed."
    except Exception as e:
        return f"Error rebuilding FTS index: {e}"
