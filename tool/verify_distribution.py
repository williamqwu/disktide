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
    wheel = _single(dist_dir.glob(f"sizetrail-{version}-*.whl"), "wheel")
    sdist = _single(dist_dir.glob(f"sizetrail-{version}.tar.gz"), "sdist")
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
            "sizetrail/__init__.py",
            "sizetrail/assets/default.tcss",
            "sizetrail/cleanup/actions.py",
            "sizetrail/cleanup/scoring.py",
            "sizetrail/cleanup/rulepacks/python.toml",
            "sizetrail/cleanup/rulepacks/node.toml",
            "sizetrail/cleanup/rulepacks/rust.toml",
            "sizetrail/cleanup/rulepacks/general.toml",
            "sizetrail/cleanup/rulepacks/ide.toml",
            "sizetrail/cleanup/rulepacks/containers.toml",
            "sizetrail/collectors/local_scanner.py",
            "sizetrail/collectors/events/__init__.py",
            "sizetrail/collectors/events/base.py",
            "sizetrail/collectors/events/native.py",
            "sizetrail/collectors/platform/linux.py",
            "sizetrail/domain/alerts.py",
            "sizetrail/domain/cleanup.py",
            "sizetrail/domain/delta.py",
            "sizetrail/domain/live_view.py",
            "sizetrail/domain/monitor.py",
            "sizetrail/domain/provisional.py",
            "sizetrail/domain/scan.py",
            "sizetrail/domain/snapshot.py",
            "sizetrail/domain/visualization.py",
            "sizetrail/extensions/cleanup_rules.py",
            "sizetrail/presentation/tui/viewmodels/visualization.py",
            "sizetrail/repositories/alerts.py",
            "sizetrail/repositories/cleanup.py",
            "sizetrail/repositories/monitors.py",
            "sizetrail/repositories/snapshots.py",
            "sizetrail/repositories/sqlite.py",
            "sizetrail/scanner/scheduler.py",
            "sizetrail/screens/cleanup.py",
            "sizetrail/services/alerts.py",
            "sizetrail/services/cleanup.py",
            "sizetrail/services/compare.py",
            "sizetrail/services/doctor.py",
            "sizetrail/services/monitor.py",
            "sizetrail/services/provisional.py",
            "sizetrail/services/retention.py",
            "sizetrail/services/scan.py",
            "sizetrail/services/scan_consumers.py",
            "sizetrail/services/snapshots.py",
            "sizetrail/services/visualization.py",
            "sizetrail/services/watch.py",
            "sizetrail/viz/layout.py",
            "sizetrail/widgets/alert_editor.py",
            "sizetrail/widgets/cleanup_modal.py",
            "sizetrail/widgets/cleanup_history.py",
            "sizetrail/widgets/cleanup_map.py",
            "sizetrail/widgets/monitor_editor.py",
            "sizetrail/widgets/growth_heatmap.py",
            "fs_monitor/__init__.py",
            "fs_monitor/__main__.py",
            f"sizetrail-{version}.dist-info/METADATA",
            f"sizetrail-{version}.dist-info/entry_points.txt",
        }
        missing = sorted(required - names)
        if missing:
            raise SystemExit(f"wheel missing required files: {missing}")
        forbidden = {
            "sizetrail/monitor/alerts.py",
            "sizetrail/monitor/scheduler.py",
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
            archive.read(f"sizetrail-{version}.dist-info/METADATA")
        )
        if metadata["Name"] != "sizetrail":
            raise SystemExit("wheel metadata project name is not sizetrail")
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
            f"sizetrail-{version}.dist-info/entry_points.txt"
        ).decode()
        for executable in ("sizetrail =", "fsmonitor =", "fsmonitor-cli ="):
            if executable not in entry_points:
                raise SystemExit(f"wheel entry point missing: {executable}")


