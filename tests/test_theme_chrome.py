"""A theme has to reach every cell of the window, and reach it live.

Two failures motivate this file, and both were invisible under the charcoal
default that shipped for a year.

**Cells with no background.** Textual paints a widget's background inside
`StylesCache.render_line`, and only for the parts of a line it generates
itself. The four chart widgets override `render_line`, so their `Strip` is
composited verbatim and every segment they built without a bgcolor reached
the terminal as SGR 49 -- the *emulator's* default background. On a charcoal
theme against a charcoal terminal that is invisible; on the navy, violet and
black themes the right-hand two thirds of the explorer was a different
colour from the left. `OpaqueStripMixin` closes it, and the sweep below is
what keeps it closed: every screen, every tab, every theme, zero cells
without a background.

**Text that does not follow the theme.** Textual's `Tree` stores each label
as a `Text` built once and handed over with `set_label`, so refreshing the
widget redraws the colours it was already drawing. Picking a theme moved the
chart and left the tree in the old one until the next launch.

Deadline polls throughout, never a fixed sleep: CI runners are two-core.
"""

from __future__ import annotations

import asyncio

import pytest
from rich.console import Console
from textual.widgets import TabbedContent

from disktide.app import DiskTideApp
from disktide.config import AppConfig
from disktide.screens.explorer import ExplorerScreen
from disktide.viz.colors import SCHEMES
from disktide.widgets.breadcrumb import Breadcrumb
from disktide.widgets.size_tree import SizeTree
from disktide.widgets.sunburst_view import SunburstView

_POLL_TRIES = 60
_POLL_DELAY = 0.1


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Never read or write the developer's real config."""
    for var, leaf in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_STATE_HOME", "state"),
    ):
        monkeypatch.setenv(var, str(tmp_path / "xdg" / leaf))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def _scan_dir(tmp_path):
    """A tree with several categories, so the charts have something to draw."""
    root = tmp_path / "project"
    plan = {
        "src": [("app.py", 90_000)],
        "docs": [("manual.md", 55_000)],
        "data": [("train.parquet", 220_000)],
        "media": [("hero.png", 140_000)],
        "build": [("core.o", 130_000), ("run.log", 70_000)],
    }
    for name, files in plan.items():
        folder = root / name
        folder.mkdir(parents=True)
        for filename, size in files:
            (folder / filename).write_bytes(b"x" * size)
    return root


def _cells_without_a_background(app) -> int:
    """How many cells the compositor emits with no bgcolor at all.

    Reads the compositor's own update through a recording Console and walks
    the `Segment`s rather than parsing escape codes: a segment whose Style
    has `bgcolor=None` is exactly the leak, and Rich will emit it as SGR 49.
    """
    update = app.screen._compositor.render_update(full=True)
    console = Console(
        width=app.size.width, record=True, force_terminal=True,
        color_system="truecolor", legacy_windows=False,
    )
    console.print(update)
    return sum(
        len(segment.text)
        for segment in console._record_buffer
        if not segment.control and segment.text and "\n" not in segment.text
        and (segment.style is None or segment.style.bgcolor is None)
    )


async def _settled_explorer(pilot, app):
    """Wait until the explorer holds a scan *and* a built sunburst layout."""
    await pilot.pause(delay=0.2)
    for _ in range(_POLL_TRIES):
        await pilot.pause(delay=_POLL_DELAY)
        screen = app.screen
        if not isinstance(screen, ExplorerScreen) or screen._root is None:
            continue
        view = screen.query_one("#sunburst-view", SunburstView)
        if view._category_index is not None and view._layout is not None:
            return screen, view
    raise AssertionError("the explorer never painted a sunburst layout")


async def _await_screen(pilot, app, name: str):
    for _ in range(_POLL_TRIES):
        await pilot.pause(delay=0.05)
        if type(app.screen).__name__ == name:
            await pilot.pause()
            return app.screen
    raise AssertionError(
        f"never reached {name}; still on {type(app.screen).__name__}"
    )


async def _settled_paint(pilot):
    """Let a screen switch finish laying out before the compositor is read."""
    await pilot.pause()
    await pilot.pause(delay=0.3)
    await pilot.pause()


def _crumb_styles(screen) -> list[str]:
    """The breadcrumb's styles. `str(Text)` is only the words."""
    crumb = screen.query_one("#breadcrumb", Breadcrumb).render()
    return [str(span.style) for span in crumb.spans]


def _tree_label_styles(screen) -> list[str]:
    """Every style the tree's materialized labels carry, as strings."""
    tree = screen.query_one("#size-tree", SizeTree)
    styles: list[str] = []
    stack = [tree.root]
    while stack:
        node = stack.pop()
        label = node.label
        styles.extend(str(span.style) for span in label.spans)
        stack.extend(node.children)
    return styles


