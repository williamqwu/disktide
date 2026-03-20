"""Welcome screen with path picker and filesystem completion."""

from __future__ import annotations

import os
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, Horizontal
from textual.screen import Screen
from textual.suggester import Suggester
from textual.widgets import Static, Input, Button, Checkbox

from fs_monitor import __version__
from fs_monitor.config import load_config, save_config


class PathSuggester(Suggester):
    """Filesystem path completion suggester providing inline ghost text."""

    def __init__(self) -> None:
        super().__init__(use_cache=False, case_sensitive=True)

    async def get_suggestion(self, value: str) -> str | None:
        if not value:
            return None

        expanded = os.path.expanduser(value)

        # Split into parent directory and prefix
        if expanded.endswith("/"):
            parent_dir = expanded
            prefix = ""
        else:
            parent_dir = os.path.dirname(expanded)
            prefix = os.path.basename(expanded)

        if not parent_dir or not os.path.isdir(parent_dir):
            return None

        try:
            for entry in os.scandir(parent_dir):
                name = entry.name
                if prefix and not name.startswith(prefix):
                    continue
                if not prefix and name.startswith("."):
                    continue
                # Return first match with trailing / for dirs
                suffix = "/" if entry.is_dir(follow_symlinks=False) else ""
                result = os.path.join(parent_dir, name) + suffix
                # Preserve ~ prefix in suggestion
                if value.startswith("~"):
                    home = os.path.expanduser("~")
                    if result.startswith(home):
                        result = "~" + result[len(home):]
                return result
        except PermissionError:
            return None

        return None


def _get_completions(value: str) -> list[str]:
    """Get all matching filesystem completions for a value."""
    if not value:
        return []

    expanded = os.path.expanduser(value)

    if expanded.endswith("/"):
        parent_dir = expanded
        prefix = ""
    else:
        parent_dir = os.path.dirname(expanded)
        prefix = os.path.basename(expanded)

    if not parent_dir or not os.path.isdir(parent_dir):
        return []

    matches = []
    try:
        for entry in os.scandir(parent_dir):
            name = entry.name
            if prefix and not name.startswith(prefix):
                continue
            if not prefix and name.startswith("."):
                continue
            suffix = "/" if entry.is_dir(follow_symlinks=False) else ""
            full = os.path.join(parent_dir, name) + suffix
            if value.startswith("~"):
                home = os.path.expanduser("~")
                if full.startswith(home):
                    full = "~" + full[len(home):]
            matches.append(full)
    except PermissionError:
        pass

    return sorted(matches)


_MAX_COMPLETIONS_DISPLAY = 15


class WelcomeScreen(Screen[str]):
    """Welcome screen with path selection and quick reference."""

    BINDINGS = [
        Binding("escape", "quit_app", "Quit", show=False),
    ]

    def compose(self) -> ComposeResult:
        config = load_config()
        saved_path = config.ui.default_scan_path
        default_path = saved_path or str(Path.home()) + "/"
        has_saved = saved_path is not None

        with Vertical(id="welcome-dialog"):
            yield Static(
                f"[bold]fsmonitor-cli[/bold] v{__version__}\n"
                "Interactive terminal disk usage explorer",
                id="welcome-title",
            )
            yield Static(
                "[dim]Commands:[/dim]  "
                "[bold]scan[/bold] <path>  "
                "[bold]watch[/bold] <path>  "
                "[bold]cleanup[/bold] <path>\n"
                "[dim]Keys:[/dim]      "
                "[bold]E[/bold]xplorer  "
                "[bold]C[/bold]leanup  "
                "[bold]M[/bold]onitor  "
                "[bold]?[/bold] Settings  "
                "[bold]Q[/bold]uit",
                id="welcome-commands",
            )
            yield Static(
                "Path to explore ([dim]→ to accept suggestion[/dim]):",
                id="welcome-path-label",
            )
            yield Input(
                value=default_path,
                placeholder="Enter a directory path...",
                suggester=PathSuggester(),
                id="welcome-path-input",
            )
            yield Static("", id="welcome-completions")
            yield Checkbox(
                "Save as default path",
                value=has_saved,
                id="welcome-save-default",
            )
            with Horizontal(classes="button-row"):
                yield Button("Explore", variant="primary", id="welcome-explore")
                yield Button("Quit", variant="default", id="welcome-quit")

    def on_mount(self) -> None:
        path_input = self.query_one("#welcome-path-input", Input)
        path_input.cursor_position = len(path_input.value)
        path_input.focus()
        self._update_completions(path_input.value)

    def _update_completions(self, value: str) -> None:
        """Update the completions display for the given input value."""
        comp_widget = self.query_one("#welcome-completions", Static)
        completions = _get_completions(value)

        if not completions:
            comp_widget.update("")
            return

        names: list[str] = []
        for c in completions[:_MAX_COMPLETIONS_DISPLAY]:
            name = os.path.basename(c.rstrip("/"))
            if c.endswith("/"):
                names.append(f"[bold]{name}/[/bold]")
            else:
                names.append(f"[dim]{name}[/dim]")

        display = "  ".join(names)
        if len(completions) > _MAX_COMPLETIONS_DISPLAY:
            display += f"  [dim]… +{len(completions) - _MAX_COMPLETIONS_DISPLAY} more[/dim]"
        comp_widget.update(display)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "welcome-path-input":
            self._update_completions(event.value)

    def _submit_path(self) -> None:
        """Validate and submit the selected path."""
        path_input = self.query_one("#welcome-path-input", Input)
        raw = path_input.value.strip()
        if not raw:
            self.notify("Please enter a path", severity="error")
            return

        resolved = Path(os.path.expanduser(raw)).resolve()
        if not resolved.is_dir():
            self.notify(f"Not a directory: {resolved}", severity="error")
            return

        save_checkbox = self.query_one("#welcome-save-default", Checkbox)
        if save_checkbox.value:
            config = load_config()
            config.ui.default_scan_path = str(resolved)
            save_config(config)

        self.dismiss(str(resolved))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "welcome-explore":
            self._submit_path()
        elif event.button.id == "welcome-quit":
            self.app.exit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "welcome-path-input":
            self._submit_path()

    def action_quit_app(self) -> None:
        self.app.exit()
