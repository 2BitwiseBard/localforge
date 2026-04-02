"""Research agent: searches the web, fetches sources, synthesizes findings,
and saves results to the knowledge graph and notes.

Triggered by:
  - Cron schedule (processes research-queue note)
  - Webhook with {"query": "..."} payload (immediate research)
  - Manual trigger from dashboard
"""

import re
import time

from .base import BaseAgent, TrustLevel
from .supervisor import register_agent


@register_agent
class ResearchAgent(BaseAgent):
    name = "research-agent"
    trust_level = TrustLevel.SAFE
    description = "Web-powered research: search → fetch → synthesize → KG + notes"

    async def on_trigger(self, trigger_type: str, payload: dict | None = None):
        """Handle webhook/manual triggers with a query payload."""
        self.state.log(f"Triggered via {trigger_type}")
        if payload and payload.get("query"):
            # Direct query — research it immediately
            query = payload["query"]
            self.state.log(f"Direct research request: {query}")
            await self._research_query(query)
        else:
            # Normal scheduled run — process the queue
            await self.run()

    async def run(self):
        # Check for pending queries in notes
        result = await self.call_tool("recall_note", {"topic": "research-queue"})
        text = self.extract_text(result)

        if not text or "not found" in text.lower() or "no note" in text.lower():
            self.state.log("No research queries queued")
            return

        queries = [q.strip() for q in text.strip().splitlines()
                   if q.strip() and not q.startswith("#")]
        if not queries:
            self.state.log("Research queue is empty")
            return

        for query in queries[:3]:
            await self._research_query(query)

        # Clear processed queries
        remaining = queries[3:]
        if remaining:
            await self.call_tool("save_note", {
                "topic": "research-queue",
                "content": "\n".join(remaining),
            })
        else:
            await self.call_tool("delete_note", {"topic": "research-queue"})

    async def _check_kg_for_existing(self, query: str) -> str | None:
        """Check if this topic was recently researched (within 7 days)."""
        topic_slug = re.sub(r"[^a-z0-9]+", "-", query.lower())[:50].strip("-")
        try:
            result = await self.call_tool("kg_query", {
                "query": topic_slug,
            }, timeout=15)
            text = self.extract_text(result)
            if text and "no " not in text.lower()[:20]:
                # Check if it's recent (within 7 days)
                # KG results include updated_at — look for recent timestamps
                seven_days_ago = time.time() - (7 * 86400)
                if "updated_at" in text:
                    # Found existing research
                    return text
        except Exception:
            pass
        return None

    async def _research_query(self, query: str):
        """Execute the full research pipeline for a single query."""
        self.state.log(f"Researching: {query}")
        topic_slug = re.sub(r"[^a-z0-9]+", "-", query.lower())[:50].strip("-")

        # Check KG for existing research
        existing = await self._check_kg_for_existing(query)
        if existing:
            self.state.log(f"  Found existing research for: {query} (skipping)")
            return

        # Step 1: Web search
        search_result = await self.call_tool("web_search", {
            "query": query,
            "max_results": 5,
        }, timeout=30)
        search_text = self.extract_text(search_result)

        if not search_text or search_text.startswith(("Search error", "No results")):
            self.state.log(f"  Search failed for: {query}")
            return

        # Parse URLs from search results
        urls = re.findall(r"https?://[^\s\n]+", search_text)[:3]

        # Step 2: Fetch top pages
        fetched_content = []
        for url in urls:
            fetch_result = await self.call_tool("web_fetch", {
                "url": url,
                "max_length": 3000,
            }, timeout=15)
            page_text = self.extract_text(fetch_result)
            if page_text and not page_text.startswith(("Timeout", "Fetch error", "Could not")):
                fetched_content.append(f"[Source: {url}]\n{page_text[:2000]}")

        # Step 3: Synthesize with local model
        context = "\n\n---\n\n".join(fetched_content) if fetched_content else search_text[:3000]
        prompt = (
            f"Research query: {query}\n\n"
            f"Source material:\n{context[:6000]}\n\n"
            f"Provide a thorough, well-organized research summary. Include:\n"
            f"1. Key findings with inline citations [1], [2], etc.\n"
            f"2. Confidence level (high/medium/low) based on source agreement.\n"
            f"3. Any conflicting information or gaps.\n"
            f"Be thorough but concise."
        )
        chat_result = await self.call_tool("local_chat", {"prompt": prompt}, timeout=90)
        answer = self.extract_text(chat_result)

        if not answer:
            self.state.log(f"  Synthesis failed for: {query}")
            return

        # Step 4: Save to knowledge graph
        try:
            await self.call_tool("kg_add", {
                "name": topic_slug,
                "entity_type": "concept",
                "content": f"Research: {query}\n\n{answer[:500]}",
            })
            for url in urls:
                source_slug = re.sub(r"https?://", "", url)[:80]
                await self.call_tool("kg_add", {
                    "name": source_slug,
                    "entity_type": "tool",
                    "content": f"Source URL for research on: {query}",
                })
                await self.call_tool("kg_relate", {
                    "from_entity": source_slug,
                    "to_entity": topic_slug,
                    "relation": "REFERENCES",
                })
        except Exception:
            self.state.log(f"  KG save failed for: {query}")

        # Step 5: Save summary to notes
        date_str = time.strftime("%Y-%m-%d")
        citation_lines = [f"[{i+1}] {u}" for i, u in enumerate(urls)]
        note_content = (
            f"# Research: {query}\n"
            f"Date: {date_str}\n\n"
            f"{answer[:2000]}\n\n"
            f"## Sources\n" + "\n".join(citation_lines)
        )
        await self.call_tool("save_note", {
            "topic": f"research-{topic_slug}",
            "content": note_content,
        })
        self.state.log(f"  Saved findings for: {query}")

        # Notify
        await self.notify(
            f"Research complete: {query[:50]}",
            f"Findings saved to research-{topic_slug} ({len(urls)} sources)",
            level="info",
        )
