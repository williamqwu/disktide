"""A failing mini-trend lookup must not take the explorer down with it.

The trend column beside each tree row is filled by a background worker
that queries the database once per newly highlighted path.  That query can
fail for reasons that have nothing to do with the session -- another
process holding the write lock past the busy timeout, an I/O error on the
volume the database lives on -- and Textual's default for a `@work` worker
is to exit the app when its body raises.  Moving the cursor onto one more
row would then end the scan the user was reading.

The column is decoration; the tree underneath it is the session.  A lookup
that fails leaves the sparkline blank, and the path is remembered as
having no trend so the next cursor visit does not re-fire the same query.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass, field

import pytest

from disktide.app import DiskTideApp
from disktide.config import load_config
from disktide.widgets.size_tree import SizeTree
from tests.waiting import wait_for_explorer


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Never read or write the developer's real config or database."""
    for var, leaf in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_STATE_HOME", "state"),
    ):
        monkeypatch.setenv(var, str(tmp_path / "xdg" / leaf))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


@dataclass(frozen=True)
class _SpaceTimeStub:
    """Enough of `ExplorerSpaceTime` for the trend cache: a `replace`-able record."""

    mini_trends: dict = field(default_factory=dict)


class _BrokenVisualization:
    """A visualization service whose database has gone away mid-session."""

    def __init__(self) -> None:
        self.calls = 0

    def path_trend(self, path, snapshot_ids, metric):
        self.calls += 1
        raise sqlite3.OperationalError("disk I/O error")


def _tree_dir(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    (root / "blob.bin").write_bytes(b"x" * 4096)
    sub = root / "sub"
    sub.mkdir()
    (sub / "note.txt").write_text("hi")
    return root


def test_failing_path_trend_leaves_the_app_running(tmp_path):
    """A raising `path_trend` blanks the column instead of exiting the app."""
    root = _tree_dir(tmp_path)

    async def go():
        app = DiskTideApp(
            scan_path=str(root), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await wait_for_explorer(pilot, app)
            broken = _BrokenVisualization()
            screen._visualization_service = broken

            screen._load_selected_trend(str(root), (1, 2), "logical")
            await pilot.pause(0.5)
            await pilot.pause()

            assert broken.calls == 1
            assert app.is_running
            assert app.return_code is None

            tree = screen.query_one("#size-tree", SizeTree)
            assert not tree._mini_trends.get(str(root))

    asyncio.run(go())


def test_failed_lookup_is_not_retried_on_the_next_visit(tmp_path):
    """The path is remembered as trendless so the cursor cannot re-fire it."""
    root = _tree_dir(tmp_path)

    async def go():
        app = DiskTideApp(
            scan_path=str(root), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await wait_for_explorer(pilot, app)
            screen._visualization_service = _BrokenVisualization()
            screen._space_time = _SpaceTimeStub()
            screen._load_selected_trend(str(root), (1, 2), "logical")
            await pilot.pause(0.5)
            await pilot.pause()

            tree = screen.query_one("#size-tree", SizeTree)
            assert str(root) in tree._mini_trends
            assert tree._mini_trends[str(root)] == ()

    asyncio.run(go())
