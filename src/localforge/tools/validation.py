"""Workflow template validation tool.

Exposes the deterministic Python-model validation logic as an MCP tool so it
can be called from within workflow templates (e.g. strict-schema-validate.yaml)
and from any agent with at least MONITOR trust.

The tool reuses the same helpers used by:
  - scripts/validate_templates.py  (CI)
  - tests/test_template_validation.py  (pytest)
  - agents/yaml_schema_validator.py  (runtime watcher)

so all four validation surfaces are guaranteed to be consistent.
"""

import logging
from pathlib import Path

from localforge.tools import tool_handler
from localforge.workflows.scanner import discover_workflow_yamls, resolve_repo_root

log = logging.getLogger("localforge.tools.validation")


@tool_handler(
    name="validate_templates",
    description=(
        "Scan the repository for YAML workflow templates and strictly validate "
        "each one against the Python WorkflowDef schema models. "
        "Returns a plain-text report listing every template as OK or FAILED, "
        "with per-error details for failures. "
        "Use repo_root='auto' (the default) to detect the git repo root automatically."
    ),
    schema={
        "type": "object",
        "properties": {
            "repo_root": {
                "type": "string",
                "description": (
                    "Where to scan for YAML templates. "
                    "'auto' (default) detects the git repository root; "
                    "any other value is treated as a literal filesystem path."
                ),
            },
        },
        "required": [],
    },
)
async def validate_templates(args: dict) -> str:
    from localforge.workflows.schema import WorkflowDef

    root_value = args.get("repo_root", "auto") or "auto"
    try:
        root = resolve_repo_root(root_value)
    except Exception as exc:
        return f"Error: could not resolve repo root '{root_value}': {exc}"

    yaml_files = discover_workflow_yamls(root)
    if not yaml_files:
        return f"No workflow YAML files found under {root}."

    lines: list[str] = [f"Validating {len(yaml_files)} workflow template(s) in {root}", ""]

    passed: list[str] = []
    failed: list[tuple[str, list[str]]] = []

    for path in yaml_files:
        try:
            rel = path.relative_to(root)
        except ValueError:
            rel = path

        try:
            wf = WorkflowDef.from_yaml(path)
            errors = wf.validate()
        except Exception as exc:
            errors = [f"Parse error: {exc}"]

        if errors:
            failed.append((str(rel), errors))
            lines.append(f"  FAILED  {rel}")
            for err in errors:
                lines.append(f"          • {err}")
        else:
            passed.append(str(rel))
            lines.append(f"  OK      {rel}")

    lines.append("")
    if failed:
        lines.append(f"{len(failed)}/{len(yaml_files)} template(s) FAILED.")
    else:
        lines.append(f"All {len(yaml_files)} template(s) PASSED.")

    return "\n".join(lines)
