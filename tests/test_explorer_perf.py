"""Interaction-loop efficiency: what a cursor move, a tab switch, and a
quarter-screen jump are each allowed to cost.

Every rule here was measured on a headless 160x48 pilot: a tab switch cost
422 ms of which ~320 ms was Textual's animation, one chart recompute costs
~40 ms, and the Details panel's table rebuild sorts and formats a node's
children whether or not its tab is on screen.
"""

from __future__ import annotations

import asyncio
import os
from io import StringIO

from rich.console import Console
from textual import constants
from textual.app import App, ComposeResult
from textual.widgets import TabbedContent, Tree

from disktide.app import DiskTideApp
from disktide.config import load_config
from disktide.domain.metrics import MetricId
from disktide.domain.snapshot import Snapshot
from disktide.domain.visualization import DiffFrame, ExplorerSpaceTime
from disktide.models.tree import FSNode
from disktide.screens.explorer import ExplorerScreen
from disktide.widgets.info_panel import InfoPanel
from disktide.widgets.size_tree import SizeTree


async def _wait_for_explorer(pilot, app) -> None:
    await pilot.pause(delay=0.2)
    for _ in range(20):
        await pilot.pause(delay=0.1)
        if isinstance(app.screen, ExplorerScreen) and app.screen._root is not None:
            return


def _make_tree_dir(tmp_path) -> None:
    for name, size in (("alpha", 60_000), ("beta", 40_000)):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "blob.bin").write_bytes(b"x" * size)


def _make_flat_tree(tmp_path, count: int) -> None:
    for i in range(count):
        (tmp_path / f"file_{i:03d}.txt").write_text("x" * (count - i))


def _explorer_app(tmp_path, viz: str = "sunburst") -> DiskTideApp:
    config = load_config()
    config.ui.default_viz = viz
    return DiskTideApp(scan_path=str(tmp_path), show_welcome=False, config=config)


# --- animations ------------------------------------------------------------


def test_app_disables_animations_by_default(monkeypatch):
    monkeypatch.delenv("TEXTUAL_ANIMATIONS", raising=False)
    app = DiskTideApp(scan_path=".", show_welcome=False, config=load_config())
    assert app.animation_level == "none"


def test_a_preset_textual_animations_env_wins(monkeypatch):
    """TEXTUAL_ANIMATIONS is Textual's own knob; a user who set it keeps
    whatever Textual resolved from it."""
    monkeypatch.setenv("TEXTUAL_ANIMATIONS", "full")
    app = DiskTideApp(scan_path=".", show_welcome=False, config=load_config())
    assert app.animation_level == constants.TEXTUAL_ANIMATIONS
    assert "TEXTUAL_ANIMATIONS" in os.environ


# --- the Details tab is only rebuilt while it is visible -------------------


def test_cursor_moves_do_not_rebuild_a_hidden_details_panel(tmp_path):
    _make_tree_dir(tmp_path)

    async def go():
        app = _explorer_app(tmp_path, viz="sunburst")
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_explorer(pilot, app)
            screen = app.screen
            panel = screen.query_one("#info-panel", InfoPanel)
            calls: list[FSNode | None] = []
            panel.update_node = lambda node: calls.append(node)

            await pilot.press("down")
            await pilot.press("down")
            await pilot.pause(delay=0.3)

            assert calls == []
            assert screen._last_highlighted is not None

    asyncio.run(go())


def test_cursor_moves_rebuild_the_details_panel_while_it_is_visible(tmp_path):
    _make_tree_dir(tmp_path)

    async def go():
        app = _explorer_app(tmp_path, viz="details")
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_explorer(pilot, app)
            screen = app.screen
            panel = screen.query_one("#info-panel", InfoPanel)
            calls: list[FSNode | None] = []
            panel.update_node = lambda node: calls.append(node)

            await pilot.press("down")
            await pilot.pause(delay=0.3)

            assert calls
            assert calls[-1] is screen._last_highlighted

    asyncio.run(go())


def test_activating_details_shows_the_node_the_cursor_is_on(tmp_path):
    """Tab activation used to feed the panel the drilled root, silently
    ignoring wherever the cursor had moved to."""
    _make_tree_dir(tmp_path)

    async def go():
        app = _explorer_app(tmp_path, viz="sunburst")
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_explorer(pilot, app)
            screen = app.screen
            tree = screen.query_one("#size-tree", SizeTree)
            panel = screen.query_one("#info-panel", InfoPanel)

            await pilot.press("down")
            await pilot.pause(delay=0.3)
            moved_to = tree.selected_path
            assert moved_to != str(tmp_path)

            screen.query_one("#viz-tabs", TabbedContent).active = "tab-details"
            await pilot.pause(delay=0.3)

            assert panel._node is not None
            assert panel._node.path == moved_to

    asyncio.run(go())


