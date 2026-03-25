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
from fs_monitor.config import (
    load_config, save_config, get_effective_paths, set_effective_paths,
)

# Limit scandir iterations to avoid blocking on huge directories (e.g. /home).
_MAX_SCANDIR_ENTRIES = 200


class PathSuggester(Suggester):
    """Filesystem path completion suggester providing inline ghost text."""

    def __init__(self) -> None:
        super().__init__(use_cache=False, case_sensitive=True)

    async def get_suggestion(self, value: str) -> str | None:
        if not value:
            return None

        expanded = os.path.expanduser(value)

        if expanded.endswith("/"):
            parent_dir = expanded
            prefix = ""
        else:
            parent_dir = os.path.dirname(expanded)
            prefix = os.path.basename(expanded)

        if not parent_dir or not os.path.isdir(parent_dir):
            return None

        try:
            count = 0
            for entry in os.scandir(parent_dir):
                count += 1
                if count > _MAX_SCANDIR_ENTRIES:
                    break
                name = entry.name
                if prefix and not name.startswith(prefix):
                    continue
                if not prefix and name.startswith("."):
                    continue
                suffix = "/" if entry.is_dir(follow_symlinks=False) else ""
                result = os.path.join(parent_dir, name) + suffix
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
        count = 0
        for entry in os.scandir(parent_dir):
            count += 1
            if count > _MAX_SCANDIR_ENTRIES:
                break
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

# Input IDs that are path options on the welcome screen.
_PATH_INPUT_IDS = ("path-cwd", "path-saved", "path-last")

_INPUT_TO_COMPLETIONS = {
    "path-cwd": "completions-cwd",
    "path-saved": "completions-saved",
    "path-last": "completions-last",
}


class WelcomeScreen(Screen[str]):
    """Welcome screen with path selection and quick reference."""

    BINDINGS = [
        Binding("escape", "quit_app", "Quit", show=False),
    ]

    def __init__(
        self,
        cwd_path: str = "",
        saved_path: str | None = None,
        last_visited_path: str | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._cwd_path = cwd_path or str(Path.home()) + "/"
        self._saved_path = saved_path
        self._last_visited_path = last_visited_path

    def compose(self) -> ComposeResult:
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
                "[dim]Use [bold]Tab[/bold] to switch between path options, "
                "[bold]Enter[/bold] to explore, "
                "[bold]\u2192[/bold] to accept suggestion[/dim]",
                id="welcome-tab-hint",
            )

            # Option 1: Current directory (always shown)
            with Vertical(classes="path-option"):
                yield Static(
                    "[bold]Current directory[/bold]",
                    classes="path-option-label",
                )
                yield Input(
                    value=self._cwd_path,
                    placeholder="Enter a directory path...",
                    suggester=PathSuggester(),
                    id="path-cwd",
                )
                yield Static("", id="completions-cwd", classes="completions-display")

            # Option 2: Saved default (only if set)
            if self._saved_path is not None:
                with Vertical(classes="path-option"):
                    yield Static(
                        "[bold]Saved default[/bold]",
                        classes="path-option-label",
                    )
                    yield Input(
                        value=self._saved_path,
                        placeholder="Enter a directory path...",
                        suggester=PathSuggester(),
                        id="path-saved",
                    )
                    yield Static("", id="completions-saved", classes="completions-display")

            # Option 3: Last visited (only if set and differs from saved)
            if (
                self._last_visited_path is not None
                and self._last_visited_path != self._saved_path
                and self._last_visited_path != self._cwd_path
            ):
                with Vertical(classes="path-option"):
                    yield Static(
                        "[bold]Last visited[/bold]",
                        classes="path-option-label",
                    )
                    yield Input(
                        value=self._last_visited_path,
                        placeholder="Enter a directory path...",
                        suggester=PathSuggester(),
                        id="path-last",
                    )
                    yield Static("", id="completions-last", classes="completions-display")

            yield Checkbox(
                "Save as default path",
                value=self._saved_path is not None,
                id="welcome-save-default",
            )
            with Horizontal(classes="button-row"):
                yield Button("Explore", variant="primary", id="welcome-explore")
                yield Button("Quit", variant="default", id="welcome-quit")

    def on_mount(self) -> None:
        path_input = self.query_one("#path-cwd", Input)
        path_input.cursor_position = len(path_input.value)
        path_input.focus()
        self._update_completions(path_input.value, "completions-cwd")

    def _update_completions(self, value: str, completions_id: str) -> None:
        """Update the completions display for the given input value."""
        try:
            comp_widget = self.query_one(f"#{completions_id}", Static)
        except Exception:
            return

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
        comp_id = _INPUT_TO_COMPLETIONS.get(event.input.id)
        if comp_id:
            self._update_completions(event.value, comp_id)

    def _submit_from_input(self, input_widget: Input) -> None:
        """Validate and submit the path from the given input."""
        raw = input_widget.value.strip()
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
            paths = get_effective_paths(config)
            paths.default_scan_path = str(resolved)
            set_effective_paths(config, paths)
            save_config(config)

        self.dismiss(str(resolved))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "welcome-explore":
            # Submit from whichever path input is focused, or fall back to cwd
            focused = self.focused
            if isinstance(focused, Input) and focused.id in _PATH_INPUT_IDS:
                self._submit_from_input(focused)
            else:
                self._submit_from_input(self.query_one("#path-cwd", Input))
        elif event.button.id == "welcome-quit":
            self.app.exit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id in _PATH_INPUT_IDS:
            self._submit_from_input(event.input)

    def action_quit_app(self) -> None:
        self.app.exit()
