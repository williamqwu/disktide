"""Path breadcrumb navigation bar."""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static
from rich.text import Text

from disktide.glyphs import visible_width
from disktide.pathdisplay import ELLIPSIS, ellipsis
from disktide.rendering import denied_glyph, partial_glyph
from disktide.viz.colors import ink


class Breadcrumb(Widget):
    """A breadcrumb navigation bar showing the current path."""

    DEFAULT_CSS = """
    Breadcrumb {
        height: 1;
        dock: top;
        background: $surface;
        padding: 0 1;
    }
    """

    #: Cells the access glyph and its separator take on the right.
    _TAIL_COST = 3

    current_path: reactive[str] = reactive("")
    access: reactive[str] = reactive("full")  # "full" | "partial" | "denied"

    def __init__(self, path: str = "", **kwargs):
        super().__init__(**kwargs)
        self.current_path = path

    def render(self) -> Text:
        text = Text()

        if not self.current_path:
            text.append("  /", style="bold")
            return text

        parts = self._fitted_parts()

        text.append("  ", style="")

        for i, part in enumerate(parts):
            if i > 0:
                text.append(" > ", style="dim")

            if part is None:
                # The folded-away middle. It is never the last crumb, so
                # the directory the user is actually in stays legible.
                text.append(ellipsis(), style="dim")
            elif i == len(parts) - 1:
                text.append(part, style=ink("dir"))
            else:
                text.append(part, style=ink("crumb"))

        # Access state of the current (last) node — surfaced here so
        # visualizations (sunburst especially) don't have to fight for
        # space to show it.
        if self.access == "denied":
            text.append(f"  {denied_glyph()}", style=ink("error_strong"))
        elif self.access == "partial":
            text.append(f"  {partial_glyph()}", style=ink("warning_strong"))

        return text

    def _fitted_parts(self) -> list[str | None]:
        """Crumbs for the current path, middle folded away if it must be.

        A crumb trail cropped at the right edge -- what any overflow rule
        does -- keeps `/fs/project/PRJ0042` and drops the directory the user is
        standing in, which is the one crumb that was worth the row. So the
        leading crumbs go first, replaced by a single `…`, and the tail is
        kept whole. `None` marks that fold for `render`.

        The last crumb is never dropped: a row too narrow even for it is
        better spent on a cropped name than on nothing.
        """
        parts: list[str] = list(Path(self.current_path).parts)
        room = self.size.width - 2  # the two-space lead-in
        if self.access != "full":
            room -= self._TAIL_COST
        if room <= 0 or len(parts) <= 1:
            return list(parts)
        if self._crumb_width(parts) <= room:
            return list(parts)
        for start in range(1, len(parts)):
            folded: list[str | None] = [None, *parts[start:]]
            if self._crumb_width(folded) <= room:
                return folded
        return [parts[-1]]

    @staticmethod
    def _crumb_width(parts: list[str | None]) -> int:
        """Cells the trail takes, counting `" > "` between crumbs."""
        rendered = [ELLIPSIS if part is None else part for part in parts]
        return (
            sum(visible_width(part) for part in rendered)
            + 3 * (len(rendered) - 1)
        )

    def on_resize(self) -> None:
        # How many crumbs fit is a function of the width, so a narrower
        # panel has to re-fold rather than keep the trail it computed for
        # the old one.
        self.refresh()

    def update_path(self, path: str, access: str = "full") -> None:
        """Update the displayed path and the access state of the current node."""
        self.current_path = path
        self.access = access
