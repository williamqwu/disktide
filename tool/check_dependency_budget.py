#!/usr/bin/env python3
"""Fail when the minimal runtime installation exceeds the core budget."""

from __future__ import annotations

import argparse
import json

from dependency_budget import (
    distribution_files,
    installed_distributions,
    installed_size,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="disktide")
    parser.add_argument("--max-distributions", type=int, default=20)
    parser.add_argument("--max-mib", type=float, default=20.0)
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args()

    distributions = installed_distributions()
    files, native = distribution_files(distributions)
    size_bytes = installed_size(files)
    max_bytes = int(args.max_mib * 1024 * 1024)
    payload = {
        "project": args.project,
        "distribution_count": len(distributions),
        "max_distributions": args.max_distributions,
        "installed_bytes": size_bytes,
        "max_bytes": max_bytes,
        "native_extensions": [str(path) for path in native],
        "distributions": [
            {
                "name": item.metadata.get("Name") or "unknown",
                "version": item.version,
            }
            for item in distributions
        ],
    }
    passed = (
        len(distributions) <= args.max_distributions
        and size_bytes <= max_bytes
        and not native
    )
    payload["passed"] = passed

    if args.json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        size_mib = size_bytes / (1024 * 1024)
        print(
            f"runtime distributions: {len(distributions)} / "
            f"{args.max_distributions}"
        )
        print(f"installed size: {size_mib:.2f} MiB / {args.max_mib:.2f} MiB")
        print(f"native extensions: {len(native)}")
        for item in payload["distributions"]:
            print(f"  {item['name']}=={item['version']}")
        print("dependency budget: PASS" if passed else "dependency budget: FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
