"""The path, printed large, with mouse reporting out of the way.

This is the fallback under `y`, and it exists because half the routes a
copy can take have no reply. OSC 52 is a write with no answer; a terminal
that drops it -- macOS Terminal.app, JupyterLab's xterm.js 6, which ships
without `@xterm/addon-clipboard` -- does so silently, and the app cannot
tell that case from a copy that worked. So `Y` gives the user the one
thing that always works: the text, on screen, selectable.

Selectable is the part that needs help. With mouse reporting on, a drag
is an event the app consumes and the terminal never sees, so there is
nothing to select. Outside this modal the escape hatch is a modifier --
Shift+drag in most terminals and in xterm.js, **Alt**+drag in hterm
(Open OnDemand's shell app; `onMouse_` short-circuits on `e.altKey`) --
which is one more thing to know at the moment a user is already stuck. So
the modal turns reporting off for as long as it is open, through
`LinuxDriver._disable_mouse_support` / `_enable_mouse_support`. Those are
private, hence the `getattr` and the try/except: on `HeadlessDriver`
(which `run_test` uses) neither exists, and a missing one has to cost the
mouse toggle rather than the modal.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static
from rich.text import Text


class PathModal(ModalScreen[None]):
    """Show one path for hand selection, mouse reporting off while open."""

    DEFAULT_CSS = """
    PathModal {
        align: center middle;
    }

    #path-dialog {
        width: 80%;
        max-width: 100;
        height: auto;
        background: $surface;
        border: double $primary;
        padding: 1 2;
    }

    #path-value {
        height: auto;
        padding: 1 0;
    }

    #path-hint {
        color: $text-muted;
        height: auto;
    }
    """

    BINDINGS = [
        Binding("escape", "close", "Close", show=False, id="modal.path_close"),
        Binding("enter", "close", "Close", show=False, id="modal.path_close_enter"),
        Binding("q", "close", "Close", show=False, id="modal.path_close_q"),
    ]

    def __init__(self, path: str, **kwargs):
        super().__init__(**kwargs)
        self._path = path

    def compose(self) -> ComposeResult:
        hint = (
            "Mouse reporting is off while this is open: drag to select, then "
            "copy with your terminal's shortcut (Ctrl+Shift+C, ⌘C). "
            "Esc closes."
        )
        with Vertical(id="path-dialog"):
            yield Static(Text("Path (select and copy by hand)", style="bold"))
            # Plain text, not a `Text`: `Static.ALLOW_SELECT` is already
            # True, so Textual's own selection and its ctrl+c work in here
            # as well -- and that copy now takes the routes `y` takes,
            # since `DiskTideApp.copy_to_clipboard` is what it calls.
            yield Static(self._path, id="path-value")
            yield Static(Text(hint, style="dim"), id="path-hint")

    def on_mount(self) -> None:
        self._set_mouse_reporting(False)

    def on_unmount(self) -> None:
        self._set_mouse_reporting(True)

    def action_close(self) -> None:
        self.dismiss(None)

    def _set_mouse_reporting(self, enabled: bool) -> None:
        """Turn the terminal's mouse tracking on or off, if it can be.

        Private Textual API on purpose: there is no public way to do this
        at runtime, and the alternative is telling the user to restart the
        app with `--no-mouse` to read one path. Both methods early-return
        when the driver was started with `mouse=False`, so a user who
        already disabled the mouse is unaffected.
        `tests/test_explorer_qol.py` pins that the two names still exist.
        """
        name = "_enable_mouse_support" if enabled else "_disable_mouse_support"
        try:
            toggle = getattr(self.app._driver, name, None)
            if toggle is not None:
                toggle()
        except Exception:
            # A driver that cannot do this costs the user a modifier key,
            # not the modal they opened to read a path.
            pass
