"""Shared fixtures.

The scheduler invariant checks run for every test rather than only the
scanner modules: the existing suite already drives the scheduler through
hundreds of directory settles from TUI, CLI, monitor, and visualization
tests, and those settles were previously exercised without ever being
checked. Turning them into assertions costs roughly two percent of the
suite's runtime.
"""

from __future__ import annotations

import pytest

from tests.scheduler_invariants import install


@pytest.fixture(autouse=True)
def scheduler_invariants(monkeypatch):
    """Validate scheduler structure on every state change, suite-wide."""
    install(monkeypatch)


@pytest.fixture(autouse=True)
def reset_global_rendering_state():
    """Return safe rendering and the colour scheme to their defaults.

    Both are process-wide and both are set by any test that launches the
    app with a config that names them, so a test that reads them was
    previously at the mercy of the (randomized) test order.
    """
    yield
    from disktide.rendering import set_safe_rendering
    from disktide.viz.colors import set_color_scheme

    set_safe_rendering(False)
    set_color_scheme("default")
