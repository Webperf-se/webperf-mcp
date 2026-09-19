#!/usr/bin/env python3
"""Fail if the package version and manifest.json disagree, or if a release tag
does not match them. Run in CI and before packing a bundle.

    python3 tools/check_versions.py            # package vs manifest
    python3 tools/check_versions.py v0.2.0     # ...and against a tag
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def package_version() -> str:
    init = (ROOT / "src" / "webperf_mcp" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', init, re.M)
    if not match:
        sys.exit("no __version__ in src/webperf_mcp/__init__.py")
    return match.group(1)


def main(argv: list[str]) -> int:
    pkg = package_version()
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))["version"]
    problems = []
    if pkg != manifest:
        problems.append(f"package {pkg} != manifest.json {manifest}")
    if len(argv) > 1:
        tag = argv[1].lstrip("v")
        if tag != pkg:
            problems.append(f"tag {argv[1]} != package {pkg}")
    if problems:
        for p in problems:
            print(f"version mismatch: {p}", file=sys.stderr)
        return 1
    print(f"versions agree: {pkg}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
