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