def _verify_sdist(path: Path, version: str) -> None:
    prefix = f"sizetrail-{version}/"
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
        prefix + "docs/adr/0012-adaptive-live-scan-engine.md",
        prefix + "docs/adr/0013-cleanup-scale-and-race-safety.md",
        prefix + "docs/adr/0014-event-assisted-provisional-current-state.md",
        prefix + "src/sizetrail/assets/default.tcss",
        prefix + "src/sizetrail/cleanup/actions.py",
        prefix + "src/sizetrail/cleanup/scoring.py",
        prefix + "src/sizetrail/cleanup/rulepacks/python.toml",
        prefix + "src/sizetrail/cleanup/rulepacks/node.toml",
        prefix + "src/sizetrail/cleanup/rulepacks/rust.toml",
        prefix + "src/sizetrail/cleanup/rulepacks/general.toml",
        prefix + "src/sizetrail/cleanup/rulepacks/ide.toml",
        prefix + "src/sizetrail/cleanup/rulepacks/containers.toml",
        prefix + "src/sizetrail/collectors/local_scanner.py",
        prefix + "src/sizetrail/collectors/events/__init__.py",
        prefix + "src/sizetrail/collectors/events/base.py",
        prefix + "src/sizetrail/collectors/events/native.py",
        prefix + "src/sizetrail/domain/alerts.py",
        prefix + "src/sizetrail/domain/cleanup.py",
        prefix + "src/sizetrail/domain/delta.py",
        prefix + "src/sizetrail/domain/live_view.py",
        prefix + "src/sizetrail/domain/monitor.py",
        prefix + "src/sizetrail/domain/provisional.py",
        prefix + "src/sizetrail/domain/scan.py",
        prefix + "src/sizetrail/domain/snapshot.py",
        prefix + "src/sizetrail/domain/visualization.py",
        prefix + "src/sizetrail/extensions/cleanup_rules.py",
        prefix + "src/sizetrail/presentation/tui/viewmodels/visualization.py",
        prefix + "src/sizetrail/repositories/alerts.py",
        prefix + "src/sizetrail/repositories/cleanup.py",
        prefix + "src/sizetrail/repositories/monitors.py",
        prefix + "src/sizetrail/repositories/snapshots.py",
        prefix + "src/sizetrail/repositories/sqlite.py",
        prefix + "src/sizetrail/scanner/scheduler.py",
        prefix + "src/sizetrail/screens/cleanup.py",
        prefix + "src/sizetrail/services/alerts.py",
        prefix + "src/sizetrail/services/cleanup.py",
        prefix + "src/sizetrail/services/compare.py",
        prefix + "src/sizetrail/services/doctor.py",
        prefix + "src/sizetrail/services/monitor.py",
        prefix + "src/sizetrail/services/provisional.py",
        prefix + "src/sizetrail/services/retention.py",
        prefix + "src/sizetrail/services/scan.py",
        prefix + "src/sizetrail/services/scan_consumers.py",
        prefix + "src/sizetrail/services/snapshots.py",
        prefix + "src/sizetrail/services/visualization.py",
        prefix + "src/sizetrail/services/watch.py",
        prefix + "src/sizetrail/visualization_formatting.py",
        prefix + "src/sizetrail/viz/layout.py",
        prefix + "src/sizetrail/widgets/alert_editor.py",
        prefix + "src/sizetrail/widgets/cleanup_modal.py",
        prefix + "src/sizetrail/widgets/cleanup_history.py",
        prefix + "src/sizetrail/widgets/cleanup_map.py",
        prefix + "src/sizetrail/widgets/monitor_editor.py",
        prefix + "src/sizetrail/widgets/growth_heatmap.py",
        prefix + "src/fs_monitor/__init__.py",
        prefix + "src/fs_monitor/__main__.py",
        prefix + "tool/benchmark_wave12.py",
        prefix + "tool/benchmark_wave13.py",
        prefix + "tool/benchmark_wave14.py",
        prefix + "tool/benchmark_wave15.py",
    }
    missing = sorted(required - names)
    if missing:
        raise SystemExit(f"sdist missing required files: {missing}")
    forbidden = {
        prefix + "src/sizetrail/monitor/alerts.py",
        prefix + "src/sizetrail/monitor/scheduler.py",
    }
    duplicate_implementations = sorted(forbidden & names)
    if duplicate_implementations:
        raise SystemExit(
            "sdist contains retired monitor implementations: "
            f"{duplicate_implementations}"
        )


if __name__ == "__main__":
    raise SystemExit(main())
