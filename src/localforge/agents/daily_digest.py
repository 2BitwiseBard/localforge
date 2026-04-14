"""Daily digest agent: aggregates outputs from all other agents into a summary.

Runs once daily, collecting:
  - News digests
  - Research findings
  - Code watcher findings
  - Health alerts
  - Git activity summary

Saves a consolidated daily summary to notes.
"""

import time

from .base import BaseAgent, TrustLevel
from .supervisor import register_agent


@register_agent
class DailyDigest(BaseAgent):
    name = "daily-digest"
    trust_level = TrustLevel.SAFE
    description = "Aggregates daily activity from all agents into a consolidated summary"

    async def run(self):
        date_str = time.strftime("%Y-%m-%d")
        self.state.log(f"Building daily digest for {date_str}")

        sections = []

        # --- News digest ---
        news = await self.call_tool("recall_note", {
            "topic": f"news-digest-{date_str}",
        })
        news_text = self.extract_text(news)
        if news_text and "not found" not in news_text.lower():
            # Trim to just the summary part
            summary_end = news_text.find("## Raw Articles")
            if summary_end > 0:
                news_text = news_text[:summary_end].strip()
            sections.append(f"## News\n\n{news_text[:1000]}")

        # --- Research findings ---
        # Check for any research notes from today
        research_notes = await self.call_tool("search_index", {
            "index_name": "__knowledge_base__",
            "query": f"research {date_str}",
        }, timeout=30)
        research_text = self.extract_text(research_notes)
        if research_text and "no " not in research_text.lower()[:20]:
            sections.append(f"## Research\n\n{research_text[:800]}")

        # --- Code watcher findings ---
        # Code watcher saves notes as code-watcher-{project}-{date}, find them via list_notes
        notes_result = await self.call_tool("list_notes", {})
        notes_text = self.extract_text(notes_result)
        code_findings = []
        if notes_text:
            for line in notes_text.splitlines():
                if "code-watcher" in line and date_str in line:
                    # Extract topic name and recall it
                    topic = line.strip().split(":")[0].strip().lstrip("- ")
                    if topic:
                        note = await self.call_tool("recall_note", {"topic": topic})
                        note_text = self.extract_text(note)
                        if note_text and "not found" not in note_text.lower():
                            code_findings.append(note_text[:400])
        if code_findings:
            sections.append("## Code Review Findings\n\n" + "\n\n".join(code_findings))

        # --- Health alerts ---
        alerts = await self.call_tool("recall_note", {
            "topic": f"alerts-{date_str}",
        })
        alerts_text = self.extract_text(alerts)
        if alerts_text and "not found" not in alerts_text.lower():
            sections.append(f"## Alerts\n\n{alerts_text[:500]}")

        # --- Git activity (from configured projects) ---
        # Check messages from code-watcher for project directories
        projects = self.config.get("projects", [])
        for project in projects[:3]:
            git_result = await self.call_tool("git_context", {
                "directory": project,
                "log_count": 10,
                "include_diff": False,
            }, timeout=15)
            git_text = self.extract_text(git_result)
            if git_text and "error" not in git_text.lower()[:20]:
                import os
                project_name = os.path.basename(os.path.expanduser(project))
                sections.append(f"## Git Activity: {project_name}\n\n{git_text[:500]}")

        if not sections:
            self.state.log("No activity to report for today")
            return

        # --- Synthesize ---
        raw_digest = "\n\n---\n\n".join(sections)
        self.state.log(f"Synthesizing {len(sections)} sections...")

        summary_result = await self.call_tool("local_chat", {
            "prompt": (
                f"Create a brief daily activity summary from the following sections. "
                f"Highlight the most important items. Be concise.\n\n"
                f"{raw_digest[:5000]}"
            ),
        }, timeout=90)
        summary = self.extract_text(summary_result)

        # --- Save ---
        digest_content = (
            f"# Daily Digest — {date_str}\n\n"
            f"{summary}\n\n"
            f"---\n\n"
            f"## Details\n\n{raw_digest[:3000]}"
        )
        await self.call_tool("save_note", {
            "topic": f"daily-digest-{date_str}",
            "content": digest_content,
        })
        self.state.log(f"Daily digest saved for {date_str} ({len(sections)} sections)")

        await self.notify(
            f"Daily digest ready ({date_str})",
            f"Summary of {len(sections)} activity areas",
            level="info",
        )
