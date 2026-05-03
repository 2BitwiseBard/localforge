"""Web search, fetch, and deep research tools."""

import asyncio
import re

from localforge.client import chat, task_type_context
from localforge.tools import tool_handler


@tool_handler(
    name="web_search",
    description=("Search the web via DuckDuckGo. Returns titles, URLs, and snippets. Zero API keys required."),
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "max_results": {"type": "integer", "description": "Maximum results (default 5, max 20)"},
            "region": {"type": "string", "description": "Region code (default 'wt-wt' for worldwide)"},
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
            parts.append(f"[{i}] {r.get('title', 'No title')}\n{r.get('href', '')}\n{r.get('body', '')}")
        return "\n\n".join(parts)
    except Exception as e:
        return f"Search error: {e}"


@tool_handler(
    name="web_fetch",
    description=("Fetch a URL and extract readable text content. Uses trafilatura for robust text extraction."),
    schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch"},
            "max_length": {"type": "integer", "description": "Maximum text length to return (default 5000)"},
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

        text = await asyncio.wait_for(asyncio.to_thread(_fetch_and_extract), timeout=10)

        if not text:
            return f"Could not extract text from: {url}"
        if len(text) > max_length:
            text = text[:max_length] + "\n\n[...truncated]"
        return text
    except asyncio.TimeoutError:
        return f"Timeout fetching: {url}"
    except Exception as e:
        return f"Fetch error: {e}"


@tool_handler(
    name="deep_research",
    description=(
        "Multi-step research pipeline: web search -> fetch top pages -> "
        "synthesize with local model -> optionally save to knowledge graph. "
        "Returns a cited summary."
    ),
    schema={
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "Research question"},
            "max_sources": {"type": "integer", "description": "Max pages to fetch and read (default 3)"},
            "save_to_kg": {"type": "boolean", "description": "Save findings to knowledge graph (default true)"},
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
        urls = re.findall(r"https?://[^\s\n]+", search_result)[:max_sources]
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
        async with task_type_context("reasoning"):
            synthesis = await chat(synthesis_prompt)

        citation_lines = [f"[{i + 1}] {url}" for i, url in enumerate(sources)]
        result = f"{synthesis}\n\n---\nSources:\n" + "\n".join(citation_lines)

        # Step 5: Save to knowledge graph
        if save_to_kg:
            try:
                from localforge.tools.knowledge import _get_kg

                kg = _get_kg()
                topic_slug = re.sub(r"[^a-z0-9]+", "-", question.lower())[:50]
                topic_id = kg.add_entity(
                    name=topic_slug,
                    type="concept",
                    content=f"Research: {question}\n\n{synthesis[:500]}",
                )
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
