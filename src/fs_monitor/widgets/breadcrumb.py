"""Path breadcrumb navigation bar."""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static
from rich.text import Text


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

    current_path: reactive[str] = reactive("")

    class PathClicked(Message):
        """Posted when a breadcrumb segment is clicked."""

        def __init__(self, path: str) -> None:
            super().__init__()
            self.path = path

    def __init__(self, path: str = "", **kwargs):
        super().__init__(**kwargs)
        self.current_path = path

    def render(self) -> Text:
        text = Text()

        if not self.current_path:
            text.append("  /", style="bold")
            return text

        parts = Path(self.current_path).parts
        accumulated = ""

        text.append("  ", style="")

        for i, part in enumerate(parts):
            if i == 0:
                accumulated = part
            else:
                accumulated = str(Path(accumulated) / part)

            if i > 0:
                text.append(" > ", style="dim")

            if i == len(parts) - 1:
                text.append(part, style="bold cyan")
            else:
                text.append(part, style="blue underline")

        return text

    def update_path(self, path: str) -> None:
        """Update the displayed path."""
        self.current_path = path
