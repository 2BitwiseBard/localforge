"""Index maintainer agent: keeps RAG indexes up-to-date.

Responds to:
  - Scheduled cron runs (full incremental scan)
  - Bus messages from code_watcher (targeted re-index of changed directories)
"""

import os

from .base import BaseAgent, TrustLevel
from .supervisor import register_agent


@register_agent
class IndexMaintainer(BaseAgent):
    name = "index-maintainer"
    trust_level = TrustLevel.SAFE
    description = "Runs incremental_index on configured directories"

    async def on_trigger(self, trigger_type: str, payload: dict | None = None):
        """Handle chain trigger from code_watcher with specific directory."""
        self.state.log(f"Triggered via {trigger_type}")
        if payload and payload.get("source") == "code-watcher":
            # Chain trigger — check for bus messages about changed directories
            messages = await self.receive_messages(timeout=1)
            for msg in messages:
                if msg.topic == "code.changes_detected":
                    directory = msg.payload.get("directory", "")
                    if directory:
                        dir_path = os.path.expanduser(directory)
                        index_name = os.path.basename(dir_path)
                        self.state.log(f"Re-indexing changed directory: {index_name}")
                        result = await self.call_tool("incremental_index", {
                            "index_name": index_name,
                        }, timeout=120)
                        text = self.extract_text(result)
                        self.state.log(f"  {index_name}: {text[:100]}")
            return
        await self.run()

    async def run(self):
        directories = self.config.get("directories", [])
        if not directories:
            self.state.log("No directories configured, skipping")
            return

        for entry in directories:
            if isinstance(entry, str):
                index_name = entry.split("/")[-1]
                directory = entry
            elif isinstance(entry, dict):
                index_name = entry.get("name", entry["directory"].split("/")[-1])
                directory = entry["directory"]
            else:
                continue

            self.state.log(f"Updating index: {index_name}")
            result = await self.call_tool("incremental_index", {
                "index_name": index_name,
            }, timeout=120)
            text = self.extract_text(result)
            self.state.log(f"  {index_name}: {text[:100]}")
