"""Bounded path-by-time growth heatmap with ASCII-safe fallback."""

from __future__ import annotations

from rich.cells import set_cell_size
from rich.segment import Segment
from rich.style import Style
from textual.binding import Binding
from textual.message import Message
from textual.strip import Strip

from disktide.widgets import OpaqueStripMixin
from textual.widget import Widget

from disktide.domain.visualization import GrowthHeatmapModel, HeatmapCell, VisualState
from disktide.glyphs import visible_width
from disktide.pathdisplay import elide_path, relative_label
from disktide.presentation.tui.viewmodels.visualization import legend_text, visual_token
from disktide.viz.colors import delta_background


class GrowthHeatmap(OpaqueStripMixin, Widget, can_focus=True):
    """Render top-changing paths across bounded snapshot intervals."""

    BINDINGS = [
        Binding("up", "cursor_up", "Previous path", show=False, id="heatmap.up"),
        Binding("down", "cursor_down", "Next path", show=False, id="heatmap.down"),
        Binding("enter", "select_path", "Open path", show=False, id="heatmap.select"),
    ]

    DEFAULT_CSS = """
    GrowthHeatmap {
        width: 1fr;
        height: 1fr;
    }
    """

    class PathSelected(Message):
        def __init__(self, path: str):
            super().__init__()
            self.path = path

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._model: GrowthHeatmapModel | None = None
        self._cursor = 0

    def _row_label(self, path: str) -> str:
        """A row's path as it reads inside a matrix about one root.

        Every row is under the monitored root, so the absolute form spends
        the column on the prefix they all share -- 24 of 34 cells were
        `…/scratchpad/stage/home` on the tree this was measured against --
        and crops away the part that tells the rows apart.
        """
        model = self._model
        root = next(
            (
                interval.root_path
                for interval in (model.intervals if model else ())
                if interval.root_path
            ),
            None,
        )
        return relative_label(path, root)

    @property
    def selected_path(self) -> str | None:
        model = self._model
        if model is None or not model.rows:
            return None
        return model.rows[self._cursor].path

    def set_model(self, model: GrowthHeatmapModel | None) -> None:
        self._model = model
        self._cursor = min(self._cursor, max(0, len(model.rows) - 1)) if model else 0
        self.refresh()

    def set_selected_path(self, path: str | None) -> None:
        if path is None or self._model is None:
            return
        for index, row in enumerate(self._model.rows):
            if row.path == path:
                self._cursor = index
                self.refresh()
                return

    def action_cursor_up(self) -> None:
        self._cursor = max(0, self._cursor - 1)
        self.refresh()

    def action_cursor_down(self) -> None:
        if self._model is None:
            return
        self._cursor = min(max(0, len(self._model.rows) - 1), self._cursor + 1)
        self.refresh()

    def action_select_path(self) -> None:
        path = self.selected_path
        if path is not None:
            self.post_message(self.PathSelected(path))

    def render_content_line(self, y: int) -> Strip:
        width = self.size.width
        height = self.size.height
        model = self._model
        if width <= 0 or height <= 0:
            return Strip.blank(max(0, width))
        if model is None or not model.rows:
            message = "Growth heatmap needs at least two changing snapshots"
            if y == height // 2:
                return Strip([Segment(message[:width].center(width), Style(dim=True))])
            return Strip.blank(width)
        if width < 55 or height < 10:
            return self._render_summary_line(y, width, model)
        return self._render_matrix_line(y, width, height, model)

    def _render_summary_line(
        self, y: int, width: int, model: GrowthHeatmapModel
    ) -> Strip:
        if y == 0:
            return Strip(
                [Segment("Persistent growth summary".ljust(width), Style(bold=True))]
            )
        index = y - 1
        if 0 <= index < min(len(model.rows), max(0, self.size.height - 2)):
            row = model.rows[index]
            token = visual_token(VisualState.GROWTH)
            marker = ">" if index == self._cursor else " "
            text = (
                f"{marker}{token.glyph} {self._row_label(row.path)}  "
                f"{row.consistency:.0%} · streak {row.longest_streak}"
            )
            return Strip(
                [Segment(set_cell_size(text, width), Style(color=token.color))]
            )
        if y == self.size.height - 1:
            return Strip(
                [Segment(set_cell_size(legend_text(), width), Style(dim=True))]
            )
        return Strip.blank(width)

    def _render_matrix_line(
        self,
        y: int,
        width: int,
        height: int,
        model: GrowthHeatmapModel,
    ) -> Strip:
        interval_count = len(model.intervals)
        stats_width = 15
        path_width = max(14, min(34, width - interval_count - stats_width - 2))
        visible_intervals = max(1, width - path_width - stats_width - 2)
        start = max(0, interval_count - visible_intervals)
        if y == 0:
            header = "Path".ljust(path_width) + "  "
            header += "".join("·" for _ in model.intervals[start:])
            header += "  consistency"
            return Strip(
                [Segment(set_cell_size(header, width), Style(bold=True, dim=True))]
            )

        row_index = y - 1
        if 0 <= row_index < min(len(model.rows), height - 2):
            row = model.rows[row_index]
            selected = row_index == self._cursor
            marker = ">" if selected else " "
            label = set_cell_size(
                marker + elide_path(self._row_label(row.path), path_width - 1),
                path_width,
            )
            segments = [
                Segment(label + "  ", Style(reverse=selected, bold=selected))
            ]
            for cell in row.cells[start:]:
                segments.append(self._cell_segment(cell))
            stats = f"  {row.consistency:>4.0%} s{row.longest_streak:<2}"
            segments.append(
                Segment(stats[:stats_width].ljust(stats_width), Style(dim=True))
            )
            used = path_width + 2 + len(row.cells[start:]) + stats_width
            if used < width:
                segments.append(Segment(" " * (width - used)))
            return Strip(segments)

        if y == height - 1:
            suffix = f"  +{model.truncated_paths} more" if model.truncated_paths else ""
            text = legend_text() + suffix
            return Strip([Segment(set_cell_size(text, width), Style(dim=True))])
        return Strip.blank(width)

    @staticmethod
    def _cell_segment(cell: HeatmapCell) -> Segment:
        token = visual_token(cell.state)
        glyph = token.glyph
        if cell.state in {VisualState.GROWTH, VisualState.NEW} and cell.intensity:
            glyph = "1234"[cell.intensity - 1]
        elif visible_width(glyph) != 1:
            # One cell per interval, and `＋` takes two.
            glyph = token.safe_glyph
        return Segment(
            glyph[:1],
            Style(
                color="white",
                bgcolor=delta_background(cell.state, max(1, cell.intensity)),
                # A cell whose snapshot pair was not read in full keeps the
                # direction it measured and is drawn desaturated. It used
                # to be repainted as PARTIAL outright, which cost a whole
                # monitored tree its growth information the moment one
                # directory under the root was unreadable.
                dim=cell.partial,
            ),
        )
