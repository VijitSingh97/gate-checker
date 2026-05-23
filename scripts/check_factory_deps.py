#!/usr/bin/env python3
"""Verify that the factory-side scripts depend only on the Python stdlib.

The factory tooling (flash_base_station.py, provision_gate.py) is meant
to run on a fresh operator laptop with no `pip install` step required.
This script walks each tool's AST, collects every top-level imported
module, and flags any name that isn't in sys.stdlib_module_names.

Run manually before merging changes that touch the factory scripts, or
wire it into a pre-commit / CI step:

    python3 scripts/check_factory_deps.py

Exits 0 if all imports are stdlib, 1 otherwise.
"""

import ast
import sys
import sysconfig
from pathlib import Path

FACTORY_SCRIPTS = (
    "flash_base_station.py",
    "provision_gate.py",
)

# Project-local modules the factory scripts may legitimately import even
# though they aren't part of the stdlib. Each entry is a sibling .py file
# at the repo root that ships alongside the factory scripts and is itself
# stdlib-only.
LOCAL_MODULES: frozenset = frozenset({"factory_sticker"})


def stdlib_module_names() -> set:
    """Return the set of stdlib module names. Falls back gracefully on 3.9.

    sys.stdlib_module_names was added in Python 3.10. Older Pythons (still
    common as the macOS system interpreter) fall through to a probe that
    asks the importer whether each name is a built-in or stdlib origin.
    """
    if hasattr(sys, "stdlib_module_names"):
        return set(sys.stdlib_module_names)
    names: set = set(sys.builtin_module_names)
    stdlib_dir = Path(sysconfig.get_paths()["stdlib"])
    if stdlib_dir.is_dir():
        for entry in stdlib_dir.iterdir():
            if entry.is_dir() and (entry / "__init__.py").exists():
                names.add(entry.name)
            elif entry.suffix == ".py":
                names.add(entry.stem)
    return names


def imported_top_levels(source: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            # Skip relative imports (`from . import x`) — those are local
            # by definition; record the top-level for absolute imports.
            if node.level == 0 and node.module:
                names.add(node.module.split(".", 1)[0])
    return names


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    allowed = stdlib_module_names() | {"__future__"} | LOCAL_MODULES

    any_failed = False
    for name in FACTORY_SCRIPTS:
        path = repo_root / name
        if not path.exists():
            print(f"SKIP {name} (not found)")
            continue
        names = imported_top_levels(path.read_text(encoding="utf-8"))
        bad = sorted(names - allowed)
        if bad:
            print(f"FAIL {name}: non-stdlib imports → {', '.join(bad)}")
            any_failed = True
        else:
            print(f"OK   {name}")
    return 1 if any_failed else 0


if __name__ == "__main__":
    sys.exit(main())
