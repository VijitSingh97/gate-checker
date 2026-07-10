#!/bin/bash
# Lint the repo: ruff over Python, shellcheck over every tracked shell
# script (including extensionless ones in the rootfs overlays, found by
# shebang). Single source of truth — called by both CI (tests.yml) and
# the pre-commit hook.
#
# A missing tool is skipped with a warning and does not fail the run:
# the hook must stay usable on a fresh clone with no dev tools, and CI
# guarantees both tools are present so nothing is silently missed.

set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO_ROOT"

if command -v ruff >/dev/null 2>&1; then
  ruff check .
elif python3 -m ruff --version >/dev/null 2>&1; then
  python3 -m ruff check .
else
  echo "lint: ruff not installed — skipping Python lint." >&2
fi

if command -v shellcheck >/dev/null 2>&1; then
  git ls-files -z | while IFS= read -r -d '' f; do
    case "$f" in
      *.sh) printf '%s\0' "$f" ;;
      # `if` (not `&&`) so a non-matching final file can't turn into a
      # bogus non-zero loop status under pipefail.
      *) if head -c 32 "$f" 2>/dev/null |
        grep -qE '^#!/(usr/)?bin/(env )?(sh|bash|dash)'; then
        printf '%s\0' "$f"
      fi ;;
    esac
  done | xargs -0 -r shellcheck
else
  echo "lint: shellcheck not installed — skipping shell lint." >&2
fi
