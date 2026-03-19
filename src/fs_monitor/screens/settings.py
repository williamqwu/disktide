"""Configuration screen."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, Static, Switch, Label
from textual.containers import Horizontal

from fs_monitor.config import AppConfig


class SettingsScreen(Screen):
    """Configuration settings screen."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back", show=True),
    ]

    DEFAULT_CSS = """
    SettingsScreen {
        layout: vertical;
    }

    .setting-row {
        height: 3;
        padding: 0 2;
    }

    .setting-label {
        width: 30;
    }

    #settings-container {
        padding: 1 2;
    }
    """

    def __init__(self, config: AppConfig, **kwargs):
        super().__init__(**kwargs)
        self._config = config

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="settings-container"):
            yield Static("Settings", classes="title")
            yield Static("")

            yield Static("Scan Settings", classes="section-title")
            with Horizontal(classes="setting-row"):
                yield Label("Show hidden files", classes="setting-label")
                yield Switch(value=self._config.ui.show_hidden, id="show-hidden")

            with Horizontal(classes="setting-row"):
                yield Label("Follow symlinks", classes="setting-label")
                yield Switch(value=self._config.scan.follow_symlinks, id="follow-symlinks")

            yield Static("")
            yield Static("Cleanup Settings", classes="section-title")
            with Horizontal(classes="setting-row"):
                yield Label("Confirm dangerous deletions", classes="setting-label")
                yield Switch(
                    value=self._config.cleanup.require_confirm_dangerous,
                    id="confirm-dangerous",
                )

            yield Static("")
            yield Static("UI Settings", classes="section-title")
            with Horizontal(classes="setting-row"):
                yield Label("Default visualization", classes="setting-label")
                yield Static(self._config.ui.default_viz)

        yield Footer()

    def on_switch_changed(self, event: Switch.Changed) -> None:
        if event.switch.id == "show-hidden":
            self._config.ui.show_hidden = event.value
        elif event.switch.id == "follow-symlinks":
            self._config.scan.follow_symlinks = event.value
        elif event.switch.id == "confirm-dangerous":
            self._config.cleanup.require_confirm_dangerous = event.value
