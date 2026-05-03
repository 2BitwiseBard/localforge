"""Code watcher agent: reviews recent git changes via local model."""

import os
import time

from .base import BaseAgent, TrustLevel
from .supervisor import register_agent


@register_agent
class CodeWatcher(BaseAgent):
    name = "code-watcher"
    trust_level = TrustLevel.SAFE
    description = "Reviews recent git changes and saves findings to notes"

    async def run(self):
        directories = self.config.get("directories", [])
        if not directories:
            self.state.log("No directories configured, skipping")
            return

        focus = self.config.get("focus", "bugs,security")

        for directory in directories:
            # Expand ~ and resolve path
            dir_path = os.path.expanduser(directory)
            self.state.log(f"Checking git changes in: {dir_path}")

            # Pass directory explicitly to git_context
            result = await self.call_tool(
                "git_context",
                {
                    "directory": dir_path,
                    "include_diff": True,
                    "log_count": 5,
                },
                timeout=30,
            )
            text = self.extract_text(result)

            if not text or "no changes" in text.lower() or "clean" in text.lower():
                self.state.log(f"  No changes in {dir_path}")
                continue

            # Check for diff content
            if "diff" not in text.lower() and "status" not in text.lower():
                self.state.log(f"  No diff found in {dir_path}")
                continue

            # Review the changes
            review = await self.call_tool(
                "review_diff",
                {
                    "diff": text,
                    "focus": focus,
                },
                timeout=90,
            )
            review_text = self.extract_text(review)

            if not review_text:
                self.state.log(f"  Review returned empty for {dir_path}")
                continue

            issue_words = [
                "issue",
                "bug",
                "vulnerability",
                "error",
                "warning",
                "critical",
                "security",
                "unsafe",
                "unwrap",
            ]
            if any(word in review_text.lower() for word in issue_words):
                dir_name = os.path.basename(dir_path)
                date_str = time.strftime("%Y-%m-%d")
                self.state.log(f"  Issues found in {dir_name}, saving to notes")
                await self.call_tool(
                    "save_note",
                    {
                        "topic": f"code-watcher-{dir_name}-{date_str}",
                        "content": f"Code review findings for {dir_name} ({date_str}):\n\n{review_text[:1500]}",
                    },
                )
                # Notify about findings
                await self.notify(
                    f"Code issues in {dir_name}",
                    f"Code watcher found issues in {dir_path}. See note: code-watcher-{dir_name}-{date_str}",
                    level="warning",
                )
            else:
                self.state.log(f"  No issues found in {dir_path}")

            # Send changed file info to index-maintainer via bus
            await self.send_message(
                "code.changes_detected",
                {
                    "directory": dir_path,
                    "agent": self.agent_id,
                },
            )
