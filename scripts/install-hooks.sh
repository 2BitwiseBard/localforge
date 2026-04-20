#!/usr/bin/env bash
# Install project git hooks by symlinking them from scripts/ into .git/hooks/.
#
# Usage:
#   bash scripts/install-hooks.sh
#
# Run this once after cloning if you plan to modify worker enrollment scripts.
# The hooks themselves live in scripts/ so they are code-reviewed with the rest
# of the project.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
HOOKS_DIR="$REPO_ROOT/.git/hooks"
SCRIPTS_DIR="$REPO_ROOT/scripts"

install_hook() {
    local name="$1"
    local src="$SCRIPTS_DIR/$name"
    local dest="$HOOKS_DIR/$name"

    if [[ ! -f "$src" ]]; then
        echo "  SKIP  $name (source not found: $src)"
        return
    fi

    chmod +x "$src"

    if [[ -e "$dest" && ! -L "$dest" ]]; then
        echo "  WARN  $dest already exists and is not a symlink — skipping."
        echo "        Merge manually or remove it and re-run this script."
        return
    fi

    # Relative symlink so the repo stays portable across machines
    ln -sf "../../scripts/$name" "$dest"
    echo "  OK    $dest  ->  ../../scripts/$name"
}

echo "Installing git hooks for localforge"
echo "===================================="
install_hook "pre-commit"
echo ""
echo "Done.  The pre-commit hook will run ShellCheck, PSScriptAnalyzer, and the"
echo "cross-platform parity checker whenever worker enrollment scripts are staged."
