"""Regressions for the chart widgets' hover text and diff-mode formatting.

Two separate ways the widgets could disagree with the rest of the screen
about a number: a hover tooltip reading its percentage off the geometry
(rings are normalised by each level's children, so a lone child fills the
disc), and a metric toggle in diff mode formatting the still-attached
byte frame with the new metric's formatter.
"""

from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult

from disktide.domain.snapshot import Snapshot
from disktide.domain.visualization import DiffFrame, VisualDelta, VisualState
from disktide.metrics import MetricId
from disktide.models.tree import FSNode
from disktide.widgets.size_tree import SizeTree
from disktide.widgets.sunburst_view import SunburstView
from disktide.widgets.treemap_view import TreemapView

MIB = 2**20


class _ChartsApp(App):
    def compose(self) -> ComposeResult:
        yield SunburstView(id="sunburst")
        yield TreemapView(id="treemap")


def _allocated_tree() -> FSNode:
    """A directory whose own blocks are half of its allocated total."""
    child = FSNode(
        name="f", path="/r/f", size=1, own_size=1, file_count=1,
        allocated_size=4096, own_allocated_size=4096, depth=1,
    )
    return FSNode(
        name="r", path="/r", is_dir=True, size=1, file_count=1, depth=0,
        allocated_size=8192, own_allocated_size=4096, children=[child],
    )


def _depth1_arc(view: SunburstView):
    return next(arc for arc in view._layout.arcs if arc.depth == 1)


def test_sunburst_tooltip_reports_metric_share_not_arc_span():
    async def go() -> None:
        app = _ChartsApp()
        async with app.run_test(size=(80, 48)) as pilot:
            view = app.query_one("#sunburst", SunburstView)
            view.set_metric("allocated")
            view.set_node(_allocated_tree())
            await pilot.pause()
            arc = _depth1_arc(view)
            # The ring is a full circle (the only child), but the file is
            # half of the directory's allocated bytes, which is what the
            # size tree and the info panel both show.
            assert round(arc.angle_span, 6) == round(2 * 3.141592653589793, 6)
            assert view._arc_tooltip(arc).endswith(" · 50%")

    asyncio.run(go())


def test_sunburst_tooltip_share_follows_diff_weights():
    async def go() -> None:
        app = _ChartsApp()
        async with app.run_test(size=(80, 48)) as pilot:
            view = app.query_one("#sunburst", SunburstView)
            root = _allocated_tree()
            child = root.children[0]
            frame = DiffFrame(
                baseline=Snapshot(id=1, root_path=root.path),
                target=Snapshot(id=2, root_path=root.path),
                metric=MetricId.ALLOCATED,
                current_root=root,
                visual_root=root,
                visuals={},
                # A removed/floored child keeps a sliver of the ring; the
                # tooltip must quote the weight share, not the full circle.
                weights={root.path: 400, child.path: 100},
            )
            view.set_diff(frame)
            await pilot.pause()
            assert view._arc_tooltip(_depth1_arc(view)).endswith(" · 25%")

    asyncio.run(go())


def _byte_frame() -> DiffFrame:
    child = FSNode(name="c", path="/r/c", size=MIB, file_count=1, depth=1)
    root = FSNode(
        name="r", path="/r", is_dir=True, size=MIB, file_count=1, depth=0,
        children=[child],
    )
    visuals = {
        "/r": VisualDelta("/r", VisualState.GROWTH, 7 * MIB, 8 * MIB, MIB, 14.3, True),
        "/r/c": VisualDelta(
            "/r/c", VisualState.GROWTH, 7 * MIB, 8 * MIB, MIB, 14.3, False
        ),
    }
    return DiffFrame(
        baseline=Snapshot(id=1, root_path="/r"),
        target=Snapshot(id=2, root_path="/r"),
        metric=MetricId.LOGICAL,
        current_root=root,
        visual_root=root,
        visuals=visuals,
        weights={"/r": 8 * MIB, "/r/c": 8 * MIB},
        selected_path="/r",
    )


def test_metric_toggle_keeps_the_attached_frame_in_its_own_units():
    async def go() -> None:
        frame = _byte_frame()
        app = _ChartsApp()
        async with app.run_test(size=(80, 48)) as pilot:
            sunburst = app.query_one("#sunburst", SunburstView)
            treemap = app.query_one("#treemap", TreemapView)
            sunburst.set_diff(frame)
            treemap.set_diff(frame)
            await pilot.pause()
            # The toggle runs ahead of the worker that rebuilds the frame,
            # so the byte deltas must not be reprinted as file counts.
            sunburst.set_metric("files")
            treemap.set_metric("files")
            await pilot.pause()
            assert sunburst.diff_mode and treemap.diff_mode
            assert any(
                "+1.0 MiB" in label.text for label in sunburst._layout.labels
            )
            assert any(
                rect.size_label and "+1.0 MiB" in rect.size_label
                for rect in treemap._layout.rects
            )
            # Leaving diff mode applies the metric the user asked for.
            sunburst.set_node(frame.visual_root)
            await pilot.pause()
            assert not sunburst.diff_mode
            assert sunburst._metric == "files"

    asyncio.run(go())


class _TreeApp(App):
    def compose(self) -> ComposeResult:
        yield SizeTree(id="size-tree")


def test_size_tree_metric_toggle_keeps_diff_labels_in_frame_units():
    async def go() -> None:
        frame = _byte_frame()
        app = _TreeApp()
        async with app.run_test(size=(80, 24)) as pilot:
            tree = app.query_one("#size-tree", SizeTree)
            tree.reload(frame.visual_root)
            tree.set_visual_context(dict(frame.visuals), diff_mode=True)
            await pilot.pause()
            tree.metric = "files"
            await pilot.pause()
            label = str(tree._tree_nodes["/r/c"].label)
            assert "+1.0 MiB" in label

    asyncio.run(go())
