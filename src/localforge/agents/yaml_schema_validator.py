"""YAML schema validator agent: validates all workflow templates against the Python schema models.

Triggered by:
  - file_watch on schema.py or any .yaml/.yml file (catches drift when models or templates change)
  - cron schedule for periodic sweeps
  - manual trigger from the dashboard

On each run:
  1. Discovers every .yaml/.yml file under configured ``watch_dirs``.
  2. Filters to files whose top-level dict contains a ``nodes`` key.
  3. Loads via WorkflowDef.from_yaml() and calls WorkflowDef.validate().
  4. Saves a summary note; fires a warning notification for any failures.

Example agents.yaml entry::

    yaml-schema-validator:
      type: yaml-schema-validator
      enabled: true
      trigger:
        type: file_watch
        paths:
          - src/localforge/workflows/schema.py
          - src/localforge/workflows/templates
      config:
        watch_dirs:
          - src/localforge/workflows/templates
"""

import time
from pathlib import Path

import yaml

from .base import BaseAgent, TrustLevel
from .supervisor import register_agent


@register_agent
class YamlSchemaValidator(BaseAgent):
    name = "yaml-schema-validator"
    trust_level = TrustLevel.MONITOR
    description = (
        "Validates YAML workflow templates against schema models; "
        "alerts on drift when schema.py or templates change"
    )

    async def on_trigger(self, trigger_type: str, payload: dict | None = None):
        self.state.log(f"Triggered via {trigger_type}")
        if payload and payload.get("changed_file"):
            self.state.log(f"Change detected: {payload['changed_file']}")
        await self.run()

    async def run(self):
        from localforge.workflows.schema import WorkflowDef

        watch_dirs = self.config.get("watch_dirs", [])
        if not watch_dirs:
            # Default to the built-in templates directory relative to this file
            watch_dirs = [
                str(Path(__file__).parent.parent / "workflows" / "templates")
            ]

        yaml_files = self._discover_workflow_yamls(watch_dirs)
        if not yaml_files:
            self.state.log("No workflow YAML files found in watch_dirs")
            return

        self.state.log(f"Validating {len(yaml_files)} workflow template(s)…")

        passed: list[str] = []
        failed: list[tuple[str, list[str]]] = []

        for path in yaml_files:
            try:
                wf = WorkflowDef.from_yaml(path)
                errors = wf.validate()
            except Exception as exc:
                failed.append((str(path), [f"Parse error: {exc}"]))
                continue

            if errors:
                failed.append((str(path), errors))
            else:
                passed.append(str(path))

        total = len(yaml_files)
        date_str = time.strftime("%Y-%m-%d")
        run_ts = time.strftime("%Y-%m-%d %H:%M:%S")

        if failed:
            lines = [
                f"# YAML Schema Validation Report — {run_ts}",
                f"",
                f"**{len(failed)}/{total} template(s) FAILED**",
            ]
            for path, errors in failed:
                lines.append(f"\n## {Path(path).name}")
                for err in errors:
                    lines.append(f"  • {err}")
            if passed:
                lines.append(f"\n## Passed ({len(passed)})")
                for p in passed:
                    lines.append(f"  ✓ {Path(p).name}")

            await self.call_tool("save_note", {
                "topic": f"yaml-validation-{date_str}",
                "content": "\n".join(lines),
            })
            await self.notify(
                f"YAML schema validation: {len(failed)} failure(s)",
                (
                    f"{len(failed)} of {total} template(s) failed schema validation. "
                    f"See note: yaml-validation-{date_str}"
                ),
                level="warning",
            )
            await self.send_message("yaml_validator.failures_detected", {
                "failed": [p for p, _ in failed],
                "total": total,
                "agent": self.agent_id,
            })
        else:
            summary_lines = [
                f"# YAML Schema Validation Report — {run_ts}",
                f"",
                f"All {total} template(s) passed.",
            ] + [f"  ✓ {Path(p).name}" for p in passed]

            await self.call_tool("save_note", {
                "topic": f"yaml-validation-{date_str}",
                "content": "\n".join(summary_lines),
            })
            self.state.log(f"All {total} template(s) passed validation.")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _discover_workflow_yamls(self, watch_dirs: list[str]) -> list[Path]:
        """Return all .yaml/.yml files under watch_dirs that look like workflow defs."""
        results: list[Path] = []
        seen: set[Path] = set()
        for dir_str in watch_dirs:
            root = Path(dir_str).expanduser().resolve()
            if not root.exists():
                self.state.log(f"Warning: watch_dir does not exist: {root}")
                continue
            candidates = sorted(root.rglob("*.yaml")) + sorted(root.rglob("*.yml"))
            for path in candidates:
                if path in seen:
                    continue
                seen.add(path)
                try:
                    data = yaml.safe_load(path.read_text())
                except Exception:
                    continue
                if isinstance(data, dict) and "nodes" in data:
                    results.append(path)
        return results
