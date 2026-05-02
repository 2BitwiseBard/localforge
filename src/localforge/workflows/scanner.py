"""Utilities for discovering workflow YAML files in a directory tree.

Kept as a standalone module (no agent imports) so it can be used from:
  - scripts/validate_templates.py  (CI / pre-commit)
  - src/localforge/agents/yaml_schema_validator.py  (runtime agent)
  - tests/  (directly importable without the full agent stack)
"""

import subprocess
from pathlib import Path

import yaml


def resolve_repo_root(value: str) -> Path:
    """Return the scan root path.

    ``"auto"`` runs ``git rev-parse --show-toplevel`` to locate the repository;
    any other string is treated as a literal filesystem path.  Falls back to
    ``Path.cwd()`` when git is unavailable or the path is not inside a repo.
    """
    if value == "auto":
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                check=True,
            )
            return Path(result.stdout.strip())
        except Exception:
            return Path.cwd()
    return Path(value).expanduser().resolve()


def discover_workflow_yamls(root: Path) -> list[Path]:
    """Return all workflow YAML files under *root*.

    A file qualifies if it parses as a YAML mapping that contains a ``nodes``
    key — the same heuristic used by ``scripts/validate_templates.py`` and the
    pytest parametrize fixture, so results are always consistent across
    the CI script, the runtime agent, and the test suite.
    """
    results: list[Path] = []
    seen: set[Path] = set()
    for path in sorted(root.rglob("*.yaml")) + sorted(root.rglob("*.yml")):
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
