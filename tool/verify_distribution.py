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
            "fs_monitor/cleanup/actions.py",
            "fs_monitor/cleanup/scoring.py",
            "fs_monitor/cleanup/rulepacks/python.toml",
            "fs_monitor/cleanup/rulepacks/node.toml",
            "fs_monitor/cleanup/rulepacks/rust.toml",
            "fs_monitor/cleanup/rulepacks/general.toml",
            "fs_monitor/cleanup/rulepacks/ide.toml",
            "fs_monitor/cleanup/rulepacks/containers.toml",
            "fs_monitor/collectors/local_scanner.py",
            "fs_monitor/collectors/events/__init__.py",
            "fs_monitor/collectors/events/base.py",
            "fs_monitor/collectors/events/native.py",
            "fs_monitor/collectors/platform/linux.py",
            "fs_monitor/domain/alerts.py",
            "fs_monitor/domain/cleanup.py",
            "fs_monitor/domain/delta.py",
            "fs_monitor/domain/live_view.py",
            "fs_monitor/domain/monitor.py",
            "fs_monitor/domain/scan.py",
            "fs_monitor/domain/snapshot.py",
            "fs_monitor/domain/visualization.py",
            "fs_monitor/extensions/cleanup_rules.py",
            "fs_monitor/presentation/tui/viewmodels/visualization.py",
            "fs_monitor/repositories/alerts.py",
            "fs_monitor/repositories/cleanup.py",
            "fs_monitor/repositories/monitors.py",
            "fs_monitor/repositories/snapshots.py",
            "fs_monitor/repositories/sqlite.py",
            "fs_monitor/scanner/scheduler.py",
            "fs_monitor/screens/cleanup.py",
            "fs_monitor/services/alerts.py",
            "fs_monitor/services/cleanup.py",
            "fs_monitor/services/compare.py",
            "fs_monitor/services/doctor.py",
            "fs_monitor/services/monitor.py",
            "fs_monitor/services/retention.py",
            "fs_monitor/services/scan.py",
            "fs_monitor/services/scan_consumers.py",
            "fs_monitor/services/snapshots.py",
            "fs_monitor/services/visualization.py",
            "fs_monitor/services/watch.py",
            "fs_monitor/viz/layout.py",
            "fs_monitor/widgets/alert_editor.py",
            "fs_monitor/widgets/cleanup_modal.py",
            "fs_monitor/widgets/cleanup_history.py",
            "fs_monitor/widgets/cleanup_map.py",
            "fs_monitor/widgets/monitor_editor.py",
            "fs_monitor/widgets/growth_heatmap.py",
            f"fsmonitor_cli-{version}.dist-info/METADATA",
            f"fsmonitor_cli-{version}.dist-info/entry_points.txt",
        }
        missing = sorted(required - names)
        if missing:
            raise SystemExit(f"wheel missing required files: {missing}")
        forbidden = {
            "fs_monitor/monitor/alerts.py",
            "fs_monitor/monitor/scheduler.py",
        }
        duplicate_implementations = sorted(forbidden & names)
        if duplicate_implementations:
            raise SystemExit(
                "wheel contains retired monitor implementations: "
                f"{duplicate_implementations}"
            )
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
        if "watch" not in metadata.get_all("Provides-Extra", []):
            raise SystemExit("wheel metadata missing watch extra")
        watch_requirements = [
            item
            for item in metadata.get_all("Requires-Dist", [])
            if "inotify-simple" in item and "extra == 'watch'" in item
        ]
        if not watch_requirements:
            raise SystemExit("wheel metadata missing conditional inotify-simple dependency")
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
        prefix + "docs/adr/0005-monitor-service-retention-and-alerts.md",
        prefix + "docs/adr/0006-space-time-visualization-contracts.md",
        prefix + "docs/adr/0007-cleanup-plan-safe-execution.md",
        prefix + "docs/adr/0008-cleanup-intelligence-rule-packs.md",
        prefix + "docs/adr/0009-optional-filesystem-event-acceleration.md",
        prefix + "docs/adr/0010-release-stabilization.md",
        prefix + "docs/adr/0011-scalable-space-time-data-plane.md",
        prefix + "src/fs_monitor/assets/default.tcss",
        prefix + "src/fs_monitor/cleanup/actions.py",
        prefix + "src/fs_monitor/cleanup/scoring.py",
        prefix + "src/fs_monitor/cleanup/rulepacks/python.toml",
        prefix + "src/fs_monitor/cleanup/rulepacks/node.toml",
        prefix + "src/fs_monitor/cleanup/rulepacks/rust.toml",
        prefix + "src/fs_monitor/cleanup/rulepacks/general.toml",
        prefix + "src/fs_monitor/cleanup/rulepacks/ide.toml",
        prefix + "src/fs_monitor/cleanup/rulepacks/containers.toml",
        prefix + "src/fs_monitor/collectors/local_scanner.py",
        prefix + "src/fs_monitor/collectors/events/__init__.py",
        prefix + "src/fs_monitor/collectors/events/base.py",
        prefix + "src/fs_monitor/collectors/events/native.py",
        prefix + "src/fs_monitor/domain/alerts.py",
        prefix + "src/fs_monitor/domain/cleanup.py",
        prefix + "src/fs_monitor/domain/delta.py",
        prefix + "src/fs_monitor/domain/live_view.py",
        prefix + "src/fs_monitor/domain/monitor.py",
        prefix + "src/fs_monitor/domain/scan.py",
        prefix + "src/fs_monitor/domain/snapshot.py",
        prefix + "src/fs_monitor/domain/visualization.py",
        prefix + "src/fs_monitor/extensions/cleanup_rules.py",
        prefix + "src/fs_monitor/presentation/tui/viewmodels/visualization.py",
        prefix + "src/fs_monitor/repositories/alerts.py",
        prefix + "src/fs_monitor/repositories/cleanup.py",
        prefix + "src/fs_monitor/repositories/monitors.py",
        prefix + "src/fs_monitor/repositories/snapshots.py",
        prefix + "src/fs_monitor/repositories/sqlite.py",
        prefix + "src/fs_monitor/scanner/scheduler.py",
        prefix + "src/fs_monitor/screens/cleanup.py",
        prefix + "src/fs_monitor/services/alerts.py",
        prefix + "src/fs_monitor/services/cleanup.py",
        prefix + "src/fs_monitor/services/compare.py",
        prefix + "src/fs_monitor/services/doctor.py",
        prefix + "src/fs_monitor/services/monitor.py",
        prefix + "src/fs_monitor/services/retention.py",
        prefix + "src/fs_monitor/services/scan.py",
        prefix + "src/fs_monitor/services/scan_consumers.py",
        prefix + "src/fs_monitor/services/snapshots.py",
        prefix + "src/fs_monitor/services/visualization.py",
        prefix + "src/fs_monitor/services/watch.py",
        prefix + "src/fs_monitor/visualization_formatting.py",
        prefix + "src/fs_monitor/viz/layout.py",
        prefix + "src/fs_monitor/widgets/alert_editor.py",
        prefix + "src/fs_monitor/widgets/cleanup_modal.py",
        prefix + "src/fs_monitor/widgets/cleanup_history.py",
        prefix + "src/fs_monitor/widgets/cleanup_map.py",
        prefix + "src/fs_monitor/widgets/monitor_editor.py",
        prefix + "src/fs_monitor/widgets/growth_heatmap.py",
        prefix + "tool/benchmark_wave12.py",
    }
    missing = sorted(required - names)
    if missing:
        raise SystemExit(f"sdist missing required files: {missing}")
    forbidden = {
        prefix + "src/fs_monitor/monitor/alerts.py",
        prefix + "src/fs_monitor/monitor/scheduler.py",
    }
    duplicate_implementations = sorted(forbidden & names)
    if duplicate_implementations:
        raise SystemExit(
            "sdist contains retired monitor implementations: "
            f"{duplicate_implementations}"
        )


if __name__ == "__main__":
    raise SystemExit(main())
