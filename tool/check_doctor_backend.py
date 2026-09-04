#!/usr/bin/env python3
"""Assert which directory reader a `disktide doctor --json` report names.

The release workflow installs disktide four ways -- the platform wheel for
the runner, the sdist with a compiler, the sdist without one -- and each of
them has an expected scanner backend. The build hook is deliberately
forgiving (no compiler means a pure wheel, quietly), so without a check like
this a container with a broken toolchain would publish a platform-tagged
wheel with nothing platform-specific inside it, and nobody would find out
until somebody profiled a scan.

Usage:
    python tool/check_doctor_backend.py native|python REPORT.json [REPORT.json ...]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    expected, reports = argv[0], argv[1:]
    failures = 0
    for report in reports:
        payload = json.loads(Path(report).read_text())
        scanner = payload["platform"]["scanner"]
        backend = scanner["backend"]
        status = "OK" if backend == expected else "WRONG"
        print(f"{report}: scanner={backend} ({scanner['reason']}) [{status}]")
        failures += backend != expected
    if failures:
        print(
            f"expected the {expected} scanner backend in {failures} report(s)",
            file=sys.stderr,
        )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
