#!/usr/bin/env python3
"""
Cross-platform worker script parity checker.

Run from the repository root:
    python scripts/check_script_parity.py

Checks:
  1. Safe-shell mode: bash scripts have 'set -euo pipefail'; PowerShell has
     '$ErrorActionPreference = "Stop"'
  2. Env-var parity: every LOCALFORGE_* variable written to an env file in
     one script appears in all others (unless declared platform-specific below)
  3. Shared constants: default port, git repo URL, and registration endpoint
     are identical across all four scripts
  4. Env-file paths: each script references the expected platform-specific
     path pattern for its persistent env file

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

# ---------------------------------------------------------------------------
# Platform-specific env-file path patterns
#
# At least one substring from each list must appear in that platform's script.
# These anchor the persistent credential file to the correct OS location so
# a typo or copy-paste from another platform is caught immediately.
# ---------------------------------------------------------------------------
ENV_FILE_PATTERNS: dict[str, list[str]] = {
    "linux":   [".config/localforge"],
    "darwin":  ["Library/Application Support/LocalForge"],
    "android": [".localforge"],
    # Windows builds the path via Join-Path rather than a string literal
    "windows": [r'Join-Path $InstallDir "env.ps1"', r"LocalForge\env.ps1"],
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


def _check_ps_error_action(platform: str, text: str) -> list[str]:
    """Verify the PowerShell script sets $ErrorActionPreference = 'Stop'."""
    if platform not in PS_SCRIPTS:
        return []
    if (
        '$ErrorActionPreference = "Stop"' not in text
        and "$ErrorActionPreference = 'Stop'" not in text
    ):
        return [
            f"SAFE-SHELL: {SCRIPTS[platform].name} is missing "
            f"'$ErrorActionPreference = \"Stop\"' (PowerShell safe-mode)"
        ]
    return []


def _check_env_file_paths(scripts_text: dict[str, str]) -> list[str]:
    """
    Verify each script references the correct platform-specific env-file path.

    Catches copy-paste errors where a path from another platform's script
    ends up in the wrong place, and flags drift when the expected location
    changes without updating this registry.
    """
    errors: list[str] = []
    for platform, patterns in ENV_FILE_PATTERNS.items():
        text = scripts_text.get(platform, "")
        if not any(p in text for p in patterns):
            errors.append(
                f"ENV-PATH: {SCRIPTS[platform].name} ({platform}) does not reference "
                f"the expected env-file path. Expected one of: {patterns!r}.\n"
                f"         If the path changed intentionally, update ENV_FILE_PATTERNS "
                f"in {Path(__file__).name}."
            )
    return errors


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
        errors.extend(_check_ps_error_action(platform, text))

    # --- env-file path patterns ---------------------------------------------
    errors.extend(_check_env_file_paths(scripts_text))

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
        print()
        print("Env-file path patterns verified per platform:")
        for platform, patterns in ENV_FILE_PATTERNS.items():
            print(f"  {platform:8s}  {patterns[0]!r}")
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
