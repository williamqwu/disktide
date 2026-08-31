"""Switching to the treemap during a scan, and what happened when it didn't.

A chart laid out during a scan is handed a `LiveViewNode` — the bounded
frozen view model the scheduler ships — not the scan's own `FSNode` tree.
Everything in `viz/` reads both shapes, except the one function that
*builds* a node: the "… N more" stand-in a fold puts in place of siblings
it cannot draw separately.  That built an `FSNode` unconditionally and
summed four subtree counters a `LiveViewNode` does not carry, so it raised
`AttributeError` from inside `render_content_line`.

The treemap folds on nearly every live frame — three children with any size
skew is enough — so `F2` during a scan was a guaranteed crash, while the
sunburst, which folds only at narrow widths or high fan-out, survived.
That asymmetry is the reported bug: sunburst → treemap, mid-scan, dead app.
"""

from __future__ import annotations

import asyncio

import pytest
from click.testing import CliRunner

from disktide.domain.live_view import LiveViewNode, build_live_view
from disktide.domain.metrics import MetricId
from disktide.domain.scan import NodeAggregateUpdated, ScanPhase
from disktide.models.tree import FSNode
from disktide.viz.layout import (
    aggregate_children,
    bounded_children,
    is_aggregate_path,
)
from disktide.viz.sunburst import compute_sunburst, render_sunburst_line
from disktide.viz.treemap import compute_layout, render_line


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Never read or write the developer's real config.

    The cell-aspect keys pressed below persist their value, so a test that
    nudged one without this would rewrite `~/.config/disktide/config.toml`
    with whatever aspect it happened to land on -- and every app test here
    would otherwise take its live-render and default-viz settings from
    whatever the developer happens to have configured.
    """
    for var, leaf in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_STATE_HOME", "state"),
    ):
        monkeypatch.setenv(var, str(tmp_path / "xdg" / leaf))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


# --- the node the fold builds ---------------------------------------------


def _fanned_tree(count: int = 40) -> FSNode:
    """One directory with more children than any cap will keep."""
    children = [
        FSNode(
            name=f"c{index:02d}",
            path=f"/r/c{index:02d}",
            size=(count - index) * 1000,
            allocated_size=(count - index) * 1024,
            unique_allocated_size=(count - index) * 512,
            file_count=index + 1,
            is_dir=True,
            depth=1,
            inaccessible_count=index % 2,
            inaccessible_subtree_count=index % 2,
            denied_dir_subtree_count=index % 3,
            partial_dir_subtree_count=index % 4,
            excluded_subtree_count=index % 5,
            depth_limited_subtree_count=index % 6,
        )
        for index in range(count)
    ]
    return FSNode(
        name="r",
        path="/r",
        is_dir=True,
        depth=0,
        children=children,
        size=sum(child.size for child in children),
        allocated_size=sum(child.allocated_size or 0 for child in children),
        unique_allocated_size=sum(
            child.unique_allocated_size or 0 for child in children
        ),
        file_count=sum(child.file_count for child in children),
        dir_count=count,
    )


def _value(node) -> int:
    return node.size


def test_a_live_parent_folds_into_a_live_node():
    """The regression: this raised AttributeError on `LiveViewNode`."""
    view = build_live_view(_fanned_tree(), metric=MetricId.LOGICAL)

    folded = bounded_children(
        view, metric="logical", value=_value, limit=8
    )

    assert all(isinstance(child, LiveViewNode) for child in folded)
    synthetic = [child for child in folded if is_aggregate_path(child.path)]
    assert len(synthetic) == 1
    assert synthetic[0].synthetic is True


def test_a_real_parent_still_folds_into_a_real_node():
    root = _fanned_tree()

    folded = bounded_children(
        root, metric="logical", value=_value, limit=8
    )

    assert all(isinstance(child, FSNode) for child in folded)
    aggregate = next(child for child in folded if is_aggregate_path(child.path))
    # The counters only an FSNode carries are still summed for one.
    omitted = [child for child in root.children if child not in folded]
    assert aggregate.denied_dir_subtree_count == sum(
        child.denied_dir_subtree_count for child in omitted
    )
    assert aggregate.depth_limited_subtree_count == sum(
        child.depth_limited_subtree_count for child in omitted
    )


def test_both_shapes_fold_to_the_same_measurements():
    """A live chart must not report different totals from a finished one."""
    root = _fanned_tree()
    view = build_live_view(root, metric=MetricId.LOGICAL)

    real = bounded_children(root, metric="logical", value=_value, limit=8)
    live = bounded_children(view, metric="logical", value=_value, limit=8)

    real_aggregate = next(c for c in real if is_aggregate_path(c.path))
    live_aggregate = next(c for c in live if is_aggregate_path(c.path))
    assert live_aggregate.path == real_aggregate.path
    assert live_aggregate.name == real_aggregate.name
    assert live_aggregate.measurements == real_aggregate.measurements
    assert live_aggregate.inaccessible_subtree_count == (
        real_aggregate.inaccessible_subtree_count
    )


def test_the_fold_is_only_as_settled_as_its_least_settled_member():
    root = _fanned_tree(count=4)
    view = build_live_view(
        root,
        metric=MetricId.LOGICAL,
        stable_paths=frozenset({"/r/c00", "/r/c01", "/r/c02"}),
    )

    settled = aggregate_children(
        view, view.children[:3], metric="logical", layout_value=10
    )
    unsettled = aggregate_children(
        view, view.children, metric="logical", layout_value=10
    )

    assert settled.stable is True
    assert unsettled.stable is False


def test_a_geometry_fold_keys_its_synthetic_path_apart():
    """Several folds under one parent may not collide on one path."""
    view = build_live_view(_fanned_tree(count=6), metric=MetricId.LOGICAL)

    crumbs = aggregate_children(
        view, view.children[:2], metric="logical", layout_value=1, key="crumbs"
    )
    run = aggregate_children(
        view, view.children[2:], metric="logical", layout_value=1, key="3"
    )

    assert crumbs.path != run.path
    assert crumbs.path.endswith("-crumbs")
    assert all(is_aggregate_path(node.path) for node in (crumbs, run))


def test_a_live_node_knows_its_own_depth():
    """The synthetic path is spelled from it, so both shapes must have one."""
    view = build_live_view(_fanned_tree(count=3), metric=MetricId.LOGICAL)

    assert view.depth == 0
    assert all(child.depth == 1 for child in view.children)


# --- the layout the fold feeds --------------------------------------------


def _skewed_tree() -> FSNode:
    """One whale and three crumbs: what a live scan looks like early on.

    Below the treemap's one-cell threshold the crumbs are folded by area
    rather than by count, which is the path the reported crash took.
    """
    whale = FSNode(
        name="whale", path="/r/whale", size=400_000, allocated_size=400_000,
        unique_allocated_size=400_000, file_count=1, is_dir=True, depth=1,
    )
    crumbs = [
        FSNode(
            name=f"crumb{index}", path=f"/r/crumb{index}", size=1,
            allocated_size=1, unique_allocated_size=1, file_count=1,
            is_dir=True, depth=1,
        )
        for index in range(3)
    ]
    children = [whale, *crumbs]
    return FSNode(
        name="r", path="/r", is_dir=True, depth=0, children=children,
        size=sum(child.size for child in children),
        allocated_size=sum(child.allocated_size or 0 for child in children),
        unique_allocated_size=sum(
            child.unique_allocated_size or 0 for child in children
        ),
        file_count=4, dir_count=4,
    )


_LIVE_TREES = {
    "skewed": _skewed_tree,
    "fanned": lambda: _fanned_tree(90),
}
# Small viewports are where both charts fold hardest: the treemap's
# per-rect cap falls with the area and the sunburst's with the width.
_SIZES = ((120, 40), (60, 24), (40, 16), (24, 10))


@pytest.mark.parametrize("tree", sorted(_LIVE_TREES))
@pytest.mark.parametrize(("width", "height"), _SIZES)
@pytest.mark.parametrize("metric", [m.value for m in MetricId])
def test_the_treemap_lays_out_and_paints_a_live_view(tree, width, height, metric):
    view = build_live_view(_LIVE_TREES[tree](), metric=MetricId.LOGICAL)

    layout = compute_layout(view, width, height, max_depth=2, metric=metric)

    assert all(isinstance(rect.node, LiveViewNode) for rect in layout.rects)
    for y in range(height):
        render_line(layout, y)


@pytest.mark.parametrize("tree", sorted(_LIVE_TREES))
@pytest.mark.parametrize(("width", "height"), _SIZES)
@pytest.mark.parametrize("metric", [m.value for m in MetricId])
def test_the_sunburst_lays_out_and_paints_a_live_view(tree, width, height, metric):
    view = build_live_view(_LIVE_TREES[tree](), metric=MetricId.LOGICAL)

    layout = compute_sunburst(view, width, height, max_depth=2, metric=metric)

    assert all(isinstance(arc.node, LiveViewNode) for arc in layout.arcs)
    for y in range(height):
        render_sunburst_line(layout, y)


def test_the_treemap_actually_folds_the_crumbs_it_cannot_draw():
    """Guards the premise: without a fold the tests above prove nothing."""
    view = build_live_view(_skewed_tree(), metric=MetricId.LOGICAL)

    layout = compute_layout(view, 120, 40, max_depth=2, metric="logical")

    folded = [
        rect for rect in layout.rects if is_aggregate_path(rect.node.path)
    ]
    assert folded, [rect.node.path for rect in layout.rects]
    assert folded[0].node.name == "… 3 more"


# --- the reported repro, through the app ----------------------------------


def _live_files(tmp_path) -> None:
    (tmp_path / "whale").mkdir()
    (tmp_path / "whale" / "blob.bin").write_bytes(b"x" * 400_000)
    for index in range(3):
        crumb = tmp_path / f"crumb{index}"
        crumb.mkdir()
        (crumb / "speck").write_bytes(b"x")


def _live_event(root: FSNode, view_root: LiveViewNode) -> NodeAggregateUpdated:
    return NodeAggregateUpdated(
        run_id="run",
        sequence=1,
        phase=ScanPhase.SCANNING,
        root=root,
        final=False,
        changed_nodes=(root,),
        view_root=view_root,
    )


async def _explorer_mid_scan(pilot, app):
    """Drive the screen into the state a live frame leaves it in."""
    from disktide.screens.explorer import ExplorerScreen

    for _ in range(60):
        await pilot.pause(delay=0.05)
        if isinstance(app.screen, ExplorerScreen) and app.screen._root is not None:
            break
    screen = app.screen
    root = screen._root
    assert root is not None, "the setup scan never finished"
    screen._scan_in_progress = True
    screen._active_metric = MetricId.LOGICAL
    screen._apply_tree_snapshot(
        _live_event(root, build_live_view(root, metric=MetricId.LOGICAL))
    )
    return screen


def _run_mid_scan(tmp_path, body, *, default_viz="sunburst"):
    from disktide.app import DiskTideApp
    from disktide.config import load_config

    _live_files(tmp_path)

    async def go():
        config = load_config()
        config.ui.default_viz = default_viz
        app = DiskTideApp(
            scan_path=str(tmp_path), show_welcome=False, config=config
        )
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _explorer_mid_scan(pilot, app)
            try:
                await body(pilot, app, screen)
            finally:
                screen._scan_in_progress = False

    asyncio.run(go())


def test_switching_sunburst_to_treemap_mid_scan_keeps_the_app_up(tmp_path):
    """The reported bug: `F2` during a scan froze the TUI and then exited."""
    from disktide.widgets.treemap_view import TreemapView

    async def body(pilot, app, screen):
        assert screen.query_one("#viz-tabs").active == "tab-sunburst"

        await pilot.press("f2")
        await pilot.pause()
        await pilot.pause()

        assert app.is_running
        assert screen.query_one("#viz-tabs").active == "tab-treemap"
        treemap = screen.query_one("#treemap-view", TreemapView)
        # Painted from the live view, not left blank by a swallowed error.
        for y in range(treemap.size.height):
            treemap.render_content_line(y)
        assert treemap._layout is not None and treemap._layout.rects

    _run_mid_scan(tmp_path, body)


def test_switching_back_and_forth_mid_scan_keeps_the_app_up(tmp_path):
    async def body(pilot, app, screen):
        for key in ("f2", "f3", "f1", "f2", "f1", "f3", "f2"):
            await pilot.press(key)
            await pilot.pause()
        assert app.is_running

    _run_mid_scan(tmp_path, body)


# Every one of these re-lays out the visible chart, so with the treemap in
# front each was the same crash under a different key.
@pytest.mark.parametrize(
    ("name", "keys"),
    [
        ("metric toggle", ("t",)),
        ("cell aspect", (",", ".")),
        ("sort cycle", ("s",)),
        ("diff toggle", ("d",)),
    ],
)
def test_mid_scan_keys_do_not_take_the_treemap_down(tmp_path, name, keys):
    async def body(pilot, app, screen):
        for key in keys:
            await pilot.press(key)
            await pilot.pause()
        assert app.is_running, name

    _run_mid_scan(tmp_path, body, default_viz="treemap")


def test_resizing_mid_scan_does_not_take_the_treemap_down(tmp_path):
    async def body(pilot, app, screen):
        for size in ((60, 24), (30, 12), (120, 40)):
            await pilot.resize_terminal(*size)
            await pilot.pause()
        assert app.is_running

    _run_mid_scan(tmp_path, body, default_viz="treemap")


def test_hovering_the_live_treemap_reads_a_tooltip_off_a_live_node(tmp_path):
    """The hover path formats measurements off whatever node it hit."""
    from disktide.widgets.treemap_view import TreemapView

    async def body(pilot, app, screen):
        treemap = screen.query_one("#treemap-view", TreemapView)
        for y in range(treemap.size.height):
            treemap.render_content_line(y)
        seen = 0
        for y in range(0, treemap.size.height, 3):
            for x in range(0, treemap.size.width, 7):
                rect = treemap._rect_at(x, y)
                if rect is not None:
                    treemap._rect_tooltip(rect)
                    seen += 1
        assert seen

    _run_mid_scan(tmp_path, body, default_viz="treemap")


def test_a_stale_space_time_context_does_not_clobber_the_live_tree(tmp_path):
    """Press `i` the moment a scan finishes and the old context lands late.

    Completion asks the visualization service for a diff context; the answer
    comes back on a worker. If the user has started the next scan by then,
    applying it reloads the size tree from the *finished* root, on top of a
    live stream that goes on patching it with the new scan's partial nodes.
    """
    from disktide.app import DiskTideApp
    from disktide.config import load_config
    from disktide.screens.explorer import ExplorerScreen
    from disktide.widgets.size_tree import SizeTree

    _live_files(tmp_path)

    async def go():
        config = load_config()
        app = DiskTideApp(
            scan_path=str(tmp_path), show_welcome=False, config=config
        )
        async with app.run_test(size=(120, 40)) as pilot:
            for _ in range(60):
                await pilot.pause(delay=0.05)
                if (
                    isinstance(app.screen, ExplorerScreen)
                    and app.screen._root is not None
                    and not app.screen._scan_in_progress
                ):
                    break
            screen = app.screen
            finished = screen._root
            assert finished.children

            screen._start_scan(force=True)
            tree = screen.query_one("#size-tree", SizeTree)
            assert screen._scan_in_progress
            assert tree._fs_root is not finished

            screen._apply_space_time_context(None, "no comparable snapshots")

            assert tree._fs_root is not finished
            assert screen._space_time is None
            screen.cancel_active_scan()

    asyncio.run(go())


# --- what turned the crash into a hang ------------------------------------


def test_a_tui_that_raises_still_cancels_its_scan(monkeypatch):
    """`app.run()` re-raises, and everything after it used to be skipped.

    An uncancelled scan outlives the app, and Textual's thread workers sit
    in the default executor the interpreter joins on the way out — so the
    terminal stayed dead for the rest of the walk before the traceback
    even printed.
    """
    import disktide.__main__ as cli_module
    import disktide.app as app_module

    torn_down: list[object] = []

    class _Boom:
        """Always raises: the success path below would `os._exit(0)`."""

        def __init__(self, **kwargs):
            pass

        def run(self, **kwargs):
            raise RuntimeError("the TUI died mid-scan")

    monkeypatch.setattr(cli_module, "_stdio_is_interactive", lambda: True)
    monkeypatch.setattr(
        cli_module, "_probe_terminal_if_unmeasured", lambda config: None
    )
    monkeypatch.setattr(cli_module, "_force_teardown", torn_down.append)
    monkeypatch.setattr(app_module, "DiskTideApp", _Boom)

    result = CliRunner().invoke(cli_module.cli, [])

    assert isinstance(result.exception, RuntimeError)
    assert len(torn_down) == 1


def test_force_teardown_cancels_an_in_flight_scan():
    from disktide.__main__ import _force_teardown

    cancelled: list[str] = []

    class _Service:
        def cancel_all(self):
            cancelled.append("scan")

    class _Monitor:
        def shutdown(self, wait=True):
            cancelled.append("monitor")

    class _Repository:
        def close(self):
            cancelled.append("db")

    class _App:
        _scan_service = _Service()
        _monitor_service = _Monitor()
        _snapshot_repository = _Repository()

    _force_teardown(_App())

    assert cancelled == ["monitor", "scan", "db"]
