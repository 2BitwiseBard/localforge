#!/usr/bin/env python3
"""
Cross-platform worker script parity checker.

Run from the repository root:
    python scripts/check_script_parity.py

Checks:
  1. ShellCheck-style: all bash scripts have 'set -euo pipefail'
  2. Env-var parity: every LOCALFORGE_* variable written to an env file in
     one script appears in all others (unless declared platform-specific below)
  3. Shared constants: default port, git repo URL, and registration endpoint
     are identical across all four scripts

Exit codes:
    0  all checks passed
    1  one or more issues found (details printed to stdout)
"""

import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Script registry
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).parent.parent

SCRIPTS: dict[str, Path] = {
    "linux":   REPO_ROOT / "scripts" / "setup-worker.sh",
    "darwin":  REPO_ROOT / "scripts" / "setup-worker-darwin.sh",
    "android": REPO_ROOT / "scripts" / "setup-worker-termux.sh",
    "windows": REPO_ROOT / "scripts" / "setup-worker.ps1",
}

BASH_SCRIPTS  = {"linux", "darwin", "android"}
PS_SCRIPTS    = {"windows"}

# ---------------------------------------------------------------------------
# Platform-specific env vars
#
# Maps LOCALFORGE_* var name -> set of platform keys where its ABSENCE is
# intentional.  Any var not listed here must appear in all four scripts.
# ---------------------------------------------------------------------------
PLATFORM_SPECIFIC: dict[str, set[str]] = {
    # Windows-only: model/llama-server paths managed by NSSM install
    "LOCALFORGE_MODEL_PATH":      {"linux", "darwin", "android"},
    "LOCALFORGE_INSTALL_DIR":     {"linux", "darwin", "android"},
    "LOCALFORGE_MODELS_DIR":      {"linux", "darwin", "android"},
    "LOCALFORGE_LLAMA_BIN":       {"linux", "darwin", "android"},
    # Windows-only: enrollment token is an *input* env-var fallback, not
    # something written to the persistent env file on any platform
    "LOCALFORGE_ENROLLMENT_TOKEN": {"linux", "darwin", "android"},
    # Android-only: battery/concurrency guards for phone workers
    "LOCALFORGE_MIN_BATTERY":     {"linux", "darwin", "windows"},
    "LOCALFORGE_MAX_CONCURRENT":  {"linux", "darwin", "windows"},
}

# ---------------------------------------------------------------------------
# Shared constants — value must appear verbatim in every script
# ---------------------------------------------------------------------------
SHARED_CONSTANTS: dict[str, str] = {
    "default worker port":      "8200",
    "git repository URL":       "https://github.com/2BitwiseBard/localforge",
    "registration endpoint":    "/api/mesh/register",
    "minimum Python version":   "3.11",
}

# Regex: match any LOCALFORGE_WORD token in a file
_LOCALFORGE_RE = re.compile(r"LOCALFORGE_([A-Z][A-Z0-9_]*)")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_localforge_vars(text: str) -> set[str]:
    """Return the set of 'LOCALFORGE_FOO' names that appear in *text*."""
    return {f"LOCALFORGE_{m}" for m in _LOCALFORGE_RE.findall(text)}


def _check_safe_shell(platform: str, text: str) -> list[str]:
    """Verify bash scripts declare safe-shell options."""
    if platform not in BASH_SCRIPTS:
        return []
    if "set -euo pipefail" not in text:
        return [
            f"SAFE-SHELL: {SCRIPTS[platform].name} is missing 'set -euo pipefail'"
        ]
    return []


def _check_env_parity(env_vars: dict[str, set[str]]) -> list[str]:
    """
    Cross-check LOCALFORGE_* variables across all four scripts.

    A variable is flagged when:
      - it appears in at least one script, AND
      - it is absent from another script, AND
      - that absence is NOT declared in PLATFORM_SPECIFIC
    """
    errors: list[str] = []
    all_vars = set().union(*env_vars.values())

    for var in sorted(all_vars):
        allowed_absent = PLATFORM_SPECIFIC.get(var, set())
        for platform, found in env_vars.items():
            if platform in allowed_absent:
                continue  # intentionally absent here
            if var not in found:
                present_in = sorted(p for p, vs in env_vars.items() if var in vs)
                errors.append(
                    f"PARITY: '{var}' found in [{', '.join(present_in)}] "
                    f"but MISSING from {SCRIPTS[platform].name} ({platform}).\n"
                    f"         Either add it to that script or register it in "
                    f"PLATFORM_SPECIFIC inside {Path(__file__).name}."
                )
    return errors


def _check_constants(scripts_text: dict[str, str]) -> list[str]:
    """Verify shared literal values appear in every script."""
    errors: list[str] = []
    for label, value in SHARED_CONSTANTS.items():
        for platform, text in scripts_text.items():
            if value not in text:
                errors.append(
                    f"CONSTANT: {label} ({value!r}) not found in "
                    f"{SCRIPTS[platform].name} ({platform})."
                )
    return errors


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_checks() -> list[str]:
    errors: list[str] = []
    scripts_text: dict[str, str] = {}

    # --- load all scripts ---------------------------------------------------
    for platform, path in SCRIPTS.items():
        if not path.exists():
            errors.append(f"MISSING FILE: {path} ({platform} script not found)")
            continue
        scripts_text[platform] = path.read_text()

    if errors:
        # Can't do parity checks without all files present
        return errors

    # --- per-script structural checks ---------------------------------------
    for platform, text in scripts_text.items():
        errors.extend(_check_safe_shell(platform, text))

    # --- cross-script env-var parity ----------------------------------------
    env_vars = {p: _find_localforge_vars(t) for p, t in scripts_text.items()}
    errors.extend(_check_env_parity(env_vars))

    # --- shared constant consistency ----------------------------------------
    errors.extend(_check_constants(scripts_text))

    return errors


def main() -> int:
    print("Worker script parity checker")
    print("=" * 60)

    errors = run_checks()

    if not errors:
        print("All checks passed.\n")
        for platform, path in SCRIPTS.items():
            text = path.read_text()
            var_count = len(_find_localforge_vars(text))
            print(f"  {platform:8s}  {path.name}  ({var_count} LOCALFORGE_* vars, "
                  f"{len(text.splitlines())} lines)")
        print()
        print("Shared constants verified in all scripts:")
        for label, value in SHARED_CONSTANTS.items():
            print(f"  {label}: {value!r}")
        return 0

    print(f"{len(errors)} issue(s) found:\n")
    for i, err in enumerate(errors, 1):
        print(f"  {i}. {err}")
    print()
    print("To fix env-var gaps: add the missing variable to the flagged script,")
    print("or — if it is intentionally absent on that platform — add an entry to")
    print(f"PLATFORM_SPECIFIC in {Path(__file__).name}.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
