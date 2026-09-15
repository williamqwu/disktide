"""A glyph is not always one terminal cell, and rows are laid out in cells.

`visible_width` counted every character except VS-15 as one cell. The diff
views mark a new path with a fullwidth `＋`, which a terminal draws two cells
wide, and so is every character of a Chinese or Japanese file name. Each row
the app put together itself came out a cell too long per wide glyph: the
Monitor summary's keys row wrapped at 150 columns and pushed `Trend marks` out
of its four-row box, a new directory's label pushed the rest of its sunburst
row right and cost the row its last cell, and a CJK name overflowed the
treemap by as many cells as it had characters.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from rich.cells import cell_len
from textual.app import App, ComposeResult
from textual.widget import Widget

from disktide.domain.visualization import (
    GrowthHeatmapModel,
    HeatmapCell,
    HeatmapInterval,
    HeatmapRow,
    VisualDelta,
    VisualState,
)
from disktide.glyphs import PARTIAL, VS15, visible_width
from disktide.models.patterns import CleanupRule, CleanupTarget
from disktide.models.tree import FSNode
from disktide.pathdisplay import elide_path, elide_text
from disktide.screens.monitor import MonitorScreen
from disktide.visualization_formatting import legend_text
from disktide.viz.sunburst import compute_sunburst, render_sunburst_line
from disktide.viz.treemap import compute_layout, render_line
from disktide.widgets.cleanup_map import CleanupMap
from disktide.widgets.growth_heatmap import GrowthHeatmap

NEW_NAMES = (("new-stuff", 50_000), ("data", 30_000), ("docs", 20_000))
CJK_NAMES = (("数据集", 50_000), ("文档资料", 30_000), ("docs", 20_000))
CJK_PATH = "/data/项目/数据集/原始数据文件夹"
DEEP_CJK_PATH = f"{CJK_PATH}/第二层目录/更多的数据"


def _tree(names) -> FSNode:
    """One directory per name, holding three files of distinct sizes."""
    root = FSNode(name="root", path="/root", size=0, own_size=0, is_dir=True, depth=0)
    for name, size in names:
        directory = FSNode(
            name=name, path=f"/root/{name}", size=0, own_size=0,
            is_dir=True, depth=1,
        )
        for index in range(3):
            share = size // (index + 2)
            directory.children.append(FSNode(
                name=f"{name}-{index}.bin",
                path=f"/root/{name}/{name}-{index}.bin",
                size=share, own_size=share, is_dir=False, depth=2, file_count=1,
            ))
        directory.size = sum(child.size for child in directory.children)
        directory.file_count = 3
        root.children.append(directory)
        root.size += directory.size
        root.file_count += 3
    root.dir_count = len(names)
    return root


def _delta(
    path: str,
    state: VisualState,
    old: int | None,
    new: int | None,
    *,
    is_dir: bool = True,
) -> VisualDelta:
    delta = (new or 0) - (old or 0)
    percent = delta / old * 100 if old else None
    return VisualDelta(path, state, old, new, delta, percent, is_dir)


def _text(segments) -> str:
    return "".join(segment.text for segment in segments)


class TestVisibleWidth:
    def test_a_wide_glyph_is_two_cells(self):
        assert visible_width("＋") == 2
        assert visible_width("数据集") == 6

    def test_a_vs15_still_takes_none(self):
        assert visible_width(PARTIAL) == 1
        assert visible_width(f"数据 {PARTIAL}") == 6


class TestMonitorSummary:
    def test_a_150_column_screen_keeps_the_trend_marks(self):
        # The box is 114 cells there: the *character* count of the keys row
        # with `Shift+←/→ pan` on it, and one short of its cells.
        box = SimpleNamespace(size=SimpleNamespace(width=114))
        screen = SimpleNamespace(
            query_one=lambda *_args: box,
            _fit_clauses=MonitorScreen._fit_clauses,
        )
        summary = MonitorScreen._history_summary(
            screen,
            "Collection active · 5 canonical point(s)",
            "Pair #4 → #5 · full confidence",
        )
        rows = summary.split("\n")
        assert len(rows) == 4
        assert rows[3].startswith("Trend marks:")
        assert all(cell_len(row) <= 114 for row in rows), rows

    def test_the_keys_row_fits_at_every_width(self):
        clauses = [legend_text(), "b/v set pair", "l latest", "z zoom", "Shift+←/→ pan"]
        for width in range(1, 140):
            row = MonitorScreen._fit_clauses(clauses, width)
            assert cell_len(row) <= width, (width, row)


class TestElision:
    @pytest.mark.parametrize(
        "text", (CJK_PATH, "＋ new  ▲ growth  ▼ shrink", f"/srv/数据/{PARTIAL}缓存")
    )
    def test_the_result_fits_in_cells(self, text):
        # Measured with Rich directly, not with the `visible_width` the
        # croppers themselves use: the two used to agree on the wrong answer.
        for width in range(1, cell_len(text) + 2):
            assert cell_len(elide_path(text, width)) <= width, width
            assert cell_len(elide_text(text, width)) <= width, width

    def test_a_cjk_path_still_keeps_its_tail(self):
        fitted = elide_path(CJK_PATH, 12)
        assert fitted.startswith("…")
        assert fitted.endswith("文件夹")
        assert cell_len(fitted) <= 12

    def test_a_cut_never_strands_a_vs15(self):
        text = f"数据{PARTIAL}缓存"
        for width in range(1, visible_width(text) + 2):
            for fitted in (elide_path(text, width), elide_text(text, width)):
                index = fitted.find(VS15)
                assert index == -1 or (index > 0 and fitted[index - 1] == "◐"), (
                    width, fitted,
                )


class TestSunburstRows:
    @pytest.mark.parametrize("width,height", ((90, 40), (75, 34), (60, 26)))
    def test_a_new_directory_in_diff_mode(self, width, height):
        visuals = {
            "/root/new-stuff": _delta("/root/new-stuff", VisualState.NEW, None, 50_000),
            "/root/data": _delta("/root/data", VisualState.GROWTH, 20_000, 30_000),
        }
        layout = compute_sunburst(_tree(NEW_NAMES), width, height, visuals=visuals)
        rows = [_text(render_sunburst_line(layout, y)) for y in range(height)]
        for y, row in enumerate(rows):
            assert cell_len(row) == width, (y, row)
        # Drawn whole, not mended into spaces: the `＋` is still there.
        for label in layout.labels:
            assert label.text in rows[label.char_y], label
        assert any("＋ new" in row for row in rows[layout.legend_start_y:])

    @pytest.mark.parametrize("width,height", ((90, 40), (75, 34)))
    def test_cjk_names(self, width, height):
        layout = compute_sunburst(_tree(CJK_NAMES), width, height)
        rows = [_text(render_sunburst_line(layout, y)) for y in range(height)]
        for y, row in enumerate(rows):
            assert cell_len(row) == width, (y, row)
        for label in layout.labels:
            assert label.text in rows[label.char_y], label

    def test_the_diff_legend_columns_line_up(self):
        removed = "/root/docs/docs-2.bin"
        visuals = {
            "/root/new-stuff": _delta("/root/new-stuff", VisualState.NEW, None, 50_000),
            "/root/data": _delta("/root/data", VisualState.GROWTH, 20_000, 30_000),
            "/root/docs": _delta("/root/docs", VisualState.SHRINK, 30_000, 20_000),
            removed: _delta(removed, VisualState.REMOVED, 5_000, None, is_dir=False),
        }
        layout = compute_sunburst(_tree(NEW_NAMES), 90, 40, visuals=visuals)
        # growth | shrink over new | removed: the second column starts at
        # the same cell on both rows.
        assert len(layout.legend_lines) == 2
        assert {cell_len(line[0][0]) for line in layout.legend_lines} == {13}


class TestTreemapRows:
    @pytest.mark.parametrize("width,height", ((90, 40), (60, 26)))
    def test_cjk_names(self, width, height):
        layout = compute_layout(_tree(CJK_NAMES), width, height)
        rows = [_text(render_line(layout, y)) for y in range(height)]
        for y, row in enumerate(rows):
            assert cell_len(row) == width, (y, row)
        assert any("数据集" in row for row in rows)

    def test_a_new_file_s_delta(self):
        path = "/root/new-stuff/new-stuff-0.bin"
        visuals = {
            "/root/new-stuff": _delta("/root/new-stuff", VisualState.NEW, None, 50_000),
            path: _delta(path, VisualState.NEW, None, 25_000, is_dir=False),
        }
        layout = compute_layout(_tree(NEW_NAMES), 90, 40, visuals=visuals)
        rows = [_text(render_line(layout, y)) for y in range(40)]
        for y, row in enumerate(rows):
            assert cell_len(row) == 90, (y, row)
        assert any("＋ +24.4 KiB" in row for row in rows)


class _Host(App):
    def __init__(self, widget: Widget) -> None:
        super().__init__()
        self._widget = widget

    def compose(self) -> ComposeResult:
        yield self._widget


def _row_widths(widget: Widget, size: tuple[int, int], load) -> list[tuple[int, int]]:
    """(cells drawn, widget width) for every row of `widget` once `load` ran."""
    widths: list[tuple[int, int]] = []

    async def go() -> None:
        async with _Host(widget).run_test(size=size) as pilot:
            load(widget)
            await pilot.pause()
            widths.extend(
                (widget.render_line(y).cell_length, widget.size.width)
                for y in range(widget.size.height)
            )

    asyncio.run(go())
    return widths


def _heatmap() -> GrowthHeatmapModel:
    stamp = datetime(2026, 9, 1, tzinfo=timezone.utc)
    cells = (
        # A path that appeared empty: a delta of 0 has no intensity, so the
        # cell falls back to the state's own glyph, which for new is `＋`.
        HeatmapCell(VisualState.NEW, 0),
        HeatmapCell(VisualState.NEW, 4096, intensity=2),
        HeatmapCell(VisualState.GROWTH, 1024, intensity=1),
        HeatmapCell(VisualState.UNCHANGED, 0),
        HeatmapCell(VisualState.MISSING, None),
        HeatmapCell(VisualState.SHRINK, -512),
    )
    return GrowthHeatmapModel(
        intervals=tuple(
            HeatmapInterval(index, index + 1, stamp, {}, root_path="/data")
            for index in range(1, len(cells) + 1)
        ),
        rows=(
            HeatmapRow(DEEP_CJK_PATH, cells, 0.5, 2, 5_120, 4_096),
            HeatmapRow("/data/docs", cells, 0.5, 2, 5_120, 4_096),
        ),
        truncated_paths=3,
    )


@pytest.mark.parametrize("size", ((100, 14), (50, 8)), ids=("matrix", "summary"))
def test_heatmap_rows_are_the_widget_width(size):
    widths = _row_widths(
        GrowthHeatmap(), size, lambda heatmap: heatmap.set_model(_heatmap())
    )
    assert widths and all(cells == width for cells, width in widths), widths


@pytest.mark.parametrize("size", ((100, 12), (50, 12)), ids=("map", "list"))
def test_cleanup_map_rows_are_the_widget_width(size):
    rule = CleanupRule(name="cache", description="build cache", patterns=["*"])
    targets = [
        CleanupTarget(
            path=f"{CJK_PATH}/缓存", size=50_000, rule=rule,
            age_days=40.0, score=9.0, confidence=0.9,
        ),
        CleanupTarget(
            path="/data/docs/tmp", size=10_000, rule=rule,
            age_days=5.0, score=3.0, confidence=0.5,
        ),
    ]
    widths = _row_widths(
        CleanupMap(), size, lambda cleanup: cleanup.set_targets(targets)
    )
    assert widths and all(cells == width for cells, width in widths), widths
