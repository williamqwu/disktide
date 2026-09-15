"""Shared fixtures.

The scheduler invariant checks run for every test rather than only the
scanner modules: the existing suite already drives the scheduler through
hundreds of directory settles from TUI, CLI, monitor, and visualization
tests, and those settles were previously exercised without ever being
checked. Turning them into assertions costs roughly two percent of the
suite's runtime.
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
from pathlib import Path

import pytest

from tests.scheduler_invariants import install

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Set to "1" to run anyway. Named in the refusal, so nobody has to find it.
ALLOW_NETWORK_TMP_ENV = "DISKTIDE_ALLOW_NETWORK_TMP"


def _load_scratchguard():
    """`tool/scratchguard.py` by path: `tool/` is not an importable package."""
    spec = importlib.util.spec_from_file_location(
        "disktide_scratchguard", REPO_ROOT / "tool" / "scratchguard.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scratchguard = _load_scratchguard()


def temp_root(config) -> Path:
    """Where `tmp_path` will put this run's trees.

    `config._tmp_path_factory` is not built yet at `pytest_configure`, so the
    option is read directly and the unset case is resolved the way `tempfile`
    itself would -- which is the whole point: `tempfile` follows `TMPDIR`.
    """
    basetemp = config.getoption("basetemp", None)
    return Path(basetemp) if basetemp else Path(tempfile.gettempdir())


def network_tmp_refusal(root) -> str | None:
    """Why this temp root must not be used, or None to let the run proceed.

    Network, not home: a laptop whose `TMPDIR` sits under a local home is
    fine, and refusing it would only teach people to set the escape hatch.
    A *network* temp root is not fine -- the suite creates thousands of small
    files per run, and on the quota'd NFS home this project is developed on
    that is a measurable slice of an account's inode budget, spent silently.

    `scratchguard.network_refusal`, not `is_network_path`, so the rule is
    the guard's own one and not a second copy of it: a `TMPDIR` under
    `$DISKTIDE_SCRATCH` is a temp root somebody chose on purpose, and a
    site's scratch is a parallel filesystem by design.
    """
    if os.environ.get(ALLOW_NETWORK_TMP_ENV) == "1":
        return None
    found = scratchguard.network_refusal(root)
    if found is None:
        return None
    mount, fstype = found
    return (
        f"tests would write their trees under {root}, which is on {mount} "
        f"({fstype}) -- a network filesystem. This suite creates thousands "
        f"of small files per run and deletes them again; a network home is "
        f"usually quota'd by inode as well as by size. Fix it with "
        f"`export TMPDIR=/tmp`, or pass `--basetemp=/tmp/pytest-disktide`, "
        f"or point `${scratchguard.SCRATCH_ENV}` at the scratch filesystem "
        f"your site gave you for this. Set {ALLOW_NETWORK_TMP_ENV}=1 to run "
        f"here anyway."
    )


@pytest.fixture(autouse=True)
def scheduler_invariants(monkeypatch):
    """Validate scheduler structure on every state change, suite-wide."""
    install(monkeypatch)


@pytest.fixture(autouse=True)
def reset_cell_geometry():
    """Forget any calibrated or measured cell aspect between tests.

    The resolver's override, in-band report and probe cache are all
    process-global, so one test that calibrates an aspect — or that drives
    the app through a resize carrying a pixel size — would otherwise decide
    the disc geometry of every test that ran after it, under a randomized
    order that makes the failure look unrelated.
    """
    from disktide.viz.cellgeom import reset_cell_geometry as reset

    reset()
    yield
    reset()


@pytest.fixture(autouse=True)
def pin_color_depth():
    """Keep the developer's own terminal out of the test results.

    The resolved depth is an *input to product behaviour*: at sixteen
    colours the app renders with the ANSI theme whatever the config says,
    so a suite run from an Open OnDemand web shell (`xterm-16color`, which
    is what this project is developed in) would put every app test into a
    theme none of them named. Pinned to 256 -- what nothing-detected has
    always meant -- and reset after, so a test that wants a depth installs
    one and cannot leak it into the next.
    """
    from disktide.viz.colordepth import (
        ColorDepth, reset_color_depth, set_active_color_depth,
    )

    set_active_color_depth(ColorDepth("256", "test"))
    yield
    reset_color_depth()


@pytest.fixture(autouse=True)
def reset_global_rendering_state():
    """Return safe rendering and the colour scheme to their defaults.

    All three are process-wide and all three are set by any test that
    launches the app with a config that names them, so a test that reads
    them was previously at the mercy of the (randomized) test order.

    The ring shape is the one that bites hardest, because the config it
    comes from is the *developer's own*: `load_config()` with no path
    reads `~/.config/.../config.toml`, so one machine with `ring_shape`
    set turned every later `compute_sunburst` in that xdist worker into a
    chart the disc-geometry unit tests do not describe. `set_ring_shape`
    takes None as "whatever the default is", which is what a reset wants.
    """
    yield
    from disktide.rendering import set_ring_shape, set_safe_rendering
    from disktide.viz.colors import set_color_scheme

    set_safe_rendering(False)
    set_color_scheme("disktide")
    set_ring_shape(None)


@pytest.fixture(autouse=True)
def host_shape(monkeypatch):
    """Keep the host out of the test results.

    The core count is pinned on every run -- `os.cpu_count()` decides
    product behaviour here, not just speed, so a test left to read it can
    quietly assert the developer's machine. The rest is off unless
    `DISKTIDE_HOST_SHAPE` names a shape. See `tests/hostshape.py` for what
    each shape models and which red board it comes from.
    """
    from tests.hostshape import install

    install(monkeypatch)


def pytest_configure(config):
    """Refuse a network temp root, and reject a typo'd shape name, once.

    Both are startup checks rather than fixtures. `active_shapes` is called
    per-test as well, and a `UsageError` from a fixture is reported once per
    test -- fifteen hundred copies of the same message. Calling it here turns
    that into a single line before collection starts.
    """
    refusal = network_tmp_refusal(temp_root(config))
    if refusal is not None:
        pytest.exit(refusal, returncode=4)

    from tests.hostshape import active_shapes

    active_shapes()


def pytest_report_header(config):
    """Name the temp root and active shapes, so a red log explains itself."""
    root = temp_root(config)
    found = scratchguard.filesystem_type(root)
    where = f"{found[1]} at {found[0]}" if found else "unknown filesystem"
    lines = [f"temp root: {root} ({where})"]

    from tests.hostshape import ENV_VAR, active_shapes

    shapes = active_shapes()
    if shapes:
        lines.append(f"host shape: {', '.join(shapes)} (via {ENV_VAR})")
    return lines
