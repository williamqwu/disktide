"""Bounded Age/Size cleanup opportunity map with narrow-terminal fallback."""

from __future__ import annotations

from dataclasses import dataclass
from math import log1p
from statistics import median

from rich.cells import set_cell_size
from rich.segment import Segment
from rich.style import Style
from textual.binding import Binding
from textual.message import Message
from textual.strip import Strip

from disktide.widgets import OpaqueStripMixin
from disktide.viz.colors import ink
from textual.widget import Widget

from disktide.models.patterns import CleanupTarget, RiskLevel
from disktide.rendering import is_safe_rendering


@dataclass(frozen=True, slots=True)
class CleanupMapPoint:
    path: str
    size: int
    age_days: float
    category: str
    risk: RiskLevel
    confidence: float
    score: float
    x: int
    y: int


@dataclass(frozen=True, slots=True)
class CleanupMapModel:
    points: tuple[CleanupMapPoint, ...]
    omitted: int
    large_threshold: int
    old_threshold_days: float

    @property
    def high_value(self) -> tuple[CleanupMapPoint, ...]:
        return tuple(
            point
            for point in self.points
            if point.size >= self.large_threshold
            and point.age_days >= self.old_threshold_days
        )


def build_cleanup_map(
    targets: list[CleanupTarget],
    *,
    max_points: int = 80,
    width: int = 48,
    height: int = 8,
) -> CleanupMapModel:
    """Bound rendering cost while preserving the highest-value candidates."""
    bounded_width = max(2, width)
    bounded_height = max(2, height)
    ordered = sorted(
        targets,
        key=lambda target: (-target.score, -target.size, target.path),
    )
    selected = ordered[: max(1, max_points)]
    maximum_size = max((target.size for target in selected), default=1)
    maximum_age = max((target.age_days for target in selected), default=1.0)
    size_scale = max(log1p(maximum_size), 1.0)
    age_scale = max(log1p(maximum_age), 1.0)
    sizes = [target.size for target in selected]
    ages = [target.age_days for target in selected]
    large_threshold = int(median(sizes)) if sizes else 0
    old_threshold = max(30.0, float(median(ages)) if ages else 30.0)
    points = tuple(
        CleanupMapPoint(
            path=target.path,
            size=target.size,
            age_days=target.age_days,
            category=target.category,
            risk=target.risk,
            confidence=target.confidence,
            score=target.score,
            x=min(
                bounded_width - 1,
                int(log1p(max(0, target.size)) / size_scale * (bounded_width - 1)),
            ),
            y=min(
                bounded_height - 1,
                int(log1p(max(0.0, target.age_days)) / age_scale * (bounded_height - 1)),
            ),
        )
        for target in selected
    )
    return CleanupMapModel(
        points=points,
        omitted=max(0, len(targets) - len(selected)),
        large_threshold=large_threshold,
        old_threshold_days=old_threshold,
    )