def test_activating_details_before_any_highlight_falls_back(tmp_path):
    _make_tree_dir(tmp_path)

    async def go():
        app = _explorer_app(tmp_path, viz="sunburst")
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_explorer(pilot, app)
            screen = app.screen
            screen._last_highlighted = None
            panel = screen.query_one("#info-panel", InfoPanel)

            screen.query_one("#viz-tabs", TabbedContent).active = "tab-details"
            await pilot.pause(delay=0.3)

            assert panel._node is not None
            assert panel._node.path == str(tmp_path)

    asyncio.run(go())


# --- diff mode recomputes are debounced too --------------------------------


def test_diff_mode_cursor_move_defers_the_chart_recompute(tmp_path):
    """`set_diff` marks the view stale, so a per-move call is one ~40 ms
    recompute per keystroke."""
    _make_tree_dir(tmp_path)

    async def go():
        app = _explorer_app(tmp_path, viz="treemap")
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_explorer(pilot, app)
            screen = app.screen
            screen._diff_mode = True
            screen._cancel_cursor_settle()

            recomputes: list[FSNode] = []
            screen._update_active_viz = lambda node, **kw: recomputes.append(node)

            tree = screen.query_one("#size-tree", SizeTree)
            tree.cursor_line = 1
            await pilot.pause()

            # The move itself only arms the timer.
            assert recomputes == []
            assert screen._cursor_settle_timer is not None

            screen._cancel_cursor_settle()
            screen._on_cursor_settled()
            assert recomputes == [screen._current or screen._root]

    asyncio.run(go())


def test_the_settled_diff_recompute_skips_the_details_tab(tmp_path):
    """The panel already follows the cursor while Details is up; letting
    `_update_active_viz` run there would rebuild it from the drilled node."""
    _make_tree_dir(tmp_path)

    async def go():
        app = _explorer_app(tmp_path, viz="details")
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_explorer(pilot, app)
            screen = app.screen
            screen._diff_mode = True

            recomputes: list[FSNode] = []
            screen._update_active_viz = lambda node, **kw: recomputes.append(node)

            screen._on_cursor_settled()
            assert recomputes == []

    asyncio.run(go())


def _space_time_for(root: FSNode) -> ExplorerSpaceTime:
    """A minimal diff context: enough for the frame bookkeeping, no storage."""
    frame = DiffFrame(
        baseline=Snapshot(id=1, root_path=root.path),
        target=Snapshot(id=2, root_path=root.path),
        metric=MetricId.LOGICAL,
        current_root=root,
        visual_root=root,
        visuals={},
        weights={},
        selected_path=root.path,
    )
    return ExplorerSpaceTime(
        frame=frame,
        snapshots=(frame.baseline, frame.target),
        pair_index=0,
        # Pre-seeded so the handler does not reach for a trend worker.
        mini_trends={node.path: (1, 2) for node in _walk(root)},
    )


def _walk(node: FSNode):
    yield node
    for child in node.children:
        yield from _walk(child)


def test_diff_mode_keeps_the_selected_path_bookkeeping_per_move(tmp_path):
    """Only the recompute is deferred: stamping the frame's selected_path
    is cheap, and the settled handler reads it back."""
    _make_tree_dir(tmp_path)

    async def go():
        app = _explorer_app(tmp_path, viz="treemap")
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_explorer(pilot, app)
            screen = app.screen
            tree = screen.query_one("#size-tree", SizeTree)
            root = screen._root
            assert root is not None
            screen._space_time = _space_time_for(root)
            screen._diff_mode = True
            screen._update_active_viz = lambda node, **kw: None

            tree.cursor_line = 1
            await pilot.pause()

            assert screen._space_time.frame.selected_path == tree.selected_path
            assert tree.selected_path != root.path

    asyncio.run(go())


# --- the quarter-screen jump is one cursor move ----------------------------


def _highlight_counter(tree) -> list[int]:
    """Count the NodeHighlighted messages the tree posts."""
    seen = [0]
    original = tree.post_message

    def counting(message):
        if isinstance(message, Tree.NodeHighlighted):
            seen[0] += 1
        return original(message)

    tree.post_message = counting
    return seen


