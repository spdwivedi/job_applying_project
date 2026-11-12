#!/usr/bin/env bash
set -euo pipefail

# JobApplier-Core macOS/Linux Git hook bootstrapper.
# Copies scripts/hooks/pre-commit to .git/hooks/pre-commit, sets +x,
# and optionally installs pre-commit framework hooks if available.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null | tr -d '\r')" && [ -n "$REPO_ROOT" ]; then
    :
else
    REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
fi

SOURCE_HOOK="${REPO_ROOT}/scripts/hooks/pre-commit"
HOOKS_DIR="${REPO_ROOT}/.git/hooks"
TARGET_HOOK="${HOOKS_DIR}/pre-commit"

if [ ! -f "$SOURCE_HOOK" ]; then
    echo "Error: Canonical hook script not found at '${SOURCE_HOOK}'" >&2
    exit 1
fi

mkdir -p "$HOOKS_DIR"
cp "$SOURCE_HOOK" "$TARGET_HOOK"
chmod +x "$TARGET_HOOK"
chmod +x "$SOURCE_HOOK" "${REPO_ROOT}/scripts/setup_hooks.sh"

if command -v pre-commit >/dev/null 2>&1; then
    (cd "$REPO_ROOT" && pre-commit install)
    echo "Installed pre-commit framework hooks via 'pre-commit install'."
else
    echo "pre-commit CLI not installed (optional); using native .git/hooks/pre-commit."
fi

echo "Successfully installed JobApplier-Core pre-commit hook to: ${TARGET_HOOK}"
