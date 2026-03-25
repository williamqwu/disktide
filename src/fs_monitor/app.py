"""Main Textual App with mode/screen management."""

from __future__ import annotations

import os
from pathlib import Path

from textual.app import App
from textual.binding import Binding

from fs_monitor.config import (
    AppConfig, load_config, save_config,
    get_effective_paths, set_effective_paths,
)
from fs_monitor.viz.colors import set_color_scheme
from fs_monitor.storage.database import Database
from fs_monitor.screens.explorer import ExplorerScreen
from fs_monitor.screens.cleanup import CleanupScreen
from fs_monitor.screens.monitor import MonitorScreen
from fs_monitor.screens.settings import SettingsScreen


class FSMonitorApp(App):
    """Interactive terminal disk usage explorer."""

    TITLE = "fsmonitor-cli"
    SUB_TITLE = "Disk Usage Explorer"
    ENABLE_COMMAND_PALETTE = False

    BINDINGS = [
        Binding("e", "switch_mode('explorer')", "[E]xplorer [C]leanup [M]onitor", show=True, key_display="Mode"),
        Binding("c", "switch_mode('cleanup')", "Cleanup", show=False),
        Binding("m", "switch_mode('monitor')", "Monitor", show=False),
        Binding("question_mark", "push_screen('settings')", "Settings", show=True, key_display="?"),
        Binding("q", "quit", "Quit", show=True),
    ]

    def __init__(
        self,
        scan_path: str | None = None,
        config: AppConfig | None = None,
        show_welcome: bool = False,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._scan_path = str(Path(scan_path).resolve()) if scan_path else None
        self._config = config or load_config()
        self._db = Database()
        self._show_welcome = show_welcome

    def on_mount(self) -> None:
        set_color_scheme(self._config.ui.color_theme)

        if self._show_welcome:
            from fs_monitor.screens.welcome import WelcomeScreen

            paths = get_effective_paths(self._config)
            cwd = os.getcwd()

            self.push_screen(
                WelcomeScreen(
                    cwd_path=cwd,
                    saved_path=paths.default_scan_path,
                    last_visited_path=paths.last_visited_path,
                ),
                callback=self._on_welcome_result,
            )
        else:
            self._launch_explorer(self._scan_path or str(Path(".").resolve()))

    def _on_welcome_result(self, path: str | None) -> None:
        """Callback from WelcomeScreen with the chosen path."""
        if path is None or self._exit:
            return
        self._save_last_visited(path)
        self._launch_explorer(path)

    def _save_last_visited(self, path: str) -> None:
        """Persist the last-visited path to config."""
        paths = get_effective_paths(self._config)
        if paths.last_visited_path == path:
            return
        paths.last_visited_path = path
        set_effective_paths(self._config, paths)
        try:
            save_config(self._config)
        except OSError:
            pass

    def _launch_explorer(self, scan_path: str) -> None:
        """Install mode screens and push the explorer."""
        self._scan_path = scan_path

        self._explorer = ExplorerScreen(self._scan_path, config=self._config)
        self._cleanup = CleanupScreen()
        self._monitor = MonitorScreen(
            db=self._db, root_path=self._scan_path,
            strict_path=self._config.monitor.strict_path,
        )

        self.install_screen(self._explorer, name="explorer")
        self.install_screen(self._cleanup, name="cleanup")
        self.install_screen(self._monitor, name="monitor")
        self.install_screen(
            SettingsScreen(self._config, db=self._db), name="settings"
        )

        self.push_screen("explorer")

    def action_switch_mode(self, mode: str) -> None:
        """Switch between explorer/cleanup/monitor modes."""
        if mode == "explorer":
            self.switch_screen("explorer")
        elif mode == "cleanup":
            # Pass current root from explorer to cleanup
            if self._explorer._root is not None:
                self._cleanup.set_root(self._explorer._root)
            self.switch_screen("cleanup")
        elif mode == "monitor":
            self.switch_screen("monitor")