def test_quarter_jump_posts_one_highlight_per_press(tmp_path):
    _make_flat_tree(tmp_path, 200)

    async def go():
        app = _explorer_app(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_explorer(pilot, app)
            screen = app.screen
            tree = screen.query_one("#size-tree", SizeTree)
            quarter = max(1, tree.size.height // 4)
            assert quarter > 1

            start = tree.cursor_line
            seen = _highlight_counter(tree)

            await pilot.press("ctrl+d")
            await pilot.pause(delay=0.3)

            assert seen[0] == 1
            assert tree.cursor_line == start + quarter

    asyncio.run(go())


def test_quarter_jump_up_lands_a_quarter_back(tmp_path):
    _make_flat_tree(tmp_path, 200)

    async def go():
        app = _explorer_app(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_explorer(pilot, app)
            screen = app.screen
            tree = screen.query_one("#size-tree", SizeTree)
            quarter = max(1, tree.size.height // 4)

            await pilot.press("ctrl+d")
            await pilot.press("ctrl+d")
            await pilot.pause(delay=0.3)
            mid = tree.cursor_line
            assert mid == 2 * quarter

            seen = _highlight_counter(tree)
            await pilot.press("ctrl+u")
            await pilot.pause(delay=0.3)

            assert seen[0] == 1
            assert tree.cursor_line == mid - quarter

    asyncio.run(go())


def test_quarter_jump_clamps_at_both_ends(tmp_path):
    """Shorter-than-a-quarter trees, and repeated presses at an edge, must
    stay on a real line without posting a highlight for a move that
    didn't happen."""
    _make_flat_tree(tmp_path, 3)

    async def go():
        app = _explorer_app(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_explorer(pilot, app)
            screen = app.screen
            tree = screen.query_one("#size-tree", SizeTree)
            assert tree.last_line < max(1, tree.size.height // 4)

            await pilot.press("ctrl+d")
            await pilot.pause(delay=0.3)
            assert tree.cursor_line == tree.last_line

            seen = _highlight_counter(tree)
            await pilot.press("ctrl+d")
            await pilot.pause(delay=0.3)
            assert tree.cursor_line == tree.last_line
            assert seen[0] == 0

            await pilot.press("ctrl+u")
            await pilot.press("ctrl+u")
            await pilot.pause(delay=0.3)
            assert tree.cursor_line == 0

    asyncio.run(go())


# --- symlink classification is off the UI thread ---------------------------


class _PanelApp(App):
    """Bare host for the InfoPanel: the worker needs a running app."""

    def compose(self) -> ComposeResult:
        yield InfoPanel(id="info-panel")


def test_update_node_does_not_classify_a_symlink_synchronously(monkeypatch):
    """readlink + a following stat are two network round-trips on NFS; the
    render path may not pay for them."""
    calls = []
    monkeypatch.setattr(
        "disktide.widgets.info_panel.classify_symlink",
        lambda node: calls.append(node),
    )
    node = FSNode(
        name="link", path="/x/link", size=12, is_symlink=True,
        link_target="/raw/target",
    )
    panel = InfoPanel()
    panel._display = type(
        "Stub", (), {"update": lambda self, x: setattr(self, "last", x)}
    )()

    panel.update_node(node)

    assert calls == []
    buf = StringIO()
    Console(file=buf, width=200, color_system=None).print(panel._display.last)
    out = buf.getvalue()
    assert "Symlink" in out
    assert "/raw/target" in out          # the raw target it already had
    assert "Resolving" in out            # the target type is still pending


def test_the_worker_fills_in_the_symlink_rows(tmp_path):
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    os.symlink(target, link)
    node = FSNode(
        name="link", path=str(link), size=12, is_symlink=True,
    )

    async def go():
        app = _PanelApp()
        async with app.run_test(size=(80, 24)) as pilot:
            panel = app.query_one("#info-panel", InfoPanel)
            panel.update_node(node)
            for _ in range(40):
                await pilot.pause(delay=0.05)
                if node.link_classified:
                    break
            await pilot.pause(delay=0.1)

            assert node.link_classified is True
            assert node.link_is_dir is True
            assert node.link_target == str(target)

    asyncio.run(go())


def test_a_stale_classification_does_not_repaint_the_panel(tmp_path):
    """The cursor may have moved on while the stat was in flight."""
    node = FSNode(name="a", path="/x/a", size=1, is_symlink=True)
    other = FSNode(name="b", path="/x/b", size=1, is_dir=True)
    panel = InfoPanel()
    rendered = []
    panel._display = type(
        "Stub", (), {"update": lambda self, x: rendered.append(x)}
    )()

    panel.update_node(other)
    before = len(rendered)
    panel._apply_classification(node)

    assert len(rendered) == before


def test_a_classified_symlink_is_not_restated(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "disktide.widgets.info_panel.classify_symlink",
        lambda node: calls.append(node),
    )
    node = FSNode(
        name="link", path="/x/link", size=12, is_symlink=True,
        link_target="/real/dir", link_is_dir=True, link_classified=True,
    )
    panel = InfoPanel()
    panel._display = type(
        "Stub", (), {"update": lambda self, x: setattr(self, "last", x)}
    )()

    panel.update_node(node)

    assert calls == []
    assert panel._node is node
