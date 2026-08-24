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
    wheel = _single(dist_dir.glob(f"disktide-{version}-*.whl"), "wheel")
    sdist = _single(dist_dir.glob(f"disktide-{version}.tar.gz"), "sdist")
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
            "disktide/__init__.py",
            "disktide/assets/default.tcss",
            "disktide/cleanup/actions.py",
            "disktide/cleanup/scoring.py",
            "disktide/cleanup/rulepacks/python.toml",
            "disktide/cleanup/rulepacks/node.toml",
            "disktide/cleanup/rulepacks/rust.toml",
            "disktide/cleanup/rulepacks/general.toml",
            "disktide/cleanup/rulepacks/ide.toml",
            "disktide/cleanup/rulepacks/containers.toml",
            "disktide/collectors/local_scanner.py",
            "disktide/collectors/events/__init__.py",
            "disktide/collectors/events/base.py",
            "disktide/collectors/events/native.py",
            "disktide/collectors/platform/linux.py",
            "disktide/domain/alerts.py",
            "disktide/domain/cleanup.py",
            "disktide/domain/delta.py",
            "disktide/domain/live_view.py",
            "disktide/domain/monitor.py",
            "disktide/domain/provisional.py",
            "disktide/domain/scan.py",
            "disktide/domain/snapshot.py",
            "disktide/domain/visualization.py",
            "disktide/extensions/cleanup_rules.py",
            "disktide/presentation/tui/viewmodels/visualization.py",
            "disktide/repositories/alerts.py",
            "disktide/repositories/cleanup.py",
            "disktide/repositories/monitors.py",
            "disktide/repositories/snapshots.py",
            "disktide/repositories/sqlite.py",
            "disktide/scanner/scheduler.py",
            "disktide/screens/cleanup.py",
            "disktide/services/alerts.py",
            "disktide/services/cleanup.py",
            "disktide/services/compare.py",
            "disktide/services/doctor.py",
            "disktide/services/monitor.py",
            "disktide/services/provisional.py",
            "disktide/services/retention.py",
            "disktide/services/scan.py",
            "disktide/services/scan_consumers.py",
            "disktide/services/snapshots.py",
            "disktide/services/visualization.py",
            "disktide/services/watch.py",
            "disktide/viz/layout.py",
            "disktide/widgets/alert_editor.py",
            "disktide/widgets/cleanup_modal.py",
            "disktide/widgets/cleanup_history.py",
            "disktide/widgets/cleanup_map.py",
            "disktide/widgets/monitor_editor.py",
            "disktide/widgets/growth_heatmap.py",
            "sizetrail/__init__.py",
            "sizetrail/__main__.py",
            "fs_monitor/__init__.py",
            "fs_monitor/__main__.py",
            f"disktide-{version}.dist-info/METADATA",
            f"disktide-{version}.dist-info/entry_points.txt",
        }
        missing = sorted(required - names)
        if missing:
            raise SystemExit(f"wheel missing required files: {missing}")
        forbidden = {
            "disktide/monitor/alerts.py",
            "disktide/monitor/scheduler.py",
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
            archive.read(f"disktide-{version}.dist-info/METADATA")
        )
        if metadata["Name"] != "disktide":
            raise SystemExit("wheel metadata project name is not disktide")
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
            f"disktide-{version}.dist-info/entry_points.txt"
        ).decode()
        for executable in (
            "disktide =",
            "sizetrail =",
            "fsmonitor =",
            "fsmonitor-cli =",
        ):
            if executable not in entry_points:
                raise SystemExit(f"wheel entry point missing: {executable}")


def _verify_sdist(path: Path, version: str) -> None:
    prefix = f"disktide-{version}/"
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
        prefix + "src/disktide/assets/default.tcss",
        prefix + "src/disktide/cleanup/actions.py",
        prefix + "src/disktide/cleanup/scoring.py",
        prefix + "src/disktide/cleanup/rulepacks/python.toml",
        prefix + "src/disktide/cleanup/rulepacks/node.toml",
        prefix + "src/disktide/cleanup/rulepacks/rust.toml",
        prefix + "src/disktide/cleanup/rulepacks/general.toml",
        prefix + "src/disktide/cleanup/rulepacks/ide.toml",
        prefix + "src/disktide/cleanup/rulepacks/containers.toml",
        prefix + "src/disktide/collectors/local_scanner.py",
        prefix + "src/disktide/collectors/events/__init__.py",
        prefix + "src/disktide/collectors/events/base.py",
        prefix + "src/disktide/collectors/events/native.py",
        prefix + "src/disktide/domain/alerts.py",
        prefix + "src/disktide/domain/cleanup.py",
        prefix + "src/disktide/domain/delta.py",
        prefix + "src/disktide/domain/live_view.py",
        prefix + "src/disktide/domain/monitor.py",
        prefix + "src/disktide/domain/provisional.py",
        prefix + "src/disktide/domain/scan.py",
        prefix + "src/disktide/domain/snapshot.py",
        prefix + "src/disktide/domain/visualization.py",
        prefix + "src/disktide/extensions/cleanup_rules.py",
        prefix + "src/disktide/presentation/tui/viewmodels/visualization.py",
        prefix + "src/disktide/repositories/alerts.py",
        prefix + "src/disktide/repositories/cleanup.py",
        prefix + "src/disktide/repositories/monitors.py",
        prefix + "src/disktide/repositories/snapshots.py",
        prefix + "src/disktide/repositories/sqlite.py",
        prefix + "src/disktide/scanner/scheduler.py",
        prefix + "src/disktide/screens/cleanup.py",
        prefix + "src/disktide/services/alerts.py",
        prefix + "src/disktide/services/cleanup.py",
        prefix + "src/disktide/services/compare.py",
        prefix + "src/disktide/services/doctor.py",
        prefix + "src/disktide/services/monitor.py",
        prefix + "src/disktide/services/provisional.py",
        prefix + "src/disktide/services/retention.py",
        prefix + "src/disktide/services/scan.py",
        prefix + "src/disktide/services/scan_consumers.py",
        prefix + "src/disktide/services/snapshots.py",
        prefix + "src/disktide/services/visualization.py",
        prefix + "src/disktide/services/watch.py",
        prefix + "src/disktide/visualization_formatting.py",
        prefix + "src/disktide/viz/layout.py",
        prefix + "src/disktide/widgets/alert_editor.py",
        prefix + "src/disktide/widgets/cleanup_modal.py",
        prefix + "src/disktide/widgets/cleanup_history.py",
        prefix + "src/disktide/widgets/cleanup_map.py",
        prefix + "src/disktide/widgets/monitor_editor.py",
        prefix + "src/disktide/widgets/growth_heatmap.py",
        prefix + "src/sizetrail/__init__.py",
        prefix + "src/sizetrail/__main__.py",
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
        prefix + "src/disktide/monitor/alerts.py",
        prefix + "src/disktide/monitor/scheduler.py",
    }
    duplicate_implementations = sorted(forbidden & names)
    if duplicate_implementations:
        raise SystemExit(
            "sdist contains retired monitor implementations: "
            f"{duplicate_implementations}"
        )


if __name__ == "__main__":
    raise SystemExit(main())
