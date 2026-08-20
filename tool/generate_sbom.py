#!/usr/bin/env python3
"""Generate a compact CycloneDX SBOM from a minimal installed wheel."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from importlib.metadata import distribution
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from dependency_budget import installed_distributions, normalize_name


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="fsmonitor-cli")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    root = distribution(args.project)
    root_name = root.metadata.get("Name") or args.project
    components = []
    for item in installed_distributions():
        name = item.metadata.get("Name") or "unknown"
        if normalize_name(name) == normalize_name(root_name):
            continue
        normalized = normalize_name(name)
        components.append({
            "type": "library",
            "bom-ref": f"pkg:pypi/{normalized}@{item.version}",
            "name": name,
            "version": item.version,
            "purl": f"pkg:pypi/{normalized}@{item.version}",
        })

    root_ref = f"pkg:pypi/{normalize_name(root_name)}@{root.version}"
    source_date = int(os.environ.get("SOURCE_DATE_EPOCH", "0") or "0")
    timestamp = datetime.fromtimestamp(
        source_date,
        tz=timezone.utc,
    ).isoformat().replace("+00:00", "Z")
    identity = "|".join(
        [root_ref] + [component["bom-ref"] for component in components]
    )
    payload = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": f"urn:uuid:{uuid5(NAMESPACE_URL, identity)}",
        "version": 1,
        "metadata": {
            "timestamp": timestamp,
            "component": {
                "type": "application",
                "bom-ref": root_ref,
                "name": root_name,
                "version": root.version,
                "purl": root_ref,
            },
        },
        "components": components,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(f"wrote {output} with {len(components)} runtime components")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
