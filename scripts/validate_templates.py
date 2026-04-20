#!/usr/bin/env python3
"""Validate all YAML workflow templates in the repository against the schema models.

Scans every .yaml/.yml file in the repo tree. Any file whose top-level dict
contains a ``nodes`` key is treated as a workflow definition and must parse
cleanly via WorkflowDef and pass WorkflowDef.validate().

Usage:
    python scripts/validate_templates.py [repo_root]

Exits 0 if all found templates are valid, 1 if any fail.
"""

import sys
from pathlib import Path

# Support running without an editable install.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import yaml  # noqa: E402 — inserted after path tweak
from localforge.workflows.schema import WorkflowDef  # noqa: E402


def _find_workflow_yamls(root: Path) -> list[Path]:
    candidates: list[Path] = []
    for path in sorted(root.rglob("*.yaml")) + sorted(root.rglob("*.yml")):
        try:
            data = yaml.safe_load(path.read_text())
        except Exception:
            continue
        if isinstance(data, dict) and "nodes" in data:
            candidates.append(path)
    return candidates


def main() -> int:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).parent.parent

    yamls = _find_workflow_yamls(root)
    if not yamls:
        print("No workflow YAML files found.")
        return 0

    print(f"Validating {len(yamls)} workflow template(s) in {root}\n")

    failed = 0
    for path in yamls:
        rel = path.relative_to(root)
        try:
            wf = WorkflowDef.from_yaml(path)
            errors = wf.validate()
        except Exception as exc:
            print(f"  FAIL  {rel}")
            print(f"        Parse error: {exc}")
            failed += 1
            continue

        if errors:
            print(f"  FAIL  {rel}")
            for msg in errors:
                print(f"        • {msg}")
            failed += 1
        else:
            print(f"  OK    {rel}  ({wf.id})")

    print()
    if failed:
        print(f"{failed}/{len(yamls)} template(s) failed.")
        return 1
    print(f"All {len(yamls)} template(s) passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
