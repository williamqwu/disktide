"""Guards on what `import disktide.app` is allowed to drag in.

Reaching the welcome screen is almost entirely import cost, and on a
network filesystem that cost is the startup time: on an NFSv4 cluster
home a module file measured ~16 ms to fault in cold against ~0.4 ms once
the page cache held it, so the pre-welcome import graph is worth roughly
40x its size in first-launch latency. The services and the five mode
screens are therefore imported at first use rather than at module scope.

Nothing about that is enforced by the code itself -- a single convenience
import added to `app.py` silently undoes it -- so these tests pin the
boundary. They read `sys.modules` from a subprocess because the suite has
long since imported everything by the time any test runs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from disktide.app import DiskTideApp
from disktide.config import AppConfig


REPO_ROOT = Path(__file__).resolve().parents[1]

# Loaded on demand once the user picks a path, never before.
DEFERRED_SERVICES = (
    "disktide.services.scan",
    "disktide.services.monitor",
    "disktide.services.cleanup",
    "disktide.services.visualization",
)
DEFERRED_SCREENS = (
    "disktide.screens.explorer",
    "disktide.screens.cleanup",
    "disktide.screens.monitor",
    "disktide.screens.settings",
    "disktide.screens.fs_overview",
)
# Reached only through the monitor screen's trend chart.
DEFERRED_PLOT_BACKEND = ("plotext", "textual_plotext")
# Reached only when the cleanup service is built.
DEFERRED_CLEANUP = ("disktide.cleanup.rules", "disktide.cleanup.actions")

# `disktide.*` modules resident after `import disktide.app`. Counting only
# our own package keeps the budget stable across the 3.11-3.13 matrix and
# across Textual releases; it was 99 before the imports were deferred and
# is 27 now. The headroom is for a genuinely new startup dependency, not
# for a subsystem creeping back in.
MAX_DISKTIDE_MODULES = 40


def _modules_after_importing_app() -> list[str]:
    """`sys.modules` in a fresh interpreter that has imported the app."""
    env = os.environ.copy()
    env["PYTHONPATH"] = (
        f"{REPO_ROOT / 'src'}{os.pathsep}{env.get('PYTHONPATH', '')}"
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json, sys; import disktide.app; "
            "print(json.dumps(sorted(sys.modules)))",
        ],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def startup_modules() -> list[str]:
    return _modules_after_importing_app()


@pytest.mark.parametrize("module", DEFERRED_SERVICES)
def test_services_are_not_imported_for_the_welcome_screen(
    startup_modules, module
):
    assert module not in startup_modules


@pytest.mark.parametrize("module", DEFERRED_SCREENS)
def test_mode_screens_are_not_imported_for_the_welcome_screen(
    startup_modules, module
):
    assert module not in startup_modules


@pytest.mark.parametrize("module", DEFERRED_PLOT_BACKEND + DEFERRED_CLEANUP)
def test_chart_and_cleanup_backends_are_not_imported_at_startup(
    startup_modules, module
):
    assert module not in startup_modules


def test_welcome_screen_itself_stays_reachable(startup_modules):
    """The deferral must not have pushed the startup screen off the path."""
    assert "textual.app" in startup_modules


def test_disktide_import_footprint_stays_small(startup_modules):
    ours = [m for m in startup_modules if m.split(".")[0] == "disktide"]
    assert len(ours) <= MAX_DISKTIDE_MODULES, (
        f"{len(ours)} disktide modules load before the welcome screen "
        f"(budget {MAX_DISKTIDE_MODULES}): {sorted(ours)}"
    )


def test_services_build_on_first_use():
    """Deferring must be invisible: the attributes still answer."""
    app = DiskTideApp(config=AppConfig())
    try:
        assert app._scan_service is app._scan_service
        assert app._monitor_service._scan_service is app._scan_service
        assert app._visualization_service is app._visualization_service
        assert app._cleanup_service is app._cleanup_service
        assert app._cleanup_safe_action is app._cleanup_safe_action
    finally:
        app._monitor_service.shutdown(wait=True)


def test_quit_from_welcome_does_not_build_services():
    """Quitting early must not pay for the imports it just avoided."""
    app = DiskTideApp(config=AppConfig(), show_welcome=True)
    app._perform_quit()

    unbuilt = object()
    for name in (
        "scan_service", "monitor_service",
        "visualization_service", "cleanup_service",
    ):
        assert getattr(app, f"_DiskTideApp__{name}", unbuilt) is None
