#!/bin/bash
# Run the ranch-os unit test suite.
#
# Discovers tests under ./tests via stdlib unittest. The suite is
# stdlib-only with one exception: it imports `cryptography` (for real
# Fernet keys in the registry / pair tests). If that's not installed
# the script reports a clear "skipped — install cryptography" message
# and exits 0, so the hook layer can stay non-blocking for fresh
# contributors who haven't set up a dev venv yet.
#
# Exit codes:
#   0  all tests passed, OR cryptography wasn't installed and we
#      skipped cleanly.
#   1  tests ran and at least one failed.
#   2  tests couldn't be discovered (project layout broken).

set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)

# Pick the Python interpreter. PYTHON env var wins (for venvs); fall
# back to python3 on PATH. We require 3.10+ because the application
# code uses PEP 604 union syntax (`X | None`) at runtime.
PY="${PYTHON:-python3}"

if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    echo "ranch-os tests need Python 3.10+ (the app code uses 'X | None')."
    echo "Set PYTHON=/path/to/python3.10+ and try again."
    echo "Skipping — exit 0 so the pre-commit hook stays non-blocking."
    exit 0
fi

if ! "$PY" -c 'import cryptography' 2>/dev/null; then
    echo "ranch-os tests need 'cryptography' (real Fernet keys)."
    echo "Install with:  $PY -m pip install --user cryptography"
    echo "Skipping — exit 0 so the pre-commit hook stays non-blocking."
    exit 0
fi

cd "$REPO_ROOT"
# -b buffers test stdout/stderr and shows it only on failure — keeps
# pre-commit output quiet on the happy path. -v shows test names on
# every run; drop to default for less noise.
exec "$PY" -m unittest discover -t "$REPO_ROOT" -s tests -b "$@"
