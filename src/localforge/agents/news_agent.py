"""News agent: scrapes news on configured topics and saves digests.

Supports:
  - DuckDuckGo search (default)
  - RSS feeds (configurable in agents.yaml)
  - Deduplication via title/URL hashing
  - Categorized, structured digests
  - Knowledge graph integration (event entities)
"""

import hashlib
import re
import time

from .base import BaseAgent, TrustLevel
from .supervisor import register_agent


@register_agent
class NewsAgent(BaseAgent):
    name = "news-agent"
    trust_level = TrustLevel.SAFE
    description = "Scrapes news on configured topics, saves categorized digests to notes + KG"

    def _hash_article(self, title: str, url: str = "") -> str:
        """Generate a hash for dedup."""
        key = f"{title.lower().strip()[:100]}|{url.strip()[:200]}"
        return hashlib.md5(key.encode()).hexdigest()[:12]

    def _is_seen(self, article_hash: str) -> bool:
        """Check if article was seen in last 24h."""
        seen = self.state.data.get("seen_hashes", {})
        if article_hash in seen:
            # Check if it's still within 24h window
            if time.time() - seen[article_hash] < 86400:
                return True
        return False

    def _mark_seen(self, article_hash: str):
        """Mark article as seen."""
        seen = self.state.data.setdefault("seen_hashes", {})
        seen[article_hash] = time.time()
        # Prune entries older than 48h
        cutoff = time.time() - (48 * 3600)
        self.state.data["seen_hashes"] = {
            k: v for k, v in seen.items() if v > cutoff
        }

    async def _fetch_rss(self, feed_url: str, max_items: int = 5) -> list[dict]:
        """Fetch articles from an RSS feed via web_fetch + parsing."""
        try:
            result = await self.call_tool("web_fetch", {
                "url": feed_url,
                "max_length": 10000,
            }, timeout=15)
            text = self.extract_text(result)
            if not text or text.startswith(("Timeout", "Fetch error", "Could not")):
                return []

            # Simple RSS title extraction (works for most feeds)
            titles = re.findall(r"<title>([^<]+)</title>", text)
            links = re.findall(r"<link>([^<]+)</link>", text)
            descriptions = re.findall(r"<description>([^<]*)</description>", text)

            items = []
            # Skip first title/link (feed-level)
            for i in range(1, min(len(titles), max_items + 1)):
                items.append({
                    "title": titles[i] if i < len(titles) else "",
                    "url": links[i] if i < len(links) else "",
                    "body": descriptions[i] if i < len(descriptions) else "",
                })
            return items
        except Exception:
            return []

    async def run(self):
        topics = self.config.get("topics", [])
        rss_feeds = self.config.get("rss_feeds", [])
        max_articles = self.config.get("max_articles_per_topic", 3)

        if not topics and not rss_feeds:
            self.state.log("No topics or RSS feeds configured")
            return

        # Categorized articles: {category: [article_text, ...]}
        categorized: dict[str, list[str]] = {}
        total_new = 0

        # --- DuckDuckGo search per topic ---
        for topic in topics:
            self.state.log(f"Searching news: {topic}")
            result = await self.call_tool("web_search", {
                "query": f"{topic} news today",
                "max_results": max_articles,
            }, timeout=30)
            text = self.extract_text(result)

            if not text or text.startswith(("Search error", "No results")):
                continue

            # Parse individual results and dedup
            articles = text.split("\n\n")
            new_articles = []
            for article in articles:
                # Extract title line
                lines = article.strip().splitlines()
                if not lines:
                    continue
                title = lines[0].lstrip("[0-9] ").strip()
                url = ""
                for line in lines:
                    if line.startswith("http"):
                        url = line.strip()
                        break

                h = self._hash_article(title, url)
                if self._is_seen(h):
                    continue
                self._mark_seen(h)
                new_articles.append(article)
                total_new += 1

            if new_articles:
                categorized.setdefault(topic, []).extend(new_articles)

        # --- RSS feeds ---
        for feed in rss_feeds:
            if isinstance(feed, str):
                feed_url = feed
                feed_name = re.sub(r"https?://", "", feed).split("/")[0]
            elif isinstance(feed, dict):
                feed_url = feed.get("url", "")
                feed_name = feed.get("name", feed_url.split("/")[2] if "/" in feed_url else "RSS")
            else:
                continue

            self.state.log(f"Fetching RSS: {feed_name}")
            items = await self._fetch_rss(feed_url, max_articles)
            new_items = []
            for item in items:
                h = self._hash_article(item.get("title", ""), item.get("url", ""))
                if self._is_seen(h):
                    continue
                self._mark_seen(h)
                item_text = f"{item['title']}\n{item.get('url', '')}\n{item.get('body', '')}"
                new_items.append(item_text)
                total_new += 1

            if new_items:
                categorized.setdefault(f"RSS: {feed_name}", []).extend(new_items)

        if not categorized:
            self.state.log("No new articles found (dedup filtered all)")
            return

        # --- Build structured digest ---
        date_str = time.strftime("%Y-%m-%d")
        sections = []
        for category, articles in categorized.items():
            section = f"## {category}\n\n" + "\n\n".join(articles[:max_articles])
            sections.append(section)

        raw_digest = "\n\n---\n\n".join(sections)

        # --- Synthesize summary ---
        self.state.log(f"Generating summary ({total_new} new articles across {len(categorized)} categories)...")
        summary_result = await self.call_tool("local_chat", {
            "prompt": (
                f"Create a concise news briefing from these articles. "
                f"Organize by category/topic. For each topic:\n"
                f"- 1-2 sentence summary of key developments\n"
                f"- Note any particularly significant or breaking news\n\n"
                f"{raw_digest[:5000]}"
            ),
        }, timeout=90)
        summary = self.extract_text(summary_result)

        # --- Save to KG (as event entities) ---
        for category in categorized:
            topic_slug = re.sub(r"[^a-z0-9]+", "-", category.lower())[:50].strip("-")
            try:
                await self.call_tool("kg_add", {
                    "name": f"news-{topic_slug}-{date_str}",
                    "entity_type": "event",
                    "content": f"News digest for {category} on {date_str}",
                })
            except Exception:
                pass

        # --- Save digest note ---
        digest_content = (
            f"# News Briefing — {date_str}\n\n"
            f"*{total_new} new articles across {len(categorized)} categories*\n\n"
            f"{summary}\n\n"
            f"---\n\n"
            f"## Raw Articles\n\n{raw_digest[:3000]}"
        )
        await self.call_tool("save_note", {
            "topic": f"news-digest-{date_str}",
            "content": digest_content,
        })
        self.state.log(f"Saved news digest: {total_new} articles, {len(categorized)} categories")

        # Notify
        await self.notify(
            f"News digest ready ({date_str})",
            f"{total_new} new articles across {len(categorized)} categories",
            level="info",
        )
