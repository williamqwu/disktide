#!/usr/bin/env python3
"""Verify wheel/sdist metadata and required packaged files."""

from __future__ import annotations

import argparse
import tarfile
import zipfile
from email.parser import BytesParser
from pathlib import Path

# Inlined rather than imported from disktide._compat so the script stays
# runnable straight from a checkout, on 3.10, without disktide installed.
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


#: The one native extension disktide is allowed to ship. Optional: a wheel
#: built where no compiler was found carries none, imports
#: `disktide/scanner/_scanfast_py.py` instead, and is just as valid.
_ACCELERATOR = "disktide/scanner/_scanfast"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", default="dist")
    args = parser.parse_args()

    dist_dir = Path(args.dist)
    project = tomllib.loads(Path("pyproject.toml").read_text())["project"]
    version = project["version"]
    # A release builds one wheel per platform and interpreter, so this takes
    # however many it finds -- but never none, and every one of them has to
    # pass. A local `uv build` produces exactly one.
    wheels = sorted(dist_dir.glob(f"disktide-{version}-*.whl"))
    if not wheels:
        raise SystemExit("expected at least one wheel, found none")
    sdist = _single(dist_dir.glob(f"disktide-{version}.tar.gz"), "sdist")
    shapes = []
    for wheel in wheels:
        shapes.append(f"{wheel.name} [{_verify_wheel(wheel, version)}]")
    _verify_sdist(sdist, version)
    print(f"distribution verification: PASS ({', '.join(shapes)}, {sdist.name})")
    return 0


def _single(paths, label: str) -> Path:
    matches = list(paths)
    if len(matches) != 1:
        raise SystemExit(f"expected one {label}, found {len(matches)}")
    return matches[0]


def _verify_wheel(path: Path, version: str) -> str:
    """Check one wheel; return "native" or "pure" for the summary line."""
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
            "disktide/keys.py",
            "disktide/commands.py",
            "disktide/presentation/tui/viewmodels/visualization.py",
            "disktide/repositories/alerts.py",
            "disktide/repositories/cleanup.py",
            "disktide/repositories/monitors.py",
            "disktide/repositories/snapshots.py",
            "disktide/repositories/sqlite.py",
            "disktide/scanner/accel.py",
            "disktide/scanner/_scanfast_py.py",
            "disktide/scanner/scheduler.py",
            "disktide/screens/cleanup.py",
            "disktide/screens/keymap.py",
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
        # Two wheel shapes are legal and the difference is one optional
        # accelerator: a `cp3xx` wheel carrying `disktide/scanner/_scanfast`
        # built for that interpreter, or a `py3-none-any` wheel carrying no
        # compiled code at all. Anything *else* compiled is a dependency
        # that has quietly stopped being pure, which is what this check has
        # always been here to catch.
        native = sorted(
            name for name in names
            if name.endswith((".so", ".pyd", ".dylib"))
        )
        unexpected = [
            name for name in native if not name.startswith(_ACCELERATOR)
        ]
        if unexpected:
            raise SystemExit(f"wheel contains native extensions: {unexpected}")
        wheel_metadata = BytesParser().parsebytes(
            archive.read(f"disktide-{version}.dist-info/WHEEL")
        )
        pure = wheel_metadata["Root-Is-Purelib"] == "true"
        if native and pure:
            raise SystemExit(
                f"wheel carries {native} but claims Root-Is-Purelib: true"
            )
        if not native and not pure:
            raise SystemExit(
                "wheel has no accelerator but is tagged as platform specific"
            )
        if len(native) > 1:
            raise SystemExit(f"wheel carries more than one accelerator: {native}")
        if native and "disktide/scanner/_scanfast_py.py" not in names:
            raise SystemExit("accelerated wheel is missing the Python fallback")
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
        return "native" if native else "pure"


def _verify_sdist(path: Path, version: str) -> None:
    prefix = f"disktide-{version}/"
    with tarfile.open(path, "r:gz") as archive:
        names = set(archive.getnames())
    required = {
        prefix + "README.md",
        prefix + "pyproject.toml",
        prefix + "docs/user-guide.md",
        prefix + "docs/release-process.md",
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
        prefix + "src/disktide/keys.py",
        prefix + "src/disktide/commands.py",
        prefix + "src/disktide/presentation/tui/viewmodels/visualization.py",
        prefix + "src/disktide/repositories/alerts.py",
        prefix + "src/disktide/repositories/cleanup.py",
        prefix + "src/disktide/repositories/monitors.py",
        prefix + "src/disktide/repositories/snapshots.py",
        prefix + "src/disktide/repositories/sqlite.py",
        prefix + "hatch_build.py",
        prefix + "src/disktide/scanner/accel.py",
        prefix + "src/disktide/scanner/_scanfast.c",
        prefix + "src/disktide/scanner/_scanfast_py.py",
        prefix + "src/disktide/scanner/scheduler.py",
        prefix + "src/disktide/screens/cleanup.py",
        prefix + "src/disktide/screens/keymap.py",
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
