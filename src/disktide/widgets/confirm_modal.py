"""Small yes/no confirmation modal."""

from __future__ import annotations

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static
from rich.text import Text


class ConfirmModal(ModalScreen[bool]):
    """Generic confirmation dialog. Dismisses True on yes, False on no/escape."""

    DEFAULT_CSS = """
    ConfirmModal {
        align: center middle;
    }

    #confirm-dialog {
        width: 50;
        height: auto;
        background: $surface;
        border: thick $primary;
        padding: 1 2;
    }

    #confirm-message {
        height: auto;
        padding: 0 0 1 0;
    }

    #confirm-hint {
        color: $text-muted;
        padding-bottom: 1;
    }

    .button-row {
        height: 3;
        align: center middle;
    }

    #btn-yes {
        margin-right: 2;
    }
    """

    # Built-in bindings; callers can pass extra keys via confirm_keys
    # (e.g. the originating key "r" or "q") so the user can press the
    # same key twice to confirm — convenient muscle memory.
    BINDINGS = [
        Binding("y", "confirm", "Yes", show=True),
        Binding("n", "cancel", "No", show=True),
        Binding("escape", "cancel", "No", show=False),
    ]

    def __init__(
        self,
        message: str,
        title: str = "Confirm",
        confirm_keys: tuple[str, ...] = (),
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._message = message
        self._title = title
        self._extra_confirm_keys = tuple(
            k for k in confirm_keys if k not in ("y", "n", "escape")
        )

    def on_key(self, event: events.Key) -> None:
        # Caller-supplied confirm keys (e.g. "r" / "q") also confirm —
        # convenient muscle memory: press the same key twice to commit.
        if event.key in self._extra_confirm_keys:
            event.stop()
            self.action_confirm()

    def compose(self) -> ComposeResult:
        confirm_hint = "y"
        if self._extra_confirm_keys:
            confirm_hint = "/".join(("y",) + self._extra_confirm_keys)
        hint = f"Press {confirm_hint} to confirm, n/Esc to cancel"
        with Vertical(id="confirm-dialog"):
            yield Static(Text(self._title, style="bold"))
            yield Static(self._message, id="confirm-message")
            yield Static(Text(hint, style="dim"), id="confirm-hint")
            with Horizontal(classes="button-row"):
                yield Button("Yes", variant="primary", id="btn-yes")
                yield Button("No", variant="default", id="btn-no")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "btn-yes")
