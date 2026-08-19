"""Welcome screen with path picker and filesystem completion."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, Horizontal
from textual.screen import Screen
from textual.suggester import Suggester
from textual.widgets import Static, Input, Button, Checkbox

from fs_monitor import APP_NAME, __version__

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


@dataclass
class _Suggestion:
    """A named path suggestion for the welcome screen."""
    label: str
    path: str


def _build_suggestions(
    cwd: str,
    saved: str | None,
    last_visited: str | None,
    recent: list[str] | None = None,
) -> list[_Suggestion]:
    """Build a deduplicated suggestion list from all available path sources."""
    suggestions: list[_Suggestion] = []
    seen: set[str] = set()

    def _add(label: str, path: str) -> None:
        resolved = str(Path(path).resolve())
        if resolved not in seen:
            seen.add(resolved)
            suggestions.append(_Suggestion(label=label, path=path))

    _add("Current directory", cwd)
    if saved is not None:
        _add("Saved default", saved)
    if last_visited is not None:
        _add("Last visited", last_visited)
    if recent:
        for p in recent:
            _add("Recent", p)

    return suggestions


class WelcomeScreen(Screen[tuple[str, bool]]):
    """Welcome screen with single path input and suggestion cycling."""

    DEFAULT_CSS = """
    #welcome-dialog {
        padding: 2 4;
        max-width: 80;
    }

    #welcome-title {
        text-align: center;
        padding: 1 0;
        text-style: bold;
    }

    #welcome-commands {
        padding: 1 0;
    }

    #suggestion-list {
        height: auto;
        padding: 0 0 1 0;
    }

    .completions-display {
        max-height: 4;
        color: $text-muted;
    }

    #welcome-tab-hint {
        text-align: center;
        padding: 1 0;
    }

    #welcome-save-default {
        padding: 0 0 1 0;
    }

    .button-row {
        height: auto;
        align: center middle;
    }

    .button-row Button {
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("escape", "quit_app", "Quit", show=False),
    ]

    def __init__(
        self,
        cwd_path: str = "",
        saved_path: str | None = None,
        last_visited_path: str | None = None,
        recent_paths: list[str] | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._cwd_path = cwd_path or str(Path.home()) + "/"
        self._saved_path = saved_path
        self._last_visited_path = last_visited_path
        self._recent_paths = recent_paths or []
        self._suggestions: list[_Suggestion] = []
        self._suggestion_idx: int = 0

    def compose(self) -> ComposeResult:
        self._suggestions = _build_suggestions(
            self._cwd_path,
            self._saved_path,
            self._last_visited_path,
            self._recent_paths,
        )

        with Vertical(id="welcome-dialog"):
            yield Static(
                f"[bold]{APP_NAME}[/bold] v{__version__}\n"
                "Interactive terminal disk usage explorer",
                id="welcome-title",
            )
            yield Static(
                "[dim]TUI keys:[/dim]  "
                "[bold]E[/bold] Explorer  "
                "[bold]M[/bold] Monitor  "
                "[bold]F[/bold] FS Overview  "
                "[bold]?[/bold] Settings  "
                "[bold]Q[/bold] Quit\n"
                "[dim]CLI:[/dim]       "
                f"{APP_NAME} [bold]scan[/bold] | [bold]watch[/bold] | "
                "[bold]cleanup[/bold] <path>  "
                "[dim](outside TUI)[/dim]",
                id="welcome-commands",
            )

            # Suggestion list: all options visible, active one in orange
            yield Static(
                self._render_suggestions(),
                id="suggestion-list",
            )
            yield Input(
                value=self._suggestions[0].path if self._suggestions else "",
                placeholder="Enter a directory path...",
                suggester=PathSuggester(),
                id="path-input",
            )
            yield Static("", id="completions-display", classes="completions-display")
            yield Static(
                "[dim][bold]Enter[/bold] explore  "
                "[bold]\u2192[/bold] accept suggestion  "
                "[bold]\u2191\u2193[/bold] switch paths[/dim]",
                id="welcome-tab-hint",
            )

            yield Checkbox(
                "Save as default path",
                value=False,
                id="welcome-save-default",
            )
            with Horizontal(classes="button-row"):
                yield Button("Explore", variant="primary", id="welcome-explore")
                yield Button("Quit", variant="default", id="welcome-quit")

    def _render_suggestions(self) -> str:
        """Render all suggestion rows as a single Rich-markup string."""
        lines: list[str] = []
        for i, s in enumerate(self._suggestions):
            if i == self._suggestion_idx:
                lines.append(f"[bold yellow]\u25b6 {s.label}: {s.path}[/]")
            else:
                lines.append(f"[dim]  {s.label}: {s.path}[/]")
        return "\n".join(lines)

    def on_mount(self) -> None:
        path_input = self.query_one("#path-input", Input)
        path_input.cursor_position = len(path_input.value)
        path_input.focus()
        self._update_completions(path_input.value)

    def on_key(self, event: events.Key) -> None:
        """Cycle through path suggestions with Up/Down when input is focused."""
        focused = self.focused
        if not isinstance(focused, Input) or focused.id != "path-input":
            return
        if len(self._suggestions) <= 1:
            return

        if event.key == "up":
            self._cycle_suggestion(-1)
            event.prevent_default()
            event.stop()
        elif event.key == "down":
            self._cycle_suggestion(1)
            event.prevent_default()
            event.stop()

    def _cycle_suggestion(self, direction: int) -> None:
        """Move to the next/previous suggestion and update the UI."""
        total = len(self._suggestions)
        self._suggestion_idx = (self._suggestion_idx + direction) % total
        s = self._suggestions[self._suggestion_idx]

        self.query_one("#suggestion-list", Static).update(
            self._render_suggestions()
        )

        path_input = self.query_one("#path-input", Input)
        path_input.value = s.path
        path_input.cursor_position = len(s.path)

        self._update_completions(s.path)

    def _update_completions(self, value: str) -> None:
        """Update the completions display for the current input value."""
        try:
            comp_widget = self.query_one("#completions-display", Static)
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
            display += (
                f"  [dim]... +{len(completions) - _MAX_COMPLETIONS_DISPLAY} more[/dim]"
            )
        comp_widget.update(display)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "path-input":
            self._update_completions(event.value)

    def _submit_path(self) -> None:
        """Validate and submit the current path."""
        path_input = self.query_one("#path-input", Input)
        raw = path_input.value.strip()
        if not raw:
            self.notify("Please enter a path", severity="error")
            return

        resolved = Path(os.path.expanduser(raw)).resolve()
        if not resolved.is_dir():
            self.notify(f"Not a directory: {resolved}", severity="error")
            return

        save_default = self.query_one("#welcome-save-default", Checkbox).value
        self.dismiss((str(resolved), save_default))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "welcome-explore":
            self._submit_path()
        elif event.button.id == "welcome-quit":
            self.app.exit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "path-input":
            self._submit_path()

    def action_quit_app(self) -> None:
        self.app.exit()