class CleanupMap(OpaqueStripMixin, Widget, can_focus=True):
    """Keyboard-selectable cleanup map synchronized with the plan table."""

    BINDINGS = [
        Binding("left", "previous", "Previous candidate", show=False, id="map.previous"),
        Binding("up", "previous", "Previous candidate", show=False, id="map.previous_up"),
        Binding("right", "next", "Next candidate", show=False, id="map.next"),
        Binding("down", "next", "Next candidate", show=False, id="map.next_down"),
        Binding("enter", "select", "Select candidate", show=False, id="map.select"),
    ]

    DEFAULT_CSS = """
    CleanupMap {
        width: 1fr;
        height: 11;
        border: solid $primary;
        background: $surface;
    }

    CleanupMap:focus {
        border: double $accent;
    }
    """

    class PathSelected(Message):
        def __init__(self, path: str):
            super().__init__()
            self.path = path

    def __init__(self, *, max_points: int = 80, **kwargs):
        super().__init__(**kwargs)
        self._targets: list[CleanupTarget] = []
        self._max_points = max(1, max_points)
        self._cursor = 0
        self._cached_model: CleanupMapModel | None = None
        self._cached_dimensions: tuple[int, int] | None = None

    @property
    def selected_path(self) -> str | None:
        model = self._model()
        if not model.points:
            return None
        return model.points[min(self._cursor, len(model.points) - 1)].path

    def set_targets(self, targets: list[CleanupTarget]) -> None:
        selected = self.selected_path
        self._targets = list(targets)
        self._cached_model = None
        self._cached_dimensions = None
        self._cursor = 0
        if selected is not None:
            self.set_selected_path(selected)
        self.refresh()

    def set_selected_path(self, path: str | None) -> None:
        if path is None:
            return
        for index, point in enumerate(self._model().points):
            if point.path == path:
                self._cursor = index
                self.refresh()
                return

    def action_previous(self) -> None:
        if not self._targets:
            return
        self._cursor = max(0, self._cursor - 1)
        self.refresh()

    def action_next(self) -> None:
        count = len(self._model().points)
        if count:
            self._cursor = min(count - 1, self._cursor + 1)
            self.refresh()

    def action_select(self) -> None:
        path = self.selected_path
        if path is not None:
            self.post_message(self.PathSelected(path))

    def render_content_line(self, y: int) -> Strip:
        width = self.size.width
        height = self.size.height
        if width <= 0 or height <= 0:
            return Strip.blank(max(0, width))
        model = self._model()
        if not model.points:
            text = "No cleanup opportunities match enabled rule packs"
            if y == height // 2:
                return Strip([Segment(text[:width].center(width), Style(dim=True))])
            return Strip.blank(width)
        if width < 60 or height < 9 or is_safe_rendering():
            return self._render_summary_line(y, width, height, model)
        return self._render_map_line(y, width, height, model)

    def _model(self) -> CleanupMapModel:
        dimensions = (
            max(2, self.size.width - 2),
            max(2, self.size.height - 3),
        )
        if (
            self._cached_model is not None
            and self._cached_dimensions == dimensions
        ):
            return self._cached_model
        self._cached_model = build_cleanup_map(
            self._targets,
            max_points=self._max_points,
            width=dimensions[0],
            height=dimensions[1],
        )
        self._cached_dimensions = dimensions
        return self._cached_model

    def _render_summary_line(
        self,
        y: int,
        width: int,
        height: int,
        model: CleanupMapModel,
    ) -> Strip:
        if y == 0:
            title = "Age/Size opportunities · fallback list"
            return Strip([Segment(set_cell_size(title, width), Style(bold=True))])
        visible = min(len(model.points), max(0, height - 2))
        index = y - 1
        if 0 <= index < visible:
            point = model.points[index]
            selected = index == self._cursor
            marker = ">" if selected else " "
            text = (
                f"{marker} {point.score:>5.1f}  {point.age_days:>6.1f}d  "
                f"{point.confidence:>4.0%}  {point.path}"
            )
            return Strip(
                [Segment(set_cell_size(text, width), _point_style(point, selected))]
            )
        if y == height - 1:
            suffix = f" · +{model.omitted} omitted" if model.omitted else ""
            text = f"score · age · confidence · path{suffix}"
            return Strip([Segment(set_cell_size(text, width), Style(dim=True))])
        return Strip.blank(width)

    def _render_map_line(
        self,
        y: int,
        width: int,
        height: int,
        model: CleanupMapModel,
    ) -> Strip:
        plot_height = max(2, height - 2)
        if y == 0:
            selected = model.points[min(self._cursor, len(model.points) - 1)]
            text = (
                f"Age ↑ / Size →  selected {selected.score:.1f} · "
                f"{selected.age_days:.1f}d · {selected.confidence:.0%} · {selected.path}"
            )
            return Strip([Segment(set_cell_size(text, width), Style(bold=True))])
        if y == height - 1:
            suffix = f" · +{model.omitted} omitted" if model.omitted else ""
            text = "o safe  ^ moderate  ! dangerous  @ selected" + suffix
            return Strip([Segment(set_cell_size(text, width), Style(dim=True))])
        plot_y = plot_height - y
        cells: dict[int, tuple[CleanupMapPoint, bool]] = {}
        for index, point in enumerate(model.points):
            if point.y != plot_y:
                continue
            selected = index == self._cursor
            current = cells.get(point.x)
            if current is None or selected or point.score > current[0].score:
                cells[point.x] = (point, selected)
        segments: list[Segment] = []
        for x in range(width):
            value = cells.get(x)
            if value is None:
                segments.append(Segment("·" if x % 8 == 0 else " ", Style(dim=True)))
                continue
            point, selected = value
            glyph = "@" if selected else {
                RiskLevel.SAFE: "o",
                RiskLevel.MODERATE: "^",
                RiskLevel.DANGEROUS: "!",
            }[point.risk]
            segments.append(Segment(glyph, _point_style(point, selected)))
        return Strip(segments[:width])


def _point_style(point: CleanupMapPoint, selected: bool) -> Style:
    role = {
        RiskLevel.SAFE: "bar",
        RiskLevel.MODERATE: "warning",
        RiskLevel.DANGEROUS: "error",
    }[point.risk]
    return Style.parse(ink(role)) + Style(bold=selected, reverse=selected)