@pytest.mark.parametrize("theme", list(SCHEMES))
def test_every_cell_carries_a_background(tmp_path, theme):
    """No cell anywhere may fall through to the terminal's own colour.

    Driven the way a user drives it -- all three explorer tabs, then
    Settings, Monitor, FS-Overview and Cleanup -- because the leak lives in
    whichever widget happens to own the blank space, and that is a different
    widget on every screen.
    """
    root = _scan_dir(tmp_path)

    async def go():
        config = AppConfig()
        config.ui.color_theme = theme
        config.ui.show_cleanup = True
        app = DiskTideApp(
            scan_path=str(root), show_welcome=False, config=config
        )
        async with app.run_test(size=(120, 40)) as pilot:
            explorer, _view = await _settled_explorer(pilot, app)
            tabs = explorer.query_one("#viz-tabs", TabbedContent)
            for tab in ("tab-sunburst", "tab-treemap", "tab-details"):
                tabs.active = tab
                await _settled_paint(pilot)
                leaked = _cells_without_a_background(app)
                assert leaked == 0, f"{theme} explorer/{tab}: {leaked} cells"

            await pilot.press("question_mark")
            await _await_screen(pilot, app, "SettingsScreen")
            await _settled_paint(pilot)
            assert _cells_without_a_background(app) == 0, f"{theme} settings"
            await pilot.press("escape")
            await _await_screen(pilot, app, "ExplorerScreen")

            for key, name in (
                ("2", "MonitorScreen"),
                ("3", "FSOverviewScreen"),
                ("c", "CleanupScreen"),
            ):
                await pilot.press(key)
                await _await_screen(pilot, app, name)
                await _settled_paint(pilot)
                leaked = _cells_without_a_background(app)
                assert leaked == 0, f"{theme} {name}: {leaked} cells"
                await pilot.press("1")
                await _await_screen(pilot, app, "ExplorerScreen")

    asyncio.run(go())


def test_switching_theme_recolours_the_tree_without_a_restart(tmp_path):
    """The tree is the one widget a refresh alone cannot re-theme.

    Its labels are built once and stored on the nodes, so before
    `rebuild_render_cache` a theme change repainted them in the old colours
    perfectly -- the chart went navy and the file names stayed cyan.
    """
    root = _scan_dir(tmp_path)

    async def go():
        config = AppConfig()
        config.ui.color_theme = "disktide"
        app = DiskTideApp(
            scan_path=str(root), show_welcome=False, config=config
        )
        async with app.run_test(size=(120, 40)) as pilot:
            explorer, _view = await _settled_explorer(pilot, app)
            before = _tree_label_styles(explorer)
            assert before, "the tree rendered no styled labels to compare"
            assert any("cyan" in style for style in before), (
                f"disktide should still print cyan directories: {before[:4]}"
            )
            crumb_before = _crumb_styles(explorer)

            await pilot.press("question_mark")
            settings = await _await_screen(pilot, app, "SettingsScreen")
            settings.query_one("#color-theme").value = "mono"
            await pilot.pause()
            await pilot.pause()
            await pilot.press("escape")
            await _await_screen(pilot, app, "ExplorerScreen")
            await _settled_paint(pilot)

            after = _tree_label_styles(explorer)
            assert after != before, (
                "the tree kept its labels after a theme change; it is still "
                "drawing the Text objects it built under the old scheme"
            )
            assert not any("cyan" in style for style in after), (
                f"mono is still printing cyan: "
                f"{[s for s in after if 'cyan' in s][:4]}"
            )
            crumb_after = _crumb_styles(explorer)
            assert crumb_after != crumb_before, (
                "the breadcrumb did not follow the theme change"
            )

    asyncio.run(go())


def test_mono_prints_no_colour_anywhere_in_the_tree(tmp_path):
    """Rendered proof, not just a table check.

    `test_palette_gates` pins the ink table; this drives the real widgets
    and looks at what actually came out, which is the only thing that
    catches a call site still holding a literal.
    """
    root = _scan_dir(tmp_path)
    coloured = ("cyan", "green", "yellow", "red", "magenta", "blue", "white")

    async def go():
        config = AppConfig()
        config.ui.color_theme = "mono"
        app = DiskTideApp(
            scan_path=str(root), show_welcome=False, config=config
        )
        async with app.run_test(size=(120, 40)) as pilot:
            explorer, _view = await _settled_explorer(pilot, app)
            styles = _tree_label_styles(explorer)
            styles.extend(_crumb_styles(explorer))
            for style in styles:
                for name in coloured:
                    assert name not in style, f"mono printed {name!r}: {style}"

    asyncio.run(go())
