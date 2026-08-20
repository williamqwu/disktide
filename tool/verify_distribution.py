#!/usr/bin/env python3
"""Verify wheel/sdist metadata and required packaged files."""

from __future__ import annotations

import argparse
import tarfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", default="dist")
    args = parser.parse_args()

    dist_dir = Path(args.dist)
    project = tomllib.loads(Path("pyproject.toml").read_text())["project"]
    version = project["version"]
    wheel = _single(dist_dir.glob(f"fsmonitor_cli-{version}-*.whl"), "wheel")
    sdist = _single(dist_dir.glob(f"fsmonitor_cli-{version}.tar.gz"), "sdist")
    _verify_wheel(wheel, version)
    _verify_sdist(sdist, version)
    print(f"distribution verification: PASS ({wheel.name}, {sdist.name})")
    return 0


def _single(paths, label: str) -> Path:
    matches = list(paths)
    if len(matches) != 1:
        raise SystemExit(f"expected one {label}, found {len(matches)}")
    return matches[0]


def _verify_wheel(path: Path, version: str) -> None:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        required = {
            "fs_monitor/__init__.py",
            "fs_monitor/assets/default.tcss",
            "fs_monitor/collectors/local_scanner.py",
            "fs_monitor/collectors/platform/linux.py",
            "fs_monitor/domain/delta.py",
            "fs_monitor/domain/live_view.py",
            "fs_monitor/domain/scan.py",
            "fs_monitor/domain/snapshot.py",
            "fs_monitor/repositories/snapshots.py",
            "fs_monitor/repositories/sqlite.py",
            "fs_monitor/scanner/scheduler.py",
            "fs_monitor/services/compare.py",
            "fs_monitor/services/doctor.py",
            "fs_monitor/services/scan.py",
            "fs_monitor/services/scan_consumers.py",
            "fs_monitor/services/snapshots.py",
            f"fsmonitor_cli-{version}.dist-info/METADATA",
            f"fsmonitor_cli-{version}.dist-info/entry_points.txt",
        }
        missing = sorted(required - names)
        if missing:
            raise SystemExit(f"wheel missing required files: {missing}")
        native = sorted(
            name for name in names
            if name.endswith((".so", ".pyd", ".dylib"))
        )
        if native:
            raise SystemExit(f"wheel contains native extensions: {native}")
        metadata = BytesParser().parsebytes(
            archive.read(f"fsmonitor_cli-{version}.dist-info/METADATA")
        )
        if metadata["Version"] != version:
            raise SystemExit("wheel metadata version does not match pyproject")
        entry_points = archive.read(
            f"fsmonitor_cli-{version}.dist-info/entry_points.txt"
        ).decode()
        for executable in ("fsmonitor =", "fsmonitor-cli ="):
            if executable not in entry_points:
                raise SystemExit(f"wheel entry point missing: {executable}")


def _verify_sdist(path: Path, version: str) -> None:
    prefix = f"fsmonitor_cli-{version}/"
    with tarfile.open(path, "r:gz") as archive:
        names = set(archive.getnames())
    required = {
        prefix + "README.md",
        prefix + "pyproject.toml",
        prefix + "docs/user-guide.md",
        prefix + "docs/release-process.md",
        prefix + "src/fs_monitor/assets/default.tcss",
        prefix + "src/fs_monitor/collectors/local_scanner.py",
        prefix + "src/fs_monitor/domain/delta.py",
        prefix + "src/fs_monitor/domain/live_view.py",
        prefix + "src/fs_monitor/domain/scan.py",
        prefix + "src/fs_monitor/domain/snapshot.py",
        prefix + "src/fs_monitor/repositories/snapshots.py",
        prefix + "src/fs_monitor/repositories/sqlite.py",
        prefix + "src/fs_monitor/scanner/scheduler.py",
        prefix + "src/fs_monitor/services/compare.py",
        prefix + "src/fs_monitor/services/doctor.py",
        prefix + "src/fs_monitor/services/scan.py",
        prefix + "src/fs_monitor/services/scan_consumers.py",
        prefix + "src/fs_monitor/services/snapshots.py",
    }
    missing = sorted(required - names)
    if missing:
        raise SystemExit(f"sdist missing required files: {missing}")


if __name__ == "__main__":
    raise SystemExit(main())
